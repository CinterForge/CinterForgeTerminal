/*
 * CinterForgeClientState.js — CinterForge Terminal browser owner
 * PURPOSE: Initializes the single in-memory UI state, native bridge, API facade and shared constants. Each later component writes only its own named state slots.
 * METHODS: iconCategoryForProfile, appendDecorativeIcon
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 106–128
  CinterForgeState.LAGOMORPH_DELAYS_MS = Object.freeze({
    initialGuestBoot: 10000,
    colorMenuPointerMove: 150,
    colorMenuMouseHeld: 220,
    colorMenuSelection: 260,
    displayRedraw: 1000,
    clickPointerSettle: 140,
    clickPress: 110,
    afterControlStripClose: 250,
    beforeStickyNotesQuit: 200,
    quitModifierDown: 180,
    quitKeyDown: 230,
    betweenQuitAttempts: 550,
    beforeCloseWindowFallback: 250,
    closeModifierDown: 160,
    closeKeyDown: 220,
    beforeDiskOpen: 750,
    doubleClickPointerSettle: 140,
    doubleClickFirstPress: 80,
    doubleClickPressGap: 100,
    doubleClickSecondPress: 80,
    betweenDiskAndAppOpen: 1000,
  });

  // Original source lines 131–137
  CinterForgeState.LAGOMORPH_GUEST_POSITIONS = Object.freeze({
    colorDepthMenu: Object.freeze({ x: 40, y: 469 }),
    sixteenColors: Object.freeze({ x: 80, y: 400 }),
    controlStripToggle: Object.freeze({ x: 8, y: 469 }),
    lagomorphDisk: Object.freeze({ x: 600, y: 160 }),
    lagomorphApplication: Object.freeze({ x: 40, y: 142 }),
  });

  // Original source lines 144–155
  CinterForgeState.TERMINAL_ICON_GLYPHS = Object.freeze({
    terminal: '>_',
    macintosh: '⌘',
    beos: '▥',
    nextstep: '◆',
    windows: '⊞',
    linux: '◆',
    minecraft: '▦',
    codeoss: '</>',
    joshua: '★',
    server: '≡',
  });

  // Original source lines 164–164
  CinterForgeState.token = document.documentElement?.getAttribute('data-cph-token') || window.__CPH_TOKEN__;

  // Original source lines 166–167
  CinterForgeState.acceptUI = new URLSearchParams(location.search || '').has('accept-ui') ||
    Boolean(window.__CINTER_ACCEPT_UI__);

  // Original source lines 174–179
  CinterForgeState.nativeEvent = (event, detail = {}) => {
    if (typeof window.cinterNative !== 'function') return Promise.resolve();
    return Promise.resolve(window.cinterNative({ event, ...detail })).catch(error => {
      console.error('Native Terminal bridge:', error);
    });
  };

  // Original source lines 183–183
  CinterForgeState.$ = id => document.getElementById(id);

  // Original source lines 191–191
  CinterForgeState.api = window.CinterForgeAPI.create(CinterForgeState.token);

  // Original source lines 200–203
  CinterForgeState.VTOutput = window.CinterForgeTerminals.createVTOutput({
    getElement: CinterForgeState.$, getActiveTab: () => CinterForgeState.activeTab,
    paintDiscordDraft: tab => CinterForgeState.paintDiscordDraft(tab)
  });

  // Original source lines 208–211
  CinterForgeState.decode = data => {
    const binary = atob(data), bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
    return new TextDecoder('utf-8').decode(bytes);
  };

  // Original source lines 217–217
  CinterForgeState.session = null;
  CinterForgeState.generation = 0;
  CinterForgeState.platform = 'linux';
  CinterForgeState.selected = '';
  CinterForgeState.profiles = [];
  CinterForgeState.waiting = false;

  // Original source lines 218–218
  CinterForgeState.devtoolsInstalled = false;

  // Original source lines 221–221
  CinterForgeState.guestState = 'not-installed';

  // Original source lines 222–222
  CinterForgeState.guestReady = new Set();

  // Original source lines 223–223
  CinterForgeState.guestVncReady = new Set();

  // Original source lines 224–224
  CinterForgeState.startupGuestRestore = null;

  // Original source lines 225–225
  CinterForgeState.retainingPriorLayout = false;

  // Original source lines 226–226
  CinterForgeState.guestRestoreRunning = false;

  // Original source lines 227–227
  CinterForgeState.guestRestoreComplete = false;

  // Original source lines 228–228
  CinterForgeState.startupSavedActiveApplied = false;

  // Original source lines 229–229
  CinterForgeState.startupUserSelectedTab = false;

  // Original source lines 230–230
  CinterForgeState.restoredGuestTabs = new Map();

  // Original source lines 231–231
  CinterForgeState.queuedSessionOpens = [];

  // Original source lines 233–233
  CinterForgeState.macCatalog = new Map();

  // Original source lines 237–237
  CinterForgeState.museumKind = x => x?.kind || 'terminal';

  // Original source lines 243–254
  CinterForgeState.iconCategoryForProfile = function iconCategoryForProfile(profile, backend = 'native') {
    if (backend === 'server' || backend === 'discord') return 'server';
    if (backend === 'minecraft') return 'minecraft';
    if (backend === 'win') return 'windows';
    if (backend === 'linux') return 'linux';
    if (profile?.kind === 'joshua') return 'joshua';
    if (profile?.kind === 'codeoss' || profile?.id === 'codeoss') return 'codeoss';
    if (profile?.kind !== 'emulator') return 'terminal';
    if (profile.id?.startsWith('beos-')) return 'beos';
    if (profile.id?.startsWith('next-')) return 'nextstep';
    return 'macintosh';
  };

  // Original source lines 260–268
  CinterForgeState.appendDecorativeIcon = function appendDecorativeIcon(target, category) {
    const icon = document.createElement('span');
    icon.className = `terminal-icon terminal-icon--${category}`;
    icon.textContent = CinterForgeState.TERMINAL_ICON_GLYPHS[category] || CinterForgeState.TERMINAL_ICON_GLYPHS.terminal;
    icon.setAttribute('aria-hidden', 'true');
    // Appending after the label retains the existing textContent contract in
    // lightweight UI tests. CSS visually orders the icon before its label.
    target.append(icon);
  };

})();
