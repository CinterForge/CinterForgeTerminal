/*
 * CinterForgeTerminal — browser-based presentation and guest-control client.
 *
 * This module is shared by the native macOS/Windows/Linux app and by the web
 * preview. Python owns authenticated terminal sessions, shell processes and
 * Bedrock state. JavaScript owns UI rendering, API requests, native menu
 * notifications and messages to the externally hosted Infinite Mac emulator.
 *
 * PUBLIC BROWSER INTERFACES
 *   window.CinterHost              Authenticated service and terminal API
 *   window.CinterTerminalNewTab    Native menu -> new in-window terminal tab
 *
 * STRUCTURE
 *   1. Configuration/constants (including every Lagomorph timing value)
 *   2. Public bridge/API endpoints and error reporting
 *   3. Guest emulator controls and session/terminal UI helpers
 *   4. Input dispatch, native menus and lifecycle event listeners
 *
 * IMPORTANT: the Lagomorph 16-color menu, Control Strip and guest keystroke
 * sequence is proven working on warm boots. Configuration values below are
 * preserved exactly; no timing, coordinate, event, or launch-order change is
 * authorized by the current readability refactor.
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
    colorDepthMenu: Object.freeze({x: 40, y: 469}),
    sixteenColors: Object.freeze({x: 80, y: 400}),
    controlStripToggle: Object.freeze({x: 8, y: 469}),
    lagomorphDisk: Object.freeze({x: 600, y: 160}),
    lagomorphApplication: Object.freeze({x: 40, y: 142}),
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

  // Keep the token in the served document, not a CSP-sensitive inline script.
  // A native WebView and a browser-only preview share this same authenticated API.
  const token = document.documentElement?.getAttribute('data-cph-token') || window.__CPH_TOKEN__;
  // Acceptance is a separate, deterministic UI probe, not a user session.
  const acceptUI=new URLSearchParams(location.search||'').has('accept-ui') ||
    Boolean(window.__CINTER_ACCEPT_UI__);
  // The Host registers this native WebView binding before navigating here.
  // Browser-only previews remain usable without a native host.
  const nativeEvent = (event, detail={}) => {
    if(typeof window.cinterNative !== 'function') return Promise.resolve();
    return Promise.resolve(window.cinterNative({event, ...detail})).catch(error => {
      console.error('Native Terminal bridge:', error);
    });
  };
  const $ = id => document.getElementById(id);
  // Native system menus own New Terminal and Edit actions. The HTML
  // titlebar contains the persistent Terminals selector only.
  const api = async (path, data) => {
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST',
      credentials: 'same-origin', cache: 'no-store',
      headers: {'X-CPH-Token': token, ...(data === undefined ? {} : {'Content-Type':'application/json'})},
      ...(data === undefined ? {} : {body: JSON.stringify(data)})
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    return value;
  };
  // Same API in all applications; pages can call these methods when the Host
  // advertises the terminal capability, otherwise they must degrade cleanly.
  window.CinterHost = Object.freeze({
    terminal: Object.freeze({open: args => api('/api/terminal/open', args || {}),
      input: (id,data) => api('/api/terminal/input',{id,data}),
      poll: (id,wait_ms=200) => api('/api/terminal/poll',{id,wait_ms}),
      resize: (id,cols,rows) => api('/api/terminal/resize',{id,cols,rows}),
      close: id => api('/api/terminal/close',{id})}),
    debugger: () => api('/api/debugger',{}),
    bedrock: Object.freeze({status: () => api('/api/bedrock/status'),
      accept: () => api('/api/bedrock/accept', {accepted:true}),
      install: () => api('/api/bedrock/install', {}),
      start: () => api('/api/bedrock/start',{}),
      stop: () => api('/api/bedrock/stop',{})})
  });

  // A deliberately bounded text/ANSI presentation. A full VT emulator may be
  // substituted without changing the Python Host terminal contract.
  class VTOutput {
    constructor(target) { this.target=target; this.lines=['']; this.history=[]; this.current=0; this.esc=''; }
    transcript(){return this.history.concat(this.lines).join('\n');}
    push(text) {
      for (const c of text) {
        if (this.esc) {
          this.esc += c;
          if (this.esc.length > 64) { this.esc=''; continue; }
          if (this.esc === '\u001b[' || this.esc === '\u001b]') continue;
          if (this.esc.startsWith('\u001b[')) {
            if (/[A-Za-z~]/.test(c)) {
              if (c === 'J') {this.history.push(...this.lines);this.lines=[''];this.current=0;}
              else if (c === 'H') this.current=0;
              else if (c === 'K') {
                // CSI K defaults to erasing from cursor, not entire draft.
                const match=this.esc.match(/^\u001b\[([0-9;]*)K$/);
                const mode=match && match[1] ? Number(match[1].split(';')[0]||0) : 0;
                const index=this.lines.length-1;
                const line=this.lines[index];
                if(mode===1) this.lines[index]=' '.repeat(this.current)+line.slice(this.current);
                else if(mode===2) this.lines[index]='';
                else this.lines[index]=line.slice(0,this.current);
              }
              this.esc='';
            }
          } else if (this.esc.startsWith('\u001b]')) {
            if (c==='\u0007' || this.esc.endsWith('\u001b\\')) this.esc='';
          } else this.esc='';
          continue;
        }
        if (c==='\u001b') {this.esc=c;continue;}
        if (c==='\r') {this.current=0;continue;}
        if (c==='\n') {this.lines.push('');this.current=0;continue;}
        if (c==='\b') {this.current=Math.max(0,this.current-1);continue;}
        if (c==='\t') {this.push(' '.repeat(8-this.current%8));continue;}
        if (c.charCodeAt(0)<32 && c!=='\u0000') continue;
        const index=this.lines.length-1, old=this.lines[index];
        this.lines[index]=old.padEnd(this.current,' ').slice(0,this.current)+c+old.slice(this.current+1);
        this.current++;
      }
      if (this.lines.length>2500) this.history.push(...this.lines.splice(0,this.lines.length-2500));
      this.render();
    }
    render(){
      // Put the insertion marker at the real VT cursor, without adding any
      // character to the copied transcript or the live shell input.
      const line=this.lines[this.lines.length-1] || '';
      const before=this.lines.slice(0,-1).join('\n');
      const prefix=(this.lines.length>1?before+'\n':'')+line.slice(0,this.current);
      const suffix=line.slice(this.current);
      const caret=document.createElement('span');caret.className='terminal-cursor';
      caret.setAttribute('aria-hidden','true');
      this.target.replaceChildren(document.createTextNode(prefix),caret,document.createTextNode(suffix));
      const parent=this.target.parentElement;
      if(parent)parent.scrollTop=parent.scrollHeight;
    }
    reset(){this.lines=[''];this.history=[];this.current=0;this.render();}
  }
  const decode = data => {
    const binary=atob(data), bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));
    return new TextDecoder('utf-8').decode(bytes);
  };
  let session=null, generation=0, platform='linux', selected='', profiles=[], waiting=false;
  const queuedSessionOpens=[];
  let macCatalog=new Map();
  const museumKind = x => x?.kind || 'terminal';
  // Display icons identify the same real terminal/emulator category on menu
  // buttons and tabs. No categories or launch behavior are changed here.
  function iconCategoryForProfile(profile, backend = 'native') {
    if (backend === 'server') return 'server';
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

  function appendDecorativeIcon(target, category) {
    const icon = document.createElement('span');
    icon.className = `terminal-icon terminal-icon--${category}`;
    icon.textContent = TERMINAL_ICON_GLYPHS[category] || TERMINAL_ICON_GLYPHS.terminal;
    icon.setAttribute('aria-hidden', 'true');
    // Appending after the label retains the existing textContent contract in
    // lightweight UI tests. CSS visually orders the icon before its label.
    target.append(icon);
  }

  const frame=$('computer-frame'), computerView=$('computer-view');
  // Older staged HTML lacked this diagnostic node even when terminal.js was
  // newer. Repair that mixed-version presentation rather than crashing the
  // entire Terminal before its native UI/PTY acceptance callbacks can run.
  const codeError=$('computer-error') || (() => {
    if(!computerView) throw new Error('Terminal computer view is missing');
    const node=document.createElement('div');
    node.id='computer-error';
    node.className='computer-error';
    node.setAttribute('role','alert');
    node.hidden=true;
    computerView.append(node);
    return node;
  })();
  const codeUrl='/codeoss/out/vs/code/browser/workbench/workbench.html';
  let codeLoad=0, codeReady=false, codeLastError='';
  const codeFailure = message => {
    if(computerView.hidden || !frame.src.includes(codeUrl) || codeReady)return;
    codeLastError=String(message).slice(0,450);
    codeError.textContent='VS Code did not start.\n'+codeLastError+'\nOpen Debugger for WebKit details.';
    codeError.hidden=false;
    notice('VS Code: '+codeLastError);
    void nativeEvent('error',{message:'VS Code: '+codeLastError});
  };
  // Require a real workbench DOM, not merely a successful HTML response.
  // Staged Code-OSS sends early resource/script errors from its HTTP response.
  window.addEventListener('message',event=>{
    if(event.origin!==new URL(location.href).origin || event.source!==frame.contentWindow ||
       computerView.hidden || !frame.src.includes(codeUrl))return;
    const data=event.data;
    if(!data || data.type!=='cinterforge-codeoss-health')return;
    if(data.status==='ready'){
      codeReady=true;codeError.hidden=true;
      notice('VS Code workbench ready');
    } else if(data.status==='error') {
      codeLastError=String(data.message||'Workbench error').slice(0,450);
    } else if(data.status==='timeout') {
      codeFailure(codeLastError||data.message||'Workbench did not initialize');
    }
  });
  // A successful iframe load is not proof that the JavaScript workbench ran.
  // Check the critical resources separately, then require an explicit ready.
  const checkCodeAssets = async stamp => {
    for(const url of [codeUrl, '/codeoss/out/vs/code/browser/workbench/workbench.js',
                      '/codeoss/out/nls.messages.js']) {
      try {
        const response=await fetch(url,{method:'HEAD',cache:'no-store'});
        if(stamp!==codeLoad)return;
        if(!response.ok) {codeFailure(`${url}: HTTP ${response.status}`);return;}
      } catch(error) {
        if(stamp===codeLoad)codeFailure(`${url}: ${error.message}`);
        return;
      }
    }
  };
  // The private Quadra 650 uses 640x480 guest coordinates. These are NOT
  // coordinates in the host browser/window (which users may resize or zoom).
  // Calibrated from the owner's real Mac OS 7.6 Control Strip screenshot:
  //   display-depth module (40,469), 16 Colors row (80,400), close box (8,469).
  // Classic Mac pop-up menus use mouse down -> move while held -> mouse up.
  // Change the color mode first; only then dismiss the Control Strip, bring
  // the Welcome/Sticky Notes window to the front and send Cmd+Q.
  // No cross-origin DOM inspection or synthetic browser input is involved.
  let lagBootSequence=0;
  let lagWelcomeTimer=null;
  const lagPause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const lagomorphBootControls=async (stamp,target)=>{
    const valid=()=> stamp===lagBootSequence && !computerView.hidden &&
      selected==='quadra-650' && frame.contentWindow===target;
    let down=false, meta=false, q=false;
    const send=data=>target.postMessage(data,'https://infinitemac.org');
    const move=(x,y)=>send({type:'emulator_mouse_move',x,y,deltaX:0,deltaY:0});
    const mouseDown=()=>{send({type:'emulator_mouse_down',button:0});down=true;};
    const mouseUp=()=>{send({type:'emulator_mouse_up',button:0});down=false;};
    const click=async(x,y)=>{
      move(x,y);await lagPause(LAGOMORPH_DELAYS_MS.clickPointerSettle);if(!valid())return false;
      mouseDown();await lagPause(LAGOMORPH_DELAYS_MS.clickPress);mouseUp();return valid();
    };
    const doubleClick=async(x,y)=>{
      // Two mouse presses in one classic Mac double-click interval, keeping
      // the pointer stationary on the icon between clicks.
      move(x,y);await lagPause(LAGOMORPH_DELAYS_MS.doubleClickPointerSettle);if(!valid())return false;
      mouseDown();await lagPause(LAGOMORPH_DELAYS_MS.doubleClickFirstPress);mouseUp();
      await lagPause(LAGOMORPH_DELAYS_MS.doubleClickPressGap);if(!valid())return false;
      mouseDown();await lagPause(LAGOMORPH_DELAYS_MS.doubleClickSecondPress);mouseUp();return valid();
    };
    try{
      if(!valid())return;
      move(LAGOMORPH_GUEST_POSITIONS.colorDepthMenu.x,LAGOMORPH_GUEST_POSITIONS.colorDepthMenu.y);await lagPause(LAGOMORPH_DELAYS_MS.colorMenuPointerMove);if(!valid())return;
      mouseDown();await lagPause(LAGOMORPH_DELAYS_MS.colorMenuMouseHeld);if(!valid())return;
      move(LAGOMORPH_GUEST_POSITIONS.sixteenColors.x,LAGOMORPH_GUEST_POSITIONS.sixteenColors.y);await lagPause(LAGOMORPH_DELAYS_MS.colorMenuSelection);mouseUp();if(!valid())return;
      // Selecting a different pixel depth can briefly redraw the guest.
      await lagPause(LAGOMORPH_DELAYS_MS.displayRedraw);if(!valid())return;
      if(!await click(LAGOMORPH_GUEST_POSITIONS.controlStripToggle.x,LAGOMORPH_GUEST_POSITIONS.controlStripToggle.y))return;      // Hide the Control Strip.
      await lagPause(LAGOMORPH_DELAYS_MS.afterControlStripClose);if(!valid())return;
      // Do NOT click (8,469) again: this is the Control Strip toggle,
      // not Sticky Notes. The first click above closes it exactly once.
      // quitSticky() focuses the guest iframe without another mouse click.
      await lagPause(LAGOMORPH_DELAYS_MS.beforeStickyNotesQuit);if(!valid())return;
      const quitSticky=async()=>{
        // Infinite Mac expects physical ADB MetaLeft + KeyQ, not a browser
        // keyboard shortcut.  Keep both keys held long enough for the guest
        // to see them; release even if the profile changes mid-sequence.
        frame.focus();
        send({type:'emulator_key_down',code:'MetaLeft'});meta=true;
        await lagPause(LAGOMORPH_DELAYS_MS.quitModifierDown);if(!valid())return false;
        send({type:'emulator_key_down',code:'KeyQ'});q=true;
        await lagPause(LAGOMORPH_DELAYS_MS.quitKeyDown);
        send({type:'emulator_key_up',code:'KeyQ'});q=false;
        send({type:'emulator_key_up',code:'MetaLeft'});meta=false;
        return valid();
      };
      if(!await quitSticky())return;
      // On a cold guest boot, the first gesture may only activate the
      // emulator. Retry Command-Q without another mouse click: the bottom
      // corner is the Control Strip toggle and a second click reopens it.
      await lagPause(LAGOMORPH_DELAYS_MS.betweenQuitAttempts);if(!valid())return;
      if(!await quitSticky())return;
      // Some System 7 Sticky Notes editions keep a note open even after a
      // Command-Q dispatch. Close the active *note window* with guest Command-W
      // as a separate fallback; this is not a host/browser shortcut. It only
      // affects the currently focused guest application, never the app shell.
      await lagPause(LAGOMORPH_DELAYS_MS.beforeCloseWindowFallback);if(!valid())return;
      frame.focus();
      send({type:'emulator_key_down',code:'MetaLeft'});meta=true;
      await lagPause(LAGOMORPH_DELAYS_MS.closeModifierDown);if(!valid())return;
      send({type:'emulator_key_down',code:'KeyW'});
      await lagPause(LAGOMORPH_DELAYS_MS.closeKeyDown);
      send({type:'emulator_key_up',code:'KeyW'});
      send({type:'emulator_key_up',code:'MetaLeft'});meta=false;
      // Finder can now reveal the Lagomorph 1 disk, whether the Sticky Notes
      // process quit or its obstructing note was just closed.
      // at (600,160). Opening it shows the Lagomorph 1 app at (40,142).
      // These are 640x480 guest coordinates, independent of host mouse capture.
      await lagPause(LAGOMORPH_DELAYS_MS.beforeDiskOpen);if(!valid())return;
      if(!await doubleClick(LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.x,LAGOMORPH_GUEST_POSITIONS.lagomorphDisk.y))return;
      await lagPause(LAGOMORPH_DELAYS_MS.betweenDiskAndAppOpen);if(!valid())return;
      if(!await doubleClick(LAGOMORPH_GUEST_POSITIONS.lagomorphApplication.x,LAGOMORPH_GUEST_POSITIONS.lagomorphApplication.y))return;
    }finally{
      // Never leave a button or modifier held if the user switches machines.
      if(down)mouseUp();
      if(q)send({type:'emulator_key_up',code:'KeyQ'});
      if(meta)send({type:'emulator_key_up',code:'MetaLeft'});
    }
  };
  window.addEventListener('message', event=>{
    if(event.origin!=='https://infinitemac.org' || event.source!==frame.contentWindow ||
       computerView.hidden || event.data?.type!=='emulator_loaded' ||
       selected!=='quadra-650')return;
    const stamp=lagBootSequence;
    if(lagWelcomeTimer)clearTimeout(lagWelcomeTimer);
    // emulator_loaded means the emulator is running, not that Finder's
    // Control Strip and Sticky Notes window have both finished opening.
    lagWelcomeTimer=setTimeout(()=>{
      lagWelcomeTimer=null;
      if(stamp!==lagBootSequence)return;
      const target=frame.contentWindow;
      if(target)void lagomorphBootControls(stamp,target).catch(e=>{
        console.error('Lagomorph guest controls:',e);
      });
    },LAGOMORPH_DELAYS_MS.initialGuestBoot);
  });
  const showTerminal = () => {
    lagBootSequence++;
    if(lagWelcomeTimer){clearTimeout(lagWelcomeTimer);lagWelcomeTimer=null;}
    codeLoad++;codeReady=false;codeError.hidden=true;
    frame.src='about:blank';computerView.hidden=true;
    $('terminal-keys').hidden=false;
    $('terminal-screen').hidden=false;$('terminal-input').disabled=false;
  };
  const showComputer = async (profile) => {
    lagBootSequence++;
    if(lagWelcomeTimer){clearTimeout(lagWelcomeTimer);lagWelcomeTimer=null;}
    // An emulator lives only in the iframe. Host titlebar menus must survive
    // every VM load and remain clickable above the embedded screen.
    document.querySelectorAll('.terminal-menu[open]').forEach(menu=>{menu.open=false;});
    $('terminal-screen').hidden=true;$('terminal-input').disabled=true;
    computerView.hidden=false;
    $('terminal-keys').hidden=true;
    codeLoad++;codeReady=false;codeLastError='';codeError.hidden=true;
    if(museumKind(profile)==='joshua') {
      // The actual standalone WarGames application, loaded from the staged
      // same-origin source, owns its game screen and keyboard in this Session.
      frame.src='/joshua/index.html';
    } else if(museumKind(profile)==='codeoss') {
      const stamp=codeLoad;
      frame.src=codeUrl;
      void checkCodeAssets(stamp);
      setTimeout(()=>{
        if(stamp===codeLoad && !codeReady)codeFailure(codeLastError||'No ready signal from staged Code-OSS after 12 seconds');
      },12500);
    } else {
      const url=new URL('https://infinitemac.org/embed');
      url.searchParams.set('machine',profile.machine);
      url.searchParams.set('disk',profile.disk);
      if(profile.macGame){
        url.searchParams.set('infinite_hd','true');
      }
      if(profile.lagomorph){
        url.searchParams.set('screenSize','640x480');
        // The distributed Lagomorph HFS/CD image is immutable. Give the
        // Macintosh a separate writable Saved HD for persistent game saves.
        // This is the existing Infinite Mac Saved HD feature, not a second UI.
        url.searchParams.set('saved_hd','true');
      }
      // Infinite Mac refuses HTTP localhost CD-ROMs, even if our HTTP server
      // allows CORS and byte ranges. Never send it an unsupported URL.
      // A separately hosted HTTPS image with range/CORS support is required.
      if(profile.id==='quadra-650' && profile.lagomorph){
        try{
          const media=await api('/api/lagomorph/media');
          // A read-only CD lacks a writable Desktop database and triggers Finder
          // repair errors. Infinite Mac's disk_url enables a writable overlay.
          if(media.configured && media.url)url.searchParams.set('disk_url',media.url);
          if(Array.isArray(media.floppies) && media.floppies.length===2){
            url.searchParams.delete('disk_url');
            media.floppies.forEach(floppy=>url.searchParams.append('disk_url',floppy));
          }
          if(!media.configured || !media.url)notice(media.reason || 'Lagomorph media requires a public HTTPS URL.');
        }catch(e){notice('Lagomorph CD-ROM unavailable: '+e.message);}
      }
      frame.src=url.toString();
    }
  };
  let display = new VTOutput($('terminal-output'));
  const tabBar=$('terminal-tabs');
  const tabs=[];
  let activeTab=null, tabSerial=0;
  const makeBuffer=()=>{const container=document.createElement('div');
    const pre=document.createElement('pre');container.append(pre);return pre;};
  const renderTabs=()=>{
    if(!tabBar)return;
    tabBar.replaceChildren();
    for(const tab of tabs){
      const tabButton=document.createElement('button');tabButton.type='button';
      tabButton.className='session-tab'+(activeTab===tab?' active':'');
      tabButton.textContent=tab.label;
      const tabProfile = profiles.find(profile => profile.id === tab.profileId);
      appendDecorativeIcon(tabButton, iconCategoryForProfile(tabProfile, tab.backend));
      tabButton.setAttribute('role','tab');
      tabButton.setAttribute('aria-selected',String(activeTab===tab));
      tabButton.addEventListener('click',()=>{void activateTab(tab);});
      tabBar.append(tabButton);
      const close=document.createElement('button');close.type='button';close.className='session-tab-close';
      close.textContent='×';close.title='Close '+tab.label;
      close.setAttribute('aria-label','Close '+tab.label);
      close.addEventListener('click',()=>{void closeTab(tab);});tabBar.append(close);
    }
    const create=document.createElement('button');create.type='button';create.className='session-tab-new';
    create.textContent='+';create.title='New terminal session';
    create.setAttribute('aria-label','New terminal session');
    create.addEventListener('click',()=>{void openSession('modern');});
    tabBar.append(create);
  };
  async function activateTab(tab){
    if(!tab || !tabs.includes(tab) || activeTab===tab)return;
    // The shell, each IF interpreter and each emulator own separate tabs.
    if(activeTab?.kind==='if' && activeTab.game)activeTab.game.draft=$('terminal-input').value;
    if(activeTab && activeTab.kind!=='if'){
      activeTab.display.target=activeTab.buffer;activeTab.display.render();
    }
    activeTab=tab;session=tab.session;selected=tab.profileId;display=tab.display;
    if(tab.kind==='if'){
      showTerminal();applyProfile(profiles.find(p=>p.id===tab.profileId)||profiles[0]);
      $('terminal-keys').hidden=false;
      $('terminal-input').value=tab.game?.draft||'';
      paintIFTab(tab);
      focusTerminal();
      renderTabs();
      return;
    }
    $('terminal-input').value='';
    $('terminal-output').removeAttribute('data-if-echo');
    display.target=$('terminal-output');display.render();
    const profile=profiles.find(p=>p.id===tab.profileId)||profiles[0];
    if(tab.kind==='server'){
      showTerminal();applyProfile(profile);
      $('terminal-input').disabled=true;
      $('terminal-keys').hidden=true;
      $('terminal-shell').textContent='Server · live HTTP/Python log (read-only)';
      notice(tab.logPath ? 'Server log: '+tab.logPath : 'Connecting to live Server log…');
    }else if(museumKind(profile)==='terminal'){
      showTerminal();applyProfile(profile);
      $('terminal-shell').textContent=tab.session?(tab.kind==='bedrock'?'Bedrock · live server console':`${platform} · ${tab.label}`):'Disconnected';
      focusTerminal();
      if(tab.session){const size=terminalSize();void api('/api/terminal/resize',{id:tab.session.id,...size}).catch(e=>notice(e.message));}
    }else{
      await showComputer(profile);
      $('terminal-overlay').dataset.profile=profile.id;
      $('terminal-machine-title').textContent=profile.name.toUpperCase();
      $('terminal-shell').textContent=`${profile.year} · ${profile.name}`;
    }
    renderTabs();
  }
  async function closeTab(tab){
    if(!tabs.includes(tab) || tab.closed)return;
    // Mark closed before awaiting network I/O. A late PTY connection or IF
    // download must never resurrect a closed tab or take over another tab.
    tab.closed=true;tab.pollGeneration=(tab.pollGeneration||0)+1;
    if(tab.game)void stopGame(tab).catch(error=>console.error('IF close:',error));
    const closingSession=tab.session;
    tab.session=null;
    const index=tabs.indexOf(tab);tabs.splice(index,1);
    if(activeTab===tab){
      activeTab=null;session=null;
      $('terminal-input').value='';
      if(tabs.length)await activateTab(tabs[Math.max(0,index-1)]);
      else await openSession('modern');
    }else renderTabs();
    if(closingSession)try{await api('/api/terminal/close',{id:closingSession.id});}
      catch(_){/* already closed */}
  }
  // Other tabs keep their own PTYs alive; a Minecraft console running in one
  // must never be closed just because another shell or emulator is selected.
  // Server is a read-only Terminal type, not a shell and not an emulator.
  // The same authenticated stream is written to the selected runtime Logs root.
  async function pollServerTab(tab){
    const stamp=++tab.pollGeneration;
    while(!tab.closed && tab.pollGeneration===stamp){
      try{
        const result=await api('/api/server/log?cursor='+tab.cursor);
        if(tab.closed || tab.pollGeneration!==stamp)return;
        tab.logPath=result.log_path;
        if(result.truncated)tab.display.push('[Server] Earlier events remain in '+tab.logPath+'\n');
        for(const line of result.lines||[])tab.display.push(line+'\n');
        tab.cursor=result.cursor;
        if(activeTab===tab)notice('Server log: '+tab.logPath);
      }catch(error){
        if(tab.closed || tab.pollGeneration!==stamp)return;
        tab.display.push('[Server] Log connection failed: '+error.message+'\n');
        if(activeTab===tab)notice('Server log connection failed');
      }
      await new Promise(resolve=>setTimeout(resolve,750));
    }
  }
  async function openServerTab(){
    const buffer=makeBuffer();
    const tab={key:++tabSerial,label:'Server',profileId:'modern',backend:'server',kind:'server',
      session:null,closed:false,cursor:0,pollGeneration:0,logPath:'',
      display:new VTOutput(buffer),buffer};
    tab.display.target=tab.buffer;
    tabs.push(tab);
    await activateTab(tab);
    tab.display.push('Connecting to the CinterForgeTerminal HTTP/Python server log…\n');
    void pollServerTab(tab);
    return tab;
  }
  window.CinterTerminalNewTab=async(kind='mac')=>{
    if(typeof kind==='string' && kind.startsWith('profile:')){
      const id=kind.slice(8);
      if(profiles.some(profile=>profile.id===id && !profile.lagomorph))
        return openSession(id);
      notice('Unknown museum machine');return;
    }
    if(kind==='server')return openServerTab();
    if(kind==='mac')return openSession('modern');
    if(kind==='linux')return openSession('vt100-terminal','linux');
    if(kind==='win')return openSession('ibm-pc-vga','win');
    if(kind==='minecraft')return openSession('modern','minecraft');
    notice('Unknown Terminal mode: '+String(kind));
  };
  // Each IF story is a separate Session tab. The interpreter writes only to
  // its own detached output, and receives input only through its own private
  // textarea. The visible Terminal output and textarea are a presentation of
  // the ACTIVE tab, never a global owner of a Z-machine process.
  const terminalScreen=$('terminal-screen');
  const scrollToLatest=()=>{terminalScreen.scrollTop=terminalScreen.scrollHeight;};
  const outputObserver=new MutationObserver(()=>scrollToLatest());
  outputObserver.observe($('terminal-output'),{subtree:true,childList:true,characterData:true});
  const activeGame=()=>activeTab?.game||null;
  function syncGameInput(){
    const output=$('terminal-output'),input=$('terminal-input'),current=activeGame();
    if(!current?.io?.lineResolver){output.removeAttribute('data-if-echo');return;}
    current.draft=input.value;
    const value=current.draft;
    const caret=Math.max(0,Math.min(input.selectionStart??value.length,value.length));
    output.setAttribute('data-if-echo',value.slice(0,caret)+'▌'+value.slice(caret));
    scrollToLatest();
  }
  function paintIFTab(tab){
    if(tab.closed || activeTab!==tab)return;
    const target=$('terminal-output');
    if(tab.buffer.childNodes && typeof tab.buffer.cloneNode==='function')
      target.replaceChildren(...Array.from(tab.buffer.childNodes,node=>node.cloneNode(true)));
    else target.textContent=tab.buffer.textContent;
    const current=tab.game;
    $('terminal-input').disabled=!current?.io;
    $('terminal-shell').textContent='Interactive Fiction · '+tab.label;
    notice(current?.statusElement.textContent||tab.statusText||('Playing '+tab.label));
    syncGameInput();
    scrollToLatest();
  }
  function scheduleIFPaint(tab){
    if(tab.paintPending || tab.closed)return;
    tab.paintPending=true;
    requestAnimationFrame(()=>{tab.paintPending=false;paintIFTab(tab);});
  }
  async function stopGame(tab,notifyServer=true){
    const current=tab?.game;
    if(!current)return;
    tab.game=null;
    current.abort.abort();
    current.releaseDisplay?.();
    current.observer?.disconnect();
    if(current.io?.lineResolver){
      try{current.io.lineResolver('quit');}catch(_){/* already exited */}
    }
    tab.finished=true;
    tab.statusText=tab.label+' stopped · close this tab to return to the shell';
    tab.buffer.append(document.createTextNode('\n[IF] '+tab.label+' stopped\n'));
    if(activeTab===tab){
      $('terminal-input').value='';
      paintIFTab(tab);
      $('terminal-output').removeAttribute('data-if-echo');
    }
    if(notifyServer){
      try{await api('/api/if/stop',{command:current.name,launch_id:current.launchId});}
      catch(error){if(activeTab===tab)notice('IF stop: '+error.message);}
    }
  }
  async function openIFTab(name,launchId,sourceSession){
    // A story can be requested twice while already open. Re-select its tab;
    // never bind a second VM to the same named autosave or another shell.
    const existing=tabs.find(tab=>!tab.closed && tab.kind==='if' && tab.game?.name===name);
    if(existing){await activateTab(existing);return existing;}
    const buffer=makeBuffer();
    const tab={key:++tabSerial,label:name,profileId:'modern',backend:'if',
      kind:'if',session:null,closed:false,display:new VTOutput(buffer),buffer,
      statusText:'Loading '+name+'…',paintPending:false,game:null};
    const current={name,launchId,sourceSession,io:null,draft:'',
      abort:new AbortController(),inputElement:document.createElement('textarea'),
      statusElement:document.createElement('span'),releaseDisplay:null,observer:null};
    tab.game=current;
    tab.buffer.textContent='[IF] Loading '+name+'…\n';
    current.observer=new MutationObserver(()=>scheduleIFPaint(tab));
    current.observer.observe(tab.buffer,{subtree:true,childList:true,characterData:true});
    // If the user already switched away from the requesting shell, do not
    // steal focus when the deferred server event finally arrives.
    const select=!sourceSession || activeTab?.session?.id===sourceSession;
    tabs.push(tab);
    if(select)await activateTab(tab);
    else renderTabs();
    void startGame(tab,current);
    return tab;
  }
  async function startGame(tab,current){
    const name=current.name;
    const alive=()=>!tab.closed && tab.game===current;
    const output=tab.buffer;
    const saveURL='/api/if/save/'+encodeURIComponent(name);
    try{
      const response=await fetch('/api/if/story/'+encodeURIComponent(name),
        {headers:{'X-CPH-Token':token},cache:'no-store',signal:current.abort.signal});
      if(!alive())return;
      if(!response.ok)throw new Error((await response.json()).error||'Story download failed');
      const story=await response.arrayBuffer();
      const [{ZMachine},{WebIOAdapter},quetzal]=await Promise.all([
        import('/zmachine/index.js'),import('/zmachine/web/WebIOAdapter.js'),
        import('/zmachine/core/state/Quetzal.js')]);
      if(!alive())return;
      const auth={'X-CPH-Token':token};
      // Save storage is optional, and a bad save must never strand the tab.
      const storyHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',story)),
        byte=>byte.toString(16).padStart(2,'0')).join('');
      if(!alive())return;
      let old={found:false,story_sha256:storyHash};
      let saveWarning='';
      try{
        const oldResponse=await fetch(saveURL,{headers:auth,cache:'no-store',signal:current.abort.signal});
        if(!alive())return;
        if(!oldResponse.ok){
          const message=await oldResponse.json().catch(()=>({}));
          throw new Error(message.error||'HTTP '+oldResponse.status);
        }
        old=await oldResponse.json();
        if(old.story_sha256!==storyHash)throw new Error('Story hash mismatch');
      }catch(error){
        if(!alive())return;
        saveWarning='[IF] Existing autosave unavailable: '+error.message+'; starting fresh\n';
      }
      if(!alive())return;
      const vmState=old.found?old.save.vm_state:null;
      const base64=bytes=>{
        let result='';for(let i=0;i<bytes.length;i+=8192)
          result+=String.fromCharCode(...bytes.subarray(i,i+8192));
        return btoa(result);
      };
      const unbase64=value=>{
        if(typeof value!=='string'||value.length>12*1024*1024||
           !/^[A-Za-z0-9+/]*={0,2}$/.test(value))throw new Error('Invalid Quetzal bytes');
        return Uint8Array.from(atob(value),c=>c.charCodeAt(0));
      };
      const io=new WebIOAdapter({outputElement:output,inputElement:current.inputElement,
        statusElement:current.statusElement});
      current.io=io;
      let vm=ZMachine.load(story,io);
      io.initialize?.(vm.version);
      if(saveWarning)output.textContent+=saveWarning;
      if(vmState){
        try{
          if(vmState.schema!=='cinterforge.if.quetzal.v1')throw new Error('Unknown Quetzal save');
          const parsed=quetzal.parseQuetzalSave(unbase64(vmState.data_base64));
          if(!quetzal.verifySaveCompatibility(parsed,vm.memory))
            throw new Error('Save does not match the installed story');
          const dynamicSize=Math.min(parsed.dynamicMemory.length,vm.memory.staticBase);
          for(let i=0;i<dynamicSize;i++)vm.memory.writeByte(i,parsed.dynamicMemory[i]);
          vm.stack.restore(parsed.callStack);
          vm._pc=parsed.gameId.pc;
          vm.executor.randomMode=String(vmState.random_mode||'random');
          vm.executor.randomConfiguredSeed=vmState.random_configured_seed===null?null:
            Number(vmState.random_configured_seed);
          vm.executor.randomXorshiftState=Number(vmState.random_xorshift_state||1)>>>0;
          output.textContent+='[IF] Restored '+name+' autosave\n';
        }catch(error){
          output.textContent+='[IF] Restore failed: '+error.message+'; starting fresh\n';
          vm=ZMachine.load(story,io);
          io.initialize?.(vm.version);
          output.textContent+='[IF] Restore unavailable; started new game\n';
        }
      }
      let boundary=0;
      let saving=Promise.resolve();
      const readLine=io.readLine.bind(io);
      io.readLine=(...args)=>{
        const pending=readLine(...args);
        const serial=++boundary;
        queueMicrotask(()=>{
          saving=saving.then(async()=>{
            if(!alive() || !io.lineResolver || serial!==boundary)return;
            const bytes=quetzal.createQuetzalSave(vm.memory,vm.stack.snapshot(),vm.pc);
            const random=vm.executor;
            const snapshot={schema:'cinterforge.if.quetzal.v1',
              data_base64:base64(bytes),random_mode:String(random.randomMode||'random'),
              random_configured_seed:random.randomConfiguredSeed,
              random_xorshift_state:random.randomXorshiftState>>>0,
              native_save_base64:null};
            const card={schema:'cinterforge.conv.if.v1',card_type:'CONV',
              story:name,interpreter:'zmachine',story_sha256:old.story_sha256,
              vm_state:snapshot,session_state:{},updated_at:new Date().toISOString()};
            const result=await fetch(saveURL,{method:'POST',headers:{...auth,
              'Content-Type':'application/json'},body:JSON.stringify(card)});
            if(!result.ok)throw new Error('HTTP '+result.status);
          }).catch(error=>{
            if(alive()){output.textContent+='\n[IF] Autosave failed: '+error.message+'\n';scheduleIFPaint(tab);}
          });
        });
        return pending;
      };
      // Preserve the original atomic initial/restored paint behavior.
      output.style.visibility='hidden';
      const releaseDisplay=()=>{
        if(current.releaseDisplay!==releaseDisplay)return;
        current.releaseDisplay=null;
        output.style.visibility='';
        scheduleIFPaint(tab);
      };
      current.releaseDisplay=releaseDisplay;
      const readyReadLine=io.readLine.bind(io);
      io.readLine=(...args)=>{
        const pending=readyReadLine(...args);
        if(alive()){
          current.statusElement.textContent='Playing '+name+' · Ctrl+C to exit';
          if(activeTab===tab){$('terminal-input').disabled=false;focusTerminal();syncGameInput();}
        }
        requestAnimationFrame(releaseDisplay);
        return pending;
      };
      if(!alive())return;
      try{await vm.run();}finally{releaseDisplay();}
    }catch(error){
      if(alive() && error.name!=='AbortError'){
        output.textContent+='\n[IF] '+error.message+'\n';
        scheduleIFPaint(tab);
      }
    }finally{
      if(alive())await stopGame(tab);
    }
  }

  const notice = text => {$('terminal-message').textContent=text;};
  const applyProfile = profile => {
    const style = $('terminal-overlay').style;
    style.setProperty('--bg',profile.background);
    style.setProperty('--fg',profile.foreground);
    style.setProperty('--accent',profile.accent);
    style.setProperty('--columns',String(profile.columns||80));
    $('terminal-overlay').dataset.profile=profile.id;
    $('terminal-machine-title').textContent=profile.name.toUpperCase();
    if(!acceptUI)localStorage.setItem('cinterterminal.profile.'+platform,profile.id);
  };
  const terminalSize = () => {
    const el=$('terminal-screen');
    return {cols:Math.max(40,Math.floor(el.clientWidth/8.5)),rows:Math.max(12,Math.floor(el.clientHeight/19))};
  };
  async function pollTab(tab){
    const id=tab.session?.id;
    while(id && !tab.closed && tab.session?.id===id){
      try{
        const result=await api('/api/terminal/poll',{id,wait_ms:300});
        if(tab.closed || tab.session?.id!==id)return;
        if(result.output){
          const bytes=decode(result.output);
          tab.display.push(bytes);
        }
        if(result.bedrock_eula_requested && activeTab===tab)requestLicense();
        if(result.closed){
          // Release the host-side PTY slot after shutdown, while preserving
          // this tab's transcript for the player to inspect.
          if(tab.kind==='bedrock')void api('/api/terminal/close',{id}).catch(()=>{});
          if(activeTab===tab)notice(tab.kind==='bedrock'?'Bedrock server exited':'Shell exited');
          tab.session=null;if(activeTab===tab)session=null;
          renderTabs();return;
        }
      }catch(e){
        if(tab.closed || tab.session?.id!==id)return;
        if(activeTab===tab)notice(e.message);
        tab.session=null;if(activeTab===tab)session=null;
        renderTabs();return;
      }
    }
  }
  async function openSession(profileId,backend='native'){
    // Do not drop a native/File-menu request while the initial PTY is still
    // connecting. Windows WebView2 can deliver the click during first startup.
    if(waiting){
      return new Promise((resolve,reject)=>queuedSessionOpens.push({profileId,backend,resolve,reject}));
    }
    if(backend==='minecraft'){
      // Menu and API both gate the attachment: never start a Bedrock VM here.
      try {
        const state=await api('/api/bedrock/status');
        if(!(state.running || state.starting)){notice('Minecraft is disabled until bedrock run starts a server');return;}
      } catch(error){notice('Minecraft unavailable: '+error.message);return;}
    }
    waiting=true;
    const profile=profiles.find(p=>p.id===profileId)||profiles[0];
    if(!profile){waiting=false;notice('No terminal profiles available');return;}
    const buffer=makeBuffer();
    const tab={key:++tabSerial,label:backend==='minecraft'?'Minecraft':
        backend==='linux'?'Linux Bash':backend==='win'?'Windows Bash':
        museumKind(profile)==='joshua'?'JOSHUA':profile.name,
      profileId:profile.id,backend,
      kind:museumKind(profile),session:null,closed:false,
      display:new VTOutput(buffer),buffer};
    // Every tab owns its renderer and shell, but only the active tab renders
    // into Kevin's existing screen and receives keyboard input.
    tab.display.target=tab.buffer;
    tabs.push(tab);
    if(activeTab?.kind==='if' && activeTab.game)activeTab.game.draft=$('terminal-input').value;
    if(activeTab && activeTab.kind!=='if'){
      activeTab.display.target=activeTab.buffer;activeTab.display.render();
    }
    activeTab=tab;session=null;selected=profile.id;display=tab.display;
    $('terminal-input').value='';$('terminal-output').removeAttribute('data-if-echo');
    display.target=$('terminal-output');display.render();renderTabs();
    try{
      if(!acceptUI && !profile.lagomorph && !profile.macGame && tab.kind==='terminal')
        if(!acceptUI)localStorage.setItem('cinterterminal.profile.'+platform,profile.id);
      if(tab.kind!=='terminal'){
        notice('Opening '+profile.name+' emulator…');
        await showComputer(profile);
        $('terminal-overlay').dataset.profile=profile.id;
        $('terminal-machine-title').textContent=profile.name.toUpperCase();
        $('terminal-shell').textContent=`${profile.year} · ${profile.name}`;
        await nativeEvent('machine',{id:profile.id,kind:tab.kind});
        return;
      }
      showTerminal();applyProfile(profile);
      notice('Connecting to '+profile.name+' session…');
      const created=await api('/api/terminal/open',{...terminalSize(),profile:profile.id,backend});
      if(tab.closed){
        await api('/api/terminal/close',{id:created.id}).catch(()=>{});
        return;
      }
      tab.session=created;
      if(activeTab===tab){
        session=created;
        $('terminal-shell').textContent=`${created.platform} · ${created.shell}`;
        notice('Connected · type help for CinterForgeTerminal commands');
        $('terminal-input').focus();
      }
      await nativeEvent('terminal',{id:profile.id,kind:'terminal'});
      void pollTab(tab);
    }catch(e){
      if(activeTab===tab){notice(e.message);tab.display.push(e.message+'\n');}
      await nativeEvent('error',{message:e.message});
    }finally{
      waiting=false;renderTabs();
      const next=queuedSessionOpens.shift();
      if(next)void openSession(next.profileId,next.backend).then(next.resolve,next.reject);
    }
  }
  // A `bedrock run <world>` command in any regular shell asks the local
  // host to create a separate PTY. Adopt that exact Session, never start a
  // second shell and never recycle the initiating tab.
  async function adoptBedrockSession(created){
    if(!created?.id || tabs.some(tab=>tab.session?.id===created.id)){
      const existing=tabs.find(tab=>tab.session?.id===created?.id);
      if(existing)await activateTab(existing);
      return;
    }
    const buffer=makeBuffer();
    const tab={key:++tabSerial,label:'Bedrock',profileId:'modern',
      kind:'bedrock',session:created,closed:false,display:new VTOutput(buffer),buffer};
    tab.display.target=tab.buffer;
    tabs.push(tab);
    await activateTab(tab);
    $('terminal-shell').textContent='Bedrock · live server console';
    notice('Bedrock has its own live two-way terminal; the previous session is unchanged');
    void pollTab(tab);
  }
  let adoptingBedrock=false;
  setInterval(async()=>{
    if(adoptingBedrock || document.visibilityState==='hidden')return;
    adoptingBedrock=true;
    try{
      const created=await api('/api/bedrock/terminal/next');
      if(created?.id)await adoptBedrockSession(created);
    }catch(_){ /* Browser or host may be shutting down. */ }
    finally{adoptingBedrock=false;}
  },500);
  async function reconnectTab(){
    if(!activeTab){await openSession('modern');return;}
    const tab=activeTab;
    if(tab.kind==='server'){
      tab.pollGeneration++;
      tab.cursor=0;tab.display.reset();
      void pollServerTab(tab);
      return;
    }
    if(tab.kind==='bedrock'){
      notice('Bedrock is a dedicated server Session. Start a new world with bedrock run from a regular terminal.');
      return;
    }
    if(tab.kind==='if'){
      notice(tab.game ? 'Story is running in its own tab' : 'Story ended. Launch it again from a shell.');
      return;
    }
    if(tab.kind!=='terminal'){
      await showComputer(profiles.find(p=>p.id===tab.profileId));return;
    }
    if(tab.session)try{await api('/api/terminal/close',{id:tab.session.id});}catch(_){/* closed */}
    tab.session=null;session=null;tab.display.reset();
    try{
      const created=await api('/api/terminal/open',{...terminalSize(),profile:tab.profileId,backend:tab.backend||'native'});
      tab.session=created;session=created;
      $('terminal-shell').textContent=`${created.platform} · ${created.shell}`;
      notice('Connected');void pollTab(tab);focusTerminal();
    }catch(error){notice(error.message);}
  }
  async function send(data){if(activeTab?.kind==='if')return;if(session)try{await api('/api/terminal/input',{id:session.id,data});}catch(e){notice(e.message);}}
  async function debuggerRequest(){
    try{await api('/api/debugger',{});notice('Inspector requested; available when the native Host enables it.');}
    catch(e){notice(e.message);}
  }
  const overlay=$('terminal-overlay');
  const input=$('terminal-input');
  const focusTerminal=()=>{if((session||activeGame()) && !overlay.hidden && !input.disabled)input.focus();};
  // Native Edit uses the existing session transcript and input bridge.
  // Copy uses the entire committed session
  // transcript, never the hidden input textarea or an emulator's document.
  const currentTranscript=()=>activeTab?.kind==='if' ? (activeTab.buffer.textContent||'') : display.transcript();
  const copyTranscript=async()=>{
    const transcript=currentTranscript();
    // In WebView2, execCommand works synchronously during a real keyboard
    // gesture even when navigator.clipboard permission is unavailable.
    const legacyCopy=()=>{
      const field=document.createElement('textarea');
      field.value=transcript;field.setAttribute('readonly','');
      field.style.cssText='position:fixed;left:-10000px;top:0';
      document.body.append(field);field.select();
      try{return document.execCommand('copy');}
      finally{field.remove();}
    };
    try{
      if(platform==='win' && legacyCopy()){
        notice('Copied terminal transcript');return transcript;
      }
      if(!navigator.clipboard?.writeText)throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(transcript);
    }catch(error){
      // Retain the existing WKWebView/WebKitGTK fallback.
      if(!legacyCopy()){notice('Copy unavailable: '+error.message);return false;}
    }
    notice('Copied terminal transcript');
    return transcript;
  };
  const pasteIntoSession=text=>{
    if((!session && !activeGame()) || computerView.hidden===false)return false;
    text=String(text ?? '');if(!text)return false;
    if(activeGame()){
      input.focus();
      input.setRangeText(text,input.selectionStart,input.selectionEnd,'end');
      input.dispatchEvent(new Event('input',{bubbles:true}));
    }else{void send(text);focusTerminal();}
    return true;
  };
  const pasteClipboard=async()=>{
    try{
      // WebView2 does not consistently grant navigator.clipboard.readText
      // to the application's loopback page. The native Windows clipboard
      // bridge is read-only and remains local to the user's installed app.
      if(platform==='win' && typeof window.cinterClipboard==='function')
        return pasteIntoSession(await window.cinterClipboard({action:'paste'}));
      if(navigator.clipboard?.readText)
        return pasteIntoSession(await navigator.clipboard.readText());
      notice('Paste from your keyboard with Ctrl+V');return false;
    }catch(error){notice('Paste unavailable: '+error.message);return false;}
  };
  window.CinterEdit=Object.freeze({copy:copyTranscript,paste:pasteClipboard,insert:pasteIntoSession,transcript:currentTranscript});

  // Windows keeps Ctrl+C for interrupting the running Bash process. Use
  // Ctrl+Shift+C for Copy, and Ctrl+V / Ctrl+Shift+V for Paste. Capture before
  // the PTY control-key handler so these combinations never send ^C or ^V.
  document.addEventListener('keydown',event=>{
    if(platform!=='win' || !event.ctrlKey || event.metaKey || event.altKey ||
       event.isComposing || computerView.hidden===false)return;
    const key=event.key.toLowerCase();
    if(key==='c' && event.shiftKey){
      event.preventDefault();event.stopImmediatePropagation();void copyTranscript();
    }else if(key==='v' && (typeof window.cinterClipboard==='function')){
      event.preventDefault();event.stopImmediatePropagation();void pasteClipboard();
    }
    // Browser previews without the native bridge retain Ctrl+V's default
    // textarea paste event. The input listener sends the pasted text once.
  },true);

  // All platforms receive both mouse and keyboard input inside the WebView.
  // The machine dropdown remains available even when an emulator owns focus.
  const keys={Enter:'\r',Escape:'\x1b',Backspace:'\x7f',Tab:'\t',ArrowUp:'\u001b[A',ArrowDown:'\u001b[B',ArrowRight:'\u001b[C',ArrowLeft:'\u001b[D',Home:'\u001b[H',End:'\u001b[F',Delete:'\u001b[3~',ControlC:'\x03'};
  const forwardKey=(name)=>{
    if(activeGame()){
      if(name==='ControlC'){void closeTab(activeTab);return true;}
      if(name==='Enter'){input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return true;}
      return false;
    }
    const code=keys[name];if(code && session){send(code);return true;}return false;
  };
  for(const button of ($('terminal-keys').querySelectorAll?.('button') || [])){
    button.addEventListener('click',()=>{forwardKey(button.dataset.terminalKey);focusTerminal();});
  }
  $('terminal-reconnect').addEventListener('click',()=>{void reconnectTab();});
  input.addEventListener('wheel',e=>{$('terminal-screen').scrollTop+=e.deltaY;e.preventDefault();},{passive:false});
  // Keyboard shortcuts work if the screen (rather than the textarea) holds focus.
  document.addEventListener('keydown',e=>{
    if(!session || activeTab?.kind==='if' || computerView.hidden===false || e.isComposing || e.target===input)return;
    if(e.target && ['SELECT','BUTTON','SUMMARY','INPUT','TEXTAREA'].includes(e.target.tagName))return;
    const ctrl=e.ctrlKey && !e.altKey && e.key.length===1 ? String.fromCharCode(e.key.toUpperCase().charCodeAt(0)&31):null;
    const special=keys[e.key];
    const printable=!e.ctrlKey&&!e.metaKey&&!e.altKey&&e.key.length===1?e.key:null;
    if(ctrl||special||printable){e.preventDefault();send(ctrl||special||printable);}
  });
  document.addEventListener('paste',e=>{
    if(!session || activeTab?.kind==='if' || computerView.hidden===false || e.target===input)return;
    const text=e.clipboardData?.getData('text/plain');
    if(text){e.preventDefault();send(text);}
  });
  $('terminal-screen').addEventListener('click',focusTerminal);
  // The Minecraft menu must never imply an absent server. The attached
  // console uses the already-running Bedrock VM and never starts another.
  let minecraftMenuState=null;
  const updateMinecraftMenu=async()=>{
    let enabled=false;
    try{const state=await api('/api/bedrock/status');enabled=!!(state.running||state.starting);}
    catch(_){/* no installed Bedrock manager */}
    if(enabled!==minecraftMenuState){minecraftMenuState=enabled;
      const windowsItem=$('windows-new-minecraft');
      if(windowsItem)windowsItem.disabled=!enabled;
      void nativeEvent('minecraft-menu',{enabled});
    }
  };
  void updateMinecraftMenu();
  setInterval(()=>{if(document.visibilityState!=='hidden')void updateMinecraftMenu();},1600);
  // Kevin's installed Terminals selector remains in the existing titlebar.
  const titleMenus=Array.from(document.querySelectorAll('.terminal-menu'));
  const setWindowsApplicationMenus=()=>{
    const isWindows=platform==='win';
    const showMenus=isWindows || platform==='mac';
    for(const id of ['windows-file-menu','windows-edit-menu']){
      const menu=$(id);if(menu){menu.hidden=!showMenus;if(!showMenus)menu.open=false;}
    }
    // The File menu mirrors the macOS entries, but unavailable guest-native
    // shells on Windows must not masquerade as runnable native Bash sessions.
    for(const button of document.querySelectorAll('[data-file-new]')){
      const kind=button.dataset.fileNew;
      if(kind==='mac'||kind==='linux')button.disabled=isWindows;
      if(kind==='minecraft')button.disabled=!minecraftMenuState;
    }
  };
  for(const button of document.querySelectorAll('[data-file-new]')){
    button.addEventListener('click',()=>{
      titleMenus.forEach(menu=>menu.open=false);
      void window.CinterTerminalNewTab(button.dataset.fileNew);
    });
  }
  for(const button of document.querySelectorAll('[data-file-action]')){
    button.addEventListener('click',()=>{
      titleMenus.forEach(menu=>menu.open=false);
      if(button.dataset.fileAction==='debugger')void debuggerRequest();
      else if(button.dataset.fileAction==='quit'){
        if(typeof window.cinterWindow==='function')void window.cinterWindow({action:'quit'});
        else notice('Quit is available in the installed Terminal application');
      }
    });
  }
  for(const button of document.querySelectorAll('[data-edit-action]')){
    button.addEventListener('click',()=>{
      titleMenus.forEach(menu=>menu.open=false);
      if(button.dataset.editAction==='copy')void copyTranscript();
      else if(button.dataset.editAction==='paste')void pasteClipboard();
    });
  }
  titleMenus.forEach(menu=>menu.addEventListener('toggle',()=>{
    if(menu.open)titleMenus.forEach(other=>{if(other!==menu)other.open=false;});
  }));
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape' && titleMenus.some(menu=>menu.open)){
      event.preventDefault();titleMenus.forEach(menu=>menu.open=false);focusTerminal();
    }
  });
  const openMuseumProfile=id=>{
    titleMenus.forEach(menu=>menu.open=false);
    void openSession(id);
  };
  const addMuseumEntry=(target,profile)=>{
    const button=document.createElement('button');button.type='button';
    button.className = 'museum-entry';
    button.textContent=`${profile.year} ${profile.name}`;
    appendDecorativeIcon(button, iconCategoryForProfile(profile));
    button.addEventListener('click',()=>openMuseumProfile(profile.id));target.append(button);
  };
  // Input events carry printable text; keydown handles control sequences.
  // Do not dispatch incomplete IME compositions as shell commands.
  input.addEventListener('input',e=>{
    if(e.isComposing)return;
    if(activeGame()){syncGameInput();return;}
    if(input.value){const value=input.value;input.value='';send(value);}
  });
  input.addEventListener('compositionend',()=>{
    if(activeGame()){syncGameInput();return;}
    if(input.value){const value=input.value;input.value='';send(value);}
  });
  // Cursor positioning is also visible when navigating/editing within a line.
  input.addEventListener('keyup',()=>{if(activeGame())syncGameInput();});
  input.addEventListener('click',()=>{if(activeGame())syncGameInput();});
  input.addEventListener('select',()=>{if(activeGame())syncGameInput();});
  // Capture IF editing keys before the interpreter's input listener. The
  // transparent textarea can be select-all after focus/restore; native
  // Backspace would erase the entire draft instead of one character.
  input.addEventListener('keydown',e=>{
    if(!activeGame()?.io?.lineResolver || e.isComposing || e.key==='Process')return;
    if(e.key!=='Backspace' && e.key!=='Delete')return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const value=input.value;
    const start=input.selectionStart ?? value.length;
    const end=input.selectionEnd ?? start;
    const caret=Math.max(0,Math.min(end>start ? end : start,value.length));
    // A selected range is not permission to discard the entire command.
    // Delete precisely one Unicode character, including surrogate pairs.
    if(e.key==='Backspace' && caret>0){
      const previous=Array.from(value.slice(0,caret)).slice(-1)[0] || '';
      input.setRangeText('',caret-previous.length,caret,'start');
    } else if(e.key==='Delete' && caret<value.length){
      const next=Array.from(value.slice(caret))[0] || '';
      input.setRangeText('',caret,caret+next.length,'start');
    }
    syncGameInput();
  },true);
  input.addEventListener('keydown',e=>{
    if(e.isComposing || e.key==='Process')return;
    // File accelerators must not leak Ctrl+N/Ctrl+Q to the shell PTY.
    if(e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey &&
       ['n','q'].includes(e.key.toLowerCase()))return;
    const current=activeGame();
    if(current){
      if(e.ctrlKey && e.key.toLowerCase()==='c'){
        e.preventDefault();e.stopImmediatePropagation();void closeTab(activeTab);
      }else if(e.key==='Enter'){
        e.preventDefault();
        if(current.io?.lineResolver){
          // Only the active story's WebIOAdapter receives this command.
          current.inputElement.value=input.value;
          current.inputElement.dispatchEvent(new KeyboardEvent('keydown',
            {key:'Enter',bubbles:false}));
          input.value='';current.draft='';syncGameInput();
        }
      }else if(current.io && e.key!=='Backspace' && e.key!=='Delete'){
        // Forward readChar / history keys to this story only. Normal text
        // editing remains in the visible input and never reaches another VM.
        current.inputElement.value=input.value;
        current.inputElement.dispatchEvent(new KeyboardEvent('keydown',
          {key:e.key,code:e.code,ctrlKey:e.ctrlKey,shiftKey:e.shiftKey,
           altKey:e.altKey,metaKey:e.metaKey,bubbles:false}));
      }
      return;
    }
    const special=keys;
    const ctrl=e.ctrlKey&&e.key.length===1 ? String.fromCharCode(e.key.toUpperCase().charCodeAt(0)&31) : null;
    const code=ctrl||special[e.key];
    if(code){e.preventDefault();send(code);}
  });
  const license=$('license-dialog'), accepted=$('license-accept');
  let licensePending=false;
  const requestLicense = () => {
    // The status endpoint is polled repeatedly. Never clear a user's consent
    // checkbox while the dialog is already open.
    if(!license.hidden)return;
    accepted.checked=false;
    $('license-confirm').disabled=true;
    license.hidden=false;
    accepted.focus();
  };
  $('license-accept').addEventListener('change',()=>{$('license-confirm').disabled=!accepted.checked;});
  $('license-cancel').addEventListener('click',async()=>{
    if(licensePending)return;
    license.hidden=true;
    try{await api('/api/bedrock/eula/ack',{});}catch(e){notice(e.message);}
    focusTerminal();
  });
  // Installation status is only polled AFTER a user explicitly accepts the
  // license; never make Bedrock a startup component or a management panel.
  const monitorBedrockInstall = async () => {
    let cursor=0,lastUpdate=Date.now();
    try{
      for(;;){
        const events=await api('/api/bedrock/events?cursor='+cursor);
        for(const event of events.events||[]){
          if(event.id<=cursor)continue;
          cursor=event.id;lastUpdate=Date.now();notice(event.message);
          display.push('\r\n[bedrock] '+event.message+'\r\n');
        }
        if(events.log_error)throw new Error('Host log write failed: '+events.log_error);
        const state=await api('/api/bedrock/status');
        if(Date.now()-lastUpdate>8000){
          lastUpdate=Date.now();
          display.push('\r\n[bedrock] Awaiting download response. Host log: '+(events.path||state.log_path)+'\r\n');
        }
        if(state.install_error)throw new Error(state.install_error);
        if(state.installed){notice('Bedrock downloaded and installed. Type bedrock start.');return;}
        if(!state.installing)throw new Error('Bedrock installation stopped before completion');
        await new Promise(resolve=>setTimeout(resolve,750));
      }
    }catch(e){notice('Bedrock install failed: '+e.message);display.push('\r\n[bedrock] '+e.message+'\r\n[bedrock] Type bedrock log for details.\r\n');}
  };
  $('license-confirm').addEventListener('click',async()=>{
    if(!accepted.checked || licensePending)return;
    licensePending=true;
    $('license-confirm').disabled=true;
    $('license-cancel').disabled=true;
    try{
      await api('/api/bedrock/accept',{accepted:true});
      license.hidden=true;
      notice('EULA accepted. Downloading and installing official Bedrock server…');
      display.push('\r\n[bedrock] Downloading and installing official Bedrock server…\r\n');
      focusTerminal();
      void monitorBedrockInstall();
    }catch(e){notice(e.message);}
    finally{licensePending=false;$('license-cancel').disabled=false;$('license-confirm').disabled=!accepted.checked;}
  });
  // A typed story command creates its OWN tab. Source PTYs and existing
  // games remain usable. Serial polling preserves start/stop event order.
  let pollingIF=false;
  setInterval(async()=>{
    if(document.visibilityState==='hidden' || pollingIF)return;
    pollingIF=true;
    try{
      const action=await api('/api/if/next');
      if(action.start){
        await openIFTab(action.start,action.launch_id,action.session_id);
      }else if(action.stop){
        const tab=tabs.find(tab=>tab.kind==='if' &&
          tab.game?.name===action.stop &&
          (!action.launch_id || tab.game.launchId===action.launch_id));
        if(tab)await stopGame(tab,false);
      }
    }catch(error){notice('IF: '+error.message);}
    finally{pollingIF=false;}
  },800);
  // Every typed `wargames` command uses the same Session path as the old
  // menu action. The JOSHUA application and iframe are unchanged.
  setInterval(async()=>{
    if(document.visibilityState==='hidden')return;
    try{
      const result=await api('/api/wargames/next');
      if(result.open)await openSession('joshua');
    }catch(error){notice('WarGames: '+error.message);}
  },850);
  // Macintosh games use the existing emulator viewer. Their library records
  // do not supply verified guest application paths, so direct auto-open is
  // experimental: boot Infinite HD, then curate per-game startup recipes.
  setInterval(async()=>{
    if(document.visibilityState==='hidden')return;
    try{
      const result=await api('/api/macgames/next');
      if(!result.game)return;
      const game=macCatalog.get(result.game);
      if(!game)throw new Error('Unknown Macintosh game: '+result.game);
      const machine=game.needs_mac_os_x?'Power Macintosh G4 (PCI Graphics)':'Quadra 650';
      const disk=game.needs_mac_os_x?'Mac OS 9.2':'Mac OS 7.6';
      profiles=profiles.filter(profile=>profile.id!=='macgame-'+result.game);
      profiles.push({id:'macgame-'+result.game, name:game.title,
        machine,disk,kind:'emulator',year:1993,macGame:true});
      await openSession('macgame-'+result.game);
    }catch(error){notice('Mac game: '+error.message);}
  },850);
  // Hidden shell command: never put the private profile into a visible menu.
  setInterval(async()=>{
    if(document.visibilityState==='hidden')return;
    try{
      const result=await api('/api/lagomorph/next');
      if(result.open && result.profile==='quadra-650'){
        
        await openSession(result.profile);
      }
    }catch(_){ /* local service may be shutting down */ }
  },850);
  (async()=>{
    try{
      const status=await api('/api/status');platform=status.platform;
      setWindowsApplicationMenus();

      const data=await (await fetch('/terminal_styles.json',{cache:'no-store'})).json();
      profiles=[...(data.profiles||[]),...(data.emulators||[]),data.codeoss,
        {id:'joshua',name:'JOSHUA',year:2026,kind:'joshua'}].filter(Boolean);
      const macData=await (await fetch('/mac_games.json',{cache:'no-store'})).json();
      macCatalog=new Map((macData.games||[]).map(game=>[game.command,game]));
      const byId=new Map(profiles.map(profile=>[profile.id,profile]));
      const stored=localStorage.getItem('cinterterminal.profile.'+platform);
      const initial=stored==='quadra-650' ? data.defaults[platform] : (stored||data.defaults[platform]);
      // Normal startup keeps the default Session non-blocking while the
      // Terminals list initializes.  Native acceptance must NOT start that
      // concurrent PTY: openSession has a single-open guard, and an in-flight
      // Apple II connection previously made its VT100 probe a false failure.
      const initialId=byId.has(initial)?initial:data.defaults[platform];
      if(!acceptUI)void openSession(initialId);
      // Historical categories are four independent menus; don't flatten
      // Macintosh, BeOS and NeXTSTEP into Kevin's original Terminals list.
      const menuLists={terminal:$('terminal-menu-list'),macintosh:$('macintosh-menu-list'),
        beos:$('beos-menu-list'),nextstep:$('nextstep-menu-list')};
      if(Object.values(menuLists).some(list=>!list))throw new Error('Museum menus missing');
      Object.values(menuLists).forEach(list=>list.replaceChildren());
      const serverEntry=document.createElement('button');serverEntry.type='button';
      serverEntry.className='museum-entry';serverEntry.textContent='Server · Live log';
      appendDecorativeIcon(serverEntry,'server');
      serverEntry.addEventListener('click',()=>{
        titleMenus.forEach(menu=>menu.open=false);
        void openServerTab();
      });
      menuLists.terminal.append(serverEntry);
      const groupOf=profile=>{
        if(profile.kind!=='emulator')return 'terminal';
        if(profile.id.startsWith('beos-'))return 'beos';
        if(profile.id.startsWith('next-'))return 'nextstep';
        return 'macintosh';
      };
      const displayed=new Set();
      const available=[...(data.profiles||[]),...(data.museum||[]).map(item=>byId.get(item.id)),
        ...(data.emulators||[]),data.codeoss].filter(Boolean);
      available.sort((a,b)=>(a.year||0)-(b.year||0)||a.name.localeCompare(b.name));
      for(const profile of available){
        if(!profile || displayed.has(profile.id) || profile.lagomorph)continue;
        displayed.add(profile.id);
        addMuseumEntry(menuLists[groupOf(profile)],profile);
      }
      await nativeEvent('ready',{menu:(data.museum||[]).length,profile:initialId});
      if(acceptUI){
        // Exercise two real Sessions sequentially, using a public museum Mac
        // rather than the private Lagomorph Quadra.  No background default
        // PTY competes with the native VT100 callback.
        for(const id of ['mac-plus','vt100-terminal']){
          if(!byId.has(id))throw new Error('Missing museum entry: '+id);
          await openSession(id);
          if(activeTab?.profileId!==id)
            throw new Error('Museum Session did not open: '+id);
          if(id==='vt100-terminal' && (!session?.id || activeTab.session!==session))
            throw new Error('VT100 PTY did not open');
          if(id==='mac-plus' && computerView.hidden)
            throw new Error('Mac Plus emulator Session did not open');
        }
      }
    }catch(e){notice(e.message);await nativeEvent('error',{message:e.message});}
  })();
  window.addEventListener('resize',()=>{if(session){const size=terminalSize();api('/api/terminal/resize',{id:session.id,...size}).catch(e=>notice(e.message));}});
  window.addEventListener('beforeunload',()=>{if(tabs.length)navigator.sendBeacon?.('/__noop');});
})();
