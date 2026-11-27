"""Stage, launch, and test the macOS Release WebView application under sibling Builds.

Cloudflare distribution packaging is a separate release lifecycle.
"""
# CINTERFORGE SOURCE HEADER — host/mac/mac_app.py
# PURPOSE: Implements the macOS mac app adapter, packaging, signing or bootstrap boundary used by the native Terminal lifecycle.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   _icon_source: Handle icon source through this component's established interfaces.
#   _png_chunk: Handle png chunk through this component's established interfaces.
#   _rgba_png: Decode ordinary 8-bit RGB/RGBA non-interlaced PNGs without Pillow.
#   _resize_png: Handle resize png through this component's established interfaces.
#   build_ico: Build build ico for this module.
#   build_icns: Build build icns for this module.
#   _copy_runtime_tree: Copy copy runtime tree for this module.
#   _stage_complete_resources: Assemble complete runtime before codesign; ZIP never edits the .app.
#   _sign_and_verify: Sign in the real login user's Keychain context, never Colima's HOME.
#   _sign_and_verify_with_login_keychain: Seal the *finished* app, after the executable, icon and plist are written.
#   ...and 7 additional internal entry points; see their local documentation.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
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
import socket
import subprocess
import threading
import time
import struct
import zlib

from host import HOST_RELEASE


