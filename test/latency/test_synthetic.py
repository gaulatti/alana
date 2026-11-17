import io
import json
from pathlib import Path
import random
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report  # noqa: E402
import synthetic  # noqa: E402
from timecode import summarize  # noqa: E402

FIXED = ('b' * 40, False)


class SyntheticLoopTests(unittest.TestCase):
    def test_recovers_injected_delays_exactly_through_the_pixel_codec(self):
        rng = random.Random(7)
        rows = synthetic.run_case(5, 2, rng)
        # Replay the same random stream to know what was injected, independently of the codec.
        replay = random.Random(7)
        injected = {name: [] for name in [f'rtmp-{index}' for index in range(1, 6)] + ['livekit']}
        for _ in synthetic.frames(2):
            for name in [f'rtmp-{index}' for index in range(1, 4)]:
                injected[name].append(1000 + replay.randrange(101))
            for name in ('rtmp-4', 'rtmp-5'):
                injected[name].append(1000 + replay.randrange(101))
            injected['livekit'].append(200 + replay.randrange(51))
        self.assertEqual([row['receiver'] for row in rows], list(injected))
        for row in rows:
            self.assertEqual(row['samples'], 60)
            self.assertIsNone(row['sender_cpu_percent'])
            self.assertEqual({key: row[key] for key in ('p50_ms', 'p95_ms', 'p99_ms')}, summarize(injected[row['receiver']]))

    def test_without_jitter_every_percentile_is_the_injected_base(self):
        with mock.patch.dict(synthetic.DELAY_MS, {'rtmp': (700, 0), 'livekit': (150, 0)}):
            rows = synthetic.run_case(3, 1, random.Random(1))
        self.assertEqual({(row['receiver'], row['p50_ms'], row['p99_ms']) for row in rows},
                         {('rtmp-1', 700, 700), ('rtmp-2', 700, 700), ('rtmp-3', 700, 700), ('livekit', 150, 150)})

    def test_frames_follow_the_benchmark_rate_in_integer_milliseconds(self):
        self.assertEqual(synthetic.frames(1)[:4], [1_800_000_000_000, 1_800_000_000_033, 1_800_000_000_066, 1_800_000_000_100])
        self.assertEqual(len(synthetic.frames(3)), 90)

    def test_report_is_labelled_synthetic_and_valid(self):
        value = synthetic.run(1, revision=lambda: FIXED)
        self.assertIs(report.validate(value), value)
        self.assertEqual(value['metadata']['kind'], 'synthetic')
        self.assertFalse(value['metadata']['physical_display_latency_measured'])
        self.assertEqual(value['metadata']['source_commit'], 'b' * 40)
        self.assertEqual(value['metadata']['injected_delay_ms'], {'rtmp': {'base': 1000, 'jitter_max': 100},
                                                                   'livekit': {'base': 200, 'jitter_max': 50}})
        self.assertEqual(sorted({row['outputs'] for row in value['rows']}), [1, 3, 5])
        self.assertEqual(synthetic.run(1, revision=lambda: FIXED), value)

    def test_commit_reads_this_checkout(self):
        head, dirty = synthetic.commit()
        self.assertEqual(head, subprocess.run(['git', '-C', str(synthetic.ROOT), 'rev-parse', 'HEAD'],
                                              capture_output=True, text=True, check=True).stdout.strip())
        self.assertIsInstance(dirty, bool)

    def test_command_prints_and_optionally_writes_the_labelled_table(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(synthetic, 'commit', lambda: FIXED):
            path = Path(directory) / 'synthetic.json'
            stdout = io.StringIO()
            synthetic.main(['--seconds', '1', '--output', str(path)], stdout)
            written = json.loads(path.read_text())
            self.assertEqual(stdout.getvalue(), report.table(written) + '\n')
            self.assertTrue(stdout.getvalue().startswith('SYNTHETIC LOOP'))
            stdout = io.StringIO()
            synthetic.main(['--seconds', '1'], stdout)
            self.assertEqual(stdout.getvalue(), report.table(written) + '\n')
        with self.assertRaises(SystemExit), mock.patch.object(sys, 'stderr', io.StringIO()):
            synthetic.main(['--seconds', '0'], io.StringIO())

    def test_script_entry_point_runs_the_loop(self):
        stdout = io.StringIO()
        with mock.patch.object(sys, 'argv', ['synthetic.py', '--seconds', '1']), mock.patch.object(sys, 'stdout', stdout):
            runpy.run_path(synthetic.__file__, run_name='__main__')
        self.assertTrue(stdout.getvalue().startswith('SYNTHETIC LOOP'))
