/*
 * CinterForgeEmulators.js — CinterForge Terminal browser owner
 * PURPOSE: Manages classic Macintosh emulators and Lagomorph guest interactions; gates automated startup on Finder menu-bar OCR.
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

  // Sample the actual emulator menu bar once per second. Boot readiness
  // is recognized by Tesseract, not by timeouts or guessed pixel patterns.
  const cinterForge68KScreenCheckMs = 1000;
  const cinterForgeOSAddress = Object.freeze({
    'System 7.1': '7.1', 'Mac OS 7.6': '7.6'
  });
  // Finder keeps desktop disk icons about 40 pixels from the right edge.
  // Open the *CinterForge Terminal disk icon* (third mounted disk) explicitly
  // on every retry. Merely clicking inside a supposed open Finder window
  // can instead select Infinite HD, as the 7.6 guest demonstrated. The
  // application icon is first in the CinterForge Terminal root window.
  // All positions are guest framebuffer coordinates, not host viewport pixels.
  const cinterForge68KFinder = Object.freeze({
    'System 7.1': Object.freeze({diskRightInset: 40, diskY: 154,
      app: {x: 40, y: 98}}),
    'Mac OS 7.6': Object.freeze({diskRightInset: 40, diskY: 154,
      app: {x: 40, y: 98}})
  });
  // A retry is allowed only while Finder is recognized by OCR. No
  // automatic clipboard challenge or clipboard success polling.
  const finderRetryDelayMs = 2800;

  // DingusPPC (Power Macintosh 7500) accepts relative movement, unlike the
  // absolute-coordinate Mini vMac and Basilisk II emulators. All guests still
  // need the parent pointer relay in the embedded Terminal WebView.
  const relativeMouseMachines = new Set(['Power Macintosh 7500']);

  // Game launches reuse an OS-specific session. Record the request before
  // opening the emulator, so the guest Terminal's startup voice is skipped.
  const pending68KGames = new Map();
  let next68KGameRequest = 0;
  const max68KGameAttempts = 12;
  CinterForgeState.queue68KGame = game => {
    // All characters are representable by Infinite Mac's physical keyboard API.
    // A fresh token per request lets the guest ignore repeated complete packets.
    const token = Date.now().toString(36).padStart(8, '0').slice(-8) + (++next68KGameRequest).toString(36);
    pending68KGames.set(game.os, {...game, token, attempts: 0,
      nextSendAt: 0, sending: false, checking: false,
      sawTerminal: false, awayChecks: 0});
  };
  CinterForgeState.cancel68KGame = os => pending68KGames.delete(os);
  const speak68KReadyGreeting = async frame => {
    // The 68K app never invokes MacinTalk. Reuse the host's native Zarvox
    // greeting, triggered once after the Terminal becomes visually frontmost.
    if (CinterForgeState.platform !== 'mac') return;
    for (const part of ['welcome', 'question']) {
      if (pending68KGames.has(frame?.cinterForge68KStartup?.os)) return;
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
  // The Terminal bundles the supplied Tesseract.js 5.1.1, worker, embedded
  // WASM core, and English language data under /ocr/. OCR is the ONLY startup
  // detector. Do not fetch code or models from the network or fall back to
  // fixed delays, pixel matching, or clipboard launch signals.
  let ocrWorkerPromise = null;
  let ocrStartupFailure = null;
  const getOCRWorker = () => {
    if (ocrStartupFailure) return Promise.reject(ocrStartupFailure);
    if (ocrWorkerPromise) return ocrWorkerPromise;
    ocrWorkerPromise = (async () => {
      if (typeof window.Tesseract?.createWorker !== 'function')
        throw new Error('Bundled /ocr/tesseract.min.js unavailable (OCR required)');
      const root = new URL('/ocr/', window.location.href);
      const worker = await window.Tesseract.createWorker('eng', 1, {
        workerPath: new URL('worker.min.js', root).href,
        workerBlobURL: false, // Load the bundled same-origin worker directly; CSP disallows Blob workers.
        corePath: new URL('tesseract-core.wasm.js', root).href,
        langPath: root.href.replace(/\/$/, ''),
        cacheMethod: 'none',
        gzip: true
      });
      await worker.setParameters({
        tessedit_pageseg_mode: '6', // Classic menu bar is one text block.
        tessedit_char_whitelist: 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz '
      });
      return worker;
    })().catch(error => {
      // Some worker failures reject with undefined rather than an Error.
      // Preserve a useful failure message and fail closed: no click fallback.
      const detail = error instanceof Error ? error.message :
        (typeof error === 'string' && error ? error :
         'worker failed without a diagnostic (check local OCR assets and CSP)');
      ocrStartupFailure = new Error(`Local Tesseract initialization failed: ${detail}`);
      throw ocrStartupFailure;
    });
    return ocrWorkerPromise;
  };
  const menuBarImage = ({width, height, data}) => {
    // Only read the guest's native 22-pixel menu bar. Enlarge without
    // smoothing so Chicago/bitmap glyphs remain sharp for Tesseract.
    const left = 20, cropWidth = Math.min(width - left, 430), cropHeight = Math.min(height, 22);
    const small = document.createElement('canvas');
    small.width = cropWidth;
    small.height = cropHeight;
    const smallCtx = small.getContext('2d');
    const pixels = smallCtx.createImageData(cropWidth, cropHeight);
    for (let y = 0; y < cropHeight; ++y) {
      const offset = (y * width + left) * 4;
      pixels.data.set(data.subarray(offset, offset + cropWidth * 4), y * cropWidth * 4);
    }
    smallCtx.putImageData(pixels, 0, 0);
    const large = document.createElement('canvas');
    large.width = cropWidth * 4;
    large.height = cropHeight * 4;
    const ctx = large.getContext('2d');
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(small, 0, 0, large.width, large.height);
    return large;
  };
  const classifyMenuText = text => {
    const words = String(text || '').toLowerCase().replace(/[^a-z ]/g, ' ').split(/\s+/).filter(Boolean);
    const has = name => words.includes(name);
    if (has('file') && has('edit') && has('games')) return 'TERMINAL_RUNNING';
    if (has('file') && has('edit') && has('label') && has('special')) return 'FINDER_VISIBLE';
    return words.length ? 'OTHER_APP_OR_MENU' : 'BOOT_OR_BLANK';
  };
  const liveStartup = frame => {
    const startup = frame?.cinterForge68KStartup;
    return startup && !startup.tab.closed &&
      CinterForgeState.emulatorFrames.get(startup.tab.key) === frame;
  };
  const visibleFrame = frame => {
    const tab = frame?.cinterForge68KStartup?.tab || frame?.lagomorphWatch?.tab;
    return tab && !tab.closed && CinterForgeState.activeTab === tab &&
      CinterForgeState.emulatorFrames.get(tab.key) === frame &&
      CinterForgeState.activeEmulatorFrame === frame &&
      !CinterForgeState.computerView.hidden && !frame.hidden;
  };
  // A second, non-interactive OCR watch runs only after the ordinary 68K
  // Terminal has been recognized. It never sends mouse/keyboard events to the
  // guest, opens new windows or changes the emulator startup classifier.
  const stopLauncherWatch = frame => {
    if (!frame?.gamesLauncherWatch) return;
    clearInterval(frame.gamesLauncherWatch.timer);
    frame.gamesLauncherWatch = null;
  };
  const launcherDimensions = screen => {
    // These are the same NewCWindow bounds and cell equations used by the
    // native 68K Games Launcher. The fixed grid contains 50/32 real slots,
    // with the remaining 8x7/6x6 cells intentionally left blank.
    const is71 = screen.os === 'System 7.1';
    const columns = is71 ? 8 : 6, rows = is71 ? 7 : 6;
    const width = Math.min(1000, Math.max(400, screen.width - 22));
    const height = Math.min(730, Math.max(350, screen.height - 72));
    return {left: 11, top: 43, width, height, columns, rows,
      expected: is71 ? 50 : 32, topGap: is71 ? 8 : 0};
  };
  const launcherTitleImage = screen => {
    // Recognize the real window title, not a guessed timer after launch.
    // The 68K window's title bar is above its content port (top=43).
    const height = Math.min(44, screen.height - 12);
    const sample = document.createElement('canvas');
    sample.width = screen.width;
    sample.height = height;
    const context = sample.getContext('2d');
    const rgba = context.createImageData(screen.width, height);
    const start = 12 * screen.width * 4;
    rgba.data.set(screen.data.subarray(start, start + rgba.data.length));
    context.putImageData(rgba, 0, 0);
    const enlarged = document.createElement('canvas');
    enlarged.width = sample.width * 3;
    enlarged.height = sample.height * 3;
    const enlargedContext = enlarged.getContext('2d');
    enlargedContext.imageSmoothingEnabled = false;
    enlargedContext.drawImage(sample, 0, 0, enlarged.width, enlarged.height);
    return enlarged;
  };
  const launcherIconPixels = (screen, layout, slot) => {
    const column = slot % layout.columns;
    const row = Math.floor(slot / layout.columns);
    const left = Math.floor(column * layout.width / layout.columns);
    const right = Math.floor((column + 1) * layout.width / layout.columns);
    const top = layout.topGap + Math.floor(row * (layout.height - layout.topGap) / layout.rows);
    const bottom = layout.topGap + Math.floor((row + 1) * (layout.height - layout.topGap) / layout.rows);
    const contentTop = top + (bottom - top > 51 ? Math.floor((bottom - top - 51) / 2) : 0);
    const x = layout.left + left + Math.floor((right - left - 32) / 2);
    const y = layout.top + contentTop;
    if (x < 0 || y < 0 || x + 32 > screen.width || y + 32 > screen.height)
      throw new Error('Games Launcher grid is outside the guest framebuffer');
    const pixels = new Uint8ClampedArray(32 * 32 * 4);
    let ink = 0;
    for (let line = 0; line < 32; ++line) {
      const source = ((y + line) * screen.width + x) * 4;
      const dest = line * 32 * 4;
      pixels.set(screen.data.subarray(source, source + 32 * 4), dest);
      for (let col = 0; col < 32; ++col) {
        const i = dest + col * 4;
        if (Math.min(pixels[i], pixels[i + 1], pixels[i + 2]) < 210) ++ink;
      }
    }
    return {pixels, ink};
  };
  const launcherGridSample = (screen, layout) => {
    // A stable pair of successive full-resolution frames protects against
    // extracting icons while the 68K catalog is still populating its grid.
    let fingerprint = 2166136261, occupied = 0;
    const icons = [];
    for (let slot = 0; slot < layout.expected; ++slot) {
      const icon = launcherIconPixels(screen, layout, slot);
      icons.push(icon);
      if (icon.ink >= 8) ++occupied;
      for (let i = 0; i < icon.pixels.length; i += 4) {
        fingerprint = Math.imul(fingerprint ^ icon.pixels[i], 16777619) >>> 0;
        fingerprint = Math.imul(fingerprint ^ icon.pixels[i + 1], 16777619) >>> 0;
        fingerprint = Math.imul(fingerprint ^ icon.pixels[i + 2], 16777619) >>> 0;
      }
    }
    return {fingerprint, occupied, icons};
  };
  const launcherIconPNGBase64 = async rgba => {
    // Finder's white icon background is not part of the artwork. Export
    // exact-white pixels with zero alpha; leave every nonwhite icon pixel
    // unchanged. Do not modify the source guest framebuffer.
    const pixels = new Uint8ClampedArray(rgba);
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i] === 255 && pixels[i + 1] === 255 && pixels[i + 2] === 255) {
        pixels[i] = pixels[i + 1] = pixels[i + 2] = 0;
        pixels[i + 3] = 0;
      }
    }
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 32;
    canvas.getContext('2d').putImageData(new ImageData(pixels, 32, 32), 0, 0);
    // PNG bytes are sent to the authenticated HTTP host as JSON. No data:
    // URL, guest clipboard, ZIP download or CSP exception is involved.
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!blob) throw new Error('Could not encode original guest icon PNG');
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let text = '';
    for (const byte of bytes) text += String.fromCharCode(byte);
    return btoa(text);
  };
  const checkLauncherWatch = async frame => {
    const watch = frame?.gamesLauncherWatch;
    if (!watch || watch.busy || !visibleFrame(frame) || !frame.cinterForgeScreen) return;
    watch.busy = true;
    try {
      const source = frame.cinterForgeScreen;
      const os = frame.cinterForge68KStartup?.os;
      if (!os || !Object.hasOwn(cinterForgeOSAddress, os)) return;
      const worker = await getOCRWorker();
      if (!visibleFrame(frame) || frame.gamesLauncherWatch !== watch) return;
      const answer = await worker.recognize(launcherTitleImage(source));
      const title = String(answer?.data?.text || '').replace(/[^a-z]+/gi, ' ').toLowerCase();
      if (!/\bgames\s+launcher\b/.test(title)) {
        watch.fingerprint = null;
        watch.stableFrames = 0;
        return;
      }
      const layout = launcherDimensions({...source, os});
      const sample = launcherGridSample(source, layout);
      if (sample.occupied < Math.floor(layout.expected * 0.8)) {
        console.info(`[Games Launcher ${os}] waiting for populated grid: ${sample.occupied}/${layout.expected}`);
        return;
      }
      watch.stableFrames = sample.fingerprint === watch.fingerprint ? watch.stableFrames + 1 : 1;
      watch.fingerprint = sample.fingerprint;
      if (watch.stableFrames < 2) return;
      // Snapshot the exact OCR-confirmed frame before awaiting any network I/O.
      // A subsequently opened game cannot affect the PNGs saved below.
      const captured = [], blank = [];
      console.info(`[Games Launcher ${os}] OCR confirmed; extracting ${layout.expected} native 32x32 cells`);
      for (let slot = 0; slot < layout.expected; ++slot) {
        if (!visibleFrame(frame) || frame.gamesLauncherWatch !== watch) return;
        const icon = sample.icons[slot];
        if (icon.ink < 8) blank.push(slot + 1); // Preserve even apparently empty real slots.
        const png_base64 = await launcherIconPNGBase64(icon.pixels);
        await CinterForgeState.api('/api/infinitemac/game-icons', {
          os: cinterForgeOSAddress[os], slot: slot + 1, png_base64
        });
        captured.push(slot + 1);
      }
      const result = await CinterForgeState.api('/api/infinitemac/game-icons/finish', {
        os: cinterForgeOSAddress[os], captured, missing: [], blank_slots: blank
      });
      console.info(`[Games Launcher ${os}] saved ${captured.length}/${layout.expected} PNG icons to ${result.directory}`);
      CinterForgeState.notice(`Games Launcher ${os}: saved ${captured.length} icon PNGs` +
        (blank.length ? ` (${blank.length} appear blank; review)` : ''));
      stopLauncherWatch(frame);
    } catch (error) {
      // Preserve the working emulator when the developer Assets destination
      // is unavailable. Do not attempt downloads or alternate destinations.
      console.error('Games Launcher icon capture:', error);
      stopLauncherWatch(frame);
      CinterForgeState.notice('Games Launcher icon capture: ' + error.message);
    } finally {
      watch.busy = false;
    }
  };
  const beginLauncherWatch = frame => {
    if (!frame?.cinterForge68KStartup?.ready || frame.gamesLauncherWatch || !visibleFrame(frame)) return;
    frame.gamesLauncherWatch = {timer: null, busy: false, fingerprint: null, stableFrames: 0};
    frame.gamesLauncherWatch.timer = setInterval(() => { void checkLauncherWatch(frame); }, 2000);
    void checkLauncherWatch(frame);
  };
  // Physical ADB key events use the same documented emulator controls as
  // the existing Lagomorph integration. All guest catalog commands are ASCII.
  const gameKeyCode = character => character === ' ' ? 'Space' :
    /[a-z]/.test(character) ? 'Key' + character.toUpperCase() :
    /[0-9]/.test(character) ? 'Digit' + character : null;
  const gamePause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const gameKey = async (frame, code, heldMs = 55) => {
    const send = type => frame.contentWindow.postMessage({type, code}, 'https://infinitemac.org');
    send('emulator_key_down');
    try { await gamePause(heldMs); } finally { send('emulator_key_up'); }
    await gamePause(35);
  };
  // The host retransmits the *same* framed command while the guest starts:
  //   cinterbegin <token> play <guest-command> cinterend <token>
  // The 68K application executes a matching complete frame once per token.
  // Do not rely on a brittle OCR window-title match to deliver input. Initial
  // Finder launching still uses OCR; after that we retry the keyboard packet.
  const deliver68KGame = async frame => {
    const startup = frame?.cinterForge68KStartup;
    const game = pending68KGames.get(startup?.os);
    if (!game || game.sending || !visibleFrame(frame) || !frame.cinterForgeScreen)
      return;
    // Never type during boot or during the scripted Finder double-clicks.
    // If 7.6's Terminal OCR is late, the completed Finder launch attempt is
    // sufficient to begin retries (not a reason to stall forever).
    const launchAttemptComplete = startup?.loaded && startup.attempts > 0 &&
      !startup.launching && Date.now() - startup.lastAttemptAt >= 1800;
    if (startup?.launching || (!startup?.ready && !launchAttemptComplete)) return;
    if (Date.now() < game.nextSendAt) return;
    if (game.attempts >= max68KGameAttempts) {
      CinterForgeState.notice('Unable to confirm launch of ' + game.title +
        ' after ' + game.attempts + ' keyboard attempts');
      CinterForgeState.cancel68KGame(startup.os);
      return;
    }
    game.sending = true;
    const packet = `cinterbegin ${game.token} play ${game.command} cinterend ${game.token}`;
    try {
      frame.focus();
      // Clear any interrupted frame before the next BEGIN marker. This is a
      // key event only; no host or guest clipboard and no new emulator disk.
      await gameKey(frame, 'Escape', 55);
      for (const character of packet) {
        if (!visibleFrame(frame) || pending68KGames.get(startup.os) !== game)
          return;
        const code = gameKeyCode(character);
        if (!code) throw new Error('Unsupported character in framed 68K command');
        await gameKey(frame, code);
      }
      if (!visibleFrame(frame) || pending68KGames.get(startup.os) !== game)
        return;
      await gameKey(frame, 'Enter', 90);
      game.attempts++;
      game.nextSendAt = Date.now() + 1900;
      console.info(`[68K ${startup.address}] sent framed play ${game.command}; ` +
        `request=${game.token}; attempt=${game.attempts}`);
    } catch (error) {
      game.nextSendAt = Date.now() + 2500;
      console.error('68K framed game keyboard:', error);
      CinterForgeState.notice('68K game keyboard: ' + error.message);
    } finally {
      game.sending = false;
    }
  };

  // OCR is used here *only* to stop retries after the game takes the front,
  // never to gate delivery of a complete keyboard packet. Require two
  // observations because an intermittent OCR miss must not lose a request.
  const check68KGameForeground = async frame => {
    const startup = frame?.cinterForge68KStartup;
    const game = pending68KGames.get(startup?.os);
    if (!game || game.checking || game.sending || !game.attempts ||
        !visibleFrame(frame) || !frame.cinterForgeScreen) return;
    game.checking = true;
    try {
      const worker = await getOCRWorker();
      const answer = await worker.recognize(menuBarImage(frame.cinterForgeScreen));
      if (pending68KGames.get(startup.os) !== game || !visibleFrame(frame)) return;
      const state = classifyMenuText(answer?.data?.text);
      if (state === 'TERMINAL_RUNNING') {
        game.sawTerminal = true;
        game.awayChecks = 0;
      } else if ((game.sawTerminal || startup.ready) &&
          (state === 'OTHER_APP_OR_MENU' || state === 'BOOT_OR_BLANK')) {
        game.awayChecks++;
        if (game.awayChecks >= 2) {
          console.info(`[68K ${startup.address}] game took foreground; ` +
            `stop framed keyboard retries for ${game.command}`);
          CinterForgeState.cancel68KGame(startup.os);
        }
      } else game.awayChecks = 0;
    } catch (error) {
      if (!game.ocrWarning) {
        console.warn('68K launch completion check unavailable:', error);
        game.ocrWarning = true;
      }
    } finally {
      game.checking = false;
    }
  };
  CinterForgeState.resume68KGame = tab => {
    const frame = tab && CinterForgeState.emulatorFrames.get(tab.key);
    if (!frame) return;
    if (frame.cinterForge68KStartup?.loaded && !frame.cinterForge68KStartup.ready)
      beginScreenWatch(frame);
    void deliver68KGame(frame);
  };
  // Retry a bounded number of times with the same request token. The guest
  // deduplicates complete packets; the host stops when the game takes focus.
  setInterval(() => {
    for (const frame of CinterForgeState.emulatorFrames.values()) {
      if (!pending68KGames.has(frame.cinterForge68KStartup?.os)) continue;
      void check68KGameForeground(frame);
      void deliver68KGame(frame);
    }
  }, 900);

  const stopScreenWatch = frame => {
    if (!frame?.screenWatch) return;
    clearInterval(frame.screenWatch.timer);
    frame.screenWatch = null;
  };
  const checkScreen = async frame => {
    const watch = frame?.screenWatch;
    if (!watch || !visibleFrame(frame)) return;
    const label = frame.lagomorphWatch ? 'Lagomorph' : `68K ${frame.cinterForge68KStartup.address}`;
    const check = ++watch.checks;
    if (watch.busy) {
      console.info(`[${label}] OCR check ${check}: OCR_BUSY`);
      return;
    }
    if (!frame.cinterForgeScreen) {
      console.info(`[${label}] OCR check ${check}: WAITING_FOR_FRAME`);
      return;
    }
    watch.busy = true;
    let result = 'OCR_UNAVAILABLE', recognized = '';
    try {
      const worker = await getOCRWorker();
      const image = menuBarImage(frame.cinterForgeScreen);
      const answer = await worker.recognize(image);
      recognized = String(answer?.data?.text || '').replace(/\s+/g, ' ').trim();
      result = classifyMenuText(recognized);
    } catch (error) {
      result = 'OCR_FAILED';
      if (!watch.ocrFailureReported) {
        console.error(`[${label}] OCR cannot initialize; automated clicks disabled:`, error);
        watch.ocrFailureReported = true;
      }
    } finally {
      watch.busy = false;
    }
    if (!visibleFrame(frame) || frame.screenWatch !== watch) return;
    console.info(`[${label}] OCR check ${check}: ${result}; menu=\"${recognized}\"`);
    watch.finderMatches = result === 'FINDER_VISIBLE' ? watch.finderMatches + 1 : 0;
    if (frame.lagomorphWatch) {
      const lag = frame.lagomorphWatch;
      if (lag.loaded && watch.finderMatches >= 2 && !lag.started) {
        lag.started = true;
        stopScreenWatch(frame);
        console.info('[Lagomorph] Finder detected; starting guest controls');
        void CinterForgeState.lagomorphBootControls(lag.stamp, frame.contentWindow)
          .catch(error => console.error('Lagomorph guest controls:', error));
      }
      return;
    }
    const startup = frame.cinterForge68KStartup;
    if (!startup) return;
    startup.screenState = result;
    if (result === 'TERMINAL_RUNNING') {
      startup.ready = true;
      stopScreenWatch(frame);
      console.info(`[68K ${startup.address}] Terminal menu detected; Finder automation complete`);
      if (pending68KGames.has(startup.os)) void deliver68KGame(frame);
      else {
        beginLauncherWatch(frame);
        void speak68KReadyGreeting(frame);
      }
    } else if (startup.loaded && watch.finderMatches >= 2) {
      CinterForgeState.schedule68KAppOpen(frame);
    }
  };
  const beginScreenWatch = frame => {
    if (!frame || frame.screenWatch || !visibleFrame(frame)) return;
    frame.screenWatch = {timer: null, checks: 0, busy: false, finderMatches: 0, ocrFailureReported: false};
    frame.screenWatch.timer = setInterval(() => { void checkScreen(frame); },
      cinterForge68KScreenCheckMs);
    void checkScreen(frame);
  };

  // Click only after repeated OCR recognition of the Finder menu. A new
  // attempt requires another OCR observation; no blind timer-based retries.
  CinterForgeState.schedule68KAppOpen = frame => {
    const startup = frame?.cinterForge68KStartup;
    if (!startup?.loaded || startup.ready || startup.launching ||
        !liveStartup(frame) || startup.screenState !== 'FINDER_VISIBLE' ||
        frame.screenWatch?.finderMatches < 2 ||
        Date.now() - startup.lastAttemptAt < finderRetryDelayMs) return;
    // Until DingusPPC pointer alignment is proven in a live guest, do not
    // inject blind Finder clicks that can hold up the guest mouse. Manual
    // mouse control must work independently of startup automation.
    if (frame.relativeMouse) return;
    const finder = cinterForge68KFinder[startup.os];
    if (!finder) return;
    // The OCR framebuffer supplies the authoritative guest width.
    // If no screen is available, do not inject a guessed mouse position.
    const diskPosition = () => {
      const width = frame.cinterForgeScreen?.width;
      if (!Number.isInteger(width) || width < 320)
        throw new Error('68K Finder OCR has no valid guest framebuffer width');
      return {x: width - finder.diskRightInset, y: finder.diskY};
    };
    const active = () => visibleFrame(frame);
    if (!active()) return; // Resume on tab activation; never click another tab.
    // Do not click during boot or when another app is frontmost. Both
    // actions and classification use guest-framebuffer coordinates.
    void (async () => {
      if (!active() || startup.ready || startup.screenState !== 'FINDER_VISIBLE') return;
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
      startup.lastAttemptAt = Date.now();
      startup.launching = true;
      console.info(`CinterForge 68K ${startup.address}: Finder launch attempt ${startup.attempts}`);
      // Ignore simultaneous host pointer moves during the scripted click.
      // A moving real cursor must not steal the guest pointer between presses.
      frame.cinterForgeAutomationClicking = true;
      try {
        frame.focus();
        // Opening the desktop disk icon also brings its Finder window to
        // the front when already open. This avoids any assumptions about
        // an HFS auto-open hint or stale window positions. Never close an
        // arbitrary window (including Infinite HD) at fixed coordinates.
        const disk = diskPosition();
        console.info(`CinterForge 68K ${startup.address}: open Terminal disk at (${disk.x}, ${disk.y})`);
        if (!await doubleClick(disk)) return;
        await pause(650); // Give Finder time to display the root directory.
        if (!active() || startup.ready) return;
        console.info(`CinterForge 68K ${startup.address}: open Terminal application at (${finder.app.x}, ${finder.app.y})`);
        await doubleClick(finder.app);
      } catch (error) {
        console.error('CinterForge 68K Finder launch:', error);
      } finally {
        up();
        frame.cinterForgeAutomationClicking = false;
        frame.resetPointerRelay?.();
        startup.launching = false;
        // Screen OCR on the next tick decides whether a retry is warranted.
      }
    })();
  };

  window.addEventListener('message', event => {
    if (event.origin !== 'https://infinitemac.org' ||
        event.data?.type !== 'emulator_loaded') return;
    for (const frame of CinterForgeState.emulatorFrames.values()) {
      if (event.source !== frame.contentWindow) continue;
      if (frame.cinterForge68KStartup) frame.cinterForge68KStartup.loaded = true;
      if (frame.lagomorphWatch) frame.lagomorphWatch.loaded = true;
      beginScreenWatch(frame);
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
      stopScreenWatch(frame);
      stopLauncherWatch(frame);
      frame.hidden = true;
      if (frame.pointerRelay) frame.pointerRelay.hidden = true;
    }
    CinterForgeState.activeEmulatorFrame = null;
  };

  CinterForgeState.removeEmulatorFrame = tab => {
    const frame = CinterForgeState.emulatorFrames.get(tab.key);
    if (!frame) return;
    // Releasing a tab is the only time we intentionally destroy its guest.
    stopScreenWatch(frame);
    stopLauncherWatch(frame);
    frame.cinterForgeScreen = null;
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

  // The emulator sends raw RGBA pixels when screen_update_messages is set.
  // Keep only the newest frame per active 68K guest, for the Finder classifier.
  window.addEventListener('message', event => {
    if (event.origin !== 'https://infinitemac.org' ||
        event.data?.type !== 'emulator_screen') return;
    for (const frame of CinterForgeState.emulatorFrames.values()) {
      if (event.source !== frame.contentWindow) continue;
      const {width, height, data} = event.data;
      if (!Number.isInteger(width) || !Number.isInteger(height) ||
          width < 320 || height < 240 || width > 4096 || height > 3072)
        break;
      if (frame.guestScreen?.width !== width || frame.guestScreen?.height !== height) {
        frame.guestScreen = {width, height};
        frame.resetPointerRelay?.();
      }
      if ((frame.cinterForge68KStartup || frame.lagomorphWatch) &&
          (data instanceof Uint8Array || data instanceof Uint8ClampedArray) &&
          data.length === width * height * 4) {
        frame.cinterForgeScreen = {width, height, data};
      }
      break;
    }
  });

  // Original source lines 441–441
  CinterForgeState.lagBootSequence = 0;

  // Original source lines 442–442

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

  // Original source lines 630–649
  CinterForgeState.showTerminal = () => {
    if (CinterForgeState.terminalOverlay.dataset.codeWrapperHidden === 'true')
      CinterForgeState.setCodeWrapperHidden(false);
    CinterForgeState.lagBootSequence++;
    // Only the recyclable emulator frame is disposed. The Code-OSS iframe
    // retains its workbench, editor state and all legitimate PTYs.
    CinterForgeState.codeFrame.hidden = true;
    CinterForgeState.hideEmulatorFrames();
    CinterForgeState.frame.hidden = false;
    CinterForgeState.frame.src = 'about:blank';
    delete CinterForgeState.frame.dataset.launcher;
    CinterForgeState.codeError.hidden = true;
    CinterForgeState.computerView.hidden = true;
    if (CinterForgeState.platform === 'mac')
      void CinterForgeState.nativeEvent('clipboard-context', {embedded: false});
    CinterForgeState.$('terminal-keys').hidden = false;
    CinterForgeState.$('terminal-screen').hidden = false;
    CinterForgeState.$('terminal-input').disabled = false;
  };

  // Original source lines 653–730
  CinterForgeState.showComputer = async (profile, tab) => {
    CinterForgeState.hideEmulatorFrames();
    CinterForgeState.lagBootSequence++;
    // An emulator lives only in the iframe. Host titlebar menus must survive
    // every VM load and remain clickable above the embedded screen.
    document.querySelectorAll('.terminal-menu[open]').forEach(menu => {
      menu.open = false;
    });
    CinterForgeState.$('terminal-screen').hidden = true;
    CinterForgeState.$('terminal-input').disabled = true;
    CinterForgeState.computerView.hidden = false;
    if (CinterForgeState.platform === 'mac')
      void CinterForgeState.nativeEvent('clipboard-context', {embedded: true});
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
        if (existing.lagomorphWatch && !existing.lagomorphWatch.started)
          existing.lagomorphWatch.stamp = CinterForgeState.lagBootSequence;
        if (existing.cinterForge68KStartup?.ready) {
          if (pending68KGames.has(existing.cinterForge68KStartup.os)) void deliver68KGame(existing);
          else beginLauncherWatch(existing);
        }
        else if (existing.cinterForge68KStartup?.loaded || existing.lagomorphWatch?.loaded)
          beginScreenWatch(existing);
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
      // The CinterForge Terminal disk is only for the two supported 68K OS versions.
      // Leave every other System 7/8/9 guest's Infinite HD behavior alone,
      // and never interfere with Lagomorph's separate calibrated media.
      // The dynamic macGame profile for a Quadra 650 is not a Lagomorph
      // session, even though the museum's Quadra 650 profile is reserved.
      const cinterForgeGameOS = Object.hasOwn(cinterForgeOSAddress, profile.disk);
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
        // Infinite Mac reads the host text clipboard on a real paste gesture.
        // Delegate clipboard-read to the trusted emulator origin only; without
        // this permission, its readText() is denied inside our cross-origin iframe.
        iframe.setAttribute('allow', 'cross-origin-isolated; clipboard-read https://infinitemac.org');
        iframe.hidden = true;
        CinterForgeState.computerView.append(iframe);
        CinterForgeState.emulatorFrames.set(tab.key, iframe);
        // Only attempt guest app startup after its HFS disk URL was attached.
        // No automation is scheduled for missing media or Lagomorph.
        if (cinterForgeGameOS && !profile.lagomorph &&
            url.searchParams.getAll('disk_url').length === 1)
          iframe.cinterForge68KStartup = {
            os: profile.disk, address: cinterForgeOSAddress[profile.disk],
            tab, loaded: false, ready: false, attempts: 0, launching: false,
            lastAttemptAt: 0, screenState: 'WAITING_FOR_FRAME' 
          };
        if (profile.id === 'quadra-650' && profile.lagomorph)
          iframe.lagomorphWatch = {tab, stamp: CinterForgeState.lagBootSequence,
            loaded: false, started: false};
        CinterForgeState.installInfiniteMacPointerRelay(iframe);
        // Receive actual RGBA frames to identify Finder before clicking.
        url.searchParams.set('screen_update_messages', 'true');
        url.searchParams.set('screen_update_interval', String(cinterForge68KScreenCheckMs));
        // Boot the original tab without writing a clipboard challenge.
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
