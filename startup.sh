#!/bin/bash
# OBS-less streaming entrypoint
# Pipeline: Xvfb :98 → Chrome (renders CHANNEL_BROWSER_URL) → ffmpeg x11grab+pulse → RTMPS → YouTube

CHANNEL_BROWSER_URL="${CHANNEL_BROWSER_URL:-https://example.com/live}"
DISPLAY_NUM=":98"
RESOLUTION="${RESOLUTION:-1920x1080}"
WINDOW_SIZE="${WINDOW_SIZE:-${RESOLUTION}}"
WINDOW_WIDTH="${WINDOW_SIZE%x*}"
WINDOW_HEIGHT="${WINDOW_SIZE#*x}"
FPS="${FPS:-30}"
DRAW_MOUSE="${DRAW_MOUSE:-0}"
VIDEO_BITRATE="${VIDEO_BITRATE:-6000k}"
VIDEO_MAXRATE="${VIDEO_MAXRATE:-6000k}"
VIDEO_BUFSIZE="${VIDEO_BUFSIZE:-12000k}"
X264_PRESET="${X264_PRESET:-veryfast}"
GOP_SIZE="${GOP_SIZE:-60}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
VIDEO_ENCODER="${VIDEO_ENCODER:-libx264}"  # libx264 | h264_vaapi
VAAPI_DEVICE="${VAAPI_DEVICE:-}"
VAAPI_DRIVER="${VAAPI_DRIVER:-}"
STREAM_MODE="${STREAM_MODE:-youtube}"  # youtube | icecast
ICECAST_HOST="${ICECAST_HOST:-localhost}"
ICECAST_PORT="${ICECAST_PORT:-8000}"
ICECAST_MOUNT="${ICECAST_MOUNT:-/stream}"
ICECAST_SOURCE_PASSWORD="${ICECAST_SOURCE_PASSWORD:-hackme}"
ICECAST_CODEC="${ICECAST_CODEC:-mp3}"  # mp3 | aac | opus
ICECAST_BITRATE="${ICECAST_BITRATE:-128k}"
ICECAST_STREAM_NAME="${ICECAST_STREAM_NAME:-Alana Radio}"
DISABLE_CHROME_GPU="${DISABLE_CHROME_GPU:-0}"
CHROME_SOFTWARE_GL="${CHROME_SOFTWARE_GL:-0}"
CHROME_DISABLE_DEV_SHM_USAGE="${CHROME_DISABLE_DEV_SHM_USAGE:-0}"
CHROME_ENABLE_PERF_FLAGS="${CHROME_ENABLE_PERF_FLAGS:-0}"
CHROME_EXTRA_FLAGS="${CHROME_EXTRA_FLAGS:-}"
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-warning}"
YOUTUBE_RTMPS_URL="rtmps://a.rtmps.youtube.com:443/live2"
STALL_TIMEOUT=30   # seconds without frame progress → restart ffmpeg
RESTART_BACKOFF=5  # initial backoff between ffmpeg restarts (seconds)
MAX_BACKOFF=60     # maximum backoff cap (seconds)

