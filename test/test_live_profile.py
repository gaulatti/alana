"""The effective live profile and the filler readiness bound to it (gaulatti/alana#29).

Contract pinned on gaulatti/alcantara#126. None of these tests need ffmpeg: the
startup agreement runs startup.sh against the same logging stubs as the audio
tests, and the rest use fakes or a real local HTTP server.
"""

import hashlib
import json
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_control_server as base
import test_startup_audio as startup


control = base.control
REPO = Path(__file__).resolve().parents[1]
STARTUP = REPO / "startup.sh"
ON = {"ALANA_LIVE_PROFILE": "on"}
# The settings that make the live profile equal LifecycleTests.filler_payload()'s.
MATCHING = {**ON, "AUDIO_BITRATE": "160k"}
DEFAULT_PROFILE = {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "videoBitrate": "6000k",
    "audioRate": 48000,
    "audioChannels": 2,
    "audioBitrate": "128k",
    "gop": 60,
    "loopSeconds": 10,
}


def binding_count(operation, result):
    key = (
        "alana_filler_profile_bindings_total",
        (("operation", operation), ("result", result)),
    )
    return control.METRICS.counters.get(key, 0)


class EffectiveProfileTests(unittest.TestCase):
    def test_defaults_are_startup_defaults_with_the_processed_48_khz_chain(self):
        self.assertEqual(control.effective_live_profile({}), DEFAULT_PROFILE)
        # An empty value is the default, exactly like ${NAME:-default} in startup.sh.
        empty = {name: "" for name in control.STARTUP_DEFAULTS} | {"ALANA_PROCESSED_AUDIO": "", "GPU_TYPE": ""}
        self.assertEqual(control.effective_live_profile(empty), DEFAULT_PROFILE)

    def test_audio_kill_switch_keeps_the_raw_44_1_khz_rate(self):
        profile = control.effective_live_profile({"ALANA_PROCESSED_AUDIO": "off"})
        self.assertEqual(profile, {**DEFAULT_PROFILE, "audioRate": 44100})

    def test_every_configured_value_reaches_the_profile(self):
        profile = control.effective_live_profile(
            {
                "RESOLUTION": "1280x720",
                "FPS": "25",
                "VIDEO_BITRATE": "3000k",
                "GOP_SIZE": "50",
                "AUDIO_BITRATE": "96k",
                "VIDEO_ENCODER": "libx264",
            }
        )
        self.assertEqual(
            profile,
            {
                **DEFAULT_PROFILE,
                "width": 1280,
                "height": 720,
                "fps": 25,
                "videoBitrate": "3000k",
                "gop": 50,
                "audioBitrate": "96k",
            },
        )

    def test_unqualified_or_unknown_encoding_modes_publish_nothing(self):
        for settings in (
            {"GPU_TYPE": "NVIDIA"},
            {"GPU_TYPE": "INTEL"},
            {"VIDEO_ENCODER": "h264_nvenc"},
            {"VIDEO_ENCODER": "h264_vaapi"},
            {"VIDEO_ENCODER": "libx265"},
            {"FPS": "29.97"},
            {"FPS": "30000/1001"},
            {"FPS": "0"},
            {"RESOLUTION": "1920*1080"},
            {"RESOLUTION": "0x1080"},
            {"VIDEO_BITRATE": "6M"},
            {"VIDEO_BITRATE": "6000000"},
            {"AUDIO_BITRATE": "128000"},
            {"GOP_SIZE": "0"},
            {"GOP_SIZE": "sixty"},
            {"ALANA_PROCESSED_AUDIO": "maybe"},
        ):
            with self.subTest(settings=settings):
                self.assertIsNone(control.effective_live_profile(settings))

    def test_gpu_type_wins_over_a_software_encoder_setting_like_startup(self):
        self.assertIsNone(
            control.effective_live_profile({"GPU_TYPE": "NVIDIA", "VIDEO_ENCODER": "libx264"})
        )
        # Any other GPU_TYPE falls through to VIDEO_ENCODER, as in startup.sh's case.
        self.assertEqual(control.effective_live_profile({"GPU_TYPE": "NONE"}), DEFAULT_PROFILE)

    def test_profile_has_exactly_the_nine_acknowledged_keys(self):
        self.assertEqual(set(DEFAULT_PROFILE), control.LIVE_PROFILE_KEYS)
        self.assertEqual(len(control.LIVE_PROFILE_KEYS), 9)

    def test_checksum_is_sha256_of_the_canonical_json(self):
        canonical = (
            '{"audioBitrate":"128k","audioChannels":2,"audioRate":48000,"fps":30,"gop":60,'
            '"height":1080,"loopSeconds":10,"videoBitrate":"6000k","width":1920}'
        )
        self.assertEqual(
            control.profile_checksum(DEFAULT_PROFILE),
            hashlib.sha256(canonical.encode()).hexdigest(),
        )

    def test_setting_is_off_by_default_and_strict(self):
        self.assertFalse(control.live_profile_enabled({}))
        self.assertFalse(control.live_profile_enabled({"ALANA_LIVE_PROFILE": ""}))
        self.assertFalse(control.live_profile_enabled({"ALANA_LIVE_PROFILE": "off"}))
        self.assertTrue(control.live_profile_enabled(ON))
        with self.assertRaises(ValueError):
            control.live_profile_enabled({"ALANA_LIVE_PROFILE": "1"})

    def test_configured_document(self):
        self.assertIsNone(control.configured_live_profile({}))
        document = control.configured_live_profile(ON)
        self.assertEqual(document["profile"], DEFAULT_PROFILE)
        self.assertEqual(document["checksum"], control.profile_checksum(DEFAULT_PROFILE))
        self.assertEqual(
            control.configured_live_profile({**ON, "GPU_TYPE": "NVIDIA"}),
            {"profile": None, "checksum": None},
        )


