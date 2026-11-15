/**
 * CinterForgeAPI.js — one authenticated same-origin HTTP transport.
 * PURPOSE: Own requests to the Python service and the public CinterHost bridge.
 * METHODS: create(token) returns the shared api(path, body) function; it also
 * creates terminal.open/input/poll/resize/close and Bedrock/Debugger facades.
 * STARTUP: loaded before CinterForgeTerminal.js; the active page supplies its bootstrap
 * token. Requests stay on the same origin with the existing session cookie.
 * ERRORS: redirect to /login on 401, surface HTTP service failures to callers.
 * SHUTDOWN: transport owns no shell processes, tabs, or VM state.
 * SECURITY: never persist or log the token, change routes, or invent a guest
 * readiness result; Python owns all API authorization and PTY lifetimes.
 */
(() => {
  'use strict';
  window.CinterForgeAPI = Object.freeze({
    create(token) {
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


    return api;
    }
  });
})();
