/*
 * CinterForgeTerminal.js — CinterForge Terminal browser owner
 * PURPOSE: Thin last-loaded client entry point: performs initial status, restores sessions, and attaches lifecycle DOM listeners.
 * METHODS: Event handlers and per-tab state.
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Terminal uses the same narration and speech settings as the bundled JOSHUA
  // game; the real shell prints the greeting, so its typewriter targets are
  // deliberately off-screen. This is browser audio, never PTY output.
  function playJoshuaWelcome() {
    if (!window.JoshuaBootSequence || !window.JoshuaSpeech || !window.JoshuaPresentation) return;
    void window.JoshuaBootSequence.runBootMessages({
      typePrelude: () => {},
      playBootNoise: () => {},
      createGreetingTarget: () => ({textContent: ''}),
      createPlayPromptTarget: () => ({textContent: ''}),
      greetingText: 'Welcome to CinterForge.',
      playPromptText: 'Shall we play a game?',
      playPromptSpokenText: 'Shall we play a game?',
      lang: 'en',
      speed: window.JoshuaPresentation.DEFAULT_SPEED,
      speech: window.JoshuaSpeech,
      sleep: ms => new Promise(resolve => setTimeout(resolve, ms))
    }).catch(() => { /* A missing native speech voice must not prevent Terminal startup. */ });
  }

  // Original source lines 3141–3275
