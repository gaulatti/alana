"""Bounded Prometheus instrumentation for the Alana runtime."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import resource
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


HISTOGRAM_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "OTHER"})
HTTP_ROUTES = frozenset({"metrics", "lifecycle", "filler", "destinations", "unknown"})
STATUS_CLASSES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "unknown"})
DEPENDENCY_OPERATIONS = frozenset(
    {"start", "stop", "prepare", "filler_status", "destination_reload", "status"}
)
DEPENDENCY_RESULTS = frozenset({"success", "http_error", "unavailable", "invalid_response", "unexpected_state", "token_unavailable"})
COMMAND_ACTIONS = frozenset({"start", "stop"})
COMMAND_RESULTS = frozenset({"success", "conflict", "dependency_failure", "not_ready", "failure"})
RECONCILE_RESULTS = frozenset({"success", "failure"})
PREPARATION_RESULTS = frozenset(
    {"success", "failure", "conflict", "unavailable", "duplicate", "reconciled", "not_ready"}
)
DESTINATION_ACTIONS = frozenset({"reload"})
DESTINATION_RESULTS = frozenset(
    {"success", "failure", "conflict", "duplicate", "active"}
)
LIFECYCLE_STATES = ("stopped", "starting", "running", "stopping", "degraded", "failed", "transitioning", "unknown")
RUNTIME_LEGS = frozenset({"browser", "audio", "rtmp", "livekit"})
RESTART_REASONS = frozenset({"watchdog", "exit", "stall", "encoder_unavailable"})
RUNTIME_EVENTS = frozenset({"restart", "stall", "fallback"})


def _labels(values: dict[str, str]) -> str:
    if not values:
        return ""
    escaped = []
    for key, value in sorted(values.items()):
        safe = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        escaped.append(f'{key}="{safe}"')
    return "{" + ",".join(escaped) + "}"


def _status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status <= 599 else "unknown"


def _histogram() -> dict[str, object]:
    return {"count": 0, "sum": 0.0, "buckets": {str(value): 0 for value in HISTOGRAM_BUCKETS}}


def _observe_histogram(histogram: dict[str, object], value: float) -> None:
    histogram["count"] = int(histogram.get("count", 0)) + 1
    histogram["sum"] = float(histogram.get("sum", 0.0)) + value
    buckets = histogram.setdefault("buckets", {})
    assert isinstance(buckets, dict)
    for boundary in HISTOGRAM_BUCKETS:
        if value <= boundary:
            key = str(boundary)
            buckets[key] = int(buckets.get(key, 0)) + 1


@contextmanager
def _runtime_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _empty_runtime_state() -> dict[str, object]:
    return {"restarts": {}, "stalls": {}, "fallbacks": {}, "backoff": {}}


def _read_runtime_state(path: Path) -> dict[str, object]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else _empty_runtime_state()
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _empty_runtime_state()


def _mapping(value: object) -> dict[object, object]:
    """Treat a corrupt persisted section as empty instead of failing a scrape."""
    return value if isinstance(value, dict) else {}


def record_runtime_event(path: Path, event: str, leg: str, reason: str = "", backoff: float = 0.0) -> None:
    if event not in RUNTIME_EVENTS or leg not in RUNTIME_LEGS:
        raise ValueError("unknown runtime metric event")
    if event == "restart" and reason not in RESTART_REASONS:
        raise ValueError("unknown restart reason")
    if not math.isfinite(backoff) or backoff < 0 or backoff > 3600:
        raise ValueError("invalid restart backoff")

    with _runtime_lock(path):
        state = _read_runtime_state(path)
        if event == "restart":
            restarts = state.setdefault("restarts", {})
            assert isinstance(restarts, dict)
            key = f"{leg}|{reason}"
            restarts[key] = int(restarts.get(key, 0)) + 1
            backoffs = state.setdefault("backoff", {})
            assert isinstance(backoffs, dict)
            histogram = backoffs.setdefault(leg, _histogram())
            assert isinstance(histogram, dict)
            _observe_histogram(histogram, backoff)
        elif event == "stall":
            stalls = state.setdefault("stalls", {})
            assert isinstance(stalls, dict)
            stalls[leg] = int(stalls.get(leg, 0)) + 1
        else:
            fallbacks = state.setdefault("fallbacks", {})
            assert isinstance(fallbacks, dict)
            fallbacks[leg] = int(fallbacks.get(leg, 0)) + 1

        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)


class Metrics:
    def __init__(self, runtime_path: Path | None = None):
        self.started_at = time.time()
        self.runtime_path = runtime_path or Path(os.environ.get("ALANA_RUNTIME_METRICS_FILE", "/run/alana/runtime-metrics.json"))
        self.lock = threading.Lock()
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self.histograms: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, object]] = {}

    @staticmethod
    def _validate(labels: dict[str, str], schema: dict[str, frozenset[str]]) -> tuple[tuple[str, str], ...]:
        if set(labels) != set(schema):
            raise ValueError("metric labels do not match the closed schema")
        for key, value in labels.items():
            if value not in schema[key]:
                raise ValueError("metric label is outside the closed schema")
        return tuple(sorted(labels.items()))

    def _increment(self, name: str, labels: dict[str, str], schema: dict[str, frozenset[str]]) -> None:
        key = (name, self._validate(labels, schema))
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + 1

    def _observe(self, name: str, labels: dict[str, str], schema: dict[str, frozenset[str]], value: float) -> None:
        key = (name, self._validate(labels, schema))
        with self.lock:
            histogram = self.histograms.setdefault(key, _histogram())
            _observe_histogram(histogram, value)

    def observe_http(self, method: str, route: str, status: int, duration: float) -> None:
        normalized_method = method if method in HTTP_METHODS else "OTHER"
        normalized_route = route if route in HTTP_ROUTES else "unknown"
        status_class = _status_class(status)
        self._increment("alana_http_requests_total", {"method": normalized_method, "route": normalized_route, "status_class": status_class}, {"method": HTTP_METHODS, "route": HTTP_ROUTES, "status_class": STATUS_CLASSES})
        self._observe("alana_http_request_duration_seconds", {"method": normalized_method, "route": normalized_route}, {"method": HTTP_METHODS, "route": HTTP_ROUTES}, max(0.0, duration))

    def observe_dependency(self, operation: str, result: str, duration: float) -> None:
        labels = {"dependency": "croccante", "operation": operation, "result": result}
        self._increment("alana_dependency_operations_total", labels, {"dependency": frozenset({"croccante"}), "operation": DEPENDENCY_OPERATIONS, "result": DEPENDENCY_RESULTS})
        self._observe("alana_dependency_duration_seconds", {"dependency": "croccante", "operation": operation}, {"dependency": frozenset({"croccante"}), "operation": DEPENDENCY_OPERATIONS}, max(0.0, duration))

    def observe_command(self, action: str, result: str) -> None:
        self._increment("alana_lifecycle_commands_total", {"action": action, "result": result}, {"action": COMMAND_ACTIONS, "result": COMMAND_RESULTS})

    def observe_reconcile(self, result: str) -> None:
        self._increment("alana_reconcile_cycles_total", {"result": result}, {"result": RECONCILE_RESULTS})

    def observe_preparation(self, result: str) -> None:
        self._increment(
            "alana_filler_preparations_total",
            {"result": result},
            {"result": PREPARATION_RESULTS},
        )

    def observe_destination(self, action: str, result: str) -> None:
        self._increment(
            "alana_destination_operations_total",
            {"action": action, "result": result},
            {"action": DESTINATION_ACTIONS, "result": DESTINATION_RESULTS},
        )

    @staticmethod
    def _gauge(lines: list[str], name: str, help_text: str, value: float | int, labels: dict[str, str] | None = None) -> None:
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name}{_labels(labels or {})} {value}"))

    @staticmethod
    def _render_histogram(lines: list[str], name: str, help_text: str, labels: dict[str, str], histogram: dict[str, object]) -> None:
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} histogram"))
        buckets = histogram.get("buckets", {})
        assert isinstance(buckets, dict)
        for boundary in HISTOGRAM_BUCKETS:
            lines.append(f'{name}_bucket{_labels({**labels, "le": str(boundary)})} {int(buckets.get(str(boundary), 0))}')
        lines.append(f'{name}_bucket{_labels({**labels, "le": "+Inf"})} {int(histogram.get("count", 0))}')
        lines.append(f'{name}_sum{_labels(labels)} {float(histogram.get("sum", 0.0)):.9g}')
        lines.append(f'{name}_count{_labels(labels)} {int(histogram.get("count", 0))}')

    def render(self, snapshot: dict[str, object]) -> str:
        lines: list[str] = []
        version = os.environ.get("ALANA_BUILD_VERSION", "dev")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version):
            version = "unknown"
        self._gauge(lines, "alana_service_info", "Alana service, runtime, and build identity.", 1, {"runtime": "python", "service": "alana", "version": version})
        self._gauge(lines, "alana_process_start_time_seconds", "Unix time when the Alana control process started.", f"{self.started_at:.6f}")
        try:
            resident_pages = int(Path("/proc/self/statm").read_text(encoding="utf-8").split()[1])
            resident = resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (FileNotFoundError, OSError, ValueError, IndexError):
            resident = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1024 if os.uname().sysname != "Darwin" else 1)
        self._gauge(lines, "alana_process_resident_memory_bytes", "Resident memory used by the Alana control process.", resident)

        with self.lock:
            counters = dict(self.counters)
            histograms = {key: json.loads(json.dumps(value)) for key, value in self.histograms.items()}
        counter_help = {
            "alana_http_requests_total": "Control HTTP requests by bounded route and outcome.",
            "alana_dependency_operations_total": "Croccante control operations by bounded outcome.",
            "alana_lifecycle_commands_total": "Lifecycle commands by bounded action and result.",
            "alana_reconcile_cycles_total": "Lifecycle reconciliation cycles by result.",
            "alana_filler_preparations_total": "Filler preparation and reconciliation outcomes.",
            "alana_destination_operations_total": "Destination validation and reload outcomes.",
        }
        for (name, label_pairs), value in sorted(counters.items()):
            lines.extend((f"# HELP {name} {counter_help[name]}", f"# TYPE {name} counter", f"{name}{_labels(dict(label_pairs))} {value}"))
        histogram_help = {
            "alana_http_request_duration_seconds": "Control HTTP request duration.",
            "alana_dependency_duration_seconds": "Croccante control operation duration.",
        }
        for (name, label_pairs), histogram in sorted(histograms.items()):
            self._render_histogram(lines, name, histogram_help[name], dict(label_pairs), histogram)

        actual = str(snapshot.get("actualState") or "unknown")
        actual = actual if actual in LIFECYCLE_STATES else "unknown"
        for state in LIFECYCLE_STATES:
            self._gauge(lines, "alana_lifecycle_state", "Current lifecycle state as a bounded one-hot gauge.", int(state == actual), {"state": state})
        self._gauge(lines, "alana_pipeline_process_healthy", "Whether the supervised pipeline process is alive.", int(bool(snapshot.get("pipelineProcessHealthy"))))
        self._gauge(lines, "alana_browser_healthy", "Whether the Chromium capture process is alive.", int(bool(snapshot.get("browserHealthy"))))
        rtmp = snapshot.get("rtmpHealth") if isinstance(snapshot.get("rtmpHealth"), list) else []
        self._gauge(lines, "alana_rtmp_outputs_configured", "Configured RTMP output count.", len(rtmp))
        self._gauge(lines, "alana_rtmp_outputs_healthy", "RTMP outputs with a live encoder process.", sum(1 for leg in rtmp if isinstance(leg, dict) and leg.get("processHealthy")))
        self._gauge(lines, "alana_rtmp_outputs_progressing", "RTMP outputs reporting frame progress.", sum(1 for leg in rtmp if isinstance(leg, dict) and leg.get("progressing")))
        self._gauge(lines, "alana_livekit_enabled", "Whether the LiveKit output is configured.", int(bool(snapshot.get("livekitEnabled"))))
        livekit_healthy = snapshot.get("livekitHealthy")
        self._gauge(lines, "alana_livekit_healthy", "Whether the configured LiveKit output is healthy.", int(bool(livekit_healthy)))
        active_filler = snapshot.get("activeFiller")
        pending_filler = snapshot.get("pendingFiller")
        self._gauge(lines, "alana_filler_active", "Whether the current session is bound to a filler version.", int(isinstance(active_filler, dict)))
        self._gauge(lines, "alana_filler_pending", "Whether a next-session filler version is configured.", int(isinstance(pending_filler, dict)))
        self._gauge(
            lines,
            "alana_filler_pending_ready",
            "Whether the configured next-session filler version is acknowledged ready.",
            int(isinstance(pending_filler, dict) and pending_filler.get("ready") is True),
        )
        active_destinations = snapshot.get("activeDestinations")
        pending_destinations = snapshot.get("pendingDestinations")
        self._gauge(
            lines,
            "alana_destinations_active",
            "Opaque destinations bound to the active broadcast.",
            int(active_destinations.get("count", 0))
            if isinstance(active_destinations, dict)
            else 0,
        )
        self._gauge(
            lines,
            "alana_destinations_pending",
            "Opaque destinations in the validated next selection.",
            int(pending_destinations.get("count", 0))
            if isinstance(pending_destinations, dict)
            else 0,
        )

        with _runtime_lock(self.runtime_path):
            runtime = _read_runtime_state(self.runtime_path)
        for key, value in sorted(_mapping(runtime.get("restarts")).items(), key=lambda item: str(item[0])):
            if not isinstance(key, str) or "|" not in key:
                continue
            leg, reason = key.split("|", 1)
            if leg not in RUNTIME_LEGS or reason not in RESTART_REASONS:
                continue
            try:
                count = max(0, int(value))
            except (TypeError, ValueError):
                continue
            lines.extend(("# HELP alana_stream_restarts_total Streaming supervisor restarts by bounded leg and reason.", "# TYPE alana_stream_restarts_total counter", f'alana_stream_restarts_total{_labels({"leg": leg, "reason": reason})} {count}'))
        for leg, value in sorted(_mapping(runtime.get("stalls")).items(), key=lambda item: str(item[0])):
            if leg not in RUNTIME_LEGS:
                continue
            try:
                count = max(0, int(value))
            except (TypeError, ValueError):
                continue
            lines.extend(("# HELP alana_stream_stalls_total Detected streaming stalls by bounded leg.", "# TYPE alana_stream_stalls_total counter", f'alana_stream_stalls_total{_labels({"leg": leg})} {count}'))
        for leg, value in sorted(_mapping(runtime.get("fallbacks")).items(), key=lambda item: str(item[0])):
            if leg not in RUNTIME_LEGS:
                continue
            try:
                count = max(0, int(value))
            except (TypeError, ValueError):
                continue
            lines.extend(("# HELP alana_software_fallbacks_total Explicit software encoder fallbacks by bounded leg.", "# TYPE alana_software_fallbacks_total counter", f'alana_software_fallbacks_total{_labels({"leg": leg})} {count}'))
        for leg, histogram in sorted(_mapping(runtime.get("backoff")).items(), key=lambda item: str(item[0])):
            if leg in RUNTIME_LEGS and isinstance(histogram, dict):
                self._render_histogram(lines, "alana_restart_backoff_seconds", "Configured restart backoff observed by bounded leg.", {"leg": leg}, histogram)
        metadata: set[str] = set()
        output: list[str] = []
        for line in lines:
            if line.startswith(("# HELP ", "# TYPE ")):
                if line in metadata:
                    continue
                metadata.add(line)
            output.append(line)
        return "\n".join(output) + "\n"
