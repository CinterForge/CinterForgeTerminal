/*
 * CinterForgeSessions.js — CinterForge Terminal browser owner
 * PURPOSE: Owns the browser tab registry, active selection, read-only Server Log and tab close/polling operations.
 * METHODS: activateTab, closeTab, pollServerTab, openServerTab
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 736–736
CinterForgeState.display = new CinterForgeState.VTOutput(CinterForgeState.$('terminal-output'));

  // Original source lines 737–737
CinterForgeState.tabBar = CinterForgeState.$('terminal-tabs');

  // Original source lines 738–738
CinterForgeState.tabs = [];

  // Original source lines 739–739
CinterForgeState.activeTab = null;
CinterForgeState.tabSerial = 0;

  // Original source lines 743–748
CinterForgeState.makeBuffer = () => {
    const container = document.createElement('div');
    const pre = document.createElement('pre');
    container.append(pre);
    return pre;
  };

  // Original source lines 752–790
CinterForgeState.renderTabs = () => {
    if (!CinterForgeState.tabBar) return;
    CinterForgeState.tabBar.replaceChildren();
    for (const tab of CinterForgeState.tabs) {
      const tabButton = document.createElement('button');
      tabButton.type = 'button';
      tabButton.className = 'session-tab' + (CinterForgeState.activeTab === tab ? ' active' : '');
      tabButton.textContent = tab.label;
      const tabProfile = CinterForgeState.profiles.find(profile => profile.id === tab.profileId);
      CinterForgeState.appendDecorativeIcon(tabButton, CinterForgeState.iconCategoryForProfile(tabProfile, tab.backend));
      tabButton.setAttribute('role', 'tab');
      tabButton.setAttribute('aria-selected', String(CinterForgeState.activeTab === tab));
      tabButton.addEventListener('click', () => {
        CinterForgeState.startupUserSelectedTab = true;
        void CinterForgeState.activateTab(tab);
      });
      CinterForgeState.tabBar.append(tabButton);
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'session-tab-close';
      close.textContent = '×';
      close.title = 'Close ' + tab.label;
      close.setAttribute('aria-label', 'Close ' + tab.label);
      close.addEventListener('click', () => {
        void CinterForgeState.closeTab(tab);
      });
      CinterForgeState.tabBar.append(close);
    }
    const create = document.createElement('button');
    create.type = 'button';
    create.className = 'session-tab-new';
    create.textContent = '+';
    create.title = 'New terminal session';
    create.setAttribute('aria-label', 'New terminal session');
    create.addEventListener('click', () => {
      void window.CinterTerminalNewTab('mac');
    });
    CinterForgeState.tabBar.append(create);
  };

  // Original source lines 794–886
CinterForgeState.activateTab = async function activateTab(tab) {
    if (!tab || !CinterForgeState.tabs.includes(tab) || CinterForgeState.activeTab === tab) return;
    // The shell, each IF interpreter and each emulator own separate tabs.
    if (CinterForgeState.activeTab?.kind === 'if' && CinterForgeState.activeTab.game) CinterForgeState.activeTab.game.draft = CinterForgeState.$('terminal-input').value;
    if (CinterForgeState.activeTab?.kind === 'discord') CinterForgeState.activeTab.draft = CinterForgeState.$('terminal-input').value;
    if (CinterForgeState.activeTab && CinterForgeState.activeTab.kind !== 'if') {
      CinterForgeState.activeTab.display.target = CinterForgeState.activeTab.buffer;
      CinterForgeState.activeTab.display.render();
    }
    CinterForgeState.activeTab = tab;
    if (tab.kind !== 'bedrock')
      CinterForgeState.$('terminal-output').removeAttribute('data-bedrock-echo');
    CinterForgeState.updateCodeMenuMode();
    CinterForgeState.session = tab.session;
    CinterForgeState.selected = tab.profileId;
    CinterForgeState.display = tab.display;
    // Keep every CraftMaster map iframe alive while switching to Bedrock,
    // Code-OSS or a shell. Hidden maps continue refreshing their ZFY tiles.
    for (const openTab of CinterForgeState.tabs) {
      if ((openTab.kind === 'craftmaster-map' || openTab.kind === 'asset-view') && openTab.mapFrame)
        openTab.mapFrame.hidden = true;
    }
    if (tab.kind === 'craftmaster-map' || tab.kind === 'asset-view') {
      if (CinterForgeState.terminalOverlay.dataset.codeWrapperHidden === 'true')
        CinterForgeState.setCodeWrapperHidden(false);
      CinterForgeState.$('terminal-screen').hidden = true;
      CinterForgeState.$('terminal-input').disabled = true;
      CinterForgeState.$('terminal-keys').hidden = true;
      CinterForgeState.computerView.hidden = false;
      CinterForgeState.codeFrame.hidden = true;
      CinterForgeState.frame.hidden = true;
      CinterForgeState.codeError.hidden = true;
      tab.mapFrame.hidden = false;
      CinterForgeState.$('terminal-shell').textContent = tab.label +
        (tab.kind === 'craftmaster-map' ? ' · live Zoomify map' : ' · canonical Assets Library');
      CinterForgeState.notice(tab.kind === 'craftmaster-map'
        ? 'CraftMaster map refreshes as the native converter writes tiles'
        : 'Assets preview · saved models and animations from the canonical UCI library');
      CinterForgeState.renderTabs();
      CinterForgeState.saveTerminalLayout();
      return;
    }
    if (tab.kind === 'launcher') {
      if (CinterForgeState.terminalOverlay.dataset.codeWrapperHidden === 'true') CinterForgeState.setCodeWrapperHidden(false);
      CinterForgeState.$('terminal-screen').hidden = true;
      CinterForgeState.$('terminal-input').disabled = true;
      CinterForgeState.$('terminal-keys').hidden = true;
      CinterForgeState.computerView.hidden = false;
      CinterForgeState.$('terminal-shell').textContent = tab.label;
      CinterForgeState.$('terminal-machine-title').textContent = tab.label.toUpperCase();
      CinterForgeState.codeFrame.hidden = true;
      CinterForgeState.frame.hidden = false;
      if (CinterForgeState.frame.dataset.launcher !== tab.backend) {
        CinterForgeState.frame.src = '/launcher/index.html?platform=' + tab.backend;
        CinterForgeState.frame.dataset.launcher = tab.backend;
      } else if (tab.launcherResult) {
        CinterForgeState.sendLauncherState(tab, tab.launcherResult);
      }
      CinterForgeState.notice(tab.launcherResult?.error ? tab.label + ': ' + tab.launcherResult.error :
        tab.viewerConnected ? tab.label + ' VNC connected' :
        tab.launcherResult ? tab.label + ' viewer connecting…' : 'Opening ' + tab.label + '…');
      CinterForgeState.renderTabs();
      return;
    }
    if (tab.kind === 'if') {
      CinterForgeState.showTerminal();
      CinterForgeState.applyProfile(CinterForgeState.profiles.find(p => p.id === tab.profileId) || CinterForgeState.profiles[0]);
      CinterForgeState.$('terminal-keys').hidden = false;
      CinterForgeState.$('terminal-input').value = tab.game?.draft || '';
      CinterForgeState.paintIFTab(tab);
      CinterForgeState.focusTerminal();
      CinterForgeState.renderTabs();
      CinterForgeState.saveTerminalLayout();
      return;
    }
    CinterForgeState.$('terminal-input').value = '';
    CinterForgeState.$('terminal-output').removeAttribute('data-if-echo');
    CinterForgeState.$('terminal-output').removeAttribute('data-bedrock-echo');
    CinterForgeState.display.target = CinterForgeState.$('terminal-output');
    CinterForgeState.display.render();
    const profile = CinterForgeState.profiles.find(p => p.id === tab.profileId) || CinterForgeState.profiles[0];
    if (tab.kind === 'discord') {
      CinterForgeState.showTerminal();
      CinterForgeState.applyProfile(profile);
      CinterForgeState.$('terminal-input').disabled = false;
      CinterForgeState.$('terminal-input').value = tab.draft || '';
      CinterForgeState.$('terminal-input').setSelectionRange?.(tab.draft.length, tab.draft.length);
      CinterForgeState.$('terminal-keys').hidden = true;
      CinterForgeState.$('terminal-shell').textContent = `Discord · ${tab.guild} / #${tab.channel} · ${tab.bot}`;
      CinterForgeState.notice('Connected through the configured Discord bot · type a message, Enter to send');
      CinterForgeState.syncGameInput();
      CinterForgeState.$('terminal-input').focus();
    } else if (tab.kind === 'server') {
      CinterForgeState.showTerminal();
      CinterForgeState.applyProfile(profile);
      CinterForgeState.$('terminal-input').disabled = true;
      CinterForgeState.$('terminal-keys').hidden = true;
      CinterForgeState.$('terminal-shell').textContent = 'Server · live HTTP/Python log (read-only)';
      CinterForgeState.notice(tab.logPath ? 'Server log: ' + tab.logPath : 'Connecting to live Server log…');
    } else if (CinterForgeState.museumKind(profile) === 'terminal') {
      CinterForgeState.showTerminal();
      CinterForgeState.applyProfile(profile);
      CinterForgeState.$('terminal-input').disabled = false;
      CinterForgeState.$('terminal-keys').hidden = false;
      CinterForgeState.$('terminal-shell').textContent = tab.session
        ? (tab.kind === 'bedrock'
          ? 'Bedrock · live server console'
          : `${CinterForgeState.platform} · ${tab.label}`)
        : 'Disconnected';
      CinterForgeState.focusTerminal();
      if (tab.session) {
        const size = CinterForgeState.terminalSize();
        CinterForgeState.terminalSocketCommand(tab, {type:'resize', ...size});
      }
    } else {
      await CinterForgeState.showComputer(profile);
      CinterForgeState.updateCodeMenuMode();
      CinterForgeState.$('terminal-overlay').dataset.profile = profile.id;
      CinterForgeState.$('terminal-machine-title').textContent = profile.name.toUpperCase();
      CinterForgeState.$('terminal-shell').textContent = `${profile.year} · ${profile.name}`;
    }
    CinterForgeState.renderTabs();
    CinterForgeState.saveTerminalLayout();
  };

  // Original source lines 891–930
CinterForgeState.closeTab = async function closeTab(tab) {
    if (!CinterForgeState.tabs.includes(tab) || tab.closed) return;
    // Mark closed before any cleanup request. A late worker response must
    // never reselect or resurrect this workbench's tabs or PTYs.
    tab.closed = true;
    // CodeOSS is a singleton until explicitly closed; release its owned
    // worker and backend sessions only on that explicit close, never tab switch.
    if (tab.profileId === 'codeoss') {
      CinterForgeState.codeFrame.src = 'about:blank';
      CinterForgeState.codeFrame.hidden = true;
      delete CinterForgeState.codeFrame.dataset.loaded;
      CinterForgeState.codeLoad++;
      CinterForgeState.codeReady = false;
      const owner = CinterForgeState.codeOwnerId;
      CinterForgeState.codeOwnerId = null;
      if (owner) {
        try { await CinterForgeState.api('/host/code-shell-close-owner', {owner}); }
        catch (e) { console.error('Code-OSS PTY cleanup:', e); }
      }
    }
    if (tab.kind === 'asset-view') tab.mapFrame?.remove();
    if (tab.kind === 'craftmaster-map') {
      tab.mapFrame?.remove();
      if (tab.mapId) void CinterForgeState.api('/api/craftmaster/map/close', {
        id: tab.mapId
      }).catch(() => {});
    }
    // Record a human outcome only after a game tab is intentionally closed.
    // A catalog file is not proof of compatibility, nor a playable URL.
    if (typeof tab.profileId === 'string' && tab.profileId.startsWith('macgame-')) {
      const command = tab.profileId.slice('macgame-'.length);
      const profile = CinterForgeState.profiles.find(item => item.id === tab.profileId);
      if (profile?.gameUnverified) {
        setTimeout(async () => {
          const answer = window.prompt('Did ' + (profile.name || command) +
            ' play correctly? Type yes, no, or skip.', 'skip');
          if (!answer || !['yes', 'no'].includes(answer.trim().toLowerCase())) return;
          try {
            const result = await CinterForgeState.api('/api/games/feedback', {
              command, succeeded: answer.trim().toLowerCase() === 'yes'
            });
            CinterForgeState.notice(result.verification?.verified_url
              ? 'Verified source URL for ' + command
              : 'Recorded ' + command + ' result; no playable URL verified');
          } catch (error) { CinterForgeState.notice('Game feedback: ' + error.message); }
        }, 0);
      }
    }
    // Late PTY connections and IF downloads cannot revive a closed tab.
    tab.pollGeneration = (tab.pollGeneration || 0) + 1;
    if (tab.game) void CinterForgeState.stopGame(tab).catch(error => console.error('IF close:', error));
    const closingSession = tab.session;
    tab.session = null;
    const index = CinterForgeState.tabs.indexOf(tab);
    CinterForgeState.tabs.splice(index, 1);
    if (CinterForgeState.activeTab === tab) {
      CinterForgeState.activeTab = null;
      CinterForgeState.session = null;
      CinterForgeState.$('terminal-input').value = '';
      if (CinterForgeState.tabs.length) await CinterForgeState.activateTab(CinterForgeState.tabs[Math.max(0, index - 1)]);
      else await CinterForgeState.openSession('modern');
    } else CinterForgeState.renderTabs();
    CinterForgeState.saveTerminalLayout();
    if (closingSession) try {
      CinterForgeState.closeTerminalSocket(tab);
      await CinterForgeState.api('/api/terminal/close', { id: closingSession.id });
    }
      catch (_) {/* already closed */ }
  };

  // Original source lines 943–961
