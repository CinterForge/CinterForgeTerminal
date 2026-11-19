/*
 * CinterForgeCraftMasterMap.js — persistent native Zoomify viewer tabs.
 *
 * The existing Terminal HTTP server serves map tiles from its managed runtime
 * directory. No new port, spawned preview server, giant PNG, or external
 * browser window is required. The Bedrock console remains a separate tab.
 */
(() => {
  'use strict';
  // Render actual 512px ZFY pyramid tiles immediately, even before the
  // converter publishes Live Map.htm or ImageProperties.xml. This same module
  // is already staged with the Terminal; no second preview server/viewer file.
  if (document.documentElement.dataset.craftmasterLiveMap === '1') {
    const prefix = location.pathname.replace(/Live(?:%20| )Map\.htm$/i, '');
    if (!/^\/craftmaster-map\/[0-9a-f]{32}\/$/.test(prefix)) return;
    const screen = document.getElementById('cf-map-screen');
    const stage = document.getElementById('cf-map-stage');
    const message = document.getElementById('cf-map-state');
    const error = document.getElementById('cf-map-error');
    const images = new Map();
    let zoomLevel = null, baseX = 0, baseY = 0, scale = 1, tx = 0, ty = 0;
    let firstTiles = false, dragging = null, fetching = false, retired = false;

    const render = () => {
      stage.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    };
    screen.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      dragging = {id: event.pointerId, x: event.clientX, y: event.clientY};
      screen.setPointerCapture(event.pointerId);
      screen.classList.add('dragging');
    });
    screen.addEventListener('pointermove', event => {
      if (!dragging || dragging.id !== event.pointerId) return;
      tx += event.clientX - dragging.x;
      ty += event.clientY - dragging.y;
      dragging.x = event.clientX;
      dragging.y = event.clientY;
      render();
    });
    const releaseDrag = () => { dragging = null; screen.classList.remove('dragging'); };
    screen.addEventListener('pointerup', releaseDrag);
    screen.addEventListener('pointercancel', releaseDrag);
    screen.addEventListener('wheel', event => {
      event.preventDefault();
      const targetScale = Math.min(8, Math.max(0.125, scale * (event.deltaY < 0 ? 1.2 : 1 / 1.2)));
      const rect = screen.getBoundingClientRect();
      const x = event.clientX - rect.left, y = event.clientY - rect.top;
      tx = x - (x - tx) * targetScale / scale;
      ty = y - (y - ty) * targetScale / scale;
      scale = targetScale;
      render();
    }, {passive: false});

    const update = async () => {
      if (fetching || retired || document.visibilityState === 'hidden') return;
      fetching = true;
      try {
        const response = await fetch(prefix + 'tiles.json', {cache: 'no-store'});
        if (!response.ok) throw new Error('Map tile index: HTTP ' + response.status);
        const result = await response.json();
        if (result.native_ready) {
          // CraftMaster's authored Zoomify viewer now owns this SAME iframe.
          retired = true;
          location.replace(prefix + 'Live%20Map.htm?native=1&refresh=5');
          return;
        }
        if (result.error) {
          error.textContent = result.error;
          error.style.display = 'block';
        }
        const tiles = Array.isArray(result.tiles) ? result.tiles : [];
        if (zoomLevel === null && tiles.length) {
          // Start on the first populated ZFY pyramid level. Later tiles fill in
          // without moving the viewport underneath the user.
          const byLevel = new Map();
          for (const tile of tiles) byLevel.set(tile.level, (byLevel.get(tile.level) || 0) + 1);
          zoomLevel = [...byLevel].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0][0];
          const first = tiles.find(tile => tile.level === zoomLevel);
          baseX = first.x; baseY = first.y;
          tx = Math.round(screen.clientWidth / 2 - 256);
          ty = Math.round(screen.clientHeight / 2 - 256);
          firstTiles = true;
          render();
        }
        for (const tile of tiles) {
          if (tile.level !== zoomLevel || !Number.isSafeInteger(tile.x) || !Number.isSafeInteger(tile.y)) continue;
          if (!/^(?:[^/.][^/]*\/){0,4}TileGroup\d+\/\d+-\d+-\d+\.(?:png|jpg|jpeg)$/i.test(tile.path)) continue;
          const previous = images.get(tile.path);
          if (previous && previous.stamp === tile.stamp) continue;
          if (previous) previous.element.remove();
          const img = document.createElement('img');
          img.alt = '';
          img.draggable = false;
          img.style.left = `${(tile.x - baseX) * 512}px`;
          img.style.top = `${(tile.y - baseY) * 512}px`;
          img.onerror = () => {
            if (images.get(tile.path)?.element === img) images.delete(tile.path);
            img.remove(); // Retry the tile on the next poll if still being written.
          };
          img.src = prefix + tile.path.split('/').map(encodeURIComponent).join('/') + '?v=' + encodeURIComponent(tile.stamp);
          stage.append(img);
          images.set(tile.path, {element: img, stamp: tile.stamp});
        }
        message.textContent = firstTiles
          ? `${images.size} live Zoomify tiles · drag to pan · scroll to zoom`
          : result.discovery === 'waiting-for-map' ? 'Waiting for CraftMaster map output…'
          : result.discovery === 'waiting-for-pyramid' ? 'Waiting for Zoomify pyramid…'
          : 'Waiting for first Zoomify tile…';
      } catch (err) {
        if (!firstTiles) message.textContent = 'Waiting for Zoomify tile stream…';
      } finally {
        fetching = false;
      }
    };
    void update();
    setInterval(() => { void update(); }, 1400);
    return;
  }
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
    frame.src = request.url;
    await state.activateTab(mapTab);
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
