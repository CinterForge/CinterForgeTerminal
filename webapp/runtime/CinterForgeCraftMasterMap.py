"""Live CraftMaster Zoomify map tabs within the existing Terminal HTTP server.

A map tab is queued before conversion so the user sees an interactive viewer
as soon as CraftMaster writes its first tiles. No new listener, preview process,
full-world PNG, or development Builds directory is created by the installed app.

Only generated assets under the selected, server-owned CraftMaster Maps tree are
served. Java saves, archives, LevelDB and arbitrary host paths are never exposed.
"""

from __future__ import annotations

import html
import json
import mimetypes
import xml.etree.ElementTree as ET
import re
import time
import threading
import uuid
from pathlib import Path
from urllib.parse import quote, unquote


MAX_VISIBLE_MAPS = 16
MAX_MAP_ASSET_BYTES = 8 * 1024 * 1024


def open_map_tab(server, world: dict) -> dict:
    """Queue a browser tab independently of Bedrock's interactive PTY."""
    manager = server.bedrock
    if manager is None:
        raise RuntimeError('CraftMaster requires a configured Bedrock runtime')
    world_name = manager.java_world_export_name(world)
    output_root = manager.craftmaster_output_root()
    # CraftMaster names its map cache after the original Java save folder,
    # not the shortened .mcworld/export name (e.g. Candlewood (Candlewood)).
    map_directory = output_root / 'Maps' / Path(world['path']).name / 'Overworld'
    map_id = uuid.uuid4().hex
    request = {
        'id': map_id,
        'world': world_name,
        'url': '/craftmaster-map/' + map_id + '/Live%20Map.htm?refresh=5',
    }
    with server.craftmaster_map_lock:
        if len(server.craftmaster_maps) >= MAX_VISIBLE_MAPS:
            raise RuntimeError('Too many CraftMaster map tabs; close an existing map')
        server.craftmaster_maps[map_id] = {
            'name': world_name,
            'directory': map_directory,
            'started_ns': time.time_ns(),
            'phase': 'preparing',
            'message': 'Preparing live Zoomify map…',
        }
        server.craftmaster_map_requests.append(request)
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
        state['phase'] = 'failed' if error else 'ready'
        state['message'] = str(error) if error else 'Conversion complete; map stays live in this tab'


def run_map_conversion(server, name: str) -> dict:
    """Start `craftmaster <world>` in a worker; return before conversion ends."""
    if server.bedrock is None:
        raise RuntimeError(server.bedrock_error or 'CraftMaster is unavailable')
    world = server.bedrock.find_java_world(name)
    if world is None:
        raise ValueError('No local Java world named ' + repr(name))
    request = open_map_tab(server, world)

    def convert() -> None:
        from runtime import CinterForgeSessions as _CinterForgeSessions
        manager = server.bedrock
        restart_name = None
        def progress(line: str) -> None:
            report_progress(server, request['id'], line)
        try:
            # The same operation lock guards missing-world import and startup.
            # An explicit update never overlaps a live server for that world.
            with server.bedrock_terminal_lock:
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
_ZOOMIFY_TILE = re.compile(r'(\d+)-(\d+)-(\d+)\.(?:jpg|jpeg|png)', re.I)
_TILE_GROUP = re.compile(r'TileGroup\d+')
_MAX_LIVE_TILES = 4096
_MAX_MAP_LAYOUT_ENTRIES = 128


def _map_directories(state: dict) -> list[Path]:
    """Stay under the selected world's Maps folder and select Overworld only."""
    expected = state['directory']
    world_root = expected.parent
    if world_root.is_symlink() or not world_root.is_dir():
        return []
    directories = [expected] if expected.is_dir() and not expected.is_symlink() else []
    try:
        for candidate in world_root.iterdir():
            if (candidate != expected and candidate.is_dir() and not candidate.is_symlink()
                    and candidate.name.casefold() in ('overworld', 'minecraft_overworld', '0')):
                directories.append(candidate)
        # Some CraftMaster ZFY layouts put the Overworld tiles directly under
        # Maps/<world>, rather than making a second dimension directory.
        directories.append(world_root)
    except OSError:
        pass
    return directories


