"""Terminal-owned native Java provisioning and command adapter.

CraftMaster owns the server controller, generation and conversion. The exact
Stage data closure supplies that controller and its Mojang version pins.
No VM on a user's native Java host, no new server implementation, no install
or consent at application startup. Developer Setup can prepare foreign JDK
archives without executing them; Test and the runtime reuse verified bytes.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen
import zipfile

TARGETS = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}
VERSION = '25.0.5.1+2'
TAG = 'jdk-' + VERSION
RELEASE = 'https://api.github.com/repos/adoptium/temurin25-binaries/releases/tags/' + quote(TAG, safe='')
BASE = 'https://github.com/adoptium/temurin25-binaries/releases/download/' + quote(TAG, safe='') + '/'


def log(message):
    print('[Java] ' + str(message), flush=True)


def provider(data: Path, *, staged=True):
    """Load the owning implementation; do not copy or modify its pins."""
    if staged:
        manifest = json.loads((data.parent / 'manifest.json').read_text('utf-8'))
        if manifest.get('schema') != 'cinterforge.craftmaster.stage.v1':
            raise RuntimeError('Invalid CraftMaster Stage receipt')
        import hashlib
        for name in ('minecraft_data.py', 'world_artifacts.py', 'java_server.py', 'JavaGenerate.mcfunction'):
            path = data / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Missing packaged Java controller resource: ' + name)
            with path.open('rb') as f:
                actual = hashlib.file_digest(f, 'sha256').hexdigest()
            if actual != manifest.get('files', {}).get('data/' + name):
                raise RuntimeError('CraftMaster Java resource checksum mismatch: ' + name)
    spec = importlib.util.spec_from_file_location('_cinter_java_data', data / 'minecraft_data.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.MAC_JDK_VERSION != VERSION:
        raise RuntimeError('CraftMaster and Terminal Java version pins differ; rebuild together')
    return module


def native_target():
    target = TARGETS.get(platform.system())
    machine = platform.machine().casefold()
    arch = 'aarch64' if machine in ('arm64', 'aarch64') else 'x64' if machine in ('amd64', 'x86_64') else None
    if not target or not arch or (target in ('linux', 'win') and arch != 'x64'):
        raise RuntimeError('Unsupported native Java platform/architecture')
    return target, arch


def archive_name(target, arch):
    if target not in ('mac','linux','win') or arch not in ('x64','aarch64') or (target != 'mac' and arch != 'x64'):
        raise ValueError('Unsupported Java archive target')
    os_name = 'windows' if target == 'win' else target
    suffix = 'zip' if target == 'win' else 'tar.gz'
    return f'OpenJDK25U-jdk_{arch}_{os_name}_hotspot_25.0.5.1_2.{suffix}'


def _asset(target, arch, cm):
    name = archive_name(target, arch)
    if target == 'mac':
        _, digest = cm.MAC_JDK_PINS['arm64' if arch == 'aarch64' else 'x86_64']
        return {'filename': name, 'url': BASE + name, 'sha256': digest}
    # Exact upstream release and asset, not a guessed hash or a floating latest.
    # The GitHub release's official SHA-256 is mandatory and persisted locally.
    with urlopen(Request(RELEASE, headers={'User-Agent':'CinterForgeTerminal/Java','Accept':'application/vnd.github+json'}),timeout=90) as response:
        if urlsplit(response.geturl()).scheme != 'https':
            raise RuntimeError('Untrusted Java release metadata redirect')
        release = json.loads(response.read(8*1024*1024))
    if release.get('tag_name') != TAG or release.get('draft') or release.get('prerelease'):
        raise RuntimeError('Unexpected Temurin release metadata')
    matches = [item for item in release.get('assets',[]) if item.get('name') == name and item.get('state') == 'uploaded']
    if len(matches) != 1:
        raise RuntimeError('Pinned Temurin platform archive unavailable: ' + name)
    entry = matches[0]
    digest = str(entry.get('digest') or '')
    url = str(entry.get('browser_download_url') or '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or urlsplit(url).hostname != 'github.com' or urlsplit(url).scheme != 'https' or unquote(urlsplit(url).path) != '/adoptium/temurin25-binaries/releases/download/'+TAG+'/'+name:
        raise RuntimeError('Missing verified Temurin release SHA-256 or unexpected URL')
    return {'filename':name,'url':url,'sha256':digest[7:]}


def _root(root, target, arch):
    path = Path(root).absolute() / 'JDK' / target / arch / VERSION
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise RuntimeError('Linked Java runtime path')
    return path


def _java_relative(directory, target):
    pattern = '*/Contents/Home/bin/java' if target == 'mac' else '*/bin/java.exe' if target == 'win' else '*/bin/java'
    paths = list(directory.glob(pattern))
    if len(paths) != 1 or not paths[0].is_file() or not paths[0].resolve().is_relative_to(directory.resolve()):
        raise RuntimeError('Unexpected native JDK archive layout')
    return paths[0].relative_to(directory).as_posix()


def _extract(archive, directory, target):
    if target == 'win':
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                name = member.filename
                path = Path(name)
                if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts or
                        stat.S_ISLNK(member.external_attr >> 16) or not (directory/path).resolve().is_relative_to(directory.resolve())):
                    raise RuntimeError('Unsafe Windows JDK ZIP member: ' + name)
            z.extractall(directory)
    else:
        # Same verified-archive extraction strategy as CraftMaster Mac Setup;
        # no unsupported tarfile filter keyword on Python 3.11.
        tar = shutil.which('tar')
        if not tar:
            raise RuntimeError('Native tar required to extract verified JDK')
        subprocess.run([tar,'-xzf',str(archive),'-C',str(directory),'--no-same-owner','--no-same-permissions'],check=True)


def verify_jdk(root, target, arch, cm, *, execute=False):
    path = _root(root,target,arch)
    receipt = cm.json_read(path/'Runtime.json')
    filename = archive_name(target,arch)
    digest = receipt['archive_sha256']
    if receipt['version'] != VERSION or not re.fullmatch(r'[0-9a-f]{64}',digest):
        raise RuntimeError('Invalid native Java receipt')
    if target == 'mac' and digest != cm.MAC_JDK_PINS['arm64' if arch == 'aarch64' else 'x86_64'][1]:
        raise RuntimeError('Mac Java archive receipt does not match CraftMaster pin')
    archive = path/filename
    java = (path/'Runtime'/receipt['java']).resolve(strict=True)
    if archive.is_symlink() or cm.digest(archive) != digest or not java.is_relative_to((path/'Runtime').resolve()) or cm.digest(java) != receipt['java_sha256']:
        raise RuntimeError('Native Java runtime failed verification')
    if execute:
        cm.java_command(java)
        output = subprocess.check_output([str(java),'-version'],stderr=subprocess.STDOUT,text=True,timeout=30)
        if VERSION not in output:
            raise RuntimeError('Unexpected Java runtime version')
    return java


def provision_jdk(root, target, arch, cm, *, execute=False):
    try:
        return verify_jdk(root,target,arch,cm,execute=execute)
    except (OSError,KeyError,ValueError,RuntimeError,subprocess.SubprocessError):
        pass
    path = _root(root,target,arch)
    path.mkdir(parents=True,exist_ok=True)
    entry = _asset(target,arch,cm)
    archive = path/entry['filename']
    def validate(p):
        if cm.digest(p) != entry['sha256']:
            raise RuntimeError('Temurin archive SHA-256 mismatch')
    cm._download(entry['url'],archive,log,validator=validate)
    with tempfile.TemporaryDirectory(prefix='Java-',dir=path) as tmp:
        pending = Path(tmp)/'Runtime';pending.mkdir()
        log('Extracting verified '+entry['filename'])
        _extract(archive,pending,target)
        relative = _java_relative(pending,target)
        java = pending/relative
        if execute:
            cm.java_command(java)
        receipt = {'version':VERSION,'archive_sha256':entry['sha256'],'java':relative,'java_sha256':cm.digest(java)}
        destination = path/'Runtime'
        if destination.is_symlink():
            raise RuntimeError('Linked Java runtime destination')
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(pending,destination)
        cm.json_write(path/'Runtime.json',receipt)
    return verify_jdk(root,target,arch,cm,execute=execute)


def server_jar(root, cm, *, acquire=False, cached=None):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root,*root.parents)):
        raise RuntimeError('Linked Java server runtime path')
    target = root/f'minecraft_server_{cm.JAVA_VERSION}.jar'
    if target.is_symlink():
        raise RuntimeError('Linked server JAR')
    if target.is_file() and cm.digest(target,'sha1') == cm.SERVER_SHA1:
        return target
    if not acquire:
        raise RuntimeError('Java server is not installed; type java install')
    if cached and cached.is_file() and not cached.is_symlink() and cm.digest(cached,'sha1') == cm.SERVER_SHA1:
        root.mkdir(parents=True,exist_ok=True)
        shutil.copy2(cached,target)
    else:
        metadata = root/'version.json'
        cm._download(cm.VERSION_URL,metadata,log,sha1=cm.VERSION_SHA1)
        info = cm.json_read(metadata)
        entry = info['downloads']['server']
        if info['id'] != cm.JAVA_VERSION or entry['sha1'] != cm.SERVER_SHA1:
            raise RuntimeError('Mojang metadata does not match CraftMaster server pin')
        cm._download(entry['url'],target,log,sha1=cm.SERVER_SHA1)
    if cm.digest(target,'sha1') != cm.SERVER_SHA1:
        raise RuntimeError('Official Java server JAR checksum mismatch')
    return target


def runtime_root(server):
    # Use the same ownership boundary as existing Bedrock, never app resources.
    return server.bedrock.root.parent/'Java'


def java_status(root,data):
    cm = provider(data)
    target,arch = native_target()
    accepted = cm.java_eula_accepted(root)
    installed = False
    if accepted:
        try:
            verify_jdk(root,target,arch,cm)
            server_jar(root,cm)
            installed = True
        except (OSError,ValueError,KeyError,RuntimeError):
            pass
    return {'accepted':accepted,'installed':installed,'version':cm.JAVA_VERSION}


def install(root,data,*,staged=True,cached=None):
    cm = provider(data,staged=staged)
    cm.require_java_eula(root)
    target,arch = native_target()
    java = provision_jdk(root,target,arch,cm,execute=True)
    jar = server_jar(root,cm,acquire=True,cached=cached)
    log('Installed verified Java '+cm.JAVA_VERSION)
    return java,jar


def controller_command(binary,data,root,world,action,*,extra=()):
    cm=provider(data)
    target,arch=native_target()
    java=verify_jdk(root,target,arch,cm,execute=True)
    jar=server_jar(root,cm)
    return [sys.executable,'-B',str(data/'java_server.py'),action,'--world',str(world),'--data-dir',str(data),'--server-root',str(root),'--java',str(java),'--server-jar',str(jar),'--native-cli',str(binary),*extra]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('install','serve','generate'))
    parser.add_argument('--binary',required=True,type=Path)
    parser.add_argument('--data',required=True,type=Path)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--world',type=Path)
    parser.add_argument('--size',type=int,default=1024)
    parser.add_argument('--seed',default='')
    parser.add_argument('--cached-jar',type=Path)
    args=parser.parse_args(argv)
    try:
        install(args.root,args.data,cached=args.cached_jar)
        if args.action=='install': return 0
        if args.world is None: raise ValueError('World path required')
        command=controller_command(args.binary,args.data,args.root,args.world,args.action,extra=('--size',str(args.size),'--seed',args.seed) if args.action=='generate' else ())
        if os.name != 'nt':
            os.execv(sys.executable,command)
        return subprocess.call(command)
    except (OSError,RuntimeError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        log('FAIL: '+str(exc));return 1


if __name__=='__main__':
    raise SystemExit(main())


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
    cm = provider(data)
    root = runtime_root(server)
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
            return {**java_status(root,data),'worlds':server.bedrock.list_java_worlds(),'selected':selected,
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
        args=[sys.executable,'-B',str(Path(__file__).resolve()),action,'--binary',str(binary),'--data',str(data),'--root',str(root)]
        if world: args += ['--world',str(world)]
        if action=='generate': args += ['--size',str(size),'--seed',seed]
        cached = server.bedrock._java_saves_destination()/'CinterForge/ReferenceData/Java'/cm.JAVA_VERSION/f'minecraft_server_{cm.JAVA_VERSION}.jar'
        args += ['--cached-jar',str(cached)]
        with server.sessions_lock:
            if len(server.sessions)>=settings.MAX_SESSIONS: raise RuntimeError('Maximum terminal sessions reached')
            item=sessions.CinterForgeJavaTerminalSession(server.mutable_builds,server.system,80,24,url=server.origin,token=server.token,internal_token=server.cli_token,command=args,working_directory=settings.ASSETS,action=action)
            server.sessions[item.id]=item
        server.bedrock_terminal_requests.append({'id':item.id,'label':'Java','kind':'java','shell':'Java','platform':server.system})
        return {'terminal_session':item.id,'message':'Opened Java '+action+' Session.'}
