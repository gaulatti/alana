# Use a single, reliable base image
FROM ubuntu:24.04

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/config
WORKDIR /config

# 1. Install core dependencies: X server, PulseAudio, ffmpeg, and Chrome runtime deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Essential utilities
    ca-certificates \
    curl \
    wget \
    # X Server (single virtual display for Chrome)
    xvfb \
    x11-xserver-utils \
    xserver-xorg-core \
    dbus \
    dbus-x11 \
    # Audio: PulseAudio daemon + control utilities (pactl/pacmd)
    pulseaudio \
    pulseaudio-utils \
    # Streaming encoder
    ffmpeg \
    # Chrome runtime dependencies
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
    libasound2t64 \
    # Fonts for proper webpage rendering
    fonts-noto-core \
    fonts-noto-cjk \
    fontconfig \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Google Chrome (required for rendering the channel webpage)
RUN apt-get update \
    && wget -O /tmp/google-chrome-stable_current_amd64.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/google-chrome-stable_current_amd64.deb \
    && rm -f /tmp/google-chrome-stable_current_amd64.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Add startup script
COPY startup.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/startup.sh

# Expose Chrome DevTools remote debugging port
EXPOSE 9222

# Set the entrypoint to the startup script
ENTRYPOINT ["/usr/local/bin/startup.sh"]
