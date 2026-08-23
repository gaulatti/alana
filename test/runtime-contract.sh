#!/bin/bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

bash -n startup.sh validate-config.sh healthcheck.sh

valid_env=(
    PROGRAM_ID=test-program
    CHANNEL_BROWSER_URL=http://renderer.test/program
    RTMP_OUTPUTS=rtmp://relay.test/live/redacted
    LIVEKIT_ENABLED=0
    STALL_TIMEOUT=30
    RESTART_BACKOFF=5
    MAX_BACKOFF=60
    HEALTHY_RESET_SECONDS=60
    ALLOW_SOFTWARE_FALLBACK=0
    ALANA_VALIDATE_ONLY=1
)
env "${valid_env[@]}" bash validate-config.sh | grep -q '\[config\] valid'

if env "${valid_env[@]}" PROGRAM_ID= bash validate-config.sh >/dev/null 2>&1; then
    echo "missing PROGRAM_ID unexpectedly passed" >&2
    exit 1
fi

if env "${valid_env[@]}" LIVEKIT_ENABLED=1 bash validate-config.sh >/dev/null 2>&1; then
    echo "incomplete enabled LiveKit unexpectedly passed" >&2
    exit 1
fi

env "${valid_env[@]}" LIVEKIT_ENABLED=1 LIVEKIT_URL=ws://livekit.test \
    LIVEKIT_API_KEY=test-key LIVEKIT_API_SECRET=test-secret LIVEKIT_ROOM=test-room \
    bash validate-config.sh | grep -q '\[config\] valid'

if rg -n 'STREAM_MODE|ICECAST_|icecast' startup.sh validate-config.sh healthcheck.sh Dockerfile docker-compose.yml .env.example README.md; then
    echo "retired radio path remains" >&2
    exit 1
fi

rg -q 'rtmp_supervisor.*&.*background_pids' startup.sh
rg -q 'livekit_supervisor.*&.*background_pids' startup.sh
rg -q 'LIVEKIT_ENABLED.*!= 1' startup.sh
rg -Fqx 'set +e' startup.sh
rg -q '/tmp/rtmp-\*\.pid' healthcheck.sh
rg -q 'ffmpeg .* -loglevel quiet' startup.sh
if rg -n 'rtmp-.*\.log|livekit-publisher\.log|channel-browser\.log' startup.sh; then
    echo "credential-bearing child-process diagnostics must not be retained" >&2
    exit 1
fi

PROGRAM_ID=test-program CHANNEL_BROWSER_URL=http://renderer.test/program \
RTMP_OUTPUTS=rtmp://relay.test/live/redacted docker compose config --quiet
