import importlib.util
import json
import os
import tempfile
import threading
import unittest
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


class FakePipeline:
    def __init__(self, events, *, running=False, ready=True):
        self.events = events
        self.is_running = running
        self.is_ready = ready

    def running(self):
        return self.is_running

    def start(self):
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
    def __init__(self, events, answers=None):
        self.events = events
        self.answers = list(answers or [])

    def command(self, action, key, sequence):
        self.events.append(f"croccante:{action}")
        if self.answers:
            return self.answers.pop(0)
        return {"accepted": True, "requestedState": f"{action}ed"}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = []
        self.store = control.StateStore(Path(self.temp.name))
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

    def tearDown(self):
        self.temp.cleanup()

    def test_start_waits_for_pipeline_before_croccante(self):
        status, body = self.manager.command("start", "start-one", 1)
        self.assertEqual(status, 200)
        self.assertEqual(self.events, ["pipeline:start", "croccante:start"])
        self.assertEqual(body["actualState"], "running")
        self.assertTrue(body["readiness"])

    def test_stop_acknowledgement_precedes_pipeline_teardown(self):
        self.manager.command("start", "start-one", 1)
        self.events.clear()
        status, body = self.manager.command("stop", "stop-one", 2)
        self.assertEqual(status, 200)
        self.assertEqual(self.events, ["croccante:stop", "pipeline:stop"])
        self.assertEqual(body["actualState"], "stopped")

    def test_duplicate_command_is_idempotent(self):
        first = self.manager.command("start", "same-key", 1)
        duplicate = self.manager.command("start", "same-key", 1)
        self.assertEqual(first[0], 200)
        self.assertEqual(duplicate[0], 200)
        self.assertTrue(duplicate[1]["commandResult"]["duplicate"])
        self.assertEqual(self.events.count("pipeline:start"), 1)
        self.assertEqual(self.events.count("croccante:start"), 1)

    def test_reused_key_for_different_command_is_rejected(self):
        self.manager.command("start", "same-key", 1)
        status, body = self.manager.command("stop", "same-key", 2)
        self.assertEqual(status, 409)
        self.assertIn("already used", body["error"])

    def test_old_sequence_is_rejected(self):
        self.manager.command("start", "one", 5)
        status, _ = self.manager.command("stop", "two", 4)
        self.assertEqual(status, 409)

    def test_failed_stop_keeps_pipeline_and_retries_before_teardown(self):
        self.manager.command("start", "start", 1)
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
        self.manager.command("start", "start", 1)
        self.events.clear()
        self.pipeline.is_running = False
        self.manager.reconcile_once()
        self.assertEqual(self.events, ["pipeline:start"])
        self.assertEqual(self.manager.view()["actualState"], "running")

    def test_restart_recovers_persisted_running_request(self):
        self.manager.command("start", "start", 1)
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
        self.manager.command("start", "start", 1)
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
        self.manager.command("start", secret, 1)
        persisted = "".join(
            path.read_text() for path in Path(self.temp.name).rglob("*.json")
        )
        self.assertNotIn(secret, persisted)
        self.assertNotIn(secret, json.dumps(self.manager.view()))

    def test_public_state_redacts_pending_downstream_key(self):
        self.croccante.answers = [{"accepted": False, "error": "unavailable"}]
        self.manager.command("start", "start-secret", 1)
        pending = self.manager.view()["pendingCroccante"]
        self.assertEqual(pending, {"action": "start", "sequence": 1})

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

            wrong = Request(
                f"{origin}/v1/programs/not-this-program/lifecycle",
                headers={"Authorization": "Bearer api-secret"},
            )
            with self.assertRaises(HTTPError) as not_found:
                urlopen(wrong)
            self.assertEqual(not_found.exception.code, 404)
            not_found.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
