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

from alana_metrics import Metrics


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
FILLER_PATH = f"/v1/programs/{quote(PROGRAM_ID, safe='')}/fillers/"
METRICS_PATH = "/metrics"
METRICS = Metrics()
FILLER_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
FILLER_FAILURE_REASONS = frozenset(
    {
        "checksum-mismatch",
        "download-failed",
        "download-too-large",
        "invalid-download",
        "invalid-profile",
        "invalid-source",
        "invalid-version",
        "missing-video",
        "probe-failed",
        "profile-mismatch",
        "transcode-failed",
        "version-conflict",
    }
)


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
            "activeFiller": None,
            "pendingFiller": None,
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

    def _token(self, operation: str, started: float) -> tuple[str, dict[str, object] | None]:
        token = read_text(self.token_file)
        if token:
            return token, None
        METRICS.observe_dependency(operation, "token_unavailable", time.monotonic() - started)
        return "", {"accepted": False, "error": "control-token-unavailable"}

    @staticmethod
    def _failure(exc: HTTPError, operation: str, started: float) -> dict[str, object]:
        reason = ""
        try:
            payload = json.loads(exc.read())
            candidate = payload.get("reason") if isinstance(payload, dict) else None
            if candidate in FILLER_FAILURE_REASONS:
                reason = str(candidate)
        except (json.JSONDecodeError, OSError):
            pass
        METRICS.observe_dependency(operation, "http_error", time.monotonic() - started)
        return {
            "accepted": False,
            "error": f"http-{exc.code}",
            **({"reason": reason} if reason else {}),
        }

    def command(
        self,
        action: str,
        key: str,
        sequence: int,
        filler_version: str | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()

        def finish(payload: dict[str, object], result: str) -> dict[str, object]:
            METRICS.observe_dependency(action, result, time.monotonic() - started)
            return payload

        token, failure = self._token(action, started)
        if failure:
            return failure
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
                **({"X-Filler-Version": filler_version} if action == "start" and filler_version else {}),
            },
        )
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            return finish({"accepted": False, "error": f"http-{exc.code}"}, "http_error")
        except json.JSONDecodeError:
            return finish({"accepted": False, "error": "unavailable-or-invalid-response"}, "invalid_response")
        except (URLError, TimeoutError, OSError):
            return finish({"accepted": False, "error": "unavailable-or-invalid-response"}, "unavailable")

        expected = "started" if action == "start" else "stopped"
        filler = payload.get("filler") if isinstance(payload.get("filler"), dict) else None
        filler_matches = action != "start" or bool(
            filler_version
            and filler
            and filler.get("version") == filler_version
            and filler.get("ready") is True
        )
        accepted = payload.get("requestedState") == expected and filler_matches
        return finish({
            "accepted": accepted,
            "requestedState": payload.get("requestedState"),
            "actualState": payload.get("actualState"),
            "sessionId": payload.get("sessionId"),
            **({"fillerVersion": filler_version} if accepted and action == "start" else {}),
            **({} if accepted else {"error": "unexpected-state"}),
        }, "success" if accepted else "unexpected_state")

    def prepare(
        self,
        version: str,
        key: str,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        started = time.monotonic()
        token, failure = self._token("prepare", started)
        if failure:
            return 503, failure
        request = Request(
            f"{self.base_url}/v1/programs/{quote(PROGRAM_ID, safe='')}/fillers/{quote(version, safe='')}",
            method="PUT",
            data=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
        )
        try:
            with urlopen(request, timeout=120) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            failure = self._failure(exc, "prepare", started)
            return exc.code, {
                "version": version,
                "status": "failed",
                "ready": False,
                **failure,
            }
        except json.JSONDecodeError:
            METRICS.observe_dependency("prepare", "invalid_response", time.monotonic() - started)
            return 502, {"accepted": False, "error": "unavailable-or-invalid-response"}
        except (URLError, TimeoutError, OSError):
            METRICS.observe_dependency("prepare", "unavailable", time.monotonic() - started)
            return 502, {"accepted": False, "error": "unavailable-or-invalid-response"}
        public = self._public_filler(result, version)
        ready = isinstance(result, dict) and result.get("version") == version and public.get("ready") is True
        METRICS.observe_dependency(
            "prepare",
            "success" if ready else "unexpected_state",
            time.monotonic() - started,
        )
        return (200 if ready else 502), public

    def filler_status(self, version: str) -> tuple[int, dict[str, object]]:
        started = time.monotonic()
        token, failure = self._token("filler_status", started)
        if failure:
            return 503, failure
        request = Request(
            f"{self.base_url}/v1/programs/{quote(PROGRAM_ID, safe='')}/fillers/{quote(version, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            failure = self._failure(exc, "filler_status", started)
            if exc.code == 404:
                return 404, {
                    "version": version,
                    "status": "unprepared",
                    "ready": False,
                }
            return exc.code, {
                "version": version,
                "status": "failed",
                "ready": False,
                **failure,
            }
        except json.JSONDecodeError:
            METRICS.observe_dependency("filler_status", "invalid_response", time.monotonic() - started)
            return 502, {"accepted": False, "error": "unavailable-or-invalid-response"}
        except (URLError, TimeoutError, OSError):
            METRICS.observe_dependency("filler_status", "unavailable", time.monotonic() - started)
            return 502, {"accepted": False, "error": "unavailable-or-invalid-response"}
        public = self._public_filler(result, version)
        ready = isinstance(result, dict) and result.get("version") == version and public.get("ready") is True
        METRICS.observe_dependency(
            "filler_status",
            "success" if ready else "unexpected_state",
            time.monotonic() - started,
        )
        return (200 if ready else 404), public

    @staticmethod
    def _public_filler(payload: object, version: str) -> dict[str, object]:
        source = payload if isinstance(payload, dict) else {}
        source_id = source.get("sourceId")
        source_sha = source.get("sourceSha256")
        artifact_sha = source.get("artifactSha256")
        profile = source.get("profile")
        profile_keys = {
            "width",
            "height",
            "fps",
            "videoBitrate",
            "audioRate",
            "audioChannels",
            "audioBitrate",
            "gop",
            "loopSeconds",
        }
        safe_profile = (
            {key: profile[key] for key in profile_keys}
            if isinstance(profile, dict)
            and set(profile) == profile_keys
            and all(isinstance(profile[key], (int, str)) for key in profile_keys)
            else None
        )
        ready_fields_valid = bool(
            isinstance(source_id, str)
            and FILLER_IDENTIFIER.fullmatch(source_id)
            and isinstance(source_sha, str)
            and re.fullmatch(r"[0-9a-f]{64}", source_sha)
            and isinstance(artifact_sha, str)
            and re.fullmatch(r"[0-9a-f]{64}", artifact_sha)
            and safe_profile is not None
        )
        ready = source.get("ready") is True and ready_fields_valid
        result: dict[str, object] = {
            "version": version,
            "status": (
                "ready"
                if ready
                else source.get("status")
                if source.get("status") in {"failed", "unprepared"}
                else "failed"
            ),
            "ready": ready,
        }
        if ready:
            result.update(
                sourceId=source_id,
                sourceSha256=source_sha,
                artifactSha256=artifact_sha,
                profile=safe_profile,
            )
            prepared_at = source.get("preparedAt")
            if isinstance(prepared_at, str) and len(prepared_at) <= 64:
                result["preparedAt"] = prepared_at
        reason = source.get("reason")
        if reason in FILLER_FAILURE_REASONS:
            result["reason"] = reason
        error = source.get("error")
        if error in {
            "control-token-unavailable",
            "unavailable-or-invalid-response",
            "unexpected-state",
        } or (isinstance(error, str) and re.fullmatch(r"http-[0-9]{3}", error)):
            result["error"] = error
        if source.get("ready") is True and not ready_fields_valid:
            result["error"] = "unexpected-state"
        return result


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
        self.preparation_lock = threading.Lock()
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
                **(
                    {"fillerVersion": pending.get("fillerVersion")}
                    if pending.get("action") == "start" and pending.get("fillerVersion")
                    else {}
                ),
            }
        public["activeFiller"] = self._public_filler(self.state.get("activeFiller"))
        public["pendingFiller"] = self._public_filler(self.state.get("pendingFiller"))
        return {**public, **self.pipeline.status()}

    @staticmethod
    def _public_filler(value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        allowed = {
            "version",
            "status",
            "ready",
            "sourceId",
            "sourceSha256",
            "artifactSha256",
            "profile",
            "preparedAt",
            "reason",
            "error",
        }
        return {key: item for key, item in value.items() if key in allowed}

    @staticmethod
    def _filler_digest(payload: dict[str, object]) -> str:
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        semantic = {
            "commandId": payload.get("commandId"),
            "source": {
                "id": source.get("id"),
                "sha256": source.get("sha256"),
            },
            "profile": payload.get("profile"),
        }
        return hashlib.sha256(
            json.dumps(semantic, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

    def prepare_filler(
        self,
        version: str,
        key: str,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        digest = self._filler_digest(payload)
        with self.preparation_lock:
            with self.lock:
                for slot in ("activeFiller", "pendingFiller"):
                    existing = self.state.get(slot)
                    if not isinstance(existing, dict) or existing.get("version") != version:
                        continue
                    if existing.get("configurationDigest") != digest:
                        METRICS.observe_preparation("conflict")
                        return 409, {
                            "version": version,
                            "status": "failed",
                            "ready": False,
                            "reason": "version-conflict",
                        }
                    if existing.get("ready") is True:
                        METRICS.observe_preparation("duplicate")
                        return 200, {**(self._public_filler(existing) or {}), "duplicate": True}
                self.state["pendingFiller"] = {
                    "version": version,
                    "status": "preparing",
                    "ready": False,
                    "configurationDigest": digest,
                }
                self._save()

            status, result = self.croccante.prepare(version, key, payload)
            with self.lock:
                filler = {
                    **result,
                    "version": version,
                    "configurationDigest": digest,
                }
                self.state["pendingFiller"] = filler
                self._save()
                if status < 300 and filler.get("ready") is True:
                    outcome = "success"
                elif status == 409:
                    outcome = "conflict"
                elif status in (502, 503):
                    outcome = "unavailable"
                else:
                    outcome = "failure"
                METRICS.observe_preparation(outcome)
                return status, self._public_filler(filler) or {}

    def filler_status(self, version: str) -> tuple[int, dict[str, object]]:
        status, result = self.croccante.filler_status(version)
        with self.lock:
            for slot in ("activeFiller", "pendingFiller"):
                existing = self.state.get(slot)
                if isinstance(existing, dict) and existing.get("version") == version:
                    self.state[slot] = {
                        **result,
                        **(
                            {"configurationDigest": existing["configurationDigest"]}
                            if existing.get("configurationDigest")
                            else {}
                        ),
                    }
            self._save()
        return status, self._public_filler(result) or {}

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

    def _acknowledge(
        self,
        action: str,
        key: str,
        sequence: int,
        filler_version: str | None = None,
    ) -> dict[str, object]:
        return self.croccante.command(action, key, sequence, filler_version)

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

        pending_filler = self.state.get("pendingFiller")
        if not isinstance(pending_filler, dict) or pending_filler.get("ready") is not True:
            return self._finish_command(key, "start", sequence, "filler-not-ready", 409)
        filler_version = str(pending_filler.get("version") or "")
        if not FILLER_IDENTIFIER.fullmatch(filler_version):
            return self._finish_command(key, "start", sequence, "filler-not-ready", 409)

        self._set_transition("running", "transitioning", "starting")
        downstream_key = self._downstream_key(key)
        self.state["pendingCroccante"] = {
            "action": "start",
            "key": downstream_key,
            "sequence": sequence,
            "fillerVersion": filler_version,
        }
        self._save()
        self.pipeline.start()
        if not self._wait_ready():
            self.state.update(actualState="failed", transition=None, readiness=False)
            return self._finish_command(key, "start", sequence, "pipeline-not-ready", 503)

        self.state["readiness"] = True
        acknowledgement = self._acknowledge(
            "start", downstream_key, sequence, filler_version
        )
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
            activeFiller=pending_filler,
            pendingFiller=None,
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
        active_filler = self.state.get("activeFiller")
        pending_filler = self.state.get("pendingFiller")
        timestamps = dict(self.state.get("timestamps") or {})
        timestamps["stoppedAt"] = now()
        self.state.update(
            actualState="stopped",
            transition=None,
            readiness=False,
            activeProgram=None,
            pendingCroccante=None,
            activeFiller=None,
            pendingFiller=(
                pending_filler
                if isinstance(pending_filler, dict)
                else active_filler
                if isinstance(active_filler, dict)
                else None
            ),
            timestamps=timestamps,
        )
        return self._finish_command(key, "stop", sequence, "stopped", 200)

    def reconcile_once(self) -> None:
        with self.lock:
            pending_filler = self.state.get("pendingFiller")
            if isinstance(pending_filler, dict) and pending_filler.get("ready") is not True:
                version = str(pending_filler.get("version") or "")
                if FILLER_IDENTIFIER.fullmatch(version):
                    status, result = self.croccante.filler_status(version)
                    self.state["pendingFiller"] = {
                        **result,
                        **(
                            {"configurationDigest": pending_filler["configurationDigest"]}
                            if pending_filler.get("configurationDigest")
                            else {}
                        ),
                    }
                    METRICS.observe_preparation(
                        "reconciled" if status < 300 and result.get("ready") is True else "not_ready"
                    )
                    self._save()
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
                    filler_version = str(pending.get("fillerVersion") or "")
                    ack = self._acknowledge(
                        "start",
                        str(pending["key"]),
                        int(pending["sequence"]),
                        filler_version,
                    )
                    self.state["croccanteAcknowledgement"] = ack
                    if ack.get("accepted"):
                        prepared = self.state.get("pendingFiller")
                        self.state.update(
                            actualState="running",
                            pendingCroccante=None,
                            transition=None,
                            activeFiller=(
                                prepared
                                if isinstance(prepared, dict)
                                and prepared.get("version") == filler_version
                                else self.state.get("activeFiller")
                            ),
                            pendingFiller=(
                                None
                                if isinstance(prepared, dict)
                                and prepared.get("version") == filler_version
                                else prepared
                            ),
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
            active_filler = self.state.get("activeFiller")
            pending_filler = self.state.get("pendingFiller")
            timestamps = dict(self.state.get("timestamps") or {})
            timestamps["stoppedAt"] = now()
            self.state.update(
                actualState="stopped",
                transition=None,
                readiness=False,
                activeProgram=None,
                pendingCroccante=None,
                activeFiller=None,
                pendingFiller=(
                    pending_filler
                    if isinstance(pending_filler, dict)
                    else active_filler
                    if isinstance(active_filler, dict)
                    else None
                ),
                timestamps=timestamps,
            )
            self._save()

    def _monitor(self) -> None:
        while not self.closed.wait(CONTROL_RETRY_SECONDS):
            try:
                self.reconcile_once()
                METRICS.observe_reconcile("success")
            except Exception as exc:
                METRICS.observe_reconcile("failure")
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
        route = (
            "metrics"
            if path == METRICS_PATH
            else "filler"
            if "/fillers/" in path
            else "lifecycle"
            if path.startswith("/v1/programs/")
            else "unknown"
        )
        print(f"[control] {self.command} route={route} status={status}", flush=True)

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.response_status = status
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_metrics(self, payload: str) -> None:
        body = payload.encode()
        self.send_response(200)
        self.response_status = 200
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorize(self) -> bool:
        expected = read_text(CONTROL_TOKEN_FILE)
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            self.send_json(401, {"error": "unauthorized"})
            return False
        return True

    def authorize_and_scope(self) -> bool:
        if not self.authorize():
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
        started = time.monotonic()
        self.response_status = 500
        path = urlsplit(self.path).path
        route = "metrics" if path == METRICS_PATH else "filler" if "/fillers/" in path else "lifecycle" if path.startswith("/v1/programs/") else "unknown"
        try:
            if path == METRICS_PATH:
                if self.authorize():
                    self.send_metrics(METRICS.render(manager().view()))
                return
            if not self.authorize_and_scope():
                return
            if path.startswith(FILLER_PATH):
                version = unquote(path[len(FILLER_PATH) :])
                if not FILLER_IDENTIFIER.fullmatch(version):
                    self.send_json(404, {"error": "not found"})
                    return
                status, payload = manager().filler_status(version)
                self.send_json(status, payload)
                return
            if path != PROGRAM_PATH:
                self.send_json(404, {"error": "not found"})
                return
            self.send_json(200, manager().view())
        finally:
            METRICS.observe_http("GET", route, self.response_status, time.monotonic() - started)

    def do_PUT(self) -> None:  # noqa: N802
        started = time.monotonic()
        self.response_status = 500
        path = urlsplit(self.path).path
        route = "filler" if "/fillers/" in path else "unknown"
        try:
            if not self.authorize_and_scope():
                return
            if not path.startswith(FILLER_PATH):
                self.send_json(404, {"error": "not found"})
                return
            version = unquote(path[len(FILLER_PATH) :])
            if not FILLER_IDENTIFIER.fullmatch(version):
                self.send_json(404, {"error": "not found"})
                return
            key = self.headers.get("Idempotency-Key", "").strip()
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not key or len(key) > 200 or length < 2 or length > 65536:
                self.send_json(400, {"error": "bounded body and Idempotency-Key are required"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or payload.get("commandId") != key:
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                self.send_json(400, {"error": "invalid preparation request"})
                return
            status, result = manager().prepare_filler(version, key, payload)
            self.send_json(status, result)
        finally:
            METRICS.observe_http("PUT", route, self.response_status, time.monotonic() - started)

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        self.response_status = 500
        path = urlsplit(self.path).path
        route = "lifecycle" if path.startswith("/v1/programs/") else "unknown"
        try:
            if not self.authorize_and_scope():
                return
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
            action = path.rsplit("/", 1)[1]
            status, payload = manager().command(action, key, sequence)
            command_result = "success" if status < 300 else "conflict" if status == 409 else "dependency_failure" if status == 502 else "not_ready" if status == 503 else "failure"
            METRICS.observe_command(action, command_result)
            self.send_json(status, payload)
        finally:
            METRICS.observe_http("POST", route, self.response_status, time.monotonic() - started)


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
