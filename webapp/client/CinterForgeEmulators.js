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

  /*
   * Every Infinite Mac tab owns one iframe for its entire lifetime. Hiding an
   * iframe preserves the Macintosh, its open files and its mounted disks;
   * changing src, removing the node or moving it through another document
   * reboots the guest.
   */
  CinterForgeState.emulatorFrames = new Map();
  CinterForgeState.activeEmulatorFrame = null;

  // Mount and auto-open only the actual three CinterForge 68K game guests.
  // emulator_loaded means the ROM started, not that Finder is ready.
  const cinterForge68KBootDelay = Object.freeze({
    'System 7.1': 12000,
    'Mac OS 7.6': 16000,
    'Mac OS 8.1': 20000
  });
  const cinterForgeOSAddress = Object.freeze({
    'System 7.1': '7.1', 'Mac OS 7.6': '7.6', 'Mac OS 8.1': '8.1'
  });
  // Guest framebuffer coordinates, never host-window positions. On the
  // Quadra 650 the Infinite HD Finder window *covers* CinterForge HD. Its
  // close box is (33, 62); simply clicking the exposed CinterForge title
  // bar while Infinite HD is in front does not reliably expose the app icon.
  // The Mac II and Power Macintosh 7500 already expose their app icons.
  const cinterForge68KFinder = Object.freeze({
    'System 7.1': Object.freeze({app: {x: 40, y: 98}}),
    'Mac OS 7.6': Object.freeze({closeInfiniteHD: {x: 33, y: 62},
      window: {x: 220, y: 36}, app: {x: 40, y: 98}}),
    'Mac OS 8.1': Object.freeze({window: {x: 205, y: 35}, app: {x: 42, y: 102}})
  });
  const startupQueue = [];
  let clipboardOwner = null;
  let clipboardPoll = null;
  let clipboardReading = false;
  // Startup isolation: without a proven automatic guest-to-host READY bridge,
  // a missing acknowledgment is not permission to double-click the app again.
  // A second Finder open can disrupt a process that has already begun startup.
  const maxFinderAttempts = 1;

  // DingusPPC (Power Macintosh 7500) accepts relative movement, unlike the
  // absolute-coordinate Mini vMac and Basilisk II emulators. All guests still
  // need the parent pointer relay in the embedded Terminal WebView.
  const relativeMouseMachines = new Set(['Power Macintosh 7500']);

  const nativeClipboard = () => CinterForgeState.platform === 'mac' &&
    typeof window.cinterClipboard === 'function';
  const readClipboard = () => window.cinterClipboard({action: 'paste'});
  const writeClipboard = text => window.cinterClipboard({action: 'copy', text});
  const log68KEvent = (os, kind) => {
    // The Python service writes the actual server log. Never treat a queued
    // browser callback or a native copy result as proof of guest readiness.
    void CinterForgeState.api('/api/infinitemac/clipboard-event', {os, kind})
      .catch(error => console.error('CinterForge 68K event log:', error));
  };
  const speak68KReadyGreeting = async () => {
    // The 68K app never invokes MacinTalk. Reuse the host's native Zarvox
    // greeting, triggered only after the guest echoes this boot's HELLO token.
    if (CinterForgeState.platform !== 'mac') return;
    for (const part of ['welcome', 'question']) {
      try {
        const result = await CinterForgeState.api('/api/startup/speech', {part});
        if (!result?.spoken) {
          console.warn('68K READY greeting unavailable:', result?.error);
          return;
        }
      } catch (error) {
        console.warn('68K READY greeting failed:', error);
        return;
      }
    }
  };
  const liveStartup = frame => {
    const startup = frame?.cinterForge68KStartup;
    return startup && !startup.tab.closed &&
      CinterForgeState.emulatorFrames.get(startup.tab.key) === frame;
  };

  const startNextHandshake = async () => {
    if (clipboardOwner || !nativeClipboard()) return;
    while (startupQueue.length) {
      const frame = startupQueue.shift();
      if (!liveStartup(frame) || frame.cinterForge68KStartup.ready) continue;
      const startup = frame.cinterForge68KStartup;
      clipboardOwner = frame; // Only one HELLO owns the shared clipboard.
      startup.helloStarted = Date.now();
      try {
        const sent = await writeClipboard(`cinter:${startup.address}:hello ${startup.nonce}`);
        if (sent !== true) throw new Error('Native clipboard rejected HELLO');
        if (!clipboardPoll) clipboardPoll = setInterval(pollClipboard, 1000);
      } catch (error) {
        console.error('CinterForge 68K HELLO:', error);
        clipboardOwner = null;
        continue;
      }
      return;
    }

    // The host keeps polling while any 68K guest is open, including after
    // READY, so unsolicited guest messages remain observable.
  };
  const releaseHandshake = frame => {
    if (clipboardOwner === frame) {
      clipboardOwner = null;
      void startNextHandshake();
    } else {
      const index = startupQueue.indexOf(frame);
      if (index >= 0) startupQueue.splice(index, 1);
    }
  };
  const pollClipboard = async () => {
    if (clipboardReading || !nativeClipboard()) return;
    clipboardReading = true;
    try {
      const value = await readClipboard();
      if (typeof value !== 'string' || value.length > 192) return;
      const frame = clipboardOwner;
      if (liveStartup(frame)) {
        const startup = frame.cinterForge68KStartup;
        if (!startup.ready && value ===
            `cinter:${startup.address}:ready ${startup.nonce}`) {
          startup.ready = true;
          if (startup.timer) clearTimeout(startup.timer);
          startup.timer = null;
          console.info(`CINTERFORGE_READY ${startup.address}: clipboard round trip`);
          log68KEvent(startup.address, 'ready');
          releaseHandshake(frame);
          void speak68KReadyGreeting();
        } else if (Date.now() - startup.helloStarted > 110000) {
          // An unproven clipboard bridge must not retry clicks indefinitely.
          console.error(`CinterForge 68K ${startup.address}: no clipboard READY`);
          releaseHandshake(frame);
        }
      }
      for (const candidate of CinterForgeState.emulatorFrames.values()) {
        const startup = candidate.cinterForge68KStartup;
        if (startup?.pendingCommand &&
            value === `cinter:${startup.address}:ack ${startup.pendingCommand}`) {
          startup.pendingCommand = null;
          log68KEvent(startup.address, 'ack');
        }
      }
    } catch (error) {
      console.error('CinterForge 68K clipboard poll:', error);
    } finally {
      clipboardReading = false;
    }
  };

  // Prepared before setting the iframe src, so HELLO precedes guest boot.
  CinterForgeState.arm68KClipboard = async frame => {
    if (!liveStartup(frame)) return;
    startupQueue.push(frame);
    await startNextHandshake();
  };

  // Future host Game Cards may call this same pipe. A machine card never
  // invokes PLAY: it only starts the OS with the Terminal companion disk.
  CinterForgeState.send68KCommand = async (os, command) => {
    if (!['7.1', '7.6', '8.1'].includes(os) ||
        !/^(play [a-zA-Z0-9 .!'-]{1,63}|games|games report|games rescan|help)$/.test(command))
      throw new Error('Invalid OS-targeted CinterForge command');
    if (!nativeClipboard() || clipboardOwner)
      throw new Error('CinterForge clipboard pipe is unavailable or handshaking');
    const matches = [...CinterForgeState.emulatorFrames.values()].filter(frame =>
      liveStartup(frame) && frame.cinterForge68KStartup.address === os &&
      frame.cinterForge68KStartup.ready);
    if (matches.length !== 1) throw new Error('Exactly one ready CinterForge guest is required');
    const startup = matches[0].cinterForge68KStartup;
    if (startup.pendingCommand) throw new Error('Previous CinterForge command not acknowledged');
    startup.pendingCommand = command;
    try {
      if (await writeClipboard(`cinter:${os}:${command}`) !== true)
        throw new Error('Native clipboard rejected command');
      if (!clipboardPoll) clipboardPoll = setInterval(pollClipboard, 1000);
    } catch (error) {
      startup.pendingCommand = null;
      throw error;
    }
  };

  // Retry a physical Finder open while the guest is the selected tab. Do not
  // flag success just because mouse events were sent: READY is the evidence.
  CinterForgeState.schedule68KAppOpen = frame => {
    const startup = frame?.cinterForge68KStartup;
    if (!startup?.loaded || startup.ready || startup.timer ||
        startup.attempts >= maxFinderAttempts || !liveStartup(frame)) return;
    // Until DingusPPC pointer alignment is proven in a live guest, do not
    // inject blind Finder clicks that can hold up the guest mouse. Manual
    // mouse control must work independently of startup automation.
    if (frame.relativeMouse) return;
    const finder = cinterForge68KFinder[startup.os];
    if (!finder) return;
    const active = () => liveStartup(frame) &&
      CinterForgeState.activeTab === startup.tab &&
      CinterForgeState.activeEmulatorFrame === frame &&
      !CinterForgeState.computerView.hidden && !frame.hidden;
    if (!active()) return; // Resume on tab activation; never click another tab.
    // Attempt Finder launch only once per guest boot. The clipboard may not
    // synchronize automatically, so no READY cannot trigger another launch.
    const deadline = startup.readyAt;
    startup.timer = setTimeout(async () => {
      startup.timer = null;
      if (!active() || startup.ready) return;
      const target = frame.contentWindow;
      if (!target) return;
      const send = data => target.postMessage(data, 'https://infinitemac.org');
      const delays = CinterForgeState.LAGOMORPH_DELAYS_MS;
      const pause = CinterForgeState.lagPause;
      let pressed = false;
      const move = position =>
        send({type: 'emulator_mouse_move', ...position, deltaX: 0, deltaY: 0});
      const down = () => { send({type: 'emulator_mouse_down', button: 0}); pressed = true; };
      const up = () => {
        if (!pressed) return;
        send({type: 'emulator_mouse_up', button: 0});
        pressed = false;
      };
      const click = async position => {
        if (!active() || startup.ready) return false;
        move(position);
        await pause(delays.clickPointerSettle);
        if (!active() || startup.ready) return false;
        down();
        await pause(delays.clickPress);
        up();
        return active();
      };
      const doubleClick = async position => {
        if (!active() || startup.ready) return false;
        move(position);
        await pause(delays.doubleClickPointerSettle);
        if (!active() || startup.ready) return false;
        down();
        await pause(delays.doubleClickFirstPress);
        up();
        await pause(delays.doubleClickPressGap);
        if (!active() || startup.ready) return false;
        down();
        await pause(delays.doubleClickSecondPress);
        up();
        return active();
      };
      startup.attempts++;
      // Ignore simultaneous host pointer moves during the scripted click.
      // A moving real cursor must not steal the guest pointer between presses.
      frame.cinterForgeAutomationClicking = true;
      try {
        frame.focus();
        if (finder.closeInfiniteHD) {
          // Close the foreground Infinite HD Finder window (not its desktop
          // disk icon) before targeting the CinterForge Terminal application.
          if (!await click(finder.closeInfiniteHD)) return;
          await pause(400);
          if (!active() || startup.ready) return;
        }
        if (finder.window) {
          if (!await click(finder.window)) return;
          await pause(400);
          if (!active() || startup.ready) return;
        }
        await doubleClick(finder.app);
      } catch (error) {
        console.error('CinterForge 68K Finder launch:', error);
      } finally {
        up();
        frame.cinterForgeAutomationClicking = false;
        frame.resetPointerRelay?.();
        if (active() && !startup.ready) {
          // Absence of READY does not mean failure: the guest may already
          // be running, and unattended scrap synchronization is unverified.
          console.info(`CinterForgeTerminal ${startup.address}: Finder open attempted; guest readiness unverified`);
        }
      }
    }, Math.max(0, deadline - Date.now()));
  };

  window.addEventListener('message', event => {
    if (event.origin !== 'https://infinitemac.org' ||
        event.data?.type !== 'emulator_loaded') return;
    for (const frame of CinterForgeState.emulatorFrames.values()) {
      if (event.source !== frame.contentWindow) continue;
      const startup = frame.cinterForge68KStartup;
      if (startup && !startup.loaded) {
        startup.loaded = true;
        startup.readyAt = Date.now() + cinterForge68KBootDelay[startup.os];
        CinterForgeState.schedule68KAppOpen(frame);
      }
      break;
    }
  });

  // The public gallery and machine menus show one representative machine per
  // operating-system version. Keep all original profiles in the registry:
  // direct game commands, restored tabs and Lagomorph still need their IDs.
  const preferredMuseumMachines = new Map([
    ['System 7.1', 'mac-ii'],
    ['Mac OS 7.6', 'mac-quadra-650'], // public, not Lagomorph's private Quadra
    ['Mac OS 8.1', 'power-macintosh-7500'],
    ['Mac OS 9.0.4', 'power-macintosh-g3-blue-white']
  ]);
  CinterForgeState.uniqueMuseumEmulators = profiles => {
    const systems = new Map();
    const rank = profile =>
      (profile.id === preferredMuseumMachines.get(profile.disk) ? 0 : 1) +
      (/\b(?:Snow|Experimental)\b/i.test(profile.name) ? 10 : 0);
    for (const profile of profiles) {
      if (profile?.kind !== 'emulator' || profile.lagomorph || profile.macGame ||
          typeof profile.id !== 'string' || !profile.disk) continue;
      const key = (profile.os || 'macos') + ':' + profile.disk;
      const previous = systems.get(key);
      if (!previous || rank(profile) < rank(previous)) systems.set(key, profile);
    }
    return [...systems.values()];
  };

  CinterForgeState.hideEmulatorFrames = () => {
    for (const frame of CinterForgeState.emulatorFrames.values()) {
      // Release a held mouse button while the guest is still addressable.
      if (!frame.hidden) frame.resetPointerRelay?.();
      frame.hidden = true;
      if (frame.pointerRelay) frame.pointerRelay.hidden = true;
    }
    CinterForgeState.activeEmulatorFrame = null;
  };

  CinterForgeState.removeEmulatorFrame = tab => {
    const frame = CinterForgeState.emulatorFrames.get(tab.key);
    if (!frame) return;
    // Releasing a tab is the only time we intentionally destroy its guest.
    if (frame.cinterForge68KStartup?.timer)
      clearTimeout(frame.cinterForge68KStartup.timer);
    releaseHandshake(frame);
    if (clipboardPoll && ![...CinterForgeState.emulatorFrames.values()].some(candidate =>
        candidate !== frame && candidate.cinterForge68KStartup)) {
      clearInterval(clipboardPoll);
      clipboardPoll = null;
    }
    frame.pointerRelay?.remove();
    frame.remove();
    CinterForgeState.emulatorFrames.delete(tab.key);
    if (CinterForgeState.activeEmulatorFrame === frame)
      CinterForgeState.activeEmulatorFrame = null;
  };

  CinterForgeState.getActiveInfiniteMacFrame = () =>
    CinterForgeState.activeEmulatorFrame || CinterForgeState.frame;

  /**
   * Infinite Mac's documented postMessage protocol accepts absolute mouse
   * positions on Mini vMac/Basilisk/SheepShaver and relative deltas on other
   * emulators. Its screen_update_messages notifications expose the *actual*
   * guest framebuffer width/height, which can differ from the parent iframe's
   * CSS box (notably when the display is letterboxed).
   */
  CinterForgeState.installInfiniteMacPointerRelay = frame => {
    // The earlier pointer-relay overlay intercepted the entire iframe and
    // regressed the 7.1 and 8.1 guest mice. Keep the *user-confirmed working*
    // recovery input path: Infinite Mac receives native iframe pointer events.
    // Guest launch automation still uses its separate postMessage control path.
    // Do not reintroduce a pointer-capturing overlay without live testing.
    if (frame) frame.resetPointerRelay = () => {};
  };

  // Screen dimension messages are restricted to the exact origin and iframe.
  // Notifications do not expose application data; only dimensions are retained.
  window.addEventListener('message', event => {
    if (event.origin !== 'https://infinitemac.org' || event.data?.type !== 'emulator_screen') return;
    for (const frame of CinterForgeState.emulatorFrames.values()) {
      if (event.source !== frame.contentWindow) continue;
      const width = event.data.width;
      const height = event.data.height;
      if (Number.isInteger(width) && Number.isInteger(height) &&
          width > 0 && height > 0 && width <= 8192 && height <= 8192 &&
          (frame.guestScreen?.width !== width || frame.guestScreen?.height !== height)) {
        frame.guestScreen = {width, height};
        frame.resetPointerRelay?.();
      }
      break;
    }
  });

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
      CinterForgeState.selected === 'quadra-650' && CinterForgeState.getActiveInfiniteMacFrame().contentWindow === target;
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
        CinterForgeState.getActiveInfiniteMacFrame().focus();
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
      CinterForgeState.getActiveInfiniteMacFrame().focus();
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
    if (event.origin !== 'https://infinitemac.org' || event.source !== CinterForgeState.getActiveInfiniteMacFrame().contentWindow ||
      CinterForgeState.computerView.hidden || event.data?.type !== 'emulator_loaded' ||
      CinterForgeState.selected !== 'quadra-650') return;
    const stamp = CinterForgeState.lagBootSequence;
    if (CinterForgeState.lagWelcomeTimer) clearTimeout(CinterForgeState.lagWelcomeTimer);
    // emulator_loaded means the emulator is running, not that Finder's
    // Control Strip and Sticky Notes window have both finished opening.
    CinterForgeState.lagWelcomeTimer = setTimeout(() => {
      CinterForgeState.lagWelcomeTimer = null;
      if (stamp !== CinterForgeState.lagBootSequence) return;
      const target = CinterForgeState.getActiveInfiniteMacFrame().contentWindow;
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
    CinterForgeState.hideEmulatorFrames();
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
  CinterForgeState.showComputer = async (profile, tab) => {
    CinterForgeState.hideEmulatorFrames();
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
      CinterForgeState.frame.hidden = CinterForgeState.museumKind(profile) !== 'joshua';
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
      const existing = tab && CinterForgeState.emulatorFrames.get(tab.key);
      if (existing) {
        CinterForgeState.activeEmulatorFrame = existing;
        existing.hidden = false;
        if (existing.pointerRelay) existing.pointerRelay.hidden = false;
        existing.resetPointerRelay?.();
        CinterForgeState.schedule68KAppOpen(existing);
        return;
      }
      const url = new URL('https://infinitemac.org/embed');
      url.searchParams.set('machine', profile.machine);
      url.searchParams.set('disk', profile.disk);
      // Every System 7+ classic Mac requests its matching Infinite HD, and
      // the existing direct-game path retains its original Infinite HD option.
      // Keep Lagomorph's fixed-media profile untouched: adding Infinite HD
      // would rearrange the Finder icons its calibrated launch clicks use.
      const classicMac = /^(?:System 7\.|Mac OS (?:7\.|8\.|9\.))/.test(profile.disk || '');
      if (profile.macGame || (classicMac && !profile.lagomorph))
        url.searchParams.set('infinite_hd', 'true');
      // The CinterForge Terminal disk is only for the three curated game OS versions.
      // Leave every other System 7/8/9 guest's Infinite HD behavior alone,
      // and never interfere with Lagomorph's separate calibrated media.
      // The dynamic macGame profile for a Quadra 650 is not a Lagomorph
      // session, even though the museum's Quadra 650 profile is reserved.
      const cinterForgeGameOS = Object.hasOwn(cinterForge68KBootDelay, profile.disk);
      if (cinterForgeGameOS && !profile.lagomorph) {
        try {
          const media = await CinterForgeState.api('/api/infinitemac/media');
          if (media.configured && media.url) {
            const image = new URL(media.url);
            if (image.protocol === 'https:' && image.hostname === 'cinterforge.ucreg.io' &&
                !image.username && !image.password)
              url.searchParams.append('disk_url', image.toString());
          } else {
            CinterForgeState.notice(media.reason ||
              'CinterForge 68K application disk is not published yet. Run ./Cinter all 68k publish beta.');
          }
        } catch (e) {
          CinterForgeState.notice('CinterForge Terminal disk unavailable: ' + e.message);
        }
      }
      if (profile.macGame) {
        // Import only the exact authored URL selected by the live server.
        // A library catalog filename is never converted into a guessed URL.
        if (profile.gameAttemptUrl) {
          const media = new URL(profile.gameAttemptUrl);
          if (media.protocol === 'https:' && !media.username && !media.password)
            url.searchParams.set('library_url', media.toString());
        }
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
      if (!tab) throw new Error('Infinite Mac launch requires its owning session tab');
      // Do not steal focus when an earlier asynchronous media lookup returns
      // after the user has already selected a different tab.
      if (tab.closed || CinterForgeState.activeTab !== tab) return;
      let iframe = CinterForgeState.emulatorFrames.get(tab.key);
      if (!iframe) {
        iframe = document.createElement('iframe');
        iframe.className = 'infinitemac-session-frame';
        iframe.title = profile.name + ' emulator';
        iframe.referrerPolicy = 'no-referrer';
        iframe.relativeMouse = relativeMouseMachines.has(profile.machine);
        iframe.setAttribute('allow', 'cross-origin-isolated');
        iframe.hidden = true;
        CinterForgeState.computerView.append(iframe);
        CinterForgeState.emulatorFrames.set(tab.key, iframe);
        // Only attempt guest app startup after its HFS disk URL was attached.
        // No automation is scheduled for missing media or Lagomorph.
        if (cinterForgeGameOS && !profile.lagomorph &&
            url.searchParams.getAll('disk_url').length === 1)
          iframe.cinterForge68KStartup = {
            os: profile.disk, address: cinterForgeOSAddress[profile.disk],
            nonce: [...crypto.getRandomValues(new Uint8Array(8))]
              .map(byte => byte.toString(16).padStart(2, '0')).join(''),
            tab, loaded: false, ready: false, attempts: 0,
            timer: null, readyAt: 0, helloStarted: 0, pendingCommand: null
          };
        CinterForgeState.installInfiniteMacPointerRelay(iframe);
        // Dimensions are sent at most once every 10 seconds.  The parent
        // discards the pixel buffer and uses only width and height.
        url.searchParams.set('screen_update_messages', 'true');
        url.searchParams.set('screen_update_interval', '10000');
        if (iframe.cinterForge68KStartup)
          await CinterForgeState.arm68KClipboard(iframe);
        // Boot its original tab even if the user switched while the native
        // clipboard handshake was being prepared; do not reactivate it.
        if (!tab.closed && CinterForgeState.emulatorFrames.get(tab.key) === iframe)
          iframe.src = url.toString();
      }
      if (tab.closed || CinterForgeState.activeTab !== tab) return;
      CinterForgeState.activeEmulatorFrame = iframe;
      iframe.hidden = false;
      if (iframe.pointerRelay) iframe.pointerRelay.hidden = false;
      iframe.resetPointerRelay?.();
    }
  };

})();
