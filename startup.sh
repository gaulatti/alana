#!/bin/bash
set -uo pipefail

CHANNEL_BROWSER_URL="${CHANNEL_BROWSER_URL:-}"
PROGRAM_ID="${PROGRAM_ID:-}"
DISPLAY_NUM="${DISPLAY_NUM:-:98}"
RESOLUTION="${RESOLUTION:-1920x1080}"
WINDOW_SIZE="${WINDOW_SIZE:-${RESOLUTION}}"
FPS="${FPS:-30}"
DRAW_MOUSE="${DRAW_MOUSE:-0}"
VIDEO_BITRATE="${VIDEO_BITRATE:-6000k}"
VIDEO_MAXRATE="${VIDEO_MAXRATE:-6000k}"
VIDEO_BUFSIZE="${VIDEO_BUFSIZE:-12000k}"
X264_PRESET="${X264_PRESET:-veryfast}"
GOP_SIZE="${GOP_SIZE:-60}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
VIDEO_ENCODER="${VIDEO_ENCODER:-libx264}"
VAAPI_DEVICE="${VAAPI_DEVICE:-}"
VAAPI_DRIVER="${VAAPI_DRIVER:-}"
RTMP_OUTPUTS="${RTMP_OUTPUTS:-}"
YOUTUBE_RTMP_URL="rtmp://a.rtmp.youtube.com/live2"
LIVEKIT_ENABLED="${LIVEKIT_ENABLED:-0}"
LIVEKIT_URL="${LIVEKIT_URL:-}"
LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-}"
LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-}"
LIVEKIT_ROOM="${LIVEKIT_ROOM:-}"
LIVEKIT_IDENTITY="${LIVEKIT_IDENTITY:-program-feed-${PROGRAM_ID}}"
LIVEKIT_METADATA="${LIVEKIT_METADATA:-{\"role\":\"program-feed\",\"programId\":\"${PROGRAM_ID}\"}}"
STALL_TIMEOUT="${STALL_TIMEOUT:-30}"
RESTART_BACKOFF="${RESTART_BACKOFF:-5}"
MAX_BACKOFF="${MAX_BACKOFF:-60}"
HEALTHY_RESET_SECONDS="${HEALTHY_RESET_SECONDS:-60}"
ALLOW_SOFTWARE_FALLBACK="${ALLOW_SOFTWARE_FALLBACK:-0}"
BROWSER_WAIT_FOR_URL="${BROWSER_WAIT_FOR_URL:-1}"
BROWSER_PID_FILE=/tmp/channel-browser.pid
LIVEKIT_PID_FILE=/tmp/livekit.pid
LIVEKIT_PROGRESS=/tmp/livekit-progress.txt
LIVEKIT_VIDEO_SOCKET=/tmp/alana-program-video.sock
LIVEKIT_AUDIO_SOCKET=/tmp/alana-program-audio.sock

# shellcheck source=validate-config.sh
source /usr/local/bin/validate-config.sh
# validate-config.sh is also executable on its own and enables errexit there.
# Supervisors must inspect child exit codes instead of inheriting that option.
set +e

