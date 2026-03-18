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

cat > /tmp/obs-browser-test.html <<'HTML'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>OBS Browser Test</title>
    <style>
      html, body {
        margin: 0;
        width: 100%;
        height: 100%;
        overflow: hidden;
        background:
          radial-gradient(circle at top left, #ffd166 0%, transparent 35%),
          linear-gradient(135deg, #c1121f 0%, #003049 55%, #111111 100%);
        color: #fdf0d5;
        font-family: monospace;
      }

      body {
        display: grid;
        place-items: center;
      }

      .card {
        width: 80vw;
        max-width: 1200px;
        padding: 48px;
        border: 6px solid rgba(253, 240, 213, 0.9);
        background: rgba(0, 0, 0, 0.35);
        box-shadow: 0 30px 80px rgba(0, 0, 0, 0.45);
      }

      h1 {
        margin: 0 0 24px;
        font-size: 96px;
        line-height: 1;
      }

      p {
        margin: 0 0 16px;
        font-size: 36px;
      }

      .pulse {
        display: inline-block;
        margin-top: 24px;
        padding: 12px 18px;
        background: #ffd166;
        color: #111111;
        font-size: 28px;
        font-weight: bold;
      }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>OBS Browser Test</h1>
      <p>If you can read this, browser rendering works.</p>
      <p>No external network. No app JS bundle. No API fetches.</p>
      <div class="pulse">LOCAL FILE RENDER CHECK</div>
    </div>
  </body>
</html>
HTML

CHANNEL_BROWSER_URL="${CHANNEL_BROWSER_URL:-https://alcantara.gaulatti.com/program/fifthbell}"
CHANNEL_BROWSER="${CHANNEL_BROWSER:-chrome}"

# 1. Create a stable startup file: Launch TWM, a persistent terminal, AND OBS
echo "#!/bin/sh" > /tmp/obs-xstartup
echo "mkdir -p /tmp/runtime-root && chmod 700 /tmp/runtime-root" >> /tmp/obs-xstartup
echo "export QT_QPA_PLATFORM=xcb" >> /tmp/obs-xstartup
echo "export HOME=/config" >> /tmp/obs-xstartup
echo "export XDG_RUNTIME_DIR=/tmp/runtime-root" >> /tmp/obs-xstartup
echo "export CEF_REMOTE_DEBUGGING_PORT=9222" >> /tmp/obs-xstartup
echo "export QTWEBENGINE_DISABLE_SANDBOX=1" >> /tmp/obs-xstartup
echo "export LIBVA_DRIVER_NAME=iHD" >> /tmp/obs-xstartup
echo "export LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri" >> /tmp/obs-xstartup
echo "export LIBVA_MESSAGING_LEVEL=2" >> /tmp/obs-xstartup

if [ "${OBS_FORCE_SOFTWARE_RENDERING:-true}" = "true" ]; then
    echo "export LIBGL_ALWAYS_SOFTWARE=1" >> /tmp/obs-xstartup
    echo "export MESA_LOADER_DRIVER_OVERRIDE=llvmpipe" >> /tmp/obs-xstartup
    echo "export QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox --disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --disable-dev-shm-usage --use-gl=swiftshader --enable-unsafe-swiftshader'" >> /tmp/obs-xstartup
    echo "export OBS_BROWSER_EXTRA_ARGS='--no-sandbox --disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --disable-dev-shm-usage --use-gl=swiftshader --enable-unsafe-swiftshader'" >> /tmp/obs-xstartup
    OBS_LAUNCH_FLAGS="--disable-gpu --disable-gpu-sandbox --use-gl=swiftshader"
else
    echo "export QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox --disable-gpu-sandbox --disable-dev-shm-usage'" >> /tmp/obs-xstartup
    echo "export OBS_BROWSER_EXTRA_ARGS='--no-sandbox --disable-gpu-sandbox --disable-dev-shm-usage'" >> /tmp/obs-xstartup
    OBS_LAUNCH_FLAGS="--disable-gpu-sandbox"
fi

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
echo "sleep 5" >> /tmp/obs-xstartup

# Launch OBS with restart loop
cat >> /tmp/obs-xstartup << 'OBSLOOP'
(
  while true; do
    echo "[obs-loop] Starting OBS on DISPLAY=${OBS_RENDER_DISPLAY:-:99} at $(date)" >&2
    DISPLAY="${OBS_RENDER_DISPLAY:-:99}" dbus-launch obs --startstreaming --verbose --disable-shutdown-check OBS_LAUNCH_FLAGS_PLACEHOLDER &
    OBS_PID=$!
    wait $OBS_PID
    echo "[obs-loop] OBS exited with code $? at $(date). Restarting in 5s..." >&2
    sleep 5
  done
) &

# Dedicated window-raiser: polls every 2s and forces OBS window visible whenever found
(
  while true; do
    OBS_WID=$(xdotool search --name "OBS" 2>/dev/null | head -1)
    if [ -n "$OBS_WID" ]; then
      xdotool windowmap "$OBS_WID" 2>/dev/null
      xdotool windowraise "$OBS_WID" 2>/dev/null
      xdotool windowmove --id "$OBS_WID" 0 0 2>/dev/null
      xdotool windowsize --id "$OBS_WID" 1280 800 2>/dev/null
      xdotool windowfocus --id "$OBS_WID" 2>/dev/null
    fi
    sleep 2
  done
) &
OBSLOOP

sed -i "s|OBS_LAUNCH_FLAGS_PLACEHOLDER|${OBS_LAUNCH_FLAGS}|" /tmp/obs-xstartup

# Launch xterm in background for debugging (not exec — don't let it steal focus)
echo "xterm -geometry 100x20+0+900 &" >> /tmp/obs-xstartup

# Keep xstartup alive
echo "wait" >> /tmp/obs-xstartup

chmod +x /tmp/obs-xstartup

# 2. Start a dedicated software X server for OBS rendering.
Xvfb :99 -screen 0 1920x1080x24 +extension GLX +extension RENDER >/tmp/xvfb.log 2>&1 &
export OBS_RENDER_DISPLAY=:99
until DISPLAY=:99 xdpyinfo >/dev/null 2>&1; do
    sleep 0.2
done

# 3. Start a separate display for the channel browser so OBS can capture it
# without recursively capturing its own render display.
Xvfb :98 -screen 0 1920x1080x24 +extension RANDR +extension MIT-SHM +extension XINERAMA >/tmp/browser-xvfb.log 2>&1 &
export CHANNEL_RENDER_DISPLAY=:98
until DISPLAY=:98 xdpyinfo >/dev/null 2>&1; do
    sleep 0.2
done

python3 -m http.server 8787 --directory /tmp >/tmp/obs-http.log 2>&1 &
until curl -fsS http://127.0.0.1:8787/obs-browser-test.html >/dev/null 2>&1; do
    sleep 0.2
done

BROWSER_LOG=/tmp/channel-browser.log
rm -f "${BROWSER_LOG}"

launch_channel_browser() {
  case "${CHANNEL_BROWSER}" in
    chrome)
      rm -rf /tmp/chrome-profile
      mkdir -p /tmp/chrome-profile
      echo "[channel-browser] Starting Google Chrome on ${CHANNEL_RENDER_DISPLAY} at $(date) -> ${CHANNEL_BROWSER_URL}" >&2
      nohup env \
        DISPLAY="${CHANNEL_RENDER_DISPLAY}" \
        GTK_A11Y=none \
        LIBGL_ALWAYS_SOFTWARE=1 \
        google-chrome \
        --no-sandbox \
        --disable-gpu \
        --disable-dev-shm-usage \
        --disable-features=Translate,MediaRouter,OptimizationHints,CalculateNativeWinOcclusion,ChromeWhatsNewUI,SigninIntercept,SearchEngineChoiceTrigger \
        --disable-background-networking \
        --disable-default-apps \
        --disable-popup-blocking \
        --disable-renderer-backgrounding \
        --disable-session-crashed-bubble \
        --disable-sync \
        --guest \
        --hide-scrollbars \
        --incognito \
        --kiosk \
        --no-default-browser-check \
        --no-first-run \
        --test-type \
        --window-position=0,0 \
        --window-size=1920,1080 \
        --autoplay-policy=no-user-gesture-required \
        --user-data-dir=/tmp/chrome-profile \
        "${CHANNEL_BROWSER_URL}" \
        >"${BROWSER_LOG}" 2>&1 &
      echo $! >/tmp/channel-browser.pid
      ;;
    firefox)
      rm -rf /tmp/firefox-profile
      mkdir -p /tmp/firefox-profile
      echo "[channel-browser] Starting Firefox on ${CHANNEL_RENDER_DISPLAY} at $(date) -> ${CHANNEL_BROWSER_URL}" >&2
      nohup env \
        DISPLAY="${CHANNEL_RENDER_DISPLAY}" \
        GTK_A11Y=none \
        LIBGL_ALWAYS_SOFTWARE=1 \
        firefox \
        --no-remote \
        --new-window \
        --profile /tmp/firefox-profile \
        "${CHANNEL_BROWSER_URL}" \
        >"${BROWSER_LOG}" 2>&1 &
      echo $! >/tmp/channel-browser.pid
      ;;
    epiphany|*)
      rm -rf /tmp/epiphany-profile
      mkdir -p /tmp/epiphany-profile
      echo "[channel-browser] Starting Epiphany on ${CHANNEL_RENDER_DISPLAY} at $(date) -> ${CHANNEL_BROWSER_URL}" >&2
      nohup env \
        DISPLAY="${CHANNEL_RENDER_DISPLAY}" \
        GTK_A11Y=none \
        LIBGL_ALWAYS_SOFTWARE=1 \
        WEBKIT_DISABLE_COMPOSITING_MODE=1 \
        WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1 \
        dbus-run-session -- \
        epiphany-browser --private-instance --new-window --profile="/tmp/epiphany-profile" "${CHANNEL_BROWSER_URL}" \
        >"${BROWSER_LOG}" 2>&1 &
      echo $! >/tmp/channel-browser.pid
      ;;
  esac
}

