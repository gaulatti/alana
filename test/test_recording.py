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

    def test_bounded_capture_bounds_every_input_as_well_as_the_output(self):
        # The loudness meter branch ends in a sink no output -t reaches; on
        # ffmpeg 5.1 only input limits let a bounded capture end.
        for synthetic in (False, True):
            with self.subTest(synthetic=synthetic):
                command = capture_command(self.root, 0, 5, synthetic=synthetic, duration=9)
                inputs = [index for index, value in enumerate(command) if value == "-i"]
                self.assertEqual(len(inputs), 2)
                previous = 0
                for index in inputs:
                    options = command[previous:index]
                    self.assertIn("-t", options)
                    self.assertEqual(options[options.index("-t") + 1], "9")
                    previous = index + 2
                output = command[inputs[-1] + 2:]
                self.assertEqual(output[output.index("-t") + 1], "9")

    def test_unbounded_capture_sets_no_duration(self):
        for synthetic in (False, True):
            with self.subTest(synthetic=synthetic):
                self.assertNotIn("-t", capture_command(self.root, 0, 5, synthetic=synthetic))

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
        self.assertEqual(store.load()["state"], "finalizing")

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

        state = store.load()
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

    def test_state_persisted_as_disabled_by_the_previous_release_loads_as_idle(self):
        store = RecordingStore(self.root)
        store.state_file.write_text(
            json.dumps({"enabled": False, "state": "disabled", "operationId": None}),
            encoding="utf-8",
        )
        controller = RecordingController(
            self.root,
            ready=lambda: True,
            start_monitor=False,
        )
        try:
            self.assertEqual(controller.status()["state"], "idle")
            self.assertEqual(
                json.loads(store.state_file.read_text(encoding="utf-8"))["state"],
                "idle",
            )
            status, state = controller.start("after-disabled")
            self.assertNotEqual(status, 500)
            self.assertNotEqual(state.get("error"), "manifest-corrupt")
        finally:
            controller.close()

    def test_corrupt_state_fails_closed_instead_of_resetting(self):
        store = RecordingStore(self.root)
        store.state_file.write_text("{not-json", encoding="utf-8")
        controller = RecordingController(
            self.root,
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
                    self.root, ready=lambda: True, synthetic=True
                )
                status, started = controller.start("recording-start")
                self.assertEqual(status, 202)
                self.assertIn(started["state"], {"requested", "active"})
                deadline = time.time() + 10
                first_pid = None
                while time.time() < deadline:
                    internal = controller.store.load()
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
                    recovered = controller.store.load()
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

    def test_final_artifact_requires_a_verified_complete_recording(self):
        controller = RecordingController(
            self.root, ready=lambda: True, start_monitor=False
        )
        state = controller.store.load()
        self.assertIsNone(controller.final_artifact())

        operation_id = "a" * 16
        operation_dir = controller.store.operation_dir(operation_id)
        operation_dir.mkdir(parents=True)
        complete = {**state, "state": "complete", "operationId": operation_id,
                    "finalizationState": "pending"}
        controller.store.save(complete)
        self.assertIsNone(controller.final_artifact())

        controller.store.save({**complete, "finalizationState": "verified",
                               "operationId": "not-a-valid-operation"})
        self.assertIsNone(controller.final_artifact())

        controller.store.save({**complete, "finalizationState": "verified"})
        self.assertIsNone(controller.final_artifact())  # no program.mp4 yet

        (operation_dir / "program.mp4").write_bytes(b"mp4")
        path, public = controller.final_artifact()
        self.assertEqual(path, operation_dir / "program.mp4")
        self.assertEqual(public["state"], "complete")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools are required")