background_pids=()
stop_pid_file() {
    [ -s "$1" ] || return 0
    pid=$(cat "$1" 2>/dev/null || true)
    [ -n "${pid}" ] && kill "${pid}" 2>/dev/null || true
    unlink "$1" 2>/dev/null || true
}
cleanup() {
    trap - EXIT INT TERM
    for pid in "${background_pids[@]:-}"; do kill "${pid}" 2>/dev/null || true; done
    stop_pid_file "${BROWSER_PID_FILE}"
    stop_pid_file "${LIVEKIT_PID_FILE}"
    for path in /tmp/rtmp-*.pid; do [ -e "${path}" ] && stop_pid_file "${path}"; done
    kill "${XVFB_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    unlink "${LIVEKIT_VIDEO_SOCKET}" "${LIVEKIT_AUDIO_SOCKET}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

get_pulse_socket() { pactl info 2>/dev/null | sed -n 's/Server String: //p'; }
get_rtmp_outputs() {
    if [ -n "${RTMP_OUTPUTS}" ]; then
        printf '%s\n' "${RTMP_OUTPUTS}" | tr ',[:space:]' '\n' | sed '/^$/d'
    else
        printf '%s/%s\n' "${YOUTUBE_RTMP_URL}" "${YOUTUBE_STREAM_KEY}"
    fi
}

mkdir -p /run/dbus /tmp/runtime-root
chmod 700 /tmp/runtime-root
export XDG_RUNTIME_DIR=/tmp/runtime-root
dbus-daemon --system --fork 2>/dev/null || true
pulseaudio --start --exit-idle-time=-1 --log-target=file:/tmp/pulseaudio.log 2>/dev/null || true
for _ in 1 2 3 4 5; do pactl info >/dev/null 2>&1 && break; sleep 1; done
pactl load-module module-null-sink sink_name=stream_out sink_properties=device.description=stream_out >/dev/null 2>&1 || true
pactl set-default-sink stream_out 2>/dev/null || true

Xvfb "${DISPLAY_NUM}" -screen 0 "${RESOLUTION}x24" +extension RANDR +extension MIT-SHM +extension XINERAMA >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
export DISPLAY="${DISPLAY_NUM}"
until xdpyinfo >/dev/null 2>&1; do sleep 0.2; done

launch_browser() {
    local browser_binary
    local -a flags chrome_env extra_flags
    browser_binary="${BROWSER_BINARY:-}"
    [ -n "${browser_binary}" ] || browser_binary=$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)
    [ -n "${browser_binary}" ] || { echo "[browser] Chromium unavailable" >&2; return 1; }
    stop_pid_file "${BROWSER_PID_FILE}"
    [ "${BROWSER_WAIT_FOR_URL}" != 1 ] || until curl -fsS --max-time 3 -o /dev/null "${CHANNEL_BROWSER_URL}" >/dev/null 2>&1; do sleep 2; done
    flags=(--no-sandbox --disable-background-networking --disable-default-apps --disable-renderer-backgrounding --disable-sync --hide-scrollbars --incognito --kiosk --no-first-run --test-type --autoplay-policy=no-user-gesture-required)
    chrome_env=(DISPLAY="${DISPLAY_NUM}" GTK_A11Y=none)
    [ "${DISABLE_CHROME_GPU:-0}" = 1 ] && flags+=(--disable-gpu)
    [ "${CHROME_DISABLE_DEV_SHM_USAGE:-0}" = 1 ] && flags+=(--disable-dev-shm-usage)
    [ "${CHROME_SOFTWARE_GL:-0}" = 1 ] && chrome_env+=(LIBGL_ALWAYS_SOFTWARE=1)
    if [ "${CHROME_ENABLE_PERF_FLAGS:-0}" = 1 ]; then
        flags+=(--disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-frame-rate-limit --disable-gpu-vsync --enable-gpu-rasterization --enable-zero-copy --ignore-gpu-blocklist)
    fi
    if [ -n "${CHROME_EXTRA_FLAGS:-}" ]; then
        # shellcheck disable=SC2206
        extra_flags=(${CHROME_EXTRA_FLAGS})
        flags+=("${extra_flags[@]}")
    fi
    # Chromium diagnostics can repeat a renderer URL that contains credentials.
    env "${chrome_env[@]}" "${browser_binary}" "${flags[@]}" --window-size="${WINDOW_SIZE/x/,}" --user-data-dir=/tmp/chrome-profile "${CHANNEL_BROWSER_URL}" >/dev/null 2>&1 &
    echo $! >"${BROWSER_PID_FILE}"
}
launch_browser
sleep 5

stall_watchdog() {
    pid=$1 progress=$2 label=$3 last=0 advanced=$(date +%s)
    while kill -0 "${pid}" 2>/dev/null; do
        sleep 5
        frame=$(grep '^frame=' "${progress}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
        if [ -n "${frame}" ] && [ "${frame}" != "${last}" ]; then last="${frame}"; advanced=$(date +%s); fi
        if [ $(( $(date +%s) - advanced )) -ge "${STALL_TIMEOUT}" ]; then
            echo "[${label}] stalled; restarting" >&2
            kill -9 "${pid}" 2>/dev/null || true
            return
        fi
    done
}

video_args() {
    local selected_encoder=$1 render_node
    case "${selected_encoder}" in
        h264_nvenc) VIDEO_ARGS=(-c:v h264_nvenc -preset p4 -tune ll -b:v "${VIDEO_BITRATE}" -maxrate "${VIDEO_MAXRATE}" -bufsize "${VIDEO_BUFSIZE}" -g "${GOP_SIZE}") ;;
        h264_vaapi)
            render_node="${VAAPI_DEVICE:-$(find /dev/dri -maxdepth 1 -name 'renderD*' -print -quit 2>/dev/null || true)}"
            [ -n "${render_node}" ] || return 1
            VIDEO_ARGS=(-vaapi_device "${render_node}" -vf "format=nv12,hwupload" -c:v h264_vaapi -b:v "${VIDEO_BITRATE}" -maxrate "${VIDEO_MAXRATE}" -bufsize "${VIDEO_BUFSIZE}" -g "${GOP_SIZE}")
            ;;
        libx264) VIDEO_ARGS=(-c:v libx264 -preset "${X264_PRESET}" -tune zerolatency -b:v "${VIDEO_BITRATE}" -maxrate "${VIDEO_MAXRATE}" -bufsize "${VIDEO_BUFSIZE}" -g "${GOP_SIZE}" -sc_threshold 0) ;;
        *) return 1 ;;
    esac
}

