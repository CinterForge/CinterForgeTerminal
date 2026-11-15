/*
 * CinterForgeLaunchers.js — CinterForge Terminal browser owner
 * PURPOSE: Displays and launches Mac/Linux/Windows graphical guest desktops after verified backend readiness.
 * METHODS: sendLauncherState, openLauncherTab
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 999–999
CinterForgeState.launcherKinds = Object.freeze({mac: 'Mac Launcher', win: 'Windows Launcher', linux: 'Linux Launcher'});

  // Original source lines 1000–1000
CinterForgeState.launcherApps = new Set(['CinterForgeTerminal']);

  // Original source lines 1001–1005
CinterForgeState.sendLauncherState = function sendLauncherState(tab, data) {
    if (tab.closed || CinterForgeState.activeTab !== tab || CinterForgeState.frame.dataset.launcher !== tab.backend) return;
    CinterForgeState.frame.contentWindow?.postMessage({type: 'cinter-launcher-session', platform: tab.backend,
      ...data}, location.origin);
  };

  // Original source lines 1006–1009
CinterForgeState.frame.addEventListener('load', () => {
    if (CinterForgeState.activeTab?.kind === 'launcher' && CinterForgeState.activeTab.launcherResult)
      CinterForgeState.sendLauncherState(CinterForgeState.activeTab, CinterForgeState.activeTab.launcherResult);
  });

  // Original source lines 1010–1036
CinterForgeState.openLauncherTab = async function openLauncherTab(kind) {
    if (!Object.hasOwn(CinterForgeState.launcherKinds, kind) || !CinterForgeState.terminalActionEnabled('launcher:' + kind)) return;
    const previous = CinterForgeState.tabs.find(tab => !tab.closed && tab.kind === 'launcher' && tab.backend === kind);
    if (previous) { await CinterForgeState.activateTab(previous); return previous; }
    const buffer = CinterForgeState.makeBuffer();
    const tab = {
      key: ++CinterForgeState.tabSerial, label: CinterForgeState.launcherKinds[kind], backend: kind,
      kind: 'launcher', profileId: 'modern', session: null, closed: false,
      display: new CinterForgeState.VTOutput(buffer), buffer, launcherResult: null
    };
    tab.display.target = tab.buffer;
    CinterForgeState.tabs.push(tab);
    await CinterForgeState.activateTab(tab);
    try {
      const result = await CinterForgeState.api('/api/launcher/start', {platform: kind});
      if (tab.closed) return tab;
      tab.launcherResult = result;
      CinterForgeState.sendLauncherState(tab, result);
      if (CinterForgeState.activeTab === tab) CinterForgeState.notice(tab.label + ' ready');
    } catch (error) {
      if (tab.closed) return tab;
      tab.launcherResult = {error: error.message};
      CinterForgeState.sendLauncherState(tab, tab.launcherResult);
      if (CinterForgeState.activeTab === tab) CinterForgeState.notice(tab.label + ': ' + error.message);
    }
    return tab;
  };

  // Original source lines 1037–1053
window.addEventListener('message', event => {
    const message = event.data;
    if (event.origin !== location.origin || event.source !== CinterForgeState.frame.contentWindow ||
        message?.type !== 'cinter-launcher') return;
    if (message.action === 'open' && Object.hasOwn(CinterForgeState.launcherKinds, message.platform)) {
      void CinterForgeState.openLauncherTab(message.platform);
      return;
    }
    if (message.action !== 'launch' || !CinterForgeState.launcherApps.has(message.app) ||
        CinterForgeState.activeTab?.kind !== 'launcher' || CinterForgeState.activeTab.backend !== message.platform) return;
    const tab = CinterForgeState.activeTab;
    tab.launcherResult = {message: 'Starting ' + message.app + ' — checking the X11 window…'};
    CinterForgeState.sendLauncherState(tab, tab.launcherResult);
    void CinterForgeState.api('/api/launcher/launch', {platform: tab.backend, app: message.app})
      .then(result => {
        if (tab.closed) return;
        tab.launcherResult = {message: result.message ||
          (result.opened ? message.app + ' window visible' : message.app + ' did not open')};
        CinterForgeState.sendLauncherState(tab, tab.launcherResult);
      })
      .catch(error => {
        if (tab.closed) return;
        // Retain the actual launch error across tab switches; do not only
        // record it in the Server log or discard an inactive iframe message.
        tab.launcherResult = {error: error.message};
        CinterForgeState.sendLauncherState(tab, tab.launcherResult);
        if (CinterForgeState.activeTab === tab)
          CinterForgeState.notice(tab.label + ': ' + error.message);
      });
  });

})();
