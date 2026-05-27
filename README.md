# Alana - Headless Browser Streaming Environment

A containerized streaming pipeline that renders a React channel webpage in Google Chrome and pushes a live H.264/AAC stream to YouTube via RTMPS — **no OBS required**.

## Architecture

```
Xvfb :98  ──▶  Google Chrome  ──▶  x11grab  ──┐
                (CHANNEL_BROWSER_URL)           ├──▶  ffmpeg  ──▶  RTMPS  ──▶  YouTube
PulseAudio null sink (stream_out) ─────────────┘
```

- **Chrome** renders the channel page (video, audio, WebRTC, overlays — everything comes from the webpage).
- **Xvfb :98** provides a single virtual display at 1920×1080.
- **PulseAudio** routes browser audio through a null sink so `ffmpeg` can capture it reliably via the monitor source.
- **ffmpeg** encodes video (`x11grab`) and audio (`pulse`) to H.264/AAC and streams via RTMPS to YouTube Live.
- A **supervisor loop** restarts ffmpeg on crash with exponential backoff, and a **stall watchdog** kills and restarts it if no frames are produced for 30 seconds.
- A **browser watchdog** restarts Chrome if it exits unexpectedly.

## Quick Start

### Setup

Copy the environment template and fill in your secrets:

```bash
cp .env.example .env
# Edit .env — at minimum set YOUTUBE_STREAM_KEY and CHANNEL_BROWSER_URL
```