rtmp_supervisor() (
    local index=$1 output=$2 selected_encoder=$3 backoff="${RESTART_BACKOFF}"
    local pid_file progress rtmp_child rtmp_watcher started code duration
    pid_file="/tmp/rtmp-${index}.pid"; progress="/tmp/rtmp-progress-${index}.txt"
    trap 'kill "${rtmp_child:-}" "${rtmp_watcher:-}" 2>/dev/null || true; unlink "${pid_file}" 2>/dev/null || true' EXIT INT TERM
    while true; do
        if ! video_args "${selected_encoder}"; then
            if [ "${ALLOW_SOFTWARE_FALLBACK}" = 1 ] && [ "${selected_encoder}" != libx264 ]; then
                echo "[rtmp-${index}] explicit software fallback" >&2; selected_encoder=libx264; continue
            fi
            echo "[rtmp-${index}] encoder unavailable" >&2; sleep "${MAX_BACKOFF}"; continue
        fi
        unlink "${progress}" 2>/dev/null || true
        started=$(date +%s)
        # Never retain ffmpeg diagnostics: connection errors can repeat stream keys.
        env PULSE_SERVER="$(get_pulse_socket)" LIBVA_DRIVER_NAME="${VAAPI_DRIVER}" ffmpeg -hide_banner -loglevel quiet -thread_queue_size 1024 \
            -f x11grab -draw_mouse "${DRAW_MOUSE}" -video_size "${RESOLUTION}" -framerate "${FPS}" -i "${DISPLAY_NUM}" \
            -thread_queue_size 1024 -f pulse -i stream_out.monitor -map 0:v:0 -map 1:a:0 "${VIDEO_ARGS[@]}" \
            -c:a aac -b:a "${AUDIO_BITRATE}" -ar 44100 -ac 2 -f flv -progress "${progress}" "${output}" >/dev/null 2>&1 &
        rtmp_child=$!; echo "${rtmp_child}" >"${pid_file}"
        stall_watchdog "${rtmp_child}" "${progress}" "rtmp-${index}" & rtmp_watcher=$!
        wait "${rtmp_child}" 2>/dev/null; code=$?; kill "${rtmp_watcher}" 2>/dev/null || true; unlink "${pid_file}" 2>/dev/null || true
        duration=$(( $(date +%s) - started ))
        echo "[rtmp-${index}] exited code=${code} duration=${duration}s retry=${backoff}s" >&2
        if [ "${ALLOW_SOFTWARE_FALLBACK}" = 1 ] && [ "${selected_encoder}" != libx264 ] && [ "${duration}" -lt "${HEALTHY_RESET_SECONDS}" ]; then
            echo "[rtmp-${index}] explicit software fallback after encoder failure" >&2
            selected_encoder=libx264
        fi
        [ "${duration}" -ge "${HEALTHY_RESET_SECONDS}" ] && backoff="${RESTART_BACKOFF}"
        sleep "${backoff}"; backoff=$((backoff * 2)); [ "${backoff}" -gt "${MAX_BACKOFF}" ] && backoff="${MAX_BACKOFF}"
    done
)

