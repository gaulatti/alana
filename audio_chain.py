#!/usr/bin/env python3
"""The one programme audio chain every Alana output applies inside its own encoder.

RTMP, LiveKit and the recorder each run this filter graph on their own
PulseAudio input, so they share one format (48 kHz stereo) and one set of
loudness parameters without sharing a process: one output restarting cannot
disturb another, and the chain adds about 5 ms, so lip sync holds.

The graph is a causal AGC keyed on a BS.1770 K-weighted copy of the programme
(no lookahead), a 4x-oversampled limiter that keeps true peak under the
ceiling, and an ebur128 meter on a side branch (it holds audio in 100 ms
blocks, so it must never sit in the output path) whose frame metadata goes to
stderr for the bounded per-output loudness metric.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Mapping


SAMPLE_RATE = 48000
DEFAULT_TARGET_LUFS = -16.0
DEFAULT_TRUE_PEAK_CEILING_DBTP = -1.0
TARGET_RANGE_LUFS = (-31.0, -10.0)
CEILING_RANGE_DBTP = (-6.0, -0.5)
# Makeup gain and threshold place a 20:1 ratio so programmes from about the
# target -22 LU to +6 LU land on the target (calibrated on speech and music).
MAKEUP_DB = 15.0
THRESHOLD_BELOW_TARGET_DB = 16.0
# The limiter runs at 4x; resampling back can add a few tenths of a dB.
OVERSAMPLED_MARGIN_DB = 0.5
K_WEIGHTING = (
    "biquad=b0=1.53512485958697:b1=-2.69169618940638:b2=1.19839281085285:a0=1:a1=-1.69065929318241:a2=0.73248077421585,"
    "biquad=b0=1:b1=-2:b2=1:a0=1:a1=-1.99004745483398:a2=0.99007225036621"
)


def _setting(environ: Mapping[str, str], name: str, default: float, bounds: tuple[float, float]) -> float:
    text = environ.get(name, "")
    if text == "":
        return default
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{name} must be between {bounds[0]:g} and {bounds[1]:g}")
    return value


def enabled(environ: Mapping[str, str] = os.environ) -> bool:
    """``ALANA_PROCESSED_AUDIO``: on by default; off is the kill switch."""
    value = environ.get("ALANA_PROCESSED_AUDIO", "") or "on"
    if value not in ("on", "off"):
        raise ValueError("ALANA_PROCESSED_AUDIO must be on or off")
    return value == "on"


def target_lufs(environ: Mapping[str, str] = os.environ) -> float:
    return _setting(environ, "ALANA_LOUDNESS_TARGET", DEFAULT_TARGET_LUFS, TARGET_RANGE_LUFS)


def true_peak_ceiling(environ: Mapping[str, str] = os.environ) -> float:
    return _setting(environ, "ALANA_TRUE_PEAK_CEILING", DEFAULT_TRUE_PEAK_CEILING_DBTP, CEILING_RANGE_DBTP)


def filter_graph(source: str, output: str, target: float, ceiling: float) -> str:
    """Return the ``-filter_complex`` graph from ``[source]`` to ``[output]``."""
    threshold = 10 ** ((target - THRESHOLD_BELOW_TARGET_DB) / 20)
    makeup = 10 ** (MAKEUP_DB / 20)
    limit = 10 ** ((ceiling - OVERSAMPLED_MARGIN_DB) / 20)
    # Every encoder keeps its inputs on their shared wall clock (-copyts, #30), so
    # audio arrives with epoch-sized timestamps. ffmpeg 5.1's sidechaincompress
    # restarts its output at 0 whatever its input says, and the 4x aresample
    # overflows its timestamp arithmetic above about 1e9 s; either moved audio
    # against video (#34). So the processing runs on a copy rebased to 0, and the
    # result is put back on the input's own timeline by mixing it with a muted
    # copy of the input: amix takes its timestamps from its first input and its
    # samples, in order, from both.
    return (
        f"[{source}]aresample={SAMPLE_RATE},asplit=2[timeline][work];"
        "[work]asetpts=PTS-STARTPTS,asplit=2[program][key];"
        f"[key]{K_WEIGHTING}[weighted];"
        f"[program][weighted]sidechaincompress=threshold={threshold:.6f}:ratio=20:attack=800:release=4000:"
        f"makeup={makeup:.6f}:knee=2:detection=rms:link=average,"
        f"aresample={SAMPLE_RATE * 4},alimiter=limit={limit:.6f}:attack=5:release=50:level=disabled,"
        f"aresample={SAMPLE_RATE}[processed];"
        "[timeline]volume=0[carrier];"
        "[carrier][processed]amix=inputs=2:normalize=0:duration=first:dropout_transition=0,"
        f"asplit=2[{output}][meter];"
        "[meter]ebur128=peak=true:metadata=1,ametadata=mode=print:file=/dev/stderr,anullsink"
    )


def configured_graph(source: str, output: str, environ: Mapping[str, str] = os.environ) -> str:
    return filter_graph(source, output, target_lufs(environ), true_peak_ceiling(environ))


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "filter":
        raise SystemExit("usage: audio_chain.py filter SOURCE_LABEL OUTPUT_LABEL")
    try:
        enabled()
        print(configured_graph(argv[1], argv[2]))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
