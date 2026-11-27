"""Terminal HTTP/session adapter for the verified CraftMaster Java runtime."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def _verified_owner(data: Path):
    path = data / 'CinterForgeJava.py'
    manifest_path = data.parent / 'manifest.json'
    if path.is_symlink() or manifest_path.is_symlink() or not path.is_file():
        raise RuntimeError('Missing or linked CraftMaster Java runtime')
    manifest = json.loads(manifest_path.read_text('utf-8'))
    if (manifest.get('schema') != 'cinterforge.craftmaster.stage.v1' or
            hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get('files', {}).get('data/CinterForgeJava.py')):
        raise RuntimeError('CraftMaster Java runtime checksum mismatch')
    spec = importlib.util.spec_from_file_location('_cinter_java_owner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report_status(server):
    """Lightweight desktop status; use the existing live PTY sessions and global world selection."""
    from runtime import CinterForgeSessions as sessions
    if server.bedrock is None:
        return {'running': False, 'can_start': False}
    with server.bedrock_terminal_lock:
        selected = server.bedrock._world_settings().get('selected') or {}
        with server.sessions_lock:
            live = [s for s in server.sessions.values() if getattr(s, 'kind', None) == 'java'
                    and sessions._terminal_process_exit_code(s.process) is None]
        return {'running': bool(live), 'can_start': selected.get('edition') == 'java',
                'action': getattr(live[0], 'java_action', None) if live else None}


def report_action(server, body):
    """Report uses the same selected world, Java console and stop path as Terminal."""
    action = body.get('action')
    if action not in ('start', 'stop'):
        raise ValueError('Unknown Java report action')
    if server.bedrock is None:
        raise RuntimeError('Minecraft world infrastructure unavailable')
    if action == 'stop':
        return session_action(server, {'action': 'stop'})
    # Resolve the global selected world by ID, never by an ambiguous display name.
    with server.bedrock_terminal_lock:
        selected = server.bedrock._world_settings().get('selected') or {}
        if selected.get('edition') != 'java' or not selected.get('id'):
            raise ValueError('Select a Java world first')
        return session_action(server, {'action': 'serve', 'name': selected['id']})


def session_action(server, body):
    """Authorized HTTP action; the existing session owner launches a native PTY."""
    from runtime import CinterForgeSessions as sessions
    from runtime import CinterForgeSettings as settings
    if server.bedrock is None:
        raise RuntimeError('Minecraft save/server infrastructure unavailable: '+str(server.bedrock_error or 'not initialized'))
    action = body.get('action')
    if action not in ('status','select','install','serve','generate','stop'):
        raise ValueError('Unknown Java action')
    binary,data = server.bedrock._craftmaster_cli()
    owner = _verified_owner(data)
    cm = owner.provider(data)
    root = server.bedrock.root.parent / 'Java'
    selected_file = root/'CurrentWorld.json'
    if root.is_symlink() or selected_file.is_symlink():
        raise RuntimeError('Linked Java server state')
    def active():
        with server.sessions_lock:
            return [s for s in server.sessions.values() if getattr(s,'kind',None)=='java' and sessions._terminal_process_exit_code(s.process) is None]
    with server.bedrock_terminal_lock:
        live = active()
        selected = cm.json_read(selected_file).get('folder') if selected_file.is_file() else None
        if action == 'status':
            return {**owner.java_status(root,data),'worlds':server.bedrock.list_java_worlds(),'selected':selected,
                    'running':getattr(live[0],'java_action',None) if live else None}
        if action == 'stop':
            for item in live:
                item.close()
                with server.sessions_lock:
                    server.sessions.pop(item.id,None)
            return {'message':'Java Session stopped.'}
        if live:
            raise RuntimeError('A Java Session is already active; stop it first')
        world = None
        if action in ('serve','select'):
            name = body.get('name') or selected
            if not isinstance(name,str) or not name.strip():
                raise ValueError('Select a Java world first')
            record = server.bedrock.find_java_world(name)
            if record is None:
                raise ValueError('No matching local Java world: '+name)
            world = Path(record['path'])
            cm.json_write(selected_file,{'folder':world.name})
            if action=='select': return {'message':'Current Java World: '+world.name}
        elif action == 'generate':
            name = body.get('name')
            if not isinstance(name,str) or not name.strip() or name != name.strip() or len(name.encode('utf-8'))>200 or name in ('.','..','CinterForge') or any(c in name for c in '\\/:*?"<>|') or any(ord(c)<32 for c in name) or name.endswith('.'):
                raise ValueError('Invalid new Java world name')
            saves=server.bedrock._java_saves_destination()
            saves.mkdir(parents=True,exist_ok=True)
            world=saves.resolve()/name
            if world.exists(): raise ValueError('World already exists; generation never overwrites')
            size=body.get('size',1024)
            seed=body.get('seed','')
            if not isinstance(size,int) or size<1024 or size>16384 or size%1024 or not isinstance(seed,str) or len(seed)>128 or any(c in seed for c in '\r\n\\'):
                raise ValueError('Invalid Java generation size or seed')
        args=[sys.executable,'-B',str(data / 'CinterForgeJava.py'),action,'--binary',str(binary),'--data',str(data),'--root',str(root)]
        if world: args += ['--world',str(world)]
        if action=='generate': args += ['--size',str(size),'--seed',seed]
        cached = server.bedrock._java_saves_destination()/'CinterForge/ReferenceData/Java'/cm.JAVA_VERSION/f'minecraft_server_{cm.JAVA_VERSION}.jar'
        args += ['--cached-jar',str(cached)]
        with server.sessions_lock:
            if len(server.sessions)>=settings.MAX_SESSIONS: raise RuntimeError('Maximum terminal sessions reached')
            item=sessions.CinterForgeJavaTerminalSession(server.mutable_builds,server.system,80,24,url=server.origin,token=server.token,internal_token=server.cli_token,command=args,working_directory=settings.ASSETS,action=action)
            item.java_world_path = str(world.resolve()) if world is not None else None
            server.sessions[item.id]=item
        server.bedrock_terminal_requests.append({'id':item.id,'label':'Java','kind':'java','shell':'Java','platform':server.system})
        return {'terminal_session':item.id,'message':'Opened Java '+action+' Session.'}


def require_stopped_source(server, world):
    """Never read a Java save while the desktop's Java server is writing it."""
    from runtime import CinterForgeSessions as sessions
    source = str(Path(world['path']).resolve())
    with server.sessions_lock:
        if any(getattr(item, 'kind', None) == 'java'
               and getattr(item, 'java_world_path', None) == source
               and sessions._terminal_process_exit_code(item.process) is None
               for item in server.sessions.values()):
            raise RuntimeError('Stop Java for this world before updating it for Bedrock.')
