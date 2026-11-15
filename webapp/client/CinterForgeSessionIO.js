/*
 * CinterForgeSessionIO.js — CinterForge Terminal browser owner
 * PURPOSE: Maintains host-backed terminal sessions, PTY polling, resize, open, reconnect and command dispatch.
 * METHODS: pollTab, openSession, adoptBedrockSession, reconnectTab, send, debuggerRequest
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 1768–1770
CinterForgeState.notice = text => {
    CinterForgeState.$('terminal-message').textContent = text;
  };

  // Original source lines 1774–1783
CinterForgeState.applyProfile = profile => {
    const style = CinterForgeState.$('terminal-overlay').style;
    style.setProperty('--bg', profile.background);
    style.setProperty('--fg', profile.foreground);
    style.setProperty('--accent', profile.accent);
    style.setProperty('--columns', String(profile.columns || 80));
    CinterForgeState.$('terminal-overlay').dataset.profile = profile.id;
    CinterForgeState.$('terminal-machine-title').textContent = profile.name.toUpperCase();
    if (!CinterForgeState.acceptUI) localStorage.setItem('cinterterminal.profile.' + CinterForgeState.platform, profile.id);
  };

  // Original source lines 1787–1793
CinterForgeState.terminalSize = () => {
    const el = CinterForgeState.$('terminal-screen');
    return {
      cols: Math.max(40, Math.floor(el.clientWidth / 8.5)),
      rows: Math.max(12, Math.floor(el.clientHeight / 19))
    };
  };

  // Original source lines 1797–1827
CinterForgeState.closeTerminalSocket = function closeTerminalSocket(tab) {
    const connection = tab.sessionSocket;
    tab.sessionSocket = null;
    tab.sessionSocketReady = false;
    tab.pendingTerminalFrames = [];
    if (connection) connection.close();
  };

  // 20300 remains HTTP/API for create and close. 20303 exclusively carries
  // normal live PTY input, output and window resize commands.
  CinterForgeState.terminalSocketCommand = function terminalSocketCommand(tab, command) {
    if (!tab?.session?.id) return false;
    if (tab.sessionSocketReady && tab.sessionSocket?.readyState === WebSocket.OPEN) {
      tab.sessionSocket.send(JSON.stringify(command));
    } else {
      const pending = tab.pendingTerminalFrames || (tab.pendingTerminalFrames = []);
      if (pending.length >= 256) {
        if (CinterForgeState.activeTab === tab) CinterForgeState.notice('Terminal transport is not connected; input queue full');
        return false;
      }
      pending.push(command);
    }
    return true;
  };

  CinterForgeState.pollTab = function pollTab(tab) {
    const id = tab.session?.id;
    if (!id || tab.closed) return;
    if (tab.sessionSocket) CinterForgeState.closeTerminalSocket(tab);
    const socket = new WebSocket(`ws://${location.hostname}:20303/session`);
    tab.sessionSocket = socket;
    tab.sessionSocketReady = false;
    tab.pendingTerminalFrames = tab.pendingTerminalFrames || [];
    tab.terminalReconnects = tab.terminalReconnects || 0;
    socket.addEventListener('open', () => {
      if (tab.closed || tab.session?.id !== id || tab.sessionSocket !== socket) return socket.close();
      socket.send(JSON.stringify({type:'attach', id, token:CinterForgeState.token}));
    });
    socket.addEventListener('message', event => {
      if (tab.closed || tab.session?.id !== id || tab.sessionSocket !== socket) return;
      let message;
      try { message = JSON.parse(event.data); }
      catch (_) { CinterForgeState.notice('Invalid terminal WebSocket message'); return; }
      if (message.type === 'attached') {
        tab.sessionSocketReady = true;
        tab.terminalReconnects = 0;
        for (const command of (tab.pendingTerminalFrames || []))
          socket.send(JSON.stringify(command));
        tab.pendingTerminalFrames = [];
        if (CinterForgeState.activeTab === tab) CinterForgeState.notice('Terminal connected on 20303');
      } else if (message.type === 'output') {
        if (message.output) tab.display.push(CinterForgeState.decode(message.output));
        if (message.bedrock_eula_requested && CinterForgeState.activeTab === tab) CinterForgeState.requestLicense();
        if (message.closed) {
          CinterForgeState.closeTerminalSocket(tab);
          if (tab.kind === 'bedrock') void CinterForgeState.api('/api/terminal/close', {id}).catch(() => {});
          if (CinterForgeState.activeTab === tab) CinterForgeState.notice(tab.kind === 'bedrock' ? 'Bedrock server exited' : 'Shell exited');
          tab.session = null;
          if (CinterForgeState.activeTab === tab) CinterForgeState.session = null;
          CinterForgeState.renderTabs();
        }
      } else if (message.error) {
        if (CinterForgeState.activeTab === tab) CinterForgeState.notice(message.error);
        CinterForgeState.closeTerminalSocket(tab);
      }
    });
    socket.addEventListener('close', () => {
      if (tab.sessionSocket !== socket || tab.closed || tab.session?.id !== id) return;
      tab.sessionSocket = null;
      tab.sessionSocketReady = false;
      if (tab.terminalReconnects++ < 3) {
        setTimeout(() => {
          if (!tab.closed && tab.session?.id === id && !tab.sessionSocket)
            CinterForgeState.pollTab(tab);
        }, 500 * tab.terminalReconnects);
      } else if (CinterForgeState.activeTab === tab) {
        CinterForgeState.notice('Terminal WebSocket 20303 disconnected; use Reconnect');
      }
    });
  };

  // Original source lines 1832–1952
CinterForgeState.openSession = async function openSession(profileId, backend = 'native') {
    // The museum, typed `code`, restored layouts and native menus all go
    // through this single readiness gate. A saved Code-OSS tab must never
    // load guest PTYs while the VM is still connecting.
    if (profileId === 'codeoss' && !CinterForgeState.terminalActionEnabled('code')) {
      CinterForgeState.notice('Code-OSS disabled until installed DevTools guests connect; see Server Log');
      return;
    }
    if ((backend === 'linux' || backend === 'win') && !CinterForgeState.terminalActionEnabled(backend)) {
      CinterForgeState.notice(backend + ' Bash is not connected; see Server Log');
      return;
    }
    // CodeOSS owns a single workbench and guest terminal set. A second
    // click or restored layout MUST focus, not allocate a second worker.
    if (profileId === 'codeoss') {
      const current = CinterForgeState.tabs.find(tab => !tab.closed && tab.profileId === 'codeoss');
      if (current) return CinterForgeState.activateTab(current);
    }
    // Do not drop a native/File-menu request while the initial PTY is still
    // connecting. Windows WebView2 can deliver the click during first startup.
    if (CinterForgeState.waiting) {
      return new Promise((resolve, reject) => CinterForgeState.queuedSessionOpens.push({ profileId, backend, resolve, reject }));
    }
    if (backend === 'minecraft') {
      // Menu and API both gate the attachment: never start a Bedrock VM here.
      try {
        const state = await CinterForgeState.api('/api/bedrock/status');
        if (!(state.running || state.starting)) {
          CinterForgeState.notice('Minecraft is disabled until bedrock run starts a server');
          return;
        }
      } catch (error) {
        CinterForgeState.notice('Minecraft unavailable: ' + error.message);
        return;
      }
    }
    CinterForgeState.waiting = true;
    const profile = CinterForgeState.profiles.find(p => p.id === profileId) || CinterForgeState.profiles[0];
    if (!profile) {
      CinterForgeState.waiting = false;
      CinterForgeState.notice('No terminal profiles available');
      return;
    }
    const buffer = CinterForgeState.makeBuffer();
    const tab = {
      key: ++CinterForgeState.tabSerial,
      label: backend === 'minecraft' ? 'Minecraft' :
        backend === 'linux' ? 'Linux Bash' : backend === 'win' ? 'Windows Bash' :
          (CinterForgeState.platform === 'mac' && backend === 'native' && profile.id === 'modern')
            ? 'Mac Bash' : CinterForgeState.museumKind(profile) === 'joshua' ? 'JOSHUA' : profile.name,
      profileId: profile.id,
      backend,
      kind: CinterForgeState.museumKind(profile),
      session: null,
      closed: false,
      display: new CinterForgeState.VTOutput(buffer),
      buffer
    };
    // Every tab owns its renderer and shell, but only the active tab renders
    // into the shared Terminal screen and receives keyboard input.
    tab.display.target = tab.buffer;
    CinterForgeState.tabs.push(tab);
    if (CinterForgeState.activeTab?.kind === 'if' && CinterForgeState.activeTab.game) CinterForgeState.activeTab.game.draft = CinterForgeState.$('terminal-input').value;
    if (CinterForgeState.activeTab?.kind === 'discord') CinterForgeState.activeTab.draft = CinterForgeState.$('terminal-input').value;
    if (CinterForgeState.activeTab && CinterForgeState.activeTab.kind !== 'if') {
      CinterForgeState.activeTab.display.target = CinterForgeState.activeTab.buffer;
      CinterForgeState.activeTab.display.render();
    }
    CinterForgeState.activeTab = tab;
    CinterForgeState.updateCodeMenuMode();
    CinterForgeState.session = null;
    CinterForgeState.selected = profile.id;
    CinterForgeState.display = tab.display;
    CinterForgeState.$('terminal-input').value = '';
    CinterForgeState.$('terminal-output').removeAttribute('data-if-echo');
    CinterForgeState.display.target = CinterForgeState.$('terminal-output');
    CinterForgeState.display.render();
    CinterForgeState.renderTabs();
    try {
      if (!CinterForgeState.acceptUI && !profile.lagomorph && !profile.macGame && tab.kind === 'terminal')
        if (!CinterForgeState.acceptUI) localStorage.setItem('cinterterminal.profile.' + CinterForgeState.platform, profile.id);
      if (tab.kind !== 'terminal') {
        CinterForgeState.notice('Opening ' + profile.name + ' emulator…');
        await CinterForgeState.showComputer(profile);
        CinterForgeState.updateCodeMenuMode();
        CinterForgeState.$('terminal-overlay').dataset.profile = profile.id;
        CinterForgeState.$('terminal-machine-title').textContent = profile.name.toUpperCase();
        CinterForgeState.$('terminal-shell').textContent = `${profile.year} · ${profile.name}`;
        await CinterForgeState.nativeEvent('machine', { id: profile.id, kind: tab.kind });
        return;
      }
      CinterForgeState.showTerminal();
      CinterForgeState.applyProfile(profile);
      CinterForgeState.notice('Connecting to ' + profile.name + ' session…');
      const created = await CinterForgeState.api('/api/terminal/open', { ...CinterForgeState.terminalSize(), profile: profile.id, backend });
      if (tab.closed) {
        await CinterForgeState.api('/api/terminal/close', { id: created.id }).catch(() => { });
        return;
      }
      tab.session = created;
      if (CinterForgeState.activeTab === tab) {
        CinterForgeState.session = created;
        CinterForgeState.$('terminal-shell').textContent = `${created.platform} · ${created.shell}`;
        CinterForgeState.notice('Connected · type help for CinterForgeTerminal commands');
        CinterForgeState.$('terminal-input').focus();
      }
      await CinterForgeState.nativeEvent('terminal', { id: profile.id, kind: 'terminal' });
      void CinterForgeState.pollTab(tab);
    } catch (e) {
      if (CinterForgeState.activeTab === tab) {
        CinterForgeState.notice(e.message);
        tab.display.push(e.message + '\n');
      }
      await CinterForgeState.nativeEvent('error', { message: e.message });
    } finally {
      CinterForgeState.waiting = false;
      CinterForgeState.renderTabs();
      const next = CinterForgeState.queuedSessionOpens.shift();
      if (next) void CinterForgeState.openSession(next.profileId, next.backend).then(next.resolve, next.reject);
    }
  };

  // Original source lines 1960–1983
CinterForgeState.adoptBedrockSession = async function adoptBedrockSession(created) {
    if (!created?.id || CinterForgeState.tabs.some(tab => tab.session?.id === created.id)) {
      const existing = CinterForgeState.tabs.find(tab => tab.session?.id === created?.id);
      if (existing) await CinterForgeState.activateTab(existing);
      return;
    }
    const buffer = CinterForgeState.makeBuffer();
    const tab = {
      key: ++CinterForgeState.tabSerial,
      label: 'Bedrock',
      profileId: 'modern',
      kind: 'bedrock',
      session: created,
      closed: false,
      display: new CinterForgeState.VTOutput(buffer),
      buffer
    };
    tab.display.target = tab.buffer;
    CinterForgeState.tabs.push(tab);
    await CinterForgeState.activateTab(tab);
    CinterForgeState.$('terminal-shell').textContent = 'Bedrock · live server console';
    CinterForgeState.notice('Bedrock has its own live two-way terminal; the previous session is unchanged');
    void CinterForgeState.pollTab(tab);
  };

  // Original source lines 1984–1984
CinterForgeState.adoptingBedrock = false;

  // Original source lines 1985–1995
setInterval(async () => {
    if (CinterForgeState.adoptingBedrock || document.visibilityState === 'hidden') return;
    CinterForgeState.adoptingBedrock = true;
    try {
      const created = await CinterForgeState.api('/api/bedrock/terminal/next');
      if (created?.id) await CinterForgeState.adoptBedrockSession(created);
    } catch (_) { /* Browser or host may be shutting down. */ }
    finally {
      CinterForgeState.adoptingBedrock = false;
    }
  }, 500);

  // Original source lines 1999–2047