BROWSER_LOG="/tmp/channel-browser.log"
BROWSER_PID_FILE="/tmp/channel-browser.pid"
FFMPEG_LOG="/tmp/ffmpeg.log"
FFMPEG_PROGRESS="/tmp/ffmpeg-progress.txt"
FFMPEG_PID_FILE="/tmp/ffmpeg.pid"

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
    local chrome_gpu_flag=()
    local chrome_shm_flag=()
    local chrome_perf_flags=()
    local chrome_extra_flags=()
    local chrome_env=(DISPLAY="${DISPLAY_NUM}" GTK_A11Y=none)
    if [ "${DISABLE_CHROME_GPU}" = "1" ]; then
        chrome_gpu_flag+=(--disable-gpu)
    fi
    if [ "${CHROME_SOFTWARE_GL}" = "1" ]; then
        chrome_env+=(LIBGL_ALWAYS_SOFTWARE=1)
    fi
    if [ "${CHROME_DISABLE_DEV_SHM_USAGE}" = "1" ]; then
        chrome_shm_flag+=(--disable-dev-shm-usage)
    fi
    if [ "${CHROME_ENABLE_PERF_FLAGS}" = "1" ]; then
        chrome_perf_flags+=(
            --disable-background-timer-throttling
            --disable-backgrounding-occluded-windows
            --disable-frame-rate-limit
            --disable-gpu-vsync
            --enable-gpu-rasterization
            --enable-zero-copy
            --ignore-gpu-blocklist
        )
    fi
    if [ -n "${CHROME_EXTRA_FLAGS}" ]; then
        # shellcheck disable=SC2206
        chrome_extra_flags=(${CHROME_EXTRA_FLAGS})
    fi

    rm -rf /tmp/chrome-profile
    mkdir -p /tmp/chrome-profile
    echo "[browser] Starting Chrome → ${CHANNEL_BROWSER_URL} on ${DISPLAY_NUM}" >&2
    env "${chrome_env[@]}" \
        google-chrome \
            --no-sandbox \
            "${chrome_gpu_flag[@]}" \
            "${chrome_shm_flag[@]}" \
            "${chrome_perf_flags[@]}" \
            "${chrome_extra_flags[@]}" \
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
            --window-size="${WINDOW_WIDTH},${WINDOW_HEIGHT}" \
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
    local encoder="${ACTIVE_VIDEO_ENCODER:-${VIDEO_ENCODER}}"
    local render_node="${VAAPI_DEVICE}"
    local -a video_args
    local -a ffmpeg_env=()

    if [ -n "${VAAPI_DRIVER}" ]; then
        ffmpeg_env+=(LIBVA_DRIVER_NAME="${VAAPI_DRIVER}")
    fi

    if [ "${encoder}" = "h264_vaapi" ]; then
        if [ -z "${render_node}" ]; then
            render_node=$(ls /dev/dri/renderD* 2>/dev/null | head -n 1)
        fi
        if [ -n "${render_node}" ]; then
            echo "[ffmpeg] Using h264_vaapi on ${render_node}" >&2
            video_args=(
                -vaapi_device "${render_node}"
                -vf "format=nv12,hwupload"
                -c:v h264_vaapi
                -b:v "${VIDEO_BITRATE}"
                -maxrate "${VIDEO_MAXRATE}"
                -bufsize "${VIDEO_BUFSIZE}"
                -g "${GOP_SIZE}"
                -keyint_min "${GOP_SIZE}"
            )
        else
            echo "[ffmpeg] VIDEO_ENCODER=h264_vaapi requested, but no /dev/dri/renderD* found; falling back to libx264" >&2
        fi
    fi

    if [ "${#video_args[@]}" -eq 0 ]; then
        video_args=(
            -c:v libx264
            -preset "${X264_PRESET}"
            -tune zerolatency
            -b:v "${VIDEO_BITRATE}"
            -maxrate "${VIDEO_MAXRATE}"
            -bufsize "${VIDEO_BUFSIZE}"
            -g "${GOP_SIZE}"
            -keyint_min "${GOP_SIZE}"
            -sc_threshold 0
        )
    fi

    rm -f "${FFMPEG_PROGRESS}"
    env "${ffmpeg_env[@]}" ffmpeg \
        -loglevel "${FFMPEG_LOGLEVEL}" \
        -thread_queue_size 1024 \
        -f x11grab -draw_mouse "${DRAW_MOUSE}" -video_size "${RESOLUTION}" -framerate "${FPS}" -i "${DISPLAY_NUM}" \
        -thread_queue_size 1024 \
        -f pulse -i stream_out.monitor \
        "${video_args[@]}" \
        -c:a aac \
        -b:a "${AUDIO_BITRATE}" \
        -ar 44100 \
        -ac 2 \
        -f flv \
        -progress "${FFMPEG_PROGRESS}" \
        "${YOUTUBE_RTMPS_URL}/${YOUTUBE_STREAM_KEY}" \
        >>"${FFMPEG_LOG}" 2>&1 &
    FFMPEG_PID=$!
    echo "${FFMPEG_PID}" > "${FFMPEG_PID_FILE}"
}

stop_ffmpeg_if_running() {
    # Primary: stop the tracked ffmpeg PID if present.
    if [ -f "${FFMPEG_PID_FILE}" ]; then
        old_pid=$(cat "${FFMPEG_PID_FILE}" 2>/dev/null || true)
        if [ -n "${old_pid}" ] && kill -0 "${old_pid}" 2>/dev/null; then
            kill "${old_pid}" 2>/dev/null || true
            sleep 1
            kill -9 "${old_pid}" 2>/dev/null || true
        fi
        rm -f "${FFMPEG_PID_FILE}"
    fi

    # Secondary safety net: remove stale duplicate ffmpeg encoders.
    pgrep -f "ffmpeg .* stream_out.monitor" 2>/dev/null | while read -r pid; do
        [ -n "${pid}" ] || continue
        kill "${pid}" 2>/dev/null || true
        sleep 0.2
        kill -9 "${pid}" 2>/dev/null || true
    done
}

