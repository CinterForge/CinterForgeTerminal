"""Stage, launch, and test the macOS Release WebView application under sibling Builds.

Cloudflare distribution packaging is a separate release lifecycle.
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import urllib.request
import shutil
import subprocess
import time
import struct
import zlib

from host import HOST_RELEASE


def _icon_source() -> Path:
    # The author-supplied existing PNG shown in Finder. Do not substitute
    # another repository's logo, synthesize branding or create an icon in source.
    path = Path(__file__).resolve().parents[1] / 'build/resources/AppIcon.png'
    if not path.is_file():
        raise FileNotFoundError('CinterForgeTerminal/build/resources/AppIcon.png is missing; '
                                'restore the authored PNG before building platform icons')
    return path


def _png_chunk(name: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))


def _rgba_png(source: Path) -> tuple[int, int, bytes]:
    """Decode ordinary 8-bit RGB/RGBA non-interlaced PNGs without Pillow.

    This keeps native Windows icon generation hermetic under MSYS2/MinGW.
    """
    content = source.read_bytes()
    if content[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('AppIcon.png is not PNG')
    offset=8;raw=b'';width=height=channels=0;ctype=0
    while offset+12 <= len(content):
        length=struct.unpack_from('>I',content,offset)[0]
        name=content[offset+4:offset+8]
        chunk=content[offset+8:offset+8+length]
        if len(chunk)!=length: raise ValueError('Truncated AppIcon PNG')
        offset+=length+12
        if name==b'IHDR':
            width,height,depth,ctype,compression,filtering,interlace=struct.unpack('>IIBBBBB',chunk)
            if depth!=8 or ctype not in (2,6) or compression or filtering or interlace:
                raise ValueError('AppIcon must be an 8-bit RGB or RGBA non-interlaced PNG')
            channels=3 if ctype==2 else 4
            if not (16 <= width <= 4096 and 16 <= height <= 4096):
                raise ValueError('Unsupported AppIcon dimensions')
        if name==b'IDAT':raw+=chunk
        if name==b'IEND':break
    if not width or not raw:raise ValueError('Missing AppIcon pixel data')
    inflated=zlib.decompress(raw)
    rowbytes=width*channels
    expected=height*(rowbytes+1)
    if len(inflated)!=expected:raise ValueError('Unexpected PNG payload length')
    result=bytearray(width*height*4);prior=bytearray(rowbytes);cursor=0
    for y in range(height):
        filter_type=inflated[cursor];cursor+=1
        scan=bytearray(inflated[cursor:cursor+rowbytes]);cursor+=rowbytes
        if filter_type>4:raise ValueError('Unsupported PNG filter')
        for i in range(rowbytes):
            left=scan[i-channels] if i>=channels else 0
            up=prior[i]
            above_left=prior[i-channels] if i>=channels else 0
            if filter_type==1:scan[i]=(scan[i]+left)&255
            elif filter_type==2:scan[i]=(scan[i]+up)&255
            elif filter_type==3:scan[i]=(scan[i]+(left+up)//2)&255
            elif filter_type==4:
                estimate=left+up-above_left
                options=(abs(estimate-left),abs(estimate-up),abs(estimate-above_left))
                predictor=(left,up,above_left)[options.index(min(options))]
                scan[i]=(scan[i]+predictor)&255
        for x in range(width):
            start=(y*width+x)*4; pixel=x*channels
            result[start:start+3]=scan[pixel:pixel+3]
            result[start+3]=scan[pixel+3] if channels==4 else 255
        prior=scan
    return width,height,bytes(result)


def _resize_png(width: int, height: int, rgba: bytes, side: int) -> bytes:
    # Nearest-neighbour is deterministic and sufficient for derived icons.
    stream=bytearray()
    for y in range(side):
        stream.append(0)
        src_y=min(height-1,int((y+.5)*height/side))
        for x in range(side):
            src_x=min(width-1,int((x+.5)*width/side))
            i=(src_y*width+src_x)*4
            stream.extend(rgba[i:i+4])
    header=struct.pack('>IIBBBBB',side,side,8,6,0,0,0)
    return (b'\x89PNG\r\n\x1a\n'+_png_chunk(b'IHDR',header)+
            _png_chunk(b'IDAT',zlib.compress(bytes(stream),level=9))+
            _png_chunk(b'IEND',b''))


def build_ico(source: Path, output: Path) -> None:
    width,height,rgba=_rgba_png(source)
    sizes=(16,24,32,48,64,128,256)
    images=[_resize_png(width,height,rgba,side) for side in sizes]
    directory=bytearray(struct.pack('<HHH',0,1,len(sizes)))
    start=6+16*len(sizes)
    for side,image in zip(sizes,images):
        directory.extend(struct.pack('<BBBBHHII',side%256,side%256,0,0,1,32,len(image),start))
        start+=len(image)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(bytes(directory)+b''.join(images))


def build_icns(icon: Path, destination: Path, builds: Path) -> None:
    iconset = builds / 'Intermediates/CinterForgeTerminal/mac-icon/AppIcon.iconset'
    if iconset.exists(): shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    # iconutil recognizes these exact names, including Retina sizes.
    for side in (16,32,128,256,512):
        for scale in (1,2):
            pixels=side*scale
            suffix='@2x' if scale==2 else ''
            output=iconset / f'icon_{side}x{side}{suffix}.png'
            subprocess.run(['/usr/bin/sips','-s','format','png','-z',str(pixels),str(pixels),
                            str(icon),'--out',str(output)],check=True,
                           stdout=subprocess.DEVNULL)
    destination.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['/usr/bin/iconutil','-c','icns',str(iconset),'-o',str(destination)],check=True)


def _copy_runtime_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError('Required staged application input missing: '+str(source))
    # Reconcile build-owned bytes in-place without touching sibling Builds or
    # Steam user content. Never link outside the finished signed application.
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source.resolve()):
                raise RuntimeError('External application runtime link: '+str(path))
            if resolved.is_dir():
                continue  # Link aliases are not application runtime dependencies.
            path = resolved
        dest = destination / relative
        if path.is_dir():
            dest.mkdir(parents=True,exist_ok=True)
        elif path.is_file():
            dest.parent.mkdir(parents=True,exist_ok=True)
            if not dest.is_file() or not filecmp.cmp(path,dest,shallow=False):
                shutil.copy2(path,dest)
        else:
            raise RuntimeError('Unsupported app runtime member: '+str(path))


def _stage_complete_resources(builds: Path, app: Path) -> None:
    """Assemble complete runtime before codesign; ZIP never edits the .app."""
    payload = app / 'Contents/Resources/Builds'
    host = builds / 'Python/Host/mac/arm64' / HOST_RELEASE
    sources = (
        (host, payload / 'Python/Host/mac/arm64' / HOST_RELEASE),
        (builds / 'Python/Environments/mac/arm64/cinterterminalenv',
         payload / 'Python/Environments/mac/arm64/cinterterminalenv'),
        (builds / 'InteractiveFiction/CodeOSS/Web', payload / 'CodeOSS'),
        (builds / 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph',
         payload / 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph'),
        (builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64',
         payload / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'),
        (builds / 'CinterForgeTerminal/Dependencies/ZMachine/0.3.2',
         payload / 'CinterForgeTerminal/Dependencies/ZMachine/0.3.2'),
    )
    # CinterForgeTerminal owns all authored shell files in its repository.
    # The Host stage contains native SDK and runtime products, but a previously
    # reconciled Host stage must not override newer authored application assets.
    # Construct the final, signed app from current repository source, and keep
    # the embedded Host manifest consistent with the exact bytes shipped.
    authored = Path(__file__).resolve().parents[1] / 'webapp'
    required = ('CinterForgeTerminal.py', 'if_titles.json', 'mac_games.json',
                'index.html', 'terminal.js', 'terminal.css', 'terminal_styles.json')
    for name in required:
        if not (authored / name).is_file():
            raise RuntimeError('Missing authored Terminal shell asset in repository: ' + name)
    catalog = json.loads((authored / 'mac_games.json').read_text(encoding='utf-8'))
    if catalog.get('schema') != 'cinterforge.terminal.macgames.v1' or len(catalog.get('games', [])) != 67:
        raise RuntimeError('Repository requires complete 67-game catalog')
    for source, destination in sources:
        _copy_runtime_tree(source, destination)
    # Build uses the already-provisioned Code-OSS Web tree but must package
    # CURRENT authored extension source. Setup's verified upstream build cache
    # is not evidence that Terminal's own extension.js is unchanged.
    from webapp.codeoss.producer import _stage_cinterforge_extension
    _stage_cinterforge_extension(payload / 'CodeOSS')
    embedded_host = payload / 'Python/Host/mac/arm64' / HOST_RELEASE
    embedded = embedded_host / 'webapp'
    receipt_path = embedded_host / 'manifest.json'
    if not receipt_path.is_file():
        raise RuntimeError('Mac app requires staged Host manifest: ' + str(receipt_path))
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if not isinstance(receipt.get('files'), dict):
        raise RuntimeError('Mac app Host manifest has no files inventory')
    for name in required:
        source = authored / name
        destination = embedded / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
            shutil.copy2(source, destination)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        receipt['files']['webapp/' + name] = digest
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Mac app embedded Terminal asset differs from repository: ' + name)
    joshua_source=authored / 'joshua'
    if not (joshua_source / 'index.html').is_file() or not (joshua_source / 'LICENSE').is_file():
        raise RuntimeError('Missing authored JOSHUA standalone source and license')
    joshua_dest=embedded / 'joshua'
    if joshua_dest.is_dir():
        shutil.rmtree(joshua_dest)
    shutil.copytree(joshua_source,joshua_dest)
    for source in sorted(joshua_source.rglob('*')):
        if source.is_file():
            rel=source.relative_to(authored)
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            receipt['files']['webapp/' + rel.as_posix()]=digest
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    # The guest probe and loader are required by Bedrock when users install
    # it later. Neither LinuxKit Setup nor the app includes a BDS executable.
    guest = payload / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64/GuestRuntime'
    for name in ('glibc-x86_64/ld-linux-x86-64.so.2','cinter-nethernet-probe'):
        if not (guest / name).is_file():
            raise RuntimeError('Bedrock guest dependency not provisioned: '+str(guest/name))
    if any('Servers/Bedrock' in str(p) for p in payload.rglob('bedrock_server')):
        raise RuntimeError('Bedrock binaries must never be packaged before EULA')


def _sign_and_verify(app: Path) -> None:
    """Seal the *finished* app, after the executable, icon and plist are written.

    Copying a Mach-O or changing Info.plist after signing invalidates the seal.
    Ad-hoc signing is for local development only. For distribution, Build
    uses a Developer ID Application keychain identity and notarizes the exact
    final package copy before creating the immutable release ZIP.
    """
    if os.uname().sysname != 'Darwin':
        raise RuntimeError('macOS application signing requires the macOS host')
    # Seal the real VM launcher with only the unrestricted virtualization
    # entitlement *before* sealing the outer app. --deep re-signs nested Mach-O
    # files and previously stripped the VM's own entitlements. Preserve
    # entitlements when re-signing the complete nested runtime closure.
    from linuxos.launcher_signing import sign_launcher, has_virtualization_entitlement
    vm = (app / 'Contents/Resources/Builds/CinterForgeTerminal/Dependencies/'
          'LinuxKit/mac/arm64/CinterBDSVM')
    if not vm.is_file():
        raise RuntimeError(f'Packaged VM launcher missing: {vm}')
    identity = os.environ.get('CINTER_MAC_SIGN_IDENTITY', '').strip()
    if identity and not identity.startswith('Developer ID Application: '):
        raise RuntimeError('CINTER_MAC_SIGN_IDENTITY must be an installed Developer ID Application identity')
    if identity:
        sign_launcher(vm, identity=identity)
    else:
        sign_launcher(vm)
    sign_flags = (['--options', 'runtime', '--timestamp'] if identity
                  else ['--timestamp=none'])
    if identity:
        # Apple notarization inspects *all* nested Mach-O code, including the
        # private Python/Host runtime below Resources (which --deep alone does
        # not necessarily discover). Sign inner code before sealing the app.
        macho_magic = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                       b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                       b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce'}
        for nested in sorted((p for p in (app / 'Contents').rglob('*')
                              if p.is_file() and not p.is_symlink()),
                             key=lambda p: (-len(p.parts), str(p))):
            if nested in (vm, app / 'Contents/MacOS/CinterForgeTerminal'):
                continue
            with nested.open('rb') as executable:
                if executable.read(4) not in macho_magic:
                    continue
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', identity,
                            '--preserve-metadata=entitlements', *sign_flags,
                            str(nested)], check=True, capture_output=True, text=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--deep',
                    '--preserve-metadata=entitlements', '--sign', identity or '-',
                    *sign_flags, str(app)], check=True, capture_output=True, text=True)
    _verify_signature(app)
    if not has_virtualization_entitlement(vm):
        raise RuntimeError('VM entitlements missing after packaging; refusing broken release')


def _verify_signature(app: Path) -> None:
    result = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict',
                             '--verbose=2', str(app)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Mac app code signature invalid: ' + str(app) + '\n' +
                           (result.stderr or result.stdout).strip())


def application(builds: Path) -> Path:
    return builds / 'CinterForgeTerminal/Applications/mac/arm64/Release/CinterForge Terminal.app'


def stage(builds: Path) -> Path:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('Mac app staging requires the sibling Builds directory')
    from host.manager import _verify_host_stage
    host = builds / 'Python/Host/mac/arm64' / HOST_RELEASE
    receipt = _verify_host_stage(host, system='mac', arch='arm64', release=HOST_RELEASE)
    if not receipt.get('features', {}).get('application_shell'):
        raise RuntimeError('The staged Host did not build the native WebView shell')
    source = host / 'bin/CinterForgeTerminal'
    if not source.is_file():
        raise RuntimeError('Staged macOS Terminal executable missing')
    with source.open('rb') as executable:
        header = executable.read(4)
    if header not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                      b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
        raise RuntimeError('Staged macOS Terminal must be a real Mach-O executable')
    python = builds / 'Python/Environments/mac/arm64/cinterterminalenv/bin/python'
    if not python.is_file():
        raise RuntimeError('Mac portable Python missing; run ./Cinter stage mac')
    app = application(builds)
    macos = app / 'Contents/MacOS'
    macos.mkdir(parents=True, exist_ok=True)
    launcher = macos / 'CinterForgeTerminal'
    shutil.copy2(source, launcher)
    launcher.chmod(0o755)
    # Older debug stages used a shell script plus a .native companion.
    # Build-owned stale launchers are obsolete; the actual Mach-O is the app entry.
    (macos / 'CinterForgeTerminal.native').unlink(missing_ok=True)
    resources = app / 'Contents/Resources'
    resources.mkdir(parents=True, exist_ok=True)
    (resources / 'HostRelease.txt').write_text(HOST_RELEASE + '\n')
    info = {
        'CFBundleName': 'CinterForge Terminal',
        'CFBundleDisplayName': 'CinterForge Terminal',
        'CFBundleIdentifier': 'com.cinterforge.terminal',
        'CFBundleExecutable': 'CinterForgeTerminal',
        'CFBundlePackageType': 'APPL',
        'CFBundleVersion': HOST_RELEASE,
        'CFBundleShortVersionString': HOST_RELEASE,
        'LSMinimumSystemVersion': '12.0',
        'NSLocalNetworkUsageDescription': 'Discover and connect to CinterForge web services on your local network.',
        'NSBonjourServices': ['_http._tcp'],
    }
    # The app icon derives only from the already-authored Terminal asset.
    icon = _icon_source()
    destination=app / 'Contents/Resources/AppIcon.icns'
    build_icns(icon,destination,builds)
    info['CFBundleIconFile']='AppIcon.icns'
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    _stage_complete_resources(builds, app)
    _sign_and_verify(app)
    print('[MAC APP] staged ' + str(app), flush=True)
    return app


def launch(builds: Path, *, verify: bool = False) -> Path:
    builds = builds.resolve()
    app = application(builds)
    launcher = app / 'Contents/MacOS/CinterForgeTerminal'
    if not launcher.is_file():
        raise RuntimeError('Mac app not staged; run ./Cinter stage mac')
    _verify_signature(app)
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / 'mac-app.log'
    working = builds / 'Run/CinterForgeTerminal/mac'
    working.mkdir(parents=True, exist_ok=True)
    with log.open('w', encoding='utf-8') as output:
        process = subprocess.Popen([str(launcher)], cwd=working,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    if verify:
        # Check the service started, not just that the process spawned.
        ready_url = None
        for _ in range(60):
            if process.poll() is not None:
                break
            line = log.read_text(encoding='utf-8', errors='replace')
            match = re.search(r'managed HTTP service ready: (http://127\.0\.0\.1:[0-9]{1,5}/?)', line)
            if match:
                ready_url = match.group(1)
                break
            time.sleep(.25)
        if not ready_url:
            process.terminate()
            raise RuntimeError('Mac WebView service failed to start; see ' + str(log))
        try:
            with urllib.request.urlopen(ready_url, timeout=3) as page:
                html = page.read(512 * 1024).decode('utf-8', errors='replace')
            if not all(word in html for word in ('terminal-profile', 'terminal-overlay')):
                raise RuntimeError('Mac Terminal UI is missing its interactive controls')
        except Exception as exc:
            process.terminate()
            raise RuntimeError('Mac Terminal HTTP UI is unavailable; see ' + str(log)) from exc
    print(f'[MAC APP] launched pid={process.pid} log={log}', flush=True)
    return app


def accept_native_ui(builds: Path) -> None:
    """Execute actual staged Release app and require JS -> native menu + PTY callbacks."""
    launcher = application(builds) / 'Contents/MacOS/CinterForgeTerminal'
    _verify_signature(application(builds))
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    (builds / 'Run/CinterForgeTerminal/mac').mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(launcher), '--accept-ui'],
                            cwd=builds / 'Run/CinterForgeTerminal/mac',
                            capture_output=True, text=True, timeout=45, check=False)
    (logs / 'mac-ui-acceptance.log').write_text(result.stdout + result.stderr)
    if result.returncode or 'TERMINAL_UI_PASS ready=1 museum=mac-plus terminal=vt100-terminal callback=native' not in result.stdout:
        # Preserve the full log and show its diagnostic tail in lifecycle output.
        # Native UI evidence is still mandatory; never mask a missing callback.
        tail = (result.stdout + result.stderr).strip().splitlines()[-12:]
        raise RuntimeError('Real macOS Terminal native UI callback failed (exit '
                           + str(result.returncode) + '):\n'
                           + '\n'.join(tail) + '\nSee ' + str(logs / 'mac-ui-acceptance.log'))


def test(builds: Path) -> None:
    builds = builds.resolve()
    binary = application(builds) / 'Contents/MacOS/CinterForgeTerminal'
    if not binary.is_file():
        raise RuntimeError('Native Mac app not staged')
    _verify_signature(application(builds))
    host = builds / 'Python/Host/mac/arm64' / HOST_RELEASE
    profiles_path = host / 'webapp/terminal_styles.json'
    if not profiles_path.is_file():
        raise RuntimeError('Current Host release ' + HOST_RELEASE +
                           ' is not staged with its museum profiles; '
                           'run ./Cinter stage mac before ./Cinter test mac: ' + str(profiles_path))
    from host.manager import _verify_host_stage
    _verify_host_stage(host, system='mac', arch='arm64', release=HOST_RELEASE)
    from json import loads
    profiles = loads(profiles_path.read_text(encoding='utf-8'))
    terminal_names = {entry['id'] for entry in profiles.get('profiles', [])}
    if not {'apple-ii', 'vt100-terminal', 'ibm-pc-vga'} <= terminal_names:
        raise RuntimeError('Mac Terminal is missing one of its three presentation profiles')
    if profiles.get('codeoss', {}).get('id') != 'codeoss' or not any(
            item.get('id') == 'quadra-650' and item.get('lagomorph')
            for item in profiles.get('emulators', [])):
        raise RuntimeError('Mac Terminal is missing CodeOSS or Lagomorph from its museum UI')
    if not (builds / 'InteractiveFiction/CodeOSS/Web').is_dir() or not (
            builds / 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph/Lagomorph-Quadra650.iso').is_file():
        raise RuntimeError('CodeOSS or Lagomorph data was not staged; rerun ./Cinter stage mac')
    # Interactive native/WebView launch acceptance is deferred to the final
    # DevTools phase. Ordinary lifecycle Test validates the signed bundle,
    # staged Host and resources without starting a desktop application.
    print('[MAC APP] PASS staged signature, Host, Code-OSS and Lagomorph resources ' +
          '(native UI launch deferred to DevTools acceptance)', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'test', 'launch', 'make-ico'))
    parser.add_argument('--builds', type=Path)
    parser.add_argument('--icon-source', type=Path)
    parser.add_argument('--icon-output', type=Path)
    args = parser.parse_args()
    if args.action == 'make-ico':
        if not args.icon_source or not args.icon_output:
            parser.error('make-ico requires --icon-source and --icon-output')
        build_ico(args.icon_source, args.icon_output)
        return 0
    if args.builds is None: parser.error(args.action+' requires --builds')
    if args.action == 'stage':
        stage(args.builds)
    elif args.action == 'test':
        test(args.builds)
    else:
        launch(args.builds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
