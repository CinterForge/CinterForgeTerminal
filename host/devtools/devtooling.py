"""Version and installed-tool-size inventory for Terminal developer tools.

Executable tools require real version probes. The macOS Conda Assimp package
is a prebuilt SDK without an `assimp` CLI; its version and dylib size come
from the verified installed package receipt, and Test executes a real C API
OBJ import. A missing tool or unknown size fails Setup. Xcode is size-exempt.
All evidence and generated content live in sibling Builds.
"""
# CINTERFORGE SOURCE HEADER — host/devtools/devtooling.py
# PURPOSE: Supervises the existing optional DevTools installation, local SDK, one managed t VM and real guest capability probes.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   VersionProbeError: class responsible for versionprobeerror
#   _version: Require version output from a successful process, not a Wine warning.
#   _local: Handle local through this component's established interfaces.
#   _guest: Handle guest through this component's established interfaces.
#   _pacman_fields: Handle pacman fields through this component's established interfaces.
#   _staged_windows_versions: Verify the installed Windows version inventory without executing Wine.
#   _windows: Handle windows through this component's established interfaces.
#   _write_report: Write write report for this module.
#   ensure: Ensure ensure for this module.
#   _windows_unavailable: Handle windows unavailable through this component's established interfaces.
#   ...and 2 additional internal entry points; see their local documentation.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import os
import platform
import re
import shutil
import subprocess
import time

from host.windows.windows_environment import WindowsCrossEnvironment


# Each entry identifies an executable, its version arguments and its role.
# The Linux sample image and Windows cross image are already owned by Setup.
MAC_TOOLS = (
    ('Xcode', 'xcodebuild', ('-version',)),
    ('Apple Clang', 'clang', ('--version',)),
    ('Apple Clang++', 'clang++', ('--version',)),
    ('Swift', 'swift', ('--version',)),
    ('Micromamba', 'micromamba', ('--version',)),
    ('Git LFS', 'git-lfs', ('--version',)),
    ('Python', 'python3', ('--version',)),
    ('Node.js', 'node', ('--version',)),
    ('CMake', 'cmake', ('--version',)),
    ('Ninja', 'ninja', ('--version',)),
    ('pkg-config', 'pkg-config', ('--version',)),
    ('Colima', 'colima', ('--version',)),
    ('Docker CLI', 'docker', ('--version',)),
    ('Docker Buildx', 'docker-buildx', ('version',)),
    ('OCI registry downloader', 'skopeo', ('--version',)),
    ('7-Zip', '7zz', ('--help',)),
    ('Lima', 'limactl', ('--version',)),
    ('QEMU x86-64 system', 'qemu-system-x86_64', ('--version',)),
    ('QEMU disk image', 'qemu-img', ('--version',)),
    ('FFmpeg', 'ffmpeg', ('-version',)),
    ('FFprobe', 'ffprobe', ('-version',)),
    ('ImageMagick', 'magick', ('-version',)),
)
LINUX_TOOLS = (
    ('Assimp', 'assimp', ('version',)),
    ('Bash', 'bash', ('--version',)),
    ('GCC', 'gcc', ('--version',)),
    ('G++', 'g++', ('--version',)),
    ('CMake', 'cmake', ('--version',)),
    ('Ninja', 'ninja', ('--version',)),
    ('Python', 'python3', ('--version',)),
    ('Node.js', 'node', ('--version',)),
    ('pkg-config', 'pkg-config', ('--version',)),
    ('WebKitGTK', 'pkg-config', ('--modversion', 'webkit2gtk-4.1')),
    ('Xvfb', 'Xvfb', ('-help',)),
    ('FFmpeg', 'ffmpeg', ('-version',)),
    ('FFprobe', 'ffprobe', ('-version',)),
    ('ImageMagick', 'convert', ('-version',)),
)
CROSS_TOOLS = (
    ('MinGW GCC', 'x86_64-w64-mingw32-gcc', ('--version',)),
    ('MinGW G++', 'x86_64-w64-mingw32-g++', ('--version',)),
    ('MinGW objdump', 'x86_64-w64-mingw32-objdump', ('--version',)),
    ('Wine', 'wine', ('--version',)),
    ('CMake', 'cmake', ('--version',)),
    ('Ninja', 'ninja', ('--version',)),
    ('Python', 'python3', ('--version',)),
    ('Bash', 'bash', ('--version',)),
    ('Xvfb', 'Xvfb', ('-help',)),
    ('7-Zip', '7z', ('-h',)),
    ('cabextract', 'cabextract', ('--version',)),
)
# Native Windows tools are verified by executing the exact staged PE under
# native Windows or Wine. Wine failure is recorded as FAIL, never as a pass.
WINDOWS_TOOLS = (
    ('Bash', 'usr/bin/bash.exe', ('--version',)),
    ('Pacman', 'usr/bin/pacman.exe', ('--version',)),
    ('ls', 'usr/bin/ls.exe', ('--version',)),
    ('tar', 'usr/bin/tar.exe', ('--version',)),
    ('WinPTY', 'usr/bin/winpty.exe', ('--version',)),
    ('GCC', 'ucrt64/bin/gcc.exe', ('--version',)),
    ('G++', 'ucrt64/bin/g++.exe', ('--version',)),
    ('objdump', 'ucrt64/bin/objdump.exe', ('--version',)),
    ('CMake', 'ucrt64/bin/cmake.exe', ('--version',)),
    ('Ninja', 'ucrt64/bin/ninja.exe', ('--version',)),
    ('Python', 'ucrt64/bin/python.exe', ('--version',)),
    ('Node.js', 'ucrt64/bin/node.exe', ('--version',)),
    ('pkgconf', 'ucrt64/bin/pkgconf.exe', ('--version',)),
    ('Assimp', 'ucrt64/bin/assimp.exe', ('version',)),
    ('FFmpeg', 'ucrt64/bin/ffmpeg.exe', ('-version',)),
    ('FFprobe', 'ucrt64/bin/ffprobe.exe', ('-version',)),
    ('ImageMagick', 'ucrt64/bin/magick.exe', ('-version',)),
)


