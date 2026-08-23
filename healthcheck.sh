#!/bin/bash
set -euo pipefail

alive_from_file() {
    local path=$1
    [ -s "${path}" ] || return 1
    local pid
    pid=$(cat "${path}")
    kill -0 "${pid}" 2>/dev/null
}

alive_from_file /tmp/channel-browser.pid

shopt -s nullglob
rtmp_pid_files=(/tmp/rtmp-*.pid)
[ "${#rtmp_pid_files[@]}" -gt 0 ] || exit 1
for path in "${rtmp_pid_files[@]}"; do
    alive_from_file "${path}"
done

if [ "${LIVEKIT_ENABLED:-0}" = "1" ]; then
    alive_from_file /tmp/livekit.pid
fi
