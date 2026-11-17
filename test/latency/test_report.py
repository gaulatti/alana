import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report  # noqa: E402

COMMIT = 'a' * 40


def row(outputs, receiver, cpu=31.5, p50=900, p95=950, p99=990, samples=300):
    return {'outputs': outputs, 'receiver': receiver, 'samples': samples,
            'p50_ms': p50, 'p95_ms': p95, 'p99_ms': p99, 'sender_cpu_percent': cpu}


def result(kind='software-display', cpu=31.5):
    rows = [row(outputs, receiver, cpu) for outputs in (1, 3, 5)
            for receiver in [f'rtmp-{index}' for index in range(1, outputs + 1)] + ['livekit']]
    return {'schema': report.SCHEMA, 'rows': rows, 'metadata': {
        'kind': kind, 'physical_display_latency_measured': False, 'source_commit': COMMIT,
        'dirty_checkout': False, 'host': 'macOS-27.0-arm64-arm-64bit'}}


class ResultFormatTests(unittest.TestCase):
    def test_table_records_kind_commit_hardware_legs_and_cpu(self):
        lines = report.table(result()).splitlines()
        self.assertEqual(lines[0], report.KINDS['software-display'])
        self.assertEqual(lines[1], f'commit {COMMIT}')
        self.assertEqual(lines[2], 'host macOS-27.0-arm64-arm-64bit')
        self.assertEqual(lines[3].split(), ['outputs', 'leg', 'receiver', 'samples', 'p50_ms', 'p95_ms', 'p99_ms', 'cpu_%'])
        body = [line.split() for line in lines[4:]]
        self.assertEqual(len(body), 2 + 4 + 6)
        self.assertEqual(body[0], ['1', 'rtmp', 'rtmp-1', '300', '900.0', '950.0', '990.0', '31.5'])
        self.assertEqual(body[1][:3], ['1', 'livekit', 'livekit'])
        self.assertEqual([line[2] for line in body[-6:]], ['rtmp-1', 'rtmp-2', 'rtmp-3', 'rtmp-4', 'rtmp-5', 'livekit'])

    def test_synthetic_results_are_labelled_and_may_omit_cpu(self):
        text = report.table(result('synthetic', None))
        self.assertTrue(text.startswith('SYNTHETIC LOOP'))
        self.assertIn('Not a measurement of Alana', text)
        self.assertTrue(text.splitlines()[4].endswith('n/a'))
        dirty = result()
        dirty['metadata']['dirty_checkout'] = True
        self.assertIn('(dirty checkout)', report.table(dirty))
        idle = result(cpu=0)
        self.assertIs(report.validate(idle), idle)

    def test_rejects_incomplete_or_mislabelled_results(self):
        def broken(change):
            value = result()
            change(value)
            return value

        cases = [
            [],
            broken(lambda r: r.update(schema='alana-latency/v0')),
            broken(lambda r: r.update(metadata=None)),
            broken(lambda r: r['metadata'].update(kind='glass')),
            broken(lambda r: r['metadata'].update(physical_display_latency_measured=True)),
            broken(lambda r: r['metadata'].pop('physical_display_latency_measured')),
            broken(lambda r: r['metadata'].update(source_commit='abc')),
            broken(lambda r: r['metadata'].update(dirty_checkout='no')),
            broken(lambda r: r['metadata'].update(host='')),
            broken(lambda r: r['metadata'].pop('host')),
            broken(lambda r: r.update(rows=[])),
            broken(lambda r: r.update(rows={})),
            broken(lambda r: r['rows'].append('row')),
            broken(lambda r: r['rows'].append(row(2, 'rtmp-1'))),
            broken(lambda r: r['rows'].append(row(True, 'rtmp-1'))),
            broken(lambda r: r['rows'].append(row(1, 'rtmp-2'))),
            broken(lambda r: r['rows'].append(row(1, 'webrtc'))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', samples=0))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', samples=True))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', samples=1.0))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', p50=1000))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', p99=float('nan')))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', p50=-1))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', p50=True))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', cpu=None))),
            broken(lambda r: r['rows'].__setitem__(0, row(1, 'rtmp-1', cpu='12'))),
            broken(lambda r: r['rows'].append(row(1, 'livekit'))),
            broken(lambda r: r['rows'].pop(0)),
        ]
        for value in cases:
            with self.subTest(value=json.dumps(value)[:120]), self.assertRaises(report.ReportError):
                report.validate(value)

    def test_a_partial_case_set_is_allowed_but_each_case_is_complete(self):
        value = result()
        value['rows'] = [r for r in value['rows'] if r['outputs'] == 3]
        self.assertIs(report.validate(value), value)

    def test_command_prints_the_table_or_fails_without_printing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            path.write_text(json.dumps(result()))
            stdout = io.StringIO()
            report.main([str(path)], stdout)
            self.assertEqual(stdout.getvalue(), report.table(result()) + '\n')
            for content in ('{', json.dumps({'schema': report.SCHEMA})):
                path.write_text(content)
                stdout = io.StringIO()
                with self.assertRaises(SystemExit) as raised:
                    report.main([str(path)], stdout)
                self.assertTrue(str(raised.exception.code).startswith('Invalid latency result'))
                self.assertEqual(stdout.getvalue(), '')
            with self.assertRaises(SystemExit) as raised:
                report.main([str(Path(directory) / 'missing.json')], io.StringIO())
            self.assertTrue(str(raised.exception.code).startswith('Invalid latency result'))
        with self.assertRaises(SystemExit) as raised:
            report.main([], io.StringIO())
        self.assertIn('Usage', str(raised.exception.code))

    def test_script_entry_point_prints_the_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            path.write_text(json.dumps(result()))
            stdout = io.StringIO()
            with mock.patch.object(sys, 'argv', ['report.py', str(path)]), mock.patch.object(sys, 'stdout', stdout):
                runpy.run_path(report.__file__, run_name='__main__')
            self.assertEqual(stdout.getvalue(), report.table(result()) + '\n')
