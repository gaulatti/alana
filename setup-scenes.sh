#!/bin/bash
set -e  # Exit on error

echo "Setting up OBS configuration..."

# Create OBS config directories
mkdir -p /config/.config/obs-studio/basic/scenes
mkdir -p /config/.config/obs-studio/basic/profiles/Untitled
mkdir -p /config/.config/obs-studio/plugin_config/obs-websocket

# Check if legacy mode is enabled
if [ "${OBS_LEGACY_MODE}" = "true" ]; then
    echo "=============================================="
    echo "WARNING: Legacy mode enabled (DEPRECATED)"
    echo "Static scene import is deprecated and will be"
    echo "removed in a future release."
    echo "Please migrate to obs-websocket API for"
    echo "dynamic scene/source/media control."
    echo "=============================================="
    
    echo "Copying scene collection..."
    # Copy scene collection with embedded Advanced Scene Switcher configuration
    cp /tmp/scenes.json /config/.config/obs-studio/basic/scenes/Untitled.json
    
    # Copy advanced scene switcher config if available (for standalone import)
    if [ -f /tmp/advanced-scene-switcher.json ]; then
        echo "Copying Advanced Scene Switcher configuration..."
        mkdir -p /config/.config/obs-studio/plugin_config/advanced-scene-switcher
        cp /tmp/advanced-scene-switcher.json /config/.config/obs-studio/plugin_config/advanced-scene-switcher/settings.json
    fi
else
    echo "Dynamic mode enabled (recommended)"
    echo "OBS will start with a minimal scene collection."
    echo "All scene/source/transition/media operations should"
    echo "be managed via obs-websocket API on port ${OBS_WEBSOCKET_PORT:-4455}."
    
    # Create minimal scene collection with a single blank scene
    cat > /config/.config/obs-studio/basic/scenes/Untitled.json << 'SCENES_EOF'
{
  "current_scene": "Scene",
  "current_program_scene": "Scene",
  "scene_order": [
    {
      "name": "Scene"
    }
  ],
  "name": "Untitled",
  "groups": [],
  "quick_transitions": [
    {
      "name": "Cut",
      "duration": 300,
      "hotkeys": [],
      "id": 1,
      "fade_to_black": false
    },
    {
      "name": "Fade",
      "duration": 300,
      "hotkeys": [],
      "id": 2,
      "fade_to_black": false
    }
  ],
  "transitions": [],
  "saved_projectors": [],
  "current_transition": "Cut",
  "transition_duration": 300,
  "preview_locked": false,
  "scaling_enabled": false,
  "scaling_level": 0,
  "scaling_off_x": 0.0,
  "scaling_off_y": 0.0,
  "modules": {
    "auto-scene-switcher": {
      "interval": 300,
      "non_matching_scene": "",
      "switch_if_not_matching": false,
      "active": false,
      "switches": []
    },
    "output-timer": {
      "streamTimerHours": 0,
      "streamTimerMinutes": 0,
      "streamTimerSeconds": 30,
      "recordTimerHours": 0,
      "recordTimerMinutes": 0,
      "recordTimerSeconds": 30,
      "autoStartStreamTimer": false,
      "autoStartRecordTimer": false,
      "pauseRecordTimer": true
    },
    "scripts-tool": []
  },
  "resolution": {
    "x": 1920,
    "y": 1080
  },
  "sources": [
    {
      "prev_ver": 503447555,
      "name": "Scene",
      "uuid": "00000000-0000-0000-0000-000000000001",
      "id": "scene",
      "versioned_id": "scene",
      "settings": {
        "id_counter": 0,
        "custom_size": false,
        "items": []
      },
      "mixers": 0,
      "sync": 0,
      "flags": 0,
      "volume": 1.0,
      "balance": 0.5,
      "enabled": true,
      "muted": false,
      "push-to-mute": false,
      "push-to-mute-delay": 0,
      "push-to-talk": false,
      "push-to-talk-delay": 0,
      "hotkeys": {
        "OBSBasic.SelectScene": []
      },
      "deinterlace_mode": 0,
      "deinterlace_field_order": 0,
      "monitoring_type": 0,
      "private_settings": {}
    }
  ]
}
SCENES_EOF
fi