livekit_once() (
    local lk_encoder publisher
    trap 'kill "${lk_encoder:-}" "${publisher:-}" 2>/dev/null || true; unlink "${LIVEKIT_VIDEO_SOCKET}" "${LIVEKIT_AUDIO_SOCKET}" 2>/dev/null || true' EXIT INT TERM
    unlink "${LIVEKIT_PROGRESS}" "${LIVEKIT_VIDEO_SOCKET}" "${LIVEKIT_AUDIO_SOCKET}" 2>/dev/null || true
    env PULSE_SERVER="$(get_pulse_socket)" ffmpeg -hide_banner -loglevel quiet -thread_queue_size 1024 \
        -f x11grab -draw_mouse "${DRAW_MOUSE}" -video_size "${RESOLUTION}" -framerate "${FPS}" -i "${DISPLAY_NUM}" \
        -thread_queue_size 1024 -f pulse -i stream_out.monitor -map 0:v:0 -an -c:v libx264 -preset "${X264_PRESET}" \
        -tune zerolatency -profile:v baseline -pix_fmt yuv420p -bf 0 -b:v "${VIDEO_BITRATE}" -g "${GOP_SIZE}" \
        -bsf:v h264_mp4toannexb -listen 1 -f h264 -progress "${LIVEKIT_PROGRESS}" "unix:${LIVEKIT_VIDEO_SOCKET}" \
        -map 1:a:0 -vn -c:a libopus -ar 48000 -ac 2 -application lowdelay -listen 1 -f opus "unix:${LIVEKIT_AUDIO_SOCKET}" >/dev/null 2>&1 &
    lk_encoder=$!
    for _ in $(seq 1 300); do [ -S "${LIVEKIT_VIDEO_SOCKET}" ] && break; kill -0 "${lk_encoder}" 2>/dev/null || exit 1; sleep 0.1; done
    [ -S "${LIVEKIT_VIDEO_SOCKET}" ] || exit 1
    # Credentials stay in the environment instead of the process argument list.
    lk room join --identity "${LIVEKIT_IDENTITY}" --metadata "${LIVEKIT_METADATA}" \
        --publish "h264://${LIVEKIT_VIDEO_SOCKET}" --publish "opus://${LIVEKIT_AUDIO_SOCKET}" \
        --fps "${FPS}" --auto-subscribe=false "${LIVEKIT_ROOM}" >/dev/null 2>&1 &
    publisher=$!
    wait -n "${lk_encoder}" "${publisher}"
)

livekit_supervisor() (
    local backoff="${RESTART_BACKOFF}" lk_child lk_watcher started code duration
    trap 'kill "${lk_child:-}" "${lk_watcher:-}" 2>/dev/null || true; unlink "${LIVEKIT_PID_FILE}" 2>/dev/null || true' EXIT INT TERM
    while true; do
        started=$(date +%s); livekit_once & lk_child=$!; echo "${lk_child}" >"${LIVEKIT_PID_FILE}"
        stall_watchdog "${lk_child}" "${LIVEKIT_PROGRESS}" livekit & lk_watcher=$!
        wait "${lk_child}" 2>/dev/null; code=$?; kill "${lk_watcher}" 2>/dev/null || true; unlink "${LIVEKIT_PID_FILE}" 2>/dev/null || true
        duration=$(( $(date +%s) - started )); echo "[livekit] exited code=${code} duration=${duration}s retry=${backoff}s" >&2
        [ "${duration}" -ge "${HEALTHY_RESET_SECONDS}" ] && backoff="${RESTART_BACKOFF}"
        sleep "${backoff}"; backoff=$((backoff * 2)); [ "${backoff}" -gt "${MAX_BACKOFF}" ] && backoff="${MAX_BACKOFF}"
    done
)

case "${GPU_TYPE:-}" in NVIDIA) rtmp_encoder=h264_nvenc ;; INTEL) rtmp_encoder=h264_vaapi ;; *) rtmp_encoder="${VIDEO_ENCODER}" ;; esac
mapfile -t outputs < <(get_rtmp_outputs)
for index in "${!outputs[@]}"; do rtmp_supervisor "$((index + 1))" "${outputs[index]}" "${rtmp_encoder}" & background_pids+=($!); done
[ "${LIVEKIT_ENABLED}" != 1 ] || { livekit_supervisor & background_pids+=($!); }

( while true; do
    sleep 15
    if ! [ -s "${BROWSER_PID_FILE}" ] || ! kill -0 "$(cat "${BROWSER_PID_FILE}")" 2>/dev/null; then launch_browser; fi
done ) & background_pids+=($!)
( while true; do sleep 15; pactl info >/dev/null 2>&1 || { pulseaudio --start --exit-idle-time=-1 2>/dev/null || true; sleep 2; pactl load-module module-null-sink sink_name=stream_out >/dev/null 2>&1 || true; pactl set-default-sink stream_out 2>/dev/null || true; }; done ) & background_pids+=($!)

echo "[startup] program=${PROGRAM_ID} rtmp_outputs=${#outputs[@]} livekit_enabled=${LIVEKIT_ENABLED}" >&2
while true; do
    sleep 60
done
