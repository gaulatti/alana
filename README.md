# Alana

Alana is Alcantara's headless Program renderer and publisher. One runtime renders
one configured Alcantara Program page continuously and publishes it through:

- one or more required RTMP outputs; and
- an optional concurrent low-latency LiveKit Program feed.

Radio is intentionally absent. Palazzo owns radio playback and Icecast output.

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

`YOUTUBE_STREAM_KEY` remains a compatibility input when `RTMP_OUTPUTS` is
empty. It resolves to YouTube's RTMP ingest URL. Full output URLs and stream keys
are never printed by Alana's supervisor.

Chromium, FFmpeg, and LiveKit child-process diagnostics are discarded because
those tools can repeat connection URLs on failure. Alana emits only indexed
health and restart telemetry, never renderer URLs or publishing credentials.

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
# Set PROGRAM_ID, CHANNEL_BROWSER_URL, and RTMP_OUTPUTS.
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

The image health check requires:

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