CinterForgeState.pollServerTab = async function pollServerTab(tab) {
    const stamp = ++tab.pollGeneration;
    while (!tab.closed && tab.pollGeneration === stamp) {
      try {
        const result = await CinterForgeState.api('/api/server/log?cursor=' + tab.cursor);
        if (tab.closed || tab.pollGeneration !== stamp) return;
        tab.logPath = result.log_path;
        if (result.truncated) tab.display.push('[Server] Earlier events remain in ' + tab.logPath + '\n');
        for (const line of result.lines || []) tab.display.push(line + '\n');
        tab.cursor = result.cursor;
        if (CinterForgeState.activeTab === tab) CinterForgeState.notice('Server log: ' + tab.logPath);
      } catch (error) {
        if (tab.closed || tab.pollGeneration !== stamp) return;
        tab.display.push('[Server] Log connection failed: ' + error.message + '\n');
        if (CinterForgeState.activeTab === tab) CinterForgeState.notice('Server log connection failed');
      }
      await new Promise(resolve => setTimeout(resolve, 750));
    }
  };

  // Original source lines 966–994
CinterForgeState.openServerTab = async function openServerTab(activate = true) {
    const existing = CinterForgeState.tabs.find(tab => !tab.closed && tab.kind === 'server');
    if (existing) {
      if (activate) await CinterForgeState.activateTab(existing);
      return existing;
    }
    const buffer = CinterForgeState.makeBuffer();
    const tab = {
      key: ++CinterForgeState.tabSerial,
      label: 'Server',
      profileId: 'modern',
      backend: 'server',
      kind: 'server',
      session: null,
      closed: false,
      cursor: 0,
      pollGeneration: 0,
      logPath: '',
      display: new CinterForgeState.VTOutput(buffer),
      buffer
    };
    tab.display.target = tab.buffer;
    CinterForgeState.tabs.push(tab);
    if (activate) await CinterForgeState.activateTab(tab);
    else CinterForgeState.renderTabs(); // Keep Server Log streaming without stealing the Mac Bash foreground.
    tab.display.push('Connecting to the CinterForgeTerminal HTTP/Python server log…\n');
    void CinterForgeState.pollServerTab(tab);
    return tab;
  };

})();
