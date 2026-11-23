/* Terminal-owned tab and file chooser integration for the canonical Assets library.
 * The actual browser, animation and Blockify controls are the original WebApp implementation.
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

  async function awaitAssetMigration(job, uci) {
    const started = Date.now();
    let lastNotice = '';
    let lastHeartbeat = started;
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const progress = await state.api('/api/assets/blockify/job/' + encodeURIComponent(job) + '?summary=1');
      const message = (progress.messages || []).at(-1) || '';
      const notice = 'Asset ' + uci + ': ' + progress.state + ' · '
        + (Number(progress.completed) + Number(progress.failed)) + '/' + progress.total
        + (message ? ' · ' + message : '');
      if (notice !== lastNotice || Date.now() - lastHeartbeat >= 30000) {
        state.notice(notice);
        lastNotice = notice;
        lastHeartbeat = Date.now();
      }
      if (progress.state === 'error') {
        throw new Error(progress.error || message || 'Original asset migration failed');
      }
      if (progress.state === 'finished') {
        return;
      }
      if (progress.state === 'queued' && Date.now() - started > 90000) {
        throw new Error('Original asset migration did not start in its Asset tab');
      }
    }
  }

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
    let sourceCommitted = false;
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
      sourceCommitted = true;
      toggleDialog(false);
      if (result.conversion_job) {
        state.notice('Saved ' + result.uci + '; original migration running before asset is ready');
        await awaitAssetMigration(result.conversion_job, result.uci);
        state.notice('Imported ' + result.uci + ': Solid and Block migration complete');
      } else {
        state.notice('Imported asset ' + result.uci + ' · opening Asset Viewer');
      }
    } catch (error) {
      // A failed migration must not be misreported as a completed import or
      // cause removal of the already-committed original source GLB.
      const prefix = sourceCommitted ? 'Asset source preserved; migration failed: ' : 'Asset import failed: ';
      status.textContent = prefix + error.message;
      state.notice(prefix + error.message);
      if (!sourceCommitted && session?.id) {
        void state.api('/api/assets/upload/cancel', {id: session.id}).catch(() => {});
      }
    } finally {
      working = false;
    }
  };
  input.addEventListener('change', () => {
    if (input.files?.[0]) void state.uploadAssetFile(input.files[0]);
  });

  // A removed Asset View or completed batch no longer belongs in a tab.
  // Reuse the existing report generator and tab opener, then close only the
  // sending asset tab. Do not expose report editing controls.
  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin ||
        event.data?.type !== 'cinterforge-assets-open-report') return;
    const tab = state.tabs.find(item => !item.closed && item.kind === 'asset-view' &&
      item.assetKey !== 'report' && item.mapFrame?.contentWindow === event.source);
    if (!tab) return;
    void (async () => {
      const report = await state.api('/api/assets/report', {});
      await state.openAssetTab(report);
      await state.closeTab(tab);
    })().catch(error => state.notice('Unable to open Assets Report: ' + error.message));
  });

  state.openAssetTab = async request => {
    if (!['report', 'view', 'blockify', 'packs', 'bedrock-report'].includes(request?.kind) || typeof request.url !== 'string' ||
        !(request.kind === 'bedrock-report' ? request.url === '/bedrock-report/index.html' : request.url.startsWith('/assets-library/'))) return;
    const identity = request.kind === 'bedrock-report' ? 'bedrock-report' : request.kind === 'packs' ? 'packs' : request.kind === 'report' ? 'report' : request.kind === 'blockify' ? 'blockify' : request.uci;
    if (request.kind === 'view' && !/^CINTERASST[A-Za-z0-9#_-]{5,90}$/.test(identity || '')) return;
    const current = state.tabs.find(tab => !tab.closed && tab.kind === 'asset-view' && tab.assetKey === identity);
    if (current) {
      current.mapFrame.src = request.url + (request.url.includes('?') ? '&' : '?') + 'refresh=' + Date.now();
      await state.activateTab(current);
      return;
    }
    const frame = document.createElement('iframe');
    frame.title = request.kind === 'bedrock-report' ? 'Bedrock and CraftMaster world report' : request.kind === 'packs' ? 'CinterForge Minecraft Packs' : request.kind === 'report' ? 'CinterForge Assets library report' : request.kind === 'blockify' ? 'CinterForge original Blockify authoring' : 'CinterForge asset ' + identity;
    frame.referrerPolicy = 'no-referrer';
    frame.hidden = true;
    state.computerView.append(frame);
    const buffer = state.makeBuffer();
    const tab = {
      key: ++state.tabSerial,
      label: request.kind === 'bedrock-report' ? 'Bedrock · Worlds' : request.kind === 'packs' ? 'Bedrock · Packs' : request.kind === 'report' ? 'Assets · Report' : request.kind === 'blockify' ? 'Assets · Blockify' : 'Asset · ' + (request.name || identity),
      kind: 'asset-view', assetKey: identity, mapFrame: frame, profileId: 'modern',
      session: null, closed: false, display: new state.VTOutput(buffer), buffer
    };
    tab.display.target = buffer;
    state.tabs.push(tab);
    await state.activateTab(tab);
    frame.src = request.url;
  };

  // Reuse the very same terminal and emulator profiles used by the museum
  // menus. The gallery is only a different discovery surface, never a second
  // emulator launcher or a second catalog of boot configurations.
  state.openEmulatorGalleryTab = async () => {
    const open = state.tabs.find(tab => !tab.closed && tab.kind === 'asset-view' && tab.assetKey === 'emulators');
    if (open) {
      await state.activateTab(open);
      return;
    }
    const styles = state.profiles.filter(profile => profile?.kind === 'terminal' &&
      typeof profile.id === 'string');
    const machines = state.uniqueMuseumEmulators(state.profiles);
    if (!machines.length || !styles.length) throw new Error('Museum catalog is not ready');
    const byYear = (a, b) => (Number(a.year) || 0) - (Number(b.year) || 0) || a.name.localeCompare(b.name);
    machines.sort(byYear);
    styles.sort(byYear);
    const sections = ['Terminal Styles', 'macOS', 'BeOS', 'NeXTSTEP'];
    const familyOf = profile => profile.kind === 'terminal' ? 'Terminal Styles' :
      profile.os === 'beos' ? 'BeOS' :
      /nextstep|openstep/i.test(String(profile.disk || '')) || /^next/i.test(profile.id) ? 'NeXTSTEP' : 'macOS';
    const element = (tag, cls, value) => {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      if (value != null) node.textContent = String(value);
      return node;
    };
    const shell = element('section', 'emulator-gallery');
    shell.hidden = true;
    shell.setAttribute('aria-label', 'Terminal styles and Infinite Mac emulators');
    const header = element('header', 'emulator-gallery-header');
    const heading = element('h2', '', 'Infinite Mac & Terminal Styles');
    const search = element('input', 'emulator-gallery-search');
    search.type = 'search';
    search.placeholder = 'Search styles, emulators, systems, years…';
    search.setAttribute('aria-label', 'Search terminal styles and emulators');
    const family = element('select', 'emulator-gallery-filter');
    family.setAttribute('aria-label', 'Filter by operating system or styles');
    for (const name of ['All sections', ...sections]) {
      const option = element('option', '', name);
      option.value = name === 'All sections' ? '' : name;
      family.append(option);
    }
    const count = element('span', 'emulator-gallery-count');
    header.append(heading, search, family, count);
    const scroll = element('div', 'emulator-gallery-scroll');
    const entries = [];
    const groups = new Map();
    for (const name of sections) {
      const section = element('section', 'emulator-gallery-section');
      section.dataset.family = name;
      const sectionHeader = element('div', 'emulator-gallery-section-header');
      const title = element('h3', '', name);
      const tally = element('span', 'emulator-gallery-section-count');
      sectionHeader.append(title, tally);
      const grid = element('div', 'emulator-gallery-grid');
      section.append(sectionHeader, grid);
      scroll.append(section);
      groups.set(name, {section, grid, tally, entries: []});
    }
    const addText = (parent, tag, cls, value) => parent.append(element(tag, cls, value));
    for (const profile of [...styles, ...machines]) {
      const group = familyOf(profile);
      const container = groups.get(group);
      if (!container) continue;
      const card = element('article', 'emulator-gallery-card cf-card cf-card--machine');
      const searchText = [profile.name, profile.machine, profile.disk,
        profile.year, profile.columns, group].filter(Boolean).join(' ').toLocaleLowerCase();
      const displayName = group === 'Terminal Styles' ? profile.name : profile.disk;
      const thumb = element('button', 'emulator-gallery-thumb cf-card__media');
      thumb.type = 'button';
      thumb.setAttribute('aria-label', 'Open ' + displayName + ' on ' + (profile.machine || profile.name) + ' in a new Terminal tab');
      thumb.title = 'Open ' + displayName;
      thumb.dataset.family = group;
      if (group === 'Terminal Styles') {
        // A true sample of this profile's authored palette, not an image of
        // an operating system that this presentation-only style cannot run.
        const preview = element('div', 'emulator-gallery-style-preview');
        preview.style.setProperty('--preview-bg', profile.background);
        preview.style.setProperty('--preview-fg', profile.foreground);
        preview.style.setProperty('--preview-accent', profile.accent);
        addText(preview, 'span', 'emulator-gallery-style-caption', 'CinterForge Terminal');
        const screen = element('pre', 'emulator-gallery-style-screen');
        screen.textContent = '> help\n' + profile.name + '\n> _';
        preview.append(screen);
        thumb.append(preview);
      } else {
        // Lightweight visual depiction of the selected OS family; no heavy
        // embedded emulator is started just to populate a search result.
        const screen = element('div', 'emulator-gallery-desktop');
        screen.dataset.family = group;
        const menubar = element('div', 'emulator-gallery-desktop-menu');
        addText(menubar, 'span', '', group === 'macOS' ? '◆  File  Edit  View' :
          group === 'BeOS' ? 'Be  Deskbar' : 'Workspace  Tools  Services');
        const window = element('div', 'emulator-gallery-desktop-window');
        addText(window, 'div', 'emulator-gallery-desktop-window-title', profile.disk || profile.name);
        addText(window, 'div', 'emulator-gallery-desktop-window-text', profile.machine || profile.name);
        addText(window, 'div', 'emulator-gallery-desktop-window-lines', '▣    ▤    ▥');
        screen.append(menubar, window);
        thumb.append(screen);
      }
      thumb.addEventListener('click', () => {
        // The existing profile/menu action creates the real Session tab.
        // Do not hide or destroy the gallery: returning to it must work.
        state.openMuseumProfile(profile.id);
      });
      const body = element('div', 'emulator-gallery-meta cf-card__details');
      addText(body, 'strong', '', displayName);
      addText(body, 'small', '', group === 'Terminal Styles'
        ? [profile.year, profile.columns ? profile.columns + ' columns' : ''].filter(Boolean).join(' · ')
        : [profile.year, profile.machine].filter(Boolean).join(' · '));
      const actions = element('div', 'cf-card__actions');
      const open = element('button', '', group === 'Terminal Styles' ? 'Apply Style' : 'Open Emulator');
      open.type = 'button';
      open.addEventListener('click', () => state.openMuseumProfile(profile.id));
      const note = element('span', 'cf-card__action-note', group === 'Terminal Styles'
        ? 'Terminal' : group);
      actions.append(open, note);
      card.append(thumb, body, actions);
      container.grid.append(card);
      const entry = {card, query: searchText, family: group};
      entries.push(entry);
      container.entries.push(entry);
    }
    const refresh = () => {
      const query = search.value.trim().toLocaleLowerCase();
      let visible = 0;
      for (const entry of entries) {
        entry.card.hidden = (Boolean(family.value) && family.value !== entry.family) ||
          (Boolean(query) && !entry.query.includes(query));
        if (!entry.card.hidden) visible++;
      }
      for (const group of groups.values()) {
        const shown = group.entries.filter(entry => !entry.card.hidden).length;
        group.section.hidden = shown === 0;
        group.tally.textContent = shown + ' of ' + group.entries.length;
      }
      count.textContent = visible + ' of ' + entries.length + ' profiles';
    };
    search.addEventListener('input', refresh);
    family.addEventListener('change', refresh);
    shell.append(header, scroll);
    refresh();
    state.computerView.append(shell);
    const buffer = state.makeBuffer();
    const tab = {key: ++state.tabSerial, label: 'Infinite Mac · Emulators',
      kind: 'asset-view', assetKey: 'emulators', mapFrame: shell,
      profileId: 'modern', session: null, closed: false,
      display: new state.VTOutput(buffer), buffer};
    tab.display.target = buffer;
    state.tabs.push(tab);
    await state.activateTab(tab);
    search.focus();
  };

  let polling = false;
  setInterval(async () => {
    if (polling || document.visibilityState === 'hidden') return;
    polling = true;
    try {
      const request = await state.api('/api/assets/next');
      if (request?.kind === 'picker') state.openAssetPicker();
      else if (request?.kind === 'emulators') await state.openEmulatorGalleryTab();
      else if (request?.kind) await state.openAssetTab(request);
    } catch (_) { /* Not configured, no credentials, or server exiting. */ }
    finally { polling = false; }
  }, 650);
})();