def _find_zoomify_root(dimension: Path) -> Path | None:
    """Locate a produced TileGroup, preferring roots with committed tile files."""
    frontier = [dimension]
    empty_root = None
    for _ in range(4):
        upcoming = []
        for parent in frontier:
            try:
                children = list(parent.iterdir())[:_MAX_MAP_LAYOUT_ENTRIES]
            except OSError:
                continue
            for child in children:
                if child.is_symlink() or not child.is_dir():
                    continue
                if _TILE_GROUP.fullmatch(child.name):
                    if empty_root is None:
                        empty_root = parent
                    try:
                        if any(_ZOOMIFY_TILE.fullmatch(tile.name) and tile.is_file()
                               and not tile.is_symlink() and tile.stat().st_size >= 32
                               for tile in child.iterdir()):
                            return parent
                    except OSError:
                        pass
                    continue
                if (not child.name.startswith('.') and
                        (parent != dimension or child.name.casefold() not in
                         ('nether', 'the_end', 'theend', 'end', 'minecraft_nether', 'minecraft_the_end', 'dim-1', 'dim1'))):
                    upcoming.append(child)
        frontier = upcoming[:_MAX_MAP_LAYOUT_ENTRIES]
    return empty_root


def _map_layout(state: dict) -> tuple[Path, Path | None]:
    candidates = _map_directories(state)
    for dimension in candidates:
        zoomify = _find_zoomify_root(dimension)
        if zoomify is not None:
            return dimension, zoomify
    return (candidates[0], None) if candidates else (state['directory'], None)


def _live_viewer_page(world_name: str) -> bytes:
    '''A graphic tile surface while CraftMaster's authored viewer is pending.

    This is NOT a conversion-progress page. It uses only real ZFY tile files;
    the authored Live Map.htm takes over as soon as it is published.
    '''
    name = html.escape(world_name, quote=True)
    return ('''<!doctype html><html lang="en" data-craftmaster-live-map="1">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>''' + name + ''' — Live Zoomify</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#080c10;color:#abb9c9;font:12px system-ui}
#cf-map-screen{position:absolute;inset:0;overflow:hidden;background:#050505;cursor:grab;touch-action:none}
#cf-map-screen.dragging{cursor:grabbing}
#cf-map-stage{position:absolute;top:0;left:0;transform-origin:0 0;will-change:transform}
#cf-map-stage img{position:absolute;width:512px;height:512px;display:block;pointer-events:none;user-select:none}
#cf-map-state{position:absolute;bottom:10px;left:10px;padding:4px 8px;background:#090f19bb;border-radius:5px;pointer-events:none}
#cf-map-error{position:absolute;top:10px;left:10px;max-width:90%;padding:8px 11px;background:#4b1414d9;color:white;border-radius:5px;display:none}
</style><script defer src="/client/CinterForgeCraftMasterMap.js"></script></head>
<body><div id="cf-map-screen" aria-label="Interactive live Zoomify tiles"><div id="cf-map-stage"></div></div>
<div id="cf-map-state">Waiting for Zoomify tiles…</div><div id="cf-map-error" role="alert"></div></body></html>''').encode('utf-8')


