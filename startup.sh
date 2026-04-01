#!/bin/bash
# OBS-less streaming entrypoint
# Pipeline: Xvfb :98 → Chrome (renders CHANNEL_BROWSER_URL) → ffmpeg x11grab+pulse → RTMPS → YouTube

CHANNEL_BROWSER_URL="${CHANNEL_BROWSER_URL:-https://alcantara.gaulatti.com/program/fifthbell}"
DISPLAY_NUM=":98"
RESOLUTION="1920x1080"
FPS=30
YOUTUBE_RTMPS_URL="rtmps://a.rtmps.youtube.com:443/live2"
STALL_TIMEOUT=30   # seconds without frame progress → restart ffmpeg
RESTART_BACKOFF=5  # initial backoff between ffmpeg restarts (seconds)
MAX_BACKOFF=60     # maximum backoff cap (seconds)

BROWSER_LOG="/tmp/channel-browser.log"
BROWSER_PID_FILE="/tmp/channel-browser.pid"
FFMPEG_LOG="/tmp/ffmpeg.log"
FFMPEG_PROGRESS="/tmp/ffmpeg-progress.txt"

# ---------------------------------------------------------------------------
# 0. GPU device permissions (no-op when devices are absent)
# ---------------------------------------------------------------------------
if [ -e /dev/dri/card0 ]; then
    chmod 666 /dev/dri/card0
fi

if [ -e /dev/dri/renderD129 ]; then
    RENDER_GID=$(stat -c '%g' /dev/dri/renderD129)
    groupadd -g "$RENDER_GID" render 2>/dev/null || true
    usermod -a -G "$RENDER_GID" root 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 1. PulseAudio: start daemon, create null sink, set as default
# ---------------------------------------------------------------------------
mkdir -p /tmp/runtime-root && chmod 700 /tmp/runtime-root
export XDG_RUNTIME_DIR=/tmp/runtime-root

echo "[pulseaudio] Starting daemon..." >&2
pulseaudio --start --exit-idle-time=-1 --log-target=file:/tmp/pulseaudio.log 2>/dev/null || true

# Give the daemon a moment to settle
for _ in 1 2 3 4 5; do
    pactl info >/dev/null 2>&1 && break
    sleep 1
done

echo "[pulseaudio] Creating null sink 'stream_out'..." >&2
pactl load-module module-null-sink \
    sink_name=stream_out \
    sink_properties=device.description=stream_out \
    >/dev/null 2>&1 || true

pactl set-default-sink stream_out 2>/dev/null || true
echo "[pulseaudio] Default sink → stream_out (monitor: stream_out.monitor)" >&2

# ---------------------------------------------------------------------------
# 2. Xvfb: single virtual display for the browser
# ---------------------------------------------------------------------------
echo "[xvfb] Starting Xvfb ${DISPLAY_NUM}..." >&2
Xvfb "${DISPLAY_NUM}" \
    -screen 0 "${RESOLUTION}x24" \
    +extension RANDR \
    +extension MIT-SHM \
    +extension XINERAMA \
    >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
export DISPLAY="${DISPLAY_NUM}"

until xdpyinfo >/dev/null 2>&1; do
    sleep 0.2
done
echo "[xvfb] ${DISPLAY_NUM} is ready (PID ${XVFB_PID})" >&2

# ---------------------------------------------------------------------------
# 3. Chrome: render CHANNEL_BROWSER_URL on the Xvfb display
# ---------------------------------------------------------------------------
launch_browser() {
    rm -rf /tmp/chrome-profile
    mkdir -p /tmp/chrome-profile
    echo "[browser] Starting Chrome → ${CHANNEL_BROWSER_URL} on ${DISPLAY_NUM}" >&2
    env \
        DISPLAY="${DISPLAY_NUM}" \
        GTK_A11Y=none \
        LIBGL_ALWAYS_SOFTWARE=1 \
        google-chrome \
            --no-sandbox \
            --disable-gpu \
            --disable-dev-shm-usage \
            --disable-features=Translate,MediaRouter,OptimizationHints,CalculateNativeWinOcclusion,ChromeWhatsNewUI,SigninIntercept,SearchEngineChoiceTrigger \
            --disable-background-networking \
            --disable-default-apps \
            --disable-popup-blocking \
            --disable-renderer-backgrounding \
            --disable-session-crashed-bubble \
            --disable-sync \
            --hide-scrollbars \
            --incognito \
            --kiosk \
            --no-default-browser-check \
            --no-first-run \
            --test-type \
            --window-position=0,0 \
            --window-size=1920,1080 \
            --autoplay-policy=no-user-gesture-required \
            --user-data-dir=/tmp/chrome-profile \
            "${CHANNEL_BROWSER_URL}" \
        >>"${BROWSER_LOG}" 2>&1 &
    echo $! >"${BROWSER_PID_FILE}"
    echo "[browser] Chrome started with PID $!" >&2
}