CinterForgeState.reconnectTab = async function reconnectTab() {
    if (!CinterForgeState.activeTab) {
      await CinterForgeState.openSession('modern');
      return;
    }
    const tab = CinterForgeState.activeTab;
    if (tab.kind === 'server') {
      tab.pollGeneration++;
      tab.cursor = 0;
      tab.display.reset();
      void CinterForgeState.pollServerTab(tab);
      return;
    }
    if (tab.kind === 'bedrock') {
      CinterForgeState.notice('Bedrock is a dedicated server Session. Start a new world with bedrock run from a regular terminal.');
      return;
    }
    if (tab.kind === 'if') {
      CinterForgeState.notice(tab.game ? 'Story is running in its own tab' : 'Story ended. Launch it again from a shell.');
      return;
    }
    if (tab.kind !== 'terminal') {
      // Reconnect is not permission to reconstruct a live Code-OSS workbench.
      if (tab.profileId === 'codeoss') return;
      await CinterForgeState.showComputer(CinterForgeState.profiles.find(p => p.id === tab.profileId));
      return;
    }
    CinterForgeState.closeTerminalSocket(tab);
    if (tab.session) try {
      await CinterForgeState.api('/api/terminal/close', { id: tab.session.id });
    } catch (_) {/* closed */ }
    tab.session = null;
    CinterForgeState.session = null;
    tab.display.reset();
    try {
      const created = await CinterForgeState.api('/api/terminal/open', {
        ...CinterForgeState.terminalSize(),
        profile: tab.profileId,
        backend: tab.backend || 'native'
      });
      tab.session = created;
      CinterForgeState.session = created;
      CinterForgeState.$('terminal-shell').textContent = `${created.platform} · ${created.shell}`;
      CinterForgeState.notice('Connected');
      void CinterForgeState.pollTab(tab);
      CinterForgeState.focusTerminal();
    } catch (error) {
      CinterForgeState.notice(error.message);
    }
  };

  // Original source lines 2052–2059
CinterForgeState.send = function send(data) {
    if (CinterForgeState.activeTab?.kind === 'if' || CinterForgeState.activeTab?.kind === 'discord') return;
    const tab = CinterForgeState.activeTab;
    if (tab?.session) CinterForgeState.terminalSocketCommand(tab, {type:'input', data});
  };

  // Original source lines 2064–2072
CinterForgeState.debuggerRequest = async function debuggerRequest() {
    try {
      await CinterForgeState.api('/api/debugger', {});
      CinterForgeState.notice('Inspector requested; available when the native Host enables it.');
    }
    catch (e) {
      CinterForgeState.notice(e.message);
    }
  };

})();
