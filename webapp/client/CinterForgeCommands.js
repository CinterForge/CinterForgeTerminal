/*
 * CinterForgeCommands.js — CinterForge Terminal browser owner
 * PURPOSE: Polls UI command events and registers game/code navigation requests.
 * METHODS: Event handlers and per-tab state.
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 3042–3042
CinterForgeState.pollingIF = false;

  // Original source lines 3043–3062
setInterval(async () => {
    if (document.visibilityState === 'hidden' || CinterForgeState.pollingIF) return;
    CinterForgeState.pollingIF = true;
    try {
      const action = await CinterForgeState.api('/api/if/next');
      if (action.start) {
        await CinterForgeState.openIFTab(action.start, action.launch_id, action.session_id);
      } else if (action.stop) {
        const tab = CinterForgeState.tabs.find(tab => tab.kind === 'if' &&
          tab.game?.name === action.stop &&
          (!action.launch_id || tab.game.launchId === action.launch_id));
        if (tab) await CinterForgeState.stopGame(tab, false);
      }
    } catch (error) {
      CinterForgeState.notice('IF: ' + error.message);
    }
    finally {
      CinterForgeState.pollingIF = false;
    }
  }, 800);

  // Original source lines 3065–3073
setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await CinterForgeState.api('/api/wargames/next');
      if (result.open) await CinterForgeState.openSession('joshua');
    } catch (error) {
      CinterForgeState.notice('WarGames: ' + error.message);
    }
  }, 850);

  // Host `play <title>` always targets the existing OS-specific 68K Terminal.
  // Never manufacture a per-game emulator profile or load a library URL.
  setInterval(async () => {
    if (document.visibilityState === 'hidden' || CinterForgeState.pollingMacGames) return;
    CinterForgeState.pollingMacGames = true;
    let requestedOS = null;
    try {
      const result = await CinterForgeState.api('/api/macgames/next');
      if (!result.game) return;
      const game = CinterForgeState.macCatalog.get(result.game);
      if (!game || !/^[a-z0-9]{1,64}$/.test(game.guest_command || ''))
        throw new Error('Missing 68K game command for ' + result.game);
      const os = game.guest_os === '7.1' ? 'System 7.1' :
        game.guest_os === '7.6' ? 'Mac OS 7.6' : null;
      const emulator = CinterForgeState.profiles.find(profile => profile.id === game.emulator_profile);
      if (!os || emulator?.disk !== os || emulator.kind !== 'emulator' || emulator.lagomorph)
        throw new Error('Unsupported 68K game emulator for ' + game.title);
      // Queue before opening a cold emulator: this suppresses the host's
      // normal startup greeting while Finder and Terminal are recognized by OCR.
      // Commands are delivered through Infinite Mac's keyboard API, not
      // through the host or guest clipboard.
      requestedOS = os;
      CinterForgeState.queue68KGame({os, command: game.guest_command,
        title: game.title});
      CinterForgeState.notice('Launching ' + game.title + '. This may take a second...');
      const opened = await CinterForgeState.openSession(emulator.id);
      // openSession returns an existing singleton tab, but returns undefined
      // on its first (new) emulator open. Resolve the actual new tab by OS.
      const tab = opened || CinterForgeState.tabs.find(item => !item.closed &&
        item.kind === 'emulator' && CinterForgeState.profiles.some(profile =>
          profile.id === item.profileId && profile.disk === os && !profile.lagomorph));
      if (!tab || tab.closed) throw new Error('Could not open the ' + os + ' emulator');
      CinterForgeState.resume68KGame(tab);
    } catch (error) {
      if (requestedOS) CinterForgeState.cancel68KGame(requestedOS);
      CinterForgeState.notice('Mac game: ' + error.message);
    } finally {
      CinterForgeState.pollingMacGames = false;
    }
  }, 850);

  // Original source lines 3104–3120
setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await CinterForgeState.api('/api/code/next');
      if (!result.open) return;
      const existing = CinterForgeState.tabs.find(tab => !tab.closed && tab.profileId === 'codeoss');
      if (existing) {
        if (result.reload && CinterForgeState.activeTab === existing) {
          // Theme/workspace changed in a shell: reload the workbench so its
          // existing IF-derived startup applies the persisted preferences.
          await CinterForgeState.showComputer(CinterForgeState.profiles.find(profile => profile.id === 'codeoss'));
        } else {
          await CinterForgeState.activateTab(existing);
        }
      } else await CinterForgeState.openSession('codeoss');
    } catch (_) { /* Service may be closing. */ }
  }, 850);

  // Original source lines 3123–3129
setInterval(async () => {
    if (CinterForgeState.platform !== 'mac' || document.visibilityState === 'hidden') return;
    try {
      const state = await CinterForgeState.api('/api/ui/html-menus');
      document.documentElement?.setAttribute?.('data-html-menus', state.show ? 'show' : 'hide');
    } catch (_) { /* Closing webview or server: keep menus hidden by default. */ }
  }, 1000);

  // Original source lines 3131–3140
setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await CinterForgeState.api('/api/lagomorph/next');
      if (result.open && result.profile === 'quadra-650') {

        await CinterForgeState.openSession(result.profile);
      }
    } catch (_) { /* local service may be shutting down */ }
  }, 850);

})();
