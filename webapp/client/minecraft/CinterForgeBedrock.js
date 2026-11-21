/*
 * CinterForgeBedrock.js — CinterForge Terminal browser owner
 * PURPOSE: Handles explicit Minecraft Bedrock license consent and install progress, never automatic provisioning.
 * METHODS: Event handlers and per-tab state.
 * STARTUP: Loaded in original dependency order from index.html and Discord view.
 * SHUTDOWN: Only releases browser-owned sessions; host controls VM lifetime.
 * CONTRACT: Browser modules share one explicit CinterForgeState; no implicit
 *   top-level globals, eval, dynamic compilation, or lifecycle imports.
 */
(() => {
  'use strict';
  const CinterForgeState = window.CinterForgeClientState || (window.CinterForgeClientState = Object.create(null));

  // Original source lines 2816–2816
CinterForgeState.license = CinterForgeState.$('license-dialog');
CinterForgeState.accepted = CinterForgeState.$('license-accept');

  // Original source lines 2817–2817
CinterForgeState.licensePending = false;

  // Original source lines 2821–2829
CinterForgeState.requestLicense = () => {
    // The status endpoint is polled repeatedly. Never clear a user's consent
    // checkbox while the dialog is already open.
    if (!CinterForgeState.license.hidden) return;
    CinterForgeState.accepted.checked = false;
    CinterForgeState.$('license-confirm').disabled = true;
    CinterForgeState.license.hidden = false;
    CinterForgeState.accepted.focus();
  };

  // Original source lines 2830–2832
CinterForgeState.$('license-accept').addEventListener('change', () => {
    CinterForgeState.$('license-confirm').disabled = !CinterForgeState.accepted.checked;
  });

  // Original source lines 2833–2842
CinterForgeState.$('license-cancel').addEventListener('click', async () => {
    if (CinterForgeState.licensePending) return;
    CinterForgeState.license.hidden = true;
    try {
      await CinterForgeState.api('/api/bedrock/eula/ack', {});
    } catch (e) {
      CinterForgeState.notice(e.message);
    }
    CinterForgeState.focusTerminal();
  });

  // Original source lines 2848–2881
CinterForgeState.monitorBedrockInstall = async () => {
    let cursor = 0, lastUpdate = Date.now();
    try {
      for (; ;) {
        const events = await CinterForgeState.api('/api/bedrock/events?cursor=' + cursor);
        for (const event of events.events || []) {
          if (event.id <= cursor) continue;
          cursor = event.id;
          lastUpdate = Date.now();
          CinterForgeState.notice(event.message);
          CinterForgeState.display.push('\r\n[bedrock] ' + event.message + '\r\n');
        }
        if (events.log_error) throw new Error('Host log write failed: ' + events.log_error);
        const state = await CinterForgeState.api('/api/bedrock/status');
        if (Date.now() - lastUpdate > 8000) {
          lastUpdate = Date.now();
          CinterForgeState.display.push(
            '\r\n[bedrock] Awaiting download response. Host log: ' +
            (events.path || state.log_path) + '\r\n'
          );
        }
        if (state.install_error) throw new Error(state.install_error);
        if (state.installed) {
          CinterForgeState.notice('Bedrock downloaded and installed. Type bedrock start.');
          return;
        }
        if (!state.installing) throw new Error('Bedrock installation stopped before completion');
        await new Promise(resolve => setTimeout(resolve, 750));
      }
    } catch (e) {
      CinterForgeState.notice('Bedrock install failed: ' + e.message);
      CinterForgeState.display.push('\r\n[bedrock] ' + e.message + '\r\n[bedrock] Type bedrock log for details.\r\n');
    }
  };

  // The live server's compact events are separate from the VM's full PTY
  // transcript. Status messages are text only; Minecraft cannot execute JS.
  let minecraftSayCursor = 0;
  let minecraftSayPolling = false;
  setInterval(async () => {
    if (document.visibilityState === 'hidden' || minecraftSayPolling) return;
    minecraftSayPolling = true;
    try {
      const response = await CinterForgeState.api('/api/bedrock/events?cursor=' + minecraftSayCursor);
      for (const event of response.events || []) {
        if (event.id > minecraftSayCursor && event.kind === 'minecraft-say')
          CinterForgeState.notice(String(event.message));
      }
      minecraftSayCursor = Number(response.cursor) || 0;
    } catch (_) { /* No Bedrock installation, WebView closing, or offline host. */ }
    finally { minecraftSayPolling = false; }
  }, 1250);

  // Original source lines 2882–2902
CinterForgeState.$('license-confirm').addEventListener('click', async () => {
    if (!CinterForgeState.accepted.checked || CinterForgeState.licensePending) return;
    CinterForgeState.licensePending = true;
    CinterForgeState.$('license-confirm').disabled = true;
    CinterForgeState.$('license-cancel').disabled = true;
    try {
      await CinterForgeState.api('/api/bedrock/accept', { accepted: true });
      CinterForgeState.license.hidden = true;
      CinterForgeState.notice('EULA accepted. Downloading and installing official Bedrock server…');
      CinterForgeState.display.push('\r\n[bedrock] Downloading and installing official Bedrock server…\r\n');
      CinterForgeState.focusTerminal();
      void CinterForgeState.monitorBedrockInstall();
    } catch (e) {
      CinterForgeState.notice(e.message);
    }
    finally {
      CinterForgeState.licensePending = false;
      CinterForgeState.$('license-cancel').disabled = false;
      CinterForgeState.$('license-confirm').disabled = !CinterForgeState.accepted.checked;
    }
  });

  // The unprivileged report iframe may request the EXISTING WebView license
  // dialog, but never accepts EULA consent on the user's behalf.
  window.addEventListener('message', event => {
    if (event.origin === location.origin && event.data?.type === 'cinter-bedrock-pack-changed') {
      const packTab = CinterForgeState.tabs?.find(tab => !tab.closed &&
        tab.kind === 'asset-view' && tab.assetKey === 'packs' &&
        tab.mapFrame?.contentWindow === event.source);
      if (!packTab) return;
      for (const tab of CinterForgeState.tabs || []) {
        if (!tab.closed && tab.kind === 'asset-view' && tab.assetKey === 'bedrock-report') {
          tab.mapFrame?.contentWindow?.postMessage({type:'cinter-bedrock-pack-changed'}, location.origin);
        }
      }
      window.dispatchEvent(new Event('cinter-bedrock-pack-changed'));
      return;
    }
    if (event.origin !== location.origin || event.data?.type !== 'cinter-bedrock-report') return;
    const reportTab = CinterForgeState.tabs?.find(tab => !tab.closed &&
      tab.kind === 'asset-view' && tab.assetKey === 'bedrock-report' &&
      tab.mapFrame?.contentWindow === event.source);
    if (!reportTab) return;
    if (event.data.action === 'eula') {
      void CinterForgeState.api('/api/bedrock/eula', {}).then(() =>
        CinterForgeState.requestLicense()).catch(error => CinterForgeState.notice(error.message));
    } else if (event.data.action === 'server-started') {
      CinterForgeState.notice('Bedrock opened in its interactive terminal tab');
    }
  });

})();
