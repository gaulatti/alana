#!/usr/bin/env python3
"""Recorder drift rig (gaulatti/alana#32): how far audio moves against video over a recording.

Runs the built Alana image unmodified in private tape mode (no RTMP, LiveKit or Croccante) with
test/drift/page.html as the program: a flash and a beep every 10 s, timed from one clock. Starts a
tape through the control API, records for --seconds, stops it (finalize), downloads program.mp4 and
fits the audio-minus-video offset of every event against time. Prints one JSON report; with
--output it also writes it. Needs Docker and ffmpeg/ffprobe on the host.

    python3 test/drift/run.py --image alana:local --seconds 600 --runs 3 --output drift.json

The control token is generated for the run, written to a private temporary file mounted read-only,
and never printed. The container and its files are removed afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import drift, offsets  # noqa: E402

PROGRAM = "drift"
PAGE_PORT = 8099


def request(base: str, token: str, method: str, path: str, key: str | None = None, timeout: float = 30):
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    data = b"" if method == "POST" else None
    if method == "POST":
        headers["Content-Length"] = "0"
    req = urllib.request.Request(f"{base}/v1/programs/{PROGRAM}/{path}", method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait_for(predicate, limit: float, every: float = 1.0, what: str = "condition"):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    raise TimeoutError(f"timed out after {limit:.0f}s waiting for {what}")


def one_run(image: str, seconds: int, work: Path, index: int, platform: str, keep: Path | None = None, extra_env: list[str] = (), keep_segments: bool = False) -> dict:
    token = secrets.token_hex(32)
    secrets_dir = work / f"secrets-{index}"
    secrets_dir.mkdir()
    for name in ("alana-control-token", "croccante-control-token"):
        (secrets_dir / name).write_text(token if name.startswith("alana") else secrets.token_hex(32))
        (secrets_dir / name).chmod(0o644)
    name = f"alana-drift-{os.getpid()}-{index}"
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "--platform", platform, "--shm-size", "1g",
         "-p", "127.0.0.1::8080",
         "-e", f"PROGRAM_ID={PROGRAM}", "-e", f"CHANNEL_BROWSER_URL=http://127.0.0.1:{PAGE_PORT}/page.html",
         "-e", "RECORDING_ENABLED=1", "-e", "CROCCANTE_CONTROL_URL=http://127.0.0.1:9",
         # Start-up validation needs an output; tape mode never publishes to it.
         "-e", "RTMP_OUTPUTS=rtmp://127.0.0.1:1935/live/drift-unused",
         # The rest as docker-compose.yml sets them.
         "-e", "STALL_TIMEOUT=30", "-e", "RESTART_BACKOFF=5", "-e", "MAX_BACKOFF=60", "-e", "HEALTHY_RESET_SECONDS=60",
         "-e", "PIPELINE_READY_TIMEOUT=90", "-e", "CONTROL_RETRY_SECONDS=5",
         "-e", "ALANA_CONTROL_TOKEN_FILE=/run/secrets/alana-control-token",
         "-e", "CROCCANTE_CONTROL_TOKEN_FILE=/run/secrets/croccante-control-token",
         "-v", f"{secrets_dir / 'alana-control-token'}:/run/secrets/alana-control-token:ro",
         "-v", f"{secrets_dir / 'croccante-control-token'}:/run/secrets/croccante-control-token:ro",
         *[part for value in extra_env for part in ("-e", value)],
         "-v", f"{HERE}:/drift:ro", image],
        check=True, stdout=subprocess.DEVNULL,
    )
    try:
        subprocess.run(["docker", "exec", "-d", name, "python3", "-m", "http.server", str(PAGE_PORT), "--bind", "127.0.0.1", "--directory", "/drift"], check=True)
        port = subprocess.run(["docker", "port", name, "8080/tcp"], capture_output=True, text=True, check=True).stdout.split(":")[-1].strip()
        base = f"http://127.0.0.1:{port}"
        wait_for(lambda: request(base, token, "GET", "tape")[0] == 200 if _reachable(base) else False, 120, what="the control server")
        status, body = request(base, token, "POST", "tape/start", key=f"drift-start-{index}-{secrets.token_hex(4)}", timeout=180)
        if status not in (200, 202):
            raise RuntimeError(f"tape start answered {status}: {body[:200]!r}")
        recording = lambda: json.loads(request(base, token, "GET", "tape")[1])["recording"]  # noqa: E731
        wait_for(lambda: recording().get("state") == "active", 120, what="the recorder to be active")
        started = time.monotonic()
        time.sleep(seconds)
        status, body = request(base, token, "POST", "tape/stop", key=f"drift-stop-{index}-{secrets.token_hex(4)}", timeout=600)
        if status not in (200, 202):
            raise RuntimeError(f"tape stop answered {status}: {body[:200]!r}")
        final = wait_for(lambda: (lambda r: r if r.get("state") in ("complete", "failed") else None)(recording()), 600, what="finalization")
        recorded = time.monotonic() - started
        if final.get("state") != "complete":
            raise RuntimeError(f"recording ended {final.get('state')}: {final.get('error')}")
        status, body = request(base, token, "GET", "recording/file", timeout=600)
        if status != 200:
            raise RuntimeError(f"recording file answered {status}")
        path = work / f"program-{index}.mp4"
        path.write_bytes(body)
        if keep:
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, keep / path.name)
            if keep_segments:
                # The raw segments, to compare joins offline on the same capture.
                subprocess.run(["docker", "cp", f"{name}:/var/lib/alana/recordings/operations", str(keep / f"segments-{index}")], check=True, stdout=subprocess.DEVNULL)
        points = offsets(path)
        return {
            "run": index,
            "recordedSeconds": round(recorded, 1),
            "finalizationMethod": final.get("finalizationMethod"),
            "segments": final.get("segmentCount"),
            "droppedFrames": final.get("droppedFrames"),
            "restarts": final.get("restarts"),
            **drift(points),
            # Without the first event: the capture's first seconds carry a start-up offset of their
            # own, measurable inside the first raw segment before any join.
            "steady": drift(points[1:]),
            "offsetsMs": [round(v, 1) for _, v in points],
        }
    except Exception:
        logs = subprocess.run(["docker", "logs", "--tail", "30", name], capture_output=True, text=True)
        print(f"run {index} failed; container log tail:\n{logs.stdout}{logs.stderr}", file=sys.stderr)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _reachable(base: str) -> bool:
    try:
        urllib.request.urlopen(f"{base}/", timeout=2)
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
    return True


def load() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default="alana:local")
    parser.add_argument("--platform", default="linux/arm64" if os.uname().machine in ("arm64", "aarch64") else "linux/amd64")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--keep", type=Path, help="copy each program.mp4 into this directory")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-segments", action="store_true", help="with --keep, also copy each run's raw segments")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE", help="extra container setting, repeatable")
    args = parser.parse_args()
    for tool in ("docker", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            parser.error(f"{tool} is required")
    report = {"image": args.image, "seconds": args.seconds, "env": args.env, "loadBefore": load(), "runs": []}
    # Under the home folder: Docker Desktop and Colima share it with their VM, not the system temp folder.
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="alana-drift-", dir=cache) as directory:
        work = Path(directory)
        for index in range(1, args.runs + 1):
            result = one_run(args.image, args.seconds, work, index, args.platform, args.keep, args.env, args.keep_segments)
            result["loadAfter"] = load()
            report["runs"].append(result)
            print(json.dumps({key: value for key, value in result.items() if key != "offsetsMs"}), file=sys.stderr, flush=True)
    runs = report["runs"]
    report["median"] = {
        "slopeMsPerMinute": statistics.median(r["slopeMsPerMinute"] for r in runs),
        "fittedChangeMs": statistics.median(r["fittedChangeMs"] for r in runs),
        "firstToLastMs": statistics.median(r["firstToLastMs"] for r in runs),
        "steadyFittedChangeMs": statistics.median(r["steady"]["fittedChangeMs"] for r in runs),
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
