"""Timecode recovered from rendered pixels, not a transport-ready timestamp.

The layout mirrors source.html: 64 cells of 10 px (16 sync bits, then 48 bits of
UTC milliseconds) drawn white for 1 and black for 0 across the top of the frame.
The observer reads a 24 px grayscale band and samples row 16 at each cell centre.
"""
import math

SYNC = 0xA55A
BITS = 64
CELL = 10
BAND = 24
ROW = 16
SPAN = BITS * CELL
STAMP_MASK = (1 << 48) - 1


def beacon(frame, width, x=0):
    """Decode 16 sync bits + 48 UTC-ms bits from a grayscale framebuffer band."""
    if len(frame) != width * BAND or x < 0 or x + SPAN > width:
        return None
    word = 0
    for bit in range(BITS):
        word = (word << 1) | (frame[ROW * width + x + bit * CELL + CELL // 2] >= 128)
    if word >> 48 != SYNC:
        return None
    return word & STAMP_MASK


def render(stamp, width, x=0, frame=None):
    """Draw the beacon for ``stamp`` into a grayscale band, as source.html does."""
    if isinstance(stamp, bool) or not isinstance(stamp, int) or not 0 <= stamp <= STAMP_MASK:
        raise ValueError('Beacon stamp must be a 48-bit integer of UTC milliseconds')
    if x < 0 or x + SPAN > width:
        raise ValueError('Beacon must fit inside the band')
    frame = bytearray(width * BAND) if frame is None else frame
    word = (SYNC << 48) | stamp
    for bit in range(BITS):
        cell = bytes([255 if word & (1 << (BITS - 1 - bit)) else 0]) * CELL
        for row in range(BAND):
            start = row * width + x + bit * CELL
            frame[start:start + CELL] = cell
    return frame


class Samples:
    """Per-receiver samples: one per newly displayed source stamp, never per observed frame."""

    LIMIT_MS = 60_000

    def __init__(self, names, seen=None):
        self.names = list(names)
        self.seen = {name: (seen or {}).get(name) for name in self.names}
        self.values = {name: [] for name in self.names}
        self.invalid = {name: 0 for name in self.names}

    def add(self, name, stamp, received, measure=True):
        """Record a newly displayed stamp; a repeated or older frame is not a sample."""
        last = self.seen[name]
        if last is not None and stamp <= last:
            return False
        self.seen[name] = stamp
        if measure:
            latency = received - stamp
            if 0 <= latency < self.LIMIT_MS:
                self.values[name].append(latency)
            else:
                self.invalid[name] += 1
        return True

    def rows(self, outputs, minimum, cpu):
        """Summarize every receiver, failing closed when any is short or saw a bad clock."""
        rows = []
        for name in self.names:
            if len(self.values[name]) < minimum or self.invalid[name]:
                raise RuntimeError('Insufficient or invalid rendered timecode samples')
            rows.append({'outputs': outputs, 'receiver': name, 'samples': len(self.values[name]),
                         **summarize(self.values[name]), 'sender_cpu_percent': cpu})
        return rows


def summarize(values):
    """Nearest-rank percentiles; no synthesized samples or empty defaults."""
    if not values or any(not math.isfinite(x) or x < 0 for x in values):
        raise ValueError('Latency samples must be finite, nonnegative and nonempty')
    ordered = sorted(values)
    return {f'p{p}_ms': ordered[math.ceil(len(ordered) * p / 100) - 1]
            for p in (50, 95, 99)}
