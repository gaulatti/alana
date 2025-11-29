/**
 * Example: Basic obs-websocket operations
 * 
 * This script demonstrates how to connect to OBS via obs-websocket
 * and perform common operations like creating scenes, adding sources,
 * and switching between scenes.
 * 
 * Requirements:
 *   npm install obs-websocket-js
 * 
 * Usage:
 *   node basic-operations.mjs
 */

import OBSWebSocket from 'obs-websocket-js';

const obs = new OBSWebSocket();

// Configuration - update these values
const OBS_WS_URL = 'ws://localhost:4455';
const OBS_WS_PASSWORD = process.env.OBS_WEBSOCKET_PASSWORD || '';

async function main() {
    try {
        // Connect to OBS
        console.log('Connecting to OBS...');
        await obs.connect(OBS_WS_URL, OBS_WS_PASSWORD || undefined);
        console.log('Connected!\n');

        // Get OBS version info
        const version = await obs.call('GetVersion');
        console.log('OBS Version:', version.obsVersion);
        console.log('WebSocket Version:', version.obsWebSocketVersion);
        console.log('Platform:', version.platformDescription);
        console.log('');

        // List existing scenes
        console.log('--- Existing Scenes ---');
        const sceneList = await obs.call('GetSceneList');
        console.log('Current Scene:', sceneList.currentProgramSceneName);
        console.log('Scenes:');
        sceneList.scenes.forEach(scene => {
            console.log(`  - ${scene.sceneName}`);
        });
        console.log('');

        // Create a new scene
        const newSceneName = 'DemoScene_' + Date.now();
        console.log(`Creating scene: ${newSceneName}`);
        await obs.call('CreateScene', { sceneName: newSceneName });
        console.log('Scene created!\n');

        // Add a color source (solid color background)
        console.log('Adding color source...');
        await obs.call('CreateInput', {
            sceneName: newSceneName,
            inputName: 'ColorBackground',
            inputKind: 'color_source_v3',
            inputSettings: {
                color: 0xFF2A2A9A, // ABGR format - blue color
                width: 1920,
                height: 1080
            }
        });
        console.log('Color source added!\n');

        // Add a text source
        console.log('Adding text source...');
        await obs.call('CreateInput', {
            sceneName: newSceneName,
            inputName: 'HelloText',
            inputKind: 'text_ft2_source_v2',
            inputSettings: {
                text: 'Hello from obs-websocket!',
                font: {
                    face: 'Sans Serif',
                    size: 64,
                    flags: 0
                },
                color1: 0xFFFFFFFF, // White
                color2: 0xFFFFFFFF
            }
        });
        console.log('Text source added!\n');

        // Switch to the new scene
        console.log(`Switching to scene: ${newSceneName}`);
        await obs.call('SetCurrentProgramScene', { sceneName: newSceneName });
        console.log('Scene switched!\n');

        // Wait a moment to see the result
        console.log('Waiting 3 seconds...');
        await new Promise(resolve => setTimeout(resolve, 3000));

        // Get list of inputs in the scene
        console.log('--- Scene Items ---');
        const sceneItems = await obs.call('GetSceneItemList', { sceneName: newSceneName });
        sceneItems.sceneItems.forEach(item => {
            console.log(`  - ${item.sourceName} (ID: ${item.sceneItemId})`);
        });
        console.log('');

        // Clean up - remove the demo scene
        console.log('Cleaning up...');
        
        // Switch back to default scene first
        if (sceneList.scenes.length > 0 && sceneList.scenes[0].sceneName !== newSceneName) {
            await obs.call('SetCurrentProgramScene', { 
                sceneName: sceneList.scenes[0].sceneName 
            });
        }
        
        // Remove the demo scene
        await obs.call('RemoveScene', { sceneName: newSceneName });
        console.log('Demo scene removed!\n');

        console.log('Done!');
    } catch (error) {
        console.error('Error:', error.message);
        if (error.code) {
            console.error('Error code:', error.code);
        }
    } finally {
        // Disconnect
        obs.disconnect();
        console.log('Disconnected from OBS');
    }
}

main();
