"""Live CraftMaster Zoomify map tabs within the existing Terminal HTTP server.

A map tab is queued before conversion so the user sees an interactive viewer
as soon as CraftMaster writes its first tiles. No new listener, preview process,
full-world PNG, or development Builds directory is created by the installed app.

Only generated assets under the selected, server-owned CraftMaster Maps tree are
served. Java saves, archives, LevelDB and arbitrary host paths are never exposed.
"""

from __future__ import annotations

import html
import mimetypes
import re
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
    map_directory = output_root / 'Maps' / world_name / 'Overworld'
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
        try:
            server.bedrock.run_native_craftmaster(
                world, progress=lambda line: report_progress(server, request['id'], line),
            )
        except Exception as error:
            mark_finished(server, request['id'], error)
            server.log_event('[CraftMaster] preview conversion failed: ' + str(error))
        else:
            mark_finished(server, request['id'])

    threading.Thread(
        target=convert, name='craftmaster-map-' + request['id'][:8], daemon=True,
    ).start()
    return {'map': request, 'started': True}


def _placeholder_page(world_name: str, message: str, *, refresh: bool) -> bytes:
    """Show native conversion progress while the first Zoomify tiles appear."""
    title = html.escape(world_name)
    detail = html.escape(message)
    reload = '<meta http-equiv="refresh" content="2">' if refresh else ''
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{reload}<title>{title} — CraftMaster</title>'
            f'<style>body{{background:#101927;color:#f0f4fb;font:15px system-ui;'
            f'padding:30px}}main{{max-width:900px;margin:auto}}'
            f'pre{{white-space:pre-wrap;overflow-wrap:anywhere;color:#b5cae2}}'
            f'</style></head><body><main><h2>{title} — live Zoomify</h2>'
            f'<pre>{detail}</pre></main></body></html>').encode('utf-8')


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
    name = state['name']
    if relative_text == 'Live Map.htm':
        allowed = True
    elif relative_text in ('zoomify-viewer.min.js', 'LiveMap.status'):
        allowed = True
    elif parts[0] == name + ' Zoomify' and len(parts) > 1:
        allowed = (
            len(parts) == 2 and parts[1] == 'ImageProperties.xml'
            or len(parts) == 3 and re.fullmatch(r'TileGroup\d+', parts[1])
            and parts[2].lower().endswith(('.png', '.jpg', '.jpeg'))
        )
    else:
        allowed = False
    if not allowed:
        handler._json(404, {'error': 'Unknown Zoomify asset'})
        return True

    directory = state['directory']
    # Never follow generated symlinks, even if they point inside the tree.
    if directory.is_symlink() or any(path.is_symlink() for path in directory.parents):
        handler._json(404, {'error': 'Unsafe map directory'})
        return True
    asset = directory / relative
    if any((directory.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)):
        handler._json(404, {'error': 'Unsafe map asset'})
        return True
    if relative_text == 'Live Map.htm' and state['phase'] == 'failed':
        handler._send(
            200, _placeholder_page(name, state['message'], refresh=False),
            'text/html; charset=utf-8', same_origin_frame=True,
        )
        return True
    if not asset.is_file():
        if relative_text == 'Live Map.htm':
            handler._send(
                200, _placeholder_page(name, state['message'], refresh=state['phase'] != 'failed'),
                'text/html; charset=utf-8', same_origin_frame=True,
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
