/*
 * CinterForgeCodeOSS.js — CinterForge Terminal browser owner
 * PURPOSE: Connects the embedded Code-OSS editor and its origin-checked command/message bridge; does not start a VM.
 * METHODS: setCodeWrapperHidden, sendCodeCommand
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 275–275
CinterForgeState.frame = CinterForgeState.$('computer-frame');
CinterForgeState.computerView = CinterForgeState.$('computer-view');

  // Original source lines 279–279
CinterForgeState.codeFrame = document.createElement('iframe');

  // Original source lines 280–280
CinterForgeState.codeFrame.id = 'codeoss-frame';

  // Original source lines 281–281
CinterForgeState.codeFrame.title = 'CinterForge Code-OSS workbench';

  // Original source lines 282–282
CinterForgeState.codeFrame.referrerPolicy = 'no-referrer';

  // Original source lines 283–283
CinterForgeState.codeFrame.hidden = true;

  // Original source lines 284–284
CinterForgeState.computerView.append(CinterForgeState.codeFrame);

  // Original source lines 288–297
CinterForgeState.codeError = CinterForgeState.$('computer-error') || (() => {
    if (!CinterForgeState.computerView) throw new Error('Terminal computer view is missing');
    const node = document.createElement('div');
    node.id = 'computer-error';
    node.className = 'computer-error';
    node.setAttribute('role', 'alert');
    node.hidden = true;
    CinterForgeState.computerView.append(node);
    return node;
  })();

  // Original source lines 300–300
CinterForgeState.CODE_COMMAND_CHANNEL = 'cinterforge-terminal-code-commands';

  // Original source lines 301–301
CinterForgeState.CODE_MENU_NAMES = ['File', 'Edit', 'Selection', 'View', 'Go', 'Run', 'Terminal', 'Help'];

  // Original source lines 302–303
CinterForgeState.codeCommands = typeof BroadcastChannel === 'function'
    ? new BroadcastChannel(CinterForgeState.CODE_COMMAND_CHANNEL) : null;

  // Original source lines 304–304
CinterForgeState.codeCommandSerial = 0;

  // Original source lines 305–306
CinterForgeState.codeIsActive = () => CinterForgeState.activeTab?.profileId === 'codeoss' &&
    !CinterForgeState.computerView.hidden && !CinterForgeState.codeFrame.hidden && CinterForgeState.codeFrame.src.includes('/codeoss/');

  // Original source lines 310–310
CinterForgeState.terminalOverlay = CinterForgeState.$('terminal-overlay');

  // Original source lines 311–316
CinterForgeState.setCodeWrapperHidden = function setCodeWrapperHidden(hidden) {
    CinterForgeState.terminalOverlay.dataset.codeWrapperHidden = hidden ? 'true' : 'false';
    if (hidden) document.querySelectorAll('.terminal-menu[open]').forEach(menu => { menu.open = false; });
    CinterForgeState.codeFrame.contentWindow?.postMessage({type: 'cinterforge-terminal-wrapper-state', hidden},
      window.location.origin);
  };

  // Original source lines 317–321
window.addEventListener('message', event => {
    if (event.source !== CinterForgeState.codeFrame.contentWindow || event.origin !== window.location.origin ||
        event.data?.type !== 'cinterforge-terminal-wrapper-toggle' || !CinterForgeState.codeIsActive()) return;
    CinterForgeState.setCodeWrapperHidden(CinterForgeState.terminalOverlay.dataset.codeWrapperHidden !== 'true');
  });

  // Original source lines 324–332
CinterForgeState.sendCodeCommand = function sendCodeCommand(type, data = {}) {
    if (!CinterForgeState.codeIsActive()) return false;
    if (!CinterForgeState.codeCommands || !CinterForgeState.codeOwnerId) {
      CinterForgeState.notice('Code-OSS commands are not ready yet; see Server Log if startup fails');
      return false;
    }
    CinterForgeState.codeCommands.postMessage({type, ...data, requestId: ++CinterForgeState.codeCommandSerial});
    return true;
  };

  // Original source lines 338–377
if (CinterForgeState.codeCommands) CinterForgeState.codeCommands.onmessage = async event => {
    const message = event.data || {};
    if (message.type === 'open-launcher') {
      if (['mac', 'win', 'linux'].includes(message.platform) &&
          CinterForgeState.terminalActionEnabled('launcher:' + message.platform) &&
          CinterForgeState.codeFrame.dataset.loaded === 'true')
        void CinterForgeState.openLauncherTab(message.platform);
      return;
    }
    if (message.type === 'code-workbench-owner') {
      if (CinterForgeState.codeFrame.dataset.loaded === 'true' &&
          typeof message.owner === 'string' && /^[a-z0-9]{1,24}$/.test(message.owner))
        CinterForgeState.codeOwnerId = message.owner;
      return;
    }
    if (message.type !== 'host-probe' && message.type !== 'host-request') return;
    if (typeof message.requestId !== 'string' ||
        !/^host-[A-Za-z0-9-]{12,100}$/.test(message.requestId) ||
        !CinterForgeState.codeFrame.src.includes('/codeoss/')) return;

    const replyType = message.type === 'host-probe' ? 'host-probe-response' : 'host-request-response';
    try {
      if (message.type === 'host-probe') {
        const payload = await CinterForgeState.api('/host/code-workspace');
        CinterForgeState.codeCommands.postMessage({type: replyType, requestId: message.requestId, ok: true, payload});
        return;
      }
      const path = String(message.path || '');
      // Only the existing Code-OSS filesystem and shell contracts can cross
      // this UI bridge; it is not an arbitrary privileged HTTP proxy.
      if (path !== '/code/command' &&
          !/^\/host\/code-shell-(open|close|close-owner|write|resize|output)(?:\?[^#]*)?$/.test(path))
        throw new Error('Unsupported Code-OSS host operation');
      const payload = message.method === 'GET' ? await CinterForgeState.api(path) : await CinterForgeState.api(path, message.body || {});
      CinterForgeState.codeCommands.postMessage({type: replyType, requestId: message.requestId, ok: true, payload});
    } catch (error) {
      CinterForgeState.codeCommands.postMessage({type: replyType, requestId: message.requestId,
        ok: false, error: String(error.message || error)});
    }
  };

  // Original source lines 379–379
CinterForgeState.codeUrl = '/codeoss/out/vs/code/browser/workbench/workbench.html';

  // Original source lines 380–380
CinterForgeState.codeLoad = 0;
CinterForgeState.codeReady = false;
CinterForgeState.codeLastError = '';
CinterForgeState.codeOwnerId = null;

  // Original source lines 384–391
CinterForgeState.codeFailure = message => {
    if (!CinterForgeState.codeIsActive() || !CinterForgeState.codeFrame.src.includes(CinterForgeState.codeUrl) || CinterForgeState.codeReady) return;
    CinterForgeState.codeLastError = String(message).slice(0, 450);
    CinterForgeState.codeError.textContent = 'VS Code did not start.\n' + CinterForgeState.codeLastError + '\nOpen Debugger for WebKit details.';
    CinterForgeState.codeError.hidden = false;
    CinterForgeState.notice('VS Code: ' + CinterForgeState.codeLastError);
    void CinterForgeState.nativeEvent('error', { message: 'VS Code: ' + CinterForgeState.codeLastError });
  };

  // Original source lines 394–411
window.addEventListener('message', event => {
    if (event.origin !== new URL(location.href).origin || event.source !== CinterForgeState.codeFrame.contentWindow ||
      CinterForgeState.computerView.hidden || !CinterForgeState.codeFrame.src.includes(CinterForgeState.codeUrl)) return;
    const data = event.data;
    if (!data || data.type !== 'cinterforge-codeoss-health') return;
    if (data.status === 'ready') {
      CinterForgeState.codeReady = true;
      CinterForgeState.codeError.hidden = true;
      CinterForgeState.updateCodeMenuMode();
      CinterForgeState.requestCodeServiceMenus();
      for (const name of CinterForgeState.CODE_MENU_NAMES) void CinterForgeState.refreshCodeMenu(name);
      CinterForgeState.notice('VS Code workbench ready');
    } else if (data.status === 'error') {
      CinterForgeState.codeLastError = String(data.message || 'Workbench error').slice(0, 450);
    } else if (data.status === 'timeout') {
      CinterForgeState.codeFailure(CinterForgeState.codeLastError || data.message || 'Workbench did not initialize');
    }
  });

  // Original source lines 417–432
CinterForgeState.checkCodeAssets = async stamp => {
    for (const url of [CinterForgeState.codeUrl, '/codeoss/out/vs/code/browser/workbench/workbench.js',
      '/codeoss/out/nls.messages.js']) {
      try {
        const response = await fetch(url, { method: 'HEAD', cache: 'no-store' });
        if (stamp !== CinterForgeState.codeLoad) return;
        if (!response.ok) {
          CinterForgeState.codeFailure(`${url}: HTTP ${response.status}`);
          return;
        }
      } catch (error) {
        if (stamp === CinterForgeState.codeLoad) CinterForgeState.codeFailure(`${url}: ${error.message}`);
        return;
      }
    }
  };

})();
