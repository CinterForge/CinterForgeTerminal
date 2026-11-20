/* Terminal-owned tab and file chooser integration for the canonical Assets library.
 * 3D model rendering and animation playback use the Terminal-owned gallery.
 */
(() => {
  'use strict';
  const state = window.CinterForgeClientState;
  if (!state) return;

  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.zip,.glb,.gltf';
  input.hidden = true;
  document.body.append(input);
  const dialog = document.createElement('div');
  dialog.hidden = true;
  dialog.style.cssText = 'position:fixed;inset:0;z-index:5000;background:#000b;display:grid;place-items:center';
  dialog.innerHTML = '<section role="dialog" aria-modal="true" aria-label="Import Assets" style="background:#1b1e23;color:white;padding:24px;border:1px solid #555;border-radius:12px;max-width:450px;font:15px system-ui">' +
    '<h3>Import asset</h3><p>Select a GLB file or canonical UCI ZIP using the native file chooser. A GLTF with external textures must be imported by an absolute local path.</p>' +
    '<p id="asset-upload-status" role="status"></p><button id="asset-choose" type="button">Choose File…</button> ' +
    '<button id="asset-dismiss" type="button">Cancel</button></section>';
  document.body.append(dialog);
  const status = dialog.querySelector('#asset-upload-status');
  let working = false;
  // Explicit hidden rather than CSS display; hidden alone cannot beat a CSS grid.
  const toggleDialog = visible => { dialog.hidden = !visible; dialog.style.display = visible ? 'grid' : 'none'; };
  toggleDialog(false);

  state.openAssetPicker = () => {
    if (working) return;
    status.textContent = '';
    toggleDialog(true);
  };
  dialog.querySelector('#asset-choose').addEventListener('click', () => {
    if (working) return;
    // Calling click directly from this trusted button gesture causes the OS's
    // standard WebView file chooser, unlike clicking from an async poll timer.
    input.value = '';
    input.click();
  });
  dialog.querySelector('#asset-dismiss').addEventListener('click', () => {
    if (!working) toggleDialog(false);
  });

  state.uploadAssetFile = async file => {
    if (working || !(file instanceof File)) return;
    if (!/\.(zip|glb|gltf)$/i.test(file.name) || file.size > 256 * 1024 * 1024 || !file.size) {
      state.notice('Asset file must be a nonempty ZIP/GLB/GLTF under 256 MiB for the picker; use assets import <path> for larger files.');
      return;
    }
    working = true;
    toggleDialog(true);
    status.textContent = 'Preparing ' + file.name + '…';
    let session;
    try {
      // The browser file dialog does not grant the Host arbitrary file access:
      // upload exactly the selected bytes to the authenticated session.
      const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
      const sha = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
      session = await state.api('/api/assets/upload/start', {name: file.name, size: file.size});
      const size = session.chunk_bytes;
      for (let offset = 0; offset < file.size; offset += size) {
        const bytes = new Uint8Array(await file.slice(offset, offset + size).arrayBuffer());
        let chars = '';
        for (let i = 0; i < bytes.length; i += 0x4000)
          chars += String.fromCharCode(...bytes.subarray(i, i + 0x4000));
        const reply = await state.api('/api/assets/upload/chunk', {id: session.id, offset, data: btoa(chars)});
        status.textContent = `Uploading ${file.name}: ${Math.round(reply.received * 100 / reply.size)}%`;
      }
      const result = await state.api('/api/assets/upload/finish', {id: session.id, sha256: sha});
      status.textContent = 'Imported ' + result.uci;
      state.notice('Imported asset ' + result.uci + ' · opening 3D viewer');
      toggleDialog(false);
    } catch (error) {
      status.textContent = 'Import failed: ' + error.message;
      state.notice('Asset import failed: ' + error.message);
      if (session?.id) void state.api('/api/assets/upload/cancel', {id: session.id}).catch(() => {});
    } finally {
      working = false;
    }
  };
  input.addEventListener('change', () => {
    if (input.files?.[0]) void state.uploadAssetFile(input.files[0]);
  });

  state.openAssetTab = async request => {
    if (!['report', 'view'].includes(request?.kind) || typeof request.url !== 'string' ||
        !request.url.startsWith('/assets-library/')) return;
    const identity = request.kind === 'report' ? 'report' : request.uci;
    if (request.kind === 'view' && !/^CINTERASST[A-Za-z0-9#_-]{5,90}$/.test(identity || '')) return;
    const current = state.tabs.find(tab => !tab.closed && tab.kind === 'asset-view' && tab.assetKey === identity);
    if (current) {
      current.mapFrame.src = request.url + (request.url.includes('?') ? '&' : '?') + 'refresh=' + Date.now();
      await state.activateTab(current);
      return;
    }
    const frame = document.createElement('iframe');
    frame.title = request.kind === 'report' ? 'CinterForge Assets library report' : 'CinterForge asset ' + identity;
    frame.referrerPolicy = 'no-referrer';
    frame.hidden = true;
    state.computerView.append(frame);
    const buffer = state.makeBuffer();
    const tab = {
      key: ++state.tabSerial,
      label: request.kind === 'report' ? 'Assets · Report' : 'Asset · ' + (request.name || identity),
      kind: 'asset-view', assetKey: identity, mapFrame: frame, profileId: 'modern',
      session: null, closed: false, display: new state.VTOutput(buffer), buffer
    };
    tab.display.target = buffer;
    state.tabs.push(tab);
    await state.activateTab(tab);
    frame.src = request.url;
  };

  let polling = false;
  setInterval(async () => {
    if (polling || document.visibilityState === 'hidden') return;
    polling = true;
    try {
      const request = await state.api('/api/assets/next');
      if (request?.kind === 'picker') state.openAssetPicker();
      else if (request?.kind) await state.openAssetTab(request);
    } catch (_) { /* Not configured, no credentials, or server exiting. */ }
    finally { polling = false; }
  }, 650);
})();
