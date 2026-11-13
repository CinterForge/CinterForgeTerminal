"""Version and installed-binary-size inventory for Terminal developer tools.

The report contains executed version probes, not versions inferred from installer
names. A missing tool, failed version command, or unknown binary size fails Setup.
Xcode is the sole size exception because its SDK installation footprint varies.
All evidence and generated content live in the sibling Builds directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import platform
import shutil
import subprocess
import time

from host.windows_environment import WindowsCrossEnvironment


# Each entry identifies an executable, its version arguments and its role.
# The Linux sample image and Windows cross image are already owned by Setup.
MAC_TOOLS = (
    ('Xcode', 'xcodebuild', ('-version',)),
    ('Apple Clang', 'clang', ('--version',)),
    ('Apple Clang++', 'clang++', ('--version',)),
    ('Swift', 'swift', ('--version',)),
    ('Micromamba', 'micromamba', ('--version',)),
    ('Python', 'python3', ('--version',)),
    ('Node.js', 'node', ('--version',)),
    ('CMake', 'cmake', ('--version',)),
    ('Ninja', 'ninja', ('--version',)),
    ('pkg-config', 'pkg-config', ('--version',)),
    ('Colima', 'colima', ('--version',)),
    ('Docker CLI', 'docker', ('--version',)),
    ('Docker Buildx', 'docker-buildx', ('version',)),
    ('7-Zip', '7zz', ('--help',)),
    ('Lima', 'limactl', ('--version',)),
)
LINUX_TOOLS = (
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
    from host.windows_msys2_cross import SCHEMA
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


def _write_report(destination: Path, rows: list[dict]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '# CinterForgeTerminal DevTooling', '',
        'Generated by `./Cinter setup all`. Mac/Linux versions are executed; Windows versions',
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
        prefix = builds / 'CinterForgeTerminal/Toolchains/mac'
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
        # The native Linux WebView build uses its own retained Linux image.
        print('[DEVTOOLING] verifying Linux WebKitGTK build environment ...', flush=True)
        from host.three_samples import _docker_image
        from host.mac_bootstrap import ensure_docker
        docker = cross.docker or (ensure_docker(builds) if host == 'Darwin' else shutil.which('docker'))
        image = None
        try:
            image = _docker_image(builds, str(docker), builds / 'Logs/CinterForgeTerminal/DevTooling-linux-image.log')
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f'[DEVTOOLING] Linux image unavailable: {exc}', flush=True)
        # This is Setup, not runtime. Verify the actual Linux GUI endpoint
        # before recording a usable Linux environment; no cached CLI version
        # string can substitute for a real X11 + VNC + noVNC connection.
        if image:
            from host.gui_preflight import verify_guest_gui
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

    root = builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
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