# Configure obs-websocket settings
OBS_WS_PORT="${OBS_WEBSOCKET_PORT:-4455}"
OBS_WS_PASSWORD="${OBS_WEBSOCKET_PASSWORD:-}"

echo "Configuring obs-websocket on port ${OBS_WS_PORT}..."

# Create obs-websocket configuration
# OBS 28+ uses a JSON config file for websocket settings
if [ -n "${OBS_WS_PASSWORD}" ]; then
    # With authentication enabled
    cat > /config/.config/obs-studio/plugin_config/obs-websocket/config.json << EOF
{
    "server_enabled": true,
    "server_port": ${OBS_WS_PORT},
    "alerts_enabled": false,
    "auth_required": true,
    "server_password": "${OBS_WS_PASSWORD}"
}
EOF
    # Set restrictive permissions on config file containing password
    chmod 600 /config/.config/obs-studio/plugin_config/obs-websocket/config.json
    echo "obs-websocket configured with authentication."
else
    # Without authentication (for development/testing only)
    cat > /config/.config/obs-studio/plugin_config/obs-websocket/config.json << EOF
{
    "server_enabled": true,
    "server_port": ${OBS_WS_PORT},
    "alerts_enabled": false,
    "auth_required": false,
    "server_password": ""
}
EOF
    echo "=============================================="
    echo "SECURITY WARNING: obs-websocket is configured"
    echo "WITHOUT authentication. This is insecure and"
    echo "should only be used for local development."
    echo ""
    echo "For production, set OBS_WEBSOCKET_PASSWORD:"
    echo "  export OBS_WEBSOCKET_PASSWORD=\"your_secure_password\""
    echo "=============================================="
fi

echo "Creating basic.ini..."

# Create basic.ini to set the default scene collection and profile
cat > /config/.config/obs-studio/basic/profiles/Untitled/basic.ini << EOF
[General]
Name=Untitled

[Output]
Mode=Advanced
FilenameFormatting=%CCYY-%MM-%DD %hh-%mm-%ss
DelayEnable=false
DelaySec=20
DelayPreserve=true
Reconnect=true
RetryDelay=2
MaxRetries=25
BindIP=default
IPFamily=IPv4+IPv6
NewSocketLoopEnable=false
LowLatencyEnable=false

[Stream1]
IgnoreRecommended=false
EnableMultitrackVideo=false
MultitrackVideoMaximumAggregateBitrateAuto=true
MultitrackVideoMaximumVideoTracksAuto=true

[SimpleOutput]
FilePath=/config
RecFormat2=mkv
VBitrate=8750
ABitrate=160
UseAdvanced=true
QSVPreset=balanced
RecQuality=Stream
RecRB=false
RecRBTime=20
RecRBSize=512
RecRBPrefix=Replay
StreamAudioEncoder=aac
RecAudioEncoder=aac
RecTracks=1
StreamEncoder=obs_qsv11_v2
RecEncoder=obs_qsv11_v2
UseAdvanced=true
QSVPreset=balanced

[Video]
BaseCX=1920
BaseCY=1080
OutputCX=1920
OutputCY=1080
FPSType=0
FPSCommon=60
FPSInt=30
FPSNum=30
FPSDen=1
ScaleType=lanczos
ColorFormat=NV12
ColorSpace=709
ColorRange=Full
SdrWhiteLevel=300
HdrNominalPeakLevel=1000

[Audio]
SampleRate=44100
ChannelSetup=Stereo
MonitoringDeviceId=default
MonitoringDeviceName=Default
MeterDecayRate=23.53
PeakMeterType=0

