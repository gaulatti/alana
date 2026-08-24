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

## Architecture

```text
Alcantara Program page
        |
Chromium + Xvfb + PulseAudio
        |
        +--> independent H.264/AAC encoder --> RTMP output 1
        +--> independent H.264/AAC encoder --> RTMP output 2...
        +--> optional H.264/Opus encoder --> LiveKit program-feed participant
```

Each RTMP destination has its own encoder, PID, progress journal, stall watchdog,
bounded restart loop, and health state. A destination failure therefore cannot
restart another RTMP destination or the optional LiveKit leg. LiveKit uses its
own encoder and publisher supervisor and never auto-subscribes to caller tracks.

Chromium and PulseAudio are shared capture inputs with independent watchdogs.
Container shutdown terminates every tracked browser, encoder, publisher,
watchdog, and virtual-display process and removes PID/socket state.

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

Every request uses `Authorization: Bearer <token>`. Mutating requests also need
a stable `Idempotency-Key` and a positive, monotonically increasing
`X-Command-Sequence`:

| Method | Private path | Meaning |
| --- | --- | --- |
| `GET` | `/v1/programs/{programId}/lifecycle` | Requested/actual state, readiness, active program, timestamps, last command, output health, and Croccante acknowledgement |
| `POST` | `/v1/programs/{programId}/lifecycle/start` | Ready local output, then request Croccante Start |
| `POST` | `/v1/programs/{programId}/lifecycle/stop` | Request Croccante Stop, then tear down local output |
| `PUT` | `/v1/programs/{programId}/fillers/{version}` | Idempotently prepare the next-session television filler in Croccante |
| `GET` | `/v1/programs/{programId}/fillers/{version}` | Reconcile and report Croccante's readiness for one version |

Commands for another program return 404. Replayed keys return their original
result without repeating side effects; old sequences and concurrent transitions
return 409. Token values, publish URLs, and raw idempotency keys are never stored
in lifecycle state or emitted by the control server.

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
| `FPS` | `30` |
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
| `alana_dependency_operations_total` | Croccante lifecycle and filler-operation results | `dependency`, `operation`, `result` |
| `alana_dependency_duration_seconds` | Croccante lifecycle and filler-operation latency | `dependency`, `operation` |
| `alana_lifecycle_commands_total` | Start/Stop command outcomes | `action`, `result` |
| `alana_reconcile_cycles_total` | Reconciliation success/failure | `result` |
| `alana_filler_preparations_total` | Preparation, duplicate, conflict, retry, and reconciliation outcomes | `result` |
| `alana_lifecycle_state` | One-hot actual lifecycle state | `state` |
| `alana_filler_active` | Whether the current session is bound to a prepared version | none |
| `alana_filler_pending` | Whether a next-session version is configured | none |
| `alana_filler_pending_ready` | Whether Croccante acknowledged that pending version | none |
| `alana_pipeline_process_healthy` | Pipeline supervisor liveness | none |
| `alana_browser_healthy` | Chromium capture liveness | none |
| `alana_rtmp_outputs_configured` | Configured RTMP leg count | none |
| `alana_rtmp_outputs_healthy` | RTMP legs with a live encoder | none |
| `alana_rtmp_outputs_progressing` | RTMP legs reporting frame progress | none |
| `alana_livekit_enabled` | LiveKit configuration state | none |
| `alana_livekit_healthy` | LiveKit runtime health | none |
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

Croccante remains a transitional RTMP destination among possible direct
destinations. G-207 owns its production soak and the later simplification of
Alana to one Croccante RTMP output. OBS may remain during staged rollout but is
not part of Alana's runtime architecture.
