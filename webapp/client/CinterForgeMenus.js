/*
 * CinterForgeMenus.js — CinterForge Terminal browser owner
 * PURPOSE: Shares native and HTML menu actions with live Code-OSS menu state and guest capability checks.
 * METHODS: requestCodeServiceMenus, terminalActionEnabled, terminalItems, codeMenuSnapshot, paintCodeMenuList, refreshCodeMenu, updateCodeMenuMode, readCodeServiceItems
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 2306–2306
CinterForgeState.codeMenuZone = CinterForgeState.$('code-menu-zone');

  // Original source lines 2307–2307
CinterForgeState.codeMenuRoots = new Map();

  // Original source lines 2308–2308
CinterForgeState.codeMenuNames = CinterForgeState.CODE_MENU_NAMES;

  // Original source lines 2309–2320
CinterForgeState.cinterTerminalActions = [
    ['code', 'New CinterForge Code Terminal'],
    ['mac', 'New Mac Bash'],
    ['linux', 'Open Linux Bash in Code-OSS'],
    ['win', 'Open Windows Bash in Code-OSS'],
    ['launcher:mac', 'Mac Launcher'],
    ['launcher:win', 'Windows Launcher'],
    ['launcher:linux', 'Linux Launcher'],
    ['server', 'New Server Log Terminal'],
    ['if', 'New Z-Machine Terminal'],
    ['minecraft', 'New Minecraft Terminal']
  ];

  // Original source lines 2325–2325
CinterForgeState.codeServiceMenus = new Map();

  // Original source lines 2326–2326
CinterForgeState.codeServiceActions = new Set();

  // Original source lines 2330–2335
CinterForgeState.requestCodeServiceMenus = function requestCodeServiceMenus() {
    if (!CinterForgeState.codeIsActive() || !CinterForgeState.codeFrame.contentWindow) return;
    CinterForgeState.codeFrame.contentWindow.postMessage({
      type: 'cinterforge-codeoss-menu-request'
    }, window.location.origin);
  };

  // Original source lines 2339–2351
CinterForgeState.terminalActionEnabled = function terminalActionEnabled(kind) {
    if (kind === 'mac') return CinterForgeState.platform === 'mac';
    if (kind === 'code') return CinterForgeState.platform !== 'mac' || (CinterForgeState.devtoolsInstalled && CinterForgeState.guestReady.size > 0);
    if (kind === 'win') return CinterForgeState.platform === 'win' ||
      (CinterForgeState.platform === 'mac' && CinterForgeState.devtoolsInstalled && CinterForgeState.guestReady.has('win'));
    if (kind === 'linux') return CinterForgeState.platform === 'linux' ||
      (CinterForgeState.platform === 'mac' && CinterForgeState.devtoolsInstalled && CinterForgeState.guestReady.has('linux'));
    if (kind === 'launcher:mac') return CinterForgeState.platform === 'mac';
    if (kind === 'launcher:win' || kind === 'launcher:linux')
      return CinterForgeState.platform === 'mac' && CinterForgeState.devtoolsInstalled && CinterForgeState.guestVncReady.has(kind.slice(9));
    if (kind === 'minecraft') return Boolean(CinterForgeState.minecraftMenuState);
    return true; // Existing working non-platform actions retain their behavior.
  };

  // Original source lines 2352–2359
CinterForgeState.codeTerminalKinds = Object.freeze({
    'cinterforge.newMacTerminal': 'mac',
    'cinterforge.newWindowsTerminal': 'win',
    'cinterforge.newLinuxTerminal': 'linux',
    'cinterforge.openMacLauncher': 'launcher:mac',
    'cinterforge.openWindowsLauncher': 'launcher:win',
    'cinterforge.openLinuxLauncher': 'launcher:linux'
  });

  // Original source lines 2360–2367
  CinterForgeState.devToolsRequestable = kind => CinterForgeState.platform === 'mac' &&
    (kind === 'code' || kind === 'win' || kind === 'linux' ||
     kind === 'launcher:win' || kind === 'launcher:linux');

CinterForgeState.terminalItems = function terminalItems() {
    // The EXISTING CinterForge application commands, used outside Code mode.
    // Code mode uses the real Code-OSS menu without overriding any of its items.
    return CinterForgeState.cinterTerminalActions.map(([kind, title]) => ({
      title, action: `terminal:${kind}`,
      enabled: CinterForgeState.terminalActionEnabled(kind) || CinterForgeState.devToolsRequestable(kind)
    }));
  };

  // Original source lines 2369–2421
CinterForgeState.codeMenuSnapshot = function codeMenuSnapshot(name) {
    const active = CinterForgeState.codeIsActive();
    // Code-OSS owns the editor menu definitions. Keep the SAME live labels
    // outside Code, but disable actions that need the Code workbench. No
    // second hard-coded editor command catalog and no disappearing menus.
    const editorItems = (CinterForgeState.codeServiceMenus.get(name) || []).map(function clone(item) {
      if (item.separator) return {separator: true};
      const kind = String(item.action || '').startsWith('terminal:')
        ? item.action.slice(9) : null;
      return {...item, enabled: kind ? CinterForgeState.terminalActionEnabled(kind) :
        (active && item.enabled !== false),
        ...(Array.isArray(item.children) ? {children: item.children.map(clone)} : {})};
    });
    if (name === 'Terminal') {
      // Real CodeOSS MenuRegistry commands may have their own enable state.
      // Never let one enable a platform action forbidden by this host.
      for (const item of editorItems) {
        const kind = CinterForgeState.codeTerminalKinds[String(item.action || '').replace(/^code:/, '')];
        if (kind && !CinterForgeState.terminalActionEnabled(kind)) item.enabled = false;
      }
    }
    // Before the first Code workbench has supplied its real menu model,
    // show a disabled editor placeholder instead of an apparently missing
    // or differently structured menu. Never invent runnable editor actions.
    if (!editorItems.length && name !== 'Terminal')
      editorItems.push({title: 'Code-OSS commands (open the workbench to populate)',
        action: 'code:unavailable', enabled: false});
    const result = [...editorItems];
    const append = items => {
      if (items.length && result.length) result.push({separator: true});
      result.push(...items);
    };
    if (name === 'File') append([
      {title: 'Relaunch CinterForge Terminal', action: 'app:relaunch', enabled: true},
      {title: 'Quit CinterForge Terminal', action: 'app:quit', enabled: true}
    ]);
    if (name === 'Edit') {
      // Outside Code the standard Copy/Paste labels keep their native actions.
      for (const [title, action] of [['Copy', 'edit:copy'], ['Paste', 'edit:paste']]) {
        const item = result.find(value => value.title === title);
        if (item && !active) Object.assign(item, {action, enabled: true});
        if (!item) result.push({title, action, enabled: true});
      }
    }
    if (name === 'Terminal') {
      // Add the complete Terminal action catalog in both modes. The pinned
      // Code workbench contributes the same Launcher commands natively;
      // avoid duplicate entries if its live model already supplies them.
      const existingTitles = new Set(editorItems.map(item => item.title));
      append(CinterForgeState.terminalItems().filter(item => !existingTitles.has(item.title)));
    }
    return result;
  };

  // Original source lines 2423–2455
CinterForgeState.paintCodeMenuList = function paintCodeMenuList(container, items) {
    container.replaceChildren();
    for (const item of items) {
      if (item.separator) {
        const separator = document.createElement('hr');
        separator.setAttribute('role', 'separator');
        container.append(separator);
        continue;
      }
      if (Array.isArray(item.children) && item.children.length) {
        const submenu = document.createElement('details');
        submenu.className = 'terminal-menu terminal-code-submenu';
        const label = document.createElement('summary');
        label.textContent = item.title;
        label.setAttribute('aria-disabled', String(item.enabled === false));
        const children = document.createElement('div');
        children.className = 'terminal-menu-list';
        CinterForgeState.paintCodeMenuList(children, item.children);
        submenu.append(label, children);
        container.append(submenu);
        continue;
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = item.title;
      button.disabled = item.enabled === false;
      button.addEventListener('click', () => {
        for (const menu of CinterForgeState.codeMenuRoots.values()) menu.open = false;
        void window.CinterMenuDispatch(item.action);
      });
      container.append(button);
    }
  };

  // Original source lines 2457–2463
CinterForgeState.refreshCodeMenu = function refreshCodeMenu(name) {
    const entry = CinterForgeState.codeMenuRoots.get(name);
    if (!entry) return;
    const items = CinterForgeState.codeMenuSnapshot(name);
    CinterForgeState.paintCodeMenuList(entry.cinterList, items);
    void CinterForgeState.nativeEvent('code-menu-model', {name, items});
  };

  // Original source lines 2465–2477
CinterForgeState.updateCodeMenuMode = function updateCodeMenuMode() {
    const enabled = CinterForgeState.codeIsActive();
    for (const [name, entry] of CinterForgeState.codeMenuRoots) {
      // Menus stay present and selectable in both modes. Individual
      // unsupported commands are visibly disabled by codeMenuSnapshot().
      const disabled = false;
      entry.classList?.toggle?.('code-menu-disabled', disabled);
      entry.querySelector?.('summary')?.setAttribute?.('aria-disabled', String(disabled));
      if (!enabled) entry.open = false;
    }
    void CinterForgeState.nativeEvent('code-menu-mode', {enabled});
    for (const name of CinterForgeState.codeMenuNames) CinterForgeState.refreshCodeMenu(name);
  };

  // Original source lines 2480–2503
window.CinterMenuDispatch = async function(action) {
    if (action === 'app:quit') {
      CinterForgeState.saveTerminalLayout();
      if (typeof window.cinterWindow === 'function') await window.cinterWindow({action: 'quit'});
      return;
    }
    if (action === 'app:relaunch') {
      CinterForgeState.saveTerminalLayout();
      if (typeof window.cinterWindow === 'function') await window.cinterWindow({action: 'relaunch'});
      else CinterForgeState.notice('Relaunch is available in the native application');
      return;
    }
    if (action === 'edit:copy') return CinterForgeState.copyTranscript();
    if (action === 'edit:paste') return CinterForgeState.pasteClipboard();
    if (action.startsWith('terminal:')) {
      const kind = action.slice(9);
      if (!CinterForgeState.terminalActionEnabled(kind) && !CinterForgeState.devToolsRequestable(kind)) return;
      return window.CinterTerminalNewTab(kind);
    }
    if (!CinterForgeState.codeIsActive() || !action.startsWith('code:') || !CinterForgeState.codeServiceActions.has(action)) return;
    CinterForgeState.codeFrame.contentWindow?.postMessage({
      type: 'cinterforge-codeoss-menu-run', action: action.slice(5)
    }, window.location.origin);
  };

  // Original source lines 2508–2517
CinterForgeState.hostTerminalMenuActions = Object.freeze({
    'New Terminal': 'native',
    'New Mac Bash': 'mac',
    'Open Linux Bash in Code-OSS': 'linux',
    'Open Windows Bash in Code-OSS': 'win',
    'New Server Log Terminal': 'server',
    'Mac Launcher': 'launcher:mac',
    'Windows Launcher': 'launcher:win',
    'Linux Launcher': 'launcher:linux'
  });

  // Original source lines 2518–2539
CinterForgeState.readCodeServiceItems = function readCodeServiceItems(items, sourceActions, menuName, depth = 0) {
    if (!Array.isArray(items) || depth > 8 || items.length > 200) return [];
    const output = [];
    for (const item of items) {
      if (item?.separator === true) { output.push({separator: true}); continue; }
      if (typeof item?.title !== 'string' || typeof item?.action !== 'string' ||
          item.title.length > 240 || item.action.length > 180) continue;
      const hostKind = menuName === 'Terminal' && depth === 0
        ? CinterForgeState.hostTerminalMenuActions[item.title] : null;
      const normalized = {title: item.title,
        action: hostKind ? `terminal:${hostKind}` : 'code:' + item.action,
        enabled: hostKind ? CinterForgeState.terminalActionEnabled(hostKind) : item.enabled !== false};
      if (Array.isArray(item.children)) {
        normalized.children = CinterForgeState.readCodeServiceItems(item.children, sourceActions,
          menuName, depth + 1);
      } else if (!hostKind) {
        sourceActions.add(normalized.action);
      }
      output.push(normalized);
    }
    return output;
  };

  // Original source lines 2543–2562
window.addEventListener('message', event => {
    if (event.source !== CinterForgeState.codeFrame.contentWindow || event.origin !== window.location.origin ||
        event.data?.type !== 'cinterforge-codeoss-menu-model') return;
    const groups = event.data.groups;
    if (!Array.isArray(groups)) return;
    const updated = new Map();
    const sourceActions = new Set();
    for (const group of groups) {
      if (!CinterForgeState.codeMenuNames.includes(group?.name) || updated.has(group.name)) continue;
      updated.set(group.name, CinterForgeState.readCodeServiceItems(group.items, sourceActions, group.name));
    }
    if (!updated.size) return;
    CinterForgeState.codeServiceMenus.clear();
    CinterForgeState.codeServiceActions.clear();
    for (const [name, items] of updated) CinterForgeState.codeServiceMenus.set(name, items);
    for (const action of sourceActions) CinterForgeState.codeServiceActions.add(action);
    // The menu model is retained for all other Sessions. Refresh inactive
    // native/HTML menu entries as disabled stubs, without changing titles.
    CinterForgeState.updateCodeMenuMode();
  });

  // Original source lines 2564–2581
for (const name of CinterForgeState.codeMenuNames) {
    const menu = document.createElement('details');
    menu.className = 'terminal-menu terminal-application-menu';
    const summary = document.createElement('summary');
    summary.textContent = name;
    const items = document.createElement('div');
    items.className = 'terminal-menu-list';
    menu.append(summary, items);
    menu.cinterList = items;
    CinterForgeState.codeMenuZone.append(menu);
    CinterForgeState.codeMenuRoots.set(name, menu);
    menu.addEventListener('toggle', () => {
      if (!menu.open) return;
      for (const other of CinterForgeState.codeMenuRoots.values()) if (other !== menu) other.open = false;
      if (CinterForgeState.codeIsActive() && !CinterForgeState.codeServiceMenus.has(name)) CinterForgeState.requestCodeServiceMenus();
      CinterForgeState.refreshCodeMenu(name);
    });
  }

})();
