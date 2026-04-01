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

### Build the Docker Image

```bash
make build
```

### Run the Container

```bash
export YOUTUBE_STREAM_KEY="your_stream_key"
make run
```

> Keep `YOUTUBE_STREAM_KEY` out of source control. Use shell environment variables or CI/CD secrets.

Or with a custom channel URL:

```bash
export YOUTUBE_STREAM_KEY="your_stream_key"
make run CHANNEL_BROWSER_URL="https://your-channel-app.example.com"
```

### Manual `docker run` (equivalent to `make run`)

```bash
docker build -t alana:latest . && docker rm -f alana
docker run -d \
    --name alana \
    --restart always \
    --shm-size=1g \
    -e YOUTUBE_STREAM_KEY="your_stream_key" \
    -e CHANNEL_BROWSER_URL="https://your-channel-app.example.com" \
    alana:latest
```

> **Note:** OBS-era env vars (`OBS_LEGACY_MODE`, `OBS_WEBSOCKET_PORT`, `OBS_WEBSOCKET_PASSWORD`) and port mappings (`4455`, `5901`) are silently ignored if passed — they are no longer used.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YOUTUBE_STREAM_KEY` | (empty) | YouTube Live stream key — **required to stream** |
| `CHANNEL_BROWSER_URL` | `https://example.com/live` | URL of the React channel page to render |

## Battle-Tested Specs

This profile has been validated for stable YouTube streaming with hardware H.264 encoding on an Intel iGPU host.

- Host: Linux `x86_64` (Intel Core i9-12900K class machine)
- Output: `1920x1080 @ 30fps`
- Encoder: `h264_vaapi` (Intel iGPU)
- Device path: `/dev/dri/renderD129`
- VAAPI driver: `iHD`
- Target bitrate: `6800k`

```bash
make build IMAGE_NAME=alana-v2 CONTAINER_NAME=alana-v2
make run \
  IMAGE_NAME=alana-v2 \
  CONTAINER_NAME=alana-v2 \
  SHM_SIZE=2g \
  YOUTUBE_STREAM_KEY="your_stream_key" \
  CHANNEL_BROWSER_URL="https://example.com/live" \
  DEVICE_FLAGS="--device=/dev/dri/card0:/dev/dri/card0 --device=/dev/dri/renderD129:/dev/dri/renderD129" \
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
  CHROME_DISABLE_DEV_SHM_USAGE=0
```

If VAAPI fails on your host, Alana falls back to `libx264` automatically so the stream stays up.

## Viewing Logs

```bash
make logs
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

## License

See LICENSE file for details.
