/**
 * Example: Media and audio control with obs-websocket
 * 
 * This script demonstrates how to control media sources,
 * manage audio levels, and work with media playback.
 * 
 * Requirements:
 *   npm install obs-websocket-js
 * 
 * Usage:
 *   node media-control.mjs
 */

import OBSWebSocket from 'obs-websocket-js';

const obs = new OBSWebSocket();

const OBS_WS_URL = 'ws://localhost:4455';
const OBS_WS_PASSWORD = process.env.OBS_WEBSOCKET_PASSWORD || '';

/**
 * List all inputs and their audio settings
 */
async function listInputsWithAudio() {
    const inputs = await obs.call('GetInputList');
    
    console.log('\n=== Inputs ===\n');
    
    for (const input of inputs.inputs) {
        console.log(`🎤 ${input.inputName}`);
        console.log(`   Kind: ${input.inputKind}`);
        
        try {
            // Try to get audio settings (not all inputs have audio)
            const volume = await obs.call('GetInputVolume', { 
                inputName: input.inputName 
            });
            console.log(`   Volume: ${(volume.inputVolumeMul * 100).toFixed(1)}%`);
            console.log(`   Volume (dB): ${volume.inputVolumeDb.toFixed(1)} dB`);
            
            const muted = await obs.call('GetInputMute', { 
                inputName: input.inputName 
            });
            console.log(`   Muted: ${muted.inputMuted ? 'Yes' : 'No'}`);
        } catch (error) {
            // Input doesn't have audio capabilities
        }
        console.log('');
    }
}

/**
 * Create a media source for audio playback
 */
async function createAudioSource(sceneName, sourceName, filePath, options = {}) {
    const {
        looping = false,
        volume = 1.0
    } = options;

    // Ensure scene exists
    try {
        await obs.call('CreateScene', { sceneName });
    } catch (error) {
        // Scene might already exist
    }

    // Create media source
    await obs.call('CreateInput', {
        sceneName,
        inputName: sourceName,
        inputKind: 'ffmpeg_source',
        inputSettings: {
            local_file: filePath,
            looping,
            restart_on_activate: true
        }
    });

    // Set volume
    await obs.call('SetInputVolume', {
        inputName: sourceName,
        inputVolumeMul: volume
    });

    console.log(`Created audio source: ${sourceName}`);
    return sourceName;
}

/**
 * Control media playback
 */
async function controlMedia(inputName, action) {
    switch (action) {
        case 'play':
            await obs.call('TriggerMediaInputAction', {
                inputName,
                mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY'
            });
            console.log(`Playing: ${inputName}`);
            break;
            
        case 'pause':
            await obs.call('TriggerMediaInputAction', {
                inputName,
                mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE'
            });
            console.log(`Paused: ${inputName}`);
            break;
            
        case 'restart':
            await obs.call('TriggerMediaInputAction', {
                inputName,
                mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART'
            });
            console.log(`Restarted: ${inputName}`);
            break;
            
        case 'stop':
            await obs.call('TriggerMediaInputAction', {
                inputName,
                mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP'
            });
            console.log(`Stopped: ${inputName}`);
            break;
            
        case 'next':
            await obs.call('TriggerMediaInputAction', {
                inputName,
                mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_NEXT'
            });
            console.log(`Next track: ${inputName}`);
            break;
            
        case 'previous':
            await obs.call('TriggerMediaInputAction', {
                inputName,
                mediaAction: 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PREVIOUS'
            });
            console.log(`Previous track: ${inputName}`);
            break;
    }
}

/**
 * Get media playback status
 */
async function getMediaStatus(inputName) {
    try {
        const status = await obs.call('GetMediaInputStatus', { inputName });
        console.log(`\n=== Media Status: ${inputName} ===`);
        console.log(`State: ${status.mediaState}`);
        console.log(`Duration: ${(status.mediaDuration / 1000).toFixed(1)}s`);
        console.log(`Position: ${(status.mediaCursor / 1000).toFixed(1)}s`);
        return status;
    } catch (error) {
        console.error(`Cannot get status for ${inputName}:`, error.message);
        return null;
    }
}

/**
 * Set volume for an input
 */
async function setVolume(inputName, volumePercent) {
    const volumeMul = Math.max(0, Math.min(1, volumePercent / 100));
    
    await obs.call('SetInputVolume', {
        inputName,
        inputVolumeMul: volumeMul
    });
    
    console.log(`Set volume of ${inputName} to ${volumePercent}%`);
}

/**
 * Mute/unmute an input
 */
async function toggleMute(inputName) {
    await obs.call('ToggleInputMute', { inputName });
    
    const muted = await obs.call('GetInputMute', { inputName });
    console.log(`${inputName} is now ${muted.inputMuted ? 'muted' : 'unmuted'}`);
}

async function main() {
    try {
        console.log('Connecting to OBS...');
        await obs.connect(OBS_WS_URL, OBS_WS_PASSWORD || undefined);
        console.log('Connected!\n');

        // List all inputs
        await listInputsWithAudio();

        // Example: Create a demo audio scene
        console.log('=== Demo: Audio Control ===\n');

        const sceneName = 'AudioDemo';
        const audioSource = 'DemoAudio';

        // Note: This demo creates a visual scene to demonstrate scene creation.
        // To test audio/media control, you would need to add media files to /media
        // and create a media source pointing to them.
        
        try {
            // Check if we can create the demo
            await obs.call('CreateScene', { sceneName });
            console.log(`Created scene: ${sceneName}`);

            // Add a color source as background
            await obs.call('CreateInput', {
                sceneName,
                inputName: 'DemoBackground',
                inputKind: 'color_source_v3',
                inputSettings: {
                    color: 0xFF1A1A1A,
                    width: 1920,
                    height: 1080
                }
            });

            console.log('\nTo test audio control with actual media files:');
            console.log('1. Mount a directory with media files to /media in the container');
            console.log('2. Use the following code:');
            console.log('   await createAudioSource("AudioDemo", "Music", "/media/your-song.mp3", { looping: true });');
            console.log('   await controlMedia("Music", "play");');
            console.log('   await setVolume("Music", 50);');
            console.log('   await toggleMute("Music");');

            // Clean up
            await new Promise(r => setTimeout(r, 2000));
            
            const sceneList = await obs.call('GetSceneList');
            const originalScene = sceneList.scenes.find(s => s.sceneName !== sceneName);
            if (originalScene) {
                await obs.call('SetCurrentProgramScene', { 
                    sceneName: originalScene.sceneName 
                });
            }

            await obs.call('RemoveInput', { inputName: 'DemoBackground' });
            await obs.call('RemoveScene', { sceneName });
            console.log('\nDemo scene cleaned up.');

        } catch (error) {
            console.log('Demo scene operations:', error.message);
        }

        console.log('\nDone!');
    } catch (error) {
        console.error('Error:', error.message);
    } finally {
        obs.disconnect();
        console.log('Disconnected from OBS');
    }
}

main();
