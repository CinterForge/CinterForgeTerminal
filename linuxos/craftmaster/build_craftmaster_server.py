#!/usr/bin/env python3
"""Acquire and prove the official Bedrock Dedicated Server build artifact.

macOS cannot execute Microsoft's Linux BDS natively: preflight acquires the exact
Linux package and Linux acceptance executes/smoke-tests it. Windows and Linux
smoke-test their native package immediately. Server artifacts remain build-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow this script to be executed directly while importing shared build_cinterforge modules.
_BUILD_CINTERFORGE_ROOT = Path(__file__).resolve().parent.parent
if str(_BUILD_CINTERFORGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUILD_CINTERFORGE_ROOT))
import argparse, hashlib, json, os, platform, queue, re, shutil, signal, socket, struct, subprocess, threading, time, urllib.error, urllib.request, zipfile

# This source is both a package import during LinuxKit Setup and a standalone
# CLI during offline Bedrock acceptance. Resolve the sibling helper accordingly.
if __package__:
    from .workspace_paths import cinterforgeBuildsRootPath
else:
    from workspace_paths import cinterforgeBuildsRootPath
from pathlib import Path
SCHEMA='cinterforge.craftmaster.server.v5'; PAGE='https://www.minecraft.net/en-us/download/server/bedrock'; LINKS_API='https://net-secondary.web.minecraft-services.net/api/v1.0/download/links'
def sha(p):
 h=hashlib.sha256();
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()
def discover(kind):
 download_type='serverBedrockWindows' if kind=='win' else 'serverBedrockLinux'
 req=urllib.request.Request(LINKS_API,headers={'User-Agent':'Mozilla/5.0 CinterForge','Accept':'application/json'})
 try:
  raw=urllib.request.urlopen(req,timeout=30).read()
  data=json.loads(raw.decode('utf-8'))
 except Exception as exc:
  raise RuntimeError(f'Official Minecraft download-links API failed: {LINKS_API}: {exc}') from exc
 links=((data.get('result') or {}).get('links') or []) if isinstance(data,dict) else []
 matches=[row.get('downloadUrl','') for row in links if isinstance(row,dict) and row.get('downloadType')==download_type]
 if len(matches)!=1:
  raise RuntimeError(f'Official Minecraft download-links API returned {len(matches)} {download_type} entries: {LINKS_API}')
 url=str(matches[0]).strip()
 expected='/bedrockdedicatedserver/bin-win/' if kind=='win' else '/bedrockdedicatedserver/bin-linux/'
 if not re.fullmatch(r'https://(?:www\.)?minecraft\.net/bedrockdedicatedserver/bin-(?:win|linux)/bedrock-server-[0-9.]+\.zip',url,re.I) or expected not in url:
  raise RuntimeError(f'Official Minecraft download-links API returned unexpected {download_type} URL: {url}')
 return url
def _download(url: str, archive: Path, attempts: int = 3):
 headers={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140 Safari/537.36','Accept':'application/zip,application/octet-stream;q=0.9,*/*;q=0.8','Accept-Encoding':'identity','Referer':PAGE,'Connection':'close'}
 part=archive.with_suffix(archive.suffix+'.part')
 last=None
 for attempt in range(1,attempts+1):
  part.unlink(missing_ok=True)
  try:
   req=urllib.request.Request(url,headers=headers)
   with urllib.request.urlopen(req,timeout=90) as r, part.open('wb') as out:
    status=getattr(r,'status',200); ctype=(r.headers.get('Content-Type') or '').lower(); expected=r.headers.get('Content-Length')
    if status!=200: raise RuntimeError(f'HTTP {status}')
    total=0
    while True:
     chunk=r.read(1024*1024)
     if not chunk: break
     out.write(chunk); total+=len(chunk)
    out.flush(); os.fsync(out.fileno())
   if expected is not None and total!=int(expected): raise RuntimeError(f'truncated response bytes={total} expected={expected}')
   if total<4 or part.read_bytes()[:4]!=b'PK\x03\x04': raise RuntimeError(f'response is not a ZIP bytes={total} content-type={ctype or "unknown"}')
   with zipfile.ZipFile(part) as z:
    bad=z.testzip()
    if bad is not None: raise RuntimeError(f'BDS ZIP CRC failure at {bad}')
   os.replace(part,archive); return
  except Exception as exc:
   last=exc; part.unlink(missing_ok=True)
   if attempt<attempts:
    print(f'[craftmaster-server] download attempt {attempt}/{attempts} failed: {exc}; retrying...',flush=True); time.sleep(attempt*2)
 raise RuntimeError(f'official BDS download failed after {attempts} attempts: {url}: {last}') from last
PRESERVED_SERVER_STATE = (
 "server.properties", "allowlist.json", "permissions.json", "worlds",
 "behavior_packs", "resource_packs", "development_behavior_packs",
 "development_resource_packs",
)
def _replace_server_from_archive_preserving_state(archive:Path,extract:Path):
 extract=Path(extract); archive=Path(archive)
 staging=extract.parent/(extract.name+".cinter-refresh")
 retired=extract.parent/(extract.name+".cinter-retired")
 for transient in (staging,retired):
  if transient.exists():
   raise RuntimeError(f"stale CraftMaster refresh directory requires attention: {transient}")
 staging.mkdir(parents=True)
 moved=[]
 old_retired=False
 promoted=False
 try:
  # Build the replacement completely beside the live tree.  Never recursively
  # delete the live Server directory: a running watcher/server can repopulate
  # mutable directories while rmtree is traversing them (observed on macOS).
  with zipfile.ZipFile(archive) as z:
   bad=z.testzip()
   if bad is not None: raise RuntimeError(f'BDS ZIP CRC failure at {bad}')
   z.extractall(staging)
  if extract.exists():
   for name in PRESERVED_SERVER_STATE:
    src=extract/name
    if not src.exists(): continue
    dst=staging/name
    if src.is_dir() and dst.is_dir():
     # Preserve authored/runtime state without deleting directories supplied by
     # the newly acquired official BDS.  In particular, behavior_packs and
     # resource_packs may contain version-matched Microsoft content required by
     # the new executable.  Overlay the preserved tree onto the fresh archive
     # tree, then remove the old copy so the live-tree mutation guard remains
     # authoritative.
     shutil.copytree(src,dst,dirs_exist_ok=True)
     shutil.rmtree(src)
    else:
     if dst.exists():
      if dst.is_dir(): shutil.rmtree(dst)
      else: dst.unlink()
     shutil.move(str(src),str(dst))
    moved.append(name)
  if moved:
   print(f'[craftmaster-server] preserving mutable server state before BDS refresh: {", ".join(moved)}',flush=True)
   # A path reappearing after it was moved proves another process is mutating
   # the live server.  Do not overwrite that newer state.
   reappeared=[name for name in moved if (extract/name).exists()]
   if reappeared:
    raise RuntimeError('CraftMaster live server changed during refresh; stop the process writing mutable state and retry: '+', '.join(reappeared))
  if extract.exists():
   os.replace(extract,retired); old_retired=True
  os.replace(staging,extract); promoted=True
  missing=[name for name in moved if not (extract/name).exists()]
  if missing: raise RuntimeError(f'CraftMaster state restore incomplete after BDS refresh: {missing}')
  if moved:
   print(f'[craftmaster-server] restored mutable server state after BDS refresh: {", ".join(moved)}',flush=True)
  if retired.exists():
   shutil.rmtree(retired)
 except Exception:
  # Before promotion, mutable state lives in staging; put it back in the live
  # tree.  After retiring the old tree, restore that tree first if promotion
  # did not complete.  Never silently discard either side of a failed swap.
  if old_retired and not promoted and retired.exists() and not extract.exists():
   os.replace(retired,extract); old_retired=False
  if not promoted and extract.exists() and staging.exists():
   for name in moved:
    src=staging/name; dst=extract/name
    if src.exists() and not dst.exists(): shutil.move(str(src),str(dst))
  raise
 finally:
  if staging.exists() and not any(staging.iterdir()): staging.rmdir()

def extract_pristine_acceptance_server(archive:Path,target:Path)->Path:
 archive=Path(archive); target=Path(target)
 if target.exists(): raise RuntimeError(f'CraftMaster acceptance target must not already exist: {target}')
 target.mkdir(parents=True)
 try:
  with zipfile.ZipFile(archive) as z:
   bad=z.testzip()
   if bad is not None: raise RuntimeError(f'BDS ZIP CRC failure at {bad}')
   z.extractall(target)
  exe=target/('bedrock_server.exe' if platform.system()=='Windows' else 'bedrock_server')
  if not exe.is_file(): raise RuntimeError(f'Official BDS archive did not contain expected executable: {exe}')
  if platform.system()!='Windows': exe.chmod(exe.stat().st_mode | 0o111)
  return target
 except Exception:
  shutil.rmtree(target,ignore_errors=True)
  raise

def acquire(root,kind):
 d=root/('Windows' if kind=='win' else 'Linux'); d.mkdir(parents=True,exist_ok=True); marker=d/'source.json'
 url=discover(kind); archive=d/'bedrock-server.zip'
 if not archive.is_file():
  print(f'[craftmaster-server] download official BDS {kind}: {url}',flush=True); _download(url,archive)
 try:
  with zipfile.ZipFile(archive) as z:
   bad=z.testzip()
   if bad is not None: raise RuntimeError(f'BDS ZIP CRC failure at {bad}')
 except Exception as exc:
  archive.unlink(missing_ok=True)
  raise RuntimeError(f'Official BDS download is not a valid ZIP: {url}: {exc}') from exc
 extract=d/'Server'; _replace_server_from_archive_preserving_state(archive,extract)
 exe=extract/('bedrock_server.exe' if kind=='win' else 'bedrock_server')
 if not exe.is_file(): raise RuntimeError(f'Official BDS archive did not contain expected executable: {exe}')
 if kind!='win': exe.chmod(exe.stat().st_mode | 0o111)
 marker.write_text(json.dumps({'url':url,'sha256':sha(archive),'bytes':archive.stat().st_size},indent=2)+'\n')
 return d,extract,url,archive
RAKNET_MAGIC=bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')
def _server_port(server:Path)->int:
 port=19132; props=server/'server.properties'
 if props.is_file():
  for raw in props.read_text(encoding='utf-8',errors='replace').splitlines():
   line=raw.strip()
   if line.startswith('server-port='):
    value=line.split('=',1)[1].strip()
    try: port=int(value)
    except ValueError: raise RuntimeError(f'invalid BDS server-port in {props}: {value}')
    break
 if not (1<=port<=65535): raise RuntimeError(f'invalid BDS server-port: {port}')
 return port
def raknet_ping(host:str,port:int,timeout:float=5.0)->dict:
 # Bedrock server-list status is a RakNet Unconnected Ping/Pong exchange.
 stamp=int(time.time()*1000) & 0xffffffffffffffff
 guid=0x43494e5445524647  # "CINTERFG"; only identifies this transient probe.
 packet=b'\x01'+struct.pack('>Q',stamp)+RAKNET_MAGIC+struct.pack('>Q',guid)
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
  sock.settimeout(timeout); sock.sendto(packet,(host,port)); reply,addr=sock.recvfrom(65535)
 if len(reply)<35 or reply[0]!=0x1c: raise RuntimeError(f'BDS RakNet ping returned invalid pong bytes={len(reply)} id={reply[:1].hex()}')
 if reply[17:33]!=RAKNET_MAGIC: raise RuntimeError('BDS RakNet pong magic mismatch')
 echoed=struct.unpack('>Q',reply[1:9])[0]
 if echoed!=stamp: raise RuntimeError('BDS RakNet pong timestamp mismatch')
 text_len=struct.unpack('>H',reply[33:35])[0]
 if len(reply)<35+text_len: raise RuntimeError(f'BDS RakNet pong truncated status bytes={len(reply)-35} expected={text_len}')
 status=reply[35:35+text_len].decode('utf-8',errors='replace')
 if not status: raise RuntimeError('BDS RakNet pong contained empty status')
 return {'host':addr[0],'port':port,'response':'unconnected-pong','status':status}
def _kill_smoke_process(p: subprocess.Popen) -> None:
 if p.poll() is not None: return
 try:
  if os.name == 'nt': p.kill()
  else: os.killpg(p.pid, signal.SIGKILL)
 except (ProcessLookupError, PermissionError):
  try: p.kill()
  except ProcessLookupError: pass
 try: p.wait(timeout=5)
 except subprocess.TimeoutExpired: raise RuntimeError(f'BDS smoke process could not be killed pid={p.pid}')

def smoke(server:Path, startup_timeout:float=45.0):
 server=server.resolve()
 exe=server/('bedrock_server.exe' if os.name=='nt' else 'bedrock_server')
 if not exe.is_file(): raise RuntimeError(f'BDS executable missing after extraction: {exe}')
 if os.name!='nt': exe.chmod(exe.stat().st_mode|0o111)
 env=os.environ.copy(); env['LD_LIBRARY_PATH']=str(server)+(':'+env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
 p=subprocess.Popen([str(exe)],cwd=server,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env,bufsize=1,start_new_session=(os.name!='nt'))
 lines=[]; output=queue.Queue()
 def _read_output():
  if not p.stdout: return
  for line in p.stdout: output.put(line)
 threading.Thread(target=_read_output,daemon=True).start()
 port=_server_port(server); listening=False; network=None; deadline=time.monotonic()+startup_timeout
 try:
  while time.monotonic()<deadline:
   try:
    line=output.get(timeout=0.1); lines.append(line); print('[craftmaster-server] '+line.rstrip(),flush=True)
    if f'Accepting clients on [::]:{port}' in line or f'Accepting clients on 0.0.0.0:{port}' in line: listening=True
   except queue.Empty: pass
   if p.poll() is not None: break
   if listening:
    try:
     network=raknet_ping('127.0.0.1',port,timeout=0.1); break
    except (OSError,RuntimeError): pass
  if not listening: raise RuntimeError(f'BDS smoke did not reach network-listening state within {startup_timeout:g}s; tail='+''.join(lines[-20:]))
  # A real RakNet pong is required for acceptance outside restricted sandboxes.
  if network is None: raise RuntimeError(f'BDS announced UDP {port} but RakNet ping did not receive a valid pong; network acceptance failed')
  print(f"[craftmaster-server] network smoke pass udp={network['host']}:{network['port']}",flush=True)
  # Console control is part of the acceptance contract, not merely a write
  # to stdin.  Require the server to answer `list` before asking it to stop.
  p.stdin.write('list\n'); p.stdin.flush()
  list_reply=[]; list_deadline=time.monotonic()+10.0; list_ok=False
  while time.monotonic()<list_deadline:
   try:
    line=output.get(timeout=0.1); lines.append(line); list_reply.append(line); print('[craftmaster-server] '+line.rstrip(),flush=True)
    # Current BDS answers `list` with a player-count sentence.  Match the
    # stable semantic portion rather than a particular count/version.
    if re.search(r'\bplayers? online\b',line,re.I) or re.search(r'\bThere are \d+/',line,re.I):
     list_ok=True; break
   except queue.Empty: pass
   if p.poll() is not None: break
  if not list_ok:
   raise RuntimeError('BDS console `list` command produced no verified player-list response; tail='+''.join(list_reply[-20:]))
  print('[craftmaster-server] console smoke pass command=list',flush=True)
  p.stdin.write('stop\n'); p.stdin.flush(); rc=p.wait(timeout=20)
  while True:
   try:
    line=output.get_nowait(); lines.append(line); print('[craftmaster-server] '+line.rstrip(),flush=True)
   except queue.Empty: break
  if rc!=0: raise RuntimeError(f'BDS graceful stop returned {rc}')
  return {'status':'pass','network_ready':True,'network':network,'console_command':'list','console_response_verified':True,'graceful_stop':True}
 finally:
  if p.poll() is None: _kill_smoke_process(p)

def _require_offline_archive(root: Path, kind: str) -> Path:
    """Check all required offline inputs before changing any live server state."""
    platform_directory = "Windows" if kind == "win" else "Linux"
    archive = root / platform_directory / "bedrock-server.zip"
    if not archive.is_file():
        raise RuntimeError(f"Offline Bedrock server archive missing: {archive}")

    executable_name = "bedrock_server.exe" if kind == "win" else "bedrock_server"
    with zipfile.ZipFile(archive) as package:
        invalid_entry = package.testzip()
        if invalid_entry is not None:
            raise RuntimeError(f"Offline Bedrock archive CRC failure: {invalid_entry}")
        if executable_name not in package.namelist():
            raise RuntimeError(
                f"Offline {platform_directory} Bedrock archive lacks {executable_name}: {archive}"
            )
    return archive


def resolve_product_root() -> Path:
    """Resolve the persistent outer Steam product root for producer and smoke.

    This intentionally matches the Cinter macOS nuclear-migration default. An
    explicitly configured Steam library wins; never substitute producer Builds.
    """
    configured = (
        os.environ.get("CINTERFORGE_PRODUCT_ROOT", "").strip()
        or os.environ.get("CINTERFORGE_STEAM_APP_INSTALL_DIR", "").strip()
    )
    if configured:
        product_root = Path(configured).expanduser().resolve()
    elif platform.system() == "Darwin":
        product_root = (
            Path.home() / "Library" / "Application Support" / "Steam"
            / "steamapps" / "common" / "CinterForge"
        ).resolve()
    else:
        raise RuntimeError(
            "Cannot resolve the outer CinterForge Steam installation. Set "
            "CINTERFORGE_STEAM_APP_INSTALL_DIR (or CINTERFORGE_PRODUCT_ROOT); "
            "refusing to publish Bedrock into Builds."
        )
    if product_root.name != "CinterForge":
        raise RuntimeError(f"Expected outer CinterForge product directory: {product_root}")
    if product_root.is_symlink():
        raise RuntimeError(f"Persistent product root must not be a symlink: {product_root}")
    return product_root


def install_server_to_applications(server: Path, kind: str, product_root: Path) -> Path:
    """Publish a prepared Bedrock server into permanent, platform-specific storage.

    Keep producer Builds intact. Never copy the downloaded ZIP into Applications.
    An existing installation is retained if publishing fails.
    """
    platform_name = "Windows" if kind == "win" else "Linux"
    executable_name = "bedrock_server.exe" if kind == "win" else "bedrock_server"
    if not (server / executable_name).is_file():
        raise RuntimeError(f"Cannot install Bedrock server without {executable_name}: {server}")
    destination = product_root / "Applications" / platform_name / "Minecraft Bedrock Server"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".installing")
    previous = destination.with_name(destination.name + ".previous")
    if staging.exists() or previous.exists():
        raise RuntimeError(f"Incomplete Bedrock installation needs attention: {staging} or {previous}")
    try:
        shutil.copytree(server, staging, symlinks=True)
        if not (staging / executable_name).is_file():
            raise RuntimeError(f"Staged Bedrock server executable missing: {staging / executable_name}")
        if destination.exists():
            # Keep user-edited world and configuration state across upgrades.
            for name in PRESERVED_SERVER_STATE:
                old = destination / name
                new = staging / name
                if not old.exists():
                    continue
                if old.is_dir():
                    shutil.copytree(old, new, dirs_exist_ok=True)
                else:
                    shutil.copy2(old, new)
            os.replace(destination, previous)
        try:
            os.replace(staging, destination)
        except Exception:
            if previous.exists() and not destination.exists():
                os.replace(previous, destination)
            raise
        if previous.exists():
            shutil.rmtree(previous)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def publish_servers_to_applications(root: Path, product_root: Path) -> list[Path]:
    """Publish both platform builds when present, even from a macOS host."""
    installed = []
    for kind, platform_name in (("linux", "Linux"), ("win", "Windows")):
        source = root / platform_name / "Server"
        executable = source / ("bedrock_server.exe" if kind == "win" else "bedrock_server")
        if executable.is_file():
            installed.append(install_server_to_applications(source, kind, product_root))
    return installed


def build(repo: Path, offline: bool = False):
    root = cinterforgeBuildsRootPath(repo) / "CraftMaster" / "Server"
    root.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system not in ("Windows", "Linux", "Darwin"):
        raise RuntimeError(f"Unsupported Bedrock server build host: {system}")

    kind = "win" if system == "Windows" else "linux"
    platform_directory = "Windows" if kind == "win" else "Linux"

    if offline:
        # macOS is the release producer for both Linux and native Windows.
        # Check both archives before refreshing either staged server tree.
        archive = _require_offline_archive(root, kind)
        if system == "Darwin":
            _require_offline_archive(root, "win")

        destination = root / platform_directory
        extract = destination / "Server"
        _replace_server_from_archive_preserving_state(archive, extract)
        executable_name = "bedrock_server.exe" if kind == "win" else "bedrock_server"
        executable = extract / executable_name
        if not executable.is_file():
            raise RuntimeError(f"Offline Bedrock archive lacks expected executable: {executable}")
        if kind != "win":
            executable.chmod(executable.stat().st_mode | 0o111)
        source_marker = destination / "source.json"
        url = json.loads(source_marker.read_text()).get("url", "") if source_marker.is_file() else ""
        if system == "Darwin":
            windows_archive = root / "Windows" / "bedrock-server.zip"
            _replace_server_from_archive_preserving_state(
                windows_archive, root / "Windows" / "Server"
            )
    else:
        destination, extract, url, archive = acquire(root, kind)
        # The macOS maintainer also stages the native Windows BDS package.
        # Windows consumers never need Linux or WSL to run it.
        if system == "Darwin":
            acquire(root, "win")

    native = system in ("Windows", "Linux")
    proof = smoke(extract) if native else {
        "status": "pending-linux-native-acceptance",
        "reason": "macOS cannot execute official Linux BDS natively",
    }
    manifest = {
        "schema": SCHEMA,
        "host": system,
        "platform": "windows-x64" if kind == "win" else "linux-x64",
        "official_download_page": PAGE,
        "source_url": url,
        "archive": str(archive.relative_to(root)),
        "archive_sha256": sha(archive),
        "smoke": proof,
        "deployment": False,
    }
    (root / "server.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # Publishing is mandatory: update-data must never silently stage Builds only.
    product_root = resolve_product_root()
    installed = publish_servers_to_applications(root, product_root)
    required_platform = "Windows" if system == "Windows" else "Linux"
    required = product_root / "Applications" / required_platform / "Minecraft Bedrock Server"
    if required not in installed:
        raise RuntimeError(f"Required persistent Bedrock installation was not published: {required}")
    for location in installed:
        print(f"[craftmaster-server] installed permanent Bedrock server: {location}", flush=True)
    print(f"[craftmaster-server] {proof['status']} archive={archive}")
    return manifest

def install_linuxkit_acceptance_server(repo: Path, target: Path, offline: bool = False) -> Path:
    """Install an isolated, official Linux Bedrock package for CFOS platform Test.

    Reuse the same official acquisition/cache as regular CraftMaster builds,
    but never touch the player's persistent server, worlds or Steam library.
    The target must not already exist (CFOS supplies a private temp directory).
    """
    repo = repo.resolve()
    builds = cinterforgeBuildsRootPath(repo)
    if not target.resolve().is_relative_to((builds / 'Intermediates').resolve()):
        raise RuntimeError(f'CFOS acceptance installation must be within Builds/Intermediates: {target}')
    cache = builds / 'Intermediates/CinterForgeTerminal/LinuxKit/Downloads'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / 'bedrock-server-linux.zip'
    if not archive.is_file():
        if offline:
            raise RuntimeError(f'Offline LinuxKit acceptance archive missing: {archive}')
        _download(discover('linux'), archive)
    with zipfile.ZipFile(archive) as z:
        broken = z.testzip()
        if broken is not None or 'bedrock_server' not in z.namelist():
            raise RuntimeError(f'Invalid official Bedrock Linux archive: {archive}: {broken}')
    server = extract_pristine_acceptance_server(archive, target)
    print(f'[craftmaster-server] CFOS LinuxKit test installed official Bedrock at {server}', flush=True)
    return server


if __name__=='__main__':
 ap=argparse.ArgumentParser()
 ap.add_argument('--repo-root',type=Path,required=True)
 ap.add_argument('--offline',action='store_true')
 ap.add_argument('--linuxkit-test-install',type=Path,
                 help='Install isolated official Linux BDS for CFOS Test under Builds/Intermediates')
 a=ap.parse_args()
 if a.linuxkit_test_install is not None:
  install_linuxkit_acceptance_server(a.repo_root,a.linuxkit_test_install,a.offline)
 else:
  build(a.repo_root,a.offline)
