/*
 * CinterForgeLayout.js — CinterForge Terminal browser owner
 * PURPOSE: Persists tab layout, builds application menus and restores saved menu selection.
 * METHODS: saveTerminalLayout
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 2587–2587
CinterForgeState.layoutKey = 'cinterterminal.window-layout.v1';

  // Original source lines 2588–2607
CinterForgeState.saveTerminalLayout = function saveTerminalLayout(force = false) {
    // A booting guest must not erase saved Code/VNC tabs. Store the currently
    // open local tabs and retain only the *unrestored* guest tabs from the
    // previous layout. Do not serialize PTY IDs or live emulator state.
    if (CinterForgeState.acceptUI || !CinterForgeState.activeTab || (CinterForgeState.guestRestoreRunning && !force)) return;
    try {
      const restorable = CinterForgeState.tabs.filter(tab => !tab.closed &&
        (tab.profileId === 'codeoss' || tab.kind === 'terminal' ||
         tab.kind === 'server' || tab.kind === 'emulator' || tab.kind === 'launcher'));
      const current = restorable.map(tab =>
        ({profileId: tab.profileId, backend: tab.backend, kind: tab.kind}));
      const pending = CinterForgeState.retainingPriorLayout && Array.isArray(CinterForgeState.startupGuestRestore?.tabs)
        ? CinterForgeState.startupGuestRestore.tabs.filter((tab, index) => !CinterForgeState.restoredGuestTabs.has(index) &&
            (tab.profileId === 'codeoss' || tab.kind === 'launcher' ||
             ['win', 'linux'].includes(tab.backend))) : [];
      localStorage.setItem(CinterForgeState.layoutKey, JSON.stringify({
        active: restorable.indexOf(CinterForgeState.activeTab), tabs: [...current, ...pending]
      }));
    } catch (_) { /* Unavailable storage must never block closing the app. */ }
  };

  // Original source lines 2608–2608
window.addEventListener('pagehide', () => CinterForgeState.saveTerminalLayout(true));

  // Original source lines 2616–2616
CinterForgeState.minecraftMenuState = null;

  // Original source lines 2620–2633
CinterForgeState.updateMinecraftMenu = async () => {
    let enabled = false;
    try {
      const state = await CinterForgeState.api('/api/bedrock/status');
      enabled = !!(state.running || state.starting);
    }
    catch (_) {/* no installed Bedrock manager */ }
    if (enabled !== CinterForgeState.minecraftMenuState) {
      CinterForgeState.minecraftMenuState = enabled;
      const windowsItem = CinterForgeState.$('windows-new-minecraft');
      if (windowsItem) windowsItem.disabled = !enabled;
      void CinterForgeState.nativeEvent('minecraft-menu', { enabled });
    }
  };

  // Original source lines 2634–2634
void CinterForgeState.updateMinecraftMenu();

  // Original source lines 2635–2637
setInterval(() => {
    if (document.visibilityState !== 'hidden') void CinterForgeState.updateMinecraftMenu();
  }, 1600);

  // Original source lines 2639–2639
CinterForgeState.titleMenus = Array.from(document.querySelectorAll('.terminal-menu'));

  // Original source lines 2642–2642
CinterForgeState.setWindowsApplicationMenus = () => {};

  // Original source lines 2643–2647
CinterForgeState.titleMenus.forEach(menu => menu.addEventListener('toggle', () => {
    if (menu.open) CinterForgeState.titleMenus.forEach(other => {
      if (other !== menu) other.open = false;
    });
  }));

  // Original source lines 2648–2654
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && CinterForgeState.titleMenus.some(menu => menu.open)) {
      event.preventDefault();
      CinterForgeState.titleMenus.forEach(menu => menu.open = false);
      CinterForgeState.focusTerminal();
    }
  });

  // Original source lines 2659–2662
CinterForgeState.openMuseumProfile = id => {
    CinterForgeState.titleMenus.forEach(menu => menu.open = false);
    void CinterForgeState.openSession(id);
  };

  // Original source lines 2666–2674
CinterForgeState.addMuseumEntry = (target, profile) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'museum-entry';
    button.textContent = profile.kind === 'emulator'
      ? `${profile.disk} · ${profile.machine || profile.name}`
      : `${profile.year} ${profile.name}`;
    CinterForgeState.appendDecorativeIcon(button, CinterForgeState.iconCategoryForProfile(profile));
    button.addEventListener('click', () => CinterForgeState.openMuseumProfile(profile.id));
    target.append(button);
  };

})();
