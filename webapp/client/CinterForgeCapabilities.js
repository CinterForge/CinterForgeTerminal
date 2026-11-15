/**
 * CinterForgeCapabilities.js — independent guest capability snapshot reader.
 * PURPOSE: Normalize already-verified Python guest status for the UI without
 * claiming that a receipt, Docker container or running VM is itself ready.
 * METHODS: normalize(status, previous) returns installed/state, distinct Bash
 * and VNC ready sets, and a change bit. Invalid responses return null.
 * STARTUP: CinterForgeTerminal.js provides previous state; only the HTTP service can
 * assert observed Linux/Windows endpoint readiness.
 * SHUTDOWN: no process/connection ownership and no periodic polling here.
 * SAFETY: Keep shell and VNC readiness independent; old disconnected tabs
 * are restored by the existing tab owner after a confirmed new capability.
 */
(() => {
  'use strict';
  window.CinterForgeCapabilities = Object.freeze({
    normalize(status, {devtoolsInstalled, guestState, guestReady, guestVncReady}) {
      if (!status || typeof status !== 'object') return null;
      const installed = status.devtools_installed === undefined
        ? devtoolsInstalled : status.devtools_installed === true;
      const state = String(status.devtools_state || status.state || guestState);
      const guests = new Set((status.devtools_guests || status.guests || [])
        .filter(kind => kind === 'linux' || kind === 'win'));
      const vnc = new Set((status.devtools_vnc || status.vnc || [])
        .filter(kind => kind === 'linux' || kind === 'win'));
      const changed = devtoolsInstalled !== installed || guestState !== state ||
        [...guests].some(kind => !guestReady.has(kind)) ||
        [...guestReady].some(kind => !guests.has(kind)) ||
        [...vnc].some(kind => !guestVncReady.has(kind)) ||
        [...guestVncReady].some(kind => !vnc.has(kind));
      return {installed, state, guests, vnc, changed};
    }
  });
})();
