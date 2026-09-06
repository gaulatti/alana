#!/bin/bash
set -euo pipefail

fail() {
    echo "[config] $1" >&2
    exit 1
}

[ -n "${PROGRAM_ID:-}" ] || fail "PROGRAM_ID is required"
[ -n "${CHANNEL_BROWSER_URL:-}" ] || fail "CHANNEL_BROWSER_URL is required"
[ -n "${CROCCANTE_CONTROL_URL:-}" ] || fail "CROCCANTE_CONTROL_URL is required"

case "${CROCCANTE_CONTROL_URL}" in
    http://*|https://*) ;;
    *) fail "CROCCANTE_CONTROL_URL must use http or https" ;;
esac
case "${CROCCANTE_CONTROL_URL}" in
    *@*|*\?*|*\#*) fail "CROCCANTE_CONTROL_URL must not contain credentials, query, or fragment" ;;
esac

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

for name in STALL_TIMEOUT RESTART_BACKOFF MAX_BACKOFF HEALTHY_RESET_SECONDS PIPELINE_READY_TIMEOUT CONTROL_RETRY_SECONDS; do
    value="${!name:-}"
    [[ "${value}" =~ ^[1-9][0-9]*$ ]] || fail "${name} must be a positive integer"
done

for name in ALANA_CONTROL_TOKEN_FILE CROCCANTE_CONTROL_TOKEN_FILE; do
    path="${!name:-}"
    [ -n "${path}" ] || fail "${name} is required"
    [ -s "${path}" ] || fail "${name} must reference a readable non-empty file"
done

case "${ALLOW_SOFTWARE_FALLBACK:-0}" in
    0|1) ;;
    *) fail "ALLOW_SOFTWARE_FALLBACK must be 0 or 1" ;;
esac

case "${RECORDING_ENABLED:-0}" in
    0|1) ;;
    *) fail "RECORDING_ENABLED must be 0 or 1" ;;
esac

for setting in RECORDING_SEGMENT_SECONDS:5 RECORDING_QUOTA_BYTES:53687091200 \
    RECORDING_MIN_FREE_BYTES:1073741824 RECORDING_RETENTION_HOURS:168 \
    RECORDING_MAX_RESTARTS:5 RECORDING_RESTART_BACKOFF_SECONDS:2 \
    RECORDING_START_TIMEOUT:10 RECORDING_FINALIZE_TIMEOUT:120; do
    name="${setting%%:*}"
    default="${setting#*:}"
    value="${!name-}"
    [ -n "${value}" ] || value="${default}"
    [[ "${value}" =~ ^[1-9][0-9]*$ ]] || fail "${name} must be a positive integer"
done

recording_minimum="${RECORDING_MIN_FREE_BYTES:-1073741824}"
recording_quota="${RECORDING_QUOTA_BYTES:-53687091200}"
if [ "${recording_minimum}" -ge "${recording_quota}" ]; then
    fail "RECORDING_MIN_FREE_BYTES must be smaller than RECORDING_QUOTA_BYTES"
fi

if [ "${ALANA_VALIDATE_ONLY:-0}" = "1" ]; then
    echo "[config] valid"
    exit 0
fi
