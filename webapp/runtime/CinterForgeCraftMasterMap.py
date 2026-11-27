"""Live CraftMaster Zoomify map tabs within the existing Terminal HTTP server.

The Map tab only embeds the single live Zoomify viewer authored by CraftMaster.
It waits for CraftMaster's atomic viewer/pyramid initialization rather than
introducing a duplicate preview renderer or replacing a live viewer.

Only generated assets under the selected, server-owned CraftMaster Maps tree are
served. Java saves, archives, LevelDB and arbitrary host paths are never exposed.
"""

from __future__ import annotations

import html
import types
import json
import mimetypes
import os
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import uuid
from pathlib import Path
from urllib.parse import unquote, quote, urlsplit, parse_qs

from runtime.CinterForgeHTTPAuthorization import CinterForgeHTTPAuthorization


MAP_HTTP_PORT = 20330
MAX_VISIBLE_MAPS = 16
MAX_MAP_ASSET_BYTES = 8 * 1024 * 1024


_provider_lock = threading.RLock()
_provider = {}


def _map_provider(server):
    # CraftMaster owns the annotated map schema; reuse its manifest-verified
    # preview resource instead of maintaining a second persistence implementation.
    manager = server.bedrock
    with _provider_lock:
        cached = _provider.get(manager)
        if cached is not None: return cached
        _, data = manager._craftmaster_cli()
        path = data / 'preview.py'
        module = types.ModuleType('craftmaster_map_preview')
        module.__file__ = str(path)
        exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
        _provider[manager] = module
        return module


def _local_java_source(server, state):
    # The map ID is server-issued; no browser-supplied filesystem paths.
    directory = Path(state['directory'])
    source = directory.parent.parent.parent.parent
    try:
        if (directory != source / 'CinterForge' / 'Maps' / source.name / 'Overworld' or
                any(p.is_symlink() for p in (source / 'level.dat', directory, *directory.parents)) or
                not (source / 'level.dat').is_file() or (source / 'db').is_dir() or
                source.parent.resolve() not in server.bedrock._java_world_roots()):
            return None
        if server.bedrock._world_id({'edition': 'java', 'path': str(source)}) != state['world_id']:
            return None
        return source
    except (OSError, ValueError, RuntimeError):
        return None


