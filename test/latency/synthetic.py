#!/usr/bin/env python3
"""Synthetic self-test of the benchmark chain: no Alana, ffmpeg, Docker or display.

Every source frame's timestamp is drawn into a receiver band with the same pixel
layout source.html uses, delayed by an injected per-leg delay, decoded by the
same beacon reader and collector the real worker uses, then summarized and
printed by the same report. The injected delays are made-up round numbers. The
result is labelled ``synthetic`` in the JSON and the table: it proves the
harness arithmetic, never Alana's latency.

Usage: python3 test/latency/synthetic.py [--output FILE]
"""
import argparse
import json
import platform
from pathlib import Path
import random
import subprocess
import sys

from report import SCHEMA, table, validate
from timecode import SPAN, Samples, beacon, render

ROOT = Path(__file__).resolve().parents[2]
FPS = 30
# Injected, not measured. Deliberately round so nobody mistakes them for data.
DELAY_MS = {'rtmp': (1000, 100), 'livekit': (200, 50)}
PER_ROW = 3


def frames(seconds, start=1_800_000_000_000):
    """Source stamps at the benchmark frame rate, as integer UTC milliseconds."""
    return [start + index * 1000 // FPS for index in range(seconds * FPS)]


def run_case(outputs, seconds, rng):
    names = [f'rtmp-{index}' for index in range(1, outputs + 1)] + ['livekit']
    collected = Samples(names)
    for stamp in frames(seconds):
        # The RTMP receivers sit three to a row, so decoding exercises grid offsets.
        for first in range(0, outputs, PER_ROW):
            row = names[first:min(outputs, first + PER_ROW)]
            band = bytearray(len(row) * SPAN * 24)
            base, jitter = DELAY_MS['rtmp']
            delays = []
            for index in range(len(row)):
                render(stamp, len(row) * SPAN, index * SPAN, band)
                delays.append(base + rng.randrange(jitter + 1))
            for index, name in enumerate(row):
                collected.add(name, beacon(band, len(row) * SPAN, index * SPAN), stamp + delays[index])
        base, jitter = DELAY_MS['livekit']
        band = render(stamp, SPAN)
        received = stamp + base + rng.randrange(jitter + 1)
        collected.add('livekit', beacon(band, SPAN), received)
        # A receiver that keeps showing the same frame must not add a sample.
        collected.add('livekit', beacon(band, SPAN), received + 1000 // FPS)
    return collected.rows(outputs, seconds * FPS, None)


def commit():
    head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True)
    status = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], capture_output=True, text=True, check=True)
    return head.stdout.strip(), bool(status.stdout.strip())


def run(seconds=10, seed=28, cases=(1, 3, 5), revision=commit):
    rng = random.Random(seed)
    source_commit, dirty = revision()
    rows = [row for outputs in cases for row in run_case(outputs, seconds, rng)]
    return validate({
        'schema': SCHEMA,
        'metadata': {
            'kind': 'synthetic',
            'measurement': 'injected delays through the rendered-pixel codec; not an Alana measurement',
            'physical_display_latency_measured': False,
            'injected_delay_ms': {name: {'base': base, 'jitter_max': jitter} for name, (base, jitter) in DELAY_MS.items()},
            'seed': seed, 'measurement_seconds': seconds, 'fps': FPS,
            'source_commit': source_commit, 'dirty_checkout': dirty,
            'host': platform.platform(), 'host_machine': platform.machine(),
        },
        'rows': rows,
    })


def main(argv=None, stdout=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--seconds', type=int, default=10)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.seconds < 1:
        parser.error('Use at least one second')
    report = run(args.seconds)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(table(report), file=stdout)


if __name__ == '__main__':
    main()