Keep `.env` out of source control (it's in `.gitignore`).

### Build and Run

```bash
docker compose build
docker compose up -d
```

> Equivalent to `docker compose build && docker compose up -d`.

Or specify variables inline (they override `.env`):

```bash
YOUTUBE_STREAM_KEY="your_key" CHANNEL_BROWSER_URL="https://your-app.example.com" docker compose up -d
```

### Direct `docker run` (no compose)

```bash
docker build -t alana:amd64 .
docker run -d \
    --name alana \
    --restart always \
    --shm-size=1g \
    -e YOUTUBE_STREAM_KEY="your_stream_key" \
    -e CHANNEL_BROWSER_URL="https://your-channel-app.example.com" \
    alana:amd64
```

### Useful commands

| Action | Command |
|--------|---------|
| Build | `docker compose build` |
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| Restart | `docker compose restart` |
| Logs (follow) | `docker compose logs -f` |
| Interactive shell | `docker compose run --rm alana bash` |
| Clean (remove everything) | `docker compose down --rmi all` |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YOUTUBE_STREAM_KEY` | (empty) | YouTube Live stream key — **required to stream** |
| `CHANNEL_BROWSER_URL` | `https://example.com/live` | URL of the React channel page to render |

## Icecast / Radio Mode

Set `STREAM_MODE=icecast` to stream audio-only to an Icecast server. Chrome still renders the channel page as the audio source (SSE-driven playback, WebRTC, direct order — everything is controlled by the webpage). Xvfb and PulseAudio run as normal; only the ffmpeg pipeline changes — `x11grab` is dropped and audio is captured directly from the PulseAudio null sink.

### Architecture (Icecast mode)

```
Xvfb :98  ──▶  Google Chrome (CHANNEL_BROWSER_URL)
                       │
              PulseAudio null sink (stream_out)
                       │
                    ffmpeg  ──▶  Icecast  ──▶  listeners
```

### Quick Start

```bash
STREAM_MODE=icecast \
  CHANNEL_BROWSER_URL="https://your-channel-app.example.com" \
  ICECAST_HOST=your-icecast-server.example.com \
  ICECAST_PORT=8000 \
  ICECAST_MOUNT=/stream.mp3 \
  ICECAST_SOURCE_PASSWORD=your_source_password \
  ICECAST_CODEC=mp3 \
  ICECAST_BITRATE=128k \
  ICECAST_STREAM_NAME="My Radio Station" \
  docker compose up -d
```

Or set these in `.env` and just run `docker compose up -d`.

### Icecast Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STREAM_MODE` | `youtube` | Set to `icecast` to enable radio mode |
| `ICECAST_HOST` | `localhost` | Icecast server hostname or IP |
| `ICECAST_PORT` | `8000` | Icecast server port |
| `ICECAST_MOUNT` | `/stream` | Mount point (e.g. `/radio.mp3`) |
| `ICECAST_SOURCE_PASSWORD` | `hackme` | Icecast source password |
| `ICECAST_CODEC` | `mp3` | Audio codec: `mp3`, `aac`, or `opus` |
| `ICECAST_BITRATE` | `128k` | Audio bitrate |
| `ICECAST_STREAM_NAME` | `Alana Radio` | Stream metadata name shown to listeners |

### Codec notes

| Codec | Container | Content-Type | Notes |
|-------|-----------|-------------|-------|
| `mp3` | raw MP3 | `audio/mpeg` | Best compatibility — works in all browsers and players |
| `aac` | ADTS | `audio/aac` | Better quality per bit than MP3; use `.aac` mount |
| `opus` | Ogg | `audio/ogg` | Best quality per bit; use `.ogg` mount; not supported in all Icecast clients |

### Resource usage in icecast mode

No x11grab or video encoding. CPU drops to roughly:
- Chrome renderer + GPU: ~0.2–0.5 cores (page still renders but no frame capture)
- ffmpeg audio encode (MP3 128k): < 0.1 core
- Xvfb + PulseAudio: negligible

A `t4g.micro` (2 vCPU / 1 GB ARM) or `t3.micro` (2 vCPU / 1 GB) is sufficient for a single audio stream.

## Battle-Tested Specs

This profile has been validated for stable YouTube streaming with hardware H.264 encoding on an Intel iGPU host.

- Host: Linux `x86_64` (Intel Core i9-12900K class machine)
- Output: `1920x1080 @ 30fps`
- Encoder: `h264_vaapi` (Intel iGPU)
- Device path: `/dev/dri/renderD129`
- VAAPI driver: `iHD`
- Target bitrate: `6800k`

```bash
# Set env vars in .env, then:
docker compose build
# Uncomment devices: in docker-compose.yml for GPU passthrough
docker compose up -d
```

Or with a custom tag and inline overrides:

```bash
# Build with a custom tag
docker build -t alana-v2:amd64 .
# Run with inline env and device passthrough
IMAGE_NAME=alana-v2 SHM_SIZE=2g \
  YOUTUBE_STREAM_KEY="your_stream_key" \
  CHANNEL_BROWSER_URL="https://example.com/live" \
  VIDEO_ENCODER=h264_vaapi \
  VAAPI_DEVICE=/dev/dri/renderD129 \
  VAAPI_DRIVER=iHD \
  RESOLUTION=1920x1080 \
  WINDOW_SIZE=1920x1080 \
  FPS=30 \
  VIDEO_BITRATE=6800k \
  VIDEO_MAXRATE=6800k \
  VIDEO_BUFSIZE=13600k \
  GOP_SIZE=60 \
  DRAW_MOUSE=0 \
  CHROME_ENABLE_PERF_FLAGS=0 \
  CHROME_DISABLE_DEV_SHM_USAGE=0 \
  docker compose up -d
```

If VAAPI fails on your host, Alana falls back to `libx264` automatically so the stream stays up.

## Viewing Logs

```bash
docker compose logs -f
# or
docker logs -f alana
```

Log sections surfaced to stdout every 10 seconds:

| Log file | Content |
|----------|---------|
| `/tmp/ffmpeg.log` | ffmpeg encode/stream output |
| `/tmp/channel-browser.log` | Chrome stderr |
| `/tmp/xvfb.log` | Xvfb virtual display |
| `/tmp/pulseaudio.log` | PulseAudio daemon |

## Resilience

| Mechanism | Behaviour |
|-----------|-----------|
| ffmpeg supervisor loop | Restarts ffmpeg on exit; backoff starts at 5 s, doubles up to 60 s; resets after a healthy 60 s run |
| Stall watchdog | Kills ffmpeg if `frame=` in `-progress` output does not advance for 30 s |
| Browser watchdog | Checks every 15 s; restarts Chrome if the process has exited |

## Troubleshooting

### Stream not starting / `YOUTUBE_STREAM_KEY` not set

ffmpeg will log `YOUTUBE_STREAM_KEY is not set` and retry every 30 s. Pass the key via `-e YOUTUBE_STREAM_KEY=...`.

### No audio in stream

1. Confirm PulseAudio started: `docker exec alana pactl info`
2. Check the null sink exists: `docker exec alana pactl list sinks short` — you should see `stream_out`.
3. Verify Chrome is using the right sink: `docker exec alana pactl list sink-inputs short`.
4. Check `/tmp/pulseaudio.log` inside the container for errors.

### Black / frozen video

1. Check Chrome is alive: `docker exec alana cat /tmp/channel-browser.pid | xargs kill -0 && echo running`
2. Check Xvfb: `docker exec alana cat /tmp/xvfb.log`
3. Verify ffmpeg is capturing: `docker exec alana grep frame= /tmp/ffmpeg-progress.txt`

### Chrome crashes immediately

- Increase `--shm-size` (currently `1g`) if you see shared memory errors in `/tmp/channel-browser.log`.
- Ensure the host kernel allows user namespaces or that `--no-sandbox` is effective.

### ffmpeg keeps restarting

Check `/tmp/ffmpeg.log` inside the container for the root cause (network, bad stream key, encoding errors).

## EC2 Sizing Guide

Alana runs entirely inside a Docker container and has no cloud-specific dependencies — it works identically on any Linux host. The guidance below targets AWS EC2 as a representative cloud deployment reference, but the same container images and `docker compose up -d` commands work on any VM, bare-metal server, or on-premises machine.

---

### TV Mode (YouTube / RTMPS)

TV mode runs the full pipeline: Chrome renders the channel page at 1920×1080, Xvfb provides the virtual display, and ffmpeg encodes and streams H.264/AAC via RTMPS to YouTube Live.

**Observed resource usage** from a live 1080p30 `h264_vaapi` stream (36+ hours continuous):

| Process | CPU | RSS |
|---------|-----|-----|
| ffmpeg (`h264_vaapi`) | ~57% (1 core) | ~177 MB |
| Chrome GPU process | ~54% (1 core) | ~248 MB |
| Chrome renderer | ~13% | ~791 MB |
| Chrome main + network + audio | ~3% | ~500 MB |
| Xvfb | ~5% | ~179 MB |
| **Total** | **~1.75 cores** | **~1.9 GB** |

ffmpeg stream health: 30 fps sustained, `speed=1x`, 43 dropped frames over 36 hours.

> `h264_vaapi` offloads encoding to an Intel iGPU and is **not available on EC2**. On cloud instances, use `h264_nvenc` (GPU instances) or `libx264` (CPU instances). `libx264 superfast` uses ~2–3 cores; `libx264 veryfast` uses ~4–5 cores at 1080p30.

**Recommended EC2 instances for TV mode:**

| Encoder | Instance | vCPU / RAM | On-demand/mo | 1-yr reserved/mo | Notes |
|---------|----------|------------|-------------|-----------------|-------|
| `libx264 superfast` | `c6i.xlarge` | 4 / 8 GB | ~$122 | ~$79 | Best cost/performance for CPU encode |
| `libx264 veryfast` | `c6i.2xlarge` | 8 / 16 GB | ~$245 | ~$122 | Headroom for quality-first preset |
| `h264_nvenc` | `g4dn.xlarge` | 4 / 16 GB + T4 GPU | ~$379 | ~$194 | Lowest CPU; GPU handles encoding |

**RAM floor:** 4 GB free after OS + Docker. Use `--shm-size=2g`; Chrome is unstable below 1 GB shared memory.

**Bandwidth:** At 6800k video + 128k audio, the container pushes ~75–80 GB/month upstream to YouTube. EC2 egress is free to YouTube (AWS partner). Lowering to `VIDEO_BITRATE=4500k` saves ~30% bandwidth with no perceptible viewer quality loss — YouTube re-encodes to 2500k anyway.

---

### Radio Mode (Icecast / Audio-only)

Radio mode drops the entire video pipeline. Chrome still renders the channel page as the audio source (SSE, WebRTC, direct playback — controlled by the webpage), but ffmpeg only captures and encodes audio.

**Resource profile:**

| Process | CPU | RSS |
|---------|-----|-----|
| Chrome (renderer + GPU + audio) | ~0.3–0.5 cores | ~600–900 MB |
| ffmpeg MP3/AAC encode | < 0.1 core | ~30 MB |
| Xvfb + PulseAudio | ~0.05 cores | ~200 MB |
| **Total** | **~0.5–0.6 cores** | **~1–1.2 GB** |

**Recommended EC2 instances for Radio mode:**

| Scenario | Instance | vCPU / RAM | On-demand/mo | 1-yr reserved/mo | Notes |
|----------|----------|------------|-------------|-----------------|-------|
| Single stream | `t3.small` | 2 / 2 GB | ~$17 | ~$11 | Comfortable headroom; `t3.micro` risks OOM with Chrome |
| Multiple streams (up to ~8) | `t3.medium` | 2 / 4 GB | ~$34 | ~$22 | Each additional ffmpeg process adds ~0.1 core + 30 MB |
| High reliability / burst | `c6i.large` | 2 / 4 GB | ~$61 | ~$39 | Fixed performance, no CPU credits to exhaust |

> `t3` instances use burstable CPU credits. A single audio encode job is well within baseline credit accrual, but if Chrome is actively rendering a complex page 24/7, monitor `CPUCreditBalance` in CloudWatch and upgrade to a `c6i` if credits drain.

**Bandwidth:** At 128k MP3, one listener consumes ~57 MB/hour. EC2 egress costs $0.09/GB. For significant listener counts, run Alana in push-only mode (`ICECAST_HOST` pointing at a separate Icecast server) and put Cloudflare or a CDN in front of the Icecast server to absorb egress costs.

---

### TV vs Radio cost comparison

| Mode | Recommended instance | 1-yr reserved/mo | Relative cost |
|------|---------------------|-----------------|---------------|
| Radio (Icecast) | `t3.small` | ~$11 | 1× |
| TV (YouTube, CPU encode) | `c6i.xlarge` | ~$79 | 7× |
| TV (YouTube, GPU encode) | `g4dn.xlarge` | ~$194 | 18× |

---

## CPU & Bandwidth Optimisations

### Quick wins (env vars, no rebuild needed)

| Change | Env var | Impact |
|--------|---------|--------|
| Disable Chrome GPU process | `DISABLE_CHROME_GPU=1` | Removes ~0.5 core (Chrome GPU process is software-only in a container) |
| Reduce raster threads | `CHROME_EXTRA_FLAGS="--num-raster-threads=1 --renderer-process-limit=1"` | Reduces Chrome CPU for mostly-static pages |
| Lower x264 preset | `X264_PRESET=superfast` | ~30–40% CPU reduction vs `veryfast` |
| Reduce bitrate to YouTube's sweet spot | `VIDEO_BITRATE=4500k VIDEO_MAXRATE=4500k VIDEO_BUFSIZE=9000k` | ~30% bandwidth saving; YouTube re-encodes to 2500k anyway |

### Code-level ffmpeg tuning

For `libx264`, add fine-grained params to reduce encoder work without changing preset:

```
-x264-params ref=1:bframes=0:aq-mode=0:deblock=0:weightp=0
```

Reduce input queue size (1024 is overkill):

```
-thread_queue_size 128
```

Add probe hints to reduce input analysis overhead:

```
-probesize 32 -analyzeduration 0
```

### Resolution

Dropping from 1080p to 720p cuts Xvfb, Chrome rendering, and ffmpeg capture/encode work by ~55%. Suitable if the channel page is UI/overlay content rather than high-detail video.

### Bitrate vs CPU

For VAAPI, bitrate has near-zero CPU impact — the GPU handles the encoding budget. For `libx264`, bitrate has a modest effect but **preset is the dominant CPU knob**. Prioritise preset changes over bitrate changes for CPU savings.

## License

See LICENSE file for details.
