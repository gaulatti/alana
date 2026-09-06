import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from recording import (
    RecordingController,
    RecordingStore,
    capture_command,
    cleanup_retention,
    disk_snapshot,
    harvest_segments,
    manifest_records,
    next_sequence,
    run_supervisor,
    synthetic_smoke,
)


class RecordingContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_capture_uses_shared_composed_inputs_and_crash_tolerant_segments(self):
        command = capture_command(self.root, 7, 5)
        rendered = " ".join(command)
        self.assertIn("-f x11grab", rendered)
        self.assertIn("-i :98", rendered)
        self.assertIn("-f pulse", rendered)
        self.assertIn("-i stream_out.monitor", rendered)
        self.assertIn("-f segment", rendered)
        self.assertIn("-segment_format matroska", rendered)
        self.assertIn("-segment_start_number 7", rendered)
        self.assertNotIn("rtmp://", rendered)
        self.assertNotIn("livekit", rendered.lower())

    def test_capture_calculates_gop_for_fractional_frame_rate(self):
        with patch.dict(os.environ, {"FPS": "30000/1001"}, clear=False):
            command = capture_command(self.root, 0, 5)

        self.assertEqual(command[command.index("-g") + 1], "150")

    def test_recording_quota_counts_all_retained_state(self):
        store = RecordingStore(self.root)
        (store.operations_dir / "retained").write_bytes(b"operation")
        (store.command_dir / "retained.json").write_bytes(b"command")
        store.state_file.write_bytes(b"state")

        snapshot = disk_snapshot(self.root, quota_bytes=10, min_free_bytes=1)

        self.assertEqual(snapshot["usageBytes"], 21)
        self.assertFalse(snapshot["healthy"])

    def test_capture_progress_does_not_overwrite_stop_transition(self):
        store = RecordingStore(self.root)
        operation_id = "a" * 16
        store.operation_dir(operation_id).mkdir()
        active = {
            "enabled": True,
            "state": "active",
            "operationId": operation_id,
            "segmentCount": 0,
            "bytes": 0,
            "durationSeconds": 0.0,
        }
        store.save(active)
        stale_capture_state = dict(active)
        finalizing = {**active, "state": "finalizing", "finalizationState": "pending"}
        store.save(finalizing)

        harvest_segments(store, stale_capture_state, include_latest=True)

        self.assertEqual(stale_capture_state["state"], "finalizing")
        self.assertEqual(store.load(enabled=True)["state"], "finalizing")

    def test_post_capture_manifest_corruption_persists_failed_state(self):
        store = RecordingStore(self.root)
        operation_id = "b" * 16
        store.operation_dir(operation_id).mkdir()
        store.save(
            {
                "enabled": True,
                "state": "requested",
                "operationId": operation_id,
                "segmentCount": 0,
                "bytes": 0,
                "durationSeconds": 0.0,
                "droppedFrames": 0,
                "errors": 0,
                "restarts": 0,
                "finalizationState": "not-requested",
            }
        )
        process = Mock()
        process.pid = 123
        process.poll.return_value = 1
        environment = {
            "PULSE_SERVER": "test",
            "RECORDING_QUOTA_BYTES": str(2**63 - 1),
            "RECORDING_MIN_FREE_BYTES": "1",
        }

        with (
            patch.dict(os.environ, environment, clear=False),
            patch("recording.harvest_segments", side_effect=[[], ValueError("corrupt")]),
            patch("recording.subprocess.Popen", return_value=process),
        ):
            result = run_supervisor(self.root, operation_id)

        state = store.load(enabled=True)
        self.assertEqual(result, 1)
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["error"], "manifest-corrupt")
        self.assertEqual(state["finalizationState"], "failed")

    def test_public_state_omits_processes_and_paths(self):
        state = {
            "enabled": True,
            "state": "failed",
            "operationId": "a" * 16,
            "workerPid": 123,
            "supervisorPid": 456,
            "path": "/private/recording",
            "error": "secret-bearing-free-form-error",
            "disk": {
                "healthy": False,
                "freeBytes": 1,
                "usageBytes": 2,
                "quotaBytes": 3,
                "minFreeBytes": 4,
                "path": "/private/recording",
            },
        }
        public = RecordingStore.public(state)
        serialized = json.dumps(public)
        self.assertNotIn("Pid", serialized)
        self.assertNotIn("/private/recording", serialized)
        self.assertNotIn("secret-bearing", serialized)
        self.assertNotIn("error", public)
        self.assertEqual(public["disk"]["freeBytes"], 1)

    def test_corrupt_open_segment_is_not_overwritten_after_restart(self):
        (self.root / "segment-000004.mkv.corrupt").write_bytes(b"partial")
        (self.root / "segment-000003.mkv").write_bytes(b"closed")
        self.assertEqual(next_sequence(self.root), 5)

    def test_corrupt_state_fails_closed_instead_of_resetting(self):
        store = RecordingStore(self.root)
        store.state_file.write_text("{not-json", encoding="utf-8")
        controller = RecordingController(
            self.root,
            enabled=True,
            ready=lambda: True,
            start_monitor=False,
        )
        try:
            self.assertEqual(controller.status()["error"], "manifest-corrupt")
            status, state = controller.start("blocked-by-corruption")
            self.assertEqual(status, 500)
            self.assertEqual(state["state"], "failed")
            self.assertEqual(state["error"], "manifest-corrupt")
            self.assertIsNone(controller.process)
        finally:
            controller.close()

    def test_retention_removes_only_expired_inactive_operations(self):
        store = RecordingStore(self.root)
        old = store.operations_dir / ("a" * 16)
        current = store.operations_dir / ("b" * 16)
        recent = store.operations_dir / ("c" * 16)
        for directory in (old, current, recent):
            directory.mkdir()
            (directory / "data").write_bytes(b"fixture")
        expired = time.time() - 7200
        os.utime(old, (expired, expired))
        os.utime(current, (expired, expired))

        removed = cleanup_retention(store, 1, current.name)

        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())
        self.assertTrue(recent.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools are required")
    def test_synthetic_program_creates_verified_manifest_and_playable_mp4(self):
        with patch.dict(
            os.environ,
            {
                "RECORDING_VIDEO_BITRATE": "500k",
                "RECORDING_AUDIO_BITRATE": "64k",
                "RECORDING_FINALIZE_TIMEOUT": "30",
            },
            clear=False,
        ):
            state = synthetic_smoke(self.root, 3, 1)

        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["finalizationState"], "verified")
        self.assertGreaterEqual(state["segmentCount"], 2)
        self.assertEqual(state["videoCodec"], "h264")
        self.assertEqual(state["audioCodec"], "aac")
        self.assertEqual(len(state["finalSha256"]), 64)
        operation = self.root / "operations" / state["operationId"]
        records = manifest_records(operation / "manifest.jsonl")
        self.assertEqual([item["sequence"] for item in records], list(range(len(records))))
        for record in records:
            self.assertEqual(len(record["sha256"]), 64)
            self.assertGreater(record["bytes"], 0)
            self.assertGreater(record["durationSeconds"], 0)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,duration",
                "-of",
                "json",
                str(operation / "program.mp4"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        streams = json.loads(probe.stdout)["streams"]
        self.assertEqual({item["codec_type"] for item in streams}, {"video", "audio"})
        durations = [float(item["duration"]) for item in streams]
        self.assertLess(abs(durations[0] - durations[1]), 0.15)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools are required")
    def test_forced_capture_exit_restarts_without_stopping_publication(self):
        environment = {
            "RECORDING_SEGMENT_SECONDS": "1",
            "RECORDING_QUOTA_BYTES": str(1024 * 1024 * 1024),
            "RECORDING_MIN_FREE_BYTES": "1",
            "RECORDING_RETENTION_HOURS": "1",
            "RECORDING_MAX_RESTARTS": "3",
            "RECORDING_RESTART_BACKOFF_SECONDS": "1",
            "RECORDING_START_TIMEOUT": "10",
            "RECORDING_FINALIZE_TIMEOUT": "30",
            "RECORDING_VIDEO_BITRATE": "500k",
            "RECORDING_AUDIO_BITRATE": "64k",
        }
        publication = subprocess.Popen(["sleep", "60"])
        controller = None
        try:
            with patch.dict(os.environ, environment, clear=False):
                controller = RecordingController(
                    self.root, enabled=True, ready=lambda: True, synthetic=True
                )
                status, started = controller.start("recording-start")
                self.assertEqual(status, 202)
                self.assertIn(started["state"], {"requested", "active"})
                deadline = time.time() + 10
                first_pid = None
                while time.time() < deadline:
                    internal = controller.store.load(enabled=True)
                    if internal.get("workerPid") and int(internal.get("segmentCount", 0)) >= 1:
                        first_pid = int(internal["workerPid"])
                        first_segment_count = int(internal["segmentCount"])
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(first_pid)
                os.kill(first_pid, signal.SIGKILL)

                deadline = time.time() + 15
                recovered = None
                while time.time() < deadline:
                    recovered = controller.store.load(enabled=True)
                    if (
                        int(recovered.get("restarts", 0)) >= 1
                        and recovered.get("workerPid")
                        and int(recovered["workerPid"]) != first_pid
                        and int(recovered.get("segmentCount", 0)) > first_segment_count
                    ):
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(recovered)
                self.assertGreaterEqual(int(recovered.get("restarts", 0)), 1)
                self.assertNotEqual(int(recovered["workerPid"]), first_pid)
                self.assertIsNone(publication.poll())

                status, stopped = controller.stop("recording-stop")
                self.assertEqual(status, 200)
                self.assertEqual(stopped["state"], "complete")
                self.assertEqual(stopped["finalizationState"], "verified")
                self.assertGreaterEqual(stopped["segmentCount"], 1)
                operation = self.root / "operations" / stopped["operationId"]
                self.assertLessEqual(len(list(operation.glob("*.corrupt"))), 1)
                self.assertIsNone(publication.poll())
                duplicate_status, duplicate = controller.stop("recording-stop")
                self.assertEqual(duplicate_status, 200)
                self.assertTrue(duplicate["commandResult"]["duplicate"])
        finally:
            if controller is not None:
                controller.close()
            publication.terminate()
            publication.wait(timeout=5)

    def test_disk_preflight_fails_recording_without_touching_publication(self):
        publication = subprocess.Popen(["sleep", "60"])
        controller = None
        try:
            with patch.dict(
                os.environ,
                {
                    "RECORDING_QUOTA_BYTES": str(2**63 - 1),
                    "RECORDING_MIN_FREE_BYTES": str(2**63 - 2),
                    "RECORDING_RETENTION_HOURS": "1",
                },
                clear=False,
            ):
                controller = RecordingController(
                    self.root,
                    enabled=True,
                    ready=lambda: True,
                    start_monitor=False,
                )
                status, state = controller.start("no-space")
            self.assertEqual(status, 507)
            self.assertEqual(state["state"], "failed")
            self.assertEqual(state["error"], "disk-exhausted")
            self.assertIsNone(publication.poll())
        finally:
            if controller is not None:
                controller.close()
            publication.terminate()
            publication.wait(timeout=5)

    def test_disabled_default_has_no_recorder_process_or_side_effect(self):
        controller = RecordingController(
            self.root,
            enabled=False,
            ready=lambda: True,
            start_monitor=False,
        )
        try:
            self.assertEqual(controller.status()["state"], "disabled")
            status, state = controller.start("disabled-start")
            self.assertEqual(status, 409)
            self.assertEqual(state["state"], "disabled")
            self.assertIsNone(controller.process)
            self.assertEqual(list(controller.store.operations_dir.iterdir()), [])
        finally:
            controller.close()


if __name__ == "__main__":
    unittest.main()
