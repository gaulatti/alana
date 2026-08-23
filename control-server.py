#!/usr/bin/env python3
"""Authenticated lifecycle authority for one Alana program runtime."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen


PROGRAM_ID = os.environ.get("PROGRAM_ID", "")
STATE_DIR = Path(os.environ.get("ALANA_STATE_DIR", "/var/lib/alana"))
CONTROL_TOKEN_FILE = Path(
    os.environ.get("ALANA_CONTROL_TOKEN_FILE", "/run/secrets/alana-control-token")
)
CONTROL_BIND = os.environ.get("ALANA_CONTROL_BIND", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("ALANA_CONTROL_PORT", "8080"))
PIPELINE_READY_TIMEOUT = int(os.environ.get("PIPELINE_READY_TIMEOUT", "90"))
CONTROL_RETRY_SECONDS = int(os.environ.get("CONTROL_RETRY_SECONDS", "5"))
PROGRAM_PATH = f"/v1/programs/{quote(PROGRAM_ID, safe='')}/lifecycle"


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def pid_alive(path: Path) -> bool:
    try:
        os.kill(int(read_text(path)), 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def interrupt_for_shutdown(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


class StateStore:
    def __init__(self, root: Path = STATE_DIR):
        self.root = root
        self.state_file = root / "lifecycle.json"
        self.command_dir = root / "commands"
        self.root.mkdir(parents=True, exist_ok=True)
        self.command_dir.mkdir(exist_ok=True)

    def initial(self) -> dict[str, object]:
        stamp = now()
        return {
            "programId": PROGRAM_ID,
            "requestedState": "stopped",
            "actualState": "stopped",
            "transition": None,
            "readiness": False,
            "activeProgram": None,
            "lastSequence": 0,
            "lastCommand": None,
            "croccanteAcknowledgement": None,
            "pendingCroccante": None,
            "timestamps": {
                "requestedAt": stamp,
                "transitionStartedAt": None,
                "runningAt": None,
                "stoppedAt": stamp,
                "updatedAt": stamp,
            },
        }

    def load(self) -> dict[str, object]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            state = self.initial()
            self.save(state)
            return state

    def save(self, state: dict[str, object]) -> None:
        timestamps = dict(state.get("timestamps") or {})
        timestamps["updatedAt"] = now()
        state["timestamps"] = timestamps
        atomic_json(self.state_file, state)

    @staticmethod
    def command_digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def command_path(self, key: str) -> Path:
        return self.command_dir / f"{self.command_digest(key)}.json"

    def prior_command(self, key: str) -> dict[str, object] | None:
        try:
            return json.loads(self.command_path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None

    def record_command(self, key: str, record: dict[str, object]) -> None:
        atomic_json(self.command_path(key), record)


class SubprocessPipeline:
    def __init__(self):
        self.process: subprocess.Popen[bytes] | None = None
        outputs = os.environ.get("RTMP_OUTPUTS", "")
        self.expected_rtmp = len([value for value in re.split(r"[,\s]+", outputs) if value]) or 1
        self.livekit_required = os.environ.get("LIVEKIT_ENABLED", "0") == "1"

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running():
            return
        self.process = subprocess.Popen(
            ["/usr/local/bin/startup.sh"],
            start_new_session=True,
        )

    @staticmethod
    def _progress_file(path: Path) -> bool:
        progress = read_text(path)
        return any(
            line.startswith("frame=") and line.split("=", 1)[1].strip() not in ("", "0")
            for line in progress.splitlines()
        )

    @classmethod
    def _progressing(cls, index: int) -> bool:
        return cls._progress_file(Path(f"/tmp/rtmp-progress-{index}.txt"))

    def ready(self) -> bool:
        if not self.running() or not pid_alive(Path("/tmp/channel-browser.pid")):
            return False
        for index in range(1, self.expected_rtmp + 1):
            if not pid_alive(Path(f"/tmp/rtmp-{index}.pid")) or not self._progressing(index):
                return False
        if self.livekit_required:
            if not pid_alive(Path("/tmp/livekit.pid")) or not self._progress_file(
                Path("/tmp/livekit-progress.txt")
            ):
                return False
        return True

    def status(self) -> dict[str, object]:
        return {
            "pipelineProcessHealthy": self.running(),
            "browserHealthy": pid_alive(Path("/tmp/channel-browser.pid")),
            "rtmpHealth": [
                {
                    "index": index,
                    "processHealthy": pid_alive(Path(f"/tmp/rtmp-{index}.pid")),
                    "progressing": self._progressing(index),
                }
                for index in range(1, self.expected_rtmp + 1)
            ],
            "livekitEnabled": self.livekit_required,
            "livekitHealthy": (
                pid_alive(Path("/tmp/livekit.pid"))
                and self._progress_file(Path("/tmp/livekit-progress.txt"))
                if self.livekit_required
                else None
            ),
        }

    def stop(self) -> None:
        if not self.running() or self.process is None:
            self.process = None
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=15)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
        finally:
            self.process = None


class CroccanteClient:
    def __init__(self):
        self.base_url = os.environ["CROCCANTE_CONTROL_URL"].rstrip("/")
        self.token_file = Path(
            os.environ.get(
                "CROCCANTE_CONTROL_TOKEN_FILE",
                "/run/secrets/croccante-control-token",
            )
        )

    def command(self, action: str, key: str, sequence: int) -> dict[str, object]:
        token = read_text(self.token_file)
        if not token:
            return {"accepted": False, "error": "control-token-unavailable"}
        url = (
            f"{self.base_url}/v1/programs/{quote(PROGRAM_ID, safe='')}/session/{action}"
        )
        request = Request(
            url,
            method="POST",
            data=b"",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key,
                "X-Command-Sequence": str(sequence),
            },
        )
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            return {"accepted": False, "error": f"http-{exc.code}"}
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            return {"accepted": False, "error": "unavailable-or-invalid-response"}

        expected = "started" if action == "start" else "stopped"
        accepted = payload.get("requestedState") == expected
        return {
            "accepted": accepted,
            "requestedState": payload.get("requestedState"),
            "actualState": payload.get("actualState"),
            "sessionId": payload.get("sessionId"),
            **({} if accepted else {"error": "unexpected-state"}),
        }


class LifecycleManager:
    def __init__(
        self,
        store: StateStore,
        pipeline: SubprocessPipeline,
        croccante: CroccanteClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        ready_timeout: int = PIPELINE_READY_TIMEOUT,
        start_monitor: bool = True,
    ):
        self.store = store
        self.pipeline = pipeline
        self.croccante = croccante
        self.sleep = sleep
        self.monotonic = monotonic
        self.ready_timeout = ready_timeout
        self.lock = threading.RLock()
        self.state = store.load()
        self.closed = threading.Event()
        if start_monitor:
            threading.Thread(target=self._monitor, daemon=True).start()

    def _save(self) -> None:
        self.store.save(self.state)

    def view(self) -> dict[str, object]:
        # Reads remain available while a serialized Start/Stop is waiting on
        # real publisher readiness or a downstream acknowledgement.
        public = {**self.state}
        pending = public.get("pendingCroccante")
        if isinstance(pending, dict):
            public["pendingCroccante"] = {
                "action": pending.get("action"),
                "sequence": pending.get("sequence"),
            }
        return {**public, **self.pipeline.status()}

    def _set_transition(self, requested: str, actual: str, transition: str) -> None:
        timestamps = dict(self.state.get("timestamps") or {})
        timestamps["requestedAt"] = now()
        timestamps["transitionStartedAt"] = now()
        self.state.update(
            requestedState=requested,
            actualState=actual,
            transition=transition,
            readiness=False,
            activeProgram=PROGRAM_ID if requested == "running" else self.state.get("activeProgram"),
            timestamps=timestamps,
        )
        self._save()

    @staticmethod
    def _downstream_key(key: str) -> str:
        return f"alana-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:40]}"

    def _wait_ready(self) -> bool:
        deadline = self.monotonic() + self.ready_timeout
        while self.monotonic() < deadline:
            if not self.pipeline.running():
                return False
            if self.pipeline.ready():
                return True
            self.sleep(0.2)
        return False

    def _acknowledge(self, action: str, key: str, sequence: int) -> dict[str, object]:
        return self.croccante.command(action, key, sequence)

    def _finish_command(
        self,
        original_key: str,
        action: str,
        sequence: int,
        result: str,
        status: int,
    ) -> tuple[int, dict[str, object]]:
        record = {
            "id": self.store.command_digest(original_key)[:16],
            "sequence": sequence,
            "action": action,
            "result": result,
            "acceptedAt": now(),
            "status": status,
        }
        self.state["lastSequence"] = sequence
        self.state["lastCommand"] = record
        self.store.record_command(original_key, record)
        self._save()
        return status, {**self.view(), "commandResult": record}

    def command(
        self, action: str, key: str, sequence: int
    ) -> tuple[int, dict[str, object]]:
        with self.lock:
            prior = self.store.prior_command(key)
            if prior:
                if prior.get("action") != action or int(prior["sequence"]) != sequence:
                    return 409, {
                        **self.view(),
                        "error": "idempotency key was already used for another command",
                    }
                return int(prior["status"]), {
                    **self.view(),
                    "commandResult": {**prior, "duplicate": True},
                }

            if sequence <= int(self.state.get("lastSequence") or 0):
                return 409, {
                    **self.view(),
                    "error": "command sequence is not newer than the last accepted command",
                    "lastAcceptedSequence": self.state.get("lastSequence"),
                }
            if self.state.get("transition") in ("starting", "stopping"):
                return 409, {**self.view(), "error": "a lifecycle transition is already active"}

            if action == "start":
                return self._start(key, sequence)
            return self._stop(key, sequence)

    def _start(self, key: str, sequence: int) -> tuple[int, dict[str, object]]:
        if (
            self.state.get("requestedState") == "running"
            and self.state.get("actualState") == "running"
        ):
            return self._finish_command(key, "start", sequence, "already-running", 200)

        self._set_transition("running", "transitioning", "starting")
        downstream_key = self._downstream_key(key)
        self.state["pendingCroccante"] = {
            "action": "start",
            "key": downstream_key,
            "sequence": sequence,
        }
        self._save()
        self.pipeline.start()
        if not self._wait_ready():
            self.state.update(actualState="failed", transition=None, readiness=False)
            return self._finish_command(key, "start", sequence, "pipeline-not-ready", 503)

        self.state["readiness"] = True
        acknowledgement = self._acknowledge("start", downstream_key, sequence)
        self.state["croccanteAcknowledgement"] = acknowledgement
        if not acknowledgement.get("accepted"):
            self.state.update(actualState="degraded", transition=None)
            return self._finish_command(
                key, "start", sequence, "croccante-start-unacknowledged", 502
            )

        timestamps = dict(self.state.get("timestamps") or {})
        timestamps["runningAt"] = now()
        self.state.update(
            actualState="running",
            transition=None,
            activeProgram=PROGRAM_ID,
            pendingCroccante=None,
            timestamps=timestamps,
        )
        return self._finish_command(key, "start", sequence, "running", 200)

    def _stop(self, key: str, sequence: int) -> tuple[int, dict[str, object]]:
        if (
            self.state.get("requestedState") == "stopped"
            and self.state.get("actualState") == "stopped"
            and not self.pipeline.running()
        ):
            return self._finish_command(key, "stop", sequence, "already-stopped", 200)

        self._set_transition("stopped", "stopping", "stopping")
        downstream_key = self._downstream_key(key)
        pending = {"action": "stop", "key": downstream_key, "sequence": sequence}
        self.state["pendingCroccante"] = pending
        self._save()
        acknowledgement = self._acknowledge("stop", downstream_key, sequence)
        self.state["croccanteAcknowledgement"] = acknowledgement
        if not acknowledgement.get("accepted"):
            self.state.update(actualState="degraded", transition=None, readiness=self.pipeline.ready())
            return self._finish_command(
                key, "stop", sequence, "croccante-stop-unacknowledged", 502
            )

        self.pipeline.stop()
        timestamps = dict(self.state.get("timestamps") or {})
        timestamps["stoppedAt"] = now()
        self.state.update(
            actualState="stopped",
            transition=None,
            readiness=False,
            activeProgram=None,
            pendingCroccante=None,
            timestamps=timestamps,
        )
        return self._finish_command(key, "stop", sequence, "stopped", 200)

    def reconcile_once(self) -> None:
        with self.lock:
            requested = self.state.get("requestedState")
            pending = self.state.get("pendingCroccante")
            if requested == "running":
                if not self.pipeline.running():
                    self.state.update(actualState="degraded", readiness=False)
                    self._save()
                    self.pipeline.start()
                    if not self._wait_ready():
                        self.state.update(actualState="failed", readiness=False)
                        self._save()
                        return
                ready = self.pipeline.ready()
                self.state["readiness"] = ready
                if not ready:
                    self.state["actualState"] = "degraded"
                    self._save()
                    return
                if isinstance(pending, dict) and pending.get("action") == "start":
                    ack = self._acknowledge(
                        "start", str(pending["key"]), int(pending["sequence"])
                    )
                    self.state["croccanteAcknowledgement"] = ack
                    if ack.get("accepted"):
                        self.state.update(
                            actualState="running", pendingCroccante=None, transition=None
                        )
                elif self.state.get("actualState") == "degraded":
                    self.state["actualState"] = "running"
                self._save()
                return

            if isinstance(pending, dict) and pending.get("action") == "stop":
                ack = self._acknowledge(
                    "stop", str(pending["key"]), int(pending["sequence"])
                )
                self.state["croccanteAcknowledgement"] = ack
                if not ack.get("accepted"):
                    self.state.update(
                        actualState="degraded", readiness=self.pipeline.ready()
                    )
                    self._save()
                    return
            if self.pipeline.running():
                self.pipeline.stop()
            timestamps = dict(self.state.get("timestamps") or {})
            timestamps["stoppedAt"] = now()
            self.state.update(
                actualState="stopped",
                transition=None,
                readiness=False,
                activeProgram=None,
                pendingCroccante=None,
                timestamps=timestamps,
            )
            self._save()

    def _monitor(self) -> None:
        while not self.closed.wait(CONTROL_RETRY_SECONDS):
            try:
                self.reconcile_once()
            except Exception as exc:
                # Exception types are safe to log; messages may contain URLs.
                print(f"[lifecycle] reconcile failed type={type(exc).__name__}", flush=True)


MANAGER: LifecycleManager | None = None


def manager() -> LifecycleManager:
    if MANAGER is None:
        raise RuntimeError("lifecycle manager is not initialized")
    return MANAGER


class Handler(BaseHTTPRequestHandler):
    server_version = "alana-control"
    sys_version = ""

    def log_message(self, format_string: str, *args: object) -> None:
        status = args[1] if len(args) > 1 else "-"
        path = urlsplit(self.path).path
        route = "lifecycle" if path.startswith("/v1/programs/") else "unknown"
        print(f"[control] {self.command} route={route} status={status}", flush=True)

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorize_and_scope(self) -> bool:
        expected = read_text(CONTROL_TOKEN_FILE)
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            self.send_json(401, {"error": "unauthorized"})
            return False
        path = urlsplit(self.path).path
        prefix = "/v1/programs/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "not found"})
            return False
        if unquote(path[len(prefix) :].split("/", 1)[0]) != PROGRAM_ID:
            self.send_json(404, {"error": "program not found"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self.authorize_and_scope():
            return
        if urlsplit(self.path).path != PROGRAM_PATH:
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(200, manager().view())

    def do_POST(self) -> None:  # noqa: N802
        if not self.authorize_and_scope():
            return
        path = urlsplit(self.path).path
        if path not in (f"{PROGRAM_PATH}/start", f"{PROGRAM_PATH}/stop"):
            self.send_json(404, {"error": "not found"})
            return
        key = self.headers.get("Idempotency-Key", "").strip()
        sequence_text = self.headers.get("X-Command-Sequence", "").strip()
        if not key or len(key) > 200:
            self.send_json(400, {"error": "a bounded Idempotency-Key is required"})
            return
        try:
            sequence = int(sequence_text)
            if sequence < 1:
                raise ValueError
        except ValueError:
            self.send_json(400, {"error": "X-Command-Sequence must be a positive integer"})
            return
        status, payload = manager().command(path.rsplit("/", 1)[1], key, sequence)
        self.send_json(status, payload)


def main() -> None:
    global MANAGER
    subprocess.run(["/usr/local/bin/validate-config.sh"], check=True)
    MANAGER = LifecycleManager(StateStore(), SubprocessPipeline(), CroccanteClient())
    server = ThreadingHTTPServer((CONTROL_BIND, CONTROL_PORT), Handler)
    signal.signal(signal.SIGTERM, interrupt_for_shutdown)
    print(
        f"[control] listening on {CONTROL_BIND}:{CONTROL_PORT} for program={PROGRAM_ID}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager().closed.set()
        manager().pipeline.stop()
        server.server_close()


if __name__ == "__main__":
    main()