(async () => {
    try {
      const status = await CinterForgeState.api('/api/status');
      CinterForgeState.platform = status.platform;
      CinterForgeState.updateGuestCapabilities(status);
      document.documentElement?.setAttribute?.('data-cinter-platform', CinterForgeState.platform);
      CinterForgeState.setWindowsApplicationMenus();

      const data = await (await fetch('/data/terminal_styles.json', { cache: 'no-store' })).json();
      CinterForgeState.profiles = [
        ...(data.profiles || []),
        ...(data.emulators || []),
        data.codeoss,
        { id: 'joshua', name: 'JOSHUA', year: 2026, kind: 'joshua' }
      ].filter(Boolean);
      const macData = await (await fetch('/data/mac_games.json', { cache: 'no-store' })).json();
      CinterForgeState.macCatalog = new Map((macData.games || []).map(game => [game.command, game]));
      const byId = new Map(CinterForgeState.profiles.map(profile => [profile.id, profile]));
      const stored = localStorage.getItem('cinterterminal.profile.' + CinterForgeState.platform);
      const initial = stored === 'quadra-650' ? data.defaults[CinterForgeState.platform] : (stored || data.defaults[CinterForgeState.platform]);
      // Normal startup keeps the default Session non-blocking while the
      // Terminals list initializes.  Native acceptance must NOT start that
      // concurrent PTY: openSession has a single-open guard, and an in-flight
      // Apple II connection previously made its VT100 probe a false failure.
      const initialId = byId.has(initial) ? initial : data.defaults[CinterForgeState.platform];
      if (!CinterForgeState.acceptUI) {
        if (CinterForgeState.platform === 'mac') {
          // Mac Bash and classic Macintosh emulators do NOT depend on Colima.
          // Always open the native shell first; the VM and its log run behind it.
          try { CinterForgeState.startupGuestRestore = JSON.parse(localStorage.getItem(CinterForgeState.layoutKey) || 'null'); }
          catch (_) { CinterForgeState.startupGuestRestore = null; }
          const previous = Array.isArray(CinterForgeState.startupGuestRestore?.tabs)
            ? CinterForgeState.startupGuestRestore.tabs : [];
          // The Macintosh game catalog has dynamic emulator profiles. Recover
          // those already-open tabs from the same local catalog; do not fetch
          // or start any new game unless it was in the saved layout.
          for (const tab of previous) {
            const id = typeof tab?.profileId === 'string' ? tab.profileId : '';
            if (!id.startsWith('macgame-') || byId.has(id)) continue;
            const game = CinterForgeState.macCatalog.get(id.slice('macgame-'.length));
            if (!game) continue;
            const profile = {
              id, name: game.title,
              machine: game.needs_mac_os_x ? 'Power Macintosh G4 (PCI Graphics)' : 'Quadra 650',
              disk: game.needs_mac_os_x ? 'Mac OS 9.2' : 'Mac OS 7.6',
              kind: 'emulator', year: 1993, macGame: true
            };
            CinterForgeState.profiles.push(profile);
            byId.set(id, profile);
          }
          // Filtering obsolete tab records must also translate the saved
          // active index; otherwise a different emulator steals the foreground.
          const kept = previous.map((tab, index) => ({tab, index})).filter(({tab}) =>
            tab && (tab.kind === 'server' || tab.kind === 'launcher' &&
              ['mac', 'win', 'linux'].includes(tab.backend) ||
              tab.profileId === 'codeoss' || byId.has(tab.profileId) &&
              ['terminal', 'emulator', 'joshua'].includes(tab.kind)));
          const active = kept.findIndex(item => item.index === CinterForgeState.startupGuestRestore?.active);
          CinterForgeState.startupGuestRestore = {...CinterForgeState.startupGuestRestore, active,
            tabs: kept.map(item => item.tab)};
          CinterForgeState.retainingPriorLayout = CinterForgeState.startupGuestRestore.tabs.length > 0;
          void CinterForgeState.openSession('modern', 'native');
          void CinterForgeState.openServerTab(false); // Live background log, never startup foreground.
        } else {
          void CinterForgeState.openSession(initialId);
        }
      }
      // The JOSHUA welcome is spoken once per Terminal WebView startup,
      // not once per Bash tab, and does not run during native UI acceptance.
      if (!CinterForgeState.acceptUI) playJoshuaWelcome();
      // Historical categories are four independent menus; don't flatten
      // Macintosh, BeOS and NeXTSTEP into the existing Terminals list.
      const menuLists = {
        terminal: CinterForgeState.$('terminal-menu-list'),
        macintosh: CinterForgeState.$('macintosh-menu-list'),
        beos: CinterForgeState.$('beos-menu-list'),
        nextstep: CinterForgeState.$('nextstep-menu-list')
      };
      if (Object.values(menuLists).some(list => !list)) throw new Error('Museum menus missing');
      Object.values(menuLists).forEach(list => list.replaceChildren());
      const serverEntry = document.createElement('button');
      serverEntry.type = 'button';
      serverEntry.className = 'museum-entry';
      serverEntry.textContent = 'Server · Live log';
      CinterForgeState.appendDecorativeIcon(serverEntry, 'server');
      serverEntry.addEventListener('click', () => {
        CinterForgeState.titleMenus.forEach(menu => menu.open = false);
        void window.CinterTerminalNewTab('server');
      });
      menuLists.terminal.append(serverEntry);
      /**
       * Assign a profile to the terminal, Macintosh, BeOS, or NeXTSTEP menu group.
       */
      const groupOf = profile => {
        if (profile.kind !== 'emulator') return 'terminal';
        if (profile.id.startsWith('beos-')) return 'beos';
        if (profile.id.startsWith('next-')) return 'nextstep';
        return 'macintosh';
      };
      const displayed = new Set();
      const available = [
        ...(data.profiles || []),
        ...(data.museum || []).map(item => byId.get(item.id)),
        ...(data.emulators || []),
        data.codeoss
      ].filter(Boolean);
      available.sort((a, b) => (a.year || 0) - (b.year || 0) || a.name.localeCompare(b.name));
      for (const profile of available) {
        if (!profile || displayed.has(profile.id) || profile.lagomorph) continue;
        displayed.add(profile.id);
        CinterForgeState.addMuseumEntry(menuLists[groupOf(profile)], profile);
      }
      await CinterForgeState.nativeEvent('ready', { menu: (data.museum || []).length, profile: initialId });
      // Restore saved local Macintosh machines immediately. Only verified
      // Linux/Windows guest Bash and VNC tabs wait for guest readiness.
      if (!CinterForgeState.acceptUI && CinterForgeState.platform === 'mac' && CinterForgeState.retainingPriorLayout)
        void CinterForgeState.restoreConnectedGuestTabs();
      if (!CinterForgeState.acceptUI) void CinterForgeState.refreshLAN().catch(() => { });
      if (CinterForgeState.acceptUI) {
        // Exercise two real Sessions sequentially, using a public museum Mac
        // rather than the private Lagomorph Quadra.  No background default
        // PTY competes with the native VT100 callback.
        for (const id of ['mac-plus', 'vt100-terminal']) {
          if (!byId.has(id)) throw new Error('Missing museum entry: ' + id);
          await CinterForgeState.openSession(id);
          if (CinterForgeState.activeTab?.profileId !== id)
            throw new Error('Museum Session did not open: ' + id);
          if (id === 'vt100-terminal' && (!CinterForgeState.session?.id || CinterForgeState.activeTab.session !== CinterForgeState.session))
            throw new Error('VT100 PTY did not open');
          if (id === 'mac-plus' && CinterForgeState.computerView.hidden)
            throw new Error('Mac Plus emulator Session did not open');
        }
      }
    } catch (e) {
      CinterForgeState.notice(e.message);
      await CinterForgeState.nativeEvent('error', { message: e.message });
    }
  })();

  // Original source lines 3276–3281
setInterval(
    () => {
      if (document.visibilityState === 'visible' && !CinterForgeState.acceptUI && !CinterForgeState.$('lan-menu')?.open)
        void CinterForgeState.refreshLAN().catch(() => { });
    },
    12000);

  // Original source lines 3282–3289
window.addEventListener(
    'resize',
    () => {
      if (CinterForgeState.session) {
        const size = CinterForgeState.terminalSize();
        CinterForgeState.api('/api/terminal/resize', { id: CinterForgeState.session.id, ...size }).catch(e => CinterForgeState.notice(e.message));
      }
    });

  // Original source lines 3291–3293
window.addEventListener('beforeunload', () => {
    if (CinterForgeState.tabs.length) navigator.sendBeacon?.('/__noop');
  });

})();
