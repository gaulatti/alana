import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


os.environ.setdefault("PROGRAM_ID", "test-program")
spec = importlib.util.spec_from_file_location(
    "alana_control", Path(__file__).parents[1] / "control-server.py"
)
control = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(control)

TEST_DESTINATION_SELECTION = {
    "version": "destinations-v1",
    "destinations": [
        {
            "id": "primary",
            "secretId": "broadcast/test/primary",
            "versionId": "version-1",
        }
    ],
}


class FakePipeline:
    def __init__(self, events, *, running=False, ready=True):
        self.events = events
        self.is_running = running
        self.is_ready = ready

    def running(self):
        return self.is_running

    def start(self):
        if self.is_running:
            return
        self.events.append("pipeline:start")
        self.is_running = True

    def ready(self):
        return self.is_running and self.is_ready

    def status(self):
        return {"pipelineProcessHealthy": self.is_running}

    def stop(self):
        self.events.append("pipeline:stop")
        self.is_running = False


class FakeCroccante:
    def __init__(self, events, answers=None, prepare_answers=None, status_answers=None):
        self.events = events
        self.answers = list(answers or [])
        self.prepare_answers = list(prepare_answers or [])
        self.status_answers = list(status_answers or [])

    def command(
        self,
        action,
        key,
        sequence,
        filler_version=None,
        destination_selection=None,
    ):
        self.events.append(f"croccante:{action}")
        if self.answers:
            return self.answers.pop(0)
        metadata = (
            control.destination_metadata(destination_selection)
            if destination_selection is not None
            else None
        )
        return {
            "accepted": True,
            "requestedState": f"{action}ed",
            "actualState": f"{action}ed",
            **({"fillerVersion": filler_version} if action == "start" else {}),
            **(
                {
                    "destinationConfiguration": metadata,
                    "destinations": [
                        {
                            "id": item["id"],
                            "mode": "waiting-for-publisher",
                            "supervisorHealthy": True,
                            "publisherProcessHealthy": False,
                        }
                        for item in destination_selection["destinations"]
                    ],
                }
                if metadata and destination_selection
                else {}
            ),
        }

    def reload_destinations(self, version, key, selection):
        self.events.append(f"croccante:reload:{version}")
        return 200, {
            "accepted": True,
            **control.destination_metadata(selection),
            "result": "validated",
        }

    def status(self):
        if self.status_answers:
            return self.status_answers.pop(0)
        return 200, {
            "requestedState": "started",
            "actualState": "started",
            "destinationConfiguration": control.destination_metadata(
                TEST_DESTINATION_SELECTION
            ),
        }

    def prepare(self, version, key, payload):
        self.events.append(f"croccante:prepare:{version}")
        if self.prepare_answers:
            return self.prepare_answers.pop(0)
        return 200, {
            "version": version,
            "status": "ready",
            "ready": True,
            "sourceId": payload["source"]["id"],
            "sourceSha256": payload["source"]["sha256"],
            "artifactSha256": "b" * 64,
            "profile": payload["profile"],
            "preparedAt": "2026-08-24T00:00:00Z",
        }

    def filler_status(self, version):
        self.events.append(f"croccante:status:{version}")
        if self.status_answers:
            return self.status_answers.pop(0)
        return 200, {"version": version, "status": "ready", "ready": True}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = []
        self.store = control.StateStore(Path(self.temp.name))
        control.METRICS = control.Metrics(Path(self.temp.name) / "runtime-metrics.json")
        self.pipeline = FakePipeline(self.events)
        self.croccante = FakeCroccante(self.events)
        self.manager = control.LifecycleManager(
            self.store,
            self.pipeline,
            self.croccante,
            sleep=lambda _: None,
            ready_timeout=1,
            start_monitor=False,
        )
        self.manager.state["pendingFiller"] = {
            "version": "filler-v1",
            "status": "ready",
            "ready": True,
            "configurationDigest": "fixture",
        }
        self.manager._save()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def destination_selection():
        return json.loads(json.dumps(TEST_DESTINATION_SELECTION))

    @staticmethod
    def filler_payload(command="prepare-one", source="source-one"):
        return {
            "commandId": command,
            "source": {
                "id": source,
                "sha256": "a" * 64,
                "downloadUrl": "https://signed.example/private?token=never-store",
            },
            "profile": {
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "videoBitrate": "6000k",
                "audioRate": 48000,
                "audioChannels": 2,
                "audioBitrate": "160k",
                "gop": 60,
                "loopSeconds": 10,
            },
        }

    def test_start_waits_for_pipeline_before_croccante(self):
        status, body = self.manager.command(
            "start", "start-one", 1, self.destination_selection()
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.events, ["pipeline:start", "croccante:start"])
        self.assertEqual(body["actualState"], "running")
        self.assertTrue(body["readiness"])
        self.assertEqual(body["activeFiller"]["version"], "filler-v1")
        self.assertIsNone(body["pendingFiller"])

    def test_stop_acknowledgement_precedes_pipeline_teardown(self):
        self.manager.command("start", "start-one", 1, self.destination_selection())
        self.events.clear()
        status, body = self.manager.command("stop", "stop-one", 2)
        self.assertEqual(status, 200)
        self.assertEqual(self.events, ["croccante:stop", "pipeline:stop"])
        self.assertEqual(body["actualState"], "stopped")

    def test_duplicate_command_is_idempotent(self):
        first = self.manager.command("start", "same-key", 1, self.destination_selection())
        duplicate = self.manager.command(
            "start", "same-key", 1, self.destination_selection()
        )
        self.assertEqual(first[0], 200)
        self.assertEqual(duplicate[0], 200)
        self.assertTrue(duplicate[1]["commandResult"]["duplicate"])
        self.assertEqual(self.events.count("pipeline:start"), 1)
        self.assertEqual(self.events.count("croccante:start"), 1)

    def test_start_fails_closed_until_exact_filler_is_ready(self):
        self.manager.state["pendingFiller"] = {
            "version": "filler-v2",
            "status": "preparing",
            "ready": False,
        }
        status, body = self.manager.command(
            "start", "start-unready", 1, self.destination_selection()
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["commandResult"]["result"], "filler-not-ready")
        self.assertEqual(self.events, [])

    def test_prepare_is_idempotent_and_precedes_bound_start(self):
        self.manager.state["pendingFiller"] = None
        payload = self.filler_payload()
        first = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        duplicate = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(first[0], 200)
        self.assertEqual(duplicate[0], 200)
        self.assertTrue(duplicate[1]["duplicate"])
        self.assertEqual(self.events, ["croccante:prepare:filler-v2"])
        status, body = self.manager.command(
            "start", "start-two", 1, self.destination_selection()
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.events,
            ["croccante:prepare:filler-v2", "pipeline:start", "croccante:start"],
        )
        self.assertEqual(body["croccanteAcknowledgement"]["fillerVersion"], "filler-v2")

    def test_version_conflict_is_rejected_without_downstream_side_effect(self):
        self.manager.state["pendingFiller"] = None
        self.manager.prepare_filler("filler-v2", "prepare-one", self.filler_payload())
        changed = self.filler_payload(command="prepare-two", source="source-two")
        status, body = self.manager.prepare_filler("filler-v2", "prepare-two", changed)
        self.assertEqual(status, 409)
        self.assertEqual(body["reason"], "version-conflict")
        self.assertEqual(self.events.count("croccante:prepare:filler-v2"), 1)

    def test_active_filler_is_immutable_while_next_version_prepares(self):
        self.manager.command("start", "start-one", 1, self.destination_selection())
        payload = self.filler_payload(command="prepare-two", source="source-two")
        status, _ = self.manager.prepare_filler("filler-v2", "prepare-two", payload)
        self.assertEqual(status, 200)
        view = self.manager.view()
        self.assertEqual(view["activeFiller"]["version"], "filler-v1")
        self.assertEqual(view["pendingFiller"]["version"], "filler-v2")
        self.manager.command("stop", "stop-one", 2)
        view = self.manager.view()
        self.assertIsNone(view["activeFiller"])
        self.assertEqual(view["pendingFiller"]["version"], "filler-v2")

    def test_failed_preparation_is_visible_and_retryable(self):
        self.manager.state["pendingFiller"] = None
        self.croccante.prepare_answers = [
            (502, {"version": "filler-v2", "status": "failed", "ready": False, "error": "unavailable-or-invalid-response"}),
            (200, {"version": "filler-v2", "status": "ready", "ready": True}),
        ]
        payload = self.filler_payload()
        first = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        second = self.manager.prepare_filler("filler-v2", "prepare-one", payload)
        self.assertEqual(first[0], 502)
        self.assertEqual(second[0], 200)
        self.assertEqual(self.events.count("croccante:prepare:filler-v2"), 2)

    def test_restart_reconciles_pending_preparation_without_download_material(self):
        self.manager.state["pendingFiller"] = {
            "version": "filler-v2",
            "status": "failed",
            "ready": False,
            "configurationDigest": "safe-digest",
        }
        self.manager._save()
        recovered_events = []
        recovered = control.LifecycleManager(
            self.store,
            FakePipeline(recovered_events),
            FakeCroccante(recovered_events),
            start_monitor=False,
        )
        recovered.reconcile_once()
        self.assertEqual(recovered_events, ["croccante:status:filler-v2"])
        self.assertTrue(recovered.view()["pendingFiller"]["ready"])

    def test_reused_key_for_different_command_is_rejected(self):
        self.manager.command("start", "same-key", 1, self.destination_selection())
        status, body = self.manager.command("stop", "same-key", 2)
        self.assertEqual(status, 409)
        self.assertIn("already used", body["error"])

    def test_old_sequence_is_rejected(self):
        self.manager.command("start", "one", 5, self.destination_selection())
        status, _ = self.manager.command("stop", "two", 4)
        self.assertEqual(status, 409)

    def test_failed_stop_keeps_pipeline_and_retries_before_teardown(self):
        self.manager.command("start", "start", 1, self.destination_selection())
        self.croccante.answers = [
            {"accepted": False, "error": "unavailable"},
            {"accepted": True, "requestedState": "stopped"},
        ]
        self.events.clear()
        status, body = self.manager.command("stop", "stop", 2)
        self.assertEqual(status, 502)
        self.assertTrue(self.pipeline.running())
        self.assertEqual(body["actualState"], "degraded")
        self.assertEqual(self.events, ["croccante:stop"])
        self.manager.reconcile_once()
        self.assertEqual(self.events, ["croccante:stop", "croccante:stop", "pipeline:stop"])
        self.assertEqual(self.manager.view()["actualState"], "stopped")

    def test_publisher_drop_recovers_without_stop_command(self):
        self.manager.command("start", "start", 1, self.destination_selection())
        self.events.clear()
        self.pipeline.is_running = False
        self.manager.reconcile_once()
        self.assertEqual(self.events, ["pipeline:start"])
        self.assertEqual(self.manager.view()["actualState"], "running")

    def test_restart_recovers_persisted_running_request(self):
        self.manager.command("start", "start", 1, self.destination_selection())
        recovered_events = []
        recovered = control.LifecycleManager(
            self.store,
            FakePipeline(recovered_events, running=False),
            FakeCroccante(recovered_events),
            sleep=lambda _: None,
            ready_timeout=1,
            start_monitor=False,
        )
        recovered.reconcile_once()
        self.assertEqual(recovered_events, ["pipeline:start"])
        self.assertEqual(recovered.view()["actualState"], "running")

    def test_restart_retries_persisted_stop_before_marking_stopped(self):
        self.manager.command("start", "start", 1, self.destination_selection())
        self.croccante.answers = [{"accepted": False, "error": "unavailable"}]
        self.manager.command("stop", "stop", 2)
        recovered_events = []
        recovered = control.LifecycleManager(
            self.store,
            FakePipeline(recovered_events, running=False),
            FakeCroccante(recovered_events),
            start_monitor=False,
        )
        recovered.reconcile_once()
        self.assertEqual(recovered_events, ["croccante:stop"])
        self.assertEqual(recovered.view()["actualState"], "stopped")

    def test_state_and_command_records_never_store_key_or_token(self):
        secret = "never-persist-this-secret"
        self.manager.command("start", secret, 1, self.destination_selection())
        self.manager.prepare_filler("filler-v2", "prepare-two", self.filler_payload(command="prepare-two"))
        persisted = "".join(
            path.read_text() for path in Path(self.temp.name).rglob("*.json")
        )
        self.assertNotIn(secret, persisted)
        self.assertNotIn(secret, json.dumps(self.manager.view()))
        self.assertNotIn("token=never-store", persisted)
        self.assertNotIn("downloadUrl", persisted)

    def test_public_state_redacts_pending_downstream_key(self):
        self.croccante.answers = [{"accepted": False, "error": "unavailable"}]
        self.manager.command(
            "start", "start-secret", 1, self.destination_selection()
        )
        pending = self.manager.view()["pendingCroccante"]
        self.assertEqual(pending["action"], "start")
        self.assertEqual(pending["sequence"], 1)
        self.assertEqual(pending["fillerVersion"], "filler-v1")
        self.assertEqual(pending["destinationVersion"], "destinations-v1")
        self.assertEqual(pending["destinationCount"], 1)
        self.assertEqual(pending["destinationIds"], ["primary"])
        self.assertNotIn("key", pending)
        self.assertNotIn("secretId", json.dumps(pending))

    def test_destination_selection_validation_is_bounded_and_exact(self):
        one = self.destination_selection()
        self.assertEqual(control.parse_destination_selection(one), one)
        many = {
            "version": "destinations-v20",
            "destinations": [
                {
                    "id": f"destination-{index}",
                    "secretId": f"broadcast/test/{index}",
                    "versionId": f"version-{index}",
                }
                for index in range(20)
            ],
        }
        self.assertEqual(
            len(control.parse_destination_selection(many)["destinations"]), 20
        )
        for invalid in (
            {"version": "destinations-empty", "destinations": []},
            {"version": "destinations-many", "destinations": many["destinations"] * 2},
            {
                "version": "destinations-duplicate",
                "destinations": one["destinations"] * 2,
            },
            {
                "version": "destinations-unknown",
                "destinations": [{**one["destinations"][0], "url": "rtmp://secret"}],
            },
        ):
            with self.assertRaises(ValueError):
                control.parse_destination_selection(invalid)

    def test_reload_is_idempotent_and_active_mutation_is_rejected(self):
        selection = self.destination_selection()
        first = self.manager.reload_destinations(
            selection["version"], "reload-one", selection
        )
        duplicate = self.manager.reload_destinations(
            selection["version"], "reload-one", selection
        )
        self.assertEqual(first[0], 200)
        self.assertEqual(duplicate[0], 200)
        self.assertTrue(duplicate[1]["duplicate"])
        self.assertEqual(self.events.count("croccante:reload:destinations-v1"), 1)
        self.manager.command("start", "start-one", 1, selection)
        status, body = self.manager.reload_destinations(
            selection["version"], "reload-two", selection
        )
        self.assertEqual(status, 409)
        self.assertIn("stopped", body["error"])

    def test_changed_selection_on_lifecycle_replay_is_rejected(self):
        selection = self.destination_selection()
        self.manager.command("start", "start-one", 1, selection)
        changed = self.destination_selection()
        changed["destinations"][0]["id"] = "secondary"
        status, body = self.manager.command("start", "start-one", 1, changed)
        self.assertEqual(status, 409)
        self.assertIn("already used", body["error"])

    def test_partial_downstream_rejection_never_reports_running(self):
        self.croccante.answers = [
            {
                "accepted": False,
                "requestedState": "started",
                "actualState": "failed",
                "error": "unexpected-state",
                "destinations": [
                    {
                        "id": "primary",
                        "mode": "failed",
                        "supervisorHealthy": False,
                        "publisherProcessHealthy": False,
                    }
                ],
            }
        ]
        status, body = self.manager.command(
            "start", "start-partial", 1, self.destination_selection()
        )
        self.assertEqual(status, 502)
        self.assertEqual(body["actualState"], "degraded")
        self.assertNotEqual(body["actualState"], "running")

    def test_restart_reconciles_pending_start_without_persisting_references(self):
        self.croccante.answers = [{"accepted": False, "error": "unavailable"}]
        self.manager.command(
            "start", "start-reconcile", 1, self.destination_selection()
        )
        persisted = "".join(
            path.read_text() for path in Path(self.temp.name).rglob("*.json")
        )
        self.assertNotIn("secretId", persisted)
        self.assertNotIn("broadcast/test/primary", persisted)
        recovered = control.LifecycleManager(
            self.store,
            FakePipeline([], running=True),
            FakeCroccante([]),
            start_monitor=False,
        )
        recovered.reconcile_once()
        self.assertEqual(recovered.view()["actualState"], "running")
        self.assertEqual(
            recovered.view()["activeDestinations"]["destinationIds"], ["primary"]
        )

    def test_exact_replay_after_restart_resupplies_unpersisted_references(self):
        self.croccante.answers = [{"accepted": False, "error": "unavailable"}]
        selection = self.destination_selection()
        self.manager.command("start", "start-replay", 1, selection)
        recovered_events = []
        downstream = FakeCroccante(
            recovered_events,
            status_answers=[
                (
                    200,
                    {
                        "requestedState": "stopped",
                        "actualState": "stopped",
                        "destinationConfiguration": None,
                    },
                )
            ],
        )
        recovered = control.LifecycleManager(
            self.store,
            FakePipeline(recovered_events, running=True),
            downstream,
            start_monitor=False,
        )
        recovered.reconcile_once()
        self.assertEqual(recovered.view()["actualState"], "degraded")
        status, body = recovered.command("start", "start-replay", 1, selection)
        self.assertEqual(status, 200)
        self.assertEqual(body["actualState"], "running")
        self.assertEqual(recovered_events, ["croccante:start"])

    def test_http_api_requires_authentication_and_program_scope(self):
        token_file = Path(self.temp.name) / "control-token"
        token_file.write_text("api-secret\n")
        control.CONTROL_TOKEN_FILE = token_file
        control.MANAGER = self.manager
        server = control.ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(HTTPError) as unauthorized:
                urlopen(f"{origin}{control.PROGRAM_PATH}")
            self.assertEqual(unauthorized.exception.code, 401)
            unauthorized.exception.close()

            request = Request(
                f"{origin}{control.PROGRAM_PATH}",
                headers={"Authorization": "Bearer api-secret"},
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)

            filler_body = json.dumps(self.filler_payload()).encode()
            unauthorized_filler = Request(
                f"{origin}{control.FILLER_PATH}filler-v2",
                method="PUT",
                data=filler_body,
                headers={"Idempotency-Key": "prepare-one"},
            )
            with self.assertRaises(HTTPError) as unauthorized:
                urlopen(unauthorized_filler)
            self.assertEqual(unauthorized.exception.code, 401)
            unauthorized.exception.close()

            prepare = Request(
                f"{origin}{control.FILLER_PATH}filler-v2",
                method="PUT",
                data=filler_body,
                headers={
                    "Authorization": "Bearer api-secret",
                    "Content-Type": "application/json",
                    "Idempotency-Key": "prepare-one",
                },
            )
            with urlopen(prepare) as response:
                prepared = json.loads(response.read())
            self.assertTrue(prepared["ready"])
            self.assertNotIn("downloadUrl", json.dumps(prepared))

            selection = self.destination_selection()
            reload_body = json.dumps(
                {"commandId": "reload-http", **selection}
            ).encode()
            reload_request = Request(
                f"{origin}{control.DESTINATION_PATH}{selection['version']}",
                method="PUT",
                data=reload_body,
                headers={
                    "Authorization": "Bearer api-secret",
                    "Content-Type": "application/json",
                    "Idempotency-Key": "reload-http",
                },
            )
            with urlopen(reload_request) as response:
                reloaded = json.loads(response.read())
            self.assertEqual(reloaded["destinationIds"], ["primary"])
            self.assertNotIn("secretId", json.dumps(reloaded))

            start_request = Request(
                f"{origin}{control.PROGRAM_PATH}/start",
                method="POST",
                data=json.dumps(selection).encode(),
                headers={
                    "Authorization": "Bearer api-secret",
                    "Content-Type": "application/json",
                    "Idempotency-Key": "start-http",
                    "X-Command-Sequence": "1",
                },
            )
            with urlopen(start_request) as response:
                started = json.loads(response.read())
            self.assertEqual(started["actualState"], "running")
            self.assertEqual(
                started["activeDestinations"]["destinationIds"], ["primary"]
            )
            self.assertNotIn("secretId", json.dumps(started))

            filler_status = Request(
                f"{origin}{control.FILLER_PATH}filler-v2",
                headers={"Authorization": "Bearer api-secret"},
            )
            with urlopen(filler_status) as response:
                self.assertTrue(json.loads(response.read())["ready"])

            with self.assertRaises(HTTPError) as unauthorized_metrics:
                urlopen(f"{origin}{control.METRICS_PATH}")
            self.assertEqual(unauthorized_metrics.exception.code, 401)
            unauthorized_metrics.exception.close()

            metrics_request = Request(
                f"{origin}{control.METRICS_PATH}",
                headers={"Authorization": "Bearer api-secret"},
            )
            with urlopen(metrics_request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.headers["Content-Type"],
                    "text/plain; version=0.0.4; charset=utf-8",
                )
                metrics = response.read().decode()
            self.assertIn("alana_service_info", metrics)
            self.assertIn("alana_lifecycle_state", metrics)
            self.assertNotIn("test-program", metrics)
            self.assertNotIn("api-secret", metrics)

            wrong = Request(
                f"{origin}/v1/programs/not-this-program/lifecycle",
                headers={"Authorization": "Bearer api-secret"},
            )
            with self.assertRaises(HTTPError) as not_found:
                urlopen(wrong)
            self.assertEqual(not_found.exception.code, 404)
            not_found.exception.close()

            wrong_filler = Request(
                f"{origin}/v1/programs/not-this-program/fillers/filler-v3",
                method="PUT",
                data=filler_body,
                headers={
                    "Authorization": "Bearer api-secret",
                    "Content-Type": "application/json",
                    "Idempotency-Key": "prepare-one",
                },
            )
            with self.assertRaises(HTTPError) as not_found:
                urlopen(wrong_filler)
            self.assertEqual(not_found.exception.code, 404)
            not_found.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CroccanteClientContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.temp.name) / "croccante-token"
        self.token_file.write_text("outbound-secret\n")
        self.requests = []
        requests = self.requests

        class DownstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def send_payload(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append(("PUT", self.path, dict(self.headers), payload))
                if "/destinations/" in self.path:
                    selection = {
                        "version": payload["version"],
                        "destinations": payload["destinations"],
                    }
                    metadata = control.destination_metadata(selection)
                    self.send_payload(
                        {
                            "version": metadata["version"],
                            "selectionHash": metadata["selectionHash"],
                            "destinationCount": metadata["count"],
                            "result": "validated",
                        }
                    )
                    return
                self.send_payload(
                    {
                        "version": "filler-v9",
                        "status": "ready",
                        "ready": True,
                        "sourceId": payload["source"]["id"],
                        "sourceSha256": payload["source"]["sha256"],
                        "artifactSha256": "b" * 64,
                        "profile": payload["profile"],
                        "preparedAt": "2026-08-24T00:00:00Z",
                    }
                )

            def do_GET(self):
                requests.append(("GET", self.path, dict(self.headers), None))
                metadata = control.destination_metadata(TEST_DESTINATION_SELECTION)
                self.send_payload(
                    {
                        "requestedState": "started",
                        "actualState": "started",
                        "destinationConfiguration": metadata,
                    }
                )

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                selection = json.loads(self.rfile.read(length)) if length else None
                requests.append(("POST", self.path, dict(self.headers), selection))
                version = self.headers.get("X-Filler-Version")
                metadata = control.destination_metadata(selection)
                self.send_payload(
                    {
                        "requestedState": "started",
                        "actualState": "started",
                        "sessionId": "safe-session",
                        "destinationConfiguration": metadata,
                        "destinations": [
                            {
                                "id": item["id"],
                                "mode": "waiting-for-publisher",
                                "supervisorHealthy": True,
                                "publisherProcessHealthy": False,
                            }
                            for item in selection["destinations"]
                        ],
                        "filler": {
                            "version": version,
                            "status": "ready",
                            "ready": True,
                        },
                    }
                )

        self.server = control.ThreadingHTTPServer(("127.0.0.1", 0), DownstreamHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        previous_url = os.environ.get("CROCCANTE_CONTROL_URL")
        previous_token = os.environ.get("CROCCANTE_CONTROL_TOKEN_FILE")
        self.previous_environment = (previous_url, previous_token)
        os.environ["CROCCANTE_CONTROL_URL"] = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["CROCCANTE_CONTROL_TOKEN_FILE"] = str(self.token_file)
        self.client = control.CroccanteClient()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        previous_url, previous_token = self.previous_environment
        if previous_url is None:
            os.environ.pop("CROCCANTE_CONTROL_URL", None)
        else:
            os.environ["CROCCANTE_CONTROL_URL"] = previous_url
        if previous_token is None:
            os.environ.pop("CROCCANTE_CONTROL_TOKEN_FILE", None)
        else:
            os.environ["CROCCANTE_CONTROL_TOKEN_FILE"] = previous_token
        self.temp.cleanup()

    def test_forwards_authenticated_preparation_and_exact_start_version(self):
        payload = LifecycleTests.filler_payload(command="prepare-nine")
        status, prepared = self.client.prepare("filler-v9", "prepare-nine", payload)
        self.assertEqual(status, 200)
        self.assertTrue(prepared["ready"])
        selection = json.loads(json.dumps(TEST_DESTINATION_SELECTION))
        acknowledgement = self.client.command(
            "start", "start-nine", 9, "filler-v9", selection
        )
        self.assertTrue(acknowledgement["accepted"])
        self.assertEqual(acknowledgement["fillerVersion"], "filler-v9")

        preparation = self.requests[0]
        self.assertEqual(preparation[0:2], ("PUT", "/v1/programs/test-program/fillers/filler-v9"))
        self.assertEqual(preparation[2]["Authorization"], "Bearer outbound-secret")
        self.assertEqual(preparation[2]["Idempotency-Key"], "prepare-nine")
        self.assertEqual(preparation[3], payload)
        start = self.requests[1]
        self.assertEqual(start[0:2], ("POST", "/v1/programs/test-program/session/start"))
        self.assertEqual(start[2]["X-Filler-Version"], "filler-v9")
        self.assertEqual(start[2]["X-Command-Sequence"], "9")
        self.assertEqual(start[3], selection)
        self.assertNotIn("downloadUrl", json.dumps(prepared))

    def test_forwards_stopped_reload_and_reads_redacted_status(self):
        selection = json.loads(json.dumps(TEST_DESTINATION_SELECTION))
        status, acknowledgement = self.client.reload_destinations(
            selection["version"], "reload-nine", selection
        )
        self.assertEqual(status, 200)
        self.assertTrue(acknowledgement["accepted"])
        request = self.requests[0]
        self.assertEqual(
            request[0:2],
            ("PUT", "/v1/programs/test-program/destinations/destinations-v1"),
        )
        self.assertEqual(request[2]["Authorization"], "Bearer outbound-secret")
        self.assertEqual(request[3], {"commandId": "reload-nine", **selection})

        status, state = self.client.status()
        self.assertEqual(status, 200)
        self.assertEqual(state["destinationConfiguration"]["count"], 1)
        self.assertNotIn("secretId", json.dumps(state))


if __name__ == "__main__":
    unittest.main()
