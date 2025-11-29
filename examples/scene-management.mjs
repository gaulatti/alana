/**
 * Example: Scene management with obs-websocket
 * 
 * This script demonstrates advanced scene management operations
 * including creating scenes with browser sources, setting up
 * transitions, and managing scene items.
 * 
 * Requirements:
 *   npm install obs-websocket-js
 * 
 * Usage:
 *   node scene-management.mjs
 */

import OBSWebSocket from 'obs-websocket-js';

const obs = new OBSWebSocket();

const OBS_WS_URL = 'ws://localhost:4455';
const OBS_WS_PASSWORD = process.env.OBS_WEBSOCKET_PASSWORD || '';

/**
 * Create a browser source scene
 */
async function createBrowserScene(sceneName, url, options = {}) {
    const {
        width = 1920,
        height = 1080,
        fps = 30,
        css = ''
    } = options;

    // Create the scene
    await obs.call('CreateScene', { sceneName });
    console.log(`Created scene: ${sceneName}`);

    // Add browser source
    const inputName = `${sceneName}_Browser`;
    await obs.call('CreateInput', {
        sceneName,
        inputName,
        inputKind: 'browser_source',
        inputSettings: {
            url,
            width,
            height,
            fps,
            css,
            shutdown: true,
            restart_when_active: false
        }
    });
    console.log(`Added browser source: ${inputName}`);

    return { sceneName, inputName };
}

/**
 * Create a media source scene with audio
 */
async function createMediaScene(sceneName, mediaPath, options = {}) {
    const {
        looping = false,
        restart_on_activate = true
    } = options;

    // Create the scene
    await obs.call('CreateScene', { sceneName });
    console.log(`Created scene: ${sceneName}`);

    // Add media source
    const inputName = `${sceneName}_Media`;
    await obs.call('CreateInput', {
        sceneName,
        inputName,
        inputKind: 'ffmpeg_source',
        inputSettings: {
            local_file: mediaPath,
            looping,
            restart_on_activate
        }
    });
    console.log(`Added media source: ${inputName}`);

    return { sceneName, inputName };
}

/**
 * Set up a stinger transition
 */
async function setupStingerTransition(transitionName, videoPath, options = {}) {
    const {
        transitionPoint = 500,
        transitionPointType = 'time'
    } = options;

    try {
        await obs.call('CreateSceneTransition', {
            transitionName,
            transitionKind: 'stinger_transition',
            transitionSettings: {
                path: videoPath,
                transition_point_type: transitionPointType,
                transition_point: transitionPoint
            }
        });
        console.log(`Created stinger transition: ${transitionName}`);
    } catch (error) {
        if (error.code === 601) {
            console.log(`Transition already exists: ${transitionName}`);
        } else {
            throw error;
        }
    }
}

/**
 * List all scenes and their sources
 */
async function listAllScenes() {
    const sceneList = await obs.call('GetSceneList');
    
    console.log('\n=== Scene List ===');
    console.log(`Current Program Scene: ${sceneList.currentProgramSceneName}`);
    console.log(`Current Preview Scene: ${sceneList.currentPreviewSceneName || 'N/A'}`);
    console.log('');

    for (const scene of sceneList.scenes) {
        console.log(`📺 ${scene.sceneName}`);
        
        const sceneItems = await obs.call('GetSceneItemList', { 
            sceneName: scene.sceneName 
        });
        
        if (sceneItems.sceneItems.length === 0) {
            console.log('   (empty)');
        } else {
            for (const item of sceneItems.sceneItems) {
                const visibility = item.sceneItemEnabled ? '✓' : '✗';
                console.log(`   ${visibility} ${item.sourceName} (ID: ${item.sceneItemId})`);
            }
        }
        console.log('');
    }
}

/**
 * Delete a scene and its sources
 */
async function deleteScene(sceneName) {
    try {
        // Get scene items first
        const sceneItems = await obs.call('GetSceneItemList', { sceneName });
        
        // Remove inputs that belong to this scene
        for (const item of sceneItems.sceneItems) {
            try {
                await obs.call('RemoveInput', { inputName: item.sourceName });
                console.log(`Removed input: ${item.sourceName}`);
            } catch (error) {
                // Input might be used by other scenes or is a scene reference
                console.log(`Skipped removing input: ${item.sourceName} (may be in use elsewhere)`);
            }
        }

        // Remove the scene
        await obs.call('RemoveScene', { sceneName });
        console.log(`Removed scene: ${sceneName}`);
    } catch (error) {
        console.error(`Failed to delete scene ${sceneName}:`, error.message);
    }
}

async function main() {
    try {
        console.log('Connecting to OBS...');
        await obs.connect(OBS_WS_URL, OBS_WS_PASSWORD || undefined);
        console.log('Connected!\n');

        // List current scenes
        await listAllScenes();

        // Create example scenes
        console.log('=== Creating Example Scenes ===\n');

        // Create a dashboard scene with browser source
        await createBrowserScene('Dashboard', 'https://example.com/dashboard', {
            width: 1920,
            height: 1080
        });

        // Create a news scene
        await createBrowserScene('NewsScene', 'https://news.example.com', {
            width: 1920,
            height: 1080,
            css: 'body { background: transparent !important; }'
        });

        // List scenes again
        await listAllScenes();

        // Switch between scenes
        console.log('=== Scene Switching Demo ===\n');
        
        console.log('Switching to Dashboard...');
        await obs.call('SetCurrentProgramScene', { sceneName: 'Dashboard' });
        await new Promise(r => setTimeout(r, 2000));

        console.log('Switching to NewsScene...');
        await obs.call('SetCurrentProgramScene', { sceneName: 'NewsScene' });
        await new Promise(r => setTimeout(r, 2000));

        // Clean up demo scenes
        console.log('\n=== Cleanup ===\n');
        
        // Get first original scene to switch to
        const sceneList = await obs.call('GetSceneList');
        const originalScene = sceneList.scenes.find(s => 
            s.sceneName !== 'Dashboard' && s.sceneName !== 'NewsScene'
        );
        
        if (originalScene) {
            await obs.call('SetCurrentProgramScene', { 
                sceneName: originalScene.sceneName 
            });
        }

        await deleteScene('Dashboard');
        await deleteScene('NewsScene');

        console.log('\nDone!');
    } catch (error) {
        console.error('Error:', error.message);
    } finally {
        obs.disconnect();
        console.log('Disconnected from OBS');
    }
}

main();
