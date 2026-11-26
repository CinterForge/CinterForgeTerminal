"""Persistent, server-wide Bedrock allowlist and operator policy.

The policy belongs to the Bedrock *installation*, never to a world. Bedrock's
native allowlist.json and permissions.json are reconciled under its one Run
root. Xbox XUIDs are learned only from authenticated Bedrock server join logs;
name-only operators remain pending until their XUID is known.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import uuid

_SCHEMA = 'cinterforge.bedrock.server-access.v1'
_NAME = re.compile(r'[A-Za-z0-9_ .-]{1,32}\Z')
_JOIN = re.compile(r'Player (?:connected:|Spawned:)\s*(.+?)[, ]+xuid:\s*(\d+)', re.I)
_LOG_PREFIX = re.compile(r'^\[\d{4}-\d\d-\d\d [^]]+\s+(?:INFO|WARN|ERROR)\]\s*')
_SAY = re.compile(r'^(?:\[([^]\r\n]{1,32})\]|<([^>\r\n]{1,32})>)\s+(.{1,240})$')
_SCRIPT_EVENT = re.compile(r'CINTERFORGE_MC_STATUS:(\{[^\r\n]{1,2048}\})')


def _name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or value != value.strip() or not value.strip():
        raise ValueError('Expected an Xbox gamertag (1–32 characters, letters, numbers, spaces, _ . -)')
    return value


def _legacy_allowlist(data) -> list[dict] | None:
    """Recognize old allowlist representations without discarding player data.

    The Bedrock Dedicated Server requires a JSON array of player objects.
    Convert only unambiguous representations and back up the original bytes.
    """
    if data is None or data == {}:
        return []
    if isinstance(data, dict):
        if 'name' in data and set(data).issubset({'name', 'xuid', 'ignoresPlayerLimit'}):
            data = [data]
        else:
            recognized = [key for key in ('players', 'allowlist', 'whitelist', 'entries') if key in data]
            if len(recognized) != 1:
                return None
            data = data[recognized[0]]
    if not isinstance(data, list):
        return None
    result = []
    for item in data:
        if isinstance(item, str):
            if not _NAME.fullmatch(item) or item != item.strip() or not item.strip():
                return None
            result.append({'ignoresPlayerLimit': False, 'name': item})
        elif isinstance(item, dict):
            if not isinstance(item.get('name'), str) or not _NAME.fullmatch(item['name']):
                return None
            result.append(dict(item))
        else:
            return None
    return result


def _read_list(path: Path) -> list[dict]:
    if path.is_symlink():
        raise RuntimeError('Refusing linked Bedrock access file: ' + str(path))
    if not path.is_file():
        return []
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'Invalid JSON in Bedrock access file {path}: {exc}; original preserved') from exc
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        for index, item in enumerate(data):
            if path.name == 'allowlist.json':
                name = item.get('name')
                if not isinstance(name, str) or not _NAME.fullmatch(name):
                    raise RuntimeError(f'Unrecognized Bedrock allowlist entry {index} in {path}; '
                                       'original preserved (no players discarded)')
            elif path.name == 'permissions.json' and not str(item.get('xuid', '')).isdigit():
                raise RuntimeError(f'Unrecognized Bedrock permissions entry {index} in {path}; '
                                   'original preserved (no permissions discarded)')
        return data
    if path.name != 'allowlist.json':
        raise RuntimeError(f'Invalid Bedrock access file {path}: expected JSON array of objects, '
                           f'got {type(data).__name__}; original preserved')
    migrated = _legacy_allowlist(data)
    if migrated is None:
        raise RuntimeError(f'Unrecognized Bedrock allowlist format in {path}: '
                           f'got {type(data).__name__}; original preserved. '
                           'Expected an array of player objects or a recognized legacy player list.')
    # A successful recognition must never silently replace a user's older
    # allowlist. Preserve the exact original bytes before making it BDS-native.
    backup = path.with_name(path.name + '.before-CinterForge-' + uuid.uuid4().hex[:12] + '.bak')
    with backup.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    _atomic_json(path, migrated)
    return migrated


def _atomic_json(path: Path, data) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError('Refusing linked Bedrock access path: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


TERMINAL_RESOURCE_PACK_MEMBERS = (
    'manifest.json', 'textures/item_texture.json', 'ui/server_form.json',
    *(f'textures/items/cinter_terminal_{size}.png'
      for size in ('1', '2', '3', '4', 'sm', 'med', 'lg', 'xl')),
)


def install_world_status_bridge(server_root: Path, target: Path, authored: Path) -> None:
    """Install the same packaged communications assets into an isolated managed world."""
    # Validate the full authored resource pack before changing either world registry.
    resources = authored.parent / 'bedrock_terminal_resources'
    for member in TERMINAL_RESOURCE_PACK_MEMBERS:
        path = resources / member
        if not path.is_file() or path.is_symlink():
            raise RuntimeError('Packaged Terminal resource missing: ' + str(path) +
                               '; apply the complete showroom update and rebuild/stage Terminal (not Setup)')
    resource_manifest = json.loads((resources / 'manifest.json').read_text('utf-8'))['header']
    manifest_file = authored / 'manifest.json'
    assets = ('manifest.json', 'scripts/main.js', 'items/terminal.json', *(f'items/terminal_{key}.json' for key in ('1','2','3','4','sm','med','lg','xl')))
    if any(not (authored / name).is_file() or (authored / name).is_symlink() for name in assets):
        raise RuntimeError('Packaged CinterForge Minecraft status bridge is missing')
    manifest = json.loads(manifest_file.read_text('utf-8'))
    pack_id = manifest['header']['uuid']
    version = manifest['header']['version']
    destination = server_root / 'behavior_packs' / 'cinterforge-status-bridge'
    if destination.parent.is_symlink():
        raise RuntimeError('Refusing symlinked Bedrock behavior-packs root')
    for folder in (destination, destination / 'scripts', destination / 'items'):
        if folder.is_symlink():
            raise RuntimeError('Refusing symlinked Bedrock status bridge directory')
        folder.mkdir(parents=True, exist_ok=True)
    for name in assets:
        source, output = authored / name, destination / name
        if output.is_symlink():
            raise RuntimeError('Refusing symlinked Bedrock status bridge asset')
        if not output.is_file() or output.read_bytes() != source.read_bytes():
            output.write_bytes(source.read_bytes())

    registry_path = target / 'world_behavior_packs.json'
    if registry_path.is_symlink():
        raise RuntimeError('Refusing symlinked world behavior-pack registry')
    entries = json.loads(registry_path.read_text('utf-8')) if registry_path.is_file() else []
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise RuntimeError('Invalid existing Bedrock world behavior-pack registry: ' + str(registry_path))
    entry = {'pack_id': pack_id, 'version': version}
    current = [item for item in entries if item.get('pack_id') != pack_id]
    current.append(entry)
    if entries != current:
        temporary = registry_path.with_name(registry_path.name + '-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(current, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, registry_path)
        finally:
            temporary.unlink(missing_ok=True)

    resource_destination = server_root / 'resource_packs' / 'cinterforge-terminal-resources'
    if resource_destination.parent.is_symlink() or resource_destination.is_symlink():
        raise RuntimeError('Refusing linked Terminal resource pack')
    for source in resources.rglob('*'):
        if source.is_symlink(): raise RuntimeError('Linked authored Terminal resource')
        if not source.is_file(): continue
        output = resource_destination / source.relative_to(resources)
        for parent in (output, *output.parents):
            if parent == resource_destination.parent: break
            if parent.is_symlink(): raise RuntimeError('Linked installed Terminal resource')
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.is_file() or output.read_bytes() != source.read_bytes(): output.write_bytes(source.read_bytes())
    registry_path = target / 'world_resource_packs.json'
    if registry_path.is_symlink(): raise RuntimeError('Linked world resource registry')
    entries = json.loads(registry_path.read_text('utf-8')) if registry_path.is_file() else []
    if not isinstance(entries,list) or any(not isinstance(item,dict) for item in entries): raise RuntimeError('Invalid resource registry')
    entry = {'pack_id':resource_manifest['uuid'],'version':resource_manifest['version']}
    # The built-in form and item atlas must take precedence over other packs.
    current = [entry] + [item for item in entries if item.get('pack_id') != entry['pack_id']]
    if current != entries:
        temporary = registry_path.with_name(registry_path.name + '-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(current,indent=2)+'\n',encoding='utf-8')
            os.replace(temporary,registry_path)
        finally: temporary.unlink(missing_ok=True)


class BedrockServerAccess:
    def __init__(self, installation_root: Path, run_root: Path, *, live, send, event, command):
        self.root = installation_root
        self.run_root = run_root
        self.path = installation_root / 'ServerAccess.json'
        self.live = live
        self.send = send
        self.event = event
        self.command = command
        self._partial = ''
        self._promoted = set()

    def _load(self) -> dict:
        if self.path.is_symlink():
            raise RuntimeError('Refusing linked server access policy')
        if not self.path.is_file():
            # Retain the user's existing native allowlist when this capability
            # is first introduced. Do not claim unknown XUIDs are named ops.
            existing = _read_list(self.run_root / 'allowlist.json')
            players = []
            for item in existing:
                name = item.get('name')
                if not isinstance(name, str) or not _NAME.fullmatch(name):
                    raise RuntimeError('Cannot import existing Bedrock allowlist entry without '
                                       'a supported gamertag; refusing to discard it')
                if name.casefold() not in {player.casefold() for player in players}:
                    players.append(name)
            known = {item['name'].casefold(): str(item['xuid']) for item in existing
                     if str(item.get('xuid', '')).isdigit()}
            return {'schema': _SCHEMA, 'operators': [], 'allowlist': players, 'xuids': known}
        state = json.loads(self.path.read_text('utf-8'))
        if not isinstance(state, dict) or state.get('schema') != _SCHEMA:
            raise RuntimeError('Invalid Bedrock ServerAccess.json; refusing to replace operator policy')
        for key in ('operators', 'allowlist'):
            if not isinstance(state.get(key), list) or not all(isinstance(x, str) and _NAME.fullmatch(x) for x in state[key]):
                raise RuntimeError('Invalid Bedrock ServerAccess.json ' + key)
        if not isinstance(state.get('xuids'), dict):
            raise RuntimeError('Invalid Bedrock ServerAccess.json xuids')
        return state

    def snapshot(self) -> dict:
        state = self._load()
        known = state['xuids']
        native = _read_list(self.run_root / 'permissions.json')
        other = [str(item.get('xuid')) for item in native
                 if item.get('permission') == 'operator' and str(item.get('xuid', '')).isdigit()
                 and str(item['xuid']) not in set(known.values())]
        return {'operators': [{'name': name, 'xuid': known.get(name.casefold()),
                 'status': 'known-xuid' if known.get(name.casefold()) else 'pending-join'}
                 for name in state['operators']],
                'allowlist': sorted(state['allowlist'], key=str.casefold),
                'other_operator_xuids': other, 'policy_path': str(self.path),
                'allowlist_enabled': self._allowlist_enabled()}

    def _allowlist_enabled(self) -> bool:
        path = self.run_root / 'server.properties'
        if not path.is_file() or path.is_symlink():
            return False
        return any(line.strip().lower() == 'allow-list=true' for line in path.read_text('utf-8').splitlines())

    def _apply_native(self, state: dict, *, running: bool) -> None:
        if not self.run_root.is_dir():
            # `bedrock op Kevin` can run before BDS was installed; synchronize
            # native files when the server is subsequently installed/started.
            return
        original_allow = _read_list(self.run_root / 'allowlist.json')
        prior = {str(item.get('name', '')).casefold(): item for item in original_allow}
        new_allow = []
        for name in state['allowlist']:
            existing = prior.get(name.casefold(), {})
            xuid = state['xuids'].get(name.casefold()) or existing.get('xuid')
            entry = {'ignoresPlayerLimit': bool(existing.get('ignoresPlayerLimit', False)), 'name': name}
            if str(xuid or '').isdigit(): entry['xuid'] = str(xuid)
            new_allow.append(entry)
        if new_allow != original_allow:
            _atomic_json(self.run_root / 'allowlist.json', new_allow)
        original_permissions = _read_list(self.run_root / 'permissions.json')
        native = {str(item.get('xuid')): dict(item) for item in original_permissions
                  if str(item.get('xuid', '')).isdigit()}
        ops = {name.casefold() for name in state['operators']}
        for name, xuid in state['xuids'].items():
            if name not in ops or not str(xuid).isdigit():
                continue  # Do not change permissions not owned by our op policy.
            native[str(xuid)] = {'permission': 'operator', 'xuid': str(xuid)}
        new_permissions = list(native.values())
        if new_permissions != original_permissions:
            _atomic_json(self.run_root / 'permissions.json', new_permissions)
        if running:
            # BDS does not watch edits to its JSON files automatically.
            self.send('allowlist reload')
            self.send('permission reload')

    def apply_before_launch(self) -> None:
        state = self._load()
        self._promoted.clear()
        self._partial = ''
        self._apply_native(state, running=False)

    def change(self, kind: str, action: str, raw_name: str) -> dict:
        if kind not in ('op', 'allowlist') or action not in ('add', 'remove'):
            raise ValueError('Expected bedrock op|allowlist [add|remove] <gamertag>')
        name = _name(raw_name)
        state = self._load()
        # Refuse partial policy changes if an existing native file cannot be
        # interpreted. Recognized legacy allowlists are backed up and migrated.
        if self.run_root.is_dir():
            _read_list(self.run_root / 'allowlist.json')
            _read_list(self.run_root / 'permissions.json')
        key = 'operators' if kind == 'op' else 'allowlist'
        normalized = name.casefold()
        if action == 'remove' and kind == 'allowlist' and any(x.casefold() == normalized for x in state['operators']):
            raise ValueError('This player is an operator; remove their operator role first')
        state[key] = [x for x in state[key] if x.casefold() != normalized]
        if action == 'add':
            state[key].append(name)
            if kind == 'op' and not any(x.casefold() == normalized for x in state['allowlist']):
                state['allowlist'].append(name)
        _atomic_json(self.path, state)
        running = bool(self.live())
        self._apply_native(state, running=running)
        if action == 'remove' and kind == 'op':
            # Removing a persistent operator must also revoke their native
            # XUID permission, not merely stop reapplying it next launch.
            xuid = state['xuids'].get(normalized)
            if str(xuid or '').isdigit() and self.run_root.is_dir():
                native_path = self.run_root / 'permissions.json'
                native = _read_list(native_path)
                updated = [({**item, 'permission': 'member'} if str(item.get('xuid')) == xuid else item)
                           for item in native]
                if updated != native:
                    _atomic_json(native_path, updated)
                    if running:
                        self.send('permission reload')
        if running and kind == 'op':
            # BDS can op only a connected player by name. The XUID-backed
            # operator role also persists and is reapplied on future starts.
            self.send(('op ' if action == 'add' else 'deop ') + _quote(name))
        return self.snapshot()

    def observe(self, data: bytes) -> None:
        # Called by the existing owning Bedrock PTY reader, not a parallel
        # Minecraft connection or an extra poller/process.
        self._partial += data.decode('utf-8', 'replace')
        if len(self._partial) > 16384:
            self._partial = self._partial[-8192:]
        lines = self._partial.split('\n')
        self._partial = lines.pop()
        for raw in lines:
            line = _LOG_PREFIX.sub('', raw.rstrip('\r'))
            joined = _JOIN.search(line)
            if joined:
                name, xuid = joined.group(1).strip(), joined.group(2)
                state = self._load()
                if any(n.casefold() == name.casefold() for n in state['allowlist']):
                    if state['xuids'].get(name.casefold()) != xuid:
                        state['xuids'][name.casefold()] = xuid
                        _atomic_json(self.path, state)
                        self._apply_native(state, running=True)
                    if any(n.casefold() == name.casefold() for n in state['operators']) and name.casefold() not in self._promoted:
                        if 'Spawned:' in line:
                            self._promoted.add(name.casefold())
                            self.send('op ' + _quote(name))
            # Decode the Script API event line already present in the Bedrock
            # console. Status/chat are display messages. Every cinter command
            # goes directly to the owning Bedrock tab's real host Bash PTY.
            script_event = _SCRIPT_EVENT.search(line)
            if script_event:
                try:
                    payload = json.loads(script_event.group(1))
                    if not isinstance(payload, dict):
                        continue
                    kind = payload.get('type')
                    sender = str(payload.get('sender', 'World'))[:32].strip()
                    if not sender or any(ord(c) < 32 for c in sender):
                        continue
                    if kind == 'command':
                        command = payload.get('command')
                        if (isinstance(command, str) and command.strip() and
                                len(command.encode('utf-8')) <= 2048 and
                                not any(c in command for c in '\r\n\x00')):
                            self.command(sender, command)
                    elif kind == 'native-command':
                        name, argument = payload.get('name'), payload.get('argument')
                        # Native commands are typed single game/world names, never
                        # arbitrary shell fragments or CraftMaster CLI switches.
                        if (name in ('play', 'craftmaster') and isinstance(argument, str)
                                and 0 < len(argument) <= 160 and argument == argument.strip()
                                and not argument.startswith('-') and argument.isprintable()
                                and all(ord(c) >= 32 and ord(c) != 127 for c in argument)
                                and (name != 'craftmaster' or argument.casefold() not in
                                     {'worlds', 'players', 'convert', 'export-name', 'self-test',
                                      'capabilities', 'help'})):
                            self.command(sender, name + ' ' + shlex.quote(argument))
                        else:
                            self.event(sender + ': Invalid CinterForge command argument', kind='minecraft-say')
                    elif kind in ('chat', 'status'):
                        message = str(payload.get('message', ''))[:240].strip()
                        if message and not any(ord(c) < 32 for c in message):
                            self.event(sender + ':' + message, kind='minecraft-say')
                except (ValueError, TypeError):
                    pass
                continue
            # Older server plugins may write conventional chat lines.
            speech = _SAY.fullmatch(line)
            if speech:
                sender = speech.group(1) or speech.group(2)
                message = speech.group(3)
                if sender.lower() not in ('info', 'error', 'warn'):
                    self.event(sender + ':' + message, kind='minecraft-say')


def _quote(name: str) -> str:
    return '"' + _name(name) + '"' if ' ' in name else _name(name)
