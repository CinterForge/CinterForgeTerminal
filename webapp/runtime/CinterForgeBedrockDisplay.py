"""Install verified prebuilt display closure and forward typed display commands.
The existing Bedrock session owns the worker; no new service, VM or shell.
"""
from __future__ import annotations
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path

ASSETS=Path(__file__).resolve().parents[1]


# Endstone 0.11.16's supported BDS release. Pin the actual official archive
# as recorded in EndstoneMC/bedrock-server-data v2/release/1.26.65/metadata.json.
# The registry version is 1.26.65; the official ZIP and server report 1.26.65.1.
BDS_VERSION = '1.26.65'
BDS_ARCHIVE_VERSION = '1.26.65.1'
BDS_ARCHIVE_SHA256 = {
    'linux': '4443e58064f5fa96f3464ab46c20cd89b631f7117e4c94d327f48c4eb64f9969',
    'win': 'c98b9824783b000b8a8d602e6f58467725316c8f635739b1e31998871995621e',
}


def compatible_bds_entry(target):
    kind = 'win' if target == 'win' else 'linux'
    return {'version': BDS_VERSION, 'sha256': BDS_ARCHIVE_SHA256[target],
            'url': f'https://www.minecraft.net/bedrockdedicatedserver/bin-{kind}/bedrock-server-{BDS_ARCHIVE_VERSION}.zip'}


def verified_bds_receipt(receipt, target):
    """Recognize the exact supported official package, including legacy null versions.

    Never infer a version from a filename, a log, or an arbitrary checksum.
    The prior Setup/Test installer saved the verified archive digest but no
    version; only the exact pinned archive can be migrated without a download.
    """
    expected = compatible_bds_entry(target)
    if (not isinstance(receipt, dict) or receipt.get('schema') != 'cinterforge.bedrock.package.v1'
            or receipt.get('platform') != target):
        raise ValueError('invalid installed Bedrock package receipt')
    version = receipt.get('version')
    if (receipt.get('sha256') != expected['sha256'] or
            version not in (None, '', BDS_VERSION, BDS_ARCHIVE_VERSION)):
        raise ValueError(f'installed Bedrock version={version!r}, archive SHA-256={receipt.get("sha256")!r}; '
                         f'Endstone 0.11.16 requires the official {BDS_ARCHIVE_VERSION} {target} archive '
                         f'({expected["sha256"]})')
    return {**receipt, 'version': expected['version'], 'source': expected['url']}


