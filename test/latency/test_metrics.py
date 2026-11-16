import math
import unittest

from metrics import beacon, summarize


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

    def test_nearest_rank_percentiles_and_invalid_samples(self):
        self.assertEqual(summarize(list(range(1, 101))), {'p50_ms': 50, 'p95_ms': 95, 'p99_ms': 99})
        self.assertEqual(summarize([0]), {'p50_ms': 0, 'p95_ms': 0, 'p99_ms': 0})
        for values in ([], [-1], [math.nan], [math.inf]):
            with self.assertRaises(ValueError):
                summarize(values)
