#!/bin/bash

# Set up GPU device permissions
if [ -e /dev/dri/card0 ]; then
    chmod 666 /dev/dri/card0
fi

if [ -e /dev/dri/renderD129 ]; then
    RENDER_GID=$(stat -c '%g' /dev/dri/renderD129)
    groupadd -g $RENDER_GID render 2>/dev/null || true
    usermod -a -G $RENDER_GID root 2>/dev/null || true
fi

# 1. Create a stable startup file: Launch TWM, a persistent terminal, AND OBS
echo "#!/bin/sh" > /tmp/obs-xstartup
echo "export QT_QPA_PLATFORM=xcb" >> /tmp/obs-xstartup
echo "export HOME=/config" >> /tmp/obs-xstartup
echo "export CEF_REMOTE_DEBUGGING_PORT=9222" >> /tmp/obs-xstartup
echo "export LIBVA_DRIVER_NAME=iHD" >> /tmp/obs-xstartup
echo "export LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri" >> /tmp/obs-xstartup
echo "export LIBVA_MESSAGING_LEVEL=2" >> /tmp/obs-xstartup

# Create TWM config to place windows at 0,0 without interaction
cat > /tmp/.twmrc << 'TWMRC'
RandomPlacement
NoDefaults
DecorateTransients
ShowIconManager
IconManagerGeometry "100x5+0+0"
BorderWidth 2
TitleFont "-adobe-helvetica-bold-r-normal--*-120-*-*-*-*-*-*"
ResizeFont "-adobe-helvetica-bold-r-normal--*-120-*-*-*-*-*-*"
MenuFont "-adobe-helvetica-bold-r-normal--*-120-*-*-*-*-*-*"
IconFont "-adobe-helvetica-bold-r-normal--*-100-*-*-*-*-*-*"

Color
{
    BorderColor "slategrey"
    DefaultBackground "rgb:2/a/9"
    DefaultForeground "gray85"
    TitleBackground "rgb:2/a/9"
    TitleForeground "gray85"
    MenuBackground "rgb:2/a/9"
    MenuForeground "gray85"
    MenuTitleBackground "gray70"
    MenuTitleForeground "rgb:2/a/9"
    IconBackground "rgb:2/a/9"
    IconForeground "gray85"
    IconBorderColor "gray85"
    IconManagerBackground "rgb:2/a/9"
    IconManagerForeground "gray85"
}

Button1 = : root : f.menu "defops"
Button2 = : root : f.menu "windowops"
Button3 = : root : f.menu "windowops"

menu "defops"
{
    "TWM"       f.title
    "Iconify"   f.iconify
    "Resize"    f.resize
    "Move"      f.move
    "Raise"     f.raise
    "Lower"     f.lower
    ""          f.nop
    "Focus"     f.focus
    "Unfocus"   f.unfocus
    ""          f.nop
    "Kill"      f.destroy
}

menu "windowops"
{
    "Windows"   f.title
    "Iconify"   f.iconify
    "Resize"    f.resize
    "Move"      f.move
    "Raise"     f.raise
    "Lower"     f.lower
    ""          f.nop
    "Focus"     f.focus
    "Unfocus"   f.unfocus
}
TWMRC

# Launch TWM (Window Manager) in the background with config
echo "twm -f /tmp/.twmrc &" >> /tmp/obs-xstartup

# Wait for X server to be ready, then setup OBS configuration
echo "sleep 3" >> /tmp/obs-xstartup
echo "/usr/local/bin/setup-scenes.sh &" >> /tmp/obs-xstartup
echo "sleep 2" >> /tmp/obs-xstartup

# Launch OBS using dbus-launch in the background
echo "dbus-launch obs --startstreaming --verbose --disable-shutdown-check &" >> /tmp/obs-xstartup  # <-- Launches OBS automatically

# Launch a persistent terminal for debugging 
echo "exec xterm -geometry 100x40+0+0" >> /tmp/obs-xstartup 

chmod +x /tmp/obs-xstartup

# 2. Start TigerVNC Server (passwordless)
/usr/bin/vncserver :1 -geometry 1920x1080 -depth 24 -pixelformat rgb888 -SecurityTypes None -rfbport 5901 -localhost no -xstartup /tmp/obs-xstartup --I-KNOW-THIS-IS-INSECURE &
export DISPLAY=:1

# 3. CRITICAL: Keep the container running indefinitely
echo "Starting VNC server and keeping container alive..."
tail -f /dev/null