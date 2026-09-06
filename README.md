# Alana

Alana is Alcantara's headless Program renderer and publisher. One runtime owns
one configured Alcantara Program and publishes it through:

- one or more required RTMP outputs; and
- an optional concurrent low-latency LiveKit Program feed.

Radio is intentionally absent. Palazzo owns radio playback and Icecast output.

Alana is lifecycle-controlled: it does not render or publish until an
authenticated Start command is accepted. It acknowledges Start only after the
local pipeline is ready and Croccante has acknowledged its matching session;
Stop is acknowledged by Croccante before Alana tears down local output.
Alcantara separately prepares the next television filler through Alana before
Start. Alana relays the exact immutable version to Croccante and never
transcodes or stores filler media itself.
For public broadcast destinations, Alana is a transport-only boundary: it
validates and forwards Alcantara's exact opaque IDs and versioned Secrets
Manager references, but never resolves a reference or handles a stream key.

## Architecture

```text
Alcantara Program page
        |
Chromium + Xvfb + PulseAudio
        |
        +--> independent H.264/AAC encoder --> RTMP output 1
        +--> independent H.264/AAC encoder --> RTMP output 2...
        +--> optional H.264/Opus encoder --> LiveKit program-feed participant
        +--> optional H.264/AAC recorder --> short Matroska segments --> verified MP4
```

Each RTMP destination has its own encoder, PID, progress journal, stall watchdog,
bounded restart loop, and health state. A destination failure therefore cannot
restart another RTMP destination or the optional LiveKit leg. LiveKit uses its
own encoder and publisher supervisor and never auto-subscribes to caller tracks.

Chromium and PulseAudio are shared capture inputs with independent watchdogs.
Container shutdown terminates every tracked browser, encoder, publisher,
watchdog, and virtual-display process and removes PID/socket state.
The recorder is a separate opt-in supervisor. Its failure state and restart
budget never stop or restart Chromium, RTMP, LiveKit, or Croccante lifecycle
control.

## Configuration

Required:

| Variable | Description |
| --- | --- |
| `PROGRAM_ID` | Stable Alcantara program identity for this runtime |
| `CHANNEL_BROWSER_URL` | Canonical Alcantara Program renderer URL |
| `RTMP_OUTPUTS` | Full publish URLs separated by spaces, commas, or newlines |
| `CROCCANTE_CONTROL_URL` | Private Croccante control origin, normally `http://croccante:8081` |
| `ALANA_CONTROL_TOKEN_FILE` | Host path to the inbound bearer-token file for Compose |
| `CROCCANTE_CONTROL_TOKEN_FILE` | Host path to Croccante's bearer-token file for Compose |

`YOUTUBE_STREAM_KEY` remains a compatibility input when `RTMP_OUTPUTS` is
empty. It resolves to YouTube's RTMP ingest URL. Full output URLs and stream keys
are never printed by Alana's supervisor.

Chromium, FFmpeg, and LiveKit child-process diagnostics are discarded because
those tools can repeat connection URLs on failure. Alana emits only indexed
health and restart telemetry, never renderer URLs or publishing credentials.

Optional recording:

| Variable | Default | Description |
| --- | --- | --- |
| `RECORDING_ENABLED` | `0` | Enable the private recording control contract; disabled startup creates no recorder process |
| `RECORDING_SEGMENT_SECONDS` | `5` | Target length of each crash-tolerant Matroska segment |
| `RECORDING_QUOTA_BYTES` | `53687091200` | Maximum retained bytes below the recording root |
| `RECORDING_MIN_FREE_BYTES` | `1073741824` | Free-space reserve required before and during capture |
| `RECORDING_RETENTION_HOURS` | `168` | Age after which inactive recording directories are removed before a new Start |
| `RECORDING_MAX_RESTARTS` | `5` | Capture-process restart budget for one operation |
| `RECORDING_RESTART_BACKOFF_SECONDS` | `2` | Initial bounded restart delay |
| `RECORDING_START_TIMEOUT` | `10` | Seconds for the control API to observe Active or Failed |
| `RECORDING_FINALIZE_TIMEOUT` | `120` | Seconds allowed for verified MP4 remux/probe |
| `RECORDING_VIDEO_BITRATE` | `6000k` | Recorder-only H.264 bitrate |
| `RECORDING_AUDIO_BITRATE` | `128k` | Recorder-only AAC bitrate |
| `RECORDING_X264_PRESET` | `veryfast` | Recorder-only software encoder preset |

## Broadcast lifecycle API

The private API listens on container port 8080 and is deliberately not
published by the supplied Compose stack. Both services share an external
`broadcast-control` network:

```bash
docker network create broadcast-control
mkdir -p secrets
openssl rand -hex 32 > secrets/alana-control-token
# Put Croccante's separately generated token in secrets/croccante-control-token.
```

Every request uses `Authorization: Bearer <token>`. Mutating requests need a
stable `Idempotency-Key`; lifecycle Start/Stop also need a positive,
monotonically increasing `X-Command-Sequence`:

| Method | Private path | Meaning |
| --- | --- | --- |
| `GET` | `/v1/programs/{programId}/lifecycle` | Requested/actual state, readiness, active program, timestamps, last command, output health, and Croccante acknowledgement |
| `POST` | `/v1/programs/{programId}/lifecycle/start` | Ready local output, then request Croccante Start |
| `POST` | `/v1/programs/{programId}/lifecycle/stop` | Request Croccante Stop, then tear down local output |
| `PUT` | `/v1/programs/{programId}/destinations/{version}` | While stopped, validate/reload an exact destination selection through Croccante |
| `PUT` | `/v1/programs/{programId}/fillers/{version}` | Idempotently prepare the next-session television filler in Croccante |
| `GET` | `/v1/programs/{programId}/fillers/{version}` | Reconcile and report Croccante's readiness for one version |
| `GET` | `/v1/programs/{programId}/recording` | Current bounded recording, disk, and finalization state |
| `POST` | `/v1/programs/{programId}/recording/start` | Start an independent recording of the already-ready composed program |
| `POST` | `/v1/programs/{programId}/recording/stop` | Stop capture, verify every segment, and remux a final MP4 |

Commands for another program return 404. Replayed keys return their original
result without repeating side effects; old sequences and concurrent transitions
return 409. Token values, publish URLs, and raw idempotency keys are never stored
in lifecycle state or emitted by the control server.

Recording Start/Stop uses the same bearer credential and a bounded
`Idempotency-Key`, but no command sequence or body. Start returns `202` after
the independent recorder reaches Requested or Active. It fails closed when the
shared publication pipeline is not ready, recording is disabled, or the disk
preflight fails. Recording health never changes publication lifecycle state.

## Composed-program recording

When enabled and explicitly started, the recorder consumes the same X11 display
and PulseAudio `stream_out.monitor` source as every publisher. It does not
record an RTMP or LiveKit destination, and it never sees destination URLs or
credentials. Its public state progresses through `requested`, `active`,
`finalizing`, `complete`, or `failed`; an enabled runtime with no operation is
`idle`, and the default configuration reports `disabled`.

Recording data lives below `/var/lib/alana/recordings`, which is already part
of the persistent `alana_state` volume:

| Path | Meaning |
| --- | --- |
| `state.json` | Atomically replaced current-operation state and bounded counters |
| `commands/<sha256>.json` | Redacted Start/Stop idempotency outcomes |
| `operations/<operationId>/segment-NNNNNN.mkv` | Closed crash-tolerant H.264/AAC segment |
| `operations/<operationId>/manifest.jsonl` | Fsynced append-only per-segment timing, sequence, bytes, codecs, dimensions, duration, and SHA-256 |
| `operations/<operationId>/manifest.json` | Final bounded summary |
| `operations/<operationId>/program.mp4` | Checksum-verified final MP4 after Stop |

The supervisor records a segment only after `ffprobe` confirms both audio and
video. A forced FFmpeg exit may discard or quarantine the one open segment;
closed segments remain checksummed and the capture restarts at the next sequence
within its bounded budget. A control-process or container restart resumes a
persisted Requested/Active operation. An explicit Stop changes state to
Finalizing, verifies every segment checksum, remuxes without transcoding, probes
the MP4, and reports Complete only with its final checksum and media metadata.

Free space and all retained bytes below the recording root, including state and
idempotency command records, are checked before Start and while recording.
Crossing the reserve or quota stops only the recorder and reports the bounded
`disk-exhausted` or `quota-exhausted` failure. Before a new Start, completed or
failed operation directories older than `RECORDING_RETENTION_HOURS` are removed.
Structured recorder logs contain only closed event/state/result values and
bounded counters; operation IDs, paths, input URLs, credentials, and free-form
FFmpeg diagnostics are excluded.
For earlier operator cleanup, stop/finalize the current recording, preserve any
required artifact and manifest outside the container volume, then remove only
the selected inactive `operations/<operationId>` directory. Never remove the
recording root or current operation while its state is Requested, Active, or
Finalizing.

Filler preparation is a separate authenticated machine operation. The `PUT`
uses an `Idempotency-Key` equal to the bounded `commandId` in the JSON body and
accepts Croccante's source and live-profile contract:

```json
{
  "commandId": "alcantara-filler-123",
  "source": {
    "id": "asset-456",
    "sha256": "64-lowercase-hex-characters",
    "downloadUrl": "https://signed-download.example/object"
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
    "loopSeconds": 10
  }
}
```

Alana forwards the signed download material only in that request. Durable and
public state contains bounded readiness/failure information, source identity
and checksum, artifact checksum, and profile, but never the download URL or a
credential. Repeating an identical version is safe; changing the semantic
content of an existing version returns `409`. A failed or interrupted request
remains visible and retryable, and restart reconciliation checks Croccante's
durable prepared state without needing the signed URL again.

Start fails closed until a pending version is ready. Alana passes that exact
version to Croccante with `X-Filler-Version` and reports Running only when
Croccante acknowledges the same ready version. The active version cannot change
during a session. Preparing another version while Running creates a pending
next-session version; Stop preserves the newest pending version, or makes the
just-stopped version available for the next Start when no replacement exists.
Alana does not invent a filler, transcode media, or retain platform destination
keys.

### Versioned destination transport

Start's JSON body is the exact destination selection authorized by Alcantara:

```json
{
  "version": "destinations-2026-08-26.1",
  "destinations": [
    {
      "id": "primary",
      "secretId": "broadcast/example/primary",
      "versionId": "00000000-0000-0000-0000-000000000000"
    }
  ]
}
```

The list contains 1-20 unique opaque IDs. Unknown fields, malformed or
duplicate IDs, invalid references, an oversized body, or a changed selection
on idempotent replay fail before Alana starts its local output. After local
readiness, Alana forwards the normalized selection byte-for-byte semantically;
Croccante resolves every exact secret version and must acknowledge the same
selection version, SHA-256 selection hash, count, and opaque IDs before Alana
reports Running. Partial or mismatched downstream state is Degraded, never a
false success.

While stopped, `PUT /destinations/{version}` accepts the same selection plus a
`commandId` equal to the `Idempotency-Key`. It propagates Croccante's explicit
validation/reload operation. Reconfiguration while Starting, Running,
Degraded, or while the local pipeline remains alive returns `409`; active
broadcast destinations are immutable until an explicit Stop.

Secret references exist only in the bounded inbound and downstream request
objects. Durable lifecycle and idempotency state stores only the version,
selection hash, count, and opaque IDs. API responses, metrics, logs, state
files, and process arguments omit secret references, URLs, provider bodies,
and keys. After restart, Alana reconciles the persisted redacted fingerprint
against Croccante's authoritative session state. If Croccante has not accepted
the selection, Alcantara must safely replay the original Start command so Alana
can forward the exact references again.

The persisted state machine is `stopped -> starting -> running -> stopping ->
stopped`, with `degraded` or `failed` representing recoverable faults. A failed
Croccante Stop leaves Alana publishing and retries the same downstream command;
it never falsely acknowledges Stop. A local publisher failure does not issue a
Croccante Stop: Alana marks itself degraded and restarts the requested pipeline.
After container restart, `/var/lib/alana/lifecycle.json` restores the requested
state and reconciliation resumes. Only one transition is serialized at a time.

Optional LiveKit:

| Variable | Default | Description |
| --- | --- | --- |
| `LIVEKIT_ENABLED` | `0` | Set to `1` to add the concurrent LiveKit leg |
| `LIVEKIT_URL` | — | LiveKit WebSocket URL |
| `LIVEKIT_API_KEY` | — | Runtime-supplied API key |
| `LIVEKIT_API_SECRET` | — | Runtime-supplied API secret |
| `LIVEKIT_ROOM` | — | Program-specific room |
| `LIVEKIT_IDENTITY` | `program-feed-$PROGRAM_ID` | Stable participant identity |
| `LIVEKIT_METADATA` | role and program ID | Participant metadata |

When `LIVEKIT_ENABLED=1`, every LiveKit value above is required. When it is
`0`, LiveKit consumes no encoder or network resources and does not affect
health.

Video and recovery:

| Variable | Default |
| --- | --- |
| `RESOLUTION` | `1920x1080` |
| `FPS` | `30` (integer, decimal, or rational FFmpeg frame rate) |
| `VIDEO_ENCODER` | `libx264` |
| `X264_PRESET` | `veryfast` |
| `VIDEO_BITRATE` / `VIDEO_MAXRATE` | `6000k` |
| `VIDEO_BUFSIZE` | `12000k` |
| `GOP_SIZE` | `60` |
| `AUDIO_BITRATE` | `128k` |
| `STALL_TIMEOUT` | `30` seconds |
| `RESTART_BACKOFF` | `5` seconds |
| `MAX_BACKOFF` | `60` seconds |
| `HEALTHY_RESET_SECONDS` | `60` seconds |
| `ALLOW_SOFTWARE_FALLBACK` | `0` |

The Intel and NVIDIA Compose profiles select `h264_vaapi` and `h264_nvenc`.
Hardware encoder failure does not silently fall back. Set
`ALLOW_SOFTWARE_FALLBACK=1` only after the actual resolution/FPS workload has
been measured within CPU capacity; the supervisor logs that explicit fallback.

## Run locally

```bash
cp .env.example .env
# Set the renderer, publisher, control URL, and secret-file paths.
docker network create broadcast-control 2>/dev/null || true
docker compose up --build
```

Example with concurrent LiveKit:

```bash
PROGRAM_ID=main \
CHANNEL_BROWSER_URL=https://alcantara.example/program/main?renderer=1 \
RTMP_OUTPUTS="rtmp://relay.example/live/REDACTED" \
LIVEKIT_ENABLED=1 \
LIVEKIT_URL=wss://livekit.example \
LIVEKIT_API_KEY=runtime-key \
LIVEKIT_API_SECRET=runtime-secret \
LIVEKIT_ROOM=program-main \
docker compose up --build
```

The root Compose stack is the supported entry point. Use the `alana-intel` or
`alana-nvidia` profile only on a host with the matching device/runtime.

## Health and recovery

The image health check considers an explicitly stopped lifecycle healthy. A
running lifecycle requires:

- the configured Chromium process;
- one live encoder PID for every RTMP output; and
- a live LiveKit pipeline PID when LiveKit is enabled.

A process in bounded restart backoff is unhealthy rather than falsely reported
as running. Useful files:

| Path | Meaning |
| --- | --- |
| `/tmp/channel-browser.pid` | current browser process |
| `/tmp/rtmp-N.pid` | current encoder for RTMP output N |
| `/tmp/rtmp-progress-N.txt` | ffmpeg progress for output N |
| `/tmp/livekit.pid` | optional LiveKit pipeline |
| `/tmp/livekit-progress.txt` | LiveKit encoder progress |

Backoff doubles to the configured cap and resets after sustained health.
Stalled encoders are killed and restarted. Browser failure restarts only
Chromium. LiveKit failure leaves every RTMP encoder untouched.

## Prometheus observability

Alana exposes Prometheus text format 0.0.4 at `GET /metrics` on the same private
port 8080 listener as lifecycle control. The route uses the existing
`ALANA_CONTROL_TOKEN_FILE` bearer credential and returns `401` when the token is
missing or incorrect. The supplied Compose stack does not publish port 8080;
the managed scraper must reach it over a private Docker or service network.

The collector does not expose program IDs, renderer or publish URLs, output
indices, room names, stream keys, tokens, raw errors, or other unbounded values.
RTMP legs are aggregated into configured, healthy, and progressing counts.

