# Use a single, reliable base image
FROM ubuntu:24.04

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/config
# obs-websocket configuration (OBS 28+ has obs-websocket built-in)
ENV OBS_WEBSOCKET_PASSWORD=""
ENV OBS_WEBSOCKET_PORT=4455
# Legacy mode: set to "true" to enable static scene import (deprecated)
ENV OBS_LEGACY_MODE="false"
ENV CHANNEL_BROWSER="chrome"
WORKDIR /config

# 1. Install core dependencies, VNC, X, QSV, and rendering fixes
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # PPA management tools, GPG, and essential utilities
    software-properties-common \
    gnupg \
    dirmngr \
    ca-certificates \
    curl \
    wget \
    # VNC and X Server
    xorg \
    xvfb \
    tigervnc-standalone-server \
    tigervnc-tools \
    x11vnc \
    websockify \
    twm \
    wmctrl \
    xdotool \
    xinit \
    dbus \
    dbus-x11 \
    # Core Qt and Media dependencies
    libmfx1 \                       
    libqt6widgets6 libqt6gui6 libqt6core6 libqt6webenginecore6 libqt6webenginewidgets6 \
    libasound2t64 \
    pulseaudio \
    libnss3 \                   
    # CRITICAL XCB dependencies for Qt GUI:
    libxcb-randr0 \
    libxcb-image0 \
    libxcb-icccm4 \
    # Standalone browser fallback for headless capture
    epiphany-browser \
    # FFMPEG and QSV runtime dependencies
    ffmpeg \
    libva-dev \
    libva-drm2 \
    i965-va-driver \
    mesa-va-drivers \
    va-driver-all \
    vainfo \
    intel-gpu-tools \
    # Additional dependencies for newer intel-media-driver
    libigdgmm12 \
    # Core graphics and Font dependencies for stable Browser Source rendering:
    libx11-6 libgl1 libxrandr2 \
    fonts-noto-core \               
    fonts-noto-cjk \                
    fontconfig \                    
    xfonts-base \                   
    libegl1 \                  
    libxkbcommon-x11-0 \            
    # CEF (Chromium Embedded Framework) runtime deps for obs-browser:
    libgbm1 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxext6 \
    libxrandr2 \
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
    libnspr4 \
    libnss3 \
    \
    # Cleanup
    xterm \
    xserver-xorg-core \
    x11-xserver-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Add PPAs: Intel graphics drivers + OBS official (required for full obs-browser/CEF).
# Ubuntu 24.04 universe ships obs-studio 30.0.2+dfsg which strips CEF for DFSG compliance.
# The OBS PPA ships the complete package with CEF bundled — Browser source requires this.
RUN add-apt-repository ppa:oibaf/graphics-drivers -y && \
    add-apt-repository ppa:obsproject/obs-studio -y

# 3. Install OBS Studio from OBS official PPA (includes obs-browser + CEF)
RUN apt-get update \
    && apt-get install -y \
    obs-studio \
    intel-media-va-driver-non-free \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 3b. Install a standalone browser for the captured channel display.
# Google Chrome is used instead of Ubuntu's snap-backed browsers so it works
# as a normal X11 app inside this container.
RUN apt-get update \
    && wget -O /tmp/google-chrome-stable_current_amd64.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/google-chrome-stable_current_amd64.deb \
    && rm -f /tmp/google-chrome-stable_current_amd64.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 4. Install Media Playlist Source Plugin (Automated via .deb)
RUN apt-get update && \
    # Download the verified .deb package using the correct, specific filename
    wget https://github.com/CodeYan01/media-playlist-source/releases/download/0.1.3/media-playlist-source-0.1.3-x86_64-linux-gnu.deb -O /tmp/media-playlist-source.deb && \
    # Install the package using dpkg. This correctly places files into OBS folders.
    dpkg -i /tmp/media-playlist-source.deb && \
    # Resolve any remaining dependencies needed by the plugin
    apt-get install -fy && \
    # Cleanup
    rm /tmp/media-playlist-source.deb && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 5. Install Advanced Scene Switcher Plugin
# 1.31.0: min OBS 30.1.2, no canvas API dependency — compatible with OBS 31.x/32.x from PPA.
RUN wget https://github.com/WarmUpTill/SceneSwitcher/releases/download/1.31.0/advanced-scene-switcher-1.31.0-x86_64-linux-gnu.deb -O /tmp/advanced-scene-switcher.deb && \
    dpkg -i /tmp/advanced-scene-switcher.deb || true && \
    apt-get update && \
    apt-get install -fy && \
    rm /tmp/advanced-scene-switcher.deb && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Add startup script and necessary configs
COPY startup.sh /usr/local/bin/
COPY setup-scenes.sh /usr/local/bin/
# DEPRECATED: scenes.json and advanced-scene-switcher.json are kept for legacy support
# Use OBS_LEGACY_MODE=true to enable static scene import (not recommended)
# All scene/source/transition/media operations should be managed via obs-websocket API
COPY scenes.json /tmp/
COPY advanced-scene-switcher.json /tmp/
RUN chmod +x /usr/local/bin/startup.sh
RUN chmod +x /usr/local/bin/setup-scenes.sh

# Expose ports for WebSocket API (4455) and VNC (5901)
# Port 4455: obs-websocket (built-in with OBS 28+) for remote control
# Port 5901: VNC for graphical access
# Port 9222: CEF remote debugging
EXPOSE 4455 5901 9222

# Set the entrypoint to the startup script
ENTRYPOINT ["/usr/local/bin/startup.sh"]
