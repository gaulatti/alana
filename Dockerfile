# Use a single, reliable base image
FROM ubuntu:22.04

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/config
WORKDIR /config

# 1. Install core dependencies, VNC, X, QSV, and rendering fixes
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # PPA management tools, GPG, and essential utilities
    software-properties-common \
    gnupg \
    dirmngr \
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
    xinit \
    dbus \
    dbus-x11 \
    # Core Qt and Media dependencies
    libmfx1 \                       
    libqt6widgets6 libqt6gui6 libqt6core6 libqt6webenginecore6 libqt6webenginewidgets6 \
    libasound2 \
    pulseaudio \
    libnss3 \                   
    # CRITICAL XCB dependencies for Qt GUI:
    libxcb-randr0 \
    libxcb-image0 \
    libxcb-icccm4 \
    # FFMPEG and QSV runtime dependencies
    ffmpeg \
    libva-dev \
    libva-drm2 \
    i965-va-driver \
    mesa-va-drivers \
    va-driver-all \
    # Core graphics and Font dependencies for stable Browser Source rendering:
    libx11-6 libgl1 libxrandr2 \
    fonts-noto-core \               
    fonts-noto-cjk \                
    fontconfig \                    
    xfonts-base \                   
    libgl1-mesa-glx \               
    libegl1-mesa \                  
    libxkbcommon-x11-0 \            
    # Cleanup
    xterm \
    xserver-xorg-core \
    x11-xserver-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Add OBS PPA and Install OBS Studio
# Using the stable Launchpad PPA path via add-apt-repository
RUN add-apt-repository ppa:obsproject/obs-studio -y

# 3. Final OBS Installation
RUN apt-get update \
    && apt-get install -y obs-studio \
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

# Add startup script and necessary configs
COPY startup.sh /usr/local/bin/
COPY setup-scenes.sh /usr/local/bin/
COPY scenes.json /tmp/
RUN chmod +x /usr/local/bin/startup.sh
RUN chmod +x /usr/local/bin/setup-scenes.sh

# Expose ports for WebSocket API (4455) and VNC (5901)
EXPOSE 4455 5901

# Set the entrypoint to the startup script
ENTRYPOINT ["/usr/local/bin/startup.sh"]