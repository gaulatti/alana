# Alana - OBS Docker Environment

A containerized OBS Studio environment with obs-websocket support for remote, dynamic control of scenes, sources, transitions, and media.

## Features

- **OBS Studio** with VNC access for visual monitoring
- **obs-websocket** (built-in with OBS 28+) for remote API control
- **Dynamic scene management** via obs-websocket commands
- Support for browser sources, media playlist, and transitions
- GPU hardware encoding support (Intel QSV)

## Quick Start

### Build the Docker Image

```bash
make build
```

### Run the Container

```bash
# Basic run (no authentication - development only)
make run

# With obs-websocket authentication (recommended for production)
export OBS_WEBSOCKET_PASSWORD="your_secure_password"
make run

# With YouTube streaming
export YOUTUBE_STREAM_KEY="your_stream_key"
export OBS_WEBSOCKET_PASSWORD="your_secure_password"
make run
```

### Connect to OBS

- **VNC**: Connect to `localhost:5901` for graphical access
- **obs-websocket**: Connect to `ws://localhost:4455` for API access

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OBS_WEBSOCKET_PASSWORD` | (empty) | Password for obs-websocket authentication. **Set this for production!** |
| `OBS_WEBSOCKET_PORT` | `4455` | Port for obs-websocket server |
| `OBS_LEGACY_MODE` | `false` | Set to `true` to enable deprecated static scene import |
| `YOUTUBE_STREAM_KEY` | (empty) | YouTube RTMP stream key |

## obs-websocket API

OBS 28+ includes obs-websocket v5 built-in. The container is configured to:

1. Start obs-websocket server automatically
2. Listen on port 4455 (configurable via `OBS_WEBSOCKET_PORT`)
3. Require authentication when `OBS_WEBSOCKET_PASSWORD` is set

### Connecting from External Applications

#### Using obs-websocket-js (Node.js)

```javascript
import OBSWebSocket from 'obs-websocket-js';

const obs = new OBSWebSocket();

// Connect to OBS
await obs.connect('ws://localhost:4455', 'your_password');

// Get version info
const version = await obs.call('GetVersion');
console.log('OBS Version:', version.obsVersion);

// Create a new scene
await obs.call('CreateScene', { sceneName: 'MyNewScene' });

// Add a browser source to the scene
await obs.call('CreateInput', {
  sceneName: 'MyNewScene',
  inputName: 'MyBrowser',
  inputKind: 'browser_source',
  inputSettings: {
    url: 'https://example.com',
    width: 1920,
    height: 1080
  }
});

// Switch to the new scene
await obs.call('SetCurrentProgramScene', { sceneName: 'MyNewScene' });

// Set a stinger transition
await obs.call('SetCurrentSceneTransition', { transitionName: 'Stinger' });

// Get list of scenes
const scenes = await obs.call('GetSceneList');
console.log('Scenes:', scenes.scenes);

// Delete a scene
await obs.call('RemoveScene', { sceneName: 'MyNewScene' });

// Disconnect
obs.disconnect();
```

#### Using curl (for testing)

obs-websocket v5 uses a binary WebSocket protocol and cannot be easily tested with curl. Use a WebSocket client or obs-websocket-js instead.

### Common obs-websocket Operations

| Operation | Request Type | Example |
|-----------|--------------|---------|
| Get scene list | `GetSceneList` | `obs.call('GetSceneList')` |
| Create scene | `CreateScene` | `obs.call('CreateScene', { sceneName: 'Name' })` |
| Remove scene | `RemoveScene` | `obs.call('RemoveScene', { sceneName: 'Name' })` |
| Switch scene | `SetCurrentProgramScene` | `obs.call('SetCurrentProgramScene', { sceneName: 'Name' })` |
| Add source | `CreateInput` | `obs.call('CreateInput', { sceneName, inputName, inputKind, inputSettings })` |
| Remove source | `RemoveInput` | `obs.call('RemoveInput', { inputName: 'Name' })` |
| Get inputs | `GetInputList` | `obs.call('GetInputList')` |
| Set transition | `SetCurrentSceneTransition` | `obs.call('SetCurrentSceneTransition', { transitionName: 'Fade' })` |
| Start streaming | `StartStream` | `obs.call('StartStream')` |
| Stop streaming | `StopStream` | `obs.call('StopStream')` |

For the complete API reference, see the [obs-websocket protocol documentation](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md).

## Dynamic Scene Management

By default, OBS starts with a minimal scene collection containing a single blank scene. All scene, source, transition, and media management should be performed via the obs-websocket API.

### Example: Setting Up a Streaming Scene

```javascript
import OBSWebSocket from 'obs-websocket-js';

const obs = new OBSWebSocket();
await obs.connect('ws://localhost:4455', 'your_password');

// Create main scene
await obs.call('CreateScene', { sceneName: 'MainScene' });

// Add a browser source
await obs.call('CreateInput', {
  sceneName: 'MainScene',
  inputName: 'MainBrowser',
  inputKind: 'browser_source',
  inputSettings: {
    url: 'https://your-dashboard.com',
    width: 1920,
    height: 1080
  }
});

// Add an audio source (media file)
await obs.call('CreateInput', {
  sceneName: 'MainScene',
  inputName: 'BackgroundMusic',
  inputKind: 'ffmpeg_source',
  inputSettings: {
    local_file: '/media/background.mp3',
    looping: true
  }
});

// Set as current scene
await obs.call('SetCurrentProgramScene', { sceneName: 'MainScene' });

// Start streaming
await obs.call('StartStream');
```

## Legacy Mode (Deprecated)

> ⚠️ **DEPRECATED**: Static scene import is deprecated and will be removed in a future release.

For backward compatibility, you can enable legacy mode to import pre-configured scenes from `scenes.json`:

```bash
export OBS_LEGACY_MODE=true
make run
```

### Deprecated Files

The following files are kept for legacy support only:

- `scenes.json` - Pre-configured scene collection (DEPRECATED)
- `advanced-scene-switcher.json` - Automation macros (DEPRECATED)

**Recommendation**: Migrate to obs-websocket API for all scene/source/media management.

## Ports

| Port | Protocol | Description |
|------|----------|-------------|
| 4455 | WebSocket | obs-websocket API for remote control |
| 5901 | VNC | TigerVNC for graphical access |
| 9222 | HTTP | CEF remote debugging (browser sources) |

## Volume Mounts

| Host Path | Container Path | Description |
|-----------|----------------|-------------|
| `./music` | `/media` | Audio files for media sources |
| `./video` | `/video` | Video files for transitions and media |

## Security Considerations

1. **Always set `OBS_WEBSOCKET_PASSWORD`** in production environments
2. Use a firewall to restrict access to ports 4455 and 5901
3. Consider using a reverse proxy with TLS for secure WebSocket connections
4. The VNC server runs without a password by default - restrict access at the network level

## Troubleshooting

### obs-websocket Connection Issues

1. Verify the container is running: `docker ps`
2. Check logs: `make logs`
3. Ensure port 4455 is accessible: `nc -zv localhost 4455`
4. Verify password matches if authentication is enabled

### VNC Connection Issues

1. Ensure port 5901 is accessible
2. Use a VNC client that supports TigerVNC protocol
3. No password is required by default

## License

See LICENSE file for details.
