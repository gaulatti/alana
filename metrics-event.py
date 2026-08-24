#!/usr/bin/env python3
"""Record one bounded streaming-supervisor metric event."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alana_metrics import record_runtime_event


def main() -> None:
    if len(sys.argv) not in (3, 5):
        raise SystemExit("usage: metrics-event.py EVENT LEG [REASON BACKOFF_SECONDS]")
    event, leg = sys.argv[1:3]
    reason = sys.argv[3] if len(sys.argv) == 5 else ""
    try:
        backoff = float(sys.argv[4]) if len(sys.argv) == 5 else 0.0
        record_runtime_event(
            Path(os.environ.get("ALANA_RUNTIME_METRICS_FILE", "/run/alana/runtime-metrics.json")),
            event,
            leg,
            reason,
            backoff,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
