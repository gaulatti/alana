import os
import re
import tempfile
import unittest
from pathlib import Path

from alana_metrics import Metrics, record_runtime_event


SAMPLE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{[a-zA-Z_][a-zA-Z0-9_]*=\"(?:[^\"\\]|\\.)*\"(?:,[a-zA-Z_][a-zA-Z0-9_]*=\"(?:[^\"\\]|\\.)*\")*\})? [-+]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$"
)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime_path = Path(self.temp.name) / "runtime.json"
        self.metrics = Metrics(self.runtime_path)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return {
            "actualState": "degraded",
            "pipelineProcessHealthy": True,
            "browserHealthy": False,
            "rtmpHealth": [
                {"index": 1, "processHealthy": True, "progressing": True},
                {"index": 2, "processHealthy": False, "progressing": False},
            ],
            "livekitEnabled": True,
            "livekitHealthy": False,
        }

    def test_success_failure_and_runtime_outcomes_render_with_bounded_labels(self):
        self.metrics.observe_http("GET", "lifecycle", 200, 0.02)
        self.metrics.observe_http("POST", "lifecycle", 503, 1.2)
        self.metrics.observe_http("PUT", "filler", 200, 2.4)
        self.metrics.observe_dependency("start", "success", 0.1)
        self.metrics.observe_dependency("stop", "unavailable", 5.0)
        self.metrics.observe_dependency("prepare", "success", 2.0)
        self.metrics.observe_command("start", "success")
        self.metrics.observe_command("stop", "dependency_failure")
        self.metrics.observe_reconcile("failure")
        self.metrics.observe_preparation("success")
        self.metrics.observe_preparation("not_ready")
        record_runtime_event(self.runtime_path, "stall", "rtmp")
        record_runtime_event(self.runtime_path, "restart", "rtmp", "stall", 5)
        record_runtime_event(self.runtime_path, "fallback", "rtmp")

        rendered = self.metrics.render(self.snapshot())

        self.assertIn('alana_http_requests_total{method="GET",route="lifecycle",status_class="2xx"} 1', rendered)
        self.assertIn('alana_http_requests_total{method="POST",route="lifecycle",status_class="5xx"} 1', rendered)
        self.assertIn('alana_http_requests_total{method="PUT",route="filler",status_class="2xx"} 1', rendered)
        self.assertIn('alana_dependency_operations_total{dependency="croccante",operation="stop",result="unavailable"} 1', rendered)
        self.assertIn('alana_filler_preparations_total{result="success"} 1', rendered)
        self.assertIn("alana_filler_pending 0", rendered)
        self.assertIn('alana_stream_restarts_total{leg="rtmp",reason="stall"} 1', rendered)
        self.assertIn('alana_rtmp_outputs_configured 2', rendered)
        self.assertIn('alana_rtmp_outputs_healthy 1', rendered)

    def test_unknown_labels_and_runtime_events_fail_closed(self):
        with self.assertRaises(ValueError):
            self.metrics.observe_dependency("publish", "success", 0.1)
        with self.assertRaises(ValueError):
            self.metrics.observe_command("start", "program-123")
        with self.assertRaises(ValueError):
            self.metrics.observe_preparation("program-123")
        with self.assertRaises(ValueError):
            record_runtime_event(self.runtime_path, "restart", "rtmp", "https://private.example", 5)
        with self.assertRaises(ValueError):
            record_runtime_event(self.runtime_path, "restart", "program-123", "exit", 5)

    def test_exposition_is_parseable_and_contains_no_sensitive_values(self):
        secret_values = ["program-raw-id", "rtmp://publisher/private-key", "wss://room/private"]
        previous = os.environ.get("ALANA_BUILD_VERSION")
        os.environ["ALANA_BUILD_VERSION"] = secret_values[0] + "/invalid"
        try:
            rendered = self.metrics.render(self.snapshot())
        finally:
            if previous is None:
                os.environ.pop("ALANA_BUILD_VERSION", None)
            else:
                os.environ["ALANA_BUILD_VERSION"] = previous

        metadata = set()
        for line in rendered.splitlines():
            if line.startswith(("# HELP ", "# TYPE ")):
                self.assertNotIn(line, metadata)
                metadata.add(line)
            elif line:
                self.assertRegex(line, SAMPLE)
        for value in secret_values:
            self.assertNotIn(value, rendered)
        self.assertIn('version="unknown"', rendered)


if __name__ == "__main__":
    unittest.main()