def _icon_source() -> Path:
    # The author-supplied existing PNG shown in Finder. Do not substitute
    # another repository's logo, synthesize branding or create an icon in source.
    path = Path(__file__).resolve().parents[2] / 'app/build/resources/AppIcon.png'
    if not path.is_file():
        raise FileNotFoundError('CinterForgeTerminal/app/build/resources/AppIcon.png is missing; '
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
        (builds / 'Python/Environments/mac/arm64/cinterterminalenv',
         payload / 'Python/Environments/mac/arm64/cinterterminalenv'),
        (builds / 'InteractiveFiction/CodeOSS/Web', payload / 'CodeOSS'),
        (builds / 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph',
         payload / 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph'),
        (builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64',
         payload / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'),
        (builds / 'CinterForgeTerminal/Dependencies/BedrockDisplay/linux',
         payload / 'CinterForgeTerminal/Dependencies/BedrockDisplay/linux'),
        (builds / 'CinterForgeTerminal/Dependencies/ZMachine/0.3.2',
         payload / 'CinterForgeTerminal/Dependencies/ZMachine/0.3.2'),
    )
    # CinterForgeTerminal owns all authored shell files in its repository.
    # The Host stage contains native SDK and runtime products, but a previously
    # reconciled Host stage must not override newer authored application assets.
    # Construct the final, signed app from current repository source, and keep
    # the embedded Host manifest consistent with the exact bytes shipped.
    authored = Path(__file__).resolve().parents[2] / 'webapp'
    # Copy *all* authored webapp files from the current checkout. Never guess
    # which JavaScript, view, image, or runtime module a new feature needs.
    # The same dynamic inventory is used for Linux/Windows application builds.
    from host.lifecycle.universal_package import _authored_webapp_assets
    required = _authored_webapp_assets(authored.parent)
    catalog = json.loads((authored / 'data/mac_games.json').read_text(encoding='utf-8'))
    if catalog.get('schema') != 'cinterforge.terminal.macgames.v1' or len(catalog.get('games', [])) != 61:
        raise RuntimeError('Repository requires complete 61-game catalog')
    # Ship only the current Host manifest, never unrelated caches or retired source.
    from host.python.manager import _verify_host_stage, retire_removed_host_members
    host_receipt = _verify_host_stage(host, system='mac', arch='arm64', release=HOST_RELEASE)
    host_dest = payload / 'Python/Host/mac/arm64' / HOST_RELEASE
    if host_dest.is_symlink():
        raise RuntimeError('Unsafe embedded Host directory')
    host_dest.mkdir(parents=True, exist_ok=True)
    retire_removed_host_members(host_dest, host_receipt)
    for relative in (*host_receipt['files'], 'manifest.json'):
        dest = host_dest / relative
        if any(p.is_symlink() for p in (dest, *dest.parents) if p.is_relative_to(host_dest)):
            raise RuntimeError('Unsafe embedded Host member: ' + str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or not filecmp.cmp(host / relative, dest, shallow=False):
            shutil.copy2(host / relative, dest)
    for source, destination in sources:
        _copy_runtime_tree(source, destination)
    # CraftMaster is built/staged first by the Terminal Build orchestrator.
    # Copy its verified, *unsigned* native Mac closure into the actual app
    # before codesign. Never leave a previous signed CraftMaster executable
    # or obsolete data member behind when reconciling a reused .app directory.
    from host.lifecycle.craftmaster_dependency import (staged_craftmaster,
                                                        verify_embedded_craftmaster)
    craftmaster_stage = staged_craftmaster(builds, 'mac')
    embedded_craftmaster = payload / 'CinterForgeCraftMaster/mac'
    if embedded_craftmaster.is_symlink():
        raise RuntimeError('Unsafe embedded CraftMaster directory')
    if embedded_craftmaster.exists():
        if not embedded_craftmaster.is_dir():
            raise RuntimeError('Embedded CraftMaster path is not a directory')
        shutil.rmtree(embedded_craftmaster)
    _copy_runtime_tree(craftmaster_stage, embedded_craftmaster)
    verify_embedded_craftmaster(app, 'mac')
    # Build uses the already-provisioned Code-OSS Web tree but must package
    # CURRENT authored extension source. Setup's verified upstream build cache
    # is not evidence that Terminal's own extension.js is unchanged.
    from host.codeoss.producer import _stage_cinterforge_extension, validate_codeoss_web
    _stage_cinterforge_extension(payload / 'CodeOSS')
    # A Build consumer must never repair Setup outputs. Reject absent browser
    # tokenizer/extension files in the final app before codesign and Steam Stage.
    validate_codeoss_web(payload / 'CodeOSS')
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
    # Required launcher/game payloads must be present in the same authored
    # inventory; there are no separate hardcoded copy passes or script lists.
    if not (embedded / 'joshua/index.html').is_file() or not (embedded / 'joshua/LICENSE').is_file():
        raise RuntimeError('Missing staged JOSHUA source and license')
    if not all((embedded / ('launcher/assets/backgrounds/' + name + '.png')).is_file()
               for name in ('mac', 'windows', 'linux')):
        raise RuntimeError('Missing staged Mac/Windows/Linux Launcher backgrounds')
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
    """Sign in the real login user's Keychain context, never Colima's HOME.

    The build orchestrator uses managed_environment() to keep Colima/Lima
    state under sibling Builds. That environment changes HOME to a temporary
    managed directory. Xcode's Developer ID lookup runs before this change,
    but codesign runs afterwards and then cannot find the same identity.
    Restore only HOME for the duration of signing and always put it back.
    """
    if os.uname().sysname != 'Darwin':
        raise RuntimeError('macOS application signing requires the macOS host')
    import pwd
    login_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not login_home.is_dir():
        raise RuntimeError('Cannot locate the current macOS login home for Keychain signing')
    managed_home = os.environ.get('HOME')
    try:
        os.environ['HOME'] = str(login_home)
        if managed_home != str(login_home) and os.environ.get('CINTER_MAC_SIGN_IDENTITY'):
            print('[MAC SIGN] Using login Keychain context; Colima HOME kept separate', flush=True)
        _sign_and_verify_with_login_keychain(app)
    finally:
        if managed_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = managed_home


def _embedded_craftmaster_receipt(app: Path) -> Path | None:
    """Locate the platform-specific receipt inside a completed Mac app.

    CraftMaster Stage bytes have their own content-verified receipt. The Mac
    release signer may subsequently alter the Mach-O to add its code signature.
    That finalized, signed executable requires a corresponding *embedded*
    receipt; CraftMaster's original Stage manifest remains untouched.
    """
    folder = (app / 'Contents/Resources/Builds/CinterForgeCraftMaster/mac')
    manifest = folder / 'manifest.json'
    if not folder.exists():
        return None
    if folder.is_symlink() or manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError('Mac CraftMaster payload has no regular manifest')
    return manifest


def _refresh_signed_craftmaster_receipt(manifest: Path) -> bool:
    """Update only the executable digest after the native Mac signing pass.

    Reject any changed data or other unexpected member rather than rewriting
    all hashes to match potentially corrupted content. The receipt is sealed
    by re-signing the *outer* app after this update.
    """
    from host.lifecycle.craftmaster_dependency import _digest
    doc = json.loads(manifest.read_text(encoding='utf-8'))
    binary_name = 'bin/CinterForgeCraftMaster'
    files = doc.get('files')
    if (doc.get('schema') != 'cinterforge.craftmaster.stage.v1'
            or doc.get('platform') != 'mac' or not isinstance(files, dict)
            or binary_name not in files):
        raise RuntimeError('Invalid signed Mac CraftMaster receipt')
    binary_digest = None
    for name, expected in files.items():
        relative = Path(name)
        if (not isinstance(name, str) or not name or relative.is_absolute()
                or '..' in relative.parts or '\\' in name
                or not isinstance(expected, str) or len(expected) != 64):
            raise RuntimeError('Unsafe signed Mac CraftMaster receipt member')
        file = manifest.parent / relative
        if file.is_symlink() or not file.is_file():
            raise RuntimeError('Signed Mac CraftMaster payload member missing: ' + name)
        actual = _digest(file)
        if name == binary_name:
            binary_digest = actual
        elif actual != expected:
            raise RuntimeError('Mac signing unexpectedly changed CraftMaster data: ' + name)
    if files[binary_name] == binary_digest:
        return False
    doc['files'][binary_name] = binary_digest
    manifest.write_text(json.dumps(doc, indent=2, sort_keys=True) + '\n',
                        encoding='utf-8')
    print('[MAC SIGN] Updated embedded CraftMaster SHA-256 for signed Mach-O', flush=True)
    return True



def _bundle_craftmaster_native_libraries(app: Path) -> list[Path]:
    """Make CraftMaster relocatable; never load Homebrew libraries at runtime.

    The standalone converter is built with macOS LibArchive found by CMake.
    Its Mach-O may reference a Homebrew dylib signed by a different Team ID.
    Hardened Runtime rejects that dependency when the signed app is launched.
    Bundle the *complete* non-system Mach-O dependency closure, rewrite each
    load command to @loader_path, and sign the copies with the app identity.
    """
    binary = (app / 'Contents/Resources/Builds/CinterForgeCraftMaster/mac/'
              'bin/CinterForgeCraftMaster')
    if not binary.is_file() or binary.is_symlink():
        raise RuntimeError('Mac CraftMaster executable is missing or linked')
    # Keep @loader_path references short enough for the existing Mach-O headers.
    destination = binary.parent.parent / 'lib'
    if destination.is_symlink():
        raise RuntimeError('Refusing linked CraftMaster native library directory')
    if destination.exists():
        if not destination.is_dir():
            raise RuntimeError('CraftMaster native library path is not a directory')
        shutil.rmtree(destination)  # The app is a generated Build output.

    def dependencies(file: Path) -> list[str]:
        result = subprocess.run(['/usr/bin/otool', '-L', str(file)],
                                check=True, capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if not lines or not lines[0].rstrip().endswith(':'):
            raise RuntimeError('Cannot inspect Mach-O dependencies: ' + str(file))
        libraries = [line.strip().split(' (', 1)[0] for line in lines[1:] if line.strip()]
        # otool lists a dylib's own LC_ID_DYLIB first; it is not a dependency.
        return libraries[1:] if file != binary else libraries

    pending = [binary]
    copied: dict[str, Path] = {}
    while pending:
        file = pending.pop()
        for dependency in dependencies(file):
            if dependency.startswith(('/usr/lib/', '/System/Library/')):
                continue  # Apple platform libraries pass hardened validation.
            if dependency.startswith('@loader_path/'):
                local = (file.parent / dependency[len('@loader_path/'):]).resolve()
                if local.is_file() and local.is_relative_to(app.resolve()):
                    continue
                raise RuntimeError('Unresolved CraftMaster @loader_path: ' + dependency)
            if not dependency.startswith('/'):
                raise RuntimeError('Unresolved CraftMaster dynamic library: ' + dependency)
            upstream = Path(dependency)
            if upstream.is_relative_to(app):
                continue
            if not upstream.is_file():
                raise RuntimeError('Required CraftMaster dynamic library missing: ' + dependency)
            name = upstream.name
            real = upstream.resolve(strict=True)
            if name in copied and copied[name] != real:
                raise RuntimeError('Conflicting CraftMaster library names: ' + name)
            if name not in copied:
                destination.mkdir(parents=True, exist_ok=True)
                target = destination / name
                shutil.copy2(real, target)
                copied[name] = real
                pending.append(target)
            target = destination / name
            relative = os.path.relpath(target, file.parent).replace(os.sep, '/')
            subprocess.run(['/usr/bin/install_name_tool', '-change', dependency,
                            '@loader_path/' + relative, str(file)], check=True,
                           capture_output=True, text=True)
    print(f'[MAC SIGN] Bundled {len(copied)} CraftMaster native libraries inside app',
          flush=True)
    return sorted((destination / name for name in copied), key=str)


def _sign_and_verify_with_login_keychain(app: Path) -> None:
    """Seal the *finished* app, after the executable, icon and plist are written.

    Copying a Mach-O or changing Info.plist after signing invalidates the seal.
    Ad-hoc signing is for local development only. For distribution, Build
    uses a Developer ID Application keychain identity and notarizes the exact
    final package copy before creating the immutable release ZIP.
    """
    if os.uname().sysname != 'Darwin':
        raise RuntimeError('macOS application signing requires the macOS host')
    # The original CraftMaster Stage receipt must match *before* codesign.
    # Never use signing to conceal an already-corrupt packaged executable.
    craftmaster_receipt = _embedded_craftmaster_receipt(app)
    if craftmaster_receipt is not None:
        from host.lifecycle.craftmaster_dependency import verify_embedded_craftmaster
        verify_embedded_craftmaster(app, 'mac')
        craftmaster_libraries = _bundle_craftmaster_native_libraries(app)
    else:
        craftmaster_libraries = []
    # Seal the real VM launcher with only the unrestricted virtualization
    # entitlement *before* sealing the outer app. --deep re-signs nested Mach-O
    # files and previously stripped the VM's own entitlements. Preserve
    # entitlements when re-signing the complete nested runtime closure.
    from host.linux.launcher_signing import sign_launcher, has_virtualization_entitlement
    vm = (app / 'Contents/Resources/Builds/CinterForgeTerminal/Dependencies/'
          'LinuxKit/mac/arm64/CinterBDSVM')
    if not vm.is_file():
        raise RuntimeError(f'Packaged VM launcher missing: {vm}')
    identity = os.environ.get('CINTER_MAC_SIGN_IDENTITY', '').strip()
    # The Xcode Release resolver passes the exact Developer ID certificate
    # SHA-1, not its display name. Preserve the preexisting named-identity
    # override while allowing this fingerprint through to codesign unchanged.
    if identity and not (identity.startswith('Developer ID Application: ') or
                         re.fullmatch(r'[0-9A-Fa-f]{40}', identity)):
        raise RuntimeError('CINTER_MAC_SIGN_IDENTITY must be a Developer ID Application name or SHA-1')
    if identity:
        sign_launcher(vm, identity=identity)
    else:
        sign_launcher(vm)
    sign_flags = (['--options', 'runtime', '--timestamp'] if identity
                  else ['--timestamp=none'])
    # --deep alone does not reliably sign loose dylibs under Resources.
    # Sign each copy with the SAME identity as the app.
    for library in craftmaster_libraries:
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', identity or '-',
                        *sign_flags, str(library)], check=True,
                       capture_output=True, text=True)
    # Public beta and release are signed identically. Preserve the existing
    # nested-code and VM entitlements; do not require a channel-specific file.
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
    if craftmaster_receipt is not None and _refresh_signed_craftmaster_receipt(craftmaster_receipt):
        # --deep already signed the embedded Mach-O. Re-seal only the outer
        # bundle's resource inventory: another --deep pass could change the
        # executable again and invalidate the freshly recorded SHA-256.
        subprocess.run(['/usr/bin/codesign', '--force',
                        '--preserve-metadata=entitlements', '--sign', identity or '-',
                        *sign_flags, str(app)],
                       check=True, capture_output=True, text=True)
        _verify_signature(app)
    if craftmaster_receipt is not None:
        from host.lifecycle.craftmaster_dependency import verify_embedded_craftmaster
        verify_embedded_craftmaster(app, 'mac')
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
    from host.python.manager import _verify_host_stage
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


def accept_native_ui(builds: Path, *, launcher: Path | None = None,
                     steamapps: Path | None = None) -> None:
    """Run real JS/native acceptance using the installed signed app, when supplied.

    Do not use the Build-tree executable as a substitute for the one actually
    installed in Steam. Test never repairs or stages dependencies.
    """
    launcher = launcher or application(builds) / 'Contents/MacOS/CinterForgeTerminal'
    launcher = launcher.resolve(strict=True)
    app = launcher.parent.parent.parent
    _verify_signature(app)
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    working = builds / 'Run/CinterForgeTerminal/mac'
    working.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if steamapps is not None:
        # Never accidentally execute the signed staged app in Xcode developer
        # mode when Test was invoked from a developer shell.
        env.pop('CINTER_BUILDS_ROOT', None)
        env['CINTER_TERMINAL_RUNTIME_MODE'] = 'installed'
        env['CINTER_STEAMAPPS'] = str(steamapps.resolve(strict=True))
    evidence = logs / ('mac-installed-ui-acceptance.log' if steamapps else 'mac-ui-acceptance.log')
    if steamapps is not None:
        # Never attach to a developer/foreign HTTP service and accidentally
        # report its working WebView as evidence for this staged installation.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(('127.0.0.1', 20300)) == 0:
                raise RuntimeError('Real staged app Test requires free port 20300; '
                                   'quit the existing Terminal first (no process killed)')
    # The normal lifecycle streams subprocess output in real time. Native UI
    # acceptance does the same instead of hiding minutes of startup/cleanup
    # inside capture_output; the full transcript also remains in Builds/Logs.
    with evidence.open('w', encoding='utf-8') as transcript:
        process = subprocess.Popen([str(launcher), '--accept-ui'], cwd=working,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, env=env)
        messages: list[str] = []
        def follow_native() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                messages.append(line)
                transcript.write(line)
                transcript.flush()
                print(line, end='', flush=True)
        follower = threading.Thread(target=follow_native, name='cinter-installed-ui-log', daemon=True)
        follower.start()
        try:
            code = process.wait(timeout=240)
        except subprocess.TimeoutExpired as error:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            follower.join(timeout=10)
            raise RuntimeError('Installed native Mac UI timed out; see ' + str(evidence)) from error
        follower.join(timeout=10)
    output = ''.join(messages)
    if (code or
            'TERMINAL_UI_PASS ready=1 museum=mac-plus terminal=vt100-terminal callback=native' not in output or
            'Native shutdown completed' not in output):
        tail = output.strip().splitlines()[-15:]
        raise RuntimeError('Installed Mac app UI/shutdown acceptance failed (exit ' +
                           str(code) + '):\n' + '\n'.join(tail) +
                           '\nSee ' + str(evidence))
    print('[MAC APP] PASS actual signed staged UI callback and owned shutdown; '
          'evidence=' + str(evidence), flush=True)


def test(builds: Path) -> None:
    builds = builds.resolve()
    binary = application(builds) / 'Contents/MacOS/CinterForgeTerminal'
    if not binary.is_file():
        raise RuntimeError('Native Mac app not staged')
    _verify_signature(application(builds))
    host = builds / 'Python/Host/mac/arm64' / HOST_RELEASE
    profiles_path = host / 'webapp/data/terminal_styles.json'
    if not profiles_path.is_file():
        raise RuntimeError('Current Host release ' + HOST_RELEASE +
                           ' is not staged with its museum profiles; '
                           'run ./Cinter stage mac before ./Cinter test mac: ' + str(profiles_path))
    from host.python.manager import _verify_host_stage
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