def write_map_annotation(handler, path):
    match = re.fullmatch(r'/craftmaster-map/([0-9a-f]{32})/(MapAnnotations|MapPlayers)\.json',path)
    if not match: return False
    server = handler.server
    scheme = getattr(server,'scheme','http')
    origin = handler.headers.get('Origin')
    expected = scheme + '://' + handler.headers.get('Host', '')
    site = handler.headers.get('Sec-Fetch-Site', '').strip().lower()
    # Fetch Metadata describes the embedding context, not a credential. Native
    # WebViews may report "none" or "same-site" for legitimate map requests.
    # An exact Origin (or an issued map ID in a non-simple header when Origin
    # is opaque/absent) is the proof; explicit foreign origins and cross-site
    # requests remain forbidden. No CORS preflight is allowed for this route.
    supplied = handler.headers.get('X-Cinter-Map-Write', '')
    issued_header = supplied.isascii() and secrets.compare_digest(supplied, match.group(1))
    json_body = handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() == 'application/json'
    reason = None
    if not handler._local():
        reason = 'Map access requires a permitted local or LAN connection'
    elif not handler._lan_logged_in():
        reason = 'Map access requires Terminal login'
    elif site == 'cross-site':
        reason = 'Cross-site map writes are not allowed'
    elif origin not in (None, 'null', expected):
        reason = 'Map request Origin does not match this listener'
    elif origin in (None, 'null') and not issued_header:
        reason = 'Map write token missing or invalid; reload the updated atlas'
    elif not json_body:
        reason = 'Map writes require application/json'
    if reason:
        # Never log header values, credentials, annotations or map tokens.
        server.log_event('Rejected map annotation write: ' + reason)
        handler._json(403, {'error': reason}); return True
    with server.craftmaster_map_lock:
        state = server.craftmaster_maps.get(match.group(1))
        if state is not None: state = dict(state)
    if state is None or (hasattr(server,'scheme') and not state.get('remote')):
        handler._json(404,{'error':'Unknown map'});return True
    source = _local_java_source(server, state)
    if source is None:
        handler._json(403, {'error':'Editing requires a local Java save'}); return True
    try:
        n=int(handler.headers.get('Content-Length','0'))
        provider = _map_provider(server)
        if not 1<=n<=getattr(provider, 'MAP_ANNOTATION_MAX_REQUEST', 4096): raise ValueError('Invalid annotation request')
        edit=json.loads(handler.rfile.read(n))
        if match.group(2) == 'MapPlayers':
            if handler.client_address[0] not in ('127.0.0.1','::1') and getattr(server,'lan_login',None) is None:
                handler._json(403,{'error':'Player changes over LAN require Terminal login'}); return True
            # Serialize with existing native world conversions and recheck the
            # map job after acquiring its operation lock.
            if not server.bedrock_terminal_lock.acquire(blocking=False):
                handler._json(409,{'error':'World operation in progress; try again when complete'}); return True
            try:
                with server.craftmaster_map_lock:
                    if server.craftmaster_maps.get(match.group(1),{}).get('working'):
                        handler._json(409,{'error':'Map conversion in progress'}); return True
                binary,_ = server.bedrock._craftmaster_cli()
                result=provider.edit_map_player(source,state['directory'].parent,binary,edit)
            finally:
                server.bedrock_terminal_lock.release()
        else:
            result=provider.map_annotations(state['directory'].parent,edit)
        handler._json(200,result)
    except (OSError,ValueError,RuntimeError,TypeError,subprocess.SubprocessError) as error:
        server.log_event('Map edit failed: ' + str(error))
        handler._json(400,{'error':str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Could not save map edit'})
    return True


def open_map_tab(server, world: dict, *, preparing: bool = True, enqueue: bool = True) -> dict:
    """Queue a browser tab independently of Bedrock's interactive PTY."""
    manager = server.bedrock
    if manager is None:
        raise RuntimeError('CraftMaster requires a configured Bedrock runtime')
    world_name = manager.java_world_export_name(world)
    output_root = manager.craftmaster_output_root(world)
    # CraftMaster names its map cache after the original Java save folder,
    # not the shortened .mcworld/export name (e.g. Candlewood (Candlewood)).
    map_directory = output_root / 'Maps' / Path(world['path']).name / 'Overworld'
    world_id = world['id']
    with server.craftmaster_map_lock:
        map_id = next((key for key, entry in server.craftmaster_maps.items()
                       if entry.get('world_id') == world_id), None)
        if map_id is None:
            if enqueue and sum(bool(entry.get('tab_open')) for entry in server.craftmaster_maps.values()) >= MAX_VISIBLE_MAPS:
                raise RuntimeError('Too many CraftMaster map tabs; close an existing map')
            map_id = uuid.uuid4().hex
            server.craftmaster_maps[map_id] = {
                'world_id': world_id, 'name': world_name, 'directory': map_directory,
                'phase': 'preparing' if preparing else 'ready',
                'working': False, 'tab_open': False, 'remote': False,
                'message': 'Preparing live Zoomify map…' if preparing else 'Map ready',
            }
        elif preparing and not server.craftmaster_maps[map_id].get('working'):
            server.craftmaster_maps[map_id].update(
                phase='preparing', message='Preparing live Zoomify map…')
        request = {
            'id': map_id, 'world': world_name, 'world_id': world_id,
            'url': '/craftmaster-map/' + map_id + '/Overworld/Live%20Map.htm?refresh=5',
        }
        state = server.craftmaster_maps[map_id]
        if enqueue:
            if not state.get('tab_open') and sum(bool(entry.get('tab_open')) for entry in server.craftmaster_maps.values()) >= MAX_VISIBLE_MAPS:
                raise RuntimeError('Too many CraftMaster map tabs; close an existing map')
            state['tab_open'] = True
            if not any(row['id'] == map_id for row in server.craftmaster_map_requests):
                server.craftmaster_map_requests.append(request)
        else:
            state['remote'] = True
    return request


def open_existing_map(server, name: str) -> dict:
    """View the last authored Zoomify map without running CraftMaster."""
    if server.bedrock is None:
        raise RuntimeError('CraftMaster is unavailable')
    world = server.bedrock.find_java_world(name)
    if world is None:
        raise ValueError('No Java save named ' + repr(name))
    output = server.bedrock.craftmaster_output_root(world)
    viewer = output / 'Maps' / Path(world['path']).name / 'Overworld/Live Map.htm'
    if not viewer.is_file() or viewer.is_symlink():
        raise FileNotFoundError('No CraftMaster map yet; select Update for Bedrock')
    request = open_map_tab(server, world, preparing=False)
    return {'map': request, 'opened': True}


def map_progress(server, map_id: str) -> dict:
    if not re.fullmatch(r'[0-9a-f]{32}', map_id):
        raise ValueError('Invalid map tab ID')
    with server.craftmaster_map_lock:
        state = server.craftmaster_maps.get(map_id)
        if state is None:
            raise ValueError('Unknown Map tab')
        return {'phase': state['phase'], 'message': state['message'], 'name': state['name']}


def open_world_report(server) -> dict:
    """Reuse the existing iframe-tab command queue for the Bedrock report."""
    if server.bedrock is None:
        raise RuntimeError(server.bedrock_error or 'Bedrock is unavailable')
    request = {'kind': 'bedrock-report', 'url': '/bedrock-report/index.html'}
    with server.asset_actions_lock:
        server.asset_actions = type(server.asset_actions)(
            row for row in server.asset_actions if row['kind'] != 'bedrock-report')
        server.asset_actions.append(request)
    return request


def report_progress(server, map_id: str, message: str) -> None:
    """Update the live tab and retain the complete native progress in Bedrock logs."""
    server.bedrock._append_log(message)
    with server.craftmaster_map_lock:
        state = server.craftmaster_maps.get(map_id)
        if state is not None:
            state['message'] = message[-1200:]


def mark_finished(server, map_id: str, error: Exception | None = None) -> None:
    with server.craftmaster_map_lock:
        state = server.craftmaster_maps.get(map_id)
        if state is None:
            return
        state['working'] = False
        state['phase'] = 'failed' if error else 'ready'
        state['message'] = str(error) if error else 'Conversion complete; map stays live in this tab'
        if not state.get('remote') and not state.get('tab_open'):
            server.craftmaster_maps.pop(map_id, None)


def run_map_conversion(server, name: str, *, enqueue: bool = True, update_served_world: bool = True) -> dict:
    """Start `craftmaster <world>` in a worker; return before conversion ends."""
    if server.bedrock is None:
        raise RuntimeError(server.bedrock_error or 'CraftMaster is unavailable')
    world = server.bedrock.find_java_world(name)
    if world is None:
        raise ValueError('No local Java world named ' + repr(name))
    with server.craftmaster_map_lock:
        request = open_map_tab(server, world, enqueue=enqueue)
        state = server.craftmaster_maps[request['id']]
        if state.get('working'):
            return {'map': request, 'started': False, 'already_running': True}
        state.update(working=True, phase='preparing', message='Preparing live Zoomify map…')

    def convert() -> None:
        from runtime import CinterForgeSessions as _CinterForgeSessions
        manager = server.bedrock
        restart_name = None
        def progress(line: str) -> None:
            report_progress(server, request['id'], line)
        try:
            if not update_served_world:
                # A map-only web request updates the portable Java-derived assets.
                # It must never stop or replace the live Bedrock server's save.
                with server.bedrock_terminal_lock:
                    from runtime.CinterForgeJava import require_stopped_source
                    require_stopped_source(server, world)
                    manager.run_native_craftmaster(world, progress=progress)
                mark_finished(server, request['id'])
                return
            # The same operation lock guards missing-world import and startup.
            # An explicit update never overlaps a live server for that world.
            with server.bedrock_terminal_lock:
                from runtime.CinterForgeJava import require_stopped_source
                require_stopped_source(server, world)
                export_name = manager.java_world_export_name(world)
                selected = manager._world_settings().get('selected') or {}
                selected_names = {str(selected.get(key, '')).casefold()
                                  for key in ('name', 'folder', 'command_name')}
                with manager._lock:
                    running = manager._live_session_locked()[0] is not None
                if running and export_name.casefold() in selected_names:
                    restart_name = str(selected.get('folder') or export_name)
                    progress(export_name + ': stopping Bedrock before CraftMaster update')
                    manager.stop()  # Save and stop the server before touching its world.
                try:
                    source = manager._convert_java_world(world, progress=progress, force=True)
                    manager.refresh_managed_world(source)
                except Exception:
                    if restart_name:
                        # The old validated save stays available if conversion fails.
                        _CinterForgeSessions.open_bedrock_terminal(server, restart_name)
                    raise
                progress(export_name + ': updated Bedrock save installed')
                if restart_name:
                    progress(export_name + ': restarting Bedrock with updated save')
                    _CinterForgeSessions.open_bedrock_terminal(server, export_name)
                mark_finished(server, request['id'])
        except Exception as error:
            mark_finished(server, request['id'], error)
            server.log_event('[CraftMaster] update failed: ' + str(error))

    threading.Thread(
        target=convert, name='craftmaster-map-' + request['id'][:8], daemon=True,
    ).start()
    return {'map': request, 'started': True}


# CraftMaster owns the Zoomify/ZFY layout.  Do not assume its output directory
# is literally "<world> Zoomify": locate the actual TileGroup tree beneath the
# known world/dimension, without traversing other worlds or following links.
from runtime.CinterForgeBedrockMap import _ZOOMIFY_TILE, _TILE_GROUP, _map_layout, _find_zoomify_root


def _authored_viewer_ready(state: dict, dimension: Path, zfy: Path | None) -> bool:
    """Recognize CraftMaster's committed viewer, not its file timestamps.

    CraftMaster atomically writes the authored HTML, JS and fixed pyramid
    properties, and intentionally does not rewrite unchanged files. Therefore
    their modification times (including LiveMap.status) do not indicate whether
    a viewer is usable for this tab. The viewer itself owns tile refresh.
    """
    if zfy is None:
        return False
    return all(path.is_file() and not path.is_symlink() for path in (
        dimension / 'Live Map.htm',
        dimension / 'zoomify-viewer.min.js',
        zfy / 'ImageProperties.xml',
    ))



def serve_map_asset(handler, path: str) -> bool:
    """Read-only, same-origin map viewer/tiles; accepts only issued tab IDs."""
    if not path.startswith('/craftmaster-map/'):
        return False
    remainder = path[len('/craftmaster-map/'):]
    map_id, separator, encoded_asset = remainder.partition('/')
    if not separator or not re.fullmatch(r'[0-9a-f]{32}', map_id):
        handler._json(404, {'error': 'Unknown CraftMaster map'})
        return True
    with handler.server.craftmaster_map_lock:
        state = handler.server.craftmaster_maps.get(map_id)
        if state is None:
            handler._json(404, {'error': 'Unknown CraftMaster map'})
            return True
        state = dict(state)

    relative_text = unquote(encoded_asset)
    relative = Path(relative_text)
    parts = relative.parts
    if (
        not parts or relative.is_absolute() or '\\' in relative_text
        or any(part in ('', '.', '..') or part.startswith('.') for part in parts)
    ):
        handler._json(404, {'error': 'Invalid map asset'})
        return True
    # World-level manifest and explicit canonical dimensions only; no raw save access.
    world_root = state['directory'].parent
    if relative_text == 'MapCapabilities.json':
        handler._json(200, {'editable': _local_java_source(handler.server, state) is not None})
        return True
    if relative_text in ('MapAnnotations.json','MapPlayers.json') or (len(parts)==2 and parts[0]=='Profiles'):
        try:
            provider=_map_provider(handler.server)
            if relative_text=='MapAnnotations.json': handler._json(200,provider.map_annotations(world_root))
            elif relative_text=='MapPlayers.json': handler._json(200,provider.map_players(world_root))
            elif parts[1].endswith('.json'): handler._json(200,provider.map_profile(world_root,parts[1][:-5]))
            elif parts[1].endswith('.png'): handler._send(200,provider.map_profile_skin(world_root,parts[1][:-4]),'image/png')
            else: raise ValueError('Unknown map profile')
        except (OSError,ValueError,RuntimeError,TypeError): handler._json(404,{'error':'Map metadata unavailable'})
        return True
    if relative_text == 'LiveMap.json':
        asset = world_root / 'LiveMap.json'
        if any(p.is_symlink() for p in (asset, world_root, *world_root.parents)) or not asset.is_file():
            handler._json(404, {'error': 'Live dimension inventory not ready'})
        elif asset.stat().st_size > MAX_MAP_ASSET_BYTES:
            handler._json(413, {'error': 'Map inventory exceeds size limit'})
        else:
            handler._send(200, asset.read_bytes(), 'application/json')
        return True
    if parts[0] in ('Overworld', 'Nether', 'End') and len(parts) > 1:
        dimension = world_root / parts[0]
        parts = parts[1:]
        relative = Path(*parts)
        relative_text = relative.as_posix()
        zoomify = (None if dimension.is_symlink() or relative_text in
                   ('LiveMap.status', 'LiveMap POIs.json', 'zoomify-viewer.min.js', 'CinterForgeAtlas.js', 'TownNames.json')
                   else _find_zoomify_root(dimension))
    else:
        dimension, zoomify = _map_layout(state)
    if relative_text in ('zoomify-viewer.min.js', 'CinterForgeAtlas.js', 'TownNames.json'):
        # Serve the verified current authored resources together, even when a
        # save's existing portable map was generated by an older version.
        try:
            _, data = handler.server.bedrock._craftmaster_cli()
            asset = data / relative_text
            handler._send(200, asset.read_bytes(), 'application/javascript' if asset.suffix == '.js' else 'application/json')
        except (OSError, ValueError, RuntimeError):
            handler._json(503, {'error':'Verified CraftMaster viewer unavailable'})
        return True
    if relative_text in ('Live Map.htm', 'LiveMap.status', 'LiveMap POIs.json', world_root.name + ' POIs.json'):
        allowed = True
    else:
        # Only serve a true Zoomify tile/metadata file under the discovered
        # producer-owned pyramid; no arbitrary generated files are accessible.
        prefix = zoomify.relative_to(dimension).parts if zoomify is not None else ()
        allowed = bool(
            zoomify is not None and parts[:len(prefix)] == prefix and
            (len(parts) == len(prefix) + 1 and parts[-1] == 'ImageProperties.xml'
             or len(parts) == len(prefix) + 2
             and _TILE_GROUP.fullmatch(parts[-2])
             and _ZOOMIFY_TILE.fullmatch(parts[-1])
             or len(parts) == len(prefix) + 3 and parts[-3:-1] == ('Layers', 'DepthY')
             and re.fullmatch(r'r\.-?\d+\.-?\d+\.bin', parts[-1]))
        )
    if not allowed:
        handler._json(404, {'error': 'Unknown Zoomify asset'})
        return True

    directory = dimension
    # Never follow generated symlinks, even if they point inside the tree.
    if directory.is_symlink() or any(path.is_symlink() for path in directory.parents):
        handler._json(404, {'error': 'Unsafe map directory'})
        return True
    asset = directory / relative
    # The converter's live status determines whether a rectangle is active.
    # Retain the authored origin even when conversion is complete.
    if any((directory.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)):
        handler._json(404, {'error': 'Unsafe map asset'})
        return True
    # A sparse world has no height tile for ungenerated regions. This is a
    # legitimate unknown sample, not a missing web resource. Never synthesize Y.
    if (not asset.is_file() and len(parts) >= 3 and parts[-3:-1] == ('Layers', 'DepthY')
            and re.fullmatch(r'r\.-?\d+\.-?\d+\.bin', parts[-1])):
        handler._send(204, b'', 'application/octet-stream')
        return True
    if not asset.is_file() or (relative_text == 'Live Map.htm' and
                               not _authored_viewer_ready(state, dimension, zoomify)):
        # HTTP 202 is a readiness response, not a second HTML viewer. Pollers
        # must wait for HTTP 200 before navigating to the authored page.
        if relative_text == 'Live Map.htm':
            handler._json(202, {'ready': False, 'phase': state['phase']})
        else:
            handler._json(404, {'error': 'Zoomify asset not ready'})
        return True
    if asset.stat().st_size > MAX_MAP_ASSET_BYTES:
        handler._json(413, {'error': 'Map tile exceeds size limit'})
        return True
    mime_type = mimetypes.guess_type(str(asset))[0] or 'application/octet-stream'
    if relative_text.endswith('.htm'):
        mime_type = 'text/html; charset=utf-8'
    handler._send(
        200, asset.read_bytes(), mime_type,
        same_origin_frame=(relative_text == 'Live Map.htm'),
    )
    return True


# The LAN map endpoint shares the Terminal runtime, world inventory, conversion
# locks and restricted Zoomify routes. It does not expose the Terminal shell.
_MAP_SERVICE_JS = r"""(() => {
'use strict';
const root = document.querySelector('main[data-map]');
if (!root) return;
const id = root.dataset.map, frame = document.querySelector('#world-map');
const status = document.querySelector('#map-status'), button = document.querySelector('#update-map');
let loaded = false, pending = false, polling = false, wasWorking = false;
async function poll() {
  if (polling) return; polling = true;
  try {
    const response = await fetch('/map-status/' + id, {cache:'no-store'});
    if (!response.ok) throw Error('Map service unavailable');
    const data = await response.json();
    status.textContent = data.message; button.disabled = pending || data.working;
    if (!loaded && data.ready) { loaded = true; frame.src = root.dataset.viewer; frame.hidden = false; }
    else if (loaded && wasWorking && !data.working && data.phase === 'ready') {
      try { frame.contentWindow.location.reload(); } catch(_) { frame.src = root.dataset.viewer; }
    }
    wasWorking = data.working;
  } catch(e) { status.textContent = e.message; }
  finally { polling = false; }
}
button.addEventListener('click', async () => {
  if (pending) return; pending = true; button.disabled = true;
  try {
    const response = await fetch('/map-update/' + id, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    if (!response.ok) {const data = await response.json(); throw Error(data.error || 'Update failed');}
    status.textContent = 'CraftMaster update requested…';
  } catch(e) {status.textContent = e.message;}
  finally {pending = false; await poll();}
});
setInterval(poll,1000); void poll();
})();"""

_MAP_SERVICE_STYLE = """html,body{height:100%;margin:0;background:#e8d6a9;color:#47321f;font:14px Georgia,serif}main{height:100%;display:flex;flex-direction:column}header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:#efe0b9;flex-wrap:wrap}h1{font-size:18px;margin:0}a,button{color:#523b25;background:#e9d6ac;border:1px solid #b59a6c;border-radius:6px;padding:7px 10px;text-decoration:none;font:inherit;cursor:pointer}button:disabled{opacity:.5;cursor:default}iframe{border:0;flex:1;width:100%;min-height:0}iframe[hidden]{display:none}ul{list-style:none;padding:16px;display:grid;gap:12px;align-content:start}#map-status{margin-left:auto;color:#614a30}"""


class _MapHTTPHandler(CinterForgeHTTPAuthorization, BaseHTTPRequestHandler):
    """Narrow map-only HTTP surface; never route through Terminal's CLI APIs."""

    def _page(self, title, body, *, script=False):
        document = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>' + html.escape(title) + '</title><style>' + _MAP_SERVICE_STYLE +
                    '</style><body>' + body +
                    ('<script src="/map-service.js" defer></script>' if script else '') + '</body></html>')
        self._send(200, document.encode('utf-8'), 'text/html; charset=utf-8')

    def _remote_state(self, map_id):
        if not re.fullmatch(r'[0-9a-f]{32}', map_id):
            return None
        with self.server.craftmaster_map_lock:
            state = self.server.craftmaster_maps.get(map_id)
            return dict(state) if state and state.get('remote') else None

    def _same_origin(self):
        # Reject cross-origin browser writes, including unauthenticated LAN mode.
        return (self.headers.get('Origin') == self.server.scheme + '://' + self.headers.get('Host', '')
                and self.headers.get('Sec-Fetch-Site', 'same-origin') == 'same-origin')

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self._lan_logged_in():
            if path.startswith(('/craftmaster-map/', '/map-status/')):
                self._json(401, {'error': 'LAN login required'})
            else:
                self._login_page()
            return
        try:
            if path == '/map-service.js':
                self._send(200, _MAP_SERVICE_JS.encode('utf-8'), 'text/javascript; charset=utf-8')
                return
            if path.startswith('/craftmaster-map/'):
                if not self._remote_state(path.split('/')[2]):
                    self._json(404, {'error': 'Unknown map'})
                else:
                    serve_map_asset(self, path)
                return
            if path.startswith('/map-status/'):
                state = self._remote_state(path[len('/map-status/'):])
                if state is None:
                    self._json(404, {'error': 'Unknown map'})
                    return
                dimension, zfy = _map_layout(state)
                ready = _authored_viewer_ready(state, dimension, zfy)
                working = bool(state.get('working'))
                # Do not expose native logs (which can contain local file paths).
                progress = re.search(r'CONVERT dimension=(\d+) phase=\d+ chunks=(\d+/\d+) percent=([\d.]+)', state['message'])
                message = ('Converting · ' + progress.group(2) + ' chunks · ' + progress.group(3) + '%') if working and progress else (
                    'Updating map…' if working else 'Update failed; see the Terminal server log' if state['phase'] == 'failed' else
                    'Map ready' if ready else 'No map yet — request an update')
                self._json(200, {'ready': ready, 'working': working, 'message': message, 'phase': state['phase']})
                return
            manager = self.server.bedrock
            if manager is None:
                self._json(503, {'error': 'CraftMaster unavailable'})
                return
            if path == '/':
                rows = []
                for world in manager.list_java_worlds():
                    # Unique IDs are accepted as well as readable world names.
                    name = world.get('command_name') or world['name']
                    rows.append('<li><a href="/' + quote(world['id'], safe='') + '">' + html.escape(name) + '</a></li>')
                self._page('CinterForge World Maps', '<main><header><h1>CinterForge World Maps</h1></header><ul>' + ''.join(rows) + '</ul></main>')
                return
            name = unquote(path[1:])
            if not name or len(name) > 240 or '/' in name or '\\' in name or name in ('.', '..') or any(ord(c) < 32 for c in name):
                self._json(404, {'error': 'Unknown world'})
                return
            world = manager.find_java_world(name)
            if world is None:
                self._json(404, {'error': 'Unknown Java world'})
                return
            request = open_map_tab(self.server, world, preparing=False, enqueue=False)
            label = html.escape(request['world'])
            self._page(request['world'], '<main data-map="' + request['id'] + '" data-viewer="' + html.escape(request['url'], quote=True) + '">'
                       '<header><a href="/">Worlds</a><h1>' + label + '</h1><button id="update-map" type="button">Update map</button>'
                       '<span id="map-status" role="status">Loading…</span></header><iframe id="world-map" title="World map" allow="clipboard-write" hidden></iframe></main>', script=True)
        except (OSError, ValueError, RuntimeError) as error:
            self.server.log_event('[CraftMaster map service] ' + str(error))
            self._json(409, {'error': 'World or map unavailable; see the Terminal server log'})

    def do_POST(self):
        path = urlsplit(self.path).path
        # Annotation writes perform their own equivalent scoped origin/login check.
        if write_map_annotation(self, path):
            return
        if not self._same_origin():
            self._json(403, {'error': 'Same-origin request required'})
            return
        if path == '/login' and self.server.lan_login is not None:
            from runtime.CinterForgeAuthentication import _check_lan_login
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 1 <= n <= 2048:
                    raise ValueError('Invalid login length')
                fields = parse_qs(self.rfile.read(n).decode('utf-8'), keep_blank_values=True)
                valid = (len(fields.get('username', [])) == 1 and len(fields.get('password', [])) == 1 and
                         _check_lan_login(self.server.lan_login, fields['username'][0], fields['password'][0]))
            except (ValueError, UnicodeDecodeError):
                valid = False
            if not valid:
                self._login_page(failed=True)
                return
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', 'cinter_lan=' + self.server.lan_cookie + '; HttpOnly; SameSite=Strict; Path=/' +
                             ('; Secure' if self.server.scheme == 'https' else ''))
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self._lan_logged_in():
            self._json(401, {'error': 'LAN login required'})
            return
        if not path.startswith('/map-update/'):
            self._json(404, {'error': 'Unknown map action'})
            return
        state = self._remote_state(path[len('/map-update/'):])
        if state is None:
            self._json(404, {'error': 'Unknown map'})
            return
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if not 0 <= n <= 2048:
                raise ValueError('Invalid update length')
            self.rfile.read(n)
            result = run_map_conversion(self.server.owner, state['world_id'], enqueue=False, update_served_world=False)
            self._json(202, {'started': result['started'], 'already_running': result.get('already_running', False)})
        except (OSError, RuntimeError, ValueError) as error:
            self.server.log_event('[CraftMaster map service] ' + str(error))
            self._json(409, {'error': 'Could not start map update; see the Terminal server log'})

    def do_HEAD(self):
        self.send_error(405)

    def do_OPTIONS(self):
        self.send_error(405)


class CraftMasterMapHTTPServer(ThreadingHTTPServer):
    """One app-owned map listener. TLS uses a host-supplied certificate/key."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, owner, port=MAP_HTTP_PORT):
        self.owner = owner
        self.scheme = 'http'
        certificate = os.environ.get('CINTER_MAP_TLS_CERT')
        private_key = os.environ.get('CINTER_MAP_TLS_KEY')
        if bool(certificate) != bool(private_key):
            raise ValueError('Map HTTPS requires both CINTER_MAP_TLS_CERT and CINTER_MAP_TLS_KEY')
        context = None
        if certificate:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certificate, private_key)
            self.scheme = 'https'
        # Respect the primary listener's LAN policy.
        super().__init__(('0.0.0.0' if owner.lan else '127.0.0.1', port), _MapHTTPHandler)
        if context is not None:
            try:
                self.socket = context.wrap_socket(self.socket, server_side=True)
            except Exception:
                self.server_close()
                raise

    def __getattr__(self, name):
        # Share authorization and map registry with the primary app owner.
        # The restricted handler never calls the full Terminal HTTP dispatcher.
        return getattr(self.owner, name)