stall_watchdog() {
    local pid=$1
    local last_frame=0
    local last_time_us=0
    local last_advance
    last_advance=$(date +%s)

    while kill -0 "${pid}" 2>/dev/null; do
        sleep 5
        if [ -f "${FFMPEG_PROGRESS}" ]; then
            # For video streams track frame=; for audio-only streams track out_time_us
            local frame
            frame=$(grep "^frame=" "${FFMPEG_PROGRESS}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
            local time_us
            time_us=$(grep "^out_time_us=" "${FFMPEG_PROGRESS}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
            if { [ -n "${frame}" ] && [ "${frame}" != "${last_frame}" ]; } || \
               { [ -n "${time_us}" ] && [ "${time_us}" != "${last_time_us}" ] && [ "${time_us}" != "0" ]; }; then
                last_frame="${frame:-${last_frame}}"
                last_time_us="${time_us:-${last_time_us}}"
                last_advance=$(date +%s)
            fi
        fi
        local elapsed=$(( $(date +%s) - last_advance ))
        if [ "${elapsed}" -ge "${STALL_TIMEOUT}" ]; then
            echo "[ffmpeg-watch] No progress for ${elapsed}s — killing PID ${pid}" >&2
            kill -9 "${pid}" 2>/dev/null || true
            return
        fi
    done
}

# ---------------------------------------------------------------------------
# 4b. ffmpeg: PulseAudio only → MP3/AAC/Opus → Icecast
# ---------------------------------------------------------------------------
start_ffmpeg_icecast() {
    local codec="${ICECAST_CODEC:-mp3}"
    local bitrate="${ICECAST_BITRATE:-128k}"
    local -a audio_args
    local content_type

    case "${codec}" in
        mp3)
            audio_args=(-c:a libmp3lame -b:a "${bitrate}" -ar 44100 -ac 2 -f mp3)
            content_type="audio/mpeg"
            ;;
        aac)
            audio_args=(-c:a aac -b:a "${bitrate}" -ar 44100 -ac 2 -f adts)
            content_type="audio/aac"
            ;;
        opus)
            audio_args=(-c:a libopus -b:a "${bitrate}" -ar 48000 -ac 2 -vbr on -f ogg)
            content_type="audio/ogg"
            ;;
        *)
            echo "[ffmpeg-icecast] Unknown codec '${codec}', defaulting to mp3" >&2
            audio_args=(-c:a libmp3lame -b:a "${bitrate}" -ar 44100 -ac 2 -f mp3)
            content_type="audio/mpeg"
            ;;
    esac

    local icecast_uri="icecast://source:${ICECAST_SOURCE_PASSWORD}@${ICECAST_HOST}:${ICECAST_PORT}${ICECAST_MOUNT}"
    echo "[ffmpeg-icecast] Streaming ${codec}@${bitrate} → ${icecast_uri}" >&2

    rm -f "${FFMPEG_PROGRESS}"
    ffmpeg \
        -loglevel "${FFMPEG_LOGLEVEL}" \
        -thread_queue_size 128 \
        -f pulse -i stream_out.monitor \
        "${audio_args[@]}" \
        -content_type "${content_type}" \
        -ice_name "${ICECAST_STREAM_NAME}" \
        -progress "${FFMPEG_PROGRESS}" \
        "${icecast_uri}" \
        >>"${FFMPEG_LOG}" 2>&1 &
    FFMPEG_PID=$!
    echo "${FFMPEG_PID}" > "${FFMPEG_PID_FILE}"
}

backoff=${RESTART_BACKOFF}
ACTIVE_VIDEO_ENCODER="${VIDEO_ENCODER}"
(
    while true; do
        if [ "${STREAM_MODE}" = "icecast" ]; then
            # ---- Icecast (audio-only) supervisor ----
            if [ -z "${ICECAST_SOURCE_PASSWORD:-}" ]; then
                echo "[ffmpeg-icecast] ICECAST_SOURCE_PASSWORD is not set; retrying in 30s..." >&2
                sleep 30
                continue
            fi

            echo "[ffmpeg-icecast] Starting at $(date)" >&2
            stop_ffmpeg_if_running
            stream_start=$(date +%s)
            start_ffmpeg_icecast
        else
            # ---- YouTube (video+audio) supervisor ----
            if [ -z "${YOUTUBE_STREAM_KEY:-}" ]; then
                echo "[ffmpeg] YOUTUBE_STREAM_KEY is not set; retrying in 30s..." >&2
                sleep 30
                continue
            fi

            echo "[ffmpeg] Starting at $(date)" >&2
            stop_ffmpeg_if_running
            stream_start=$(date +%s)
            start_ffmpeg
        fi

        stall_watchdog "${FFMPEG_PID}" &
        WATCHER_PID=$!

        wait "${FFMPEG_PID}" 2>/dev/null
        EXIT_CODE=$?
        kill "${WATCHER_PID}" 2>/dev/null || true

        stream_duration=$(( $(date +%s) - stream_start ))
        echo "[ffmpeg] Exited (code ${EXIT_CODE}) after ${stream_duration}s at $(date)" >&2

        # If VAAPI fails immediately, fall back to libx264 automatically (YouTube mode only).
        if [ "${STREAM_MODE}" != "icecast" ] && \
           [ "${ACTIVE_VIDEO_ENCODER}" = "h264_vaapi" ] && \
           [ "${EXIT_CODE}" -ne 0 ] && [ "${stream_duration}" -lt 5 ]; then
            echo "[ffmpeg] VAAPI failed quickly; switching to libx264 fallback." >&2
            ACTIVE_VIDEO_ENCODER="libx264"
            backoff=${RESTART_BACKOFF}
        fi

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
