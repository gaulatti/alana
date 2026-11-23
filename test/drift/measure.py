"""Audio-against-video offsets of a flash-and-beep program, and their drift (gaulatti/alana#32).

Shared by the recorder's drift test and the drift rig (test/drift/run.py)."""

from __future__ import annotations

import array
import json
import subprocess
import wave
from pathlib import Path


def flash_times(path: Path) -> list[float]:
    """Onset (s) of each white flash in the file's video, in its own timeline."""
    stats = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-map", "0:v:0", "-vf",
         "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    flashes, moment, last = [], 0.0, -10.0
    for line in stats.splitlines():
        if line.startswith("frame:"):
            moment = float(line.split("pts_time:")[1])
        elif "YAVG" in line and float(line.split("=")[1]) > 128:
            if moment - last > 1:
                flashes.append(moment)
            last = moment
    return flashes


def beep_times(path: Path) -> list[float]:
    """Onset (s) of each beep in the file's first audio stream, in the file's timeline."""
    start = float(json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=start_time", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout)["streams"][0]["start_time"])
    pcm = path.with_suffix(".beeps.wav")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-ac", "1", "-c:a", "pcm_s16le", str(pcm)], check=True)
    try:
        with wave.open(str(pcm)) as handle:
            rate, samples = handle.getframerate(), array.array("h", handle.readframes(handle.getnframes()))
    finally:
        pcm.unlink(missing_ok=True)
    beeps, last = [], -10.0
    for index, sample in enumerate(samples):
        if abs(sample) > 1500:
            moment = start + index / rate
            if moment - last > 1:
                beeps.append(moment)
            last = moment
    return beeps


def offsets(path: Path) -> list[tuple[float, float]]:
    """(time s, audio minus video ms) for each beep, against the nearest flash."""
    flashes = flash_times(path)
    return [(beep, (beep - min(flashes, key=lambda flash: abs(flash - beep))) * 1000) for beep in beep_times(path)] if flashes else []


def drift(points: list[tuple[float, float]]) -> dict[str, float]:
    """Least-squares slope of the offset (ms per minute), and the fitted change over the span."""
    if len(points) < 2:
        raise ValueError(f"need at least two events, got {len(points)}")
    times, values = [t for t, _ in points], [v for _, v in points]
    mean_t, mean_v = sum(times) / len(times), sum(values) / len(values)
    slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / sum((t - mean_t) ** 2 for t in times)
    span = times[-1] - times[0]
    return {
        "events": len(points),
        "spanSeconds": round(span, 1),
        "slopeMsPerMinute": round(slope * 60, 2),
        "fittedChangeMs": round(slope * span, 1),
        "firstToLastMs": round(values[-1] - values[0], 1),
        "spreadMs": round(max(values) - min(values), 1),
        "meanOffsetMs": round(mean_v, 1),
    }