class StartupSourceAgreementTests(unittest.TestCase):
    """The derivation reads startup.sh's own defaults and flags, not a second copy."""

    source = STARTUP.read_text(encoding="utf-8")

    def test_defaults_match_startup_sh(self):
        for name, default in control.STARTUP_DEFAULTS.items():
            with self.subTest(name=name):
                self.assertIn(f'{name}="${{{name}:-{default}}}"', self.source)

    def test_rtmp_encoder_reads_exactly_these_settings(self):
        for fragment in (
            '-video_size "${RESOLUTION}" -framerate "${FPS}"',
            '-c:a aac -b:a "${AUDIO_BITRATE}" -ar "${AUDIO_RATE}" -ac 2',
            'libx264) VIDEO_ARGS=(-c:v libx264 -preset "${X264_PRESET}" -tune zerolatency -b:v "${VIDEO_BITRATE}"',
            '-g "${GOP_SIZE}" -sc_threshold 0)',
            "AUDIO_IN=(-map 1:a:0); AUDIO_RATE=44100",
            "-map '[aout]'); AUDIO_RATE=48000",
            'case "${GPU_TYPE:-}" in NVIDIA) rtmp_encoder=h264_nvenc ;; INTEL) rtmp_encoder=h264_vaapi ;; *) rtmp_encoder="${VIDEO_ENCODER}" ;; esac',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertEqual(control.audio_chain.SAMPLE_RATE, 48000)


def profile_from_rtmp_arguments(command):
    """Read the nine contract values back from one real RTMP encoder argv."""

    def value(flag):
        return command[command.index(flag) + 1]

    width, height = value("-video_size").split("x")
    return {
        "width": int(width),
        "height": int(height),
        "fps": int(value("-framerate")),
        "videoBitrate": value("-b:v"),
        "audioRate": int(value("-ar")),
        "audioChannels": int(value("-ac")),
        "audioBitrate": value("-b:a"),
        "gop": int(value("-g")),
        "loopSeconds": control.FILLER_LOOP_SECONDS,
    }


@unittest.skipUnless(startup._bash_supports_startup(), "startup.sh requires bash 4.3 or newer")
class StartupArgumentAgreementTests(unittest.TestCase):
    """Run startup.sh with logging stubs and compare its real RTMP argv to the profile."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        stubs = self.root / "bin"
        stubs.mkdir()
        for name in startup.STUBS:
            path = stubs / name
            path.write_text(startup.STUB, encoding="utf-8")
            path.chmod(0o755)
        self.log = self.root / "stub.log"
        self.process = None
        self.environment = {
            "PATH": f"{stubs}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STUB_LOG": str(self.log),
            "PROGRAM_ID": "test-program",
            "CHANNEL_BROWSER_URL": "http://renderer.test/program",
            "RTMP_OUTPUTS": "rtmp://relay.test/live/a",
            "RESTART_BACKOFF": "1",
            "ALANA_RUNTIME_METRICS_FILE": str(self.root / "runtime-metrics.json"),
            "ALANA_REPO": str(REPO),
            "STUB_PYTHON": sys.executable,
        }

    def tearDown(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
        self.temp.cleanup()

    def rtmp_arguments(self, settings):
        self.process = subprocess.Popen(
            ["bash", str(STARTUP)],
            env={**self.environment, **settings},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                lines = self.log.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            for line in lines:
                command = tuple(line.split(startup.SEPARATOR))
                if command[0] == "ffmpeg" and "flv" in command:
                    return command
            time.sleep(0.1)
        self.fail("startup.sh never started the RTMP encoder")

    def test_profile_equals_the_started_rtmp_encoder_across_supported_modes(self):
        for settings in (
            {},
            {"ALANA_PROCESSED_AUDIO": "off"},
            {
                "RESOLUTION": "1280x720",
                "FPS": "25",
                "VIDEO_BITRATE": "3000k",
                "GOP_SIZE": "50",
                "AUDIO_BITRATE": "96k",
            },
        ):
            with self.subTest(settings=settings):
                command = self.rtmp_arguments(settings)
                self.assertIn("libx264", command)
                self.assertEqual(
                    profile_from_rtmp_arguments(command),
                    control.effective_live_profile(settings),
                )
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
                self.process = None
                self.log.unlink()


class LifecycleFixture(unittest.TestCase):
    """test_control_server's lifecycle fixture, without re-running its tests."""

    filler_payload = staticmethod(base.LifecycleTests.filler_payload)
    destination_selection = staticmethod(base.LifecycleTests.destination_selection)

    def setUp(self):
        base.LifecycleTests.setUp(self)

    def tearDown(self):
        base.LifecycleTests.tearDown(self)


class BoundLifecycleTests(LifecycleFixture):
    """The same fixture with ALANA_LIVE_PROFILE=on."""

    def setUp(self):
        super().setUp()
        self.live = control.configured_live_profile(MATCHING)
        self.manager = self.bound_manager(self.live)
        self.manager.state["pendingFiller"] = None
        self.manager._save()

    def bound_manager(self, live, croccante=None, events=None):
        return control.LifecycleManager(
            self.store,
            base.FakePipeline(self.events if events is None else events),
            croccante or self.croccante,
            sleep=lambda _: None,
            ready_timeout=1,
            start_monitor=False,
            live_profile=live,
        )

    def ack(self, version, payload, **changes):
        answer = {
            "version": version,
            "status": "ready",
            "ready": True,
            "sourceId": payload["source"]["id"],
            "sourceSha256": payload["source"]["sha256"],
            "artifactSha256": "b" * 64,
            "profile": dict(payload["profile"]),
            "preparedAt": "2026-08-24T00:00:00Z",
        }
        answer.update(changes)
        return answer

    def test_matching_acknowledgement_is_ready_with_the_live_checksum_and_binds_start(self):
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", self.filler_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["profileChecksum"], self.live["checksum"])
        self.assertEqual(self.manager.view()["pendingFiller"]["profileChecksum"], self.live["checksum"])
        self.assertEqual(binding_count("prepare", "bound"), 1)
        status, started = self.manager.command("start", "start-two", 1, self.destination_selection())
        self.assertEqual(status, 200)
        self.assertEqual(started["activeFiller"]["version"], "filler-v2")
        self.assertEqual(started["activeFiller"]["profileChecksum"], self.live["checksum"])
        self.assertEqual(binding_count("start", "bound"), 1)
        # The request's source identity is private bookkeeping, never public.
        self.assertNotIn("requestedSource", json.dumps(started))

    def test_caller_profile_that_differs_from_the_live_profile_is_refused_before_any_change(self):
        prepared = self.manager.prepare_filler("filler-v2", "prepare-one", self.filler_payload())
        self.assertEqual(prepared[0], 200)
        stale = self.filler_payload(command="prepare-two", source="source-two")
        stale["profile"]["audioBitrate"] = "128k"
        status, body = self.manager.prepare_filler("filler-v3", "prepare-two", stale)
        self.assertEqual(status, 409)
        self.assertEqual(body, {"version": "filler-v3", "status": "failed", "ready": False, "reason": "profile-mismatch"})
        self.assertEqual(self.events, ["croccante:prepare:filler-v2"])
        # The known-good pending version is untouched.
        self.assertEqual(self.manager.view()["pendingFiller"]["version"], "filler-v2")
        self.assertTrue(self.manager.view()["pendingFiller"]["ready"])
        self.assertEqual(binding_count("prepare", "caller_mismatch"), 1)
        for broken in (None, "1920x1080", {}):
            payload = self.filler_payload(command="prepare-three")
            payload["profile"] = broken
            self.assertEqual(self.manager.prepare_filler("filler-v4", "prepare-three", payload)[0], 409)
        typed = self.filler_payload(command="prepare-four")
        typed["profile"]["fps"] = 30.0
        self.assertEqual(self.manager.prepare_filler("filler-v5", "prepare-four", typed)[0], 409)
        self.assertEqual(self.events, ["croccante:prepare:filler-v2"])

    def test_unknown_caller_profile_fields_are_ignored_and_counted(self):
        payload = self.filler_payload()
        payload["profile"]["pixelFormat"] = "yuv420p"
        output = StringIO()
        with redirect_stdout(output):
            status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])
        self.assertEqual(base.unrecognized_count("filler_profile", "unknown_field"), 1)
        self.assertNotIn("yuv420p", output.getvalue())

    def test_unqualified_renderer_refuses_every_preparation(self):
        manager = self.bound_manager(control.configured_live_profile({**ON, "GPU_TYPE": "NVIDIA"}))
        status, body = manager.prepare_filler("filler-v2", "prepare-one", self.filler_payload())
        self.assertEqual(status, 503)
        self.assertEqual(body["reason"], "invalid-profile")
        self.assertFalse(body["ready"])
        self.assertEqual(self.events, [])
        self.assertEqual(binding_count("prepare", "unavailable"), 1)

    def test_acknowledged_profile_other_than_the_live_one_is_not_ready(self):
        payload = self.filler_payload()
        self.croccante.prepare_answers = [
            (200, self.ack("filler-v2", payload, profile={**payload["profile"], "audioRate": 44100}))
        ]
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 409)
        self.assertEqual(body, {"version": "filler-v2", "status": "failed", "ready": False, "reason": "profile-mismatch"})
        self.assertEqual(binding_count("prepare", "profile_mismatch"), 1)
        status, started = self.manager.command("start", "start-two", 1, self.destination_selection())
        self.assertEqual(status, 409)
        self.assertEqual(started["commandResult"]["result"], "filler-not-ready")
        self.assertNotIn("pipeline:start", self.events)

    def test_old_or_incomplete_acknowledgement_fails_closed(self):
        payload = self.filler_payload()
        incomplete = self.ack("filler-v2", payload)
        del incomplete["profile"]
        self.croccante.prepare_answers = [(200, incomplete), (200, {"version": "filler-v2", "status": "ready", "ready": True})]
        for _ in range(2):
            status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
            self.assertEqual(status, 409)
            self.assertFalse(body["ready"])
            self.assertEqual(body["reason"], "profile-mismatch")
        # A failed version stays retryable: a complete acknowledgement readies it.
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])

    def test_acknowledged_source_other_than_the_request_is_not_ready(self):
        payload = self.filler_payload()
        self.croccante.prepare_answers = [(200, self.ack("filler-v2", payload, sourceSha256="c" * 64))]
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 409)
        self.assertEqual(body, {"version": "filler-v2", "status": "failed", "ready": False, "error": "unexpected-state"})
        self.assertEqual(binding_count("prepare", "source_mismatch"), 1)

    def test_changed_live_profile_after_restart_fails_start_closed(self):
        self.assertEqual(self.manager.prepare_filler("filler-v2", "prepare-one", self.filler_payload())[0], 200)
        recovered_events = []
        changed = self.bound_manager(control.configured_live_profile(ON), events=recovered_events)
        pending = changed.view()["pendingFiller"]
        self.assertFalse(pending["ready"])
        self.assertEqual(pending["reason"], "profile-mismatch")
        status, body = changed.command("start", "start-two", 1, self.destination_selection())
        self.assertEqual(status, 409)
        self.assertEqual(body["commandResult"]["result"], "filler-not-ready")
        self.assertEqual(recovered_events, [])
        self.assertEqual(binding_count("start", "profile_mismatch"), 1)
        # The same live profile after a restart keeps the prepared version ready.
        same = self.bound_manager(control.configured_live_profile(MATCHING), events=recovered_events)
        self.assertTrue(same.view()["pendingFiller"]["ready"])

    def test_renderer_that_became_unqualified_after_preparation_fails_start_closed(self):
        self.assertEqual(self.manager.prepare_filler("filler-v2", "prepare-one", self.filler_payload())[0], 200)
        events = []
        unqualified = self.bound_manager(
            control.configured_live_profile({**MATCHING, "GPU_TYPE": "INTEL"}), events=events
        )
        self.assertEqual(unqualified.live_profile_state(), "unqualified")
        self.assertFalse(unqualified.view()["pendingFiller"]["ready"])
        status, body = unqualified.command("start", "start-two", 1, self.destination_selection())
        self.assertEqual(status, 409)
        self.assertEqual(body["commandResult"]["result"], "filler-not-ready")
        self.assertEqual(events, [])
        self.assertEqual(binding_count("start", "unavailable"), 1)

    def test_identical_replay_is_a_bound_duplicate(self):
        payload = self.filler_payload()
        self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicate"])
        self.assertEqual(body["profileChecksum"], self.live["checksum"])
        self.assertEqual(self.events.count("croccante:prepare:filler-v2"), 1)

    def test_version_prepared_while_off_rebinds_on_replay(self):
        payload = self.filler_payload()
        off = control.LifecycleManager(
            self.store, base.FakePipeline([]), self.croccante, sleep=lambda _: None, ready_timeout=1, start_monitor=False
        )
        self.assertEqual(off.prepare_filler("filler-v2", "prepare-one", payload)[0], 200)
        self.assertNotIn("requestedSource", json.dumps(off.state))
        bound = self.bound_manager(self.live)
        # No recorded request source yet: not ready until the caller replays it.
        self.assertFalse(bound.view()["pendingFiller"]["ready"])
        status, body = bound.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicate"])
        self.assertEqual(body["profileChecksum"], self.live["checksum"])
        self.assertEqual(self.events.count("croccante:prepare:filler-v2"), 1)

    def test_replayed_version_whose_acknowledgement_no_longer_binds_is_prepared_again(self):
        payload = self.filler_payload()
        self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.manager.state["pendingFiller"]["sourceSha256"] = "c" * 64
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 200)
        self.assertNotIn("duplicate", body)
        self.assertEqual(self.events.count("croccante:prepare:filler-v2"), 2)

    def test_active_version_is_immutable_even_when_its_binding_no_longer_holds(self):
        payload = self.filler_payload()
        self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.manager.command("start", "start-two", 1, self.destination_selection())
        active = dict(self.manager.state["activeFiller"])
        self.manager.state["activeFiller"]["sourceSha256"] = "c" * 64
        tampered = dict(self.manager.state["activeFiller"])
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 409)
        self.assertFalse(body["ready"])
        self.assertEqual(self.manager.state["activeFiller"], tampered)
        self.assertNotEqual(active, tampered)

    def test_restart_reconciliation_binds_with_the_recorded_request(self):
        payload = self.filler_payload()
        self.croccante.prepare_answers = [(502, {"version": "filler-v2", "status": "failed", "ready": False, "error": "unavailable-or-invalid-response"})]
        self.assertEqual(self.manager.prepare_filler("filler-v2", "prepare-one", payload)[0], 502)
        recorded = json.loads((Path(self.temp.name) / "lifecycle.json").read_text())
        self.assertEqual(recorded["pendingFiller"]["requestedSource"], ["source-one", "a" * 64])
        self.assertNotIn("never-store", json.dumps(recorded))

        events = []
        downstream = base.FakeCroccante(
            events,
            status_answers=[
                (404, {"version": "filler-v2", "status": "preparing", "ready": False}),
                (200, {"version": "filler-v2", "status": "ready", "ready": True}),
                (200, self.ack("filler-v2", payload)),
            ],
        )
        recovered = self.bound_manager(self.live, croccante=downstream, events=events)
        recovered.reconcile_once()
        self.assertEqual(recovered.view()["pendingFiller"]["status"], "preparing")
        self.assertEqual(recovered.state["pendingFiller"]["requestedSource"], ["source-one", "a" * 64])
        recovered.reconcile_once()
        self.assertFalse(recovered.view()["pendingFiller"]["ready"])
        self.assertEqual(binding_count("reconcile", "profile_mismatch"), 1)
        recovered.reconcile_once()
        pending = recovered.view()["pendingFiller"]
        self.assertTrue(pending["ready"])
        self.assertEqual(pending["profileChecksum"], self.live["checksum"])
        self.assertEqual(binding_count("reconcile", "bound"), 1)

    def test_status_read_binds_held_versions_and_never_readies_unknown_ones(self):
        payload = self.filler_payload()
        self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.croccante.status_answers = [(404, {"version": "filler-v2", "status": "preparing", "ready": False})]
        self.assertEqual(
            self.manager.filler_status("filler-v2"),
            (404, {"version": "filler-v2", "status": "preparing", "ready": False}),
        )
        self.croccante.status_answers = [(200, self.ack("filler-v2", payload)), (200, self.ack("filler-v7", payload))]
        status, body = self.manager.filler_status("filler-v2")
        self.assertEqual(status, 200)
        self.assertEqual(body["profileChecksum"], self.live["checksum"])
        self.assertNotIn("requestedSource", body)
        status, body = self.manager.filler_status("filler-v7")
        self.assertEqual(status, 409)
        self.assertFalse(body["ready"])
        self.assertEqual(body["error"], "unexpected-state")
        self.assertEqual(binding_count("status", "bound"), 1)
        self.assertEqual(binding_count("status", "source_mismatch"), 1)
        self.croccante.status_answers = [(404, {"version": "filler-v8", "status": "unprepared", "ready": False})]
        self.assertEqual(self.manager.filler_status("filler-v8"), (404, {"version": "filler-v8", "status": "unprepared", "ready": False}))


