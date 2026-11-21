/*
 * CinterForgeCraftMasterMap.js — tabs for CraftMaster's authored Zoomify viewer.
 *
 * The existing Terminal HTTP server serves map tiles from its managed runtime
 * directory. No new port, spawned preview server, giant PNG, or external
 * browser window is required. The Bedrock console remains a separate tab.
 */
(() => {
  'use strict';
  const state = window.CinterForgeClientState;
  if (!state) return;

  state.openCraftMasterMapTab = async function openCraftMasterMapTab(request) {
    if (!/^[0-9a-f]{32}$/.test(request?.id || '') ||
        typeof request.url !== 'string' ||
        !request.url.startsWith('/craftmaster-map/' + request.id + '/')) return;
    const existing = state.tabs.find(tab =>
      tab.kind === 'craftmaster-map' && tab.mapId === request.id && !tab.closed);
    if (existing) {
      await state.activateTab(existing);
      return;
    }
    const shell = document.createElement('div');
    shell.className = 'craftmaster-map-shell';
    shell.hidden = true;
    const controls = document.createElement('div');
    controls.className = 'craftmaster-map-controls';
    const frame = document.createElement('iframe');
    frame.title = 'CraftMaster live Zoomify map: ' + request.world;
    frame.referrerPolicy = 'no-referrer';
    shell.append(controls, frame);
    state.computerView.append(shell);
    const buffer = state.makeBuffer();
    const mapTab = {
      key: ++state.tabSerial,
      label: 'Map · ' + request.world,
      kind: 'craftmaster-map',
      profileId: 'modern',
      mapId: request.id,
      mapFrame: shell,
      session: null,
      closed: false,
      display: new state.VTOutput(buffer),
      buffer
    };
    mapTab.display.target = buffer;
    state.tabs.push(mapTab);
    await state.activateTab(mapTab);

    const create = (label, action) => {
      const node = document.createElement('button');
      node.type = 'button'; node.textContent = label;
      node.addEventListener('click', () => {void action().catch(error => state.notice(error.message));});
      controls.append(node);
      return node;
    };
    const played = document.createElement('span'), updated = document.createElement('span');
    const pack = document.createElement('span'), progress = document.createElement('span');
    controls.append(played, updated);
    const update = create('Update for Bedrock', async () => {
      const result = await state.api('/api/craftmaster/map/run', {name: request.world});
      state.notice('CraftMaster update started in map tab ' + (result.map?.world || request.world));
    });
    const select = create('Set Current', async () => {
      const details = await state.api('/api/bedrock/report');
      const world = details.java_worlds.find(w => w.command_name === request.world);
      if (!world) throw new Error('Java world is no longer available');
      await state.api('/api/bedrock/world/select', {name:world.id});
      await refreshControls();
    });
    controls.append(pack);
    const startStop = create('Start', async () => {
      const details = await state.api('/api/bedrock/report');
      if (details.running) await state.api('/api/bedrock/stop', {});
      else {
        const world = details.java_worlds.find(w => w.command_name === request.world);
        if (!world) throw new Error('Java world is no longer available');
        if (!world.current) await state.api('/api/bedrock/world/select', {name: world.id});
        let allow_stale=false;
        if (world.conversion?.needs_update) {
          if (!world.conversion.has_conversion || confirm('Update for Bedrock before starting?')) {
            await state.api('/api/craftmaster/map/run', {name:request.world});
            state.notice('CraftMaster update started; start again after it finishes');
            return;
          }
          if (!confirm('Run the last converted world anyway?')) return;
          allow_stale=true;
        }
        try {await state.api('/api/bedrock/run', {allow_stale});}
        catch (error) {
          if (/EULA|not installed/i.test(error.message)) {
            await state.api('/api/bedrock/eula', {});
            state.requestLicense();
          }
          throw error;
        }
      }
      await refreshControls();
    });
    controls.append(progress);
    const format = value => value ? new Date(value * 1000).toLocaleString() : 'Unknown';
    const refreshControls = async () => {
      if (mapTab.closed) return;
      const details = await state.api('/api/bedrock/report');
      const world = details.java_worlds.find(w => w.command_name === request.world);
      if (!world) return;
      played.textContent='Last Played: ' + format(world.conversion?.last_played);
      updated.textContent='Last Updated: ' + format(world.conversion?.last_updated);
      update.disabled = world.conversion?.needs_update !== true;
      select.disabled = world.current === true;
      select.textContent = world.current ? '✓ Current World' : 'Set Current';
      pack.textContent = 'Current Pack: ' + (world.pack || 'None');
      startStop.textContent = details.running ? 'Stop' : 'Start';
      try {
        const phase = await state.api('/api/craftmaster/map/status?id=' + request.id);
        progress.textContent = phase.phase === 'failed' ? 'Failed: ' + phase.message :
          phase.phase === 'preparing' ? phase.message :
          world.conversion?.ready ? 'Bedrock ready' :
          !world.conversion?.has_conversion ? 'Not converted to Bedrock' :
          world.conversion?.needs_update ? 'Update for Bedrock required' :
          'Bedrock conversion present · freshness unverified';
      } catch (_) {progress.textContent = 'Map status unavailable';}
    };
    mapTab.mapControlTimer = setInterval(() => {
      if (mapTab.closed) {clearInterval(mapTab.mapControlTimer);return;}
      if (state.activeTab === mapTab) void refreshControls().catch(error => state.notice(error.message));
    }, 30000);
    window.addEventListener('cinter-bedrock-pack-changed', () => {
      if (!mapTab.closed) void refreshControls().catch(error => state.notice(error.message));
    });
    void refreshControls().catch(error => state.notice(error.message));

    // The converter writes its own viewer + all black ZFY placeholders first.
    // Poll only its HTML readiness. Load that SAME authored viewer once, then
    // leave it in place to handle fit, zoom and incremental tile replacement.
    // There is deliberately no Terminal-owned tile renderer or HTML fallback.
    let probing = false;
    const loadNativeViewer = async () => {
      if (mapTab.closed) {
        clearInterval(mapTab.mapReadyTimer);
        return;
      }
      if (probing || mapTab.mapReady) return;
      probing = true;
      try {
        const response = await fetch(request.url, {cache: 'no-store'});
        if (response.status === 200 && !mapTab.closed) {
          mapTab.mapReady = true;
          clearInterval(mapTab.mapReadyTimer);
          frame.src = request.url;
        }
      } catch (_) {
        // The existing conversion log owns errors; the map stays uninitialized.
      } finally {
        probing = false;
      }
    };
    mapTab.mapReadyTimer = setInterval(() => { void loadNativeViewer(); }, 1000);
    void loadNativeViewer();
  };

  // Requests are pushed before conversion starts. Never wait for the synchronous
  // Bedrock run request to finish before showing the world preview tab.
  let polling = false;
  setInterval(async () => {
    if (polling || document.visibilityState === 'hidden') return;
    polling = true;
    try {
      const request = await state.api('/api/craftmaster/map/next');
      if (request?.id) await state.openCraftMasterMapTab(request);
    } catch (_) {
      // App shutdown / disconnected HTTP owner: no extra UI error tab.
    } finally {
      polling = false;
    }
  }, 500);
})();
