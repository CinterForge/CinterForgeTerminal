/*
 * CinterForgeGuestStatus.js — CinterForge Terminal browser owner
 * PURPOSE: Normalizes backend guest capability snapshots and restores only live Bash/VNC tabs.
 * METHODS: updateGuestCapabilities, restoreConnectedGuestTabs
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // The same app-owned guest supervisor is started only after a requested
  // Code/Bash/VNC action and explicit consent. Restoring old tabs never boots it.
  CinterForgeState.requestDevTools = async function requestDevTools(kind) {
    if (CinterForgeState.terminalActionEnabled(kind)) return true;
    if (!CinterForgeState.devToolsRequestable(kind)) return false;
    if (CinterForgeState.devToolsBootRequest) return CinterForgeState.devToolsBootRequest;
    CinterForgeState.devToolsBootRequest = (async () => {
      try {
        const status = await CinterForgeState.api('/api/status');
        CinterForgeState.updateGuestCapabilities(status);
        if (!status.devtools_installed) {
          if (!window.confirm('Developer Tools are not installed. Install them in a Mac Bash Terminal?')) return false;
          const source = window.prompt('Absolute Source directory for DevTools installation:', '');
          if (!source?.startsWith('/') || /[\r\n]/.test(source)) {
            CinterForgeState.notice('DevTools installation requires an absolute Source directory');
            return false;
          }
          // Run the existing interactive, verified CLI installer in an actual
          // Mac Bash PTY; do not install from a browser worker or skip consent.
          await CinterForgeState.openSession('modern', 'native');
          const id = CinterForgeState.activeTab?.session?.id;
          if (!id) throw new Error('Mac Bash did not connect for DevTools installation');
          const quoted = "'" + source.replace(/'/g, "'\\''") + "'";
          await CinterForgeState.api('/api/terminal/input', {
            id, data: 'devtools install ' + quoted + '\n'
          });
          CinterForgeState.notice('DevTools installation started in Mac Bash; follow its prompts');
          return false;
        }
        if (!window.confirm('Start installed DevTools for ' + kind + '? This loads the Linux/Windows guest VM.')) return false;
        const result = await CinterForgeState.api('/api/lifecycle/guest-boot', {});
        if (result.state === 'failed' || result.state === 'not-installed')
          throw new Error(result.error || 'DevTools did not start');
        CinterForgeState.notice('Starting DevTools guests; monitoring readiness…');
        for (let attempt = 0; attempt < 300; ++attempt) {
          const snapshot = await CinterForgeState.api('/api/lifecycle/guest-status');
          CinterForgeState.updateGuestCapabilities(snapshot);
          if (CinterForgeState.terminalActionEnabled(kind)) return true;
          if (snapshot.state === 'failed') throw new Error(snapshot.error || 'DevTools guest startup failed');
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
        throw new Error('DevTools guests did not become ready; see Server Log');
      } catch (error) {
        CinterForgeState.notice('DevTools: ' + error.message);
        return false;
      }
    })();
    try { return await CinterForgeState.devToolsBootRequest; }
    finally { CinterForgeState.devToolsBootRequest = null; }
  };

  // Original source lines 2908–2942
CinterForgeState.updateGuestCapabilities = function updateGuestCapabilities(status) {
    const snapshot = window.CinterForgeCapabilities.normalize(status, {
      devtoolsInstalled: CinterForgeState.devtoolsInstalled,
      guestState: CinterForgeState.guestState,
      guestReady: CinterForgeState.guestReady,
      guestVncReady: CinterForgeState.guestVncReady
    });
    if (!snapshot) return;
    const {installed, state, guests, vnc, changed} = snapshot;
    CinterForgeState.devtoolsInstalled = installed;
    CinterForgeState.guestState = state;
    CinterForgeState.guestReady = guests;
    CinterForgeState.guestVncReady = vnc;
    if (changed) {
      CinterForgeState.updateCodeMenuMode();
      CinterForgeState.codeCommands?.postMessage({type: 'devtools-capabilities', installed,
        guests: [...CinterForgeState.guestReady], vnc: [...CinterForgeState.guestVncReady], state});
      // A disconnected Code-OSS/noVNC view cannot remain in the foreground.
      // The native Mac Bash is independent of this VM; never send users to
      // Server Log or hide the working Mac shell when a guest is unavailable.
      const foreground = CinterForgeState.activeTab;
      const needed = foreground?.kind === 'launcher'
        ? 'launcher:' + foreground.backend
        : foreground?.profileId === 'codeoss' ? 'code'
        : (foreground?.backend === 'win' || foreground?.backend === 'linux')
          ? foreground.backend : null;
      if (needed && !CinterForgeState.terminalActionEnabled(needed)) {
        const mac = CinterForgeState.tabs.find(tab => !tab.closed && tab.profileId === 'modern' &&
          tab.backend === 'native' && tab.kind === 'terminal');
        if (mac) void CinterForgeState.activateTab(mac);
        else void CinterForgeState.openSession('modern', 'native');
      }
      if (state === 'failed' && CinterForgeState.guestReady.size === 0)
        CinterForgeState.notice('DevTools guest startup failed. See Server Log; Mac Bash remains available.');
    }
    if (CinterForgeState.retainingPriorLayout && !CinterForgeState.guestRestoreComplete && !CinterForgeState.guestRestoreRunning)
      void CinterForgeState.restoreConnectedGuestTabs();
  };

  // Original source lines 2944–3022
CinterForgeState.restoreConnectedGuestTabs = async function restoreConnectedGuestTabs() {
    if (CinterForgeState.guestRestoreRunning || CinterForgeState.guestRestoreComplete || !CinterForgeState.retainingPriorLayout) return;
    CinterForgeState.guestRestoreRunning = true;
    let allConnected = true;
    try {
      const saved = CinterForgeState.startupGuestRestore;
      const previous = Array.isArray(saved?.tabs) ? saved.tabs.slice(0, 12) : [];
      const restored = CinterForgeState.restoredGuestTabs;
      const serverTab = CinterForgeState.tabs.find(tab => !tab.closed && tab.kind === 'server');
      for (const [index, tab] of previous.entries()) {
        if (tab.kind === 'server') {
          if (serverTab) restored.set(index, serverTab);
          continue;
        }
        if (restored.has(index) && !restored.get(index).closed) continue;
        if (tab.kind === 'terminal' && tab.profileId === 'modern' &&
            (!tab.backend || tab.backend === 'native')) {
          const mac = CinterForgeState.tabs.find(item => !item.closed && item.profileId === 'modern' &&
            item.kind === 'terminal' && item.backend === 'native' &&
            ![...restored.values()].includes(item));
          if (mac) { restored.set(index, mac); continue; }
        }
        const needed = tab.kind === 'launcher' ? 'launcher:' + tab.backend :
          tab.profileId === 'codeoss' ? 'code' :
          ['linux', 'win'].includes(tab.backend) ? tab.backend : null;
        if (needed && !CinterForgeState.terminalActionEnabled(needed)) {
          allConnected = false; // Preserve the unavailable tab for a later launch.
          continue;
        }
        const foreground = CinterForgeState.activeTab;
        const before = CinterForgeState.tabs.length;
        if (tab.kind === 'launcher') await CinterForgeState.openLauncherTab(tab.backend);
        else if (tab.kind === 'terminal' && ['linux', 'win'].includes(tab.backend)) {
          // Guest Bash is *inside Code-OSS only*. Old layouts never open a
          // standalone simulated Session to reconnect a Windows/Linux PTY.
          await CinterForgeState.openSession('codeoss');
        } else await CinterForgeState.openSession(tab.profileId, tab.backend || 'native');
        const opened = CinterForgeState.tabs[before] || CinterForgeState.tabs.find(item => !item.closed &&
          (tab.kind === 'terminal' && ['linux', 'win'].includes(tab.backend)
            ? item.profileId === 'codeoss'
            : item.kind === tab.kind && item.profileId === tab.profileId &&
              item.backend === tab.backend));
        if (opened) restored.set(index, opened);
        else allConnected = false;
        // Creating a background-restored tab must not steal the tab the user
        // was already using. Reapply the saved selection once, below.
        const selectingSavedTab = !CinterForgeState.startupSavedActiveApplied && !CinterForgeState.startupUserSelectedTab &&
          saved?.active === index && opened?.kind !== 'server';
        if (!selectingSavedTab && foreground && !foreground.closed &&
            CinterForgeState.tabs.includes(foreground) && CinterForgeState.activeTab === opened && foreground !== opened)
          await CinterForgeState.activateTab(foreground);
      }
      // Restore a previously selected local machine immediately. If its
      // guest isn't connected yet, leave Mac Bash usable and select it only
      // when verified ready (unless the user has chosen another tab meanwhile).
      const wanted = restored.get(saved?.active);
      if (!CinterForgeState.startupSavedActiveApplied && !CinterForgeState.startupUserSelectedTab) {
        if (wanted && wanted.kind !== 'server') {
          await CinterForgeState.activateTab(wanted);
          CinterForgeState.startupSavedActiveApplied = true;
        } else {
          const mac = CinterForgeState.tabs.find(item => !item.closed && item.kind === 'terminal' &&
            item.profileId === 'modern' && item.backend === 'native');
          if (mac) await CinterForgeState.activateTab(mac);
        }
      }
      if (!allConnected)
        CinterForgeState.notice('Some previous guest tabs remain disconnected; see Server Log');
      else {
        CinterForgeState.guestRestoreComplete = true;
        CinterForgeState.retainingPriorLayout = false;
        CinterForgeState.saveTerminalLayout();
      }
    } catch (error) {
      CinterForgeState.notice('Session restore delayed: ' + error.message);
    } finally {
      CinterForgeState.guestRestoreRunning = false;
    }
  };

  // Original source lines 3025–3025
CinterForgeState.guestStatusPolling = false;

  // Original source lines 3026–3034
setInterval(async () => {
    if (CinterForgeState.platform !== 'mac' || !CinterForgeState.devtoolsInstalled || CinterForgeState.guestStatusPolling) return;
    CinterForgeState.guestStatusPolling = true;
    try { CinterForgeState.updateGuestCapabilities(await CinterForgeState.api('/api/lifecycle/guest-status')); }
    catch (error) {
      // A lost service cannot be evidence that old VNC/PTY connections are live.
      CinterForgeState.updateGuestCapabilities({state: 'unavailable', guests: [], vnc: []});
    } finally { CinterForgeState.guestStatusPolling = false; }
  }, 1500);

})();