launch_browser

# Wait for the page to load before starting the stream
sleep 5

# ---------------------------------------------------------------------------
# 4. ffmpeg: x11grab + PulseAudio → H.264/AAC → RTMPS to YouTube
#    Supervisor loop: restart on exit, detect stalls, apply backoff
# ---------------------------------------------------------------------------
start_ffmpeg() {
    rm -f "${FFMPEG_PROGRESS}"
    ffmpeg \
        -loglevel warning \
        -f x11grab -video_size "${RESOLUTION}" -framerate "${FPS}" -i "${DISPLAY_NUM}" \
        -f pulse -i stream_out.monitor \
        -c:v libx264 \
        -preset veryfast \
        -tune zerolatency \
        -b:v 6000k \
        -maxrate 6000k \
        -bufsize 12000k \
        -g 60 \
        -keyint_min 60 \
        -sc_threshold 0 \
        -c:a aac \
        -b:a 128k \
        -ar 44100 \
        -ac 2 \
        -f flv \
        -progress "${FFMPEG_PROGRESS}" \
        "${YOUTUBE_RTMPS_URL}/${YOUTUBE_STREAM_KEY}" \
        >>"${FFMPEG_LOG}" 2>&1 &
    echo $!
}

stall_watchdog() {
    local pid=$1
    local last_frame=0
    local last_advance
    last_advance=$(date +%s)

    while kill -0 "${pid}" 2>/dev/null; do
        sleep 5
        if [ -f "${FFMPEG_PROGRESS}" ]; then
            local frame
            frame=$(grep "^frame=" "${FFMPEG_PROGRESS}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
            if [ -n "${frame}" ] && [ "${frame}" != "${last_frame}" ]; then
                last_frame="${frame}"
                last_advance=$(date +%s)
            fi
        fi
        local elapsed=$(( $(date +%s) - last_advance ))
        if [ "${elapsed}" -ge "${STALL_TIMEOUT}" ]; then
            echo "[ffmpeg-watch] No frame advance for ${elapsed}s — killing PID ${pid}" >&2
            kill "${pid}" 2>/dev/null || true
            return
        fi
    done
}

backoff=${RESTART_BACKOFF}
(
    while true; do
        if [ -z "${YOUTUBE_STREAM_KEY:-}" ]; then
            echo "[ffmpeg] YOUTUBE_STREAM_KEY is not set; retrying in 30s..." >&2
            sleep 30
            continue
        fi

        echo "[ffmpeg] Starting at $(date)" >&2
        stream_start=$(date +%s)
        FFMPEG_PID=$(start_ffmpeg)

        stall_watchdog "${FFMPEG_PID}" &
        WATCHER_PID=$!

        wait "${FFMPEG_PID}" 2>/dev/null || true
        EXIT_CODE=$?
        kill "${WATCHER_PID}" 2>/dev/null || true

        stream_duration=$(( $(date +%s) - stream_start ))
        echo "[ffmpeg] Exited (code ${EXIT_CODE}) after ${stream_duration}s at $(date)" >&2

        # Reset backoff if the stream ran for more than a minute (healthy run)
        if [ "${stream_duration}" -ge 60 ]; then
            backoff=${RESTART_BACKOFF}
        fi

        echo "[ffmpeg] Restarting in ${backoff}s..." >&2
        sleep "${backoff}"
        backoff=$(( backoff * 2 ))
        [ "${backoff}" -gt "${MAX_BACKOFF}" ] && backoff=${MAX_BACKOFF}
    done
) &

# ---------------------------------------------------------------------------
# 5. Browser watchdog: restart Chrome if it exits unexpectedly
# ---------------------------------------------------------------------------
(
    while true; do
        sleep 15
        if [ -f "${BROWSER_PID_FILE}" ]; then
            BROWSER_PID=$(cat "${BROWSER_PID_FILE}")
            if ! kill -0 "${BROWSER_PID}" 2>/dev/null; then
                echo "[browser-watch] Chrome exited — restarting..." >&2
                launch_browser
            fi
        fi
    done
) &

# ---------------------------------------------------------------------------
# 6. Forward logs to container stdout and keep the container alive
# ---------------------------------------------------------------------------
echo "[startup] Pipeline running. Tailing logs..." >&2
exec sh -c '
    while true; do
        echo "[log] --- $(date -Iseconds) ---"
        for f in /tmp/ffmpeg.log /tmp/channel-browser.log /tmp/xvfb.log /tmp/pulseaudio.log; do
            [ -f "$f" ] || continue
            echo "[log] === $(basename "$f") ==="
            tail -n 20 "$f" | sed "s|^|  |"
        done
        echo "[log] ---------------------------"
        sleep 10
    done
'