| Metric | Meaning | Bounded labels |
| --- | --- | --- |
| `alana_service_info` | Service, Python runtime, and build identity | `service`, `runtime`, `version` |
| `alana_process_start_time_seconds` | Control-process start time | none |
| `alana_process_resident_memory_bytes` | Control-process resident memory | none |
| `alana_http_requests_total` | Private HTTP requests by normalized outcome | `method`, `route`, `status_class` |
| `alana_http_request_duration_seconds` | Private HTTP request latency | `method`, `route` |
| `alana_dependency_operations_total` | Croccante lifecycle, filler, destination, and status results | `dependency`, `operation`, `result` |
| `alana_dependency_duration_seconds` | Croccante operation latency | `dependency`, `operation` |
| `alana_lifecycle_commands_total` | Start/Stop command outcomes | `action`, `result` |
| `alana_reconcile_cycles_total` | Reconciliation success/failure | `result` |
| `alana_filler_preparations_total` | Preparation, duplicate, conflict, retry, and reconciliation outcomes | `result` |
| `alana_destination_operations_total` | Destination reload validation outcomes | `action`, `result` |
| `alana_recording_commands_total` | Recording Start/Stop outcomes | `action`, `result` |
| `alana_lifecycle_state` | One-hot actual lifecycle state | `state` |
| `alana_filler_active` | Whether the current session is bound to a prepared version | none |
| `alana_filler_pending` | Whether a next-session version is configured | none |
| `alana_filler_pending_ready` | Whether Croccante acknowledged that pending version | none |
| `alana_destinations_active` | Opaque destinations bound to the active broadcast | none |
| `alana_destinations_pending` | Opaque destinations in the validated next selection | none |
| `alana_pipeline_process_healthy` | Pipeline supervisor liveness | none |
| `alana_browser_healthy` | Chromium capture liveness | none |
| `alana_rtmp_outputs_configured` | Configured RTMP leg count | none |
| `alana_rtmp_outputs_healthy` | RTMP legs with a live encoder | none |
| `alana_rtmp_outputs_progressing` | RTMP legs reporting frame progress | none |
| `alana_livekit_enabled` | LiveKit configuration state | none |
| `alana_livekit_healthy` | LiveKit runtime health | none |
| `alana_recording_enabled` | Whether recording control is enabled | none |
| `alana_recording_state` | One-hot bounded recording lifecycle | `state` |
| `alana_recording_segments` / `alana_recording_bytes` / `alana_recording_duration_seconds` | Current durable recording size and duration | none |
| `alana_recording_dropped_frames` / `alana_recording_errors_total` / `alana_recording_restarts_total` | Current operation capture counters | none |
| `alana_recording_disk_free_bytes` / `alana_recording_disk_usage_bytes` | Current recording filesystem capacity | none |
| `alana_recording_disk_quota_bytes` / `alana_recording_disk_minimum_free_bytes` | Configured disk gates | none |
| `alana_recording_final_artifact_verified` / `alana_recording_final_bytes` | Verified finalization state and size | none |
| `alana_stream_restarts_total` | Supervisor retries by leg/reason | `leg`, `reason` |
| `alana_stream_stalls_total` | Watchdog stalls by leg | `leg` |
| `alana_restart_backoff_seconds` | Observed restart backoff histogram | `leg` |
| `alana_software_fallbacks_total` | Explicit software fallbacks | `leg` |

For a private in-container check:

```bash
docker compose exec -T alana python3 -c 'from pathlib import Path; from urllib.request import Request, urlopen; token=Path("/run/secrets/alana-control-token").read_text().strip(); print(urlopen(Request("http://127.0.0.1:8080/metrics", headers={"Authorization": f"Bearer {token}"})).read().decode())'
```

`ALANA_BUILD_VERSION` may be supplied as a short release or source identifier;
Compose defaults it to `dev`. Application instrumentation and its private
endpoint belong here. The `gaulatti/prometheus` deployment separately owns the
private target, credential delivery, storage, dashboards, and alerts and must
be updated before production scraping begins.

`PIPELINE_READY_TIMEOUT` controls how long Start waits for browser and publisher
readiness (default 90 seconds). `CONTROL_RETRY_SECONDS` controls reconciliation
and failed-ack retry cadence (default 5 seconds).

## Platform builds

The Dockerfile uses native Debian Chromium and selects the pinned LiveKit CLI
archive for the image architecture.

```bash
docker buildx build --platform linux/amd64 --output type=cacheonly .
docker buildx build --platform linux/arm64 --output type=cacheonly .
PLATFORM=linux/arm64 docker compose build
```

amd64 is the Intel i9 production target. arm64 is the Apple Silicon development
target. Production-class combined RTMP+LiveKit soak, measured latency, dropped
frames, encoder utilization, and network behavior must be recorded before
promoting a particular program configuration.

## Failure drills

Before UAT, verify:

1. Disconnect one RTMP destination; other RTMP and LiveKit PIDs remain stable.
2. Interrupt LiveKit; RTMP PIDs and progress continue.
3. Kill Chromium; it restarts and publishers recover without orphan processes.
4. Repeat container stop/start and confirm no stale PID or Unix socket remains.
5. Run with LiveKit disabled and confirm the unattended RTMP-only program has no
   LiveKit process or health requirement.
6. With recording enabled, kill its FFmpeg PID; confirm a later segment appears,
   the recording restart counter advances, and every RTMP/LiveKit PID is stable.
7. Raise the recording free-space reserve above available space; confirm
   recording alone becomes Failed while lifecycle health remains Running.
8. Stop a synthetic recording and use `ffprobe` to confirm synchronized audio
   and video in `program.mp4`; verify its checksum matches the final manifest.

Croccante remains a transitional RTMP destination among possible direct
destinations. G-207 owns its production soak and the later simplification of
Alana to one Croccante RTMP output. OBS may remain during staged rollout but is
not part of Alana's runtime architecture.