class RecordingDriftTests(unittest.TestCase):
    """The finalized program keeps audio and video in sync for its whole length (#32)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def capture_flash_and_beep(self, seconds: int) -> tuple["RecordingStore", dict]:
        """Run the recorder's own encode and segment arguments on an in-sync flash and beep."""
        from recording import RecordingStore, capture_command, harvest_segments, now

        store = RecordingStore(self.root)
        state = {
            "enabled": True, "state": "active", "operationId": "0123456789abcdef", "requestedAt": now(), "startedAt": now(),
            "segmentCount": 0, "bytes": 0, "durationSeconds": 0.0, "droppedFrames": 0, "errors": 0, "restarts": 0,
            "finalizationState": "not-requested",
        }
        operation = store.operation_dir("0123456789abcdef")
        operation.mkdir(parents=True, exist_ok=True)
        store.save(state)
        with patch.dict(os.environ, {"RECORDING_TEST_RESOLUTION": "160x90", "RECORDING_X264_PRESET": "ultrafast"}, clear=False):
            command = capture_command(operation, 0, 5, synthetic=True, duration=seconds)
        beep = "if(between(mod(t\\,10)\\,5\\,5.3)\\,0.1*sin(2*PI*1000*t)\\,0)"
        inputs = [index for index, value in enumerate(command) if value == "-i"]
        command[inputs[0] + 1] = "color=c=black:s=160x90:r=30,drawbox=c=white:t=fill:enable='between(mod(t,10),5,5.3)'"
        command[inputs[1] + 1] = f"aevalsrc={beep}|{beep}:s=48000"
        command = [value for value in command if value != "-re"]  # the inputs are synthetic; no need for real time
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=seconds + 60)
        harvest_segments(store, state, include_latest=True)
        return store, state

    @staticmethod
    def offsets(path: Path) -> list[float]:
        """Audio minus video onset per flash/beep event, in the program's own timeline (ms)."""
        stats = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path), "-map", "0:v:0", "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        flashes, moment, last = [], 0.0, -10.0
        for line in stats.splitlines():
            if line.startswith("frame:"):
                moment = float(line.split("pts_time:")[1])
            elif "YAVG" in line and float(line.split("=")[1]) > 128:
                if moment - last > 1:
                    flashes.append(moment)
                last = moment
        start = float(json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=start_time", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout)["streams"][0]["start_time"])
        pcm = path.with_suffix(".wav")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-ac", "1", "-c:a", "pcm_s16le", str(pcm)], check=True)
        import array
        import wave

        with wave.open(str(pcm)) as handle:
            rate, samples = handle.getframerate(), array.array("h", handle.readframes(handle.getnframes()))
        beeps, last = [], -10.0
        for index, sample in enumerate(samples):
            if abs(sample) > 1500:
                moment = start + index / rate
                if moment - last > 1:
                    beeps.append(moment)
                last = moment
        return [(beep - min(flashes, key=lambda flash: abs(flash - beep))) * 1000 for beep in beeps]

    def test_finalized_program_does_not_drift_over_a_minute(self):
        from recording import finalize_recording

        store, state = self.capture_flash_and_beep(62)
        self.assertTrue(finalize_recording(store, state))
        offsets = self.offsets(store.operation_dir("0123456789abcdef") / "program.mp4")
        self.assertEqual(len(offsets), 6, offsets)
        self.assertLessEqual(max(offsets) - min(offsets), 10, f"drift across the program: {offsets}")
        self.assertLessEqual(max(abs(offset) for offset in offsets), 40, offsets)

    def test_unmeasurable_segments_still_finalize_by_the_previous_join(self):
        from recording import finalize_recording

        real_run = subprocess.run
        aac = [{"codec_name": "aac", "sample_rate": "48000"}]
        for name, answer in (("no audio packets", {"streams": aac, "packets": []}), ("unreadable timing", {"streams": aac, "packets": [{"pts_time": "x"}]}), ("probe failure", None)):
            with self.subTest(case=name):
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.root = Path(self.temp.name)
                store, state = self.capture_flash_and_beep(11)

                def span_probe_breaks(command, *args, _answer=answer, **kwargs):
                    if "format=start_time:stream=codec_name,sample_rate:packet=pts_time" in command:
                        if _answer is None:
                            raise subprocess.CalledProcessError(1, command)
                        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_answer), stderr="")
                    return real_run(command, *args, **kwargs)

                with patch("recording.subprocess.run", side_effect=span_probe_breaks), patch("recording.log_event") as events:
                    self.assertTrue(finalize_recording(store, state))
                operation = store.operation_dir("0123456789abcdef")
                self.assertTrue((operation / "program.mp4").stat().st_size > 0)
                saved = store.load()
                self.assertEqual((saved["state"], saved["finalizationState"], saved["finalizationMethod"]), ("complete", "verified", "fallback"))
                self.assertEqual(json.loads((operation / "manifest.json").read_text(encoding="utf-8"))["finalizationMethod"], "fallback")
                self.assertGreaterEqual(len(list(operation.glob("segment-*.mkv"))), 3, "every segment is kept")
                self.assertNotIn("duration", (operation / "concat.txt").read_text(encoding="utf-8"))
                events.assert_any_call("finalize", "finalizing", "span-unavailable", saved["segmentCount"])

    def test_a_changed_segment_still_fails_finalization(self):
        from recording import finalize_recording

        store, state = self.capture_flash_and_beep(6)
        segment = sorted(store.operation_dir("0123456789abcdef").glob("segment-*.mkv"))[0]
        with segment.open("ab") as handle:
            handle.write(b"\x00")
        self.assertFalse(finalize_recording(store, state))
        self.assertEqual(store.load()["finalizationState"], "failed")

    def test_measurable_segments_finalize_by_the_exact_join(self):
        from recording import finalize_recording

        store, state = self.capture_flash_and_beep(11)
        self.assertTrue(finalize_recording(store, state))
        operation = store.operation_dir("0123456789abcdef")
        self.assertEqual(store.load()["finalizationMethod"], "exact")
        # Every segment but the last is placed by a duration; the last needs none.
        self.assertEqual((operation / "concat.txt").read_text(encoding="utf-8").count("duration "), store.load()["segmentCount"] - 1)


