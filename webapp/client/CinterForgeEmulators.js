/*
 * CinterForgeEmulators.js — CinterForge Terminal browser owner
 * PURPOSE: Manages classic Macintosh emulators and Lagomorph guest interactions; preserves exact timed input sequence and emulator origins.
 * METHODS: Event handlers and per-tab state.
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 441–441
  CinterForgeState.lagBootSequence = 0;

  // Original source lines 442–442
  CinterForgeState.lagWelcomeTimer = null;

  // Original source lines 447–447
  CinterForgeState.lagPause = ms => new Promise(resolve => setTimeout(resolve, ms));

  // Original source lines 452–607
  CinterForgeState.lagomorphBootControls = async (stamp, target) => {
    /**
     * Confirm that the current Lagomorph startup still owns the selected iframe and launch
     * generation.
     */
    const valid = () => stamp === CinterForgeState.lagBootSequence && !CinterForgeState.computerView.hidden &&
      CinterForgeState.selected === 'quadra-650' && CinterForgeState.frame.contentWindow === target;
    let down = false, meta = false, q = false;
    /**
     * Post an emulator control message to Infinite Mac’s fixed, trusted origin.
     */
    const send = data => target.postMessage(data, 'https://infinitemac.org');
    /**
     * Position the guest pointer in the emulated 640×480 display.
     */
    const move = (x, y) => send({ type: 'emulator_mouse_move', x, y, deltaX: 0, deltaY: 0 });

    /**
     * Press and remember the guest primary mouse button so cleanup can release it.
     */
    const mouseDown = () => {
      send({ type: 'emulator_mouse_down', button: 0 });
      down = true;
    };
    /**
     * Release the guest primary mouse button.
     */
    const mouseUp = () => {
      send({ type: 'emulator_mouse_up', button: 0 });
      down = false;
    };
    /**
     * Perform one calibrated guest click; abort if the selected emulator changes.
     */
    const click = async (x, y) => {
      move(x, y);
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.clickPointerSettle);
      if (!valid()) return false;
      mouseDown();
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.clickPress);
      mouseUp();
      return valid();
    };
    /**
     * Perform two timed guest clicks to open classic Mac icons.
     */
    const doubleClick = async (x, y) => {
      // Two mouse presses in one classic Mac double-click interval, keeping
      // the pointer stationary on the icon between clicks.
      move(x, y);
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.doubleClickPointerSettle);
      if (!valid()) return false;
      mouseDown();
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.doubleClickFirstPress);
      mouseUp();
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.doubleClickPressGap);
      if (!valid()) return false;
      mouseDown();
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.doubleClickSecondPress);
      mouseUp();
      return valid();
    };
    try {
      if (!valid()) return;
      move(CinterForgeState.LAGOMORPH_GUEST_POSITIONS.colorDepthMenu.x, CinterForgeState.LAGOMORPH_GUEST_POSITIONS.colorDepthMenu.y);
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.colorMenuPointerMove);
      if (!valid()) return;
      mouseDown();
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.colorMenuMouseHeld);
      if (!valid()) return;
      move(CinterForgeState.LAGOMORPH_GUEST_POSITIONS.sixteenColors.x, CinterForgeState.LAGOMORPH_GUEST_POSITIONS.sixteenColors.y);
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.colorMenuSelection);
      mouseUp();
      if (!valid()) return;
      // Selecting a different pixel depth can briefly redraw the guest.
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.displayRedraw);
      if (!valid()) return;
      // Hide the Control Strip exactly once.
      if (!await click(
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.controlStripToggle.x,
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.controlStripToggle.y
      )) return;
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.afterControlStripClose);
      if (!valid()) return;
      // Do NOT click (8,469) again: this is the Control Strip toggle,
      // not Sticky Notes. The first click above closes it exactly once.
      // quitSticky() focuses the guest iframe without another mouse click.
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.beforeStickyNotesQuit);
      if (!valid()) return;

      if (!valid()) return;
      if (!await doubleClick(
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.x,
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.y
      )) return;

      /**
       * Send a physical guest Command-Q chord with balanced key-down and key-up events.
       */
      const quitSticky = async () => {
        // Infinite Mac expects physical ADB MetaLeft + KeyQ, not a browser
        // keyboard shortcut.  Keep both keys held long enough for the guest
        // to see them; release even if the profile changes mid-sequence.
        CinterForgeState.frame.focus();
        send({ type: 'emulator_key_down', code: 'MetaLeft' });
        meta = true;
        await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.quitModifierDown);
        if (!valid()) return false;
        send({ type: 'emulator_key_down', code: 'KeyQ' });
        q = true;
        await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.quitKeyDown);
        send({ type: 'emulator_key_up', code: 'KeyQ' });
        q = false;
        send({ type: 'emulator_key_up', code: 'MetaLeft' });
        meta = false;
        return valid();
      };
      if (!await quitSticky()) return;
      // On a cold guest boot, the first gesture may only activate the
      // emulator. Retry Command-Q without another mouse click: the bottom
      // corner is the Control Strip toggle and a second click reopens it.
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.betweenQuitAttempts);
      if (!valid()) return;
      if (!await quitSticky()) return;
      // Some System 7 Sticky Notes editions keep a note open even after a
      // Command-Q dispatch. Close the active *note window* with guest Command-W
      // as a separate fallback; this is not a host/browser shortcut. It only
      // affects the currently focused guest application, never the app shell.
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.beforeCloseWindowFallback);
      if (!valid()) return;
      CinterForgeState.frame.focus();
      send({ type: 'emulator_key_down', code: 'MetaLeft' });
      meta = true;
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.closeModifierDown);
      if (!valid()) return;
      send({ type: 'emulator_key_down', code: 'KeyW' });
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.closeKeyDown);
      send({ type: 'emulator_key_up', code: 'KeyW' });
      send({ type: 'emulator_key_up', code: 'MetaLeft' });
      meta = false;
      // Finder can now reveal the Lagomorph 1 disk, whether the Sticky Notes
      // process quit or its obstructing note was just closed.
      // at (600,160). Opening it shows the Lagomorph 1 app at (40,142).
      // These are 640x480 guest coordinates, independent of host mouse capture.
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.beforeDiskOpen);
      if (!valid()) return;
      if (!await doubleClick(
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.x,
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.y
      )) return;
      await CinterForgeState.lagPause(CinterForgeState.LAGOMORPH_DELAYS_MS.betweenDiskAndAppOpen);
      if (!valid()) return;
      if (!await doubleClick(
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.lagomorphApplication.x,
        CinterForgeState.LAGOMORPH_GUEST_POSITIONS.lagomorphApplication.y
      )) return;
    } finally {
      // Never leave a button or modifier held if the user switches machines.
      if (down) mouseUp();
      if (q) send({ type: 'emulator_key_up', code: 'KeyQ' });
      if (meta) send({ type: 'emulator_key_up', code: 'MetaLeft' });
    }
  };

  // Original source lines 608–624
  window.addEventListener('message', event => {
    if (event.origin !== 'https://infinitemac.org' || event.source !== CinterForgeState.frame.contentWindow ||
      CinterForgeState.computerView.hidden || event.data?.type !== 'emulator_loaded' ||
      CinterForgeState.selected !== 'quadra-650') return;
    const stamp = CinterForgeState.lagBootSequence;
    if (CinterForgeState.lagWelcomeTimer) clearTimeout(CinterForgeState.lagWelcomeTimer);
    // emulator_loaded means the emulator is running, not that Finder's
    // Control Strip and Sticky Notes window have both finished opening.
    CinterForgeState.lagWelcomeTimer = setTimeout(() => {
      CinterForgeState.lagWelcomeTimer = null;
      if (stamp !== CinterForgeState.lagBootSequence) return;
      const target = CinterForgeState.frame.contentWindow;
      if (target) void CinterForgeState.lagomorphBootControls(stamp, target).catch(e => {
        console.error('Lagomorph guest controls:', e);
      });
    }, CinterForgeState.LAGOMORPH_DELAYS_MS.initialGuestBoot);
  });

  // Original source lines 630–649
  CinterForgeState.showTerminal = () => {
    if (CinterForgeState.terminalOverlay.dataset.codeWrapperHidden === 'true')
      CinterForgeState.setCodeWrapperHidden(false);
    CinterForgeState.lagBootSequence++;
    if (CinterForgeState.lagWelcomeTimer) {
      clearTimeout(CinterForgeState.lagWelcomeTimer);
      CinterForgeState.lagWelcomeTimer = null;
    }
    // Only the recyclable emulator frame is disposed. The Code-OSS iframe
    // retains its workbench, editor state and all legitimate PTYs.
    CinterForgeState.codeFrame.hidden = true;
    CinterForgeState.frame.hidden = false;
    CinterForgeState.frame.src = 'about:blank';
    delete CinterForgeState.frame.dataset.launcher;
    CinterForgeState.codeError.hidden = true;
    CinterForgeState.computerView.hidden = true;
    CinterForgeState.$('terminal-keys').hidden = false;
    CinterForgeState.$('terminal-screen').hidden = false;
    CinterForgeState.$('terminal-input').disabled = false;
  };

  // Original source lines 653–730
  CinterForgeState.showComputer = async (profile) => {
    CinterForgeState.lagBootSequence++;
    if (CinterForgeState.lagWelcomeTimer) {
      clearTimeout(CinterForgeState.lagWelcomeTimer);
      CinterForgeState.lagWelcomeTimer = null;
    }
    // An emulator lives only in the iframe. Host titlebar menus must survive
    // every VM load and remain clickable above the embedded screen.
    document.querySelectorAll('.terminal-menu[open]').forEach(menu => {
      menu.open = false;
    });
    CinterForgeState.$('terminal-screen').hidden = true;
    CinterForgeState.$('terminal-input').disabled = true;
    CinterForgeState.computerView.hidden = false;
    CinterForgeState.$('terminal-keys').hidden = true;
    CinterForgeState.codeError.hidden = true;
    if (CinterForgeState.museumKind(profile) !== 'codeoss') {
      CinterForgeState.codeFrame.hidden = true;
      CinterForgeState.frame.hidden = false;
      delete CinterForgeState.frame.dataset.launcher;
    }
    if (CinterForgeState.museumKind(profile) === 'joshua') {
      // The actual standalone WarGames application, loaded from the staged
      // same-origin source, owns its game screen and keyboard in this Session.
      CinterForgeState.frame.src = '/joshua/index.html';
    } else if (CinterForgeState.museumKind(profile) === 'codeoss') {
      CinterForgeState.frame.hidden = true;
      CinterForgeState.codeFrame.hidden = false;
      if (!CinterForgeState.codeFrame.dataset.loaded) {
        // Initialize exactly once until the user explicitly closes Code-OSS.
        if (CinterForgeState.terminalOverlay.dataset.codeWrapperHidden === 'true')
          CinterForgeState.setCodeWrapperHidden(false);
        const stamp = ++CinterForgeState.codeLoad;
        CinterForgeState.codeReady = false;
        CinterForgeState.codeLastError = '';
        CinterForgeState.codeFrame.dataset.loaded = 'true';
        CinterForgeState.codeFrame.src = CinterForgeState.codeUrl;
        void CinterForgeState.checkCodeAssets(stamp);
        setTimeout(() => {
          if (stamp === CinterForgeState.codeLoad && !CinterForgeState.codeReady)
            CinterForgeState.codeFailure(CinterForgeState.codeLastError || 'No ready signal from staged Code-OSS after 12 seconds');
        }, 12500);
      }
    } else {
      const url = new URL('https://infinitemac.org/embed');
      url.searchParams.set('machine', profile.machine);
      url.searchParams.set('disk', profile.disk);
      if (profile.macGame) {
        url.searchParams.set('infinite_hd', 'true');
      }
      if (profile.lagomorph) {
        url.searchParams.set('screenSize', '640x480');
        // The distributed Lagomorph HFS/CD image is immutable. Give the
        // Macintosh a separate writable Saved HD for persistent game saves.
        // This is the existing Infinite Mac Saved HD feature, not a second UI.
        url.searchParams.set('saved_hd', 'true');
      }
      // Infinite Mac refuses HTTP localhost CD-ROMs, even if our HTTP server
      // allows CORS and byte ranges. Never send it an unsupported URL.
      // A separately hosted HTTPS image with range/CORS support is required.
      if (profile.id === 'quadra-650' && profile.lagomorph) {
        try {
          const media = await CinterForgeState.api('/api/lagomorph/media');
          // A read-only CD lacks a writable Desktop database and triggers Finder
          // repair errors. Infinite Mac's disk_url enables a writable overlay.
          if (media.configured && media.url) url.searchParams.set('disk_url', media.url);
          if (Array.isArray(media.floppies) && media.floppies.length === 2) {
            url.searchParams.delete('disk_url');
            media.floppies.forEach(floppy => url.searchParams.append('disk_url', floppy));
          }
          if (!media.configured || !media.url) CinterForgeState.notice(media.reason || 'Lagomorph media requires a public HTTPS URL.');
        } catch (e) {
          CinterForgeState.notice('Lagomorph CD-ROM unavailable: ' + e.message);
        }
      }
      CinterForgeState.frame.src = url.toString();
    }
  };

})();
