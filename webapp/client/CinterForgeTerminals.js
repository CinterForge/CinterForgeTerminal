/**
 * CinterForgeTerminals.js — bounded, per-tab VT output renderer.
 * PURPOSE: Render actual PTY output, ANSI controls, cursor and copied transcript
 * without owning the PTY itself or becoming an alternate terminal protocol.
 * METHODS: createVTOutput injects the owning UI's element/tab/draft callbacks;
 * CinterForgeVTOutput.push interprets output, prepend inserts earlier history,
 * transcript exports unmodified text, render paints cursor/scroll position,
 * and reset clears only the selected tab's display state.
 * STARTUP: loaded before CinterForgeTerminal.js and instantiated separately per tab.
 * SHUTDOWN: controller releases its tab object; no process to terminate here.
 * KEEP CURRENT: ANSI, Discord draft and scroll semantics are unchanged.
 */
(() => {
  'use strict';
  window.CinterForgeTerminals = Object.freeze({
    createVTOutput({getElement, getActiveTab, paintDiscordDraft}) {
  class CinterForgeVTOutput {
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
      const activeTab = getActiveTab();
      // Put the insertion marker at the real VT cursor, without adding any
      // character to the copied transcript or the live shell input.
      const parent = this.target.parentElement;
      const previousTop = parent?.scrollTop || 0, previousHeight = parent?.scrollHeight || 0;
      const visibleDiscord = this.target === getElement('terminal-output') && activeTab?.kind === 'discord';
      const visibleBedrock = this.target === getElement('terminal-output') && activeTab?.kind === 'bedrock';
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
        : visibleBedrock
          ? [document.createTextNode(prefix + suffix +
              (line ? '\n' : '') + 'minecraft> ' + (activeTab.bedrockDraft || '')), caret]
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

      return CinterForgeVTOutput;
    }
  });
})();
