#!/bin/bash

# Create OBS config directories
mkdir -p /config/.config/obs-studio/basic/scenes
mkdir -p /config/.config/obs-studio/basic/profiles/Untitled

# Copy scene collection
cp /tmp/scenes.json /config/.config/obs-studio/basic/scenes/Untitled.json

# Create basic.ini to set the default scene collection and profile
cat > /config/.config/obs-studio/basic/profiles/Untitled/basic.ini << 'EOF'
[General]
Name=Untitled

[Video]
BaseCX=1920
BaseCY=1080
OutputCX=1920
OutputCY=1080
FPSType=0
FPSCommon=30

[Audio]
SampleRate=44100
ChannelSetup=Stereo

[AdvOut]
RecEncoder=obs_x264
RecType=Standard
RecFormat=mkv
RecQuality=Stream
EOF

# Set the scene collection in global.ini
cat > /config/.config/obs-studio/global.ini << 'EOF'
[General]
CurrentSceneCollection=Untitled
CurrentProfile=Untitled
FirstRun=false
EOF