launch_channel_browser

# 4. Clean up stale VNC/X11 lock files from previous container runs
# Without this, vncserver silently fails to start after a container restart
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 /tmp/.X0-lock /tmp/.X11-unix/X0
vncserver -kill :1 2>/dev/null || true

# 5. Start TigerVNC on :1 for debugging and shell access
/usr/bin/vncserver :1 -geometry 1920x1080 -depth 24 -pixelformat rgb888 -SecurityTypes None -rfbport 5901 -localhost no -xstartup /tmp/obs-xstartup --I-KNOW-THIS-IS-INSECURE &
export DISPLAY=:1

# 6. Stream logs to container stdout and keep alive
echo "Starting VNC server and streaming logs..."
mkdir -p /config/.vnc /config/.config/obs-studio/logs /config/.config/obs-studio/crashes
exec sh -c '
  while true; do
    echo "[log-stream] --- $(date -Iseconds) ---"
    for f in /config/.vnc/*.log /config/.config/obs-studio/logs/*.txt /config/.config/obs-studio/crashes/*; do
      [ -f "$f" ] || continue
      echo "[log-stream] tailing: $f"
      tail -n 80 "$f" | sed "s|^|[$(basename $f)] |"
    done
    echo "[log-stream] ---------------------------"
    sleep 5
  done
'
