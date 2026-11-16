"""Timecode recovered from rendered pixels, not a transport-ready timestamp."""
import math


def beacon(frame, width, x=0):
    """Decode 16 sync bits + 48 UTC-ms bits from a grayscale framebuffer row."""
    if len(frame) != width * 24 or x < 0 or x + 640 > width:
        return None
    word = 0
    for bit in range(64):
        word = (word << 1) | (frame[16 * width + x + bit * 10 + 5] >= 128)
    if word >> 48 != 0xA55A:
        return None
    return word & ((1 << 48) - 1)


def summarize(values):
    """Nearest-rank percentiles; no synthesized samples or empty defaults."""
    if not values or any(not math.isfinite(x) or x < 0 for x in values):
        raise ValueError('Latency samples must be finite, nonnegative and nonempty')
    ordered = sorted(values)
    return {f'p{p}_ms': ordered[math.ceil(len(ordered) * p / 100) - 1]
            for p in (50, 95, 99)}