class VersionProbeError(RuntimeError):
    """Keep file evidence when a required tool cannot execute."""

    def __init__(self, message: str, *, path: Path, size: int):
        super().__init__(message)
        self.path = str(path)
        self.size = size


def _version(result: subprocess.CompletedProcess) -> str:
    """Require version output from a successful process, not a Wine warning."""
    output = result.stdout or ''
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        raise RuntimeError(f'exit={result.returncode}: ' + (output or '<no output>')[-350:])
    if any('cygwin warning:' in line.lower() for line in lines):
        raise RuntimeError('MSYS/Cygwin emitted a Wine compatibility warning; '
                           'cannot verify a native Windows tool version under Wine: '
                           + output[-350:])
    return lines[0][:180]


def _local(name: str, executable: Path | str, args: tuple[str, ...], *, xcode: bool = False) -> dict:
    path = Path(executable)
    if not path.is_file():
        found = shutil.which(str(executable))
        if not found:
            raise RuntimeError(f'executable missing: {executable}')
        path = Path(found)
    path = path.resolve()
    result = subprocess.run([str(path), *args], capture_output=True, text=True,
                            timeout=40, check=False)
    # stderr is used by some version commands (e.g. Xvfb).
    combined = subprocess.CompletedProcess(result.args, result.returncode,
                                          (result.stdout or '') + '\n' + (result.stderr or ''))
    version = _version(combined)
    size = None if xcode else path.stat().st_size
    if size is not None and size <= 0:
        raise RuntimeError('executable size missing: ' + str(path))
    return {'version': version, 'size': size, 'path': str(path), 'check': 'executed'}


def _guest(executor, name: str, binary: str, args: tuple[str, ...], *, cwd: Path) -> dict:
    # Print version and exact executable size in a single container invocation.
    script = 'set -e; p=$(command -v "$1"); test -f "$p"; stat -Lc %s "$p"; "$p" "${@:2}"'
    command = ['bash', '-c', script, 'bash', binary, *args]
    result = executor(command, cwd)
    lines = (result.stdout or '').splitlines()
    if result.returncode or not lines or not lines[0].strip().isdigit():
        raise RuntimeError(f'guest probe failed for {name}: rc={result.returncode} ' + (result.stdout or '')[-350:])
    size = int(lines[0].strip())
    if size <= 0:
        raise RuntimeError('guest executable size is zero: ' + binary)
    version = _version(subprocess.CompletedProcess(command, 0, '\n'.join(lines[1:])))
    return {'version': version, 'size': size, 'path': binary, 'check': 'executed-in-linux-amd64'}

