/*
 * CinterForgeInput.js — CinterForge Terminal browser owner
 * PURPOSE: Handles active-tab keyboard, clipboard, paste and native Edit commands, preserving platform shortcuts.
 * METHODS: Event handlers and per-tab state.
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 2078–2078
CinterForgeState.overlay = CinterForgeState.$('terminal-overlay');

  // Original source lines 2079–2079
CinterForgeState.input = CinterForgeState.$('terminal-input');

  // Original source lines 2083–2085
CinterForgeState.focusTerminal = () => {
    if ((CinterForgeState.session || CinterForgeState.activeGame() || CinterForgeState.activeTab?.kind === 'discord') && !CinterForgeState.overlay.hidden && !CinterForgeState.input.disabled) CinterForgeState.input.focus();
  };

  // Original source lines 2092–2094
CinterForgeState.currentTranscript = () => CinterForgeState.activeTab?.kind === 'if'
    ? (CinterForgeState.activeTab.buffer.textContent || '')
    : CinterForgeState.display.transcript();

  // Original source lines 2098–2141
CinterForgeState.copyTranscript = async () => {
    const transcript = CinterForgeState.currentTranscript();
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
      if (CinterForgeState.platform === 'mac' && typeof window.cinterClipboard === 'function') {
        if (await window.cinterClipboard({action: 'copy', text: transcript}) !== true)
          throw new Error('Native pasteboard rejected Copy');
        CinterForgeState.notice('Copied terminal transcript');
        return transcript;
      }
      if (CinterForgeState.platform === 'win' && legacyCopy()) {
        CinterForgeState.notice('Copied terminal transcript');
        return transcript;
      }
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(transcript);
    } catch (error) {
      // Retain the existing WKWebView/WebKitGTK fallback.
      if (!legacyCopy()) {
        CinterForgeState.notice('Copy unavailable: ' + error.message);
        return false;
      }
    }
    CinterForgeState.notice('Copied terminal transcript');
    return transcript;
  };

  // Original source lines 2145–2158
CinterForgeState.pasteIntoSession = text => {
    if ((!CinterForgeState.session && !CinterForgeState.activeGame() && CinterForgeState.activeTab?.kind !== 'discord') || CinterForgeState.computerView.hidden === false) return false;
    text = String(text ?? '');
    if (!text) return false;
    if (CinterForgeState.activeGame() || CinterForgeState.activeTab?.kind === 'discord') {
      CinterForgeState.input.focus();
      CinterForgeState.input.setRangeText(text, CinterForgeState.input.selectionStart, CinterForgeState.input.selectionEnd, 'end');
      CinterForgeState.input.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      void CinterForgeState.send(text);
      CinterForgeState.focusTerminal();
    }
    return true;
  };

  // Original source lines 2163–2179
CinterForgeState.pasteClipboard = async () => {
    try {
      // WebView2 does not consistently grant navigator.clipboard.readText
      // to the application's loopback page. The native Windows clipboard
      // bridge is read-only and remains local to the user's installed app.
      if ((CinterForgeState.platform === 'win' || CinterForgeState.platform === 'mac') &&
          typeof window.cinterClipboard === 'function')
        return CinterForgeState.pasteIntoSession(await window.cinterClipboard({ action: 'paste' }));
      if (navigator.clipboard?.readText)
        return CinterForgeState.pasteIntoSession(await navigator.clipboard.readText());
      CinterForgeState.notice('Paste from your keyboard with Ctrl+V');
      return false;
    } catch (error) {
      CinterForgeState.notice('Paste unavailable: ' + error.message);
      return false;
    }
  };

  // Original source lines 2181–2186
window.CinterEdit = Object.freeze({
    copy: CinterForgeState.copyTranscript,
    paste: CinterForgeState.pasteClipboard,
    insert: CinterForgeState.pasteIntoSession,
    transcript: CinterForgeState.currentTranscript
  });

  // Original source lines 2192–2201
document.addEventListener('keydown', event => {
    if (CinterForgeState.platform !== 'mac' || !event.metaKey || event.ctrlKey || event.altKey ||
        event.shiftKey || event.isComposing || !CinterForgeState.computerView.hidden) return;
    const key = event.key.toLowerCase();
    if (key !== 'c' && key !== 'v') return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (key === 'c') void CinterForgeState.copyTranscript();
    else void CinterForgeState.pasteClipboard();
  }, true);

  // Original source lines 2206–2221
document.addEventListener('keydown', event => {
    if (CinterForgeState.platform !== 'win' || !event.ctrlKey || event.metaKey || event.altKey ||
      event.isComposing || CinterForgeState.computerView.hidden === false) return;
    const key = event.key.toLowerCase();
    if (key === 'c' && event.shiftKey) {
      event.preventDefault();
      event.stopImmediatePropagation();
      void CinterForgeState.copyTranscript();
    } else if (key === 'v' && (typeof window.cinterClipboard === 'function')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      void CinterForgeState.pasteClipboard();
    }
    // Browser previews without the native bridge retain Ctrl+V's default
    // textarea paste event. The input listener sends the pasted text once.
  }, true);

  // Original source lines 2225–2238
CinterForgeState.keys = {
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

  // Original source lines 2242–2260
CinterForgeState.forwardKey = (name) => {
    if (CinterForgeState.activeGame()) {
      if (name === 'ControlC') {
        void CinterForgeState.closeTab(CinterForgeState.activeTab);
        return true;
      }
      if (name === 'Enter') {
        CinterForgeState.input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        return true;
      }
      return false;
    }
    const code = CinterForgeState.keys[name];
    if (code && CinterForgeState.session) {
      CinterForgeState.send(code);
      return true;
    }
    return false;
  };

  // Original source lines 2261–2266
for (const button of (CinterForgeState.$('terminal-keys').querySelectorAll?.('button') || [])) {
    button.addEventListener('click', () => {
      CinterForgeState.forwardKey(button.dataset.terminalKey);
      CinterForgeState.focusTerminal();
    });
  }

  // Original source lines 2267–2269
CinterForgeState.$('terminal-reconnect').addEventListener('click', () => {
    void CinterForgeState.reconnectTab();
  });

  // Original source lines 2270–2273
CinterForgeState.input.addEventListener('wheel', e => {
    CinterForgeState.$('terminal-screen').scrollTop += e.deltaY;
    e.preventDefault();
  }, { passive: false });

  // Original source lines 2275–2288
document.addEventListener('keydown', e => {
    if (!CinterForgeState.session || CinterForgeState.activeTab?.kind === 'if' || CinterForgeState.computerView.hidden === false ||
        e.isComposing || e.target === CinterForgeState.input) return;
    if (e.target && ['SELECT', 'BUTTON', 'SUMMARY', 'INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;
    const ctrl = e.ctrlKey && !e.altKey && e.key.length === 1
      ? String.fromCharCode(e.key.toUpperCase().charCodeAt(0) & 31)
      : null;
    const special = CinterForgeState.keys[e.key];
    const printable = !e.ctrlKey && !e.metaKey && !e.altKey && e.key.length === 1 ? e.key : null;
    if (ctrl || special || printable) {
      e.preventDefault();
      CinterForgeState.send(ctrl || special || printable);
    }
  });

  // Original source lines 2289–2296
document.addEventListener('paste', e => {
    if (!CinterForgeState.session || CinterForgeState.activeTab?.kind === 'if' || CinterForgeState.computerView.hidden === false || e.target === CinterForgeState.input) return;
    const text = e.clipboardData?.getData('text/plain');
    if (text) {
      e.preventDefault();
      CinterForgeState.send(text);
    }
  });

  // Original source lines 2297–2297
CinterForgeState.$('terminal-screen').addEventListener('click', CinterForgeState.focusTerminal);

})();
