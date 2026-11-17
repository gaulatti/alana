"""Run startup.sh against stub encoders and prove a stalled LiveKit leg really recovers."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
STARTUP = REPO / "startup.sh"
SEPARATOR = "\x1f"
# Every invocation is logged with one write so concurrent appends never interleave.
STUB = r"""#!/bin/bash
name=$(basename "$0")
line=$(printf '%s' "${name}"; [ "$#" -eq 0 ] || printf '\x1f%s' "$@")
printf '%s\n' "${line}" >>"${STUB_LOG}"
case "${name}" in
    sleep) exec /bin/sleep 0.05 ;;
    pactl) [ "${1:-}" = info ] && echo "Server String: unix:/tmp/alana-stub-pulse" ;;
    ffmpeg) exec "${STUB_PYTHON}" "${STUB_DIR}/fake_encoder.py" "$@" ;;
    chromium|Xvfb|lk) exec /bin/sleep 600 ;;
esac
exit 0
"""
# A stand-in encoder: it advances -progress like ffmpeg and listens on its unix
# outputs. Like ffmpeg, it fails when a live process still holds one of them or
# when a stale socket path is left behind.
FAKE_ENCODER = r"""
import os, socket, sys, time
args = sys.argv[1:]
progress = args[args.index("-progress") + 1]
listeners = []
for path in [a[len("unix:"):] for a in args if a.startswith("unix:")]:
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.connect(path)
        sys.exit(1)  # address in use: an earlier encoder still owns it
    except OSError:
        pass
    finally:
        probe.close()
    if os.path.exists(path):
        sys.exit(1)  # like ffmpeg's listen: a leftover socket path is "Address already in use"
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(1)
    listeners.append(server)
with open(os.environ["ENCODERS_LOG"], "a") as log:
    log.write("%d %s\n" % (os.getpid(), "livekit" if listeners else "rtmp"))
frame = 0
while True:
    frame += 1
    with open(progress, "w") as out:
        out.write("frame=%d\nprogress=continue\n" % frame)
    time.sleep(0.1)
"""
STUBS = ("sleep", "pactl", "pulseaudio", "dbus-daemon", "Xvfb", "xdpyinfo", "curl", "chromium", "ffmpeg", "lk")


def _bash_supports_startup() -> bool:
    """startup.sh needs bash 4.3+ (mapfile, wait -n); macOS ships bash 3.2."""
    bash = shutil.which("bash")
    if bash is None:
        return False
    probe = subprocess.run([bash, "-c", "mapfile -t x </dev/null; sleep 0 & wait -n"], capture_output=True, check=False)
    return probe.returncode == 0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


@unittest.skipUnless(_bash_supports_startup(), "startup.sh requires bash 4.3 or newer")
class LiveKitStallRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        stubs = self.root / "bin"
        stubs.mkdir()
        for name in STUBS:
            path = stubs / name
            path.write_text(STUB, encoding="utf-8")
            path.chmod(0o755)
        (stubs / "fake_encoder.py").write_text(FAKE_ENCODER, encoding="utf-8")
        self.encoders = self.root / "encoders.log"
        self.process: subprocess.Popen | None = None
        self.environment = {
            "PATH": f"{stubs}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STUB_LOG": str(self.root / "stub.log"),
            "STUB_DIR": str(stubs),
            "STUB_PYTHON": sys.executable,
            "ENCODERS_LOG": str(self.encoders),
            "PROGRAM_ID": "test-program",
            "CHANNEL_BROWSER_URL": "http://renderer.test/program",
            "RTMP_OUTPUTS": "rtmp://relay.test/live/a",
            "LIVEKIT_ENABLED": "1",
            "LIVEKIT_URL": "ws://livekit.test",
            "LIVEKIT_API_KEY": "test-key",
            "LIVEKIT_API_SECRET": "test-secret",
            "LIVEKIT_ROOM": "test-room",
            "STALL_TIMEOUT": "2",
            "RESTART_BACKOFF": "1",
            "ALANA_RUNTIME_METRICS_FILE": str(self.root / "runtime-metrics.json"),
        }

    def tearDown(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid, _ in self.started():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.temp.cleanup()

    def started(self) -> list[tuple[int, str]]:
        try:
            lines = self.encoders.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [(int(pid), leg) for pid, leg in (line.split() for line in lines)]

    def wait_for(self, predicate, timeout: float = 30.0, message: str = ""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail(f"{message}; encoders started: {self.started()}")

    def test_a_stalled_livekit_encoder_is_replaced_and_publishes_again(self):
        self.process = subprocess.Popen(
            ["bash", str(STARTUP)], env=self.environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        # Both encoders start independently; a slow runner may start either first.
        self.wait_for(
            lambda: {leg for _, leg in self.started()} >= {"rtmp", "livekit"},
            message="the RTMP and LiveKit encoders did not both start",
        )
        first = next(pid for pid, leg in self.started() if leg == "livekit")
        rtmp = [pid for pid, leg in self.started() if leg == "rtmp"]
        self.assertEqual(len(rtmp), 1)

        os.kill(first, signal.SIGSTOP)  # frozen: no progress, sockets still held
        stalled_at = time.monotonic()
        replacement = self.wait_for(
            lambda: next((pid for pid, leg in self.started() if leg == "livekit" and pid != first), None),
            timeout=2 + 15,
            message="no new LiveKit encoder took over the sockets after the stall",
        )
        self.assertLessEqual(time.monotonic() - stalled_at, 2 + 15)
        self.assertFalse(alive(first), "the frozen encoder is left running and holding the sockets")
        self.assertTrue(alive(replacement))
        progress = Path("/tmp/livekit-progress.txt")
        before = progress.read_text(encoding="utf-8")
        time.sleep(0.5)
        self.assertNotEqual(progress.read_text(encoding="utf-8"), before, "the new encoder publishes progress")
        # Only the LiveKit leg restarted.
        self.assertEqual([pid for pid, leg in self.started() if leg == "rtmp"], rtmp)
        self.assertTrue(alive(rtmp[0]))


if __name__ == "__main__":
    unittest.main()