# The MSYS base ships pacman's installed-package database. UCRT64 packages
# are verified against official SHA-256 digests by Windows Setup and recorded
# in closure.json. For a Mac/Linux host these are the authoritative installed
# Windows versions: stock Wine does not reliably execute MSYS/Cygwin tools.
WINDOWS_PACKAGE_OWNERS = {
    'usr/bin/bash.exe': 'bash',
    'usr/bin/pacman.exe': 'pacman',
    'usr/bin/ls.exe': 'coreutils',
    'usr/bin/tar.exe': 'tar',
    'usr/bin/winpty.exe': 'winpty',
    'ucrt64/bin/gcc.exe': 'mingw-w64-ucrt-x86_64-gcc',
    'ucrt64/bin/g++.exe': 'mingw-w64-ucrt-x86_64-gcc',
    'ucrt64/bin/objdump.exe': 'mingw-w64-ucrt-x86_64-binutils',
    'ucrt64/bin/cmake.exe': 'mingw-w64-ucrt-x86_64-cmake',
    'ucrt64/bin/ninja.exe': 'mingw-w64-ucrt-x86_64-ninja',
    'ucrt64/bin/python.exe': 'mingw-w64-ucrt-x86_64-python',
    'ucrt64/bin/node.exe': 'mingw-w64-ucrt-x86_64-nodejs',
    'ucrt64/bin/pkgconf.exe': 'mingw-w64-ucrt-x86_64-pkgconf',
    'ucrt64/bin/assimp.exe': 'mingw-w64-ucrt-x86_64-assimp',
    'ucrt64/bin/ffmpeg.exe': 'mingw-w64-ucrt-x86_64-ffmpeg',
    'ucrt64/bin/ffprobe.exe': 'mingw-w64-ucrt-x86_64-ffmpeg',
    'ucrt64/bin/magick.exe': 'mingw-w64-ucrt-x86_64-imagemagick',
}


