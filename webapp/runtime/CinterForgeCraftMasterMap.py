"""Live CraftMaster Zoomify map tabs within the existing Terminal HTTP server.

The Map tab only embeds the single live Zoomify viewer authored by CraftMaster.
It waits for CraftMaster's atomic viewer/pyramid initialization rather than
introducing a duplicate preview renderer or replacing a live viewer.

Only generated assets under the selected, server-owned CraftMaster Maps tree are
served. Java saves, archives, LevelDB and arbitrary host paths are never exposed.
"""

from __future__ import annotations

import mimetypes
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import unquote


MAX_VISIBLE_MAPS = 16
MAX_MAP_ASSET_BYTES = 8 * 1024 * 1024
_MAX_MAP_LAYOUT_ENTRIES = 4096


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
    dimension, zoomify = _map_layout(state)
    if relative_text in ('Live Map.htm', 'zoomify-viewer.min.js', 'LiveMap.status'):
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
