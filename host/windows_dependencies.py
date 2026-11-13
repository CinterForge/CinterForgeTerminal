"""Stage the COMPLETE macOS/Linux-hosted Windows build/execute closure in Builds.

This runs during ``./Cinter setup all``, BEFORE any Windows CMake command.
The portable Windows cross environment is a verified Linux/amd64 OCI image;
MinGW-w64, Wine, Xvfb and dependent Linux shared libraries are INSIDE that
image.  WebView source/headers, the actual Fixed Version Windows WebView2 browser and
native Windows MSYS2 bootstrap installer are staged separately.  A byte-checked
WindowsToolchains.zip is generated for transfer to a second machine.

A saved image is not native Windows acceptance, nor an operational Docker
engine.  No manifest is written as 'passed' until the entire archive exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import time
import urllib.request
import zipfile

from host.webview import provision as provision_webview, provision_webview2
from host.wine_webview2 import _install_archive as provision_offline_webview2
from host.windows_environment import WindowsCrossEnvironment
from host.setup_progress import progress_step
from host.gui_preflight import verify_guest_gui
from host.windows_install import MSYS2_INSTALLER_URL, MSYS2_VERSION
from host.windows_fixed_runtime import (ensure as provision_fixed_webview2,
                                        VERSION as WEBVIEW2_FIXED_VERSION)
from host.windows_msys2_cross import (ensure as provision_msys2_cross,
                                     _fetch as fetch_msys2_base,
                                     BASE_URL as MSYS2_BASE_URL)

SCHEMA = 'cinterforge.terminal.windows-dependencies.v2'
ZIP_SCHEMA = 'cinterforge.terminal.windows-toolchains-transfer.v2'
REQUIRED = ('WindowsCrossImage', 'WebViewSource', 'WebView2SDK',
            'WebView2FixedBrowser', 'WindowsMSYS2Installed', 'MSYS2Installer',
            'WebView2OfflineRuntime')


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as src:
        for part in iter(lambda: src.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def _download_msys2(builds: Path) -> Path:
    """Acquire the existing official MSYS2 installer without losing a good retry.

    Keep the established version, URL, filename and Builds layout.  A crash
    between creating the payload and its receipt must not permanently poison
    Setup; however, an unverified orphan payload is *never* silently adopted.
    Re-download into a separate temporary file and compare bytes before
    recovering provenance.  A receipt without its payload is likewise
    recovered only when the new download matches its recorded SHA-256.
    """
    stage = builds / 'CinterForgeTerminal/Toolchains/Windows/Downloads'
    stage.mkdir(parents=True, exist_ok=True)
    installer = stage / f'msys2-x86_64-{MSYS2_VERSION}.exe'
    receipt = stage / f'msys2-x86_64-{MSYS2_VERSION}.json'
    original = json.loads(receipt.read_text(encoding='utf-8')) if receipt.is_file() else None
    if original is not None and (original.get('url') != MSYS2_INSTALLER_URL or
                                 not isinstance(original.get('sha256'), str) or
                                 len(original['sha256']) != 64):
        raise RuntimeError(f'Windows MSYS2 installer receipt integrity failure: {receipt}')

    def checked(path: Path) -> str:
        if not path.is_file() or path.stat().st_size <= 1_000_000:
            raise RuntimeError(f'Windows MSYS2 installer is invalid: {path}')
        with path.open('rb') as stream:
            signature = stream.read(2)
        if signature != b'MZ':
            raise RuntimeError(f'Windows MSYS2 installer has no PE signature: {path}')
        return _sha(path)

    if installer.is_file() and original is not None:
        if original['sha256'] != checked(installer):
            raise RuntimeError(f'Windows MSYS2 installer integrity failure: {installer}')
        return installer
    if installer.exists() and not installer.is_file():
        raise RuntimeError(f'Windows MSYS2 installer path is not a file: {installer}')

    # Without a receipt, a leftover download is not a trusted build input.
    # Download the SAME source again to verify it, without overwriting the
    # earlier bytes or using a different mirror/version.
    partial = stage / (installer.name + '.part')
    try:
        print('[WINDOWS DEPENDENCIES] downloading native Windows MSYS2 bootstrap: '
              + MSYS2_INSTALLER_URL, flush=True)
        req = urllib.request.Request(MSYS2_INSTALLER_URL,
                                     headers={'User-Agent': 'CinterForgeTerminal-Setup/1.0'})
        with urllib.request.urlopen(req, timeout=180) as src, partial.open('wb') as dst:
            shutil.copyfileobj(src, dst)
        digest = checked(partial)
        if original is not None and digest != original['sha256']:
            raise RuntimeError('Redownloaded MSYS2 installer differs from its recorded SHA-256')
        if installer.is_file():
            if digest != checked(installer):
                raise RuntimeError('Orphaned MSYS2 installer differs from official redownload; '
                                   'retaining both for inspection')
            partial.unlink()
        else:
            partial.replace(installer)
        receipt.write_text(json.dumps({'url': MSYS2_INSTALLER_URL,
                                       'sha256': digest, 'size': installer.stat().st_size},
                                      indent=2, sort_keys=True) + '\n', encoding='utf-8')
        return installer
    finally:
        partial.unlink(missing_ok=True)


def _verify_image(builds: Path) -> Path:
    image_dir, manifest = WindowsCrossEnvironment.validate_staged(builds)
    return image_dir / (manifest['dockerfile_sha256'][:16] + '.tar')


def _verify_staged_closure(builds: Path, entries: dict[str, Path]) -> dict:
    """Verify Setup's local dependency closure without packaging a running VM.

    Source acquisition and tool-specific integrity/version checks happen above.
    The DevTools transfer artifact is a separate Stage/Publish concern: the
    live Lima disk's ``in_use_by`` symlink is an active ownership lock, not a
    relocatable build dependency.  Never stop, copy or mutate a running VM
    just to finish Windows Setup.
    """
    missing = set(REQUIRED) - set(entries)
    if missing:
        raise RuntimeError(f'Windows Setup is missing required dependencies: {sorted(missing)}')
    categories: dict[str, str] = {}
    for name, entry in entries.items():
        # Resolve only after checking the entry itself: resolving first masks
        # symlinked roots and can silently adopt an external dependency.
        if entry.is_symlink():
            raise RuntimeError(f'Windows dependency root must not be a symlink: {name}: {entry}')
        root = entry.resolve()
        if not root.is_relative_to(builds):
            raise RuntimeError(f'Windows dependency escapes Builds: {name}: {entry}')
        if root.is_file():
            if root.stat().st_size == 0:
                raise RuntimeError(f'Windows dependency is empty: {name}: {root}')
        elif root.is_dir():
            if not any(root.iterdir()):
                raise RuntimeError(f'Windows dependency directory is empty: {name}: {root}')
        else:
            raise RuntimeError(f'Windows dependency is missing: {name}: {root}')
        categories[name] = root.relative_to(builds).as_posix()
    return {'status': 'verified-local', 'packaging': 'deferred',
            'categories': categories}


def _transfer_archive(builds: Path, entries: dict[str, Path], *, output: Path) -> dict:
    """Make a transfer ZIP that contains exactly the recorded build tool inputs.

    Archive paths are relative to Builds, so extracted content may be merged
    back into the Builds folder on another compatible development machine.
    """
    roots = {name: path.resolve() for name, path in entries.items()}
    files: dict[str, Path] = {}
    for name, root in roots.items():
        if not root.is_relative_to(builds) or root.is_symlink():
            raise RuntimeError(f'{name} escapes Builds: {root}')
        if root.is_file():
            paths = (root,)
        elif root.is_dir():
            paths = (p for p in sorted(root.rglob('*')) if p.is_file())
        else:
            raise RuntimeError(f'{name} missing from Builds: {root}')
        if root.is_dir():
            linked_directories = [p for p in root.rglob('*') if p.is_symlink() and p.is_dir()]
            if linked_directories:
                raise RuntimeError(f'Windows closure has unresolved directory symlinks: {linked_directories[:4]}')
        for path in paths:
            if not path.resolve().is_relative_to(builds):
                raise RuntimeError(f'Windows dependency references external file: {path}')
            # Follow only symlinks that stay inside the Builds closure. ZIP
            # stores bytes, not links, so relocation cannot break them.
            rel = path.relative_to(builds).as_posix()
            if rel in files and files[rel] != path:
                raise RuntimeError(f'Windows dependency path collision: {rel}')
            files[rel] = path
    if not files or set(REQUIRED) - set(entries):
        raise RuntimeError('Windows dependency transfer archive missing required category')
    payload = {'schema': ZIP_SCHEMA, 'platform': 'linux/amd64-wine-cross',
               'source': 'Builds-relative', 'categories': {n: p.relative_to(builds).as_posix()
                                                      for n, p in roots.items()},
               'files': {rel: {'sha256': _sha(p), 'bytes': p.stat().st_size}
                         for rel, p in files.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + '.part')
    try:
        with zipfile.ZipFile(partial, 'w', allowZip64=True,
                             compression=zipfile.ZIP_DEFLATED, compresslevel=3) as z:
            for rel, path in sorted(files.items()):
                z.write(path, rel)
            z.writestr('WindowsToolchains-manifest.json',
                       json.dumps(payload, indent=2, sort_keys=True) + '\n')
        # Verify actual emitted archive, not just in-memory staging names.
        with zipfile.ZipFile(partial) as z:
            names = z.namelist()
            if len(names) != len(set(names)) or set(names) != set(files) | {'WindowsToolchains-manifest.json'}:
                raise RuntimeError('WindowsToolchains ZIP contains missing or unexpected entries')
            if z.testzip() is not None:
                raise RuntimeError('WindowsToolchains ZIP CRC failed')
            for rel, item in payload['files'].items():
                with z.open(rel) as src:
                    h = hashlib.sha256()
                    for block in iter(lambda: src.read(1024 * 1024), b''):
                        h.update(block)
                if h.hexdigest() != item['sha256']:
                    raise RuntimeError(f'WindowsToolchains ZIP SHA-256 mismatch: {rel}')
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    return {'output': str(output), 'sha256': _sha(output), 'files': len(files),
            'bytes': output.stat().st_size}



def _mac_transfer_entries(builds: Path) -> dict[str, Path]:
    """Collect the managed Mac host and the short, physically Builds-owned VM.

    Tool binaries live in the long-lived mac toolchain; socket-bearing Lima,
    Colima and Docker state is intentionally at Builds/CFTVM instead.  Both
    trees are required in the Windows transfer archive.
    """
    from host.mac_bootstrap import (COLIMA_PROFILE, VM_STATE_FOLDER,
                                    managed_state_root)

    mac = builds / 'CinterForgeTerminal/Toolchains/mac'
    state = managed_state_root(builds)
    receipt = state / 'backend.json'
    if not receipt.is_file():
        raise RuntimeError(f'Missing verified Mac container engine receipt: {receipt}')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if (data.get('schema') != 'cinterforge.terminal.managed-mac-oci.v2'
            or data.get('state_root') != VM_STATE_FOLDER
            or data.get('profile') != COLIMA_PROFILE
            or not data.get('engine_version')):
        raise RuntimeError(f'Invalid managed Mac container engine receipt: {receipt}')
    # The Docker CLI package alone is insufficient: a clean Mac also needs
    # the separately provisioned Buildx plugin for --progress/BuildKit builds.
    # Check the actual executable copied into DOCKER_CONFIG, as well as the
    # original pinned binary included with the Mac host environment.
    buildx_source = mac / 'env/bin/docker-buildx'
    buildx_registered = state / 'docker/cli-plugins/docker-buildx'
    buildx_digest = data.get('buildx_plugin_sha256')
    if (not isinstance(buildx_digest, str) or len(buildx_digest) != 64 or
            not buildx_source.is_file() or buildx_source.is_symlink() or
            not buildx_registered.is_file() or buildx_registered.is_symlink() or
            _sha(buildx_source) != buildx_digest or
            _sha(buildx_registered) != buildx_digest):
        raise RuntimeError('Managed Mac Docker Buildx plugin missing or modified; '
                           're-run ./Cinter setup all to restore verified toolchain')
    entries = {'MacContainerBackend': state}
    for name, rel in (
        ('MacHostEnvironment', 'env'),
        ('MacLimaRuntime', 'lima'),
        ('MacMicromamba', 'mamba'),
        ('MacMicromambaCache', 'mamba-cache'),
        ('MacDownloads', 'downloads'),
        ('MacBootstrapManifest', 'bootstrap.json'),
        ('MacResolvedPackageManifest', 'mac-packages-explicit.txt'),
    ):
        path = mac / rel
        if not path.exists():
            raise RuntimeError(f'Missing staged macOS Windows-build dependency {name}: {path}')
        entries[name] = path
    return entries



def _verify_wine_runtime(cross: WindowsCrossEnvironment, *, cwd: Path) -> dict[str, str]:
    """Functional Setup acceptance; never repair or provision in Build/Publish.

    Avoid Wine's interactive Mono/Gecko first-run prompt here. The independent
    native Linux wixl MSI producer does not require either component.
    """
    # Set up Wine on the managed Colima Linux disk, never on the macOS
    # bind mount. A previously verified prefix is reused across Setup runs.
    if hasattr(cross, 'prepare_wine_runtime'):
        with progress_step('prepare guest-native Wine storage'):
            cross.prepare_wine_runtime()
    initialized = (cross.wine_prefix_initialized()
                   if hasattr(cross, 'wine_prefix_initialized') else False)
    if not initialized:
        log_path = cwd / 'Logs/CinterForgeTerminal/windows-wineboot.log'
        print(f'[WINDOWS SETUP] Wine boot diagnostic log: {log_path}', flush=True)
        with progress_step('initialize managed Wine prefix (wineboot --init)'):
            command = ['env', 'WINEDLLOVERRIDES=mscoree,mshtml=',
                       'WINEDEBUG=warn+all,err+all', 'wineboot', '--init']
            boot = cross.execute(command, cwd=cwd, timeout=180, graphical=True,
                                 diagnostic_log=log_path)
        if boot.returncode:
            raise RuntimeError('Windows cross Wine prefix initialization failed: '
                               + (boot.stdout or '')[-1000:])
    else:
        print('[WINDOWS SETUP] REUSE previously verified Wine prefix', flush=True)
    with progress_step('verify Wine executable loader (winepath)'):
        result = cross.execute(['winepath', '-w', '/builds'], cwd=cwd, timeout=90,
                               graphical=True)
    if (result.returncode or not (result.stdout or '').strip() or
            not (result.stdout or '').strip().lower().endswith('\\builds')):
        raise RuntimeError('Windows cross Wine runtime failed executable preflight (winepath): '
                           + (result.stdout or '')[-1000:])
    if hasattr(cross, 'mark_wine_prefix_initialized'):
        cross.mark_wine_prefix_initialized()
    return {'wineboot': 'prefix-initialized', 'winepath': 'executable-verified'}


def ensure(builds: Path) -> dict:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError(f'Windows dependency staging requires sibling Builds directory: {builds}')
    if platform.system() not in ('Darwin', 'Linux'):
        raise RuntimeError('Windows cross-dependency staging requires a macOS or Linux host')
    stage = builds / 'CinterForgeTerminal/Toolchains/WindowsCross'
    stage.mkdir(parents=True, exist_ok=True)
    log = builds / 'Logs/CinterForgeTerminal/windows-dependencies.json'
    log.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': SCHEMA, 'status': 'failed', 'host': platform.system(),
              'required': list(REQUIRED), 'stages': {}, 'timestamp': time.time()}
    try:
        # First create a real cross compiler + Wine runtime (all system libs in OCI layers).
        cross = WindowsCrossEnvironment(builds)
        cross.ensure_portable_image()
        # Confirm the actual image runs every cross-build tool, not just an
        # image inspect. Missing executables fail Windows Setup immediately.
        report['cross_tool_versions'] = cross.version_probes(Path(__file__).resolve().parents[1])
        for component, command in {
            'xvfb': ['xvfb-run', '--help'],
            'windows-binutils': ['x86_64-w64-mingw32-objdump', '--version'],
            'cab-extractor': ['7z', '--help'],
        }.items():
            with progress_step(f'verify Windows cross dependency {component}'):
                proc = cross.execute(command, cwd=builds, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f'Windows cross runtime missing {component}: {proc.stdout[-400:]}')
            report['stages'][component] = 'executable-verified'
        # Provision the *existing* native Linux sample image during Setup,
        # and validate its real display/VNC path before any Wine process.
        # The later Developer Tooling phase reuses this exact image/receipt.
        from host.three_samples import _docker_image
        linux_docker = (cross.docker if cross.provider != 'host' else
                        (shutil.which('docker') or shutil.which('podman')))
        if not linux_docker:
            from host.mac_bootstrap import ensure_docker
            linux_docker = ensure_docker(builds)
        linux_image = _docker_image(builds, str(linux_docker),
                                   builds / 'Logs/CinterForgeTerminal/DevTooling-linux-image.log')
        with progress_step('verify Linux X11 and VNC display'):
            report['stages']['LinuxGUI'] = verify_guest_gui(
                builds, guest='linux', docker=str(linux_docker), image=linux_image)
        # Do not start Wine until a real X11 screen, RFB connection and
        # noVNC HTTP endpoint all pass. Wine commands then re-run this check
        # in their own container before launch, on that exact same display.
        with progress_step('verify Windows X11 and VNC display before Wine'):
            report['stages']['WindowsGUI'] = verify_guest_gui(
                builds, guest='windows', docker=cross.docker if cross.provider != 'host' else None,
                image=cross.image if cross.provider != 'host' else None)
        # A Wine version string does not prove the actual PE loader works.
        # Initialize the image-specific, Builds-owned prefix before probing
        # winepath (the old shared prefix could fail loading kernel32.dll).
        report['stages'].update(_verify_wine_runtime(cross, cwd=builds))
        image_archive = _verify_image(builds)
        report['stages']['WindowsCrossImage'] = str(image_archive)
        print('[WINDOWS DEPENDENCIES] OCI MinGW/Wine/Xvfb image staged: ' + str(image_archive), flush=True)

        with progress_step('provision WebView source'):
            webview = provision_webview(builds)
        report['stages']['WebViewSource'] = str(webview)
        with progress_step('provision Windows WebView2 SDK'):
            sdk = provision_webview2(builds)
        report['stages']['WebView2SDK'] = str(sdk)
        with progress_step('provision offline WebView2 runtime'):
            installer, _ = provision_offline_webview2(builds)
        report['stages']['WebView2OfflineRuntime'] = str(installer)
        with progress_step('verify/download Windows MSYS2 bootstrap'):
            msys2_installer = _download_msys2(builds)
        report['stages']['MSYS2Installer'] = str(msys2_installer)
        # Executable closure, not simply installers: both browser and MSYS2
        # must be fully unpacked/installed in Builds during Setup.
        with progress_step('provision fixed Windows WebView2 browser'):
            browser_folder = provision_fixed_webview2(builds, executor=cross)
        report['stages']['WebView2FixedBrowser'] = str(browser_folder)
        # The official MSYS2 base archive is functionally equivalent to the
        # Qt installer, which hangs under Wine. Native UCRT64 packages are
        # resolved and staged without executing Cygwin-derived tools in Wine.
        with progress_step('verify/download Windows MSYS2 base archive'):
            msys2_base = fetch_msys2_base(
                MSYS2_BASE_URL + f'msys2-base-x86_64-{MSYS2_VERSION}.tar.xz',
                msys2_installer.parent / f'msys2-base-x86_64-{MSYS2_VERSION}.tar.xz')
        with progress_step('provision Windows MSYS2 UCRT64 toolchain'):
            msys2_root = provision_msys2_cross(builds, cross, msys2_base)
        report['stages']['WindowsMSYS2Installed'] = str(msys2_root)
        entries = {'WindowsCrossImage': image_archive,
                   'WindowsCrossManifest': image_archive.with_suffix('.json'),
                   'WebViewSource': webview,
                   'WebView2SDK': sdk,
                   'WebView2OfflineRuntime': installer,
                   'WebView2OfflineRuntimeManifest': installer.parent / 'manifest.json',
                   'MSYS2Installer': msys2_installer,
                   'MSYS2InstallerManifest': msys2_installer.with_suffix('.json'),
                   'MSYS2BaseArchive': msys2_base,
                   'MSYS2BaseReceipt': msys2_base.with_name(msys2_base.name + '.json'),
                   'MSYS2NativePackageCache': msys2_root.parent / 'Downloads/UCRT64',
                   'WindowsMSYS2Installed': msys2_root,
                   'WebView2FixedBrowser': browser_folder.parent,
                   'WebView2FixedCAB': (builds / 'Intermediates/CinterForgeTerminal/WebView2'
                                       / f'Microsoft.WebView2.FixedVersionRuntime.{WEBVIEW2_FIXED_VERSION}.x64.cab'),
                   'WebView2FixedAcquisition': (builds / 'Intermediates/CinterForgeTerminal/WebView2'
                                       / f'fixed-{WEBVIEW2_FIXED_VERSION}-x64.json')}
        if platform.system() == 'Darwin':
            # Include the independently shortened, physically Builds-owned VM
            # state, not the old deep mac/container tree that breaks AF_UNIX.
            entries.update(_mac_transfer_entries(builds))
        # Packaging is deferred until the four-artifact DevTools release.
        # In particular, a live Lima disk lock is NOT an exportable tool.
        report['closure'] = _verify_staged_closure(builds, entries)
        report['status'] = 'passed'
        print('[WINDOWS DEPENDENCIES] Local Windows toolchain closure verified; '
              'DevTools packaging deferred', flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        raise
    finally:
        log.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    args = parser.parse_args()
    ensure(args.builds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
