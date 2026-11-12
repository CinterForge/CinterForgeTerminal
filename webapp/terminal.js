/**
 * CinterForge Terminal — browser and native WebView user interface
 * ===========================================================================
 *
 * OVERVIEW
 * --------
 * This is the single user-interface controller for CinterForge Terminal on
 * macOS, Windows, Linux, and the LAN-accessible browser. The HTML/CSS provide
 * the screen and menus; this JavaScript connects them to the existing Python
 * HTTP service and, when present, the native application bridge.
 *
 * The browser does not launch shell processes itself. Python owns PTY/ConPTY
 * sessions, command execution, Bedrock server state, Discord API credentials,
 * and HTTP endpoints. This file displays results, forwards user input, and
 * manages which independent session is currently visible.
 *
 * DATA FLOW
 * ---------
 *   Keyboard / on-screen buttons
 *       -> active tab's input handler
 *       -> authenticated HTTP API -> host process / service
 *       -> polled results -> tab-owned VTOutput -> visible screen
 *
 * TAB TYPES
 * ---------
 * - Shell / Minecraft: independently owned host-side terminal sessions.
 * - Discord: selected server/channel history and bot messages, with a local
 *   editable draft. Credentials stay in the Python service, not this file.
 * - Interactive fiction (IF): a browser Z-machine interpreter per story tab,
 *   with an independent input draft, output buffer, and Quetzal autosave.
 * - Server: read-only view of the host's persisted Python/HTTP log.
 * - Computers and games: Code-OSS, JOSHUA, and Infinite Mac inside an iframe.
 *
 * STATE AND LIFETIME
 * ------------------
 * Each tab owns its output renderer and, where appropriate, its shell session,
 * interpreter, cursor, message history, and polling generation. `activeTab`
 * only selects the presentation; switching tabs must not stop a background
 * session. Closing a tab releases only its own resources. Generation checks
 * prevent late network replies from reviving a closed tab or stealing focus.
 *
 * CONNECTIONS AND TRUST
 * ---------------------
 * `api()` talks to the *same-origin* host service, sending the token supplied
 * by the served HTML. A user who logs into a LAN Terminal controls the remote
 * host's real development environment. Authorization, session security, and
 * secret storage belong to the Python/native host, not to UI-side JavaScript.
 * LAN discovery opens an additional window; it does not replace local tabs.
 * Infinite Mac messages use the emulator's explicit origin, and Code-OSS
 * messages are checked against the current local frame and origin.
 *
 * HOST INTERFACES
 * ---------------
 * - `window.CinterHost`: authenticated terminal, debugger, and Bedrock API.
 * - `window.CinterTerminalNewTab(kind)`: called by native New Terminal menus.
 * - `window.CinterShowLAN()`: show/refresh available LAN Terminal hosts.
 * - `window.CinterEdit`: copy/paste/transcript actions for native Edit menus.
 * - `window.cinterNative`, `cinterConnect`, `cinterClipboard` (when present):
 *   callbacks installed by the platform-specific native host.
 *
 * MAINTENANCE NOTES
 * -----------------
 * This is an IIFE in strict mode to avoid exporting internal mutable state.
 * Keep shell/Discord/IF input routes distinct. Preserve browser-side token
 * handling, existing HTTP contracts, native menu semantics, and tab ownership.
 * In particular, the Quadra 650 / Lagomorph automation relies on calibrated
 * 640x480 guest coordinates and exact delays: formatting is safe, changing
 * its event order or timing is not. See the functions below for local details.
 *
 * Source sections follow the runtime: configuration -> host bridge -> VT
 * output -> embedded computers -> tabs -> Discord/LAN -> IF -> terminal I/O ->
 * menus/input -> Bedrock consent -> event polling and startup.
 */
