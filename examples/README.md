# obs-websocket Example Scripts

This directory contains example scripts demonstrating how to use the obs-websocket API to control OBS remotely.

## Prerequisites

1. OBS container is running with obs-websocket enabled
2. Node.js installed on your machine
3. Install dependencies:

```bash
cd examples
npm install
```

## Configuration

Set the password environment variable if authentication is enabled:

```bash
export OBS_WEBSOCKET_PASSWORD="your_password"
```

## Running Examples

### Basic Operations
Demonstrates connecting to OBS, getting info, creating scenes, and adding sources:
```bash
npm run basic
```

### Scene Management
Shows advanced scene management including browser sources and transitions:
```bash
npm run scenes
```

### Media Control
Demonstrates audio and media playback control:
```bash
npm run media
```

## Example Operations

### Connect to OBS
```javascript
import OBSWebSocket from 'obs-websocket-js';
const obs = new OBSWebSocket();
await obs.connect('ws://localhost:4455', 'your_password');
```

### Scene Operations
```javascript
// Create a scene
await obs.call('CreateScene', { sceneName: 'MyScene' });

// Switch to a scene
await obs.call('SetCurrentProgramScene', { sceneName: 'MyScene' });

// List scenes
const scenes = await obs.call('GetSceneList');

// Delete a scene
await obs.call('RemoveScene', { sceneName: 'MyScene' });
```

### Source Operations
```javascript
// Add a browser source
await obs.call('CreateInput', {
  sceneName: 'MyScene',
  inputName: 'MyBrowser',
  inputKind: 'browser_source',
  inputSettings: {
    url: 'https://example.com',
    width: 1920,
    height: 1080
  }
});

// Add a media source
await obs.call('CreateInput', {
  sceneName: 'MyScene',
  inputName: 'MyMedia',
  inputKind: 'ffmpeg_source',
  inputSettings: {
    local_file: '/media/video.mp4',
    looping: true
  }
});

// Remove an input
await obs.call('RemoveInput', { inputName: 'MyBrowser' });
```

### Audio Control
```javascript
// Set volume (0.0 to 1.0)
await obs.call('SetInputVolume', {
  inputName: 'MyMedia',
  inputVolumeMul: 0.5
});

// Mute/unmute
await obs.call('ToggleInputMute', { inputName: 'MyMedia' });

// Get mute status
const status = await obs.call('GetInputMute', { inputName: 'MyMedia' });
```

### Media Playback
```javascript
// Play
await obs.call('TriggerMediaInputAction', {
  inputName: 'MyMedia',
  mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY'
});

// Pause
await obs.call('TriggerMediaInputAction', {
  inputName: 'MyMedia',
  mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE'
});

// Get status
const status = await obs.call('GetMediaInputStatus', { inputName: 'MyMedia' });
```

### Streaming
```javascript
// Start streaming
await obs.call('StartStream');

// Stop streaming
await obs.call('StopStream');

// Get streaming status
const status = await obs.call('GetStreamStatus');
```

## API Reference

For the complete obs-websocket v5 API reference, see:
https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md