class OffPathTests(LifecycleFixture):
    """With the setting off, nothing in the filler contract changes."""

    def test_off_never_adds_a_checksum_or_refuses_a_profile(self):
        self.manager.state["pendingFiller"] = None
        payload = self.filler_payload()
        payload["profile"]["audioBitrate"] = "1k"
        status, body = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(status, 200)
        self.assertNotIn("profileChecksum", body)
        self.assertNotIn("profileChecksum", json.dumps(self.manager.view()))
        self.assertNotIn("requestedSource", json.dumps(self.manager.state))
        self.manager.state["pendingFiller"]["profileChecksum"] = "d" * 64
        self.assertNotIn("profileChecksum", json.dumps(self.manager.view()))
        self.assertEqual(self.manager.live_profile_state(), "off")


class MainWiringTests(unittest.TestCase):
    """main() builds the manager from the process environment, as in the image."""

    def run_main(self, environment):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)

        class Server:
            def __init__(self, address, handler):
                self.address = address

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        previous = (control.MANAGER, control.RECORDER, control.TAPE)
        self.addCleanup(lambda: setattr(control, "MANAGER", previous[0]))
        self.addCleanup(lambda: setattr(control, "RECORDER", previous[1]))
        self.addCleanup(lambda: setattr(control, "TAPE", previous[2]))
        validations = []
        settings = {
            "CROCCANTE_CONTROL_URL": "http://127.0.0.1:9",
            "CROCCANTE_CONTROL_TOKEN_FILE": str(root / "missing-token"),
            **environment,
        }
        with (
            patch.dict(os.environ, settings),
            patch.object(control, "STATE_DIR", root),
            # StateStore() binds its root default at definition time.
            patch.object(control.StateStore.__init__, "__defaults__", (root,)),
            patch.object(control.subprocess, "run", lambda command, check: validations.append((tuple(command), check))),
            patch.object(control, "ThreadingHTTPServer", Server),
            patch.object(control.signal, "signal", lambda *_: None),
            redirect_stdout(StringIO()) as output,
        ):
            control.main()
        self.assertEqual(validations, [(("/usr/local/bin/validate-config.sh",), True)])
        self.assertTrue(control.MANAGER.closed.is_set())
        return control.MANAGER, output.getvalue()

    def test_main_reads_the_setting_and_the_renderer_configuration(self):
        manager, output = self.run_main({"ALANA_LIVE_PROFILE": "on", "RESOLUTION": "1280x720"})
        self.assertEqual(manager.live_profile["profile"]["width"], 1280)
        self.assertEqual(manager.live_profile_state(), "published")
        self.assertIn("[control] live profile published", output)
        manager, output = self.run_main({"ALANA_LIVE_PROFILE": "off"})
        self.assertIsNone(manager.live_profile)
        self.assertIn("[control] live profile off", output)


