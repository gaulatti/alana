#!/bin/bash

# VNC Password setup 
if [ -n "$VNC_PASSWORD" ]; then
    mkdir -p ~/.vnc
    echo "$VNC_PASSWORD" | /usr/bin/vncpasswd -f > ~/.vnc/passwd
    chmod 600 ~/.vnc/passwd
fi

# 1. Create a stable startup file: Launch TWM, a persistent terminal, AND OBS
echo "#!/bin/sh" > /tmp/obs-xstartup
echo "export QT_QPA_PLATFORM=xcb" >> /tmp/obs-xstartup

# Launch TWM (Window Manager) in the background
echo "twm &" >> /tmp/obs-xstartup

# Launch OBS using dbus-launch in the background
echo "dbus-launch obs --startstreaming --verbose --studio-mode &" >> /tmp/obs-xstartup  # <-- Launches OBS automatically

# Launch a persistent terminal for debugging 
echo "exec xterm -geometry 100x40+0+0" >> /tmp/obs-xstartup 

chmod +x /tmp/obs-xstartup

# 2. Start TigerVNC Server
/usr/bin/vncserver :1 -geometry 1920x1080 -depth 24 -rfbauth ~/.vnc/passwd -rfbport 5901 -localhost no -xstartup /tmp/obs-xstartup &
export DISPLAY=:1

# 3. CRITICAL: Keep the container running indefinitely
echo "Starting VNC server and keeping container alive..."
tail -f /dev/null