#!/usr/bin/env python3
"""Crash-tolerant recording supervisor for Alana's composed program output."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

RECORDING_STATES = frozenset(
    {"disabled", "idle", "requested", "active", "finalizing", "complete", "failed"}
)
FINALIZATION_STATES = frozenset({"not-requested", "pending", "verified", "failed"})
FAILURE_REASONS = frozenset(
    {
        "disk-exhausted",
        "quota-exhausted",
        "capture-failed",
        "restart-exhausted",
        "manifest-corrupt",
        "segment-corrupt",
        "finalize-failed",
        "final-artifact-invalid",
    }
)
OPERATION_ID = re.compile(r"^[a-f0-9]{16}$")
SEGMENT_NAME = re.compile(r"^segment-(\d{6})\.mkv$")
PUBLIC_FIELDS = frozenset(
    {
        "enabled",
        "state",
        "operationId",
        "requestedAt",
        "startedAt",
        "stoppedAt",
        "finalizedAt",
        "updatedAt",
        "segmentCount",
        "bytes",
        "durationSeconds",
        "videoCodec",
        "audioCodec",
        "width",
        "height",
        "fps",
        "audioRate",
        "audioChannels",
        "droppedFrames",
        "errors",
        "restarts",
        "finalizationState",
        "finalSha256",
        "finalBytes",
        "error",
        "disk",
    }
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_event(event: str, state: str, result: str, count: int = 0) -> None:
    """Emit only closed-vocabulary recording telemetry."""
    print(
        f"[recording] event={event} state={state} result={result} count={max(0, count)}",
        file=sys.stderr,
        flush=True,
    )


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(payload, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def append_json_line(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(line)
        output.flush()
        os.fsync(output.fileno())
        fcntl.flock(output, fcntl.LOCK_UN)


def read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_positive(name: str, default: int) -> int:
    value = os.environ.get(name, str(default))
    if not value.isdigit() or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def tree_size(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


class RecordingStore:
    def __init__(self, root: Path):
        self.root = root
        self.state_file = root / "state.json"
        self.command_dir = root / "commands"
        self.operations_dir = root / "operations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.command_dir.mkdir(exist_ok=True)
        self.operations_dir.mkdir(exist_ok=True)

    @staticmethod
    def command_digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def command_path(self, key: str) -> Path:
        return self.command_dir / f"{self.command_digest(key)}.json"

    def read_command(self, key: str) -> dict[str, object] | None:
        return read_json(self.command_path(key))

    def write_command(self, key: str, payload: dict[str, object]) -> None:
        atomic_json(self.command_path(key), payload)

    def operation_dir(self, operation_id: str) -> Path:
        if not OPERATION_ID.fullmatch(operation_id):
            raise ValueError("invalid recording operation")
        return self.operations_dir / operation_id

    def load(self, *, enabled: bool = True) -> dict[str, object]:
        state = read_json(self.state_file)
        if state is None:
            stamp = now()
            if self.state_file.exists():
                state = {
                    "enabled": enabled,
                    "state": "failed",
                    "operationId": None,
                    "segmentCount": 0,
                    "bytes": 0,
                    "durationSeconds": 0.0,
                    "droppedFrames": 0,
                    "errors": 1,
                    "restarts": 0,
                    "finalizationState": "failed",
                    "error": "manifest-corrupt",
                    "updatedAt": stamp,
                }
            else:
                state = {
                    "enabled": enabled,
                    "state": "idle" if enabled else "disabled",
                    "operationId": None,
                    "segmentCount": 0,
                    "bytes": 0,
                    "durationSeconds": 0.0,
                    "droppedFrames": 0,
                    "errors": 0,
                    "restarts": 0,
                    "finalizationState": "not-requested",
                    "updatedAt": stamp,
                }
            self.save(state)
        if state.get("state") not in RECORDING_STATES:
            state["state"] = "failed"
            state["error"] = "manifest-corrupt"
        state["enabled"] = enabled
        if not enabled:
            state["state"] = "disabled"
        return state

    def save(self, state: dict[str, object]) -> None:
        state["updatedAt"] = now()
        atomic_json(self.state_file, state)

    @staticmethod
    def public(state: dict[str, object]) -> dict[str, object]:
        result = {key: value for key, value in state.items() if key in PUBLIC_FIELDS}
        disk = result.get("disk")
        if isinstance(disk, dict):
            result["disk"] = {
                key: disk[key]
                for key in ("healthy", "freeBytes", "usageBytes", "quotaBytes", "minFreeBytes")
                if key in disk
            }
        if result.get("error") not in FAILURE_REASONS:
            result.pop("error", None)
        return result


def disk_snapshot(root: Path, quota_bytes: int, min_free_bytes: int) -> dict[str, object]:
    usage = shutil.disk_usage(root)
    stored = tree_size(root / "operations")
    return {
        "healthy": usage.free >= min_free_bytes and stored < quota_bytes,
        "freeBytes": usage.free,
        "usageBytes": stored,
        "quotaBytes": quota_bytes,
        "minFreeBytes": min_free_bytes,
    }


def cleanup_retention(store: RecordingStore, retention_hours: int, current: str | None) -> int:
    cutoff = time.time() - (retention_hours * 3600)
    removed = 0
    for directory in sorted(store.operations_dir.iterdir()):
        if not directory.is_dir() or directory.name == current:
            continue
        summary = read_json(directory / "manifest.json")
        try:
            expired = directory.stat().st_mtime < cutoff
        except OSError:
            continue
        if isinstance(summary, dict):
            finished = summary.get("finalizedAt") or summary.get("stoppedAt")
            if isinstance(finished, str):
                try:
                    expired = datetime.fromisoformat(finished.replace("Z", "+00:00")).timestamp() < cutoff
                except ValueError:
                    pass
        if expired:
            shutil.rmtree(directory)
            removed += 1
    return removed


def frame_rate(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:/[1-9][0-9]*)?", value):
        return "unknown"
    return value


def probe_media(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") if isinstance(payload, dict) else None
    media_format = payload.get("format") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or not isinstance(media_format, dict):
        raise ValueError("invalid probe response")
    video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"), None)
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise ValueError("recording must contain video and audio")
    duration = float(media_format.get("duration", 0))
    size = int(media_format.get("size", path.stat().st_size))
    if duration <= 0 or size <= 0:
        raise ValueError("recording is empty")
    return {
        "durationSeconds": round(duration, 6),
        "bytes": size,
        "videoCodec": str(video.get("codec_name") or "unknown")[:32],
        "audioCodec": str(audio.get("codec_name") or "unknown")[:32],
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": frame_rate(video.get("r_frame_rate")),
        "audioRate": int(audio.get("sample_rate") or 0),
        "audioChannels": int(audio.get("channels") or 0),
    }


def segment_sequence(path: Path) -> int:
    match = SEGMENT_NAME.fullmatch(path.name)
    if match is None:
        raise ValueError("invalid segment name")
    return int(match.group(1))


def manifest_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return records
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("manifest-corrupt") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("sequence"), int):
            raise ValueError("manifest-corrupt")
        records.append(payload)
    return records


def refresh_summary(store: RecordingStore, state: dict[str, object], records: list[dict[str, object]]) -> None:
    state["segmentCount"] = len(records)
    state["bytes"] = sum(int(item.get("bytes", 0)) for item in records)
    state["durationSeconds"] = round(
        sum(float(item.get("durationSeconds", 0)) for item in records), 6
    )
    if records:
        latest = records[-1]
        for key in (
            "videoCodec",
            "audioCodec",
            "width",
            "height",
            "fps",
            "audioRate",
            "audioChannels",
        ):
            state[key] = latest.get(key)
    store.save(state)


def harvest_segments(
    store: RecordingStore,
    state: dict[str, object],
    *,
    include_latest: bool,
) -> list[dict[str, object]]:
    operation_id = str(state.get("operationId") or "")
    operation_dir = store.operation_dir(operation_id)
    manifest_path = operation_dir / "manifest.jsonl"
    records = manifest_records(manifest_path)
    recorded = {int(item["sequence"]) for item in records}
    segments = sorted(
        (path for path in operation_dir.glob("segment-*.mkv") if SEGMENT_NAME.fullmatch(path.name)),
        key=segment_sequence,
    )
    candidates = segments if include_latest else segments[:-1]
    for path in candidates:
        sequence = segment_sequence(path)
        if sequence in recorded:
            continue
        try:
            metadata = probe_media(path)
            ended_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            started_at = ended_at.timestamp() - float(metadata["durationSeconds"])
            record = {
                "sequence": sequence,
                "file": path.name,
                "startedAt": datetime.fromtimestamp(started_at, timezone.utc).isoformat().replace("+00:00", "Z"),
                "endedAt": ended_at.isoformat().replace("+00:00", "Z"),
                **metadata,
                "sha256": sha256_file(path),
            }
            append_json_line(manifest_path, record)
            records.append(record)
            recorded.add(sequence)
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            quarantine = path.with_suffix(path.suffix + ".corrupt")
            try:
                os.replace(path, quarantine)
            except OSError:
                pass
            state["errors"] = int(state.get("errors", 0)) + 1
    records.sort(key=lambda item: int(item["sequence"]))
    refresh_summary(store, state, records)
    return records


def capture_command(
    operation_dir: Path,
    start_number: int,
    segment_seconds: int,
    *,
    synthetic: bool = False,
    duration: int | None = None,
) -> list[str]:
    resolution = os.environ.get("RESOLUTION", "1920x1080")
    fps = os.environ.get("FPS", "30")
    display = os.environ.get("DISPLAY_NUM", ":98")
    draw_mouse = os.environ.get("DRAW_MOUSE", "0")
    video_bitrate = os.environ.get("RECORDING_VIDEO_BITRATE", os.environ.get("VIDEO_BITRATE", "6000k"))
    audio_bitrate = os.environ.get("RECORDING_AUDIO_BITRATE", os.environ.get("AUDIO_BITRATE", "128k"))
    progress = operation_dir / "capture-progress.txt"
    pattern = operation_dir / "segment-%06d.mkv"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if synthetic:
        resolution = os.environ.get("RECORDING_TEST_RESOLUTION", "640x360")
        fps = os.environ.get("RECORDING_TEST_FPS", "30")
        command.extend(
            [
                "-re",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={resolution}:rate={fps}",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=48000",
            ]
        )
    else:
        command.extend(
            [
                "-thread_queue_size",
                "1024",
                "-f",
                "x11grab",
                "-draw_mouse",
                draw_mouse,
                "-video_size",
                resolution,
                "-framerate",
                fps,
                "-i",
                display,
                "-thread_queue_size",
                "1024",
                "-f",
                "pulse",
                "-i",
                "stream_out.monitor",
            ]
        )
    command.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            os.environ.get("RECORDING_X264_PRESET", "veryfast"),
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            video_bitrate,
            "-g",
            str(max(1, int(fps) * segment_seconds)),
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{segment_seconds})",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
    )
    if duration is not None:
        command.extend(["-t", str(duration)])
    command.extend(
        [
            "-progress",
            str(progress),
            "-f",
            "segment",
            "-segment_format",
            "matroska",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            "-segment_start_number",
            str(start_number),
            str(pattern),
        ]
    )
    return command


def capture_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
    if "PULSE_SERVER" not in environment:
        try:
            result = subprocess.run(
                ["pactl", "info"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
            )
            for line in result.stdout.splitlines():
                if line.startswith("Server String: "):
                    environment["PULSE_SERVER"] = line.split(": ", 1)[1]
                    break
        except (OSError, subprocess.SubprocessError):
            pulse_socket = Path(environment["XDG_RUNTIME_DIR"]) / "pulse" / "native"
            if pulse_socket.exists():
                environment["PULSE_SERVER"] = f"unix:{pulse_socket}"
    return environment


def read_dropped_frames(progress: Path) -> int:
    try:
        values = [
            int(line.split("=", 1)[1])
            for line in progress.read_text(encoding="utf-8").splitlines()
            if line.startswith("drop_frames=") and line.split("=", 1)[1].isdigit()
        ]
        return max(values, default=0)
    except OSError:
        return 0


def next_sequence(operation_dir: Path) -> int:
    values = []
    for path in operation_dir.glob("segment-*"):
        candidate = path.name.removesuffix(".corrupt")
        match = SEGMENT_NAME.fullmatch(candidate)
        if match:
            values.append(int(match.group(1)))
    return max(values, default=-1) + 1


def terminate_process(process: subprocess.Popen[bytes], timeout: int = 15) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def finalize_recording(store: RecordingStore, state: dict[str, object]) -> bool:
    state["state"] = "finalizing"
    state["finalizationState"] = "pending"
    store.save(state)
    try:
        records = harvest_segments(store, state, include_latest=True)
        if not records:
            raise ValueError("no segments")
        operation_dir = store.operation_dir(str(state["operationId"]))
        concat_path = operation_dir / "concat.txt"
        with concat_path.open("w", encoding="utf-8") as output:
            for record in records:
                segment = operation_dir / str(record["file"])
                if sha256_file(segment) != record.get("sha256"):
                    raise ValueError("segment checksum mismatch")
                output.write(f"file '{segment.name}'\n")
            output.flush()
            os.fsync(output.fileno())
        final_path = operation_dir / "program.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                concat_path.name,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                final_path.name,
            ],
            cwd=operation_dir,
            check=True,
            timeout=parse_positive("RECORDING_FINALIZE_TIMEOUT", 120),
        )
        metadata = probe_media(final_path)
        state.update(
            state="complete",
            finalizationState="verified",
            finalizedAt=now(),
            stoppedAt=state.get("stoppedAt") or now(),
            finalSha256=sha256_file(final_path),
            finalBytes=int(metadata["bytes"]),
            durationSeconds=metadata["durationSeconds"],
            videoCodec=metadata["videoCodec"],
            audioCodec=metadata["audioCodec"],
            width=metadata["width"],
            height=metadata["height"],
            fps=metadata["fps"],
            audioRate=metadata["audioRate"],
            audioChannels=metadata["audioChannels"],
            workerPid=None,
            supervisorPid=None,
        )
        state.pop("error", None)
        atomic_json(operation_dir / "manifest.json", RecordingStore.public(state))
        store.save(state)
        log_event("finalize", "complete", "verified", int(state.get("segmentCount", 0)))
        return True
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        state.update(
            state="failed",
            finalizationState="failed",
            error="finalize-failed",
            errors=int(state.get("errors", 0)) + 1,
            workerPid=None,
            supervisorPid=None,
        )
        store.save(state)
        try:
            atomic_json(
                store.operation_dir(str(state["operationId"])) / "manifest.json",
                RecordingStore.public(state),
            )
        except (OSError, ValueError):
            pass
        log_event("finalize", "failed", "failed", int(state.get("errors", 0)))
        return False


def run_supervisor(root: Path, operation_id: str, *, synthetic: bool = False) -> int:
    store = RecordingStore(root)
    state = store.load(enabled=True)
    if state.get("operationId") != operation_id or state.get("state") not in {
        "requested",
        "active",
        "finalizing",
    }:
        return 2
    operation_dir = store.operation_dir(operation_id)
    operation_dir.mkdir(parents=True, exist_ok=True)
    shutdown = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    segment_seconds = parse_positive("RECORDING_SEGMENT_SECONDS", 5)
    quota_bytes = parse_positive("RECORDING_QUOTA_BYTES", 50 * 1024 * 1024 * 1024)
    min_free_bytes = parse_positive("RECORDING_MIN_FREE_BYTES", 1024 * 1024 * 1024)
    max_restarts = parse_positive("RECORDING_MAX_RESTARTS", 5)
    restart_backoff = parse_positive("RECORDING_RESTART_BACKOFF_SECONDS", 2)
    dropped_base = int(state.get("droppedFrames", 0))

    if state.get("state") == "finalizing":
        return 0 if finalize_recording(store, state) else 1

    while not shutdown.is_set():
        disk = disk_snapshot(root, quota_bytes, min_free_bytes)
        state["disk"] = disk
        if int(disk["freeBytes"]) < min_free_bytes:
            state.update(state="failed", error="disk-exhausted", errors=int(state.get("errors", 0)) + 1)
            store.save(state)
            log_event("preflight", "failed", "disk-exhausted", int(state["errors"]))
            return 1
        if int(disk["usageBytes"]) >= quota_bytes:
            state.update(state="failed", error="quota-exhausted", errors=int(state.get("errors", 0)) + 1)
            store.save(state)
            log_event("preflight", "failed", "quota-exhausted", int(state["errors"]))
            return 1
        try:
            harvest_segments(store, state, include_latest=True)
        except ValueError:
            state.update(state="failed", error="manifest-corrupt", errors=int(state.get("errors", 0)) + 1)
            store.save(state)
            log_event("manifest", "failed", "corrupt", int(state["errors"]))
            return 1
        progress = operation_dir / "capture-progress.txt"
        progress.unlink(missing_ok=True)
        command = capture_command(
            operation_dir,
            next_sequence(operation_dir),
            segment_seconds,
            synthetic=synthetic,
        )
        process = subprocess.Popen(
            command,
            env=capture_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        state.update(
            state="active",
            startedAt=state.get("startedAt") or now(),
            supervisorPid=os.getpid(),
            workerPid=process.pid,
        )
        store.save(state)
        log_event("capture", "active", "started", int(state.get("restarts", 0)))
        disk_failure: str | None = None
        while process.poll() is None and not shutdown.wait(0.25):
            current = store.load(enabled=True)
            if current.get("operationId") != operation_id:
                shutdown.set()
                break
            if current.get("state") == "finalizing":
                state = current
                terminate_process(process)
                break
            state = current
            try:
                harvest_segments(store, state, include_latest=False)
            except ValueError:
                disk_failure = "manifest-corrupt"
                terminate_process(process)
                break
            state["droppedFrames"] = dropped_base + read_dropped_frames(progress)
            disk = disk_snapshot(root, quota_bytes, min_free_bytes)
            state["disk"] = disk
            if int(disk["freeBytes"]) < min_free_bytes:
                disk_failure = "disk-exhausted"
                terminate_process(process)
            elif int(disk["usageBytes"]) >= quota_bytes:
                disk_failure = "quota-exhausted"
                terminate_process(process)
            else:
                store.save(state)

        if process.poll() is None:
            terminate_process(process)
        state = store.load(enabled=True)
        dropped_base = max(
            int(state.get("droppedFrames", 0)),
            dropped_base + read_dropped_frames(progress),
        )
        state["droppedFrames"] = dropped_base
        harvest_segments(store, state, include_latest=True)
        state["workerPid"] = None
        store.save(state)
        current = store.load(enabled=True)
        if current.get("state") == "finalizing":
            current["stoppedAt"] = current.get("stoppedAt") or now()
            return 0 if finalize_recording(store, current) else 1
        if shutdown.is_set():
            current["workerPid"] = None
            current["supervisorPid"] = None
            store.save(current)
            return 0
        if disk_failure:
            current.update(
                state="failed",
                error=disk_failure,
                errors=int(current.get("errors", 0)) + 1,
                supervisorPid=None,
            )
            store.save(current)
            log_event("capture", "failed", disk_failure, int(current["errors"]))
            return 1
        restarts = int(current.get("restarts", 0)) + 1
        current.update(
            restarts=restarts,
            errors=int(current.get("errors", 0)) + 1,
            workerPid=None,
        )
        if restarts > max_restarts:
            current.update(state="failed", error="restart-exhausted", supervisorPid=None)
            store.save(current)
            log_event("capture", "failed", "restart-exhausted", restarts)
            return 1
        store.save(current)
        log_event("capture", "active", "restarting", restarts)
        state = current
        if shutdown.wait(min(restart_backoff * (2 ** (restarts - 1)), 60)):
            return 0
    return 0


class RecordingController:
    def __init__(
        self,
        root: Path,
        *,
        enabled: bool,
        ready: Callable[[], bool],
        synthetic: bool = False,
        start_monitor: bool = True,
    ):
        self.store = RecordingStore(root)
        self.enabled = enabled
        self.ready = ready
        self.synthetic = synthetic
        self.process: subprocess.Popen[bytes] | None = None
        self.lock = threading.RLock()
        self.closed = threading.Event()
        state = self.store.load(enabled=enabled)
        if enabled:
            cleanup_retention(
                self.store,
                parse_positive("RECORDING_RETENTION_HOURS", 168),
                str(state.get("operationId") or "") or None,
            )
        if start_monitor:
            threading.Thread(target=self._monitor, daemon=True).start()

    def status(self) -> dict[str, object]:
        return self.store.public(self.store.load(enabled=self.enabled))

    def _spawn(self) -> None:
        state = self.store.load(enabled=self.enabled)
        if not self.enabled or state.get("state") not in {"requested", "active", "finalizing"}:
            return
        if state.get("state") in {"requested", "active"} and not self.ready():
            return
        if self.process is not None and self.process.poll() is None:
            return
        operation_id = str(state.get("operationId") or "")
        if not OPERATION_ID.fullmatch(operation_id):
            state.update(state="failed", error="manifest-corrupt", errors=int(state.get("errors", 0)) + 1)
            self.store.save(state)
            return
        command = [sys.executable, str(Path(__file__).resolve()), "supervise", "--root", str(self.store.root), "--operation", operation_id]
        if self.synthetic:
            command.append("--synthetic")
        self.process = subprocess.Popen(
            command,
            start_new_session=True,
        )

    def _prior(self, key: str, action: str) -> tuple[int, dict[str, object]] | None:
        prior = self.store.read_command(key)
        if prior is None:
            return None
        if prior.get("action") != action:
            return 409, {**self.status(), "error": "idempotency key was already used"}
        return int(prior.get("status", 200)), {**self.status(), "commandResult": {**prior, "duplicate": True}}

    def _record(self, key: str, action: str, status: int, result: str) -> tuple[int, dict[str, object]]:
        state = self.status()
        record = {
            "id": self.store.command_digest(key)[:16],
            "action": action,
            "status": status,
            "result": result,
            "operationId": state.get("operationId"),
            "acceptedAt": now(),
        }
        self.store.write_command(key, record)
        log_event("command", str(state.get("state") or "failed"), result, status)
        return status, {**state, "commandResult": record}

    def start(self, key: str) -> tuple[int, dict[str, object]]:
        with self.lock:
            prior = self._prior(key, "start")
            if prior:
                return prior
            state = self.store.load(enabled=self.enabled)
            if not self.enabled:
                return self._record(key, "start", 409, "disabled")
            if state.get("error") == "manifest-corrupt":
                return self._record(key, "start", 500, "manifest-corrupt")
            if state.get("state") in {"requested", "active", "finalizing"}:
                return self._record(key, "start", 409, "already-active")
            if not self.ready():
                return self._record(key, "start", 409, "pipeline-not-ready")
            cleanup_retention(
                self.store,
                parse_positive("RECORDING_RETENTION_HOURS", 168),
                None,
            )
            quota = parse_positive("RECORDING_QUOTA_BYTES", 50 * 1024 * 1024 * 1024)
            minimum = parse_positive("RECORDING_MIN_FREE_BYTES", 1024 * 1024 * 1024)
            disk = disk_snapshot(self.store.root, quota, minimum)
            operation_id = self.store.command_digest(key)[:16]
            state = {
                "enabled": True,
                "state": "requested",
                "operationId": operation_id,
                "requestedAt": now(),
                "startedAt": None,
                "stoppedAt": None,
                "finalizedAt": None,
                "segmentCount": 0,
                "bytes": 0,
                "durationSeconds": 0.0,
                "droppedFrames": 0,
                "errors": 0,
                "restarts": 0,
                "finalizationState": "not-requested",
                "disk": disk,
                "workerPid": None,
                "supervisorPid": None,
            }
            if int(disk["freeBytes"]) < minimum:
                state.update(state="failed", error="disk-exhausted", errors=1)
                self.store.save(state)
                return self._record(key, "start", 507, "disk-exhausted")
            if int(disk["usageBytes"]) >= quota:
                state.update(state="failed", error="quota-exhausted", errors=1)
                self.store.save(state)
                return self._record(key, "start", 507, "quota-exhausted")
            operation_dir = self.store.operation_dir(operation_id)
            operation_dir.mkdir(parents=True, exist_ok=False)
            self.store.save(state)
            self._spawn()
            timeout = parse_positive("RECORDING_START_TIMEOUT", 10)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.store.load(enabled=True)
                if current.get("state") in {"active", "failed"}:
                    break
                time.sleep(0.05)
            current = self.store.load(enabled=True)
            if current.get("state") == "failed":
                return self._record(key, "start", 503, str(current.get("error") or "capture-failed"))
            return self._record(key, "start", 202, "accepted")

    def stop(self, key: str) -> tuple[int, dict[str, object]]:
        with self.lock:
            prior = self._prior(key, "stop")
            if prior:
                return prior
            state = self.store.load(enabled=self.enabled)
            if not self.enabled:
                return self._record(key, "stop", 409, "disabled")
            if state.get("state") in {"idle"}:
                return self._record(key, "stop", 409, "not-active")
            if state.get("state") in {"complete", "failed"}:
                return self._record(key, "stop", 200, str(state.get("state")))
            state.update(state="finalizing", finalizationState="pending", stoppedAt=now())
            self.store.save(state)
            self._spawn()
            if self.process is not None and self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
            timeout = parse_positive("RECORDING_FINALIZE_TIMEOUT", 120) + 5
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.store.load(enabled=True)
                if current.get("state") in {"complete", "failed"}:
                    break
                time.sleep(0.1)
            current = self.store.load(enabled=True)
            if current.get("state") == "complete":
                return self._record(key, "stop", 200, "complete")
            if current.get("state") == "failed":
                return self._record(key, "stop", 500, str(current.get("error") or "finalize-failed"))
            current.update(state="failed", error="finalize-failed", finalizationState="failed", errors=int(current.get("errors", 0)) + 1)
            self.store.save(current)
            return self._record(key, "stop", 500, "finalize-failed")

    def command(self, action: str, key: str) -> tuple[int, dict[str, object]]:
        return self.start(key) if action == "start" else self.stop(key)

    def _monitor(self) -> None:
        while not self.closed.wait(1):
            with self.lock:
                state = self.store.load(enabled=self.enabled)
                if state.get("state") in {"requested", "active", "finalizing"}:
                    if self.process is not None and self.process.poll() is not None:
                        self.process = None
                        if state.get("state") != "finalizing":
                            restarts = int(state.get("restarts", 0)) + 1
                            state.update(
                                restarts=restarts,
                                errors=int(state.get("errors", 0)) + 1,
                                supervisorPid=None,
                                workerPid=None,
                            )
                            if restarts > parse_positive("RECORDING_MAX_RESTARTS", 5):
                                state.update(state="failed", error="restart-exhausted")
                            self.store.save(state)
                            log_event(
                                "supervisor",
                                str(state["state"]),
                                "restart-exhausted"
                                if state["state"] == "failed"
                                else "restarting",
                                restarts,
                            )
                    self._spawn()

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)


def synthetic_smoke(root: Path, seconds: int, segment_seconds: int) -> dict[str, object]:
    store = RecordingStore(root)
    operation_id = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    operation_dir = store.operation_dir(operation_id)
    operation_dir.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {
        "enabled": True,
        "state": "active",
        "operationId": operation_id,
        "requestedAt": now(),
        "startedAt": now(),
        "segmentCount": 0,
        "bytes": 0,
        "durationSeconds": 0.0,
        "droppedFrames": 0,
        "errors": 0,
        "restarts": 0,
        "finalizationState": "not-requested",
    }
    store.save(state)
    result = subprocess.run(
        capture_command(
            operation_dir,
            0,
            segment_seconds,
            synthetic=True,
            duration=seconds,
        ),
        env=capture_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=seconds + 30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("synthetic capture failed")
    harvest_segments(store, state, include_latest=True)
    state["stoppedAt"] = now()
    if not finalize_recording(store, state):
        raise RuntimeError("synthetic finalization failed")
    return store.public(store.load(enabled=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    supervise = subparsers.add_parser("supervise")
    supervise.add_argument("--root", type=Path, required=True)
    supervise.add_argument("--operation", required=True)
    supervise.add_argument("--synthetic", action="store_true", help=argparse.SUPPRESS)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--root", type=Path, required=True)
    smoke.add_argument("--seconds", type=int, default=4)
    smoke.add_argument("--segment-seconds", type=int, default=1)
    args = parser.parse_args()
    if args.command == "supervise":
        raise SystemExit(run_supervisor(args.root, args.operation, synthetic=args.synthetic))
    if args.seconds < 2 or args.segment_seconds < 1 or args.segment_seconds >= args.seconds:
        raise SystemExit("invalid synthetic smoke duration")
    print(json.dumps(synthetic_smoke(args.root, args.seconds, args.segment_seconds), sort_keys=True))


if __name__ == "__main__":
    main()