class LiveProfileMetricTests(unittest.TestCase):
    def test_state_gauge_and_binding_counter_stay_bounded(self):
        metrics = control.Metrics(Path(tempfile.gettempdir()) / "alana-live-profile-unused.json")
        rendered = metrics.render({"liveProfileState": "https://renderer.example/secret"})
        self.assertIn('alana_live_profile_state{state="off"} 1', rendered)
        self.assertNotIn("renderer.example", rendered)
        metrics.observe_profile_binding("start", "bound")
        self.assertIn(
            'alana_filler_profile_bindings_total{operation="start",result="bound"} 1',
            metrics.render({}),
        )
        for operation, result in (("start", "free-form"), ("deploy", "bound")):
            with self.assertRaises(ValueError):
                metrics.observe_profile_binding(operation, result)


class LiveProfileHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        control.METRICS = control.Metrics(Path(self.temp.name) / "runtime-metrics.json")
        token_file = Path(self.temp.name) / "control-token"
        token_file.write_text("api-secret\n")
        self.previous_token = control.CONTROL_TOKEN_FILE
        control.CONTROL_TOKEN_FILE = token_file
        self.previous_manager = control.MANAGER
        self.previous_recorder = control.RECORDER
        control.RECORDER = base.FakeRecording()
        self.server = control.ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        control.CONTROL_TOKEN_FILE = self.previous_token
        control.MANAGER = self.previous_manager
        control.RECORDER = self.previous_recorder
        self.temp.cleanup()

    def use(self, live):
        control.MANAGER = control.LifecycleManager(
            control.StateStore(Path(self.temp.name)),
            base.FakePipeline([]),
            base.FakeCroccante([]),
            start_monitor=False,
            live_profile=live,
        )

    def get(self, path, token="api-secret"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urlopen(Request(f"{self.origin}{path}", headers=headers)) as response:
                return response.status, response.read().decode()
        except HTTPError as error:
            with error:
                return error.code, error.read().decode()

    def test_published_profile_is_authenticated_program_scoped_and_self_consistent(self):
        self.use(control.configured_live_profile(ON))
        self.assertEqual(self.get(control.LIVE_PROFILE_PATH, token="")[0], 401)
        self.assertEqual(self.get(control.LIVE_PROFILE_PATH, token="wrong")[0], 401)
        self.assertEqual(self.get("/v1/programs/other-program/live-profile")[0], 404)
        status, text = self.get(control.LIVE_PROFILE_PATH)
        self.assertEqual(status, 200)
        document = json.loads(text)
        self.assertEqual(
            document,
            {
                "contractVersion": 1,
                "programId": "test-program",
                "profile": DEFAULT_PROFILE,
                "checksum": control.profile_checksum(DEFAULT_PROFILE),
            },
        )
        self.assertRegex(document["checksum"], r"^[0-9a-f]{64}$")
        _, metrics = self.get(control.METRICS_PATH)
        self.assertIn('alana_live_profile_state{state="published"} 1', metrics)
        self.assertIn('alana_live_profile_state{state="off"} 0', metrics)
        self.assertIn('alana_http_requests_total{method="GET",route="live_profile",status_class="2xx"} 1', metrics)
        self.assertIn('alana_http_requests_total{method="GET",route="live_profile",status_class="4xx"} 3', metrics)

    def test_off_and_unqualified_answer_404(self):
        for live, state in (
            (None, "off"),
            (control.configured_live_profile({**ON, "VIDEO_ENCODER": "h264_vaapi"}), "unqualified"),
        ):
            with self.subTest(state=state):
                self.use(live)
                self.assertEqual(self.get(control.LIVE_PROFILE_PATH)[0], 404)
                _, metrics = self.get(control.METRICS_PATH)
                self.assertIn(f'alana_live_profile_state{{state="{state}"}} 1', metrics)

    def test_bound_preparation_over_http_carries_the_checksum(self):
        live = control.configured_live_profile(MATCHING)
        self.use(live)
        body = json.dumps(base.LifecycleTests.filler_payload()).encode()
        request = Request(
            f"{self.origin}{control.FILLER_PATH}filler-v2",
            method="PUT",
            data=body,
            headers={"Authorization": "Bearer api-secret", "Idempotency-Key": "prepare-one", "Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            prepared = json.loads(response.read())
        self.assertEqual(prepared["profileChecksum"], live["checksum"])
        status, text = self.get(control.PROGRAM_PATH)
        self.assertEqual(json.loads(text)["pendingFiller"]["profileChecksum"], live["checksum"])
        self.assertNotIn("never-store", text)
        _, metrics = self.get(control.METRICS_PATH)
        self.assertIn('alana_filler_profile_bindings_total{operation="prepare",result="bound"} 1', metrics)


class BoundCroccanteContractTests(unittest.TestCase):
    """A real local HTTP Croccante whose acknowledgement carries an unknown profile key."""

    def setUp(self):
        base.CroccanteClientContractTests.setUp(self)

    def tearDown(self):
        base.CroccanteClientContractTests.tearDown(self)

    def test_unknown_acknowledged_profile_key_still_binds_on_the_nine_known_keys(self):
        self.overrides["profile"] = {"pixelFormat": "yuv420p"}
        live = control.configured_live_profile(MATCHING)
        manager = control.LifecycleManager(
            control.StateStore(Path(self.temp.name)),
            base.FakePipeline([]),
            self.client,
            start_monitor=False,
            live_profile=live,
        )
        payload = base.LifecycleTests.filler_payload(command="prepare-nine")
        with redirect_stdout(StringIO()):
            status, prepared = manager.prepare_filler("filler-v9", "prepare-nine", payload)
        self.assertEqual(status, 200)
        self.assertTrue(prepared["ready"])
        self.assertEqual(prepared["profileChecksum"], live["checksum"])
        self.assertEqual(base.unrecognized_count("filler_profile", "unknown_field"), 1)
        self.assertEqual(self.requests[0][3], payload)


if __name__ == "__main__":
    unittest.main()