(() => {
  'use strict';

  // -------------------------------------------------------------------------
  // CONFIGURATION: Lagomorph guest timing (milliseconds) and 640x480 positions
  // -------------------------------------------------------------------------
  // The external `emulator_loaded` event may arrive before the guest Finder
  // desktop is usable. This original fixed delay is intentionally unchanged.
  const LAGOMORPH_DELAYS_MS = Object.freeze({
    initialGuestBoot: 18000,
    colorMenuPointerMove: 150,
    colorMenuMouseHeld: 220,
    colorMenuSelection: 260,
    displayRedraw: 1000,
    clickPointerSettle: 140,
    clickPress: 110,
    afterControlStripClose: 350,
    beforeStickyNotesQuit: 300,
    quitModifierDown: 180,
    quitKeyDown: 230,
    betweenQuitAttempts: 800,
    beforeCloseWindowFallback: 400,
    closeModifierDown: 160,
    closeKeyDown: 220,
    beforeDiskOpen: 1100,
    doubleClickPointerSettle: 140,
    doubleClickFirstPress: 80,
    doubleClickPressGap: 100,
    doubleClickSecondPress: 80,
    betweenDiskAndAppOpen: 1400,
  });
  // Coordinates are measured in the *guest* Quadra 650 screen, not the host.
  // Do not adjust these until the cold-boot issue is diagnosed and evidenced.
  const LAGOMORPH_GUEST_POSITIONS = Object.freeze({
    colorDepthMenu: Object.freeze({ x: 40, y: 469 }),
    sixteenColors: Object.freeze({ x: 80, y: 400 }),
    controlStripToggle: Object.freeze({ x: 8, y: 469 }),
    lagomorphDisk: Object.freeze({ x: 600, y: 160 }),
    lagomorphApplication: Object.freeze({ x: 40, y: 142 }),
  });
  // -------------------------------------------------------------------------
  // CONFIGURATION: compact icons for Terminal menus and in-window Session tabs
  // -------------------------------------------------------------------------
  // Text glyphs avoid an external icon font or CDN dependency in the native
  // WebViews. Color and fixed-size framing are defined in terminal.css.
  // Decorative icons are hidden from screen readers; labels remain unchanged.
  const TERMINAL_ICON_GLYPHS = Object.freeze({
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


  // ===========================================================================
  // HOST BRIDGE / AUTHENTICATED HTTP
  // ===========================================================================

  // Keep the token in the served document, not a CSP-sensitive inline script.
  // A native WebView and a browser-only preview share this same authenticated API.
  const token = document.documentElement?.getAttribute('data-cph-token') || window.__CPH_TOKEN__;
  // Acceptance is a separate, deterministic UI probe, not a user session.
  const acceptUI = new URLSearchParams(location.search || '').has('accept-ui') ||
    Boolean(window.__CINTER_ACCEPT_UI__);
  // The Host registers this native WebView binding before navigating here.
  // Browser-only previews remain usable without a native host.
  /**
   * Notify the optional native host of a UI event; report bridge failures without interrupting
   * the browser UI.
   */
  const nativeEvent = (event, detail = {}) => {
    if (typeof window.cinterNative !== 'function') return Promise.resolve();
    return Promise.resolve(window.cinterNative({ event, ...detail })).catch(error => {
      console.error('Native Terminal bridge:', error);
    });
  };
  /**
   * Return the existing document element with the specified ID.
   */
  const $ = id => document.getElementById(id);
  // Native system menus own New Terminal and Edit actions. The HTML
  // titlebar contains the persistent Terminals selector only.
  /**
   * Send an authenticated JSON request to the Python service. Undefined data selects GET;
   * otherwise POST.
   */
  const api = async (path, data) => {
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-CPH-Token': token, ...(data === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(data === undefined ? {} : { body: JSON.stringify(data) })
    });
    const value = await response.json();
    if (response.status === 401) {
      // setpass takes effect immediately. An already-open browser window must
      // show the login page instead of leaving a disconnected Terminal visible.
      window.location.assign('/login');
      throw new Error('Terminal login required');
    }
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    return value;
  };
  // Same API in all applications; pages can call these methods when the Host
  // advertises the terminal capability, otherwise they must degrade cleanly.
  /** The stable browser/native API facade; all methods delegate to the Python host. */
  window.CinterHost = Object.freeze({
    terminal: Object.freeze({
      /** Create a new host PTY/ConPTY session. */
      open: args => api('/api/terminal/open', args || {}),
      /** Forward text or control bytes into one host session. */
      input: (id, data) => api('/api/terminal/input', { id, data }),
      /** Read pending output and lifecycle flags for a session. */
      poll: (id, wait_ms = 200) => api('/api/terminal/poll', { id, wait_ms }),
      /** Resize the terminal attached to an existing session. */
      resize: (id, cols, rows) => api('/api/terminal/resize', { id, cols, rows }),
      /** Release one host session. */
      close: id => api('/api/terminal/close', { id })
    }),
    /** Request the host JavaScript inspector. */
    debugger: () => api('/api/debugger', {}),
    bedrock: Object.freeze({
      /** Read Bedrock server status. */
      status: () => api('/api/bedrock/status'),
      /** Accept the Bedrock EULA explicitly. */
      accept: () => api('/api/bedrock/accept', { accepted: true }),
      /** Begin the authorized Bedrock installation. */
      install: () => api('/api/bedrock/install', {}),
      /** Start the installed Bedrock server. */
      start: () => api('/api/bedrock/start', {}),
      /** Stop the running Bedrock server. */
      stop: () => api('/api/bedrock/stop', {})
    })
  });


  // ===========================================================================
  // TERMINAL TEXT, ANSI CODES, AND SCROLLBACK
  // ===========================================================================

  // A deliberately bounded text/ANSI presentation. A full VT emulator may be
  // substituted without changing the Python Host terminal contract.
  class VTOutput {
    /**
     * Create a bounded VT text renderer with a target element and independent cursor/history
     * state.
     */
    constructor(target) {
      this.target = target;
      this.lines = [''];
      this.history = [];
      this.current = 0;
      this.esc = '';
    }

    /**
     * Return the committed text transcript without the visual caret or unsubmitted input.
     */
    transcript() {
      return this.history.concat(this.lines).join('\n');
    }

    /**
     * Interpret incoming printable text and supported ANSI controls, updating the current lines
     * and cursor.
     */
    push(text) {
      for (const c of text) {
        if (this.esc) {
          this.esc += c;
          if (this.esc.length > 64) {
            this.esc = '';
            continue;
          }
          if (this.esc === '\u001b[' || this.esc === '\u001b]') continue;
          if (this.esc.startsWith('\u001b[')) {
            if (/[A-Za-z~]/.test(c)) {
              if (c === 'J') {
                this.history.push(...this.lines);
                this.lines = [''];
                this.current = 0;
              } else if (c === 'H') this.current = 0;
              else if (c === 'K') {
                // CSI K defaults to erasing from cursor, not entire draft.
                const match = this.esc.match(/^\u001b\[([0-9;]*)K$/);
                const mode = match && match[1] ? Number(match[1].split(';')[0] || 0) : 0;
                const index = this.lines.length - 1;
                const line = this.lines[index];
                if (mode === 1) this.lines[index] = ' '.repeat(this.current) + line.slice(this.current);
                else if (mode === 2) this.lines[index] = '';
                else this.lines[index] = line.slice(0, this.current);
              }
              this.esc = '';
            }
          } else if (this.esc.startsWith('\u001b]')) {
            if (c === '\u0007' || this.esc.endsWith('\u001b\\')) this.esc = '';
          } else this.esc = '';
          continue;
        }
        if (c === '\u001b') {
          this.esc = c;
          continue;
        }
        if (c === '\r') {
          this.current = 0;
          continue;
        }
        if (c === '\n') {
          this.lines.push('');
          this.current = 0;
          continue;
        }
        if (c === '\b') {
          this.current = Math.max(0, this.current - 1);
          continue;
        }
        if (c === '\t') {
          this.push(' '.repeat(8 - this.current % 8));
          continue;
        }
        if (c.charCodeAt(0) < 32 && c !== '\u0000') continue;
        const index = this.lines.length - 1, old = this.lines[index];
        this.lines[index] = old.padEnd(this.current, ' ').slice(0, this.current) + c + old.slice(this.current + 1);
        this.current++;
      }
      if (this.lines.length > 2500) this.history.push(...this.lines.splice(0, this.lines.length - 2500));
      this.render();
    }

    /**
     * Insert older Discord messages above the current view without losing the reader’s scroll
     * position.
     */
    prepend(text) {
      // Older Discord messages belong above the visible history, not below
      // the live input. Preserve the exact line currently being read.
      const earlier = String(text).split('\n');
      if (earlier[earlier.length - 1] === '') earlier.pop();
      if (!earlier.length) return;
      this.lines.unshift(...earlier);
      this.render({ insertedAbove: true });
    }

    /**
     * Draw the output and caret for the active view, respecting Discord’s draft and scroll-follow
     * mode.
     */
    render({ insertedAbove = false } = {}) {
      // Put the insertion marker at the real VT cursor, without adding any
      // character to the copied transcript or the live shell input.
      const parent = this.target.parentElement;
      const previousTop = parent?.scrollTop || 0, previousHeight = parent?.scrollHeight || 0;
      const visibleDiscord = this.target === $('terminal-output') && activeTab?.kind === 'discord';
      const followTail = !visibleDiscord || activeTab.followTail;
      const line = this.lines[this.lines.length - 1] || '';
      const before = this.lines.slice(0, -1).join('\n');
      const prefix = (this.lines.length > 1 ? before + '\n' : '') + line.slice(0, this.current);
      const suffix = line.slice(this.current);
      const caret = document.createElement('span');
      caret.className = 'terminal-cursor';
      caret.setAttribute('aria-hidden', 'true');
      // Discord's editable draft is a real DOM node. Never let the VT cursor
      // compete with the draft caret, or leave a CSS-only echo behind on redraw.
      this.target.replaceChildren(...(visibleDiscord
        ? [document.createTextNode(prefix + suffix)]
        : [document.createTextNode(prefix), caret, document.createTextNode(suffix)]));
      if (visibleDiscord) {
        activeTab.visibleDraft = null;
        paintDiscordDraft(activeTab);
      }
      if (parent) {
        if (insertedAbove && visibleDiscord)
          parent.scrollTop = previousTop + Math.max(0, parent.scrollHeight - previousHeight);
        else if (followTail) parent.scrollTop = parent.scrollHeight;
        else parent.scrollTop = previousTop;
      }
    }

    /**
     * Clear this renderer’s retained lines, history, and cursor, then repaint.
     */
    reset() {
      this.lines = [''];
      this.history = [];
      this.current = 0;
      this.render();
    }
  }

  /**
   * Decode base64-encoded UTF-8 bytes returned by terminal polling.
   */
  const decode = data => {
    const binary = atob(data), bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
    return new TextDecoder('utf-8').decode(bytes);
  };

  // ===========================================================================
  // SHARED UI STATE, PROFILE METADATA, AND ICONS
  // ===========================================================================

  let session = null, generation = 0, platform = 'linux', selected = '', profiles = [], waiting = false;
  const queuedSessionOpens = [];
  let macCatalog = new Map();
  /**
   * Read a machine profile’s kind, defaulting to a standard terminal.
   */
  const museumKind = x => x?.kind || 'terminal';
  // Display icons identify the same real terminal/emulator category on menu
  // buttons and tabs. No categories or launch behavior are changed here.
  /**
   * Choose the decorative icon category for a profile and its active backend.
   */
  function iconCategoryForProfile(profile, backend = 'native') {
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
  }


  /**
   * Append an accessibility-hidden icon while leaving the button’s text label available.
   */
  function appendDecorativeIcon(target, category) {
    const icon = document.createElement('span');
    icon.className = `terminal-icon terminal-icon--${category}`;
    icon.textContent = TERMINAL_ICON_GLYPHS[category] || TERMINAL_ICON_GLYPHS.terminal;
    icon.setAttribute('aria-hidden', 'true');
    // Appending after the label retains the existing textContent contract in
    // lightweight UI tests. CSS visually orders the icon before its label.
    target.append(icon);
  }


  // ===========================================================================
  // EMBEDDED COMPUTER PROFILES: CODE-OSS AND INFINITE MAC
  // ===========================================================================

  const frame = $('computer-frame'), computerView = $('computer-view');
  // Older staged HTML lacked this diagnostic node even when terminal.js was
  // newer. Repair that mixed-version presentation rather than crashing the
  // entire Terminal before its native UI/PTY acceptance callbacks can run.
  const codeError = $('computer-error') || (() => {
    if (!computerView) throw new Error('Terminal computer view is missing');
    const node = document.createElement('div');
    node.id = 'computer-error';
    node.className = 'computer-error';
    node.setAttribute('role', 'alert');
    node.hidden = true;
    computerView.append(node);
    return node;
  })();
  // The Code extension runs in a Web Worker. BroadcastChannel is the existing
  // same-origin control path; never create an outer PTY while Code is active.
  const CODE_COMMAND_CHANNEL = 'cinterforge-terminal-code-commands';
  const CODE_MENU_NAMES = ['File', 'Edit', 'Selection', 'View', 'Go', 'Run', 'Terminal', 'Help'];
  const codeCommands = typeof BroadcastChannel === 'function'
    ? new BroadcastChannel(CODE_COMMAND_CHANNEL) : null;
  let codeCommandSerial = 0;
  const codeIsActive = () => activeTab?.profileId === 'codeoss' &&
    !computerView.hidden && frame.src.includes('/codeoss/');

  // The CinterForge activity-bar button controls the existing outer wrapper,
  // not an IFPlayer game menu. The Code iframe and its PTYs remain mounted.
  const terminalOverlay = $('terminal-overlay');
  function setCodeWrapperHidden(hidden) {
    terminalOverlay.dataset.codeWrapperHidden = hidden ? 'true' : 'false';
    if (hidden) document.querySelectorAll('.terminal-menu[open]').forEach(menu => { menu.open = false; });
    frame.contentWindow?.postMessage({type: 'cinterforge-terminal-wrapper-state', hidden},
      window.location.origin);
  }
  window.addEventListener('message', event => {
    if (event.source !== frame.contentWindow || event.origin !== window.location.origin ||
        event.data?.type !== 'cinterforge-terminal-wrapper-toggle' || !codeIsActive()) return;
    setCodeWrapperHidden(terminalOverlay.dataset.codeWrapperHidden !== 'true');
  });

  /** Send a command to the actual Code-OSS extension, not the outer Terminal. */
  function sendCodeCommand(type, data = {}) {
    if (!codeIsActive()) return false;
    if (!codeCommands) {
      notice('Code-OSS command channel is unavailable');
      return false;
    }
    codeCommands.postMessage({type, ...data, requestId: ++codeCommandSerial});
    return true;
  }

  // Code-OSS runs its extension in a worker. WebKit can give that worker a
  // file: or blob: location even when the workbench is served over HTTP.
  // Resolve Terminal capabilities through the already-authenticated outer UI;
  // the extension must never guess 127.0.0.1 or inherit an IFPlayer control URL.
  if (codeCommands) codeCommands.onmessage = async event => {
    const message = event.data || {};
    if (message.type !== 'host-probe' && message.type !== 'host-request') return;
    if (typeof message.requestId !== 'string' ||
        !/^host-[A-Za-z0-9-]{12,100}$/.test(message.requestId) ||
        !frame.src.includes('/codeoss/')) return;

    const replyType = message.type === 'host-probe' ? 'host-probe-response' : 'host-request-response';
    try {
      if (message.type === 'host-probe') {
        const payload = await api('/host/code-workspace');
        codeCommands.postMessage({type: replyType, requestId: message.requestId, ok: true, payload});
        return;
      }
      const path = String(message.path || '');
      // Only the existing Code-OSS filesystem and shell contracts can cross
      // this UI bridge; it is not an arbitrary privileged HTTP proxy.
      if (path !== '/code/command' &&
          !/^\/host\/code-shell-(open|close|write|resize|output)(?:\?[^#]*)?$/.test(path))
        throw new Error('Unsupported Code-OSS host operation');
      const payload = message.method === 'GET' ? await api(path) : await api(path, message.body || {});
      codeCommands.postMessage({type: replyType, requestId: message.requestId, ok: true, payload});
    } catch (error) {
      codeCommands.postMessage({type: replyType, requestId: message.requestId,
        ok: false, error: String(error.message || error)});
    }
  };

  const codeUrl = '/codeoss/out/vs/code/browser/workbench/workbench.html';
  let codeLoad = 0, codeReady = false, codeLastError = '';
  /**
   * Report a Code-OSS workbench failure only if the affected iframe is still the selected view.
   */
  const codeFailure = message => {
    if (computerView.hidden || !frame.src.includes(codeUrl) || codeReady) return;
    codeLastError = String(message).slice(0, 450);
    codeError.textContent = 'VS Code did not start.\n' + codeLastError + '\nOpen Debugger for WebKit details.';
    codeError.hidden = false;
    notice('VS Code: ' + codeLastError);
    void nativeEvent('error', { message: 'VS Code: ' + codeLastError });
  };
  // Require a real workbench DOM, not merely a successful HTML response.
  // Staged Code-OSS sends early resource/script errors from its HTTP response.
  window.addEventListener('message', event => {
    if (event.origin !== new URL(location.href).origin || event.source !== frame.contentWindow ||
      computerView.hidden || !frame.src.includes(codeUrl)) return;
    const data = event.data;
    if (!data || data.type !== 'cinterforge-codeoss-health') return;
    if (data.status === 'ready') {
      codeReady = true;
      codeError.hidden = true;
      updateCodeMenuMode();
      requestCodeServiceMenus();
      for (const name of CODE_MENU_NAMES) void refreshCodeMenu(name);
      notice('VS Code workbench ready');
    } else if (data.status === 'error') {
      codeLastError = String(data.message || 'Workbench error').slice(0, 450);
    } else if (data.status === 'timeout') {
      codeFailure(codeLastError || data.message || 'Workbench did not initialize');
    }
  });
  // A successful iframe load is not proof that the JavaScript workbench ran.
  // Check the critical resources separately, then require an explicit ready.
  /**
   * Probe the required Code-OSS resources and report failures for the current load generation.
   */
  const checkCodeAssets = async stamp => {
    for (const url of [codeUrl, '/codeoss/out/vs/code/browser/workbench/workbench.js',
      '/codeoss/out/nls.messages.js']) {
      try {
        const response = await fetch(url, { method: 'HEAD', cache: 'no-store' });
        if (stamp !== codeLoad) return;
        if (!response.ok) {
          codeFailure(`${url}: HTTP ${response.status}`);
          return;
        }
      } catch (error) {
        if (stamp === codeLoad) codeFailure(`${url}: ${error.message}`);
        return;
      }
    }
  };
  // The private Quadra 650 uses 640x480 guest coordinates. These are NOT
  // coordinates in the host browser/window (which users may resize or zoom).
  // Calibrated against a real Mac OS 7.6 Control Strip screenshot:
  //   display-depth module (40,469), 16 Colors row (80,400), close box (8,469).
  // Classic Mac pop-up menus use mouse down -> move while held -> mouse up.
  // Change the color mode first; only then dismiss the Control Strip, bring
  // the Welcome/Sticky Notes window to the front and send Cmd+Q.
  // No cross-origin DOM inspection or synthetic browser input is involved.
  let lagBootSequence=0;
  let lagWelcomeTimer = null;
  /**
   * Wait for a calibrated guest-emulator delay; callers independently check whether their launch
   * is current.
   */
  const lagPause = ms => new Promise(resolve => setTimeout(resolve, ms));
  /**
   * Drive the existing Quadra 650 display-depth, Control Strip, Sticky Notes, and game-launch
   * gestures.
   */
  const lagomorphBootControls = async (stamp, target) => {
    /**
     * Confirm that the current Lagomorph startup still owns the selected iframe and launch
     * generation.
     */
    const valid = () => stamp === lagBootSequence && !computerView.hidden &&
      selected === 'quadra-650' && frame.contentWindow === target;
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
      await lagPause(LAGOMORPH_DELAYS_MS.clickPointerSettle);
      if (!valid()) return false;
      mouseDown();
      await lagPause(LAGOMORPH_DELAYS_MS.clickPress);
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
      await lagPause(LAGOMORPH_DELAYS_MS.doubleClickPointerSettle);
      if (!valid()) return false;
      mouseDown();
      await lagPause(LAGOMORPH_DELAYS_MS.doubleClickFirstPress);
      mouseUp();
      await lagPause(LAGOMORPH_DELAYS_MS.doubleClickPressGap);
      if (!valid()) return false;
      mouseDown();
      await lagPause(LAGOMORPH_DELAYS_MS.doubleClickSecondPress);
      mouseUp();
      return valid();
    };
    try {
      if (!valid()) return;
      move(LAGOMORPH_GUEST_POSITIONS.colorDepthMenu.x, LAGOMORPH_GUEST_POSITIONS.colorDepthMenu.y);
      await lagPause(LAGOMORPH_DELAYS_MS.colorMenuPointerMove);
      if (!valid()) return;
      mouseDown();
      await lagPause(LAGOMORPH_DELAYS_MS.colorMenuMouseHeld);
      if (!valid()) return;
      move(LAGOMORPH_GUEST_POSITIONS.sixteenColors.x, LAGOMORPH_GUEST_POSITIONS.sixteenColors.y);
      await lagPause(LAGOMORPH_DELAYS_MS.colorMenuSelection);
      mouseUp();
      if (!valid()) return;
      // Selecting a different pixel depth can briefly redraw the guest.
      await lagPause(LAGOMORPH_DELAYS_MS.displayRedraw);
      if (!valid()) return;
      // Hide the Control Strip exactly once.
      if (!await click(
        LAGOMORPH_GUEST_POSITIONS.controlStripToggle.x,
        LAGOMORPH_GUEST_POSITIONS.controlStripToggle.y
      )) return;
      await lagPause(LAGOMORPH_DELAYS_MS.afterControlStripClose);
      if (!valid()) return;
      // Do NOT click (8,469) again: this is the Control Strip toggle,
      // not Sticky Notes. The first click above closes it exactly once.
      // quitSticky() focuses the guest iframe without another mouse click.
      await lagPause(LAGOMORPH_DELAYS_MS.beforeStickyNotesQuit);
      if (!valid()) return;
      /**
       * Send a physical guest Command-Q chord with balanced key-down and key-up events.
       */
      const quitSticky = async () => {
        // Infinite Mac expects physical ADB MetaLeft + KeyQ, not a browser
        // keyboard shortcut.  Keep both keys held long enough for the guest
        // to see them; release even if the profile changes mid-sequence.
        frame.focus();
        send({ type: 'emulator_key_down', code: 'MetaLeft' });
        meta = true;
        await lagPause(LAGOMORPH_DELAYS_MS.quitModifierDown);
        if (!valid()) return false;
        send({ type: 'emulator_key_down', code: 'KeyQ' });
        q = true;
        await lagPause(LAGOMORPH_DELAYS_MS.quitKeyDown);
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
      await lagPause(LAGOMORPH_DELAYS_MS.betweenQuitAttempts);
      if (!valid()) return;
      if (!await quitSticky()) return;
      // Some System 7 Sticky Notes editions keep a note open even after a
      // Command-Q dispatch. Close the active *note window* with guest Command-W
      // as a separate fallback; this is not a host/browser shortcut. It only
      // affects the currently focused guest application, never the app shell.
      await lagPause(LAGOMORPH_DELAYS_MS.beforeCloseWindowFallback);
      if (!valid()) return;
      frame.focus();
      send({ type: 'emulator_key_down', code: 'MetaLeft' });
      meta = true;
      await lagPause(LAGOMORPH_DELAYS_MS.closeModifierDown);
      if (!valid()) return;
      send({ type: 'emulator_key_down', code: 'KeyW' });
      await lagPause(LAGOMORPH_DELAYS_MS.closeKeyDown);
      send({ type: 'emulator_key_up', code: 'KeyW' });
      send({ type: 'emulator_key_up', code: 'MetaLeft' });
      meta = false;
      // Finder can now reveal the Lagomorph 1 disk, whether the Sticky Notes
      // process quit or its obstructing note was just closed.
      // at (600,160). Opening it shows the Lagomorph 1 app at (40,142).
      // These are 640x480 guest coordinates, independent of host mouse capture.
      await lagPause(LAGOMORPH_DELAYS_MS.beforeDiskOpen);
      if (!valid()) return;
      if (!await doubleClick(
        LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.x,
        LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.y
      )) return;
      await lagPause(LAGOMORPH_DELAYS_MS.betweenDiskAndAppOpen);
      if (!valid()) return;
      if (!await doubleClick(
        LAGOMORPH_GUEST_POSITIONS.lagomorphApplication.x,
        LAGOMORPH_GUEST_POSITIONS.lagomorphApplication.y
      )) return;
    } finally {
      // Never leave a button or modifier held if the user switches machines.
      if (down) mouseUp();
      if (q) send({ type: 'emulator_key_up', code: 'KeyQ' });
      if (meta) send({ type: 'emulator_key_up', code: 'MetaLeft' });
    }
  };
  window.addEventListener('message', event => {
    if (event.origin !== 'https://infinitemac.org' || event.source !== frame.contentWindow ||
      computerView.hidden || event.data?.type !== 'emulator_loaded' ||
      selected !== 'quadra-650') return;
    const stamp = lagBootSequence;
    if (lagWelcomeTimer) clearTimeout(lagWelcomeTimer);
    // emulator_loaded means the emulator is running, not that Finder's
    // Control Strip and Sticky Notes window have both finished opening.
    lagWelcomeTimer = setTimeout(() => {
      lagWelcomeTimer = null;
      if (stamp !== lagBootSequence) return;
      const target = frame.contentWindow;
      if (target) void lagomorphBootControls(stamp, target).catch(e => {
        console.error('Lagomorph guest controls:', e);
      });
    }, LAGOMORPH_DELAYS_MS.initialGuestBoot);
  });

  /**
   * Return from an embedded computer to the terminal display and clear pending guest-startup
   * work.
   */
  const showTerminal = () => {
    if (terminalOverlay.dataset.codeWrapperHidden === 'true')
      setCodeWrapperHidden(false);
    lagBootSequence++;
    if (lagWelcomeTimer) {
      clearTimeout(lagWelcomeTimer);
      lagWelcomeTimer = null;
    }
    codeLoad++;
    codeReady = false;
    codeError.hidden = true;
    frame.src = 'about:blank';
    computerView.hidden = true;
    $('terminal-keys').hidden = false;
    $('terminal-screen').hidden = false;
    $('terminal-input').disabled = false;
  };
  /**
   * Display a selected emulator, JOSHUA, or Code-OSS profile in the existing iframe.
   */
  const showComputer = async (profile) => {
    lagBootSequence++;
    if (lagWelcomeTimer) {
      clearTimeout(lagWelcomeTimer);
      lagWelcomeTimer = null;
    }
    // An emulator lives only in the iframe. Host titlebar menus must survive
    // every VM load and remain clickable above the embedded screen.
    document.querySelectorAll('.terminal-menu[open]').forEach(menu => {
      menu.open = false;
    });
    $('terminal-screen').hidden = true;
    $('terminal-input').disabled = true;
    computerView.hidden = false;
    $('terminal-keys').hidden = true;
    codeLoad++;
    codeReady = false;
    codeLastError = '';
    codeError.hidden = true;
    if (museumKind(profile) === 'joshua') {
      // The actual standalone WarGames application, loaded from the staged
      // same-origin source, owns its game screen and keyboard in this Session.
      frame.src = '/joshua/index.html';
    } else if (museumKind(profile) === 'codeoss') {
      // A reloaded workbench starts with its outer wrapper visible again.
      if (terminalOverlay.dataset.codeWrapperHidden === 'true')
        setCodeWrapperHidden(false);
      const stamp = codeLoad;
      frame.src = codeUrl;
      void checkCodeAssets(stamp);
      setTimeout(() => {
        if (stamp === codeLoad && !codeReady)
          codeFailure(codeLastError || 'No ready signal from staged Code-OSS after 12 seconds');
      }, 12500);
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
          const media = await api('/api/lagomorph/media');
          // A read-only CD lacks a writable Desktop database and triggers Finder
          // repair errors. Infinite Mac's disk_url enables a writable overlay.
          if (media.configured && media.url) url.searchParams.set('disk_url', media.url);
          if (Array.isArray(media.floppies) && media.floppies.length === 2) {
            url.searchParams.delete('disk_url');
            media.floppies.forEach(floppy => url.searchParams.append('disk_url', floppy));
          }
          if (!media.configured || !media.url) notice(media.reason || 'Lagomorph media requires a public HTTPS URL.');
        } catch (e) {
          notice('Lagomorph CD-ROM unavailable: ' + e.message);
        }
      }
      frame.src = url.toString();
    }
  };

  // ===========================================================================
  // INDEPENDENT TERMINAL SESSION TABS
  // ===========================================================================

  let display = new VTOutput($('terminal-output'));
  const tabBar = $('terminal-tabs');
  const tabs = [];
  let activeTab = null, tabSerial = 0;
  /**
   * Create the detached <pre> element used to retain a background tab’s output.
   */
  const makeBuffer = () => {
    const container = document.createElement('div');
    const pre = document.createElement('pre');
    container.append(pre);
    return pre;
  };
  /**
   * Rebuild the tab strip with activation/close controls and its New Terminal button.
   */
  const renderTabs = () => {
    if (!tabBar) return;
    tabBar.replaceChildren();
    for (const tab of tabs) {
      const tabButton = document.createElement('button');
      tabButton.type = 'button';
      tabButton.className = 'session-tab' + (activeTab === tab ? ' active' : '');
      tabButton.textContent = tab.label;
      const tabProfile = profiles.find(profile => profile.id === tab.profileId);
      appendDecorativeIcon(tabButton, iconCategoryForProfile(tabProfile, tab.backend));
      tabButton.setAttribute('role', 'tab');
      tabButton.setAttribute('aria-selected', String(activeTab === tab));
      tabButton.addEventListener('click', () => {
        void activateTab(tab);
      });
      tabBar.append(tabButton);
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'session-tab-close';
      close.textContent = '×';
      close.title = 'Close ' + tab.label;
      close.setAttribute('aria-label', 'Close ' + tab.label);
      close.addEventListener('click', () => {
        void closeTab(tab);
      });
      tabBar.append(close);
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
    tabBar.append(create);
  };
  /**
   * Switch the visible screen and input to one tab without terminating its previous owner.
   */
  async function activateTab(tab) {
    if (!tab || !tabs.includes(tab) || activeTab === tab) return;
    // The shell, each IF interpreter and each emulator own separate tabs.
    if (activeTab?.kind === 'if' && activeTab.game) activeTab.game.draft = $('terminal-input').value;
    if (activeTab?.kind === 'discord') activeTab.draft = $('terminal-input').value;
    if (activeTab && activeTab.kind !== 'if') {
      activeTab.display.target = activeTab.buffer;
      activeTab.display.render();
    }
    activeTab = tab;
    updateCodeMenuMode();
    session = tab.session;
    selected = tab.profileId;
    display = tab.display;
    if (tab.kind === 'if') {
      showTerminal();
      applyProfile(profiles.find(p => p.id === tab.profileId) || profiles[0]);
      $('terminal-keys').hidden = false;
      $('terminal-input').value = tab.game?.draft || '';
      paintIFTab(tab);
      focusTerminal();
      renderTabs();
      saveTerminalLayout();
      return;
    }
    $('terminal-input').value = '';
    $('terminal-output').removeAttribute('data-if-echo');
    display.target = $('terminal-output');
    display.render();
    const profile = profiles.find(p => p.id === tab.profileId) || profiles[0];
    if (tab.kind === 'discord') {
      showTerminal();
      applyProfile(profile);
      $('terminal-input').disabled = false;
      $('terminal-input').value = tab.draft || '';
      $('terminal-input').setSelectionRange?.(tab.draft.length, tab.draft.length);
      $('terminal-keys').hidden = true;
      $('terminal-shell').textContent = `Discord · ${tab.guild} / #${tab.channel} · ${tab.bot}`;
      notice('Connected through the configured Discord bot · type a message, Enter to send');
      syncGameInput();
      $('terminal-input').focus();
    } else if (tab.kind === 'server') {
      showTerminal();
      applyProfile(profile);
      $('terminal-input').disabled = true;
      $('terminal-keys').hidden = true;
      $('terminal-shell').textContent = 'Server · live HTTP/Python log (read-only)';
      notice(tab.logPath ? 'Server log: ' + tab.logPath : 'Connecting to live Server log…');
    } else if (museumKind(profile) === 'terminal') {
      showTerminal();
      applyProfile(profile);
      $('terminal-input').disabled = false;
      $('terminal-keys').hidden = false;
      $('terminal-shell').textContent = tab.session
        ? (tab.kind === 'bedrock'
          ? 'Bedrock · live server console'
          : `${platform} · ${tab.label}`)
        : 'Disconnected';
      focusTerminal();
      if (tab.session) {
        const size = terminalSize();
        void api('/api/terminal/resize', { id: tab.session.id, ...size }).catch(e => notice(e.message));
      }
    } else {
      await showComputer(profile);
      updateCodeMenuMode();
      $('terminal-overlay').dataset.profile = profile.id;
      $('terminal-machine-title').textContent = profile.name.toUpperCase();
      $('terminal-shell').textContent = `${profile.year} · ${profile.name}`;
    }
    renderTabs();
    saveTerminalLayout();
  }

  /**
   * Mark a tab closed, detach it, and release only that tab’s game or PTY resources.
   */
  async function closeTab(tab) {
    if (!tabs.includes(tab) || tab.closed) return;
    // Mark closed before awaiting network I/O. A late PTY connection or IF
    // download must never resurrect a closed tab or take over another tab.
    tab.closed = true;
    tab.pollGeneration = (tab.pollGeneration || 0) + 1;
    if (tab.game) void stopGame(tab).catch(error => console.error('IF close:', error));
    const closingSession = tab.session;
    tab.session = null;
    const index = tabs.indexOf(tab);
    tabs.splice(index, 1);
    if (activeTab === tab) {
      activeTab = null;
      session = null;
      $('terminal-input').value = '';
      if (tabs.length) await activateTab(tabs[Math.max(0, index - 1)]);
      else await openSession('modern');
    } else renderTabs();
    saveTerminalLayout();
    if (closingSession) try {
      await api('/api/terminal/close', { id: closingSession.id });
    }
      catch (_) {/* already closed */ }
  }

  // ===========================================================================
  // READ-ONLY SERVER LOG TAB
  // ===========================================================================

  // Other tabs keep their own PTYs alive; a Minecraft console running in one
  // must never be closed just because another shell or emulator is selected.
  // Server is a read-only Terminal type, not a shell and not an emulator.
  // The same authenticated stream is written to the selected runtime Logs root.
  /**
   * Poll the persisted host log into a read-only Server tab until that tab closes.
   */
  async function pollServerTab(tab) {
    const stamp = ++tab.pollGeneration;
    while (!tab.closed && tab.pollGeneration === stamp) {
      try {
        const result = await api('/api/server/log?cursor=' + tab.cursor);
        if (tab.closed || tab.pollGeneration !== stamp) return;
        tab.logPath = result.log_path;
        if (result.truncated) tab.display.push('[Server] Earlier events remain in ' + tab.logPath + '\n');
        for (const line of result.lines || []) tab.display.push(line + '\n');
        tab.cursor = result.cursor;
        if (activeTab === tab) notice('Server log: ' + tab.logPath);
      } catch (error) {
        if (tab.closed || tab.pollGeneration !== stamp) return;
        tab.display.push('[Server] Log connection failed: ' + error.message + '\n');
        if (activeTab === tab) notice('Server log connection failed');
      }
      await new Promise(resolve => setTimeout(resolve, 750));
    }
  }

  /**
   * Create a read-only Server tab and begin following its log.
   */
  async function openServerTab() {
    const buffer = makeBuffer();
    const tab = {
      key: ++tabSerial,
      label: 'Server',
      profileId: 'modern',
      backend: 'server',
      kind: 'server',
      session: null,
      closed: false,
      cursor: 0,
      pollGeneration: 0,
      logPath: '',
      display: new VTOutput(buffer),
      buffer
    };
    tab.display.target = tab.buffer;
    tabs.push(tab);
    await activateTab(tab);
    tab.display.push('Connecting to the CinterForgeTerminal HTTP/Python server log…\n');
    void pollServerTab(tab);
    return tab;
  }

  // ===========================================================================
  // DISCORD: MESSAGE HISTORY, POLLING, AND DRAFTS
  // ===========================================================================

  /**
   * Format a Discord message with its author and timestamp, stripping terminal control
   * characters.
   */
  const discordMessageText = message => {
    // Discord text is untrusted. Never feed ANSI/control sequences to the VT.
    const content = String(message.content || '').replace(/[\x00-\x08\x0B-\x1F\x7F-\x9F]/g, '');
    const date = message.timestamp && !Number.isNaN(Date.parse(message.timestamp))
      ? new Date(message.timestamp).toLocaleString() + ' · ' : '';
    return `${date}${message.author}: ${content}\n`;
  };
  /**
   * Deduplicate and render fetched Discord messages in chronological order or as older history.
   */
  function showDiscordMessages(tab, messages, older = false) {
    const fresh = [];
    for (const message of messages) {
      if (!message?.id || tab.received.has(message.id)) continue;
      tab.received.add(message.id);
      fresh.push(message);
    }
    if (!fresh.length) return;
    if (older) {
      tab.oldestId = fresh[0].id;
      tab.display.prepend(fresh.map(discordMessageText).join(''));
    } else {
      if (!tab.oldestId) tab.oldestId = fresh[0].id;
      tab.newestId = fresh[fresh.length - 1].id;
      tab.display.push(fresh.map(discordMessageText).join(''));
    }
  }

  /**
   * Fetch earlier messages for this tab’s channel when the user scrolls toward the top.
   */
  async function loadOlderDiscordMessages(tab) {
    if (tab.closed || !tab.historyReady || !tab.hasOlder || !tab.oldestId || tab.loadingOlder) return;
    tab.loadingOlder = true;
    tab.followTail = false;
    const generation = tab.pollGeneration;
    try {
      const result = await api('/api/discord/messages?channel=' + encodeURIComponent(tab.channelId) +
        '&before=' + encodeURIComponent(tab.oldestId));
      if (tab.closed || tab.pollGeneration !== generation) return;
      showDiscordMessages(tab, result.messages || [], true);
      tab.hasOlder = !!result.has_more && (result.messages || []).length > 0;
    } catch (error) {
      if (activeTab === tab) notice('Discord history: ' + error.message);
    }
    finally {
      tab.loadingOlder = false;
    }
  }

  /**
   * Load the selected channel’s recent history, then poll for messages after its last seen ID.
   */
  async function pollDiscordTab(tab) {
    const generation = ++tab.pollGeneration;
    while (!tab.closed && tab.pollGeneration === generation) {
      try {
        const cursor = tab.historyReady && tab.newestId ? '&after=' + encodeURIComponent(tab.newestId) : '';
        const result = await api('/api/discord/messages?channel=' + encodeURIComponent(tab.channelId) + cursor);
        if (tab.closed || tab.pollGeneration !== generation) return;
        if (!tab.historyReady) {
          tab.historyReady = true;
          tab.hasOlder = !!result.has_more;
          tab.display.reset();
          if (!(result.messages || []).length) tab.display.push(`No messages yet in #${tab.channel}\n`);
        }
        showDiscordMessages(tab, result.messages || []);
      } catch (error) {
        if (activeTab === tab) notice('Discord history: ' + error.message);
      }
      await new Promise(resolve => setTimeout(resolve, 3000));
    }
  }

  /**
   * Open or activate a Discord channel tab; keep its draft, history cursors, and bot label
   * independent.
   */
  async function openDiscordTab(action) {
    const existing = tabs.find(tab => !tab.closed && tab.kind === 'discord' && tab.channelId === action.channel_id);
    if (existing) {
      await activateTab(existing);
      return;
    }
    const buffer = makeBuffer();
    const tab = {
      key: ++tabSerial,
      label: '#' + action.channel,
      profileId: 'modern',
      backend: 'discord',
      kind: 'discord',
      guild: action.guild,
      channel: action.channel,
      channelId: action.channel_id,
      bot: action.bot,
      session: null,
      closed: false,
      pollGeneration: 0,
      received: new Set(),
      draft: '',
      oldestId: null,
      newestId: null,
      historyReady: false,
      hasOlder: false,
      loadingOlder: false,
      followTail: true,
      display: new VTOutput(buffer),
      buffer
    };
    tab.display.target = tab.buffer;
    tabs.push(tab);
    await activateTab(tab);
    tab.display.push(`Loading recent history from ${tab.guild} / #${tab.channel}…\n`);
    void pollDiscordTab(tab);
  }
  setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const ids = tabs.filter(tab => tab.session?.id && !tab.closed).map(tab => tab.session.id);
      if (!ids.length) return;
      const action = await api('/api/discord/next', { session_ids: ids });
      if (action.channel_id) await openDiscordTab(action);
    } catch (error) {/* Discord is optional; keep normal shell and games available. */ }
  }, 1100);
  /**
   * Handle New Terminal requests from the native menus, including explicit backend/profile
   * modes.
   */
  window.CinterTerminalNewTab = async (kind = 'mac') => {
    // The same menu always has the same label. Its destination depends on mode.
    // In Code: create a VS Code integrated terminal tab. Else: outer Session tab.
    if (codeIsActive() && !String(kind).startsWith('profile:')) {
      return sendCodeCommand('new-terminal', {backend: kind === 'code' ? 'native' : kind});
    }

    if (typeof kind === 'string' && kind.startsWith('profile:')) {
      const id = kind.slice(8);
      if (profiles.some(profile => profile.id === id && !profile.lagomorph))
        return openSession(id);
      notice('Unknown museum machine');
      return;
    }
    if (kind === 'code') return openSession('codeoss');
    if (kind === 'server') return openServerTab();
    if (kind === 'mac') return openSession('modern');
    if (kind === 'linux') return openSession('vt100-terminal', 'linux');
    if (kind === 'win') return openSession('ibm-pc-vga', 'win');
    if (kind === 'minecraft') return openSession('modern', 'minecraft');
    notice('Unknown Terminal mode: ' + String(kind));
  };
  // Full development access to another Terminal host uses its existing HTTP
  // UI, PTY and native bridge. A native connection opens a separate app window.

  // ===========================================================================
  // LAN DISCOVERY AND REMOTE-WINDOW CONNECTIONS
  // ===========================================================================

  /**
   * Open any discovered CinterForge HTTP app at its advertised LAN IP and port.
   * Another Terminal opens its real remote PTY; another product opens its web UI.
   */
  const connectToLAN = async url => {
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
      $('lan-menu').open = false;
    } catch (error) {
      notice('LAN connection: ' + error.message);
    }
  };
  /**
   * Refresh the local HTTP endpoint and all advertised CinterForge LAN services.
   */
  const refreshLAN = async () => {
    const container = $('lan-menu-list');
    if (!container) return;
    const [info, found] = await Promise.all([api('/api/lan/info'), api('/api/lan/peers')]);
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
        void connectToLAN(peer.url);
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
      void connectToLAN('http://' + destination);
    });
    void nativeEvent('lan-peers', { peers });
  };
  /**
   * Toggle the LAN discovery popup, refreshing hosts when it opens.
   */
  window.CinterShowLAN = () => {
    const menu = $('lan-menu');
    if (menu) {
      if (menu.open) void refreshLAN().catch(error => notice('LAN: ' + error.message)); else menu.open = true;
    }
  };
  $('lan-menu')?.addEventListener('toggle', event => {
    if (event.target.open) void refreshLAN().catch(error => notice('LAN: ' + error.message));
  });

  // ===========================================================================
  // INTERACTIVE FICTION: TAB OWNERSHIP, INPUT, AND SAVES
  // ===========================================================================

  // Each IF story is a separate Session tab. The interpreter writes only to
  // its own detached output, and receives input only through its own private
  // textarea. The visible Terminal output and textarea are a presentation of
  // the ACTIVE tab, never a global owner of a Z-machine process.
  // Code-OSS's Z-Machine PTY is a view/controller for the SAME browser VM
  // that owns an ordinary IF Session tab. It does not create an interpreter,
  // invent game commands or bypass the existing CONV/Quetzal save path.
  const codeZMachineChannel = typeof BroadcastChannel === 'function'
    ? new BroadcastChannel('cinterforge-terminal-zmachine') : null;
  const codeZMachineSubscribers = new Map();
  function codeZMachineStory() {
    const running = tabs.filter(tab => !tab.closed && tab.kind === 'if' && tab.game);
    if (activeTab?.kind === 'if' && activeTab.game) return activeTab;
    return running.length === 1 ? running[0] : null;
  }
  function updateCodeZMachineSubscriber(id, subscriber) {
    if (!codeZMachineChannel) return;
    const story = codeZMachineStory();
    if (!story) {
      if (!subscriber.noStory) {
        codeZMachineChannel.postMessage({type: 'output', id,
          text: '[IF] No uniquely selected running story. Start an installed story using its existing Terminal command.\n'});
        subscriber.noStory = true;
      }
      return;
    }
    subscriber.noStory = false;
    const transcript = String(story.buffer.textContent || '');
    if (subscriber.storyKey !== story.key) {
      subscriber.storyKey = story.key;
      subscriber.transcript = '';
      codeZMachineChannel.postMessage({type: 'output', id,
        text: '[IF] ' + story.label + '\n'});
    }
    const previous = subscriber.transcript || '';
    const delta = transcript.startsWith(previous) ? transcript.slice(previous.length) : transcript;
    subscriber.transcript = transcript;
    if (delta) codeZMachineChannel.postMessage({type: 'output', id, text: delta});
  }
  function updateCodeZMachineSubscribers() {
    for (const [id, subscriber] of codeZMachineSubscribers)
      updateCodeZMachineSubscriber(id, subscriber);
  }
  if (codeZMachineChannel) codeZMachineChannel.onmessage = event => {
    const request = event.data || {}, id = String(request.id || '');
    if (!/^zmachine-[a-z0-9]{4,24}$/.test(id)) return;
    if (request.type === 'subscribe') {
      if (!codeZMachineSubscribers.has(id) && codeZMachineSubscribers.size >= 8) return;
      const subscriber = {storyKey: null, transcript: '', noStory: false};
      codeZMachineSubscribers.set(id, subscriber);
      updateCodeZMachineSubscriber(id, subscriber);
    } else if (request.type === 'unsubscribe') {
      codeZMachineSubscribers.delete(id);
    } else if (request.type === 'input') {
      const subscriber = codeZMachineSubscribers.get(id);
      if (!subscriber || typeof request.text !== 'string' || request.text.length > 4096) return;
      const selectedStory = codeZMachineStory();
      const tab = selectedStory?.key === subscriber.storyKey ? selectedStory : null;
      const current = tab?.game;
      if (!current?.io?.lineResolver) {
        codeZMachineChannel.postMessage({type: 'output', id, text: '[IF] No input requested by the active story.\n'});
        return;
      }
      // Exactly the same WebIOAdapter event used by the normal IF Session.
      current.inputElement.value = request.text;
      current.inputElement.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'Enter', bubbles: false}));
    }
  };
  const terminalScreen = $('terminal-screen');
  /**
   * Scroll the currently displayed Terminal view to its bottom.
   */
  const scrollToLatest = () => {
    terminalScreen.scrollTop = terminalScreen.scrollHeight;
  };
  const outputObserver = new MutationObserver(() => {
    if (activeTab?.kind !== 'discord' || (activeTab.followTail && !activeTab.loadingOlder)) scrollToLatest();
  });
  outputObserver.observe($('terminal-output'), { subtree: true, childList: true, characterData: true });
  terminalScreen.addEventListener('scroll', () => {
    const tab = activeTab;
    if (tab?.kind !== 'discord' || tab.loadingOlder) return;
    tab.followTail = terminalScreen.scrollHeight - terminalScreen.scrollTop - terminalScreen.clientHeight < 40;
    if (tab.historyReady && tab.hasOlder && tab.oldestId && terminalScreen.scrollTop < 80 &&
      terminalScreen.scrollHeight > terminalScreen.clientHeight)
      void loadOlderDiscordMessages(tab);
  });

  /**
   * Return the interpreter owned by the active IF tab, if any.
   */
  const activeGame = () => activeTab?.game || null;
  /**
   * Draw the exact editable Discord textarea contents and caret as visible output nodes.
   */
  function paintDiscordDraft(tab) {
    if (!tab || tab.closed || activeTab !== tab) return;
    const output = $('terminal-output'), input = $('terminal-input');
    // The textarea remains the only source of truth for editing and sending.
    // Drawing actual text nodes (not CSS attr()) keeps the display in sync
    // when history refreshes or a native WebView repaints during Backspace.
    if (!tab.visibleDraft) {
      tab.visibleDraft = document.createElement('span');
      tab.visibleDraft.className = 'discord-input-draft';
      output.append(tab.visibleDraft);
    }
    const value = input.value;
    const caret = Math.max(0, Math.min(input.selectionStart ?? value.length, value.length));
    tab.visibleDraft.replaceChildren(document.createTextNode(value.slice(0, caret)),
      document.createTextNode('▌'), document.createTextNode(value.slice(caret)));
  }

  /**
   * Keep the visible draft synchronized with the active Discord or IF input textarea.
   */
  function syncGameInput() {
    const output = $('terminal-output'), input = $('terminal-input'), current = activeGame();
    const discord = activeTab?.kind === 'discord';
    if (!discord && !current?.io?.lineResolver) {
      output.removeAttribute('data-if-echo');
      return;
    }
    if (discord) {
      activeTab.draft = input.value;
      output.removeAttribute('data-if-echo');
      paintDiscordDraft(activeTab);
    } else {
      current.draft = input.value;
      const value = input.value;
      const caret = Math.max(0, Math.min(input.selectionStart ?? value.length, value.length));
      output.setAttribute('data-if-echo', value.slice(0, caret) + '▌' + value.slice(caret));
    }
    scrollToLatest();
  }

  /**
   * Present the active story’s buffered output, status, input state, and draft.
   */
  function paintIFTab(tab) {
    if (tab.closed || activeTab !== tab) return;
    const target = $('terminal-output');
    if (tab.buffer.childNodes && typeof tab.buffer.cloneNode === 'function')
      target.replaceChildren(...Array.from(tab.buffer.childNodes, node => node.cloneNode(true)));
    else target.textContent = tab.buffer.textContent;
    const current = tab.game;
    $('terminal-input').disabled = !current?.io;
    $('terminal-shell').textContent = 'Interactive Fiction · ' + tab.label;
    notice(current?.statusElement.textContent || tab.statusText || ('Playing ' + tab.label));
    syncGameInput();
    scrollToLatest();
  }

  /**
   * Coalesce repeated interpreter DOM changes into one animation-frame repaint.
   */
  function scheduleIFPaint(tab) {
    if (tab.paintPending || tab.closed) return;
    tab.paintPending = true;
    requestAnimationFrame(() => {
      tab.paintPending = false;
      paintIFTab(tab);
      updateCodeZMachineSubscribers();
    });
  }

  /**
   * Abort and release one IF interpreter, optionally notifying the server of its launch ID.
   */
  async function stopGame(tab, notifyServer = true) {
    const current = tab?.game;
    if (!current) return;
    tab.game = null;
    current.abort.abort();
    current.releaseDisplay?.();
    current.observer?.disconnect();
    if (current.io?.lineResolver) {
      try {
        current.io.lineResolver('quit');
      } catch (_) {/* already exited */ }
    }
    tab.finished = true;
    updateCodeZMachineSubscribers();
    tab.statusText = tab.label + ' stopped · close this tab to return to the shell';
    tab.buffer.append(document.createTextNode('\n[IF] ' + tab.label + ' stopped\n'));
    if (activeTab === tab) {
      $('terminal-input').value = '';
      paintIFTab(tab);
      $('terminal-output').removeAttribute('data-if-echo');
    }
    if (notifyServer) {
      try {
        await api('/api/if/stop', { command: current.name, launch_id: current.launchId });
      }
      catch (error) {
        if (activeTab === tab) notice('IF stop: ' + error.message);
      }
    }
  }

  /**
   * Allocate an independent story tab and start its Z-machine without stealing unrelated shell
   * focus.
   */
  async function openIFTab(name, launchId, sourceSession) {
    // A story can be requested twice while already open. Re-select its tab;
    // never bind a second VM to the same named autosave or another shell.
    const existing = tabs.find(tab => !tab.closed && tab.kind === 'if' && tab.game?.name === name);
    if (existing) {
      await activateTab(existing);
      return existing;
    }
    const buffer = makeBuffer();
    const tab = {
      key: ++tabSerial,
      label: name,
      profileId: 'modern',
      backend: 'if',
      kind: 'if',
      session: null,
      closed: false,
      display: new VTOutput(buffer),
      buffer,
      statusText: 'Loading ' + name + '…',
      paintPending: false,
      game: null
    };
    const current = {
      name,
      launchId,
      sourceSession,
      io: null,
      draft: '',
      abort: new AbortController(),
      inputElement: document.createElement('textarea'),
      statusElement: document.createElement('span'),
      releaseDisplay: null,
      observer: null
    };
    tab.game = current;
    tab.buffer.textContent = '[IF] Loading ' + name + '…\n';
    current.observer = new MutationObserver(() => scheduleIFPaint(tab));
    current.observer.observe(tab.buffer, { subtree: true, childList: true, characterData: true });
    // If the user already switched away from the requesting shell, do not
    // steal focus when the deferred server event finally arrives.
    const select = !sourceSession || activeTab?.session?.id === sourceSession;
    tabs.push(tab);
    updateCodeZMachineSubscribers();
    if (select) await activateTab(tab);
    else renderTabs();
    void startGame(tab, current);
    return tab;
  }

  /**
   * Fetch, restore, run, and autosave one IF story using its dedicated interpreter and Quetzal
   * state.
   */
  async function startGame(tab, current) {
    const name = current.name;
    /**
     * Check that an asynchronous IF operation still belongs to the open tab and current
     * interpreter.
     */
    const alive = () => !tab.closed && tab.game === current;
    const output = tab.buffer;
    const saveURL = '/api/if/save/' + encodeURIComponent(name);
    try {
      const response = await fetch('/api/if/story/' + encodeURIComponent(name),
        { headers: { 'X-CPH-Token': token }, cache: 'no-store', signal: current.abort.signal });
      if (!alive()) return;
      if (!response.ok) throw new Error((await response.json()).error || 'Story download failed');
      const story = await response.arrayBuffer();
      const [{ ZMachine }, { WebIOAdapter }, quetzal] = await Promise.all([
        import('/zmachine/index.js'), import('/zmachine/web/WebIOAdapter.js'),
        import('/zmachine/core/state/Quetzal.js')]);
      if (!alive()) return;
      const auth = { 'X-CPH-Token': token };
      // Save storage is optional, and a bad save must never strand the tab.
      const storyHash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', story)),
        byte => byte.toString(16).padStart(2, '0')).join('');
      if (!alive()) return;
      let old = { found: false, story_sha256: storyHash };
      let saveWarning = '';
      try {
        const oldResponse = await fetch(saveURL, { headers: auth, cache: 'no-store', signal: current.abort.signal });
        if (!alive()) return;
        if (!oldResponse.ok) {
          const message = await oldResponse.json().catch(() => ({}));
          throw new Error(message.error || 'HTTP ' + oldResponse.status);
        }
        old = await oldResponse.json();
        if (old.story_sha256 !== storyHash) throw new Error('Story hash mismatch');
      } catch (error) {
        if (!alive()) return;
        saveWarning = '[IF] Existing autosave unavailable: ' + error.message + '; starting fresh\n';
      }
      if (!alive()) return;
      const vmState = old.found ? old.save.vm_state : null;
      /**
       * Encode Quetzal binary snapshot bytes for JSON storage in the host service.
       */
      const base64 = bytes => {
        let result = '';
        for (let i = 0; i < bytes.length; i += 8192)
          result += String.fromCharCode(...bytes.subarray(i, i + 8192));
        return btoa(result);
      };
      /**
       * Validate and decode bounded base64 snapshot data before restoring a save.
       */
      const unbase64 = value => {
        if (typeof value !== 'string' || value.length > 12 * 1024 * 1024 ||
          !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new Error('Invalid Quetzal bytes');
        return Uint8Array.from(atob(value), c => c.charCodeAt(0));
      };
      const io = new WebIOAdapter({        
outputElement: output, inputElement: current.inputElement,
        statusElement: current.statusElement      
});
      current.io = io;
      let vm = ZMachine.load(story, io);
      io.initialize?.(vm.version);
      if (saveWarning) output.textContent += saveWarning;
      if (vmState) {
        try {
          if (vmState.schema !== 'cinterforge.if.quetzal.v1') throw new Error('Unknown Quetzal save');
          const parsed = quetzal.parseQuetzalSave(unbase64(vmState.data_base64));
          if (!quetzal.verifySaveCompatibility(parsed, vm.memory))
            throw new Error('Save does not match the installed story');
          const dynamicSize = Math.min(parsed.dynamicMemory.length, vm.memory.staticBase);
          for (let i = 0; i < dynamicSize; i++)vm.memory.writeByte(i, parsed.dynamicMemory[i]);
          vm.stack.restore(parsed.callStack);
          vm._pc = parsed.gameId.pc;
          vm.executor.randomMode = String(vmState.random_mode || 'random');
          vm.executor.randomConfiguredSeed = vmState.random_configured_seed === null ? null :
            Number(vmState.random_configured_seed);
          vm.executor.randomXorshiftState = Number(vmState.random_xorshift_state || 1) >>> 0;
          output.textContent += '[IF] Restored ' + name + ' autosave\n';
        } catch (error) {
          output.textContent += '[IF] Restore failed: ' + error.message + '; starting fresh\n';
          vm = ZMachine.load(story, io);
          io.initialize?.(vm.version);
          output.textContent += '[IF] Restore unavailable; started new game\n';
        }
      }
      let boundary = 0;
      let saving = Promise.resolve();
      const readLine = io.readLine.bind(io);
      io.readLine = (...args) => {
        const pending = readLine(...args);
        const serial = ++boundary;
        queueMicrotask(() => {
          saving = saving.then(async () => {
            if (!alive() || !io.lineResolver || serial !== boundary) return;
            const bytes = quetzal.createQuetzalSave(vm.memory, vm.stack.snapshot(), vm.pc);
            const random = vm.executor;
            const snapshot = {
              schema: 'cinterforge.if.quetzal.v1',
              data_base64: base64(bytes),
              random_mode: String(random.randomMode || 'random'),
              random_configured_seed: random.randomConfiguredSeed,
              random_xorshift_state: random.randomXorshiftState >>> 0,
              native_save_base64: null
            };
            const card = {
              schema: 'cinterforge.conv.if.v1',
              card_type: 'CONV',
              story: name,
              interpreter: 'zmachine',
              story_sha256: old.story_sha256,
              vm_state: snapshot,
              session_state: {},
              updated_at: new Date().toISOString()
            };
            const result = await fetch(saveURL, {              
method: 'POST', headers: {                
...auth,
                'Content-Type': 'application/json'              
}, body: JSON.stringify(card)            
});
            if (!result.ok) throw new Error('HTTP ' + result.status);
          }).catch(error => {
            if (alive()) {
              output.textContent += '\n[IF] Autosave failed: ' + error.message + '\n';
              scheduleIFPaint(tab);
            }
          });
        });
        return pending;
      };
      // Preserve the original atomic initial/restored paint behavior.
      output.style.visibility = 'hidden';
      /**
       * Reveal the initialized IF output once the first input boundary has been reached.
       */
      const releaseDisplay = () => {
        if (current.releaseDisplay !== releaseDisplay) return;
        current.releaseDisplay = null;
        output.style.visibility = '';
        scheduleIFPaint(tab);
      };
      current.releaseDisplay = releaseDisplay;
      const readyReadLine = io.readLine.bind(io);
      io.readLine = (...args) => {
        const pending = readyReadLine(...args);
        if (alive()) {
          current.statusElement.textContent = 'Playing ' + name + ' · Ctrl+C to exit';
          if (activeTab === tab) {
            $('terminal-input').disabled = false;
            focusTerminal();
            syncGameInput();
          }
        }
        requestAnimationFrame(releaseDisplay);
        return pending;
      };
      if (!alive()) return;
      try {
        await vm.run();
      } finally {
        releaseDisplay();
      }
    } catch (error) {
      if (alive() && error.name !== 'AbortError') {
        output.textContent += '\n[IF] ' + error.message + '\n';
        scheduleIFPaint(tab);
      }
    } finally {
      if (alive()) await stopGame(tab);
    }
  }


  // ===========================================================================
  // HOST TERMINAL PROFILE, SESSIONS, AND PTY OUTPUT
  // ===========================================================================

  /**
   * Show a concise non-modal status message below the Terminal UI.
   */
  const notice = text => {
    $('terminal-message').textContent = text;
  };
  /**
   * Apply a computer profile’s presentation colors, geometry, and persisted selection.
   */
  const applyProfile = profile => {
    const style = $('terminal-overlay').style;
    style.setProperty('--bg', profile.background);
    style.setProperty('--fg', profile.foreground);
    style.setProperty('--accent', profile.accent);
    style.setProperty('--columns', String(profile.columns || 80));
    $('terminal-overlay').dataset.profile = profile.id;
    $('terminal-machine-title').textContent = profile.name.toUpperCase();
    if (!acceptUI) localStorage.setItem('cinterterminal.profile.' + platform, profile.id);
  };
  /**
   * Estimate terminal columns and rows from the current on-screen dimensions.
   */
  const terminalSize = () => {
    const el = $('terminal-screen');
    return {
      cols: Math.max(40, Math.floor(el.clientWidth / 8.5)),
      rows: Math.max(12, Math.floor(el.clientHeight / 19))
    };
  };
  /**
   * Poll one PTY or Bedrock console, rendering output until its backend session ends.
   */
  async function pollTab(tab) {
    const id = tab.session?.id;
    while (id && !tab.closed && tab.session?.id === id) {
      try {
        const result = await api('/api/terminal/poll', { id, wait_ms: 300 });
        if (tab.closed || tab.session?.id !== id) return;
        if (result.output) {
          const bytes = decode(result.output);
          tab.display.push(bytes);
        }
        if (result.bedrock_eula_requested && activeTab === tab) requestLicense();
        if (result.closed) {
          // Release the host-side PTY slot after shutdown, while preserving
          // this tab's transcript for the player to inspect.
          if (tab.kind === 'bedrock') void api('/api/terminal/close', { id }).catch(() => { });
          if (activeTab === tab) notice(tab.kind === 'bedrock' ? 'Bedrock server exited' : 'Shell exited');
          tab.session = null;
          if (activeTab === tab) session = null;
          renderTabs();
          return;
        }
      } catch (e) {
        if (tab.closed || tab.session?.id !== id) return;
        if (activeTab === tab) notice(e.message);
        tab.session = null;
        if (activeTab === tab) session = null;
        renderTabs();
        return;
      }
    }
  }

  /**
   * Create a new independent tab and open its selected shell, emulator, or Bedrock backend.
   */
  async function openSession(profileId, backend = 'native') {
    // Do not drop a native/File-menu request while the initial PTY is still
    // connecting. Windows WebView2 can deliver the click during first startup.
    if (waiting) {
      return new Promise((resolve, reject) => queuedSessionOpens.push({ profileId, backend, resolve, reject }));
    }
    if (backend === 'minecraft') {
      // Menu and API both gate the attachment: never start a Bedrock VM here.
      try {
        const state = await api('/api/bedrock/status');
        if (!(state.running || state.starting)) {
          notice('Minecraft is disabled until bedrock run starts a server');
          return;
        }
      } catch (error) {
        notice('Minecraft unavailable: ' + error.message);
        return;
      }
    }
    waiting = true;
    const profile = profiles.find(p => p.id === profileId) || profiles[0];
    if (!profile) {
      waiting = false;
      notice('No terminal profiles available');
      return;
    }
    const buffer = makeBuffer();
    const tab = {
      key: ++tabSerial,
      label: backend === 'minecraft' ? 'Minecraft' :
        backend === 'linux' ? 'Linux Bash' : backend === 'win' ? 'Windows Bash' :
          museumKind(profile) === 'joshua' ? 'JOSHUA' : profile.name,
      profileId: profile.id,
      backend,
      kind: museumKind(profile),
      session: null,
      closed: false,
      display: new VTOutput(buffer),
      buffer
    };
    // Every tab owns its renderer and shell, but only the active tab renders
    // into the shared Terminal screen and receives keyboard input.
    tab.display.target = tab.buffer;
    tabs.push(tab);
    if (activeTab?.kind === 'if' && activeTab.game) activeTab.game.draft = $('terminal-input').value;
    if (activeTab?.kind === 'discord') activeTab.draft = $('terminal-input').value;
    if (activeTab && activeTab.kind !== 'if') {
      activeTab.display.target = activeTab.buffer;
      activeTab.display.render();
    }
    activeTab = tab;
    updateCodeMenuMode();
    session = null;
    selected = profile.id;
    display = tab.display;
    $('terminal-input').value = '';
    $('terminal-output').removeAttribute('data-if-echo');
    display.target = $('terminal-output');
    display.render();
    renderTabs();
    try {
      if (!acceptUI && !profile.lagomorph && !profile.macGame && tab.kind === 'terminal')
        if (!acceptUI) localStorage.setItem('cinterterminal.profile.' + platform, profile.id);
      if (tab.kind !== 'terminal') {
        notice('Opening ' + profile.name + ' emulator…');
        await showComputer(profile);
        updateCodeMenuMode();
        $('terminal-overlay').dataset.profile = profile.id;
        $('terminal-machine-title').textContent = profile.name.toUpperCase();
        $('terminal-shell').textContent = `${profile.year} · ${profile.name}`;
        await nativeEvent('machine', { id: profile.id, kind: tab.kind });
        return;
      }
      showTerminal();
      applyProfile(profile);
      notice('Connecting to ' + profile.name + ' session…');
      const created = await api('/api/terminal/open', { ...terminalSize(), profile: profile.id, backend });
      if (tab.closed) {
        await api('/api/terminal/close', { id: created.id }).catch(() => { });
        return;
      }
      tab.session = created;
      if (activeTab === tab) {
        session = created;
        $('terminal-shell').textContent = `${created.platform} · ${created.shell}`;
        notice('Connected · type help for CinterForgeTerminal commands');
        $('terminal-input').focus();
      }
      await nativeEvent('terminal', { id: profile.id, kind: 'terminal' });
      void pollTab(tab);
    } catch (e) {
      if (activeTab === tab) {
        notice(e.message);
        tab.display.push(e.message + '\n');
      }
      await nativeEvent('error', { message: e.message });
    } finally {
      waiting = false;
      renderTabs();
      const next = queuedSessionOpens.shift();
      if (next) void openSession(next.profileId, next.backend).then(next.resolve, next.reject);
    }
  }
  // A `bedrock run <world>` command in any regular shell asks the local
  // host to create a separate PTY. Adopt that exact Session, never start a
  // second shell and never recycle the initiating tab.
  /**
   * Attach the existing host-created Bedrock PTY to its own tab, without creating another
   * session.
   */
  async function adoptBedrockSession(created) {
    if (!created?.id || tabs.some(tab => tab.session?.id === created.id)) {
      const existing = tabs.find(tab => tab.session?.id === created?.id);
      if (existing) await activateTab(existing);
      return;
    }
    const buffer = makeBuffer();
    const tab = {
      key: ++tabSerial,
      label: 'Bedrock',
      profileId: 'modern',
      kind: 'bedrock',
      session: created,
      closed: false,
      display: new VTOutput(buffer),
      buffer
    };
    tab.display.target = tab.buffer;
    tabs.push(tab);
    await activateTab(tab);
    $('terminal-shell').textContent = 'Bedrock · live server console';
    notice('Bedrock has its own live two-way terminal; the previous session is unchanged');
    void pollTab(tab);
  }
  let adoptingBedrock = false;
  setInterval(async () => {
    if (adoptingBedrock || document.visibilityState === 'hidden') return;
    adoptingBedrock = true;
    try {
      const created = await api('/api/bedrock/terminal/next');
      if (created?.id) await adoptBedrockSession(created);
    } catch (_) { /* Browser or host may be shutting down. */ }
    finally {
      adoptingBedrock = false;
    }
  }, 500);
  /**
   * Reconnect or refresh the current tab using the behavior appropriate to its kind.
   */
  async function reconnectTab() {
    if (!activeTab) {
      await openSession('modern');
      return;
    }
    const tab = activeTab;
    if (tab.kind === 'server') {
      tab.pollGeneration++;
      tab.cursor = 0;
      tab.display.reset();
      void pollServerTab(tab);
      return;
    }
    if (tab.kind === 'bedrock') {
      notice('Bedrock is a dedicated server Session. Start a new world with bedrock run from a regular terminal.');
      return;
    }
    if (tab.kind === 'if') {
      notice(tab.game ? 'Story is running in its own tab' : 'Story ended. Launch it again from a shell.');
      return;
    }
    if (tab.kind !== 'terminal') {
      await showComputer(profiles.find(p => p.id === tab.profileId));
      return;
    }
    if (tab.session) try {
      await api('/api/terminal/close', { id: tab.session.id });
    } catch (_) {/* closed */ }
    tab.session = null;
    session = null;
    tab.display.reset();
    try {
      const created = await api('/api/terminal/open', {
        ...terminalSize(),
        profile: tab.profileId,
        backend: tab.backend || 'native'
      });
      tab.session = created;
      session = created;
      $('terminal-shell').textContent = `${created.platform} · ${created.shell}`;
      notice('Connected');
      void pollTab(tab);
      focusTerminal();
    } catch (error) {
      notice(error.message);
    }
  }

  /**
   * Forward raw shell control/text input to the active host PTY only (never Discord or IF).
   */
  async function send(data) {
    if (activeTab?.kind === 'if' || activeTab?.kind === 'discord') return;
    if (session) try {
      await api('/api/terminal/input', { id: session.id, data });
    } catch (e) {
      notice(e.message);
    }
  }

  /**
   * Ask the native host to enable/open its platform-appropriate JavaScript inspector.
   */
  async function debuggerRequest() {
    try {
      await api('/api/debugger', {});
      notice('Inspector requested; available when the native Host enables it.');
    }
    catch (e) {
      notice(e.message);
    }
  }

  // ===========================================================================
  // INPUT FOCUS, CLIPBOARD, AND KEY TRANSLATION
  // ===========================================================================

  const overlay = $('terminal-overlay');
  const input = $('terminal-input');
  /**
   * Focus the shared hidden textarea when a terminal-style tab can accept input.
   */
  const focusTerminal = () => {
    if ((session || activeGame() || activeTab?.kind === 'discord') && !overlay.hidden && !input.disabled) input.focus();
  };
  // Native Edit uses the existing session transcript and input bridge.
  // Copy uses the entire committed session
  // transcript, never the hidden input textarea or an emulator's document.
  /**
   * Get the active tab’s committed transcript, including IF output when applicable.
   */
  const currentTranscript = () => activeTab?.kind === 'if'
    ? (activeTab.buffer.textContent || '')
    : display.transcript();
  /**
   * Copy committed terminal output with a native/WebView-compatible clipboard fallback.
   */
  const copyTranscript = async () => {
    const transcript = currentTranscript();
    // In WebView2, execCommand works synchronously during a real keyboard
    // gesture even when navigator.clipboard permission is unavailable.
    /**
     * Use a temporary off-screen textarea for synchronous clipboard copy in constrained WebViews.
     */
    const legacyCopy = () => {
      const field = document.createElement('textarea');
      field.value = transcript;
      field.setAttribute('readonly', '');
      field.style.cssText = 'position:fixed;left:-10000px;top:0';
      document.body.append(field);
      field.select();
      try {
        return document.execCommand('copy');
      }
      finally {
        field.remove();
      }
    };
    try {
      if (platform === 'mac' && typeof window.cinterClipboard === 'function') {
        if (await window.cinterClipboard({action: 'copy', text: transcript}) !== true)
          throw new Error('Native pasteboard rejected Copy');
        notice('Copied terminal transcript');
        return transcript;
      }
      if (platform === 'win' && legacyCopy()) {
        notice('Copied terminal transcript');
        return transcript;
      }
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(transcript);
    } catch (error) {
      // Retain the existing WKWebView/WebKitGTK fallback.
      if (!legacyCopy()) {
        notice('Copy unavailable: ' + error.message);
        return false;
      }
    }
    notice('Copied terminal transcript');
    return transcript;
  };
  /**
   * Insert clipboard text into the active Discord/IF draft or send it to the active shell.
   */
  const pasteIntoSession = text => {
    if ((!session && !activeGame() && activeTab?.kind !== 'discord') || computerView.hidden === false) return false;
    text = String(text ?? '');
    if (!text) return false;
    if (activeGame() || activeTab?.kind === 'discord') {
      input.focus();
      input.setRangeText(text, input.selectionStart, input.selectionEnd, 'end');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      void send(text);
      focusTerminal();
    }
    return true;
  };
  /**
   * Read clipboard text through the native Windows bridge or browser API, then paste it into the
   * active tab.
   */
  const pasteClipboard = async () => {
    try {
      // WebView2 does not consistently grant navigator.clipboard.readText
      // to the application's loopback page. The native Windows clipboard
      // bridge is read-only and remains local to the user's installed app.
      if ((platform === 'win' || platform === 'mac') &&
          typeof window.cinterClipboard === 'function')
        return pasteIntoSession(await window.cinterClipboard({ action: 'paste' }));
      if (navigator.clipboard?.readText)
        return pasteIntoSession(await navigator.clipboard.readText());
      notice('Paste from your keyboard with Ctrl+V');
      return false;
    } catch (error) {
      notice('Paste unavailable: ' + error.message);
      return false;
    }
  };
  /** Native Edit menu hooks: transcript copy, paste, and direct draft insertion. */
  window.CinterEdit = Object.freeze({
    copy: copyTranscript,
    paste: pasteClipboard,
    insert: pasteIntoSession,
    transcript: currentTranscript
  });

  // macOS's native menu handles Command-C/V when WebKit gives AppKit the
  // accelerators. This capture path also handles WKWebView input focus states
  // that consume the keys before AppKit can dispatch a menu action. Do not
  // intercept Code-OSS: its editor iframe owns its own clipboard shortcuts.
  document.addEventListener('keydown', event => {
    if (platform !== 'mac' || !event.metaKey || event.ctrlKey || event.altKey ||
        event.shiftKey || event.isComposing || !computerView.hidden) return;
    const key = event.key.toLowerCase();
    if (key !== 'c' && key !== 'v') return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (key === 'c') void copyTranscript();
    else void pasteClipboard();
  }, true);

  // Windows keeps Ctrl+C for interrupting the running Bash process. Use
  // Ctrl+Shift+C for Copy, and Ctrl+V / Ctrl+Shift+V for Paste. Capture before
  // the PTY control-key handler so these combinations never send ^C or ^V.
  document.addEventListener('keydown', event => {
    if (platform !== 'win' || !event.ctrlKey || event.metaKey || event.altKey ||
      event.isComposing || computerView.hidden === false) return;
    const key = event.key.toLowerCase();
    if (key === 'c' && event.shiftKey) {
      event.preventDefault();
      event.stopImmediatePropagation();
      void copyTranscript();
    } else if (key === 'v' && (typeof window.cinterClipboard === 'function')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      void pasteClipboard();
    }
    // Browser previews without the native bridge retain Ctrl+V's default
    // textarea paste event. The input listener sends the pasted text once.
  }, true);

  // All platforms receive both mouse and keyboard input inside the WebView.
  // The machine dropdown remains available even when an emulator owns focus.
  const keys = {
    Enter: '\r',
    Escape: '\x1b',
    Backspace: '\x7f',
    Tab: '\t',
    ArrowUp: '\u001b[A',
    ArrowDown: '\u001b[B',
    ArrowRight: '\u001b[C',
    ArrowLeft: '\u001b[D',
    Home: '\u001b[H',
    End: '\u001b[F',
    Delete: '\u001b[3~',
    ControlC: '\x03'
  };
  /**
   * Translate an on-screen keyboard key into the active IF action or PTY control sequence.
   */
  const forwardKey = (name) => {
    if (activeGame()) {
      if (name === 'ControlC') {
        void closeTab(activeTab);
        return true;
      }
      if (name === 'Enter') {
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        return true;
      }
      return false;
    }
    const code = keys[name];
    if (code && session) {
      send(code);
      return true;
    }
    return false;
  };
  for (const button of ($('terminal-keys').querySelectorAll?.('button') || [])) {
    button.addEventListener('click', () => {
      forwardKey(button.dataset.terminalKey);
      focusTerminal();
    });
  }
  $('terminal-reconnect').addEventListener('click', () => {
    void reconnectTab();
  });
  input.addEventListener('wheel', e => {
    $('terminal-screen').scrollTop += e.deltaY;
    e.preventDefault();
  }, { passive: false });
  // Keyboard shortcuts work if the screen (rather than the textarea) holds focus.
  document.addEventListener('keydown', e => {
    if (!session || activeTab?.kind === 'if' || computerView.hidden === false ||
        e.isComposing || e.target === input) return;
    if (e.target && ['SELECT', 'BUTTON', 'SUMMARY', 'INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;
    const ctrl = e.ctrlKey && !e.altKey && e.key.length === 1
      ? String.fromCharCode(e.key.toUpperCase().charCodeAt(0) & 31)
      : null;
    const special = keys[e.key];
    const printable = !e.ctrlKey && !e.metaKey && !e.altKey && e.key.length === 1 ? e.key : null;
    if (ctrl || special || printable) {
      e.preventDefault();
      send(ctrl || special || printable);
    }
  });
  document.addEventListener('paste', e => {
    if (!session || activeTab?.kind === 'if' || computerView.hidden === false || e.target === input) return;
    const text = e.clipboardData?.getData('text/plain');
    if (text) {
      e.preventDefault();
      send(text);
    }
  });
  $('terminal-screen').addEventListener('click', focusTerminal);

  // ===========================================================================
  // SHARED CODE-OSS / NATIVE / HTML MENU MODEL
  // ===========================================================================

  // VS Code supplies the live item labels, enabled state and command handlers.
  // Never maintain a duplicate, frozen list of editor commands in Terminal.
  // The same-origin workbench exposes its own menu model and command handlers.
  const codeMenuZone = $('code-menu-zone');
  const codeMenuRoots = new Map();
  const codeMenuNames = CODE_MENU_NAMES;
  const cinterTerminalActions = [
    ['code', 'New CinterForge Code Terminal'],
    ['mac', 'New Mac Terminal'],
    ['linux', 'New Linux Terminal'],
    ['win', 'New Windows Terminal'],
    ['server', 'New Server Log Terminal'],
    ['if', 'New Z-Machine Terminal'],
    ['minecraft', 'New Minecraft Terminal']
  ];

  // The Code-OSS workbench publishes its *own* IMenuService/IAction model from
  // the pinned MenubarControl source. No popup DOM scraping, positional command
  // names, or fabricated editor-command menu is permitted here.
  const codeServiceMenus = new Map();
  const codeServiceActions = new Set();
  // A Code-OSS menu publish can precede activation of the outer Terminal
  // toolbar. Request the same live model when a menu is opened; never fabricate
  // editor actions when the actual workbench has not answered yet.
  function requestCodeServiceMenus() {
    if (!codeIsActive() || !frame.contentWindow) return;
    frame.contentWindow.postMessage({
      type: 'cinterforge-codeoss-menu-request'
    }, window.location.origin);
  }
  function terminalItems() {
    // The EXISTING CinterForge application commands, used outside Code mode.
    // Code mode uses the real Code-OSS menu without overriding any of its items.
    return cinterTerminalActions.map(([kind, title]) => ({
      title, action: `terminal:${kind}`,
      enabled: (kind !== 'minecraft' || Boolean(minecraftMenuState)) &&
        (kind !== 'mac' || platform === 'mac') &&
        (kind !== 'win' || platform === 'mac' || platform === 'win') &&
        (kind !== 'linux' || platform === 'mac' || platform === 'linux')
    }));
  }

  function codeMenuSnapshot(name) {
    if (codeIsActive()) return codeServiceMenus.get(name) || [];
    if (name === 'File') return [
      {title: 'Relaunch CinterForge Terminal', action: 'app:relaunch', enabled: true},
      {title: 'Quit CinterForge Terminal', action: 'app:quit', enabled: true}
    ];
    if (name === 'Edit') return [
      {title: 'Copy', action: 'edit:copy', enabled: true},
      {title: 'Paste', action: 'edit:paste', enabled: true}
    ];
    if (name === 'Terminal') return terminalItems();
    return [];
  }

  function paintCodeMenuList(container, items) {
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
        paintCodeMenuList(children, item.children);
        submenu.append(label, children);
        container.append(submenu);
        continue;
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = item.title;
      button.disabled = item.enabled === false;
      button.addEventListener('click', () => {
        for (const menu of codeMenuRoots.values()) menu.open = false;
        void window.CinterMenuDispatch(item.action);
      });
      container.append(button);
    }
  }

  function refreshCodeMenu(name) {
    const entry = codeMenuRoots.get(name);
    if (!entry) return;
    const items = codeMenuSnapshot(name);
    paintCodeMenuList(entry.cinterList, items);
    void nativeEvent('code-menu-model', {name, items});
  }

  function updateCodeMenuMode() {
    const enabled = codeIsActive();
    for (const [name, entry] of codeMenuRoots) {
      const disabled = enabled && !codeServiceMenus.has(name);
      entry.classList?.toggle?.('code-menu-disabled', disabled);
      entry.querySelector?.('summary')?.setAttribute?.('aria-disabled', String(disabled));
      if (!enabled) entry.open = false;
    }
    void nativeEvent('code-menu-mode', {enabled});
    for (const name of codeMenuNames) refreshCodeMenu(name);
  }

  /** Dispatch a native or HTML action back to the original Code-OSS IAction. */
  window.CinterMenuDispatch = async function(action) {
    if (action === 'app:quit') {
      saveTerminalLayout();
      if (typeof window.cinterWindow === 'function') await window.cinterWindow({action: 'quit'});
      return;
    }
    if (action === 'app:relaunch') {
      saveTerminalLayout();
      if (typeof window.cinterWindow === 'function') await window.cinterWindow({action: 'relaunch'});
      else notice('Relaunch is available in the native application');
      return;
    }
    if (action === 'edit:copy') return copyTranscript();
    if (action === 'edit:paste') return pasteClipboard();
    if (action.startsWith('terminal:')) return window.CinterTerminalNewTab(action.slice(9));
    if (!codeIsActive() || !action.startsWith('code:') || !codeServiceActions.has(action)) return;
    frame.contentWindow?.postMessage({
      type: 'cinterforge-codeoss-menu-run', action: action.slice(5)
    }, window.location.origin);
  };

  function readCodeServiceItems(items, sourceActions, depth = 0) {
    if (!Array.isArray(items) || depth > 8 || items.length > 200) return [];
    const output = [];
    for (const item of items) {
      if (item?.separator === true) { output.push({separator: true}); continue; }
      if (typeof item?.title !== 'string' || typeof item?.action !== 'string' ||
          item.title.length > 240 || item.action.length > 180) continue;
      const normalized = {title: item.title, action: 'code:' + item.action,
        enabled: item.enabled !== false};
      if (Array.isArray(item.children)) {
        normalized.children = readCodeServiceItems(item.children, sourceActions, depth + 1);
      } else {
        sourceActions.add(normalized.action);
      }
      output.push(normalized);
    }
    return output;
  }

  // Message origin AND iframe identity are checked. The workbench service
  // publishes menu updates; no periodic DOM probing and no positional guesses.
  window.addEventListener('message', event => {
    if (event.source !== frame.contentWindow || event.origin !== window.location.origin ||
        event.data?.type !== 'cinterforge-codeoss-menu-model') return;
    const groups = event.data.groups;
    if (!Array.isArray(groups)) return;
    const updated = new Map();
    const sourceActions = new Set();
    for (const group of groups) {
      if (!codeMenuNames.includes(group?.name) || updated.has(group.name)) continue;
      updated.set(group.name, readCodeServiceItems(group.items, sourceActions));
    }
    if (!updated.size) return;
    codeServiceMenus.clear();
    codeServiceActions.clear();
    for (const [name, items] of updated) codeServiceMenus.set(name, items);
    for (const action of sourceActions) codeServiceActions.add(action);
    if (codeIsActive()) updateCodeMenuMode();
  });

  for (const name of codeMenuNames) {
    const menu = document.createElement('details');
    menu.className = 'terminal-menu terminal-application-menu';
    const summary = document.createElement('summary');
    summary.textContent = name;
    const items = document.createElement('div');
    items.className = 'terminal-menu-list';
    menu.append(summary, items);
    menu.cinterList = items;
    codeMenuZone.append(menu);
    codeMenuRoots.set(name, menu);
    menu.addEventListener('toggle', () => {
      if (!menu.open) return;
      for (const other of codeMenuRoots.values()) if (other !== menu) other.open = false;
      if (codeIsActive() && !codeServiceMenus.has(name)) requestCodeServiceMenus();
      refreshCodeMenu(name);
    });
  }

  // ---------------------------------------------------------------------------
  // Reopen the same Code workspace/mode after the native app is rebuilt.
  // Process state is intentionally not serialized; new PTYs are created safely.
  // ---------------------------------------------------------------------------
  const layoutKey = 'cinterterminal.window-layout.v1';
  function saveTerminalLayout() {
    if (acceptUI || !activeTab) return;
    try {
      const restorable = tabs.filter(tab => !tab.closed &&
        (tab.profileId === 'codeoss' || (tab.kind === 'terminal' &&
        !tab.profileId?.startsWith('macgame-')) || tab.kind === 'server' ||
        tab.kind === 'emulator' && !tab.profileId?.startsWith('quadra-650')));
      localStorage.setItem(layoutKey, JSON.stringify({
        active: restorable.indexOf(activeTab),
        tabs: restorable.map(tab => ({profileId: tab.profileId, backend: tab.backend, kind: tab.kind}))
      }));
    } catch (_) { /* Unavailable storage must never block closing the app. */ }
  }
  window.addEventListener('pagehide', saveTerminalLayout);

  // ===========================================================================
  // NATIVE / HTML APPLICATION MENUS
  // ===========================================================================

  // The Minecraft menu must never imply an absent server. The attached
  // console uses the already-running Bedrock VM and never starts another.
  let minecraftMenuState = null;
  /**
   * Enable or disable the Minecraft menu entry according to an already-running Bedrock server.
   */
  const updateMinecraftMenu = async () => {
    let enabled = false;
    try {
      const state = await api('/api/bedrock/status');
      enabled = !!(state.running || state.starting);
    }
    catch (_) {/* no installed Bedrock manager */ }
    if (enabled !== minecraftMenuState) {
      minecraftMenuState = enabled;
      const windowsItem = $('windows-new-minecraft');
      if (windowsItem) windowsItem.disabled = !enabled;
      void nativeEvent('minecraft-menu', { enabled });
    }
  };
  void updateMinecraftMenu();
  setInterval(() => {
    if (document.visibilityState !== 'hidden') void updateMinecraftMenu();
  }, 1600);
  // The installed Terminals selector remains in the existing titlebar.
  const titleMenus = Array.from(document.querySelectorAll('.terminal-menu'));
  // Both bars now use the shared live Code menu model. Legacy Windows-only
  // File/Edit controls have been removed; callers need no platform toggles.
  const setWindowsApplicationMenus = () => {};
  titleMenus.forEach(menu => menu.addEventListener('toggle', () => {
    if (menu.open) titleMenus.forEach(other => {
      if (other !== menu) other.open = false;
    });
  }));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && titleMenus.some(menu => menu.open)) {
      event.preventDefault();
      titleMenus.forEach(menu => menu.open = false);
      focusTerminal();
    }
  });

  /**
   * Open a selected historical computer profile in a new Session tab.
   */
  const openMuseumProfile = id => {
    titleMenus.forEach(menu => menu.open = false);
    void openSession(id);
  };
  /**
   * Append a menu button for a historical computer and connect it to its profile.
   */
  const addMuseumEntry = (target, profile) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'museum-entry';
    button.textContent = `${profile.year} ${profile.name}`;
    appendDecorativeIcon(button, iconCategoryForProfile(profile));
    button.addEventListener('click', () => openMuseumProfile(profile.id));
    target.append(button);
  };
  // Input events carry printable text; keydown handles control sequences.
  // Do not dispatch incomplete IME compositions as shell commands.
  // Keep live shell input, Discord drafts, and IF drafts on their separate routes.
  input.addEventListener('input', e => {
    // Composing text is still part of the Discord draft; just defer Enter.
    if (e.isComposing && activeTab?.kind === 'discord') {
      syncGameInput();
      return;
    }
    if (e.isComposing) return;
    if (activeTab?.kind === 'discord' || activeGame()) {
      syncGameInput();
      return;
    }
    if (input.value) {
      const value = input.value;
      input.value = '';
      send(value);
    }
  });
  input.addEventListener('compositionend', () => {
    if (activeTab?.kind === 'discord' || activeGame()) {
      syncGameInput();
      return;
    }
    if (input.value) {
      const value = input.value;
      input.value = '';
      send(value);
    }
  });
  // Cursor positioning is also visible when navigating/editing within a line.
  input.addEventListener('keyup', () => {
    if (activeGame() || activeTab?.kind === 'discord') syncGameInput();
  });
  input.addEventListener('click', () => {
    if (activeGame() || activeTab?.kind === 'discord') syncGameInput();
  });
  input.addEventListener('select', () => {
    if (activeGame() || activeTab?.kind === 'discord') syncGameInput();
  });
  // The transparent full-screen textarea's native mouse coordinates don't
  // correspond to visible Discord text. Clicking the Terminal focuses the
  // draft at its end rather than silently moving the insertion point.
  input.addEventListener('pointerdown', e => {
    if (activeTab?.kind !== 'discord') return;
    e.preventDefault();
    input.focus();
    input.setSelectionRange?.(input.value.length, input.value.length);
    syncGameInput();
  });
  // Capture IF/Discord editing keys before either input listener. In native
  // WebViews a restored textarea can be select-all without user intent;
  // Backspace/Delete must erase exactly one character, not the whole draft.
  input.addEventListener('keydown', e => {
    if (!(activeGame()?.io?.lineResolver || activeTab?.kind === 'discord') ||
      e.isComposing || e.key === 'Process') return;
    if (e.key !== 'Backspace' && e.key !== 'Delete') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const value = input.value;
    const start = input.selectionStart ?? value.length;
    const end = input.selectionEnd ?? start;
    const caret = Math.max(0, Math.min(end > start ? end : start, value.length));
    // A selected range is not permission to discard the entire command.
    // Delete precisely one Unicode character, including surrogate pairs.
    if (e.key === 'Backspace' && caret > 0) {
      const previous = Array.from(value.slice(0, caret)).slice(-1)[0] || '';
      input.setRangeText('', caret - previous.length, caret, 'start');
    } else if (e.key === 'Delete' && caret < value.length) {
      const next = Array.from(value.slice(caret))[0] || '';
      input.setRangeText('', caret, caret + next.length, 'start');
    }
    syncGameInput();
  }, true);
  input.addEventListener('keydown', e => {
    if (e.isComposing || e.key === 'Process') return;
    if (activeTab?.kind === 'discord') {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        e.stopImmediatePropagation();
        const message = input.value.trim();
        input.value = '';
        activeTab.draft = '';
        syncGameInput();
        if (message) void api('/api/discord/send', {
          channel_id: activeTab.channelId, content: message
        }).catch(error => notice('Discord send: ' + error.message));
      }
      return;
    }
    // File accelerators must not leak Ctrl+N/Ctrl+Q to the shell PTY.
    if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey &&
      ['n', 'q'].includes(e.key.toLowerCase())) return;
    const current = activeGame();
    if (current) {
      if (e.ctrlKey && e.key.toLowerCase() === 'c') {
        e.preventDefault();
        e.stopImmediatePropagation();
        void closeTab(activeTab);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (current.io?.lineResolver) {
          // Only the active story's WebIOAdapter receives this command.
          current.inputElement.value = input.value;
          current.inputElement.dispatchEvent(new KeyboardEvent('keydown',
            { key: 'Enter', bubbles: false }));
          input.value = '';
          current.draft = '';
          syncGameInput();
        }
      } else if (current.io && e.key !== 'Backspace' && e.key !== 'Delete') {
        // Forward readChar / history keys to this story only. Normal text
        // editing remains in the visible input and never reaches another VM.
        current.inputElement.value = input.value;
        current.inputElement.dispatchEvent(new KeyboardEvent('keydown',
          {
            key: e.key,
            code: e.code,
            ctrlKey: e.ctrlKey,
            shiftKey: e.shiftKey,
            altKey: e.altKey,
            metaKey: e.metaKey,
            bubbles: false
          }));
      }
      return;
    }
    const special = keys;
    const ctrl = e.ctrlKey && e.key.length === 1 ? String.fromCharCode(e.key.toUpperCase().charCodeAt(0) & 31) : null;
    const code = ctrl || special[e.key];
    if (code) {
      e.preventDefault();
      send(code);
    }
  });

  // ===========================================================================
  // BEDROCK EULA CONSENT AND INSTALLATION PROGRESS
  // ===========================================================================

  const license = $('license-dialog'), accepted = $('license-accept');
  let licensePending = false;
  /**
   * Show the Bedrock EULA dialog once without clearing an in-progress user decision.
   */
  const requestLicense = () => {
    // The status endpoint is polled repeatedly. Never clear a user's consent
    // checkbox while the dialog is already open.
    if (!license.hidden) return;
    accepted.checked = false;
    $('license-confirm').disabled = true;
    license.hidden = false;
    accepted.focus();
  };
  $('license-accept').addEventListener('change', () => {
    $('license-confirm').disabled = !accepted.checked;
  });
  $('license-cancel').addEventListener('click', async () => {
    if (licensePending) return;
    license.hidden = true;
    try {
      await api('/api/bedrock/eula/ack', {});
    } catch (e) {
      notice(e.message);
    }
    focusTerminal();
  });
  // Installation status is only polled AFTER a user explicitly accepts the
  // license; never make Bedrock a startup component or a management panel.
  /**
   * Show the post-consent Bedrock installation progress and errors in the current terminal.
   */
  const monitorBedrockInstall = async () => {
    let cursor = 0, lastUpdate = Date.now();
    try {
      for (; ;) {
        const events = await api('/api/bedrock/events?cursor=' + cursor);
        for (const event of events.events || []) {
          if (event.id <= cursor) continue;
          cursor = event.id;
          lastUpdate = Date.now();
          notice(event.message);
          display.push('\r\n[bedrock] ' + event.message + '\r\n');
        }
        if (events.log_error) throw new Error('Host log write failed: ' + events.log_error);
        const state = await api('/api/bedrock/status');
        if (Date.now() - lastUpdate > 8000) {
          lastUpdate = Date.now();
          display.push(
            '\r\n[bedrock] Awaiting download response. Host log: ' +
            (events.path || state.log_path) + '\r\n'
          );
        }
        if (state.install_error) throw new Error(state.install_error);
        if (state.installed) {
          notice('Bedrock downloaded and installed. Type bedrock start.');
          return;
        }
        if (!state.installing) throw new Error('Bedrock installation stopped before completion');
        await new Promise(resolve => setTimeout(resolve, 750));
      }
    } catch (e) {
      notice('Bedrock install failed: ' + e.message);
      display.push('\r\n[bedrock] ' + e.message + '\r\n[bedrock] Type bedrock log for details.\r\n');
    }
  };
  $('license-confirm').addEventListener('click', async () => {
    if (!accepted.checked || licensePending) return;
    licensePending = true;
    $('license-confirm').disabled = true;
    $('license-cancel').disabled = true;
    try {
      await api('/api/bedrock/accept', { accepted: true });
      license.hidden = true;
      notice('EULA accepted. Downloading and installing official Bedrock server…');
      display.push('\r\n[bedrock] Downloading and installing official Bedrock server…\r\n');
      focusTerminal();
      void monitorBedrockInstall();
    } catch (e) {
      notice(e.message);
    }
    finally {
      licensePending = false;
      $('license-cancel').disabled = false;
      $('license-confirm').disabled = !accepted.checked;
    }
  });

  // ===========================================================================
  // COMMAND EVENT POLLING AND APPLICATION STARTUP
  // ===========================================================================

  // A typed story command creates its OWN tab. Source PTYs and existing
  // games remain usable. Serial polling preserves start/stop event order.
  let pollingIF = false;
  setInterval(async () => {
    if (document.visibilityState === 'hidden' || pollingIF) return;
    pollingIF = true;
    try {
      const action = await api('/api/if/next');
      if (action.start) {
        await openIFTab(action.start, action.launch_id, action.session_id);
      } else if (action.stop) {
        const tab = tabs.find(tab => tab.kind === 'if' &&
          tab.game?.name === action.stop &&
          (!action.launch_id || tab.game.launchId === action.launch_id));
        if (tab) await stopGame(tab, false);
      }
    } catch (error) {
      notice('IF: ' + error.message);
    }
    finally {
      pollingIF = false;
    }
  }, 800);
  // Every typed `wargames` command uses the same Session path as the old
  // menu action. The JOSHUA application and iframe are unchanged.
  setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await api('/api/wargames/next');
      if (result.open) await openSession('joshua');
    } catch (error) {
      notice('WarGames: ' + error.message);
    }
  }, 850);
  // Macintosh games use the existing emulator viewer. Their library records
  // do not supply verified guest application paths, so direct auto-open is
  // experimental: boot Infinite HD, then curate per-game startup recipes.
  setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await api('/api/macgames/next');
      if (!result.game) return;
      const game = macCatalog.get(result.game);
      if (!game) throw new Error('Unknown Macintosh game: ' + result.game);
      const machine = game.needs_mac_os_x ? 'Power Macintosh G4 (PCI Graphics)' : 'Quadra 650';
      const disk = game.needs_mac_os_x ? 'Mac OS 9.2' : 'Mac OS 7.6';
      profiles = profiles.filter(profile => profile.id !== 'macgame-' + result.game);
      profiles.push({
        id: 'macgame-' + result.game,
        name: game.title,
        machine,
        disk,
        kind: 'emulator',
        year: 1993,
        macGame: true
      });
      await openSession('macgame-' + result.game);
    } catch (error) {
      notice('Mac game: ' + error.message);
    }
  }, 850);
  // The documented `code` shell command opens the staged Code-OSS tab.
  // A second invocation selects the existing workbench instead of replacing
  // the requesting shell or creating a duplicate editor instance.
  setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await api('/api/code/next');
      if (!result.open) return;
      const existing = tabs.find(tab => !tab.closed && tab.profileId === 'codeoss');
      if (existing) {
        if (result.reload && activeTab === existing) {
          // Theme/workspace changed in a shell: reload the workbench so its
          // existing IF-derived startup applies the persisted preferences.
          await showComputer(profiles.find(profile => profile.id === 'codeoss'));
        } else {
          await activateTab(existing);
        }
      } else await openSession('codeoss');
    } catch (_) { /* Service may be closing. */ }
  }, 850);
  // Development-only HTML menu inspection on macOS. The native menu remains
  // authoritative; the command only controls visibility of the existing row.
  setInterval(async () => {
    if (platform !== 'mac' || document.visibilityState === 'hidden') return;
    try {
      const state = await api('/api/ui/html-menus');
      document.documentElement?.setAttribute?.('data-html-menus', state.show ? 'show' : 'hide');
    } catch (_) { /* Closing webview or server: keep menus hidden by default. */ }
  }, 1000);
  // Hidden shell command: never put the private profile into a visible menu.
  setInterval(async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const result = await api('/api/lagomorph/next');
      if (result.open && result.profile === 'quadra-650') {

        await openSession(result.profile);
      }
    } catch (_) { /* local service may be shutting down */ }
  }, 850);
  (async () => {
    try {
      const status = await api('/api/status');
      platform = status.platform;
      document.documentElement?.setAttribute?.('data-cinter-platform', platform);
      setWindowsApplicationMenus();

      const data = await (await fetch('/terminal_styles.json', { cache: 'no-store' })).json();
      profiles = [
        ...(data.profiles || []),
        ...(data.emulators || []),
        data.codeoss,
        { id: 'joshua', name: 'JOSHUA', year: 2026, kind: 'joshua' }
      ].filter(Boolean);
      const macData = await (await fetch('/mac_games.json', { cache: 'no-store' })).json();
      macCatalog = new Map((macData.games || []).map(game => [game.command, game]));
      const byId = new Map(profiles.map(profile => [profile.id, profile]));
      const stored = localStorage.getItem('cinterterminal.profile.' + platform);
      const initial = stored === 'quadra-650' ? data.defaults[platform] : (stored || data.defaults[platform]);
      // Normal startup keeps the default Session non-blocking while the
      // Terminals list initializes.  Native acceptance must NOT start that
      // concurrent PTY: openSession has a single-open guard, and an in-flight
      // Apple II connection previously made its VT100 probe a false failure.
      const initialId = byId.has(initial) ? initial : data.defaults[platform];
      if (!acceptUI) {
        let restored = null;
        try { restored = JSON.parse(localStorage.getItem(layoutKey) || 'null'); }
        catch (_) { /* Corrupt optional layout is not a startup failure. */ }
        const previous = Array.isArray(restored?.tabs) ? restored.tabs.filter(tab =>
          (tab.kind === 'server' || byId.has(tab.profileId) &&
           !byId.get(tab.profileId)?.lagomorph && !byId.get(tab.profileId)?.macGame) &&
          ['native', 'mac', 'win', 'linux', 'server'].includes(tab.backend) ||
          tab.profileId === 'codeoss').slice(0, 12) : [];
        if (previous.length) {
          void (async () => {
            for (const tab of previous) {
              if (tab.kind === 'server') await openServerTab();
              else await openSession(tab.profileId, tab.backend || 'native');
            }
            if (Number.isInteger(restored.active) && tabs[restored.active])
              await activateTab(tabs[restored.active]);
          })().catch(error => notice('Session restore: ' + error.message));
        } else void openSession(initialId);
      }
      // Historical categories are four independent menus; don't flatten
      // Macintosh, BeOS and NeXTSTEP into the existing Terminals list.
      const menuLists = {
        terminal: $('terminal-menu-list'),
        macintosh: $('macintosh-menu-list'),
        beos: $('beos-menu-list'),
        nextstep: $('nextstep-menu-list')
      };
      if (Object.values(menuLists).some(list => !list)) throw new Error('Museum menus missing');
      Object.values(menuLists).forEach(list => list.replaceChildren());
      const serverEntry = document.createElement('button');
      serverEntry.type = 'button';
      serverEntry.className = 'museum-entry';
      serverEntry.textContent = 'Server · Live log';
      appendDecorativeIcon(serverEntry, 'server');
      serverEntry.addEventListener('click', () => {
        titleMenus.forEach(menu => menu.open = false);
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
        addMuseumEntry(menuLists[groupOf(profile)], profile);
      }
      await nativeEvent('ready', { menu: (data.museum || []).length, profile: initialId });
      if (!acceptUI) void refreshLAN().catch(() => { });
      if (acceptUI) {
        // Exercise two real Sessions sequentially, using a public museum Mac
        // rather than the private Lagomorph Quadra.  No background default
        // PTY competes with the native VT100 callback.
        for (const id of ['mac-plus', 'vt100-terminal']) {
          if (!byId.has(id)) throw new Error('Missing museum entry: ' + id);
          await openSession(id);
          if (activeTab?.profileId !== id)
            throw new Error('Museum Session did not open: ' + id);
          if (id === 'vt100-terminal' && (!session?.id || activeTab.session !== session))
            throw new Error('VT100 PTY did not open');
          if (id === 'mac-plus' && computerView.hidden)
            throw new Error('Mac Plus emulator Session did not open');
        }
      }
    } catch (e) {
      notice(e.message);
      await nativeEvent('error', { message: e.message });
    }
  })();
  setInterval(
    () => {
      if (document.visibilityState === 'visible' && !acceptUI && !$('lan-menu')?.open)
        void refreshLAN().catch(() => { });
    },
    12000);
  window.addEventListener(
    'resize',
    () => {
      if (session) {
        const size = terminalSize();
        api('/api/terminal/resize', { id: session.id, ...size }).catch(e => notice(e.message));
      }
    });
  // Notify the browser lifetime boundary without changing the host’s shutdown ownership.
  window.addEventListener('beforeunload', () => {
    if (tabs.length) navigator.sendBeacon?.('/__noop');
  });
})();
