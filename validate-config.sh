#!/bin/bash
set -euo pipefail

fail() {
    echo "[config] $1" >&2
    exit 1
}

[ -n "${PROGRAM_ID:-}" ] || fail "PROGRAM_ID is required"
[ -n "${CHANNEL_BROWSER_URL:-}" ] || fail "CHANNEL_BROWSER_URL is required"

if [ -z "${RTMP_OUTPUTS:-}" ] && [ -z "${YOUTUBE_STREAM_KEY:-}" ]; then
    fail "RTMP_OUTPUTS or YOUTUBE_STREAM_KEY is required"
fi

case "${LIVEKIT_ENABLED:-0}" in
    0|1) ;;
    *) fail "LIVEKIT_ENABLED must be 0 or 1" ;;
esac

if [ "${LIVEKIT_ENABLED:-0}" = "1" ]; then
    [ -n "${LIVEKIT_URL:-}" ] || fail "LIVEKIT_URL is required when LiveKit is enabled"
    [ -n "${LIVEKIT_API_KEY:-}" ] || fail "LIVEKIT_API_KEY is required when LiveKit is enabled"
    [ -n "${LIVEKIT_API_SECRET:-}" ] || fail "LIVEKIT_API_SECRET is required when LiveKit is enabled"
    [ -n "${LIVEKIT_ROOM:-}" ] || fail "LIVEKIT_ROOM is required when LiveKit is enabled"
fi

for name in STALL_TIMEOUT RESTART_BACKOFF MAX_BACKOFF HEALTHY_RESET_SECONDS; do
    value="${!name:-}"
    [[ "${value}" =~ ^[1-9][0-9]*$ ]] || fail "${name} must be a positive integer"
done

case "${ALLOW_SOFTWARE_FALLBACK:-0}" in
    0|1) ;;
    *) fail "ALLOW_SOFTWARE_FALLBACK must be 0 or 1" ;;
esac

if [ "${ALANA_VALIDATE_ONLY:-0}" = "1" ]; then
    echo "[config] valid"
    exit 0
fi
