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
    const frame = document.createElement('iframe');
    frame.title = 'CraftMaster live Zoomify map: ' + request.world;
    frame.referrerPolicy = 'no-referrer';
    frame.hidden = true;
    state.computerView.append(frame);
    const buffer = state.makeBuffer();
    const mapTab = {
      key: ++state.tabSerial,
      label: 'Map · ' + request.world,
      kind: 'craftmaster-map',
      profileId: 'modern',
      mapId: request.id,
      mapFrame: frame,
      session: null,
      closed: false,
      display: new state.VTOutput(buffer),
      buffer
    };
    mapTab.display.target = buffer;
    state.tabs.push(mapTab);
    await state.activateTab(mapTab);

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