class SegmentAudioTimingTests(unittest.TestCase):
    AAC = [{"codec_name": "aac", "sample_rate": "48000"}]
    START = {"start_time": "0.000000"}

    def probe(self, payload):
        from recording import segment_audio_timing

        answer = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch("recording.subprocess.run", return_value=answer):
            return segment_audio_timing(Path("segment-000000.mkv"))

    def test_timing_is_the_first_packet_from_the_file_start_and_the_sample_count(self):
        packets = [{"pts_time": "0.042000"}, {"pts_time": "0.021000"}, {"pts_time": "0.064000"}]
        first, length = self.probe({"format": {"start_time": "0.000000"}, "streams": self.AAC, "packets": packets})
        self.assertAlmostEqual(first, 0.021, places=6)
        # A first segment whose file starts at its priming delay: the concat demuxer removes it.
        first, _ = self.probe({"format": {"start_time": "0.021000"}, "streams": self.AAC, "packets": packets})
        self.assertAlmostEqual(first, 0.0, places=6)
        self.assertAlmostEqual(length, 3 * 1024 / 48000, places=9)

    def test_a_segment_without_usable_audio_timing_is_refused(self):
        for payload in (
            {"streams": self.AAC, "packets": []}, {"packets": [{"pts_time": "0"}]}, {}, ["packets"],
            {"streams": self.AAC, "packets": [{"pts_time": "0"}]}, {"format": {}, "streams": self.AAC, "packets": [{"pts_time": "0"}]},
            {"format": {"start_time": "x"}, "streams": self.AAC, "packets": [{"pts_time": "0"}]},
            {"format": self.START, "streams": [{"codec_name": "opus", "sample_rate": "48000"}], "packets": [{"pts_time": "0"}]},
            {"format": self.START, "streams": [{"codec_name": "aac", "sample_rate": "0"}], "packets": [{"pts_time": "0"}]},
            {"format": self.START, "streams": [{"codec_name": "aac"}], "packets": [{"pts_time": "0"}]},
            {"format": self.START, "streams": self.AAC, "packets": [{"pts_time": "nan"}]},
            {"format": self.START, "streams": self.AAC, "packets": [{"pts_time": "x"}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.probe(payload)


class AudioAnchoredDurationTests(unittest.TestCase):
    def test_each_segment_starts_where_the_previous_audio_ends(self):
        from recording import audio_anchored_durations

        # Audio of 5 s starting at 0.021 in the first segment and at 0.000 in the next.
        self.assertEqual([round(value, 6) for value in audio_anchored_durations([(0.021, 5.0), (0.0, 5.0), (0.013, 4.5)])], [5.021, 4.987])
        self.assertEqual(audio_anchored_durations([(0.0, 5.0)]), [])

    def test_positions_telescope_so_rounding_never_accumulates(self):
        from recording import audio_anchored_durations

        timings = [(0.001 * (index % 3), 234 * 1024 / 48000) for index in range(120)]
        durations = audio_anchored_durations(timings)
        # Segment k starts at the sum of the earlier durations; its audio then starts at
        # k audio lengths exactly, whatever each segment's own first timestamp was.
        start = sum(durations[:119]) + timings[119][0]
        self.assertAlmostEqual(start - timings[0][0], 119 * 234 * 1024 / 48000, places=9)

    def test_audio_that_does_not_continue_is_refused(self):
        from recording import audio_anchored_durations

        for timings in ([(0.0, 1.0), (1.0, 1.0)], [(0.0, 1.0), (2.0, 1.0)], [(0.0, float("nan")), (0.0, 1.0)]):
            with self.subTest(timings=timings), self.assertRaises(ValueError):
                audio_anchored_durations(timings)


class FinalizeJoinTests(unittest.TestCase):
    """finalize_recording's join, with ffmpeg and ffprobe faked at the subprocess boundary (#32)."""

    OPERATION = "0123456789abcdef"

    def setUp(self):
        import hashlib

        self.temp = tempfile.TemporaryDirectory()
        self.store = RecordingStore(Path(self.temp.name))
        self.operation = self.store.operation_dir(self.OPERATION)
        self.operation.mkdir(parents=True)
        self.records = []
        for sequence in range(3):
            segment = self.operation / f"segment-{sequence:06d}.mkv"
            segment.write_bytes(b"fictional segment %d" % sequence)
            self.records.append({"sequence": sequence, "file": segment.name, "sha256": hashlib.sha256(segment.read_bytes()).hexdigest()})
        self.state = {
            "enabled": True, "state": "active", "operationId": self.OPERATION, "segmentCount": 3,
            "errors": 0, "restarts": 0, "finalizationState": "not-requested",
        }
        self.store.save(self.state)
        self.remuxes = []

    def tearDown(self):
        self.temp.cleanup()

    def run_finalize(self, span_answer):
        """span_answer(segment_name) returns the ffprobe packet JSON, or raises."""
        from recording import finalize_recording

        def fake_run(command, *args, **kwargs):
            if command[0] == "ffprobe" and "format=start_time:stream=codec_name,sample_rate:packet=pts_time" in command:
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(span_answer(Path(command[-1]).name)), stderr="")
            if command[0] == "ffmpeg":
                self.remuxes.append((self.operation / "concat.txt").read_text(encoding="utf-8"))
                (Path(kwargs["cwd"]) / command[-1]).write_bytes(b"fictional mp4")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected command {command[0]}")

        metadata = {
            "bytes": 13, "durationSeconds": 15.0, "videoCodec": "h264", "audioCodec": "aac", "width": 160,
            "height": 90, "fps": "30/1", "audioRate": 48000, "audioChannels": 2,
        }
        with patch("recording.harvest_segments", return_value=self.records), patch("recording.subprocess.run", side_effect=fake_run), \
                patch("recording.probe_media", return_value=metadata), patch("recording.log_event") as events:
            result = finalize_recording(self.store, self.state)
        return result, events

    @staticmethod
    def packets(first, count=234, step=1024 / 48000):
        return {"format": {"start_time": "0.000000"}, "streams": [{"codec_name": "aac", "sample_rate": "48000"}],
                "packets": [{"pts_time": f"{first + index * step:.6f}"} for index in range(count)]}

    def saved(self):
        return self.store.load()

    def test_readable_segments_join_exactly_by_their_audio(self):
        result, events = self.run_finalize(lambda name: self.packets(0.021))
        self.assertTrue(result)
        self.assertEqual(self.saved()["finalizationMethod"], "exact")
        self.assertEqual(self.saved()["finalizationState"], "verified")
        concat = self.remuxes[0]
        # 234 AAC packets of 1024 samples at 48 kHz: 4.992 s each, the last segment unplaced.
        self.assertEqual(concat.count("duration 4.992000\n"), 2)
        self.assertEqual(concat.count("duration "), 2)
        self.assertEqual([line for line in concat.splitlines() if line.startswith("file ")], [f"file '{r['file']}'" for r in self.records])
        self.assertEqual(json.loads((self.operation / "manifest.json").read_text(encoding="utf-8"))["finalizationMethod"], "exact")
        self.assertNotIn(unittest.mock.call("finalize", "finalizing", "span-unavailable", 3), events.mock_calls)

    def test_an_unmeasurable_segment_falls_back_to_the_previous_join_and_keeps_the_recording(self):
        failures = {
            "no audio packets": lambda name: {**self.packets(0.0), "packets": []} if name.endswith("1.mkv") else self.packets(0.0),
            "unreadable timing": lambda name: {**self.packets(0.0), "packets": [{"pts_time": "x"}]} if name.endswith("2.mkv") else self.packets(0.0),
            "audio that does not continue": lambda name: self.packets(9.0) if name.endswith("1.mkv") else self.packets(0.0),
            "probe failure": lambda name: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["ffprobe"])),
            "not json": lambda name: (_ for _ in ()).throw(json.JSONDecodeError("bad", "", 0)),
        }
        for case, answer in failures.items():
            with self.subTest(case=case):
                self.remuxes.clear()
                self.state.update(state="active", finalizationState="not-requested")
                self.state.pop("finalizationMethod", None)
                result, events = self.run_finalize(answer)
                self.assertTrue(result)
                saved = self.saved()
                self.assertEqual((saved["state"], saved["finalizationState"], saved["finalizationMethod"]), ("complete", "verified", "fallback"))
                self.assertEqual(json.loads((self.operation / "manifest.json").read_text(encoding="utf-8"))["finalizationMethod"], "fallback")
                self.assertEqual(self.remuxes, ["".join(f"file '{r['file']}'\n" for r in self.records)], "the join before #32, every segment kept")
                events.assert_any_call("finalize", "finalizing", "span-unavailable", 3)
                self.assertTrue((self.operation / "program.mp4").exists())

    def test_a_changed_segment_still_fails_before_any_join(self):
        (self.operation / "segment-000001.mkv").write_bytes(b"changed")
        result, _ = self.run_finalize(lambda name: self.packets(0.0))
        self.assertFalse(result)
        self.assertEqual(self.saved()["finalizationState"], "failed")
        self.assertEqual(self.remuxes, [])


class RecordingAudioChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"ALANA_RUNTIME_METRICS_FILE": str(self.root / "runtime-metrics.json")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("ALANA_PROCESSED_AUDIO", "ALANA_LOUDNESS_TARGET", "ALANA_TRUE_PEAK_CEILING"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.temp.cleanup()

    def test_recorder_applies_the_same_chain_as_the_broadcast_encoders(self):
        import audio_chain

        # The switch defaults on: unset in this environment.
        self.assertNotIn("ALANA_PROCESSED_AUDIO", os.environ)
        command = capture_command(self.root, 0, 5)
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(graph, audio_chain.filter_graph("1:a:0", "aout", -16.0, -1.0))
        self.assertEqual(command[command.index("-filter_complex") + 2 : command.index("-filter_complex") + 4], ["-map", "[aout]"])
        self.assertNotIn("1:a:0", command[command.index("-filter_complex") + 2 :])
        self.assertEqual(command[command.index("-ar") + 1], "48000")

    def test_kill_switch_restores_todays_recorder_command(self):
        os.environ["ALANA_PROCESSED_AUDIO"] = "off"
        command = capture_command(self.root, 0, 5)
        self.assertNotIn("-filter_complex", command)
        self.assertEqual(command[command.index("-map") : command.index("-map") + 4], ["-map", "0:v:0", "-map", "1:a:0"])

    def test_metered_capture_feeds_the_recording_loudness_metric(self):
        frames = b"frame:0 pts:0\nlavfi.r128.I=-16.200\nlavfi.r128.true_peak=0.5\n"
        process = subprocess.Popen(["sh", "-c", 'printf "$0" >&2', frames.decode()], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with patch("recording.subprocess.Popen", return_value=process) as popen, patch("recording.capture_environment", return_value={}):
            from recording import start_capture

            self.assertIs(start_capture(["ffmpeg"]), process)
        process.wait()
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)
        path = self.root / "loudness" / "recording.json"
        expected = {"integrated": -16.2, "true_peak": -6.02}
        values = None
        deadline = time.monotonic() + 5
        # The meter thread writes the first frame at once and the rest at end of stream.
        while values != expected and time.monotonic() < deadline:
            try:
                values = json.loads(path.read_text(encoding="utf-8"))["values"]
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        self.assertEqual(values, expected)

    def test_unmetered_capture_discards_stderr_with_the_kill_switch(self):
        os.environ["ALANA_PROCESSED_AUDIO"] = "off"
        with patch("recording.subprocess.Popen") as popen, patch("recording.capture_environment", return_value={}):
            from recording import start_capture

            start_capture(["ffmpeg"])
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse((self.root / "loudness").exists())

    def test_meter_keeps_draining_when_the_metric_cannot_be_written(self):
        from recording import meter

        lines = iter(["lavfi.r128.I=-16.0\n", "lavfi.r128.I=-15.0\n", "lavfi.r128.I=-14.0\n"])
        with patch("recording.monitor_loudness", side_effect=lambda stream, path: (next(stream), (_ for _ in ()).throw(OSError("read-only")))):
            meter(lines, self.root / "loudness" / "recording.json")
        self.assertEqual(list(lines), [])
