# Debian ships native Chromium builds for amd64 production hosts and arm64
# developer machines. Native execution is required for realtime capture.
FROM debian:bookworm-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/config
WORKDIR /config

# 1. Install core dependencies: X server, PulseAudio, ffmpeg, and Chrome runtime deps
RUN architecture="$(dpkg --print-architecture)" \
    && case "${architecture}" in amd64) architecture_packages="intel-media-va-driver" ;; arm64) architecture_packages="" ;; *) echo "Unsupported architecture: ${architecture}" >&2; exit 1 ;; esac \
    && apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xvfb \
    x11-xserver-utils \
    xserver-xorg-core \
    dbus \
    dbus-x11 \
    pulseaudio \
    pulseaudio-utils \
    python3 \
    ffmpeg \
    vainfo \
    libva2 \
    libva-drm2 \
    mesa-va-drivers \
    chromium \
    libnss3 \
    libnspr4 \
    libgbm1 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxext6 \
    libxrandr2 \
    libx11-6 \
    libgl1 \
    libegl1 \
    libxkbcommon-x11-0 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 \
    libgtk-3-0 \
    libasound2 \
    fonts-noto-core \
    fonts-noto-cjk \
    fontconfig \
    ${architecture_packages} \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Used by the optional LiveKit leg to publish ffmpeg's H.264/Opus socket outputs
# directly to a LiveKit room as a normal WebRTC participant.
ARG LIVEKIT_CLI_VERSION=2.18.2
RUN architecture="$(dpkg --print-architecture)" \
    && case "${architecture}" in amd64) lk_arch=amd64 ;; arm64) lk_arch=arm64 ;; *) echo "Unsupported architecture: ${architecture}" >&2; exit 1 ;; esac \
    && archive="lk_${LIVEKIT_CLI_VERSION}_linux_${lk_arch}.tar.gz" \
    && release="https://github.com/livekit/livekit-cli/releases/download/v${LIVEKIT_CLI_VERSION}" \
    && curl --http1.1 --retry 5 --retry-all-errors -fsSL "${release}/${archive}" -o "/tmp/${archive}" \
    && curl --http1.1 --retry 5 --retry-all-errors -fsSL "${release}/checksums.txt" -o /tmp/livekit-checksums.txt \
    && expected="$(awk -v archive="${archive}" '$2 == archive {print $1}' /tmp/livekit-checksums.txt)" \
    && [ -n "${expected}" ] \
    && echo "${expected}  /tmp/${archive}" | sha256sum -c - \
    && tar -xzf "/tmp/${archive}" -C /usr/local/bin lk \
    && chmod +x /usr/local/bin/lk \
    && rm -f "/tmp/${archive}" /tmp/livekit-checksums.txt

# Add runtime scripts
COPY startup.sh validate-config.sh healthcheck.sh control-server.py /usr/local/bin/
RUN chmod +x /usr/local/bin/startup.sh /usr/local/bin/validate-config.sh /usr/local/bin/healthcheck.sh /usr/local/bin/control-server.py

# Expose Chrome DevTools remote debugging port
EXPOSE 8080 9222

HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=3 CMD ["/usr/local/bin/healthcheck.sh"]

# The control server owns the publisher subprocess and its lifecycle.
ENTRYPOINT ["/usr/local/bin/control-server.py"]