def _pacman_fields(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    section = ''
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('%') and line.endswith('%'):
            section = line.strip('%')
        elif section and line.strip() and section not in fields:
            fields[section] = line.strip()
    return fields


def _staged_windows_versions(root: Path) -> dict[str, str]:
    """Verify the installed Windows version inventory without executing Wine.

    The upstream MSYS2 base already includes pacman's installed-package
    database. The separately downloaded UCRT64 packages have a strict
    SHA-256-verified closure receipt written by Windows Setup.
    """
    manifest = root / 'closure.json'
    base_receipt = root / 'base-staged.json'
    if not manifest.is_file() or not base_receipt.is_file():
        raise RuntimeError('MSYS2 verified native package closure missing')
    from host.windows.windows_msys2_cross import SCHEMA
    saved = json.loads(manifest.read_text(encoding='utf-8'))
    base = json.loads(base_receipt.read_text(encoding='utf-8'))
    if (saved.get('schema') != SCHEMA or not isinstance(saved.get('resolved'), dict) or
            not saved.get('base_sha256') or base.get('sha256') != saved['base_sha256']):
        raise RuntimeError('MSYS2 installed-package receipt does not match staged base')

    versions = {}
    local_db = root / 'var/lib/pacman/local'
    if not local_db.is_dir():
        raise RuntimeError('MSYS2 base local package database is missing')
    for desc in local_db.glob('*/desc'):
        fields = _pacman_fields(desc)
        if fields.get('NAME') and fields.get('VERSION'):
            versions[fields['NAME']] = fields['VERSION']
    for package, entry in saved['resolved'].items():
        if isinstance(entry, dict) and entry.get('version') and entry.get('repository') in ('UCRT64', 'MSYS'):
            versions[package] = entry['version']
    missing = sorted(set(WINDOWS_PACKAGE_OWNERS.values()) - versions.keys())
    if missing:
        raise RuntimeError('Verified installed MSYS2 package versions missing: ' + ', '.join(missing))
    return versions


def _windows(executor: WindowsCrossEnvironment | None, root: Path, rel: str,
             args: tuple[str, ...], *, package_versions: dict[str, str] | None = None) -> dict:
    path = root / rel
    if not path.is_file() or path.is_symlink():
        raise RuntimeError('Windows executable missing: ' + str(path))
    size = path.stat().st_size
    if size <= 0 or path.open('rb').read(2) != b'MZ':
        raise RuntimeError('Windows executable invalid or empty: ' + str(path))
    if platform.system() != 'Windows':
        if package_versions is None:
            package_versions = _staged_windows_versions(root)
        package = WINDOWS_PACKAGE_OWNERS[rel]
        version = package_versions.get(package)
        if not version:
            raise VersionProbeError('Verified installed Windows package version missing: ' + package,
                                    path=path, size=size)
        return {'version': version, 'size': size, 'path': str(path),
                'check': 'verified-installed-MSYS2-package; native-execution-not-tested'}

    command = [str(path), *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        combined = subprocess.CompletedProcess(command, result.returncode,
                                              (result.stdout or '') + '\n' + (result.stderr or ''))
        version = _version(combined)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise VersionProbeError(str(exc), path=path, size=size) from exc
    return {'version': version, 'size': size, 'path': str(path),
            'check': 'executed-on-native-windows'}


# Retro68's publisher distributes a ready-to-run toolchain OCI image. Freeze
# the registry digest and the offline archive in Setup's Builds rather than
# compiling GCC/binutils or requiring a second Docker daemon. `latest` is
# resolved only on the first acquisition: subsequent Setups reuse the sealed
# image, and Build/Stage/Publish never query the registry.
RETRO68_SOURCE = 'ghcr.io/autc04/retro68:latest'
RETRO68_IMAGE = 'cinterforge-retro68:setup'
RETRO68_SCHEMA = 'cinterforge.terminal.retro68-oci.v1'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _retro68_probe(builds: Path, docker: str, *, provision_only: bool = False) -> dict:
    """Setup-only installation and executable proof for both classic Mac ABIs.

    The *prebuilt* AMD64 OCI image is an offline DevTools member. It runs on the
    existing owned t Docker daemon; there is no container at Terminal runtime.
    """
    if platform.system() != 'Darwin':
        raise RuntimeError('Retro68 OCI acquisition belongs to macOS Setup')
    root = builds / 'Toolchains/Retro68'
    archive = root / 'retro68-amd64.tar'
    receipt = root / 'retro68-image.json'
    if root.is_symlink() or archive.is_symlink() or receipt.is_symlink():
        raise RuntimeError('Unsafe symlink in Retro68 DevTools stage')
    root.mkdir(parents=True, exist_ok=True)
    saved = None
    if receipt.exists():
        saved = json.loads(receipt.read_text(encoding='utf-8'))
        if (not isinstance(saved, dict) or saved.get('schema') != RETRO68_SCHEMA or
                saved.get('source') != RETRO68_SOURCE or saved.get('image') != RETRO68_IMAGE or
                saved.get('platform') != 'linux/amd64' or
                not re.fullmatch(r'sha256:[0-9a-f]{64}', saved.get('registry_digest', '')) or
                not re.fullmatch(r'[0-9a-f]{64}', saved.get('archive_sha256', ''))):
            raise RuntimeError('Invalid sealed Retro68 DevTools receipt: ' + str(receipt))
        if not archive.is_file() or not archive.stat().st_size:
            raise RuntimeError('Sealed Retro68 archive missing: ' + str(archive))
        if _sha256(archive) != saved['archive_sha256']:
            raise RuntimeError('Retro68 DevTools archive SHA-256 mismatch: ' + str(archive))
        print('[RETRO68 SETUP] Reusing sealed prebuilt 68K/PPC image: ' +
              saved['registry_digest'], flush=True)
    elif archive.exists():
        raise RuntimeError('Unverified Retro68 archive exists without a receipt: ' + str(archive))
    else:
        skopeo = builds / 'Toolchains/mac/env/bin/skopeo'
        if not skopeo.is_file() or skopeo.is_symlink():
            raise RuntimeError('Setup-managed native macOS skopeo is required for Retro68')
        registry_config = builds / 'Intermediates/CinterForgeTerminal/RegistryInputs/registries-v2.conf'
        registry_config.parent.mkdir(parents=True, exist_ok=True)
        if registry_config.is_symlink():
            raise RuntimeError('Unsafe Retro68 registry configuration link')
        registry_config.write_text('unqualified-search-registries = []\n', encoding='utf-8')
        base = [str(skopeo), '--registries-conf', str(registry_config),
                '--override-os', 'linux', '--override-arch', 'amd64']
        print('[RETRO68 SETUP] Resolving immutable registry digest for ' + RETRO68_SOURCE,
              flush=True)
        version = subprocess.run([*base, 'inspect', '--format', '{{.Digest}}',
                                  'docker://' + RETRO68_SOURCE], capture_output=True,
                                 text=True, timeout=180, check=False)
        digest = version.stdout.strip()
        if version.returncode or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise RuntimeError('Cannot resolve Retro68 image digest: ' +
                               (version.stderr or version.stdout)[-1200:])
        temp = root / 'retro68-amd64.tar.part'
        if temp.exists() or temp.is_symlink():
            raise RuntimeError('Existing partial Retro68 archive; remove only that incomplete '
                               'Builds/Toolchains/Retro68/*.part file before retrying')
        print('[RETRO68 SETUP] Downloading prebuilt Linux/amd64 toolchain ' + digest,
              flush=True)
        try:
            # Native macOS HTTPS avoids QEMU TLS failures. The OCI image
            # remains offline and relocatable as a DevTools ZIP member.
            subprocess.run([*base, 'copy', 'docker://' + RETRO68_SOURCE.split(':latest')[0] +
                            '@' + digest, 'docker-archive:' + str(temp) + ':' + RETRO68_IMAGE],
                           timeout=3600, check=True)
            if not temp.is_file() or not temp.stat().st_size:
                raise RuntimeError('Downloaded Retro68 OCI archive is empty')
            checksum = _sha256(temp)
            temp.replace(archive)
            saved = {'schema': RETRO68_SCHEMA, 'source': RETRO68_SOURCE,
                     'registry_digest': digest, 'image': RETRO68_IMAGE,
                     'platform': 'linux/amd64', 'archive_sha256': checksum,
                     'archive_bytes': archive.stat().st_size}
            temporary_receipt = root / 'retro68-image.json.part'
            temporary_receipt.write_text(json.dumps(saved, indent=2, sort_keys=True) + '\n',
                                         encoding='utf-8')
            temporary_receipt.replace(receipt)
        finally:
            temp.unlink(missing_ok=True)
    if provision_only:
        return saved
    if not docker:
        raise RuntimeError('Retro68 acceptance requires the existing managed t Docker daemon')
    # Always load from the sealed archive: a reused daemon might contain a
    # different image under the same tag, so tag presence cannot prove identity.
    print('[RETRO68 SETUP] Loading verified offline image into the existing t guest',
          flush=True)
    subprocess.run([docker, 'load', '--input', str(archive)], timeout=1800, check=True)
    image_arch = subprocess.run([docker, 'image', 'inspect', '--format',
                                 '{{.Architecture}}', RETRO68_IMAGE], capture_output=True,
                                text=True, timeout=30, check=False)
    if image_arch.returncode or image_arch.stdout.strip() != 'amd64':
        raise RuntimeError('Loaded Retro68 image must be Linux/amd64: ' +
                           (image_arch.stdout or image_arch.stderr)[-500:])
    script = r"""set -eu
BIN=/Retro68-build/toolchain/bin
for compiler in m68k-apple-macos-gcc powerpc-apple-macos-gcc; do
    test -x "$BIN/$compiler"
    printf 'int main(void) { return 0; }\n' > /tmp/retro68-probe.c
    "$BIN/$compiler" -c /tmp/retro68-probe.c -o "/tmp/$compiler.o"
    test -s "/tmp/$compiler.o"
done
test -x "$BIN/Rez"
"$BIN/m68k-apple-macos-gcc" --version | head -n 1
stat -Lc %s "$BIN/m68k-apple-macos-gcc"
"""
    probe = subprocess.run([docker, 'run', '--rm', '--platform', 'linux/amd64',
                            '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,exec',
                            RETRO68_IMAGE, 'bash', '-c', script],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=240, check=False)
    lines = (probe.stdout or '').strip().splitlines()
    if probe.returncode or len(lines) < 2 or not lines[-1].isdigit():
        raise RuntimeError('Retro68 68K/PPC compiler smoke failed: ' +
                           (probe.stdout or '<no output>')[-2000:])
    print('[RETRO68 SETUP] PASS: 68K and PowerPC GCC compile, Rez is installed; '
          'image=' + saved['registry_digest'], flush=True)
    return {'version': lines[-2][:180], 'size': int(lines[-1]),
            'path': str(archive), 'check': '68K/PPC object compilation in offline OCI',
            'registry_digest': saved['registry_digest'],
            'archive_bytes': archive.stat().st_size}


def _write_report(destination: Path, rows: list[dict]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '# CinterForgeTerminal DevTooling', '',
        'Generated by `./Cinter test devtools`. Mac/Linux versions are executed; Windows versions',
        'on non-Windows hosts come from the verified, installed MSYS2 package records.',
        'Native Windows execution remains a separate validation gate.',
        'Sizes are **installed executable bytes**, not SDK or package download sizes.',
        '**Xcode is exempt from size measurement only; its version must still execute successfully.**', '',
        '| Platform | Tool | Version | Executable size | Location | Result | Version evidence |',
        '|---|---|---|---:|---|---|---|',
    ]
    for row in rows:
        def safe(value) -> str:
            return str(value).replace('|', '\\|').replace('\n', ' ')[:260]
        size = ('Exempt (Xcode)' if row.get('xcode') else
                f'{row["size"]:,} bytes' if isinstance(row.get('size'), int) else 'FAILED')
        lines.append('| ' + ' | '.join(map(safe, (
            row['platform'], row['name'], row.get('version', 'FAILED'), size,
            row.get('path', 'missing'), row['status'], row.get('check', 'not verified')))) + ' |')
    failures = [row for row in rows if row['status'] != 'PASS']
    lines.extend(['', f'**Result: {"FAIL" if failures else "PASS"}** — '
                   f'{len(rows)-len(failures)}/{len(rows)} version and size checks passed.', ''])
    destination.write_text('\n'.join(lines), encoding='utf-8')


def ensure(builds: Path) -> Path:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('DevTooling report requires sibling Builds directory')
    destination = builds / 'Logs/CinterForgeTerminal/DevTooling.md'
    rows: list[dict] = []

    def check(system: str, name: str, probe, *, xcode: bool = False):
        row = {'platform': system, 'name': name, 'status': 'FAIL', 'xcode': xcode}
        start = time.monotonic()
        print(f'[DEVTOOLING] checking {system} / {name} ...', flush=True)
        try:
            row.update(probe())
            if not row.get('version') or (not xcode and not isinstance(row.get('size'), int)):
                raise RuntimeError('version or executable size missing')
            row['status'] = 'PASS'
            size = 'Xcode size exempt' if xcode else f'{row["size"]} bytes'
            print(f'[DEVTOOLING] PASS {system} / {name} ({time.monotonic()-start:.1f}s): '
                  f'{row["version"]} [{size}]', flush=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, VersionProbeError):
                row.update(path=exc.path, size=exc.size)
            row['version'] = 'FAILED: ' + str(exc)[:450]
            print(f'[DEVTOOLING] FAIL {system} / {name} ({time.monotonic()-start:.1f}s): '
                  f'{exc}', flush=True)
        rows.append(row)
        _write_report(destination, rows)  # preserve evidence if later tools fail

    host = platform.system()
    if host == 'Darwin':
        from host.devtools.assimp_library import prebuilt_assimp
        prefix = builds / 'Toolchains/mac'
        check('macOS arm64', 'Assimp library',
              lambda: prebuilt_assimp(prefix / 'env'))
        binaries = prefix / 'env/bin'
        for name, executable, args in MAC_TOOLS:
            if name == 'Xcode':
                path = 'xcodebuild'
            elif name in ('Apple Clang', 'Apple Clang++', 'Swift'):
                path = shutil.which(executable) or executable
            elif name == 'Micromamba':
                path = prefix / 'mamba/bin/micromamba'
            elif name == 'Lima':
                path = prefix / 'lima/bin/limactl'
            elif name.startswith('QEMU '):
                path = prefix / 'qemu/bin' / executable
            elif name == '7-Zip':
                path = next((binaries / n for n in ('7zz', '7z') if (binaries / n).is_file()), binaries / '7zz')
            else:
                path = binaries / executable
            check('macOS arm64', name, lambda p=path, a=args, n=name: _local(n, p, a, xcode=n == 'Xcode'),
                  xcode=name == 'Xcode')

    cross = None
    cross_error = None
    if host in ('Darwin', 'Linux'):
        try:
            cross = WindowsCrossEnvironment(builds).ensure()
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            cross_error = str(exc)
        for name, binary, args in CROSS_TOOLS:
            def cross_probe(b=binary, a=args, n=name):
                if cross is None:
                    raise RuntimeError('cross-build environment unavailable: ' + str(cross_error))
                return _guest(lambda command, cwd: cross.execute(command, cwd=cwd, timeout=90),
                              n, b, a, cwd=builds)
            check('Linux amd64 (Windows cross)', name, cross_probe)
        if host == 'Darwin':
            check('Classic Macintosh (68K/PPC)', 'Retro68 prebuilt GCC + Rez',
                  lambda: _retro68_probe(builds, cross.docker if cross is not None else ''))
        # The native Linux WebView build uses its own retained Linux image.
        print('[DEVTOOLING] verifying Linux WebKitGTK build environment ...', flush=True)
        from host.lifecycle.three_samples import _docker_image
        docker = cross.docker if cross is not None else None
        image = None
        try:
            if not docker:
                raise RuntimeError('AMD64 guest Docker not available for Linux WebKitGTK checks')
            image = _docker_image(builds, str(docker), builds / 'Logs/CinterForgeTerminal/DevTooling-linux-image.log')
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f'[DEVTOOLING] Linux image unavailable: {exc}', flush=True)
        # This is Setup, not runtime. Verify the actual Linux GUI endpoint
        # before recording a usable Linux environment; no cached CLI version
        # string can substitute for a real X11 + VNC + noVNC connection.
        if image:
            from host.lifecycle.gui_preflight import verify_guest_gui
            verify_guest_gui(builds, guest='linux', docker=str(docker), image=image)
        for name, binary, args in LINUX_TOOLS:
            def linux_probe(b=binary, a=args, n=name):
                if not image:
                    raise RuntimeError('Linux native build image unavailable')
                def execute(command, cwd):
                    return subprocess.run([str(docker), 'run', '--rm', '--platform', 'linux/amd64',
                        '--network', 'none', image, *command], cwd=cwd, capture_output=True,
                        text=True, timeout=90, check=False)
                return _guest(lambda command, cwd: _merge_output(execute(command, cwd)), n, b, a, cwd=builds)
            check('Linux amd64 (native WebKitGTK)', name, linux_probe)

    root = builds / 'Toolchains/Windows/MSYS2'
    if host in ('Darwin', 'Linux', 'Windows'):
        windows_versions = None
        windows_error = None
        if host != 'Windows':
            try:
                windows_versions = _staged_windows_versions(root)
            except (OSError, RuntimeError, ValueError) as exc:
                windows_error = str(exc)
        for name, rel, args in WINDOWS_TOOLS:
            def windows_probe(r=rel, a=args):
                if windows_error:
                    raise RuntimeError(windows_error)
                return _windows(cross, root, r, a, package_versions=windows_versions)
            check('Windows x64 (native UCRT64)', name, windows_probe)

    _write_report(destination, rows)
    failures = [r['name'] + ' (' + r['platform'] + ')' for r in rows if r['status'] == 'FAIL']
    print('[DEVTOOLING] report: ' + str(destination), flush=True)
    if failures:
        raise RuntimeError('DevTooling version/size verification failed: ' + ', '.join(failures))
    return destination


def _windows_unavailable(reason: str | None):
    raise RuntimeError('Windows executor unavailable: ' + str(reason))


def _merge_output(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(result.args, result.returncode,
                                       (result.stdout or '') + '\n' + (result.stderr or ''))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    ensure(parser.parse_args().builds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


def smoke_media_toolchain(builds: Path) -> dict[str, str]:
    """Test-phase-only executable acceptance: tiny real OBJ and PNG conversions.

    Uses the three native platform toolchains already provisioned by Setup.
    No package acquisition, alternate containers, or prebuilt-file substitution.
    """
    import tempfile
    builds = Path(builds).resolve()
    test_root = builds / 'Intermediates/CinterForgeTerminal/MediaSmoke'
    test_root.mkdir(parents=True, exist_ok=True)
    def check(command, *, cwd=test_root, env=None, timeout=120):
        print('[MEDIA TEST] ' + ' '.join(str(v) for v in command), flush=True)
        result = subprocess.run([str(v) for v in command], cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout, check=False)
        if result.returncode:
            raise RuntimeError(f'Media smoke rc={result.returncode}: ' + result.stdout[-1700:])
        return result

    obj = test_root / 'triangle.obj'
    obj.write_text('o Triangle\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n', encoding='utf-8')
    mac_bin = builds / 'Toolchains/mac/env/bin'
    from host.devtools.assimp_library import prebuilt_assimp
    assimp = prebuilt_assimp(builds / 'Toolchains/mac/env')
    for tool in ('ffmpeg', 'ffprobe', 'magick'):
        if not (mac_bin / tool).is_file():
            raise RuntimeError('Missing Setup-provisioned macOS DevTools binary: ' + str(mac_bin / tool))
    # Compile a tiny C API client against the *prebuilt* Conda Assimp dylib.
    # This is a Test-only action, not an upstream Assimp source build.
    prefix = mac_bin.parent
    smoke_source = Path(__file__).with_name('assimp_import_smoke.c')
    smoke_binary = test_root / 'assimp-import-mac'
    check(['xcrun', 'clang', '-std=c11', '-I' + str(prefix / 'include'),
           smoke_source, str(assimp['path']), '-Wl,-rpath,' + str(prefix / 'lib'),
           '-o', smoke_binary])
    check([smoke_binary, obj])
    mac_image = test_root / 'mac.png'
    check([mac_bin / 'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
           '-i', 'color=c=red:s=16x16:d=0.2', '-frames:v', '1', '-threads', '1', '-y', mac_image])
    check([mac_bin / 'ffprobe', '-v', 'error', mac_image])
    resized = test_root / 'mac-resized.png'
    check([mac_bin / 'magick', mac_image, '-resize', '8x8!', resized])
    if not resized.is_file() or resized.stat().st_size <= 0:
        raise RuntimeError('ImageMagick did not produce resized PNG')
    result = {'mac': 'PASS'}

    from host.windows.windows_environment import WindowsCrossEnvironment
    # Caller owns the managed t guest lifetime and Test-only locks.
    cross = WindowsCrossEnvironment(builds).ensure()
    if not cross.image or not cross.docker:
        raise RuntimeError('Three-platform media acceptance needs Setup-owned amd64 guest image')
    guest_obj = cross.path(obj)
    for cmd in (["assimp", "info", guest_obj],
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=blue:s=16x16:d=0.2", "-frames:v", "1", "-threads", "1", "-y",
                 cross.path(test_root / 'linux.png')],
                ["ffprobe", "-v", "error", cross.path(test_root / 'linux.png')],
                ["convert", cross.path(test_root / 'linux.png'), "-resize", "8x8!",
                 cross.path(test_root / 'linux-resized.png')]):
        rc = cross.execute(cmd, cwd=test_root, timeout=120)
        if rc.returncode:
            raise RuntimeError('Linux media smoke failed: ' + (rc.stdout or '')[-1400:])
    if not (test_root / 'linux-resized.png').is_file():
        raise RuntimeError('Linux ImageMagick output missing')
    result['linux'] = 'PASS'

    # Wine maps /builds to Z:\\builds, and the installed Windows UCRT64 tools
    # require their original DLL closure in WINEPATH.
    root = builds / 'Toolchains/Windows/MSYS2/ucrt64/bin'
    for tool in ('assimp.exe', 'ffmpeg.exe', 'ffprobe.exe', 'magick.exe'):
        file = root / tool
        if file.is_symlink() or not file.is_file() or file.read_bytes()[:2] != b'MZ':
            raise RuntimeError('Missing native Windows media executable: ' + str(file))
    def wine_path(path):
        return 'Z:' + cross.path(Path(path)).replace('/', chr(92))
    prefix = 'WINEPATH=' + wine_path(root)
    def win(exe, args):
        cmd = ['env', prefix, 'wine', cross.path(root / exe), *args]
        result = cross.execute(cmd, cwd=test_root, timeout=180)
        if result.returncode:
            raise RuntimeError('Windows Wine media smoke failed: ' + (result.stdout or '')[-1600:])
    win('assimp.exe', ['info', wine_path(obj)])
    image = test_root / 'win.png'
    win('ffmpeg.exe', ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                       'color=c=green:s=16x16:d=0.2', '-frames:v', '1', '-threads', '1', '-y', wine_path(image)])
    win('ffprobe.exe', ['-v', 'error', wine_path(image)])
    converted = test_root / 'win-resized.png'
    win('magick.exe', [wine_path(image), '-resize', '8x8!', wine_path(converted)])
    if not converted.is_file() or converted.stat().st_size <= 0:
        raise RuntimeError('Windows native ImageMagick did not produce PNG under Wine')
    result['win'] = 'PASS'
    return result
