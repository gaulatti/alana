import io
import json
import os
import re
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import alana_metrics
from alana_metrics import Metrics, monitor_loudness, record_loudness, record_runtime_event


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
            "activeDestinations": {"count": 2},
            "pendingDestinations": {"count": 1},
            "recording": {
                "enabled": True,
                "state": "active",
                "segmentCount": 3,
                "bytes": 4096,
                "durationSeconds": 15.0,
                "droppedFrames": 1,
                "errors": 2,
                "restarts": 1,
                "finalizationState": "not-requested",
                "disk": {
                    "freeBytes": 10000,
                    "usageBytes": 4096,
                    "quotaBytes": 100000,
                    "minFreeBytes": 1000,
                },
            },
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
        self.metrics.observe_destination("reload", "success")
        self.metrics.observe_recording_command("start", "success")
        self.metrics.observe_unrecognized("filler_profile", "unknown_field")
        self.metrics.observe_unrecognized("command_body", "unexpected_body")
        record_runtime_event(self.runtime_path, "stall", "rtmp")
        record_runtime_event(self.runtime_path, "restart", "rtmp", "stall", 5)
        record_runtime_event(self.runtime_path, "fallback", "rtmp")

        rendered = self.metrics.render(self.snapshot())

        self.assertIn('alana_http_requests_total{method="GET",route="lifecycle",status_class="2xx"} 1', rendered)
        self.assertIn('alana_http_requests_total{method="POST",route="lifecycle",status_class="5xx"} 1', rendered)
        self.assertIn('alana_http_requests_total{method="PUT",route="filler",status_class="2xx"} 1', rendered)
        self.assertIn('alana_dependency_operations_total{dependency="croccante",operation="stop",result="unavailable"} 1', rendered)
        self.assertIn('alana_filler_preparations_total{result="success"} 1', rendered)
        self.assertIn(
            'alana_destination_operations_total{action="reload",result="success"} 1',
            rendered,
        )
        self.assertIn(
            'alana_unrecognized_input_total{boundary="filler_profile",kind="unknown_field"} 1',
            rendered,
        )
        self.assertIn(
            'alana_unrecognized_input_total{boundary="command_body",kind="unexpected_body"} 1',
            rendered,
        )
        self.assertIn("# TYPE alana_unrecognized_input_total counter", rendered)
        self.assertIn("alana_destinations_active 2", rendered)
        self.assertIn("alana_destinations_pending 1", rendered)
        self.assertIn("alana_filler_pending 0", rendered)
        self.assertIn('alana_stream_restarts_total{leg="rtmp",reason="stall"} 1', rendered)
        self.assertIn('alana_rtmp_outputs_configured 2', rendered)
        self.assertIn('alana_rtmp_outputs_healthy 1', rendered)
        self.assertIn('alana_recording_state{state="active"} 1', rendered)
        self.assertIn("alana_recording_segments 3", rendered)
        self.assertIn("alana_recording_errors_total 2", rendered)
        self.assertIn("# TYPE alana_recording_errors_total counter", rendered)

    def test_unknown_labels_and_runtime_events_fail_closed(self):
        with self.assertRaises(ValueError):
            self.metrics.observe_dependency("publish", "success", 0.1)
        with self.assertRaises(ValueError):
            self.metrics.observe_command("start", "program-123")
        with self.assertRaises(ValueError):
            self.metrics.observe_preparation("program-123")
        with self.assertRaises(ValueError):
            self.metrics.observe_destination("reload", "program-123")
        with self.assertRaises(ValueError):
            self.metrics.observe_recording_command("start", "program-123")
        with self.assertRaises(ValueError):
            self.metrics.observe_unrecognized("pixelFormat", "unknown_field")
        with self.assertRaises(ValueError):
            self.metrics.observe_unrecognized("filler_status", "queued")
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


# ffmpeg ametadata=mode=print output for two ebur128 frames.
FRAMES = """frame:0    pts:0       pts_time:0
lavfi.r128.M=-120.691
lavfi.r128.S=-120.691
lavfi.r128.I=-70.000
lavfi.r128.LRA=0.000
lavfi.r128.true_peaks_ch0=0.125
lavfi.r128.true_peak=0.125
frame:1    pts:4800    pts_time:0.1
lavfi.r128.M=-15.900
lavfi.r128.S=-16.200
lavfi.r128.I=-16.100
lavfi.r128.true_peak=0.794328
""".splitlines()


class FakeClock:
    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        return self.readings.pop(0)


class LoudnessMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime_path = Path(self.temp.name) / "runtime.json"
        self.loudness_dir = Path(self.temp.name) / "loudness"
        self.metrics = Metrics(self.runtime_path)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("ALANA_PROCESSED_AUDIO", "ALANA_LOUDNESS_TARGET", "ALANA_TRUE_PEAK_CEILING"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.temp.cleanup()

    def samples(self):
        rendered = self.metrics.render({})
        for line in rendered.splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, SAMPLE)
        return rendered

    def read(self, output):
        return json.loads((self.loudness_dir / f"{output}.json").read_text(encoding="utf-8"))

    def test_monitor_keeps_latest_loudness_and_highest_true_peak_in_dbtp(self):
        writes = monitor_loudness(FRAMES, self.loudness_dir / "rtmp-1.json", interval=1.0, clock=FakeClock(0.0, *[0.5] * 7), wallclock=FakeClock(100.0, 101.0))
        self.assertEqual(writes, 2)
        self.assertEqual(self.read("rtmp-1"), {"values": {"momentary": -15.9, "short_term": -16.2, "integrated": -16.1, "true_peak": -2.0}, "updated": 101.0})
        self.assertFalse((self.loudness_dir / "rtmp-1.json.tmp").exists())

    def test_monitor_ignores_other_lines_and_non_finite_values(self):
        lines = ["Stream mapping:", "lavfi.r128.M=-inf", "lavfi.r128.true_peak=0.000", "lavfi.r128.LRA=4.0", "noise"]
        self.assertEqual(monitor_loudness(lines, self.loudness_dir / "livekit.json"), 0)
        self.assertFalse((self.loudness_dir / "livekit.json").exists())

    def test_monitor_writes_again_once_the_interval_elapses(self):
        writes = monitor_loudness(FRAMES[:3], self.loudness_dir / "recording.json", interval=1.0, clock=FakeClock(0.0, 1.0), wallclock=FakeClock(5.0, 6.0))
        self.assertEqual(writes, 2)
        self.assertEqual(self.read("recording"), {"values": {"momentary": -120.691, "short_term": -120.691}, "updated": 6.0})

    def test_render_exports_loudness_per_output_with_bounded_labels(self):
        for output, value in (("rtmp-1", -16.1), ("rtmp-12", -16.3), ("livekit", -15.8), ("recording", -16.0)):
            record_loudness(self.loudness_dir / f"{output}.json", {"momentary": -15.0, "short_term": -16.0, "integrated": value, "true_peak": -1.5}, 1700000000.5)
        record_loudness(self.loudness_dir / "program-123.json", {"integrated": -1.0}, 1.0)
        (self.loudness_dir / "notes.txt").write_text("x", encoding="utf-8")
        rendered = self.samples()
        self.assertIn("alana_audio_processing_enabled 1", rendered)
        self.assertIn("alana_audio_loudness_target_lufs -16.0", rendered)
        self.assertIn("alana_audio_true_peak_ceiling_dbtp -1.0", rendered)
        self.assertIn('alana_audio_loudness_lufs{output="rtmp-1",window="integrated"} -16.1', rendered)
        self.assertIn('alana_audio_loudness_lufs{output="livekit",window="momentary"} -15.0', rendered)
        self.assertIn('alana_audio_loudness_lufs{output="recording",window="short_term"} -16.0', rendered)
        self.assertIn('alana_audio_true_peak_dbtp{output="rtmp-12"} -1.5', rendered)
        self.assertIn('alana_audio_loudness_updated_timestamp_seconds{output="recording"} 1700000000.500000', rendered)
        self.assertEqual(set(re.findall(r'output="([^"]+)"', rendered)), {"rtmp-1", "rtmp-12", "livekit", "recording"})
        self.assertEqual(rendered.count("# TYPE alana_audio_loudness_lufs gauge"), 1)

    def test_render_reports_configured_values(self):
        os.environ["ALANA_LOUDNESS_TARGET"] = "-23"
        os.environ["ALANA_TRUE_PEAK_CEILING"] = "-2"
        rendered = self.samples()
        self.assertIn("alana_audio_loudness_target_lufs -23.0", rendered)
        self.assertIn("alana_audio_true_peak_ceiling_dbtp -2.0", rendered)

    def test_render_omits_corrupt_or_missing_measurements(self):
        os.environ["ALANA_LOUDNESS_TARGET"] = "loud"
        rendered = self.samples()
        self.assertNotIn("alana_audio_loudness_target_lufs", rendered)
        self.assertNotIn("alana_audio_loudness_lufs", rendered)
        self.loudness_dir.mkdir()
        for corrupt in ("{not json", json.dumps(["values"]), json.dumps({"values": {"momentary": True, "short_term": None, "integrated": "nan", "true_peak": [1]}, "updated": "soon"})):
            (self.loudness_dir / "rtmp-1.json").write_text(corrupt, encoding="utf-8")
            rendered = self.samples()
            self.assertNotIn("alana_audio_loudness_lufs", rendered)
            self.assertNotIn("alana_audio_true_peak_dbtp", rendered)
            self.assertNotIn("alana_audio_loudness_updated_timestamp_seconds", rendered)

    def test_unreadable_loudness_directory_renders_no_measurements(self):
        with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            rendered = self.samples()
        self.assertIn("alana_audio_processing_enabled 1", rendered)
        self.assertNotIn("alana_audio_loudness_lufs", rendered)

    def test_kill_switch_reports_processing_off_and_no_loudness(self):
        os.environ["ALANA_PROCESSED_AUDIO"] = "off"
        record_loudness(self.loudness_dir / "rtmp-1.json", {"integrated": -16.0}, 1.0)
        rendered = self.samples()
        self.assertIn("alana_audio_processing_enabled 0", rendered)
        self.assertNotIn("alana_audio_loudness", rendered)
        self.assertNotIn("alana_audio_true_peak", rendered)

    def test_loudness_monitor_command_reads_ffmpeg_stderr_from_stdin(self):
        path = self.loudness_dir / "rtmp-1.json"
        stdin = io.TextIOWrapper(io.BytesIO(("\n".join(FRAMES) + "\n\xff\n").encode("latin-1")))
        with mock.patch.object(sys, "argv", ["alana_metrics.py", "loudness-monitor", str(path)]), mock.patch.object(sys, "stdin", stdin):
            runpy.run_path(alana_metrics.__file__, run_name="__main__")
        self.assertEqual(self.read("rtmp-1")["values"]["integrated"], -16.1)

    def test_loudness_monitor_command_rejects_other_arguments(self):
        for argv in ([], ["loudness-monitor"], ["other", "path"]):
            with self.assertRaises(SystemExit):
                alana_metrics.main(argv)


if __name__ == "__main__":
    unittest.main()
