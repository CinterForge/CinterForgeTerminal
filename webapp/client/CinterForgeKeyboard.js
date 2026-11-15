/*
 * CinterForgeKeyboard.js — CinterForge Terminal browser owner
 * PURPOSE: Wires live keyboard and IME input to the current tab, without altering guest runtime ownership.
 * METHODS: Event handlers and per-tab state.
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 2678–2694
CinterForgeState.input.addEventListener('input', e => {
    // Composing text is still part of the Discord draft; just defer Enter.
    if (e.isComposing && CinterForgeState.activeTab?.kind === 'discord') {
      CinterForgeState.syncGameInput();
      return;
    }
    if (e.isComposing) return;
    if (CinterForgeState.activeTab?.kind === 'discord' || CinterForgeState.activeGame()) {
      CinterForgeState.syncGameInput();
      return;
    }
    if (CinterForgeState.input.value) {
      const value = CinterForgeState.input.value;
      CinterForgeState.input.value = '';
      CinterForgeState.send(value);
    }
  });

  // Original source lines 2695–2705
CinterForgeState.input.addEventListener('compositionend', () => {
    if (CinterForgeState.activeTab?.kind === 'discord' || CinterForgeState.activeGame()) {
      CinterForgeState.syncGameInput();
      return;
    }
    if (CinterForgeState.input.value) {
      const value = CinterForgeState.input.value;
      CinterForgeState.input.value = '';
      CinterForgeState.send(value);
    }
  });

  // Original source lines 2707–2709
CinterForgeState.input.addEventListener('keyup', () => {
    if (CinterForgeState.activeGame() || CinterForgeState.activeTab?.kind === 'discord') CinterForgeState.syncGameInput();
  });

  // Original source lines 2710–2712
CinterForgeState.input.addEventListener('click', () => {
    if (CinterForgeState.activeGame() || CinterForgeState.activeTab?.kind === 'discord') CinterForgeState.syncGameInput();
  });

  // Original source lines 2713–2715
CinterForgeState.input.addEventListener('select', () => {
    if (CinterForgeState.activeGame() || CinterForgeState.activeTab?.kind === 'discord') CinterForgeState.syncGameInput();
  });

  // Original source lines 2719–2725
CinterForgeState.input.addEventListener('pointerdown', e => {
    if (CinterForgeState.activeTab?.kind !== 'discord') return;
    e.preventDefault();
    CinterForgeState.input.focus();
    CinterForgeState.input.setSelectionRange?.(CinterForgeState.input.value.length, CinterForgeState.input.value.length);
    CinterForgeState.syncGameInput();
  });

  // Original source lines 2729–2749
CinterForgeState.input.addEventListener('keydown', e => {
    if (!(CinterForgeState.activeGame()?.io?.lineResolver || CinterForgeState.activeTab?.kind === 'discord') ||
      e.isComposing || e.key === 'Process') return;
    if (e.key !== 'Backspace' && e.key !== 'Delete') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const value = CinterForgeState.input.value;
    const start = CinterForgeState.input.selectionStart ?? value.length;
    const end = CinterForgeState.input.selectionEnd ?? start;
    const caret = Math.max(0, Math.min(end > start ? end : start, value.length));
    // A selected range is not permission to discard the entire command.
    // Delete precisely one Unicode character, including surrogate pairs.
    if (e.key === 'Backspace' && caret > 0) {
      const previous = Array.from(value.slice(0, caret)).slice(-1)[0] || '';
      CinterForgeState.input.setRangeText('', caret - previous.length, caret, 'start');
    } else if (e.key === 'Delete' && caret < value.length) {
      const next = Array.from(value.slice(caret))[0] || '';
      CinterForgeState.input.setRangeText('', caret, caret + next.length, 'start');
    }
    CinterForgeState.syncGameInput();
  }, true);

  // Original source lines 2750–2810
CinterForgeState.input.addEventListener('keydown', e => {
    if (e.isComposing || e.key === 'Process') return;
    if (CinterForgeState.activeTab?.kind === 'discord') {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        e.stopImmediatePropagation();
        const message = CinterForgeState.input.value.trim();
        CinterForgeState.input.value = '';
        CinterForgeState.activeTab.draft = '';
        CinterForgeState.syncGameInput();
        if (message) void CinterForgeState.api('/api/discord/send', {
          channel_id: CinterForgeState.activeTab.channelId, content: message
        }).catch(error => CinterForgeState.notice('Discord send: ' + error.message));
      }
      return;
    }
    // File accelerators must not leak Ctrl+N/Ctrl+Q to the shell PTY.
    if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey &&
      ['n', 'q'].includes(e.key.toLowerCase())) return;
    const current = CinterForgeState.activeGame();
    if (current) {
      if (e.ctrlKey && e.key.toLowerCase() === 'c') {
        e.preventDefault();
        e.stopImmediatePropagation();
        void CinterForgeState.closeTab(CinterForgeState.activeTab);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (current.io?.lineResolver) {
          // Only the active story's WebIOAdapter receives this command.
          current.inputElement.value = CinterForgeState.input.value;
          current.inputElement.dispatchEvent(new KeyboardEvent('keydown',
            { key: 'Enter', bubbles: false }));
          CinterForgeState.input.value = '';
          current.draft = '';
          CinterForgeState.syncGameInput();
        }
      } else if (current.io && e.key !== 'Backspace' && e.key !== 'Delete') {
        // Forward readChar / history keys to this story only. Normal text
        // editing remains in the visible input and never reaches another VM.
        current.inputElement.value = CinterForgeState.input.value;
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
    const special = CinterForgeState.keys;
    const ctrl = e.ctrlKey && e.key.length === 1 ? String.fromCharCode(e.key.toUpperCase().charCodeAt(0) & 31) : null;
    const code = ctrl || special[e.key];
    if (code) {
      e.preventDefault();
      CinterForgeState.send(code);
    }
  });

})();
