/*
 * CinterForgeIF.js — CinterForge Terminal browser owner
 * PURPOSE: Owns browser interactive-fiction sessions, Z-machine autosave, painting and Code-OSS story handoff.
 * METHODS: codeZMachineStory, updateCodeZMachineSubscriber, updateCodeZMachineSubscribers, paintDiscordDraft, syncGameInput, paintIFTab, scheduleIFPaint, stopGame, openIFTab, startGame
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 1334–1335
CinterForgeState.codeZMachineChannel = typeof BroadcastChannel === 'function'
    ? new BroadcastChannel('cinterforge-terminal-zmachine') : null;

  // Original source lines 1336–1336
CinterForgeState.codeZMachineSubscribers = new Map();

  // Original source lines 1337–1341
CinterForgeState.codeZMachineStory = function codeZMachineStory() {
    const running = CinterForgeState.tabs.filter(tab => !tab.closed && tab.kind === 'if' && tab.game);
    if (CinterForgeState.activeTab?.kind === 'if' && CinterForgeState.activeTab.game) return CinterForgeState.activeTab;
    return running.length === 1 ? running[0] : null;
  };

  // Original source lines 1342–1365
CinterForgeState.updateCodeZMachineSubscriber = function updateCodeZMachineSubscriber(id, subscriber) {
    if (!CinterForgeState.codeZMachineChannel) return;
    const story = CinterForgeState.codeZMachineStory();
    if (!story) {
      if (!subscriber.noStory) {
        CinterForgeState.codeZMachineChannel.postMessage({type: 'output', id,
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
      CinterForgeState.codeZMachineChannel.postMessage({type: 'output', id,
        text: '[IF] ' + story.label + '\n'});
    }
    const previous = subscriber.transcript || '';
    const delta = transcript.startsWith(previous) ? transcript.slice(previous.length) : transcript;
    subscriber.transcript = transcript;
    if (delta) CinterForgeState.codeZMachineChannel.postMessage({type: 'output', id, text: delta});
  };

  // Original source lines 1366–1369
CinterForgeState.updateCodeZMachineSubscribers = function updateCodeZMachineSubscribers() {
    for (const [id, subscriber] of CinterForgeState.codeZMachineSubscribers)
      CinterForgeState.updateCodeZMachineSubscriber(id, subscriber);
  };

  // Original source lines 1370–1395
if (CinterForgeState.codeZMachineChannel) CinterForgeState.codeZMachineChannel.onmessage = event => {
    const request = event.data || {}, id = String(request.id || '');
    if (!/^zmachine-[a-z0-9]{4,24}$/.test(id)) return;
    if (request.type === 'subscribe') {
      if (!CinterForgeState.codeZMachineSubscribers.has(id) && CinterForgeState.codeZMachineSubscribers.size >= 8) return;
      const subscriber = {storyKey: null, transcript: '', noStory: false};
      CinterForgeState.codeZMachineSubscribers.set(id, subscriber);
      CinterForgeState.updateCodeZMachineSubscriber(id, subscriber);
    } else if (request.type === 'unsubscribe') {
      CinterForgeState.codeZMachineSubscribers.delete(id);
    } else if (request.type === 'input') {
      const subscriber = CinterForgeState.codeZMachineSubscribers.get(id);
      if (!subscriber || typeof request.text !== 'string' || request.text.length > 4096) return;
      const selectedStory = CinterForgeState.codeZMachineStory();
      const tab = selectedStory?.key === subscriber.storyKey ? selectedStory : null;
      const current = tab?.game;
      if (!current?.io?.lineResolver) {
        CinterForgeState.codeZMachineChannel.postMessage({type: 'output', id, text: '[IF] No input requested by the active story.\n'});
        return;
      }
      // Exactly the same WebIOAdapter event used by the normal IF Session.
      current.inputElement.value = request.text;
      current.inputElement.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'Enter', bubbles: false}));
    }
  };

  // Original source lines 1396–1396
CinterForgeState.terminalScreen = CinterForgeState.$('terminal-screen');

  // Original source lines 1400–1402
CinterForgeState.scrollToLatest = () => {
    CinterForgeState.terminalScreen.scrollTop = CinterForgeState.terminalScreen.scrollHeight;
  };

  // Original source lines 1403–1405
CinterForgeState.outputObserver = new MutationObserver(() => {
    if (CinterForgeState.activeTab?.kind !== 'discord' || (CinterForgeState.activeTab.followTail && !CinterForgeState.activeTab.loadingOlder)) CinterForgeState.scrollToLatest();
  });

  // Original source lines 1406–1406
CinterForgeState.outputObserver.observe(CinterForgeState.$('terminal-output'), { subtree: true, childList: true, characterData: true });

  // Original source lines 1407–1414
CinterForgeState.terminalScreen.addEventListener('scroll', () => {
    const tab = CinterForgeState.activeTab;
    if (tab?.kind !== 'discord' || tab.loadingOlder) return;
    tab.followTail = CinterForgeState.terminalScreen.scrollHeight - CinterForgeState.terminalScreen.scrollTop - CinterForgeState.terminalScreen.clientHeight < 40;
    if (tab.historyReady && tab.hasOlder && tab.oldestId && CinterForgeState.terminalScreen.scrollTop < 80 &&
      CinterForgeState.terminalScreen.scrollHeight > CinterForgeState.terminalScreen.clientHeight)
      void CinterForgeState.loadOlderDiscordMessages(tab);
  });

  // Original source lines 1419–1419
CinterForgeState.activeGame = () => CinterForgeState.activeTab?.game || null;

  // Original source lines 1423–1438
CinterForgeState.paintDiscordDraft = function paintDiscordDraft(tab) {
    if (!tab || tab.closed || CinterForgeState.activeTab !== tab) return;
    const output = CinterForgeState.$('terminal-output'), input = CinterForgeState.$('terminal-input');
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
  };

  // Original source lines 1443–1461
CinterForgeState.syncGameInput = function syncGameInput() {
    const output = CinterForgeState.$('terminal-output'), input = CinterForgeState.$('terminal-input'), current = CinterForgeState.activeGame();
    const discord = CinterForgeState.activeTab?.kind === 'discord';
    if (!discord && !current?.io?.lineResolver) {
      output.removeAttribute('data-if-echo');
      return;
    }
    if (discord) {
      CinterForgeState.activeTab.draft = input.value;
      output.removeAttribute('data-if-echo');
      CinterForgeState.paintDiscordDraft(CinterForgeState.activeTab);
    } else {
      current.draft = input.value;
      const value = input.value;
      const caret = Math.max(0, Math.min(input.selectionStart ?? value.length, value.length));
      output.setAttribute('data-if-echo', value.slice(0, caret) + '▌' + value.slice(caret));
    }
    CinterForgeState.scrollToLatest();
  };

  // Original source lines 1466–1478
CinterForgeState.paintIFTab = function paintIFTab(tab) {
    if (tab.closed || CinterForgeState.activeTab !== tab) return;
    const target = CinterForgeState.$('terminal-output');
    if (tab.buffer.childNodes && typeof tab.buffer.cloneNode === 'function')
      target.replaceChildren(...Array.from(tab.buffer.childNodes, node => node.cloneNode(true)));
    else target.textContent = tab.buffer.textContent;
    const current = tab.game;
    CinterForgeState.$('terminal-input').disabled = !current?.io;
    CinterForgeState.$('terminal-shell').textContent = 'Interactive Fiction · ' + tab.label;
    CinterForgeState.notice(current?.statusElement.textContent || tab.statusText || ('Playing ' + tab.label));
    CinterForgeState.syncGameInput();
    CinterForgeState.scrollToLatest();
  };

  // Original source lines 1483–1491
CinterForgeState.scheduleIFPaint = function scheduleIFPaint(tab) {
    if (tab.paintPending || tab.closed) return;
    tab.paintPending = true;
    requestAnimationFrame(() => {
      tab.paintPending = false;
      CinterForgeState.paintIFTab(tab);
      CinterForgeState.updateCodeZMachineSubscribers();
    });
  };

  // Original source lines 1496–1525
CinterForgeState.stopGame = async function stopGame(tab, notifyServer = true) {
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
    if (!current.displayScreen) CinterForgeState.updateCodeZMachineSubscribers();
    tab.statusText = tab.label + ' stopped · close this tab to return to the shell';
    tab.buffer.append(document.createTextNode('\n[IF] ' + tab.label + ' stopped\n'));
    if (CinterForgeState.activeTab === tab) {
      CinterForgeState.$('terminal-input').value = '';
      CinterForgeState.paintIFTab(tab);
      CinterForgeState.$('terminal-output').removeAttribute('data-if-echo');
    }
    if (notifyServer) {
      try {
        await CinterForgeState.api('/api/if/stop', { command: current.name, launch_id: current.launchId });
      }
      catch (error) {
        if (CinterForgeState.activeTab === tab) CinterForgeState.notice('IF stop: ' + error.message);
      }
    }
  };

  // Original source lines 1531–1579
CinterForgeState.openIFTab = async function openIFTab(name, launchId, sourceSession) {
    // A story can be requested twice while already open. Re-select its tab;
    // never bind a second VM to the same named autosave or another shell.
    const existing = CinterForgeState.tabs.find(tab => !tab.closed && tab.kind === 'if' && tab.game?.name === name);
    if (existing) {
      await CinterForgeState.activateTab(existing);
      return existing;
    }
    const buffer = CinterForgeState.makeBuffer();
    const tab = {
      key: ++CinterForgeState.tabSerial,
      label: name,
      profileId: 'modern',
      backend: 'if',
      kind: 'if',
      session: null,
      closed: false,
      display: new CinterForgeState.VTOutput(buffer),
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
    current.observer = new MutationObserver(() => CinterForgeState.scheduleIFPaint(tab));
    current.observer.observe(tab.buffer, { subtree: true, childList: true, characterData: true });
    // If the user already switched away from the requesting shell, do not
    // steal focus when the deferred server event finally arrives.
    const select = !sourceSession || CinterForgeState.activeTab?.session?.id === sourceSession;
    CinterForgeState.tabs.push(tab);
    CinterForgeState.updateCodeZMachineSubscribers();
    if (select) await CinterForgeState.activateTab(tab);
    else CinterForgeState.renderTabs();
    void CinterForgeState.startGame(tab, current);
    return tab;
  };

  // Original source lines 1585–1758
CinterForgeState.startGame = async function startGame(tab, current) {
    const name = current.name;
    /**
     * Check that an asynchronous IF operation still belongs to the open tab and current
     * interpreter.
     */
    const alive = () => !tab.closed && tab.game === current;
    const output = tab.buffer;
    const saveURL = '/api/if/save/' + encodeURIComponent(name) +
      (current.displayScreen ? '?screen=' + encodeURIComponent(current.displayScreen) : '');
    try {
      const response = await fetch('/api/if/story/' + encodeURIComponent(name),
        { headers: { 'X-CPH-Token': CinterForgeState.token }, cache: 'no-store', signal: current.abort.signal });
      if (!alive()) return;
      if (!response.ok) throw new Error((await response.json()).error || 'Story download failed');
      const story = await response.arrayBuffer();
      const [{ ZMachine }, { WebIOAdapter }, quetzal] = await Promise.all([
        import('/zmachine/index.js'), import('/zmachine/web/WebIOAdapter.js'),
        import('/zmachine/core/state/Quetzal.js')]);
      if (!alive()) return;
      const auth = { 'X-CPH-Token': CinterForgeState.token };
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
              CinterForgeState.scheduleIFPaint(tab);
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
        CinterForgeState.scheduleIFPaint(tab);
      };
      current.releaseDisplay = releaseDisplay;
      const readyReadLine = io.readLine.bind(io);
      io.readLine = (...args) => {
        if (current.displayScreen && !alive()) throw new Error('In-world story session closed');
        const pending = readyReadLine(...args);
        if (alive()) {
          current.statusElement.textContent = 'Playing ' + name + ' · Ctrl+C to exit';
          if (current.displayBoundary) queueMicrotask(() => { if (alive()) current.displayBoundary(); });
          if (CinterForgeState.activeTab === tab) {
            CinterForgeState.$('terminal-input').disabled = false;
            CinterForgeState.focusTerminal();
            CinterForgeState.syncGameInput();
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
        CinterForgeState.scheduleIFPaint(tab);
      }
    } finally {
      if (alive()) {
        if (current.displayFinished) {
          current.displayFinished();
          await CinterForgeState.stopGame(tab, false);
        } else await CinterForgeState.stopGame(tab);
      }
    }
  };

  // Bedrock owns the game lifetime; the installed native WebView supplies the
  // very same browser Z-machine and WebIOAdapter, using detached DOM nodes.
  // Never publish these sessions into tabs or the desktop IF action queue.
  const displayGames = new Map();
  // The native host may retry a turn until its result is acknowledged.
  // Never run the same game input twice, and retain a result until accepted.
  let displayHandled = null;
  let displayResult = null;
  const displayReply = (job, tab, finished, error = '') => {
    const text = (tab?.buffer.textContent || '') + (error ? '\n[IF] ' + error + '\n' : '');
    displayResult = {id: job.id, game: job.game, output: text.slice(-16384), finished};
  };
  function displayTurn(job) {
    let tab = displayGames.get(job.game);
    if (job.start) {
      if (tab) throw new Error('Duplicate in-world story start');
      const current = {
        name: job.start, launchId: job.game, displayScreen: job.screen,
        abort: new AbortController(), io: null, draft: '',
        inputElement: document.createElement('textarea'),
        statusElement: document.createElement('span'), releaseDisplay: null,
        observer: null
      };
      tab = {label: job.start, buffer: CinterForgeState.makeBuffer(),
        game: current, closed: false, paintPending: false, job};
      const report = finished => {
        const pending = tab.job;
        tab.job = null;
        if (finished) displayGames.delete(job.game);
        if (pending) displayReply(pending, tab, finished);
      };
      current.displayBoundary = () => report(false);
      current.displayFinished = () => report(true);
      displayGames.set(job.game, tab);
      void CinterForgeState.startGame(tab, current);
      return;
    }
    if (!tab || !tab.game?.io?.lineResolver || tab.job)
      throw new Error('The in-world story is no longer waiting for input');
    tab.job = job;
    // Use exactly the same adapter input event as the desktop story and Code-OSS.
    const input = tab.game.inputElement;
    input.value = job.command;
    input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:false}));
  }

  let pollingDisplay = false;
  setInterval(async () => {
    // Native bridge is present in the installed WebView, not a LAN browser.
    // Do not gate on visibility: the host can be playing in Minecraft.
    if (window.top !== window || typeof window.cinterNative !== 'function' || pollingDisplay) return;
    // The host may own a Bedrock session before its tab is painted.
    pollingDisplay = true;
    try {
      const state = await CinterForgeState.api('/api/if/display/next');
      const active = new Set(state.active || []);
      for (const [id, tab] of displayGames) {
        if (!active.has(id)) {
          displayGames.delete(id);
          tab.closed = true;
          tab.job = null;
          await CinterForgeState.stopGame(tab, false);
        }
      }
      if (state.action && displayHandled !== state.action.id) {
        displayHandled = state.action.id;
        try { displayTurn(state.action); }
        catch (error) { displayReply(state.action, null, true, error.message); }
      }
      if (displayResult) {
        const result = displayResult;
        // A stale reply after a closed session is discarded; network failures
        // leave the result in place to retry on the next poll.
        const response = await CinterForgeState.api('/api/if/display/result', result);
        if (displayResult === result && (response.accepted || state.action?.id !== result.id))
          displayResult = null;
      }
    } catch (error) { console.error('Bedrock IF bridge:', error); }
    finally { pollingDisplay = false; }
  }, 500);

})();