def install(manager):
    root=manager.run_root/'.cinter/display-runtime'
    enabled=root/'enabled'
    enabled.unlink(missing_ok=True)
    manager._display_unavailable_reason = ''
    package_receipt = manager.package/'receipt.json'
    try:
        if package_receipt.is_symlink():
            raise ValueError('linked installed Bedrock package receipt')
        old = json.loads(package_receipt.read_text('utf-8'))
        verified = verified_bds_receipt(old, manager.target)
    except (OSError, ValueError, TypeError) as exc:
        # Every new launch must pass the shared official installer first.
        # A mismatch here is a failed preparation, not permission to start
        # vanilla BDS or silently disable the Terminal provider.
        manager._display_unavailable_reason = str(exc)
        manager._append_log('CINTERFORGE_DISPLAY_UNAVAILABLE: ' + str(exc))
        raise RuntimeError('Bedrock display compatibility verification failed: ' + str(exc)) from exc
    if verified != old:
        from runtime.CinterForgeBedrockProperties import _atomic_write
        _atomic_write(package_receipt, (json.dumps(verified, indent=2)+'\n').encode('utf-8'))
        manager._append_log('CINTERFORGE_DISPLAY_BDS_RECEIPT_VERIFIED: '
                            + BDS_ARCHIVE_VERSION + ' ' + verified['sha256'])
    source=manager.builds/'CinterForgeTerminal/Dependencies/BedrockDisplay'/manager.target
    receipt=source/'receipt.json'
    if not receipt.is_file(): raise RuntimeError('Bedrock display dependencies missing: run ./Cinter setup all, build and stage')
    meta=json.loads(receipt.read_text('utf-8'))
    if meta.get('schema')!='cinterforge.terminal.bedrock-display.v1' or meta.get('target')!=manager.target:
        raise RuntimeError('Invalid Bedrock display dependency receipt')
    marker=root/'receipt.json'
    if not marker.is_file() or marker.read_bytes()!=receipt.read_bytes():
        temp=root.with_name('display-runtime-install')
        if temp.exists(): shutil.rmtree(temp)
        shutil.copytree(source,temp)
        if root.exists(): shutil.rmtree(root)
        temp.rename(root)
    for name in ('launch.py','cinterforge_display.py','terminal_mount.py'):
        dst=root/name if name=='launch.py' else root/'site'/name
        shutil.copy2(ASSETS/'bedrock_display'/name,dst)
    obsolete=root/'site/cinterforge_display-1.0.dist-info'
    if obsolete.is_dir(): shutil.rmtree(obsolete)
    info=root/'site/endstone_cinterforge_display-1.0.dist-info'; info.mkdir(exist_ok=True)
    (info/'METADATA').write_text('Metadata-Version: 2.1\nName: endstone-cinterforge-display\nVersion: 1.0\n')
    (info/'entry_points.txt').write_text('[endstone]\ncinterforge-display = cinterforge_display:CinterForgeDisplay\n')
    (info/'WHEEL').write_text('Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
    (info/'RECORD').write_text('cinterforge_display.py,,\nterminal_mount.py,,\n')
    plugin='endstone_mediaplayer.dll' if manager.target=='win' else 'endstone_mediaplayer.so'
    plugins=manager.run_root/'plugins'; plugins.mkdir(exist_ok=True)
    shutil.copy2(root/plugin,plugins/plugin)
    io=manager.run_root/'.cinter/display-io'
    io.mkdir(parents=True,exist_ok=True)
    for pattern in ('*.request.json','*.response.json','*.tmp'):
        for p in io.glob(pattern): p.unlink(missing_ok=True)
    for p in io.glob('*.tiles'):
        if p.is_dir() and not p.is_symlink(): shutil.rmtree(p)
    enabled.write_text("Endstone 0.11.16 / MediaPlayer 2.3.1\n",encoding="utf-8")
    manager._append_log('CINTERFORGE_DISPLAY_DEPENDENCIES_INSTALLED: BDS ' + BDS_ARCHIVE_VERSION + '; ' + str(root))
    return root


def command_args(command):
    if not isinstance(command,str) or len(command)>180 or any(ord(c)<32 for c in command): raise ValueError('Invalid command')
    parts=shlex.split(command)
    if not parts: raise ValueError('Enter a command')
    name=parts[0].casefold()
    if name in ('help','games') and len(parts)==1: return [name]
    if name in ('play','craftmaster') and len(parts)>1:
        arg=' '.join(parts[1:])
        native={'worlds','players','convert','export-name','self-test','capabilities','help'}
        if not arg.startswith('-') and len(arg)<=160 and not (name=='craftmaster' and arg in native): return [name,arg]
    raise ValueError('Available display commands: help, games, play <title>, craftmaster <world>')


class DisplayFiction:
    """One Bedrock-owned turn queue, consumed by the existing native WebView VM.

    No desktop tab, extra interpreter dependency, or public shell access.
    Each screen owns its game until the interpreter returns (including quit
    confirmation). Closing a Minecraft form does not close the game.
    """
    def __init__(self):
        self.condition = threading.Condition()
        self.active = {}
        self.job = None
        self.result = None
        self.last_completed = None
        self.closed = False

    def next(self):
        with self.condition:
            job = self.job
            action = None
            # A lost HTTP response must not consume the only copy of a turn.
            if job and self.result is None:
                action = dict(job)
            return {'action': action, 'active': list(self.active.values())}

    def complete(self, body):
        with self.condition:
            identity = (body.get('id'), body.get('game'))
            if identity == self.last_completed:
                return True  # A retry after a lost acknowledgement is harmless.
            if (not self.job or identity != (self.job['id'], self.job['game']) or self.result is not None):
                return False
            output, finished = body.get('output'), body.get('finished')
            if not isinstance(output, str) or len(output) > 16384 or type(finished) is not bool:
                raise ValueError('Invalid interactive fiction turn response')
            self.result = {'output': output, 'finished': finished}
            self.last_completed = identity
            self.condition.notify_all()
            return True

    def close(self):
        with self.condition:
            self.closed = True
            self.active.clear()
            self.condition.notify_all()

    def turn(self, screen, command, *, entry=None):
        with self.condition:
            if self.closed:
                raise RuntimeError('Bedrock session closed')
            game = self.active.get(screen)
            if entry is not None:
                if game is not None:
                    raise RuntimeError('A story is already running on this screen')
                if len(self.active) >= 8:
                    raise RuntimeError('Maximum in-world story sessions reached')
                game = uuid.uuid4().hex
                self.active[screen] = game
            elif game is None:
                raise RuntimeError('No story running on this screen')
            self.result = None
            self.job = {'id': uuid.uuid4().hex, 'game': game, 'screen': screen,
                        'start': Path(entry['file']).stem if entry else None,
                        'command': command if entry is None else ''}
            try:
                self.condition.wait_for(lambda: self.result is not None or self.closed, timeout=45)
                if self.result is None:
                    # Do not silently return to the host dispatcher on a lost VM.
                    # Explicitly end this session and report the failure instead.
                    self.active.pop(screen, None)
                    return {'output': '[IF] Native story interpreter did not respond; session stopped.',
                            'mode': 'terminal', 'replace': True}
                result = self.result
                if result['finished']:
                    self.active.pop(screen, None)
                return {'output': result['output'], 'replace': True,
                        'mode': 'terminal' if result['finished'] else 'if'}
            finally:
                self.job = None
                self.result = None


def fiction_entry(command):
    from runtime.CinterForgeCommands import _cli_if_catalog
    parts = shlex.split(command)
    if parts and parts[0].casefold() == 'play':
        parts = parts[1:]
    value = ' '.join(parts).casefold()
    if value == 'zork':
        value = 'zork1'
    for key, entry in _cli_if_catalog().items():
        if value in (key.casefold(), Path(entry['file']).stem.casefold(), entry['title'].casefold()):
            return entry
    return None


def serve(session,manager,env):
    from runtime.CinterForgeCommands import _if_command
    from runtime.CinterForgeState import _if_story
    from types import SimpleNamespace
    root=manager.run_root/'.cinter/display-io'; root.mkdir(parents=True,exist_ok=True)
    fiction=session.display_fiction
    from runtime.CinterForgeBedrockMap import DisplayMaps
    maps=DisplayMaps(manager,root)
    try:
        while not session.closed:
            for request in sorted(root.glob('*.request.json'), key=lambda p:p.stat().st_mtime_ns if p.exists() else 0):
                if session.closed:return
                if not re.fullmatch(r'[0-9a-f]{32}\.request\.json',request.name):continue
                response=root/request.name.replace('.request.','.response.')
                data={}
                try:
                    if request.is_symlink() or request.stat().st_size>4096:raise ValueError('Invalid display request')
                    data=json.loads(request.read_text('utf-8'))
                    screen, command = data.get('screen'), data.get('command')
                    if not isinstance(screen,str) or not re.fullmatch(r'cinter_[0-9a-f]{32}',screen):
                        raise ValueError('Invalid display screen')
                    if not isinstance(command,str) or len(command)>180 or any(ord(c)<32 for c in command):
                        raise ValueError('Invalid display input')
                    action=data.get('action')
                    if action is not None:
                        if screen not in maps.active or action not in ('click','refresh'):
                            raise ValueError('No active CraftMaster map')
                        point=None
                        if action=='click':
                            w,h=maps.active[screen]['size']; point=data.get('point')
                            if not isinstance(point,list) or len(point)!=2 or any(type(v) is not int for v in point) or not (0<=point[0]<w and 0<=point[1]<h):
                                raise ValueError('Invalid map control coordinates')
                        result=maps.turn(screen,request.name.split('.')[0],action,point)
                    elif screen in maps.active:
                        result=maps.turn(screen,request.name.split('.')[0])
                    elif screen in fiction.active:
                        result=fiction.turn(screen,command)
                        if result['mode']=='terminal':
                            manager._append_log('CINTERFORGE_DISPLAY_IF_END '+screen)
                    else:
                        entry=fiction_entry(command)
                        if entry is not None:
                            if data.get('operator') is not True:
                                raise PermissionError('Starting a story requires a server operator.')
                            name=Path(entry['file']).stem
                            story=manager.steamapps/'common/CinterForge/InteractiveFiction/Titles'/name/entry['file']
                            if not story.is_file():
                                _if_command(entry['command'],'install',entry,path=story)
                            _if_story(SimpleNamespace(bedrock=manager),name)
                            manager._append_log('CINTERFORGE_DISPLAY_IF_REQUEST '+screen+' '+name)
                            result=fiction.turn(screen,command,entry=entry)
                            manager._append_log('CINTERFORGE_DISPLAY_IF_START '+screen+' '+name+' mode='+result['mode'])
                        else:
                            args=command_args(command)
                            if args[0]=='craftmaster':
                                if data.get('operator') is not True:
                                    raise PermissionError('Starting a map requires a server operator.')
                                w,h=data.get('width'),data.get('height')
                                if type(w) is not int or type(h) is not int or (w,h) not in {(128,128),(256,256),(384,384),(512,512),(256,128),(384,256),(512,384),(640,512)}:
                                    raise ValueError('Invalid display dimensions')
                                maps.start(screen,args[1],w,h)
                                result=maps.turn(screen,request.name.split('.')[0])
                                manager._append_log('CINTERFORGE_DISPLAY_MAP_START '+screen+' '+args[1])
                            else:
                                proc=subprocess.run([sys.executable,str(ASSETS/'CinterForgeTerminal.py'),'--terminal-cli',*args],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120,text=True,errors='replace')
                                result={'output':proc.stdout[-16384:], 'mode':'terminal'}
                                manager._append_log('CINTERFORGE_DISPLAY_COMMAND '+args[0]+' rc='+str(proc.returncode))
                except Exception as exc:
                    screen = data.get('screen') if isinstance(data,dict) else None
                    manager._append_log('CINTERFORGE_DISPLAY_COMMAND_FAILED '+str(screen)+' '+str(exc))
                    if isinstance(screen,str) and screen in maps.active:
                        maps.active.pop(screen,None)
                    shutil.rmtree(root/(request.name.split('.')[0]+'.tiles'),ignore_errors=True)
                    result={'output':str(exc),'mode':'if' if isinstance(screen,str) and screen in fiction.active else 'terminal'}
                tmp=response.with_suffix('.tmp');tmp.write_text(json.dumps(result),encoding='utf-8');tmp.replace(response)
                request.unlink(missing_ok=True)
            time.sleep(.15)
    finally:
        fiction.close()
