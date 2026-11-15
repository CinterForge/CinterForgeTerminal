/*
 * CinterForgeLAN.js — CinterForge Terminal browser owner
 * PURPOSE: Handles connecting to discovered LAN endpoints and the LAN host menu.
 * METHODS: Event handlers and per-tab state.
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 1191–1225
window.CinterTerminalNewTab = async (kind = 'mac') => {
    // Launchers are singleton graphical Sessions, never Code-OSS shell PTYs.
    if (typeof kind === 'string' && !CinterForgeState.terminalActionEnabled(kind)) return;
    if (typeof kind === 'string' && kind.startsWith('launcher:'))
      return CinterForgeState.openLauncherTab(kind.slice('launcher:'.length));
    if (kind === 'code') {
      const existing = CinterForgeState.tabs.find(tab => !tab.closed && tab.profileId === 'codeoss');
      return existing ? CinterForgeState.activateTab(existing) : CinterForgeState.openSession('codeoss');
    }
    // Guest Bash belongs EXCLUSIVELY inside Code-OSS, never an outer Session.
    // Opening it outside Code focuses the workbench; its extension owns the
    // single Linux/Windows shell and creates no duplicate on repeated requests.
    if (kind === 'linux' || kind === 'win') {
      if (CinterForgeState.codeIsActive()) return CinterForgeState.sendCodeCommand('new-terminal', {backend: kind});
      const existing = CinterForgeState.tabs.find(tab => !tab.closed && tab.profileId === 'codeoss');
      return existing ? CinterForgeState.activateTab(existing) : CinterForgeState.openSession('codeoss');
    }
    // The same menu always has the same label. Its destination depends on mode.
    // In Code: create a VS Code integrated terminal tab. Else: outer Session tab.
    if (CinterForgeState.codeIsActive() && !String(kind).startsWith('profile:')) {
      return CinterForgeState.sendCodeCommand('new-terminal', {backend: kind === 'code' ? 'native' : kind});
    }

    if (typeof kind === 'string' && kind.startsWith('profile:')) {
      const id = kind.slice(8);
      if (CinterForgeState.profiles.some(profile => profile.id === id && !profile.lagomorph))
        return CinterForgeState.openSession(id);
      CinterForgeState.notice('Unknown museum machine');
      return;
    }
    if (kind === 'server') return CinterForgeState.openServerTab();
    if (kind === 'mac' || kind === 'native') return CinterForgeState.openSession('modern');
    if (kind === 'minecraft') return CinterForgeState.openSession('modern', 'minecraft');
    CinterForgeState.notice('Unknown Terminal mode: ' + String(kind));
  };

  // Original source lines 1237–1260
CinterForgeState.connectToLAN = async url => {
    try {
      const parsed = new URL(url);
      const octets = parsed.hostname.split('.').map(Number);
      const lanAddress = octets.length === 4 &&
        octets.every(part => Number.isInteger(part) && part >= 0 && part <= 255) &&
        (octets[0] === 10 || octets[0] === 127 ||
          (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
          (octets[0] === 192 && octets[1] === 168) ||
          (octets[0] === 169 && octets[1] === 254));
      const servicePort = Number(parsed.port);
      if (parsed.protocol !== 'http:' || !lanAddress ||
        !Number.isInteger(servicePort) || servicePort < 20300 || servicePort > 20399 ||
        parsed.username || parsed.password || parsed.search || parsed.hash)
        throw new Error('Use a CinterForge LAN IP and HTTP port 20300–20399');
      if (typeof window.cinterConnect === 'function') {
        const result = await window.cinterConnect({ url: parsed.href });
        if (result === 'invalid LAN URL') throw new Error(result);
      } else window.open(parsed.href, '_blank', 'noopener');
      CinterForgeState.$('lan-menu').open = false;
    } catch (error) {
      CinterForgeState.notice('LAN connection: ' + error.message);
    }
  };

  // Original source lines 1264–1309
CinterForgeState.refreshLAN = async () => {
    const container = CinterForgeState.$('lan-menu-list');
    if (!container) return;
    const [info, found] = await Promise.all([CinterForgeState.api('/api/lan/info'), CinterForgeState.api('/api/lan/peers')]);
    container.replaceChildren();
    /**
     * Add a plain-text informational row to the LAN connection menu.
     */
    const line = text => {
      const item = document.createElement('div');
      item.className = 'museum-entry';
      item.textContent = text;
      container.append(item);
    };
    line(info.enabled ? `${info.name} · port ${info.port}` : 'LAN server unavailable');
    line(info.configured ? `Login: ${info.username}` : 'Login not configured — run setpass in a Terminal shell');
    const peers = found.peers || [];
    if (!peers.length) line('No other CinterForge web services discovered');
    for (const peer of peers) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'museum-entry';
      button.textContent = `${peer.name} (${peer.app || 'web'}) · ${peer.url}`;
      button.addEventListener('click', () => {
        void CinterForgeState.connectToLAN(peer.url);
      });
      container.append(button);
    }
    const manual = document.createElement('form');
    manual.className = 'lan-manual';
    const field = document.createElement('input');
    field.placeholder = 'LAN IP[:port] (e.g. 192.168.1.20:20310)';
    field.setAttribute('aria-label', 'Connect to LAN IP address');
    const button = document.createElement('button');
    button.type = 'submit';
    button.textContent = 'Connect';
    manual.append(field, button);
    container.append(manual);
    manual.addEventListener('submit', event => {
      event.preventDefault();
      const input = field.value.trim().replace(/^https?:\/\//, '');
      const destination = /:[0-9]{5}(?:\/|$)/.test(input) ? input : input + ':20300/';
      void CinterForgeState.connectToLAN('http://' + destination);
    });
    void CinterForgeState.nativeEvent('lan-peers', { peers });
  };

  // Original source lines 1313–1318
window.CinterShowLAN = () => {
    const menu = CinterForgeState.$('lan-menu');
    if (menu) {
      if (menu.open) void CinterForgeState.refreshLAN().catch(error => CinterForgeState.notice('LAN: ' + error.message)); else menu.open = true;
    }
  };

  // Original source lines 1319–1321
CinterForgeState.$('lan-menu')?.addEventListener('toggle', event => {
    if (event.target.open) void CinterForgeState.refreshLAN().catch(error => CinterForgeState.notice('LAN: ' + error.message));
  });

})();
