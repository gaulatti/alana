import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from timecode import SPAN, Samples, beacon, render, summarize  # noqa: E402


class PixelTimecodeTests(unittest.TestCase):
    def test_recovers_rendered_ms_at_a_receiver_grid_offset(self):
        stamp = 1_800_000_000_123
        word = (0xA55A << 48) | stamp
        pixels = bytearray(1280 * 24)
        for bit in range(64):
            pixels[16 * 1280 + 640 + bit * 10 + 5] = 255 if word & (1 << (63 - bit)) else 0
        self.assertEqual(beacon(pixels, 1280, 640), stamp)
        self.assertIsNone(beacon(pixels, 1280))
        self.assertIsNone(beacon(pixels[:-1], 1280, 640))
        self.assertIsNone(beacon(pixels, 1280, -1))
        self.assertIsNone(beacon(pixels, 1280, 650))

    def test_rendered_band_round_trips_through_the_reader(self):
        band = bytearray(3 * SPAN * 24)
        for index, stamp in enumerate((0, 1_800_000_000_000, (1 << 48) - 1)):
            render(stamp, 3 * SPAN, index * SPAN, band)
        self.assertEqual([beacon(band, 3 * SPAN, index * SPAN) for index in range(3)],
                         [0, 1_800_000_000_000, (1 << 48) - 1])
        self.assertEqual(beacon(render(42, SPAN), SPAN), 42)
        # The sync word is drawn in every row of the band, as source.html paints full cells.
        self.assertEqual(render(42, SPAN)[:SPAN], render(42, SPAN)[23 * SPAN:])

    def test_render_rejects_stamps_and_offsets_the_reader_cannot_recover(self):
        for stamp in (-1, 1 << 48, 1.5, True, '1'):
            with self.assertRaises(ValueError):
                render(stamp, SPAN)
        for x in (-1, 1):
            with self.assertRaises(ValueError):
                render(1, SPAN, x)

    def test_nearest_rank_percentiles_and_invalid_samples(self):
        self.assertEqual(summarize(list(range(1, 101))), {'p50_ms': 50, 'p95_ms': 95, 'p99_ms': 99})
        self.assertEqual(summarize([0]), {'p50_ms': 0, 'p95_ms': 0, 'p99_ms': 0})
        for values in ([], [-1], [math.nan], [math.inf]):
            with self.assertRaises(ValueError):
                summarize(values)


class SampleCollectorTests(unittest.TestCase):
    def test_one_sample_per_new_stamp_and_warmup_is_not_measured(self):
        collected = Samples(['rtmp-1', 'livekit'], {'rtmp-1': 100})
        self.assertFalse(collected.add('rtmp-1', 100, 900))   # frozen frame from readiness
        self.assertFalse(collected.add('rtmp-1', 99, 900))    # older frame
        self.assertTrue(collected.add('rtmp-1', 110, 500, measure=False))
        self.assertTrue(collected.add('rtmp-1', 120, 620))
        self.assertFalse(collected.add('rtmp-1', 120, 700))
        self.assertTrue(collected.add('livekit', 5, 25))
        self.assertEqual(collected.values, {'rtmp-1': [500], 'livekit': [20]})
        self.assertEqual(collected.rows(1, 1, 12.5), [
            {'outputs': 1, 'receiver': 'rtmp-1', 'samples': 1, 'p50_ms': 500, 'p95_ms': 500, 'p99_ms': 500,
             'sender_cpu_percent': 12.5},
            {'outputs': 1, 'receiver': 'livekit', 'samples': 1, 'p50_ms': 20, 'p95_ms': 20, 'p99_ms': 20,
             'sender_cpu_percent': 12.5},
        ])

    def test_fails_closed_on_a_bad_clock_or_a_short_receiver(self):
        collected = Samples(['livekit'])
        collected.add('livekit', 100, 99)
        self.assertEqual(collected.invalid, {'livekit': 1})
        with self.assertRaises(RuntimeError):
            collected.rows(1, 0, 1.0)
        collected = Samples(['livekit'])
        collected.add('livekit', 100, 100 + Samples.LIMIT_MS)
        self.assertEqual(collected.invalid, {'livekit': 1})
        collected = Samples(['livekit'])
        collected.add('livekit', 100, 150)
        with self.assertRaises(RuntimeError):
            collected.rows(1, 2, 1.0)
