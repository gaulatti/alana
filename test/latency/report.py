#!/usr/bin/env python3
"""Validate a latency result file and print its p50/p95/p99 table.

Usage: python3 test/latency/report.py RESULT.json
"""
import json
import math
from pathlib import Path
import re
import sys

SCHEMA = 'alana-latency/v1'
OUTPUT_CASES = (1, 3, 5)
# Every kind names what was actually measured. Only an end-to-end run of the
# real publisher may claim to be anything other than a synthetic loop.
KINDS = {
    'synthetic': 'SYNTHETIC LOOP: injected delays through the pixel codec. Not a measurement of Alana.',
    'software-display': 'Software display to display, local only: no physical panel, camera or CDN path.',
}
RECEIVER = re.compile(r'rtmp-([1-5])|livekit')


class ReportError(ValueError):
    """A result file that must not be printed or committed as a baseline."""


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def leg(receiver):
    return 'livekit' if receiver == 'livekit' else 'rtmp'


def validate(report):
    """Return ``report`` when it is a complete, honestly labelled result."""
    if not isinstance(report, dict) or report.get('schema') != SCHEMA:
        raise ReportError(f'Result must declare schema {SCHEMA}')
    metadata = report.get('metadata')
    if not isinstance(metadata, dict) or metadata.get('kind') not in KINDS:
        raise ReportError('Result metadata must name a known measurement kind')
    if metadata.get('physical_display_latency_measured') is not False:
        raise ReportError('No supported kind measures physical display latency')
    if not re.fullmatch(r'[0-9a-f]{40}', str(metadata.get('source_commit'))):
        raise ReportError('Result must record the full source commit')
    if not isinstance(metadata.get('dirty_checkout'), bool):
        raise ReportError('Result must record whether the checkout was dirty')
    if not isinstance(metadata.get('host'), str) or not metadata['host']:
        raise ReportError('Result must record the host hardware')
    rows = report.get('rows')
    if not isinstance(rows, list) or not rows:
        raise ReportError('Result must contain measured rows')
    cases = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('outputs') not in OUTPUT_CASES or isinstance(row['outputs'], bool):
            raise ReportError('Each row must be a 1, 3 or 5 output case')
        match = RECEIVER.fullmatch(str(row.get('receiver')))
        if not match or (match.group(1) and int(match.group(1)) > row['outputs']):
            raise ReportError('Row receiver must be rtmp-N within the case or livekit')
        samples = row.get('samples')
        if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
            raise ReportError('Row must count at least one sample')
        percentiles = [row.get(key) for key in ('p50_ms', 'p95_ms', 'p99_ms')]
        if not all(_number(value) for value in percentiles) or percentiles != sorted(percentiles):
            raise ReportError('Row percentiles must be finite, nonnegative and ordered')
        cpu = row.get('sender_cpu_percent')
        if not (_number(cpu) or (cpu is None and metadata['kind'] == 'synthetic')):
            raise ReportError('Row must record publisher CPU; only a synthetic loop has none')
        receivers = cases.setdefault(row['outputs'], [])
        if row['receiver'] in receivers:
            raise ReportError('Row receiver is duplicated within its case')
        receivers.append(row['receiver'])
    for outputs, receivers in cases.items():
        if sorted(receivers) != sorted([f'rtmp-{index}' for index in range(1, outputs + 1)] + ['livekit']):
            raise ReportError('Each case must report every RTMP receiver and the LiveKit receiver')
    return report


def table(report):
    """Render a validated report as the benchmark's printed table."""
    validate(report)
    metadata = report['metadata']
    lines = [
        KINDS[metadata['kind']],
        f'commit {metadata["source_commit"]}{" (dirty checkout)" if metadata["dirty_checkout"] else ""}',
        f'host {metadata["host"]}',
        f'{"outputs":>7} {"leg":<7} {"receiver":<8} {"samples":>7} {"p50_ms":>8} {"p95_ms":>8} {"p99_ms":>8} {"cpu_%":>7}',
    ]
    for row in sorted(report['rows'], key=lambda row: (row['outputs'], leg(row['receiver']) == 'livekit', row['receiver'])):
        cpu = 'n/a' if row['sender_cpu_percent'] is None else f'{row["sender_cpu_percent"]:.1f}'
        lines.append(f'{row["outputs"]:>7} {leg(row["receiver"]):<7} {row["receiver"]:<8} {row["samples"]:>7} '
                     f'{row["p50_ms"]:>8.1f} {row["p95_ms"]:>8.1f} {row["p99_ms"]:>8.1f} {cpu:>7}')
    return '\n'.join(lines)


def main(argv=None, stdout=sys.stdout):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit(__doc__.strip())
    try:
        report = json.loads(Path(argv[0]).read_text())
        print(table(report), file=stdout)
    except (OSError, json.JSONDecodeError, ReportError) as error:
        raise SystemExit(f'Invalid latency result: {error}')


if __name__ == '__main__':
    main()
