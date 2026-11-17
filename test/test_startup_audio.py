"""Run startup.sh against logging stubs and assert the audio process tree it builds."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import audio_chain


REPO = Path(__file__).resolve().parents[1]
STARTUP = REPO / "startup.sh"
SEPARATOR = "\x1f"
# One printf per invocation, so concurrent appends never interleave. python3
# runs the repository's audio_chain.py and alana_metrics.py in place of /usr/local/bin.
STUB = r"""#!/bin/bash
name=$(basename "$0")
if [ "${name}" = python3 ]; then exec "${STUB_PYTHON}" "${@//\/usr\/local\/bin/${ALANA_REPO}}"; fi
line=$(printf '%s' "${name}"; [ "$#" -eq 0 ] || printf '\x1f%s' "$@")
printf '%s\n' "${line}" >>"${STUB_LOG}"
case "${name}" in
    sleep) exec /bin/sleep 0.05 ;;
    pactl) [ "${1:-}" = info ] && echo "Server String: unix:/tmp/alana-stub-pulse" ;;
    ffmpeg|chromium|Xvfb) exec /bin/sleep 60 ;;
esac
exit 0
"""
STUBS = ("python3", "sleep", "pactl", "pulseaudio", "dbus-daemon", "Xvfb", "xdpyinfo", "curl", "chromium", "ffmpeg", "lk")
OUTPUTS = ("rtmp://relay.test/live/a", "rtmp://relay.test/live/b")


def _bash_supports_startup() -> bool:
    """startup.sh needs bash 4.3+ (mapfile, wait -n); macOS ships bash 3.2."""
    bash = shutil.which("bash")
    if bash is None:
        return False
    probe = subprocess.run([bash, "-c", "mapfile -t x </dev/null; sleep 0 & wait -n"], capture_output=True, check=False)
    return probe.returncode == 0


GRAPH = audio_chain.filter_graph("1:a:0", "aout", -16.0, -1.0)
PROCESSED = ("-filter_complex", GRAPH, "-map", "[aout]")


def processed(command: tuple[str, ...]) -> tuple[str, ...]:
    """The same encoder with its raw audio map replaced by the shared chain."""
    index = command.index("1:a:0") - 1
    return command[:index] + PROCESSED + command[index + 2 :]


def rtmp_command(output: str, index: int, rate: str) -> tuple[str, ...]:
    return (
        "ffmpeg", "-hide_banner", "-loglevel", "quiet", "-copyts", "-thread_queue_size", "1024",
        "-f", "x11grab", "-draw_mouse", "0", "-video_size", "1920x1080", "-framerate", "30", "-i", ":98",
        "-thread_queue_size", "1024", "-f", "pulse", "-i", "stream_out.monitor", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-b:v", "6000k", "-maxrate", "6000k",
        "-bufsize", "12000k", "-g", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-ar", rate, "-ac", "2", "-avoid_negative_ts", "make_zero", "-f", "flv",
        "-progress", f"/tmp/rtmp-progress-{index}.txt", output,
    )


LIVEKIT_COMMAND = (
    "ffmpeg", "-hide_banner", "-loglevel", "quiet", "-copyts", "-thread_queue_size", "1024",
    "-f", "x11grab", "-draw_mouse", "0", "-video_size", "1920x1080", "-framerate", "30", "-i", ":98",
    "-thread_queue_size", "1024", "-f", "pulse", "-i", "stream_out.monitor", "-map", "0:v:0", "-an",
    "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
    "-bf", "0", "-b:v", "6000k", "-g", "60", "-bsf:v", "h264_mp4toannexb", "-listen", "1", "-avoid_negative_ts", "make_zero", "-f", "h264",
    "-progress", "/tmp/livekit-progress.txt", "unix:/tmp/alana-program-video.sock",
    "-map", "1:a:0", "-vn", "-c:a", "libopus", "-ar", "48000", "-ac", "2", "-application", "lowdelay",
    "-listen", "1", "-avoid_negative_ts", "make_zero", "-f", "opus", "unix:/tmp/alana-program-audio.sock",
)

# The complete set of distinct commands the pre-change startup.sh ran for this
# configuration (main with #30's shared-wallclock flags). The kill switch must restore it exactly.
RAW_AUDIO_PROCESS_TREE = frozenset(
    {
        ("dbus-daemon", "--system", "--fork"),
        ("pulseaudio", "--start", "--exit-idle-time=-1", "--log-target=file:/tmp/pulseaudio.log"),
        ("pactl", "info"),
        ("pactl", "load-module", "module-null-sink", "sink_name=stream_out", "sink_properties=device.description=stream_out"),
        ("pactl", "set-default-sink", "stream_out"),
        ("Xvfb", ":98", "-screen", "0", "1920x1080x24", "+extension", "RANDR", "+extension", "MIT-SHM", "+extension", "XINERAMA"),
        ("xdpyinfo",),
        ("curl", "-fsS", "--max-time", "3", "-o", "/dev/null", "http://renderer.test/program"),
        (
            "chromium", "--no-sandbox", "--disable-background-networking", "--disable-default-apps",
            "--disable-renderer-backgrounding", "--disable-sync", "--hide-scrollbars", "--incognito", "--kiosk",
            "--no-first-run", "--test-type", "--autoplay-policy=no-user-gesture-required", "--window-size=1920,1080",
            "--user-data-dir=/tmp/chrome-profile", "http://renderer.test/program",
        ),
        ("sleep", "5"),
        ("sleep", "15"),
        ("sleep", "60"),
        ("sleep", "0.1"),
        rtmp_command(OUTPUTS[0], 1, "44100"),
        rtmp_command(OUTPUTS[1], 2, "44100"),
        LIVEKIT_COMMAND,
    }
)


@unittest.skipUnless(_bash_supports_startup(), "startup.sh requires bash 4.3 or newer")
class StartupAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        stubs = self.root / "bin"
        stubs.mkdir()
        for name in STUBS:
            path = stubs / name
            path.write_text(STUB, encoding="utf-8")
            path.chmod(0o755)
        self.log = self.root / "stub.log"
        self.process: subprocess.Popen | None = None
        self.environment = {
            "PATH": f"{stubs}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STUB_LOG": str(self.log),
            "PROGRAM_ID": "test-program",
            "CHANNEL_BROWSER_URL": "http://renderer.test/program",
            "RTMP_OUTPUTS": " ".join(OUTPUTS),
            "LIVEKIT_ENABLED": "1",
            "LIVEKIT_URL": "ws://livekit.test",
            "LIVEKIT_API_KEY": "test-key",
            "LIVEKIT_API_SECRET": "test-secret",
            "LIVEKIT_ROOM": "test-room",
            "RESTART_BACKOFF": "1",
            "ALANA_RUNTIME_METRICS_FILE": str(self.root / "runtime-metrics.json"),
            "ALANA_REPO": str(REPO),
            "STUB_PYTHON": sys.executable,
        }

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.temp.cleanup()

    def start(self, **settings: str) -> None:
        self.process = subprocess.Popen(
            ["bash", str(STARTUP)],
            env={**self.environment, **settings},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def commands(self) -> list[tuple[str, ...]]:
        try:
            text = self.log.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [tuple(line.split(SEPARATOR)) for line in text.splitlines() if line]

    def wait_for(self, predicate, timeout: float = 30.0) -> list[tuple[str, ...]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            commands = self.commands()
            if predicate(commands):
                return commands
            time.sleep(0.1)
        self.fail(f"startup.sh did not reach the expected state; ran {sorted(set(self.commands()))}")

    def test_kill_switch_restores_the_raw_audio_process_tree_exactly(self):
        self.start(ALANA_PROCESSED_AUDIO="off")
        self.wait_for(lambda seen: RAW_AUDIO_PROCESS_TREE <= set(seen))
        time.sleep(1.0)
        self.assertEqual(set(self.commands()), RAW_AUDIO_PROCESS_TREE)

    def test_unset_switch_runs_the_shared_48_khz_chain_inside_every_encoder(self):
        # The switch defaults on: no ALANA_PROCESSED_AUDIO in the environment at all.
        self.assertNotIn("ALANA_PROCESSED_AUDIO", self.environment)
        self.start()
        expected = {processed(rtmp_command(OUTPUTS[0], 1, "48000")), processed(rtmp_command(OUTPUTS[1], 2, "48000")), processed(LIVEKIT_COMMAND)}
        seen = set(self.wait_for(lambda seen: expected <= set(seen)))
        self.assertFalse({rtmp_command(OUTPUTS[0], 1, "44100"), LIVEKIT_COMMAND} & seen)
        # Nothing else changes: same sinks, no shared audio process.
        self.assertEqual(seen - expected, RAW_AUDIO_PROCESS_TREE - {rtmp_command(OUTPUTS[0], 1, "44100"), rtmp_command(OUTPUTS[1], 2, "44100"), LIVEKIT_COMMAND})

    def test_empty_switch_is_also_on(self):
        self.start(ALANA_PROCESSED_AUDIO="")
        self.wait_for(lambda seen: processed(rtmp_command(OUTPUTS[0], 1, "48000")) in seen)

    def test_configured_target_and_ceiling_reach_every_encoder(self):
        self.start(ALANA_LOUDNESS_TARGET="-23", ALANA_TRUE_PEAK_CEILING="-2")
        graph = audio_chain.filter_graph("1:a:0", "aout", -23.0, -2.0)
        commands = self.wait_for(lambda seen: sum(1 for command in seen if graph in command) >= 3)
        self.assertEqual({command[0] for command in commands if graph in command}, {"ffmpeg"})

    def test_killing_one_output_restarts_only_that_output(self):
        self.start()
        first, second, livekit = processed(rtmp_command(OUTPUTS[0], 1, "48000")), processed(rtmp_command(OUTPUTS[1], 2, "48000")), processed(LIVEKIT_COMMAND)
        self.wait_for(lambda seen: first in seen and second in seen and livekit in seen)
        livekit_starts = self.commands().count(livekit)
        pid = int(Path("/tmp/rtmp-1.pid").read_text(encoding="utf-8"))
        os.kill(pid, signal.SIGKILL)
        commands = self.wait_for(lambda seen: seen.count(first) == 2)
        self.assertEqual(commands.count(second), 1)
        self.assertEqual(commands.count(livekit), livekit_starts)


if __name__ == "__main__":
    unittest.main()