[AdvOut]
RecEncoder=obs_qsv11_v2
RecType=Standard
RecFormat=mkv
ApplyServiceSettings=true
UseRescale=false
TrackIndex=1
VodTrackIndex=2
Encoder=obs_qsv11_v2
RecFilePath=/config
RecUseRescale=false
RecTracks=1
FLVTrack=1
StreamMultiTrackAudioMixes=1
FFOutputToFile=true
FFFilePath=/config
FFExtension=mp4
FFVBitrate=2500
FFVGOPSize=250
FFUseRescale=false
FFIgnoreCompat=false
FFABitrate=160
FFAudioMixes=1
Track1Bitrate=160
Track2Bitrate=160
Track3Bitrate=160
Track4Bitrate=160
Track5Bitrate=160
Track6Bitrate=160
RecSplitFileTime=15
RecSplitFileSize=2048
RecRB=false
RecRBTime=20
RecRBSize=512
AudioEncoder=libfdk_aac
RecAudioEncoder=libfdk_aac
EOF

# Set the scene collection in global.ini
cat > /config/.config/obs-studio/global.ini << 'EOF'
[General]
CurrentSceneCollection=Untitled
CurrentProfile=Untitled
FirstRun=false
Pre19Defaults=false
Pre21Defaults=false
Pre23Defaults=false
Pre24.1Defaults=false
MaxLogs=10
InfoIncrement=1
ProcessPriority=Normal
EnableAutoUpdates=false
ConfirmOnExit=true
HotkeyFocusType=NeverDisableHotkeys
YtDockCleanupDone=true
LastVersion=503447555

[BasicWindow]
geometry=@ByteArray(\x1\xd9\xd0\xcb\0\x3\0\0\0\0\0\0\0\0\0\0\0\0\a\x7f\0\0\x4\x37\0\0\0\0\0\0\0\0\0\0\a\x7f\0\0\x4\x37\0\0\0\0\0\0\0\0\a\x80\0\0\0\0\0\0\0\0\0\0\a\x7f\0\0\x4\x37)
PreviewEnabled=true
PreviewProgramMode=false
SceneDuplicationMode=true
SwapScenesMode=true
SnappingEnabled=true
ScreenSnapping=true
SourceSnapping=true
CenterSnapping=false
SnapDistance=10
SpacingHelpersEnabled=true
RecordWhenStreaming=false
KeepRecordingWhenStreamStops=false
SysTrayEnabled=true
SysTrayWhenStarted=false
SaveProjectors=false
ShowTransitions=true
ShowListboxToolbars=true
ShowStatusBar=true
ShowSourceIcons=true
ShowContextToolbars=true
StudioModeLabels=true
VerticalVolControl=false
MultiviewMouseSwitch=true
MultiviewDrawNames=true
MultiviewDrawAreas=true
MediaControlsCountdownTimer=true

[Notifications]
FirstRun=false

[Video]
Renderer=OpenGL

[Basic]
Profile=Untitled
ProfileDir=Untitled
SceneCollection=Untitled
SceneCollectionFile=Untitled
ConfigOnNewProfile=true
EOF

# Create service.json for YouTube streaming
cat > /config/.config/obs-studio/basic/profiles/Untitled/service.json << EOF
{
    "type": "rtmp_common",
    "settings": {
        "service": "YouTube - RTMPS",
        "server": "rtmps://a.rtmps.youtube.com:443/live2",
        "key": "${YOUTUBE_STREAM_KEY:-}",
        "protocol": "RTMPS",
        "stream_key_link": "https://www.youtube.com/live_dashboard",
        "multitrack_video_name": "Multitrack Video",
        "multitrack_video_disclaimer": "Multitrack Video automatically optimizes your settings to encode and send multiple video qualities to YouTube - RTMPS. Selecting this option will send YouTube - RTMPS information about your computer and software setup."
    }
}
EOF

# Skip auto-config wizard
mkdir -p /config/.config/obs-studio/plugin_config/obs-browser/obs_profile_cookies
cat > /config/.config/obs-studio/plugin_config/obs-browser/obs_profile_cookies/Cookies << 'EOF'
EOF

# Disable What's New dialog by creating window-state.json
cat > /config/.config/obs-studio/basic/profiles/Untitled/window-state.json << 'EOF'
{
    "WhatsNewInfoLastVersion": 503447555
}
EOF

echo "OBS configuration setup complete!"
