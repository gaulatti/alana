#!/bin/bash
set -e  # Exit on error

echo "Setting up OBS configuration..."

# Create OBS config directories
mkdir -p /config/.config/obs-studio/basic/scenes
mkdir -p /config/.config/obs-studio/basic/profiles/Untitled

echo "Copying scene collection..."
# Copy scene collection
cp /tmp/scenes.json /config/.config/obs-studio/basic/scenes/Untitled.json

# Create Advanced Scene Switcher directory and copy config BEFORE OBS starts
mkdir -p /config/.config/obs-studio/plugin_config/advanced-scene-switcher
cp /tmp/advanced-scene-switcher.json /config/.config/obs-studio/plugin_config/advanced-scene-switcher/advanced-scene-switcher.json
chmod 644 /config/.config/obs-studio/plugin_config/advanced-scene-switcher/advanced-scene-switcher.json
echo "Advanced Scene Switcher config prepared"

echo "Creating basic.ini..."

# Create basic.ini to set the default scene collection and profile
cat > /config/.config/obs-studio/basic/profiles/Untitled/basic.ini << EOF
[General]
Name=Untitled

[Output]
Mode=Simple
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
Preset=veryfast
NVENCPreset2=p5
RecQuality=Stream
RecRB=false
RecRBTime=20
RecRBSize=512
RecRBPrefix=Replay
StreamAudioEncoder=aac
RecAudioEncoder=aac
RecTracks=1
StreamEncoder=x264
RecEncoder=x264
x264Profile=high
x264Tune=none
x264Options=bframes=2:ref=3:subme=7:me_range=16:qcomp=0.60:keyint=250:min-keyint=25

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
RecEncoder=obs_x264
RecType=Standard
RecFormat=mkv
RecQuality=Stream
RecFormat2=mkv
ApplyServiceSettings=true
UseRescale=false
TrackIndex=1
VodTrackIndex=2
Encoder=obs_x264
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
