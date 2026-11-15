/*
 * CINTERFORGE SOURCE HEADER — webapp/startup.js
 * PURPOSE: Manages the startup screen and transitions while native assets and services become available.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   enterTerminal: Handle enter terminal through this component's established interfaces.
 *   check: Check check for this module.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
/* Only the native UI's loading gate. No boot/download or other privileged
 * operation starts in the browser: the native-owned Python supervisor does it.
 */
(() => {
  'use strict';
  const status = document.getElementById('startup-status');
  const token = document.documentElement.dataset.cphToken;
  const started = Date.now();
  let finished = false;
  const enterTerminal = (message) => {
    if (finished) return;
    finished = true;
    if (message) status.textContent = message;
    // Never leave the user trapped by a broken guest or broken status API.
    window.setTimeout(() => window.location.replace('/?devtools-boot-complete=1'), 550);
  };
  const check = async () => {
    if (finished) return;
    // A wedged Colima supervisor must not make Mac Bash inaccessible.
    if (Date.now() - started > 3 * 60 * 1000) {
      enterTerminal('DevTools startup exceeded three minutes. Opening Mac Bash.');
      return;
    }
    try {
      const response = await fetch('/api/lifecycle/guest-status', {
        headers: {'X-CPH-Token': token}, cache: 'no-store',
        signal: AbortSignal.timeout(8000)
      });
      if (!response.ok) throw new Error('Guest status unavailable (' + response.status + ')');
      const data = await response.json();
      if (data.state === 'ready') {
        enterTerminal('DevTools ready. Opening Terminal.');
      } else if (data.state === 'failed') {
        enterTerminal('DevTools could not initialize. Opening Mac Bash; see Server Log.');
      } else if (data.state === 'not-installed') {
        enterTerminal('DevTools not installed. Opening Mac Bash.');
      } else if (data.state === 'starting') {
        status.textContent = 'Initializing DevTools... Checking Linux and Windows Bash connections.';
        window.setTimeout(check, 800);
      } else {
        enterTerminal('Guest readiness unavailable. Opening Mac Bash.');
      }
    } catch (error) {
      // A one-off request failure isn't proof Colima failed. Retry briefly,
      // then fall back to the normal UI instead of leaving a permanent splash.
      if (Date.now() - started > 15000) enterTerminal('DevTools status unavailable. Opening Mac Bash.');
      else window.setTimeout(check, 1000);
    }
  };
  check();
})();