def _live_tile_manifest(state: dict) -> dict:
    """Enumerate real ZFY tiles from the discovered map layout, without XML/HTML."""
    dimension, zfy = _map_layout(state)
    manifest = {'phase': state['phase'], 'error': state['message'] if state['phase'] == 'failed' else '',
                'native_ready': False, 'tiles': [], 'tile_size': 512,
                'discovery': 'waiting-for-map' if not dimension.is_dir() else 'waiting-for-pyramid'}
    if zfy is None:
        if state['phase'] == 'ready':
            manifest['error'] = 'CraftMaster finished, but no Zoomify TileGroup directory was found for this world.'
        return manifest
    native = dimension / 'Live Map.htm'
    props = zfy / 'ImageProperties.xml'
    # CraftMaster initializes the real live viewer and its ZFY index *before*
    # region conversion. Use it as soon as the current conversion publishes its
    # rendering status, not only after the Bedrock archive is complete. A stale
    # viewer left by an earlier run must not be mistaken for a fresh map.
    status = dimension / 'LiveMap.status'
    status_ready = False
    try:
        if (status.is_file() and not status.is_symlink() and
                status.stat().st_mtime_ns >= state.get('started_ns', 0) and
                status.stat().st_size <= 64):
            status_ready = status.read_text('utf-8').strip() in ('rendering', 'complete')
    except (OSError, UnicodeError):
        pass
    manifest['native_ready'] = (state['phase'] != 'failed' and status_ready and
                                native.is_file() and not native.is_symlink() and
                                props.is_file() and not props.is_symlink() and
                                (dimension / 'zoomify-viewer.min.js').is_file())
    if manifest['native_ready']:
        manifest['discovery'] = 'native-viewer'
        return manifest
    if props.is_file() and not props.is_symlink() and props.stat().st_size < 131072:
        try:
            xml = ET.fromstring(props.read_bytes())
            manifest['width'] = int(xml.attrib['WIDTH'])
            manifest['height'] = int(xml.attrib['HEIGHT'])
        except (OSError, ET.ParseError, KeyError, TypeError, ValueError):
            pass
    manifest['discovery'] = 'waiting-for-tiles'
    try:
        groups = sorted((p for p in zfy.iterdir()
                         if p.is_dir() and not p.is_symlink() and _TILE_GROUP.fullmatch(p.name)),
                        key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for group in groups:
            for tile in group.iterdir():
                match = _ZOOMIFY_TILE.fullmatch(tile.name)
                if not match or tile.is_symlink() or not tile.is_file():
                    continue
                info = tile.stat()
                if info.st_size < 32 or info.st_size > MAX_MAP_ASSET_BYTES:
                    continue
                manifest['tiles'].append({
                    'path': tile.relative_to(dimension).as_posix(),
                    'level': int(match.group(1)), 'x': int(match.group(2)),
                    'y': int(match.group(3)),
                    'stamp': str(info.st_mtime_ns) + '-' + str(info.st_size),
                })
                if len(manifest['tiles']) >= _MAX_LIVE_TILES:
                    manifest['discovery'] = 'streaming'
                    return manifest
    except OSError:
        # Tiles can be concurrently committed or replaced by CraftMaster.
        pass
    if manifest['tiles']:
        manifest['discovery'] = 'streaming'
    elif state['phase'] == 'ready':
        manifest['error'] = 'CraftMaster finished, but the Zoomify pyramid contains no readable tiles.'
    return manifest


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
    dimension, zoomify = _map_layout(state)
    name = state['name']
    if relative_text in ('Live Map.htm', 'tiles.json', 'zoomify-viewer.min.js', 'LiveMap.status'):
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
             and _ZOOMIFY_TILE.fullmatch(parts[-1]))
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
    if any((directory.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)):
        handler._json(404, {'error': 'Unsafe map asset'})
        return True
    if relative_text == 'tiles.json':
        handler._send(200, json.dumps(_live_tile_manifest(state)).encode('utf-8'))
        return True
    if not asset.is_file() or (relative_text == 'Live Map.htm' and
                               not _live_tile_manifest(state)['native_ready']):
        if relative_text == 'Live Map.htm':
            handler._send(
                200, _live_viewer_page(name), 'text/html; charset=utf-8',
                same_origin_frame=True,
            )
        else:
            handler._json(404, {'error': 'Zoomify tile not ready'})
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
