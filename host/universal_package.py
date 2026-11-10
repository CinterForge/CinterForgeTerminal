"""Build and Steam-stage the four Terminal distribution ZIPs.

Three native app bundles (macOS, Linux, Windows) and one shared optional
DevTools bundle are independently checksummed. The historical universal ZIP
reader remains for older archives, but is not the active release lifecycle.
Bedrock server bytes are never included before app-side EULA acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import tempfile
import time
import zipfile

from host import HOST_RELEASE
from host.bootstrap_bundle import verify_bundle

SCHEMA = "cinterforge.terminal.universal-package.v1"
TARGETS = ("mac", "linux", "win")
TARGET_ARCH = {"mac": "arm64", "linux": "x64", "win": "x64"}


def _hash(path: Path, *, progress_label: str = '') -> str:
    h = hashlib.sha256()
    done = 0
    total = path.stat().st_size if progress_label else 0
    last_report = time.monotonic()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
            if progress_label:
                done += len(chunk)
                now = time.monotonic()
                if now - last_report >= 2:
                    _progress(progress_label, done, total)
                    last_report = now
    if progress_label:
        _progress(progress_label + ' complete', done, total)
    return h.hexdigest()


def _check_regular(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Invalid package source: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Package cannot contain symlinks: {path}")


def _native_binary(bundle: Path, target: str) -> Path:
    suffix = ".exe" if target == "win" else ""
    path = bundle / "Host" / "bin" / ("CinterForgeTerminal" + suffix)
    if not path.is_file():
        raise RuntimeError(f"Missing staged native {target} launcher: {path}")
    with path.open("rb") as stream:
        header = stream.read(4)
    if target == "win" and header[:2] != b"MZ":
        raise RuntimeError(f"Windows launcher is not PE: {path}")
    if target == "linux" and header != b"\x7fELF":
        raise RuntimeError(f"Linux launcher is not ELF: {path}")
    if target == "mac" and header not in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xbe\xba\xfe\xca"):
        raise RuntimeError(f"Mac launcher is not Mach-O: {path}")
    return path


def _contents_launcher() -> str:
    return """#!/bin/sh
set -eu
APP=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
PAYLOAD="$APP/Contents/Resources/Builds/mac"
export CINTER_TERMINAL_PACKAGE_ROOT="$APP"
exec "$PAYLOAD/Host/bin/CinterForgeTerminal" --builds "$APP/Contents/Resources/Builds" --python "$PAYLOAD/PythonRuntime/bin/python" --worker "$PAYLOAD/Host/python/worker.py" --service "$PAYLOAD/Host/webapp/CinterForgeTerminal.py" "$@"
"""


def _linux_launcher() -> str:
    return """#!/bin/sh
set -eu
APP=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
export CINTER_TERMINAL_PACKAGE_ROOT="$APP"
PAYLOAD="$APP/Contents/Resources/Builds/linux"
exec "$PAYLOAD/Host/bin/CinterForgeTerminal" --builds "$APP/Contents/Resources/Builds" --python "$PAYLOAD/PythonRuntime/bin/python" --worker "$PAYLOAD/Host/python/worker.py" --service "$PAYLOAD/Host/webapp/CinterForgeTerminal.py" "$@"
"""


def _windows_launcher() -> str:
    return r"""@echo off
setlocal EnableExtensions
set "CINTER_TERMINAL_PACKAGE_ROOT=%~dp0"
set "CINTER_WEBVIEW2_FIXED=%~dp0Contents\Resources\Builds\win\WebView2FixedRuntime"
if not exist "%CINTER_WEBVIEW2_FIXED%\msedgewebview2.exe" (
  >&2 echo [CinterForgeTerminal] Bundled Microsoft WebView2 Fixed Runtime is missing.
  exit /b 1
)
set "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER=%CINTER_WEBVIEW2_FIXED%"
"%~dp0Contents\Resources\Builds\win\Host\bin\CinterForgeTerminal.exe" --builds "%~dp0Contents\Resources\Builds" --python "%~dp0Contents\Resources\Builds\win\PythonRuntime\python.exe" --worker "%~dp0Contents\Resources\Builds\win\Host\python\worker.py" --service "%~dp0Contents\Resources\Builds\win\Host\examples\shell\CinterForgeTerminal.py" %*
exit /b %ERRORLEVEL%
"""


def _write_launcher(path: Path, text: str, executable: bool = False) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)


def _manifest_files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _hash(p) for p in sorted(root.rglob("*")) if p.is_file()}


def assemble(builds: Path, *, release: str = HOST_RELEASE, arches: dict[str, str] | None = None,
             output: Path | None = None) -> Path:
    builds = builds.resolve()
    if builds.name != "Builds" or not builds.is_dir():
        raise RuntimeError("Universal packaging requires existing sibling Builds")
    arches = dict(TARGET_ARCH if arches is None else arches)
    if set(arches) != set(TARGETS):
        raise RuntimeError("One verified Mac, Linux and Windows bundle is mandatory")
    packages = {}
    for target in TARGETS:
        candidate = builds / "CinterForgeTerminal/Bundles" / target / arches[target] / release
        if not candidate.is_dir():
            raise RuntimeError(f"Cannot package unaccepted {target}: {candidate}")
        data = verify_bundle(candidate)
        if data.get("platform") != target or data.get("arch") != arches[target] or data.get("release") != release:
            raise RuntimeError(f"Incorrect accepted target bundle: {candidate}")
        if data.get("relocated_runtime_smoke", {}).get("execution") != "native-relocated":
            raise RuntimeError(f"Missing native relocated acceptance for {target}: {candidate}")
        _check_regular(candidate)
        _native_binary(candidate, target)
        packages[target] = candidate

    destination = (output or builds / "CinterForgeTerminal/Distribution/CinterForgeTerminal.zip").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="universal-", dir=destination.parent) as temp:
        app = Path(temp) / "CinterForgeTerminal.app"
        resources = app / "Contents/Resources/Builds"
        resources.mkdir(parents=True)
        for target in TARGETS:
            shutil.copytree(packages[target], resources / target, symlinks=False)
        from host.windows_fixed_runtime import verify as verify_fixed
        fixed = resources / 'win/WebView2FixedRuntime/msedgewebview2.exe'
        if not fixed.is_file() or fixed.open('rb').read(2) != b'MZ':
            raise RuntimeError('Universal Windows payload lacks the complete Fixed Version WebView2 browser')
        # The source under Builds is verified byte-for-byte before packaging.
        source_browser = verify_fixed(builds) / 'msedgewebview2.exe'
        if _hash(fixed) != _hash(source_browser):
            raise RuntimeError('Packaged Windows WebView2 browser differs from Setup closure')
        macos = app / "Contents/MacOS"
        macos.mkdir(parents=True)
        _write_launcher(macos / "CinterForgeTerminal", _contents_launcher(), executable=True)
        plist = {"CFBundleName": "CinterForgeTerminal", "CFBundleDisplayName": "CinterForgeTerminal",
                 "CFBundleIdentifier": "com.cinterforge.terminal", "CFBundleExecutable": "CinterForgeTerminal",
                 "CFBundlePackageType": "APPL", "CFBundleVersion": release,
                 "CFBundleShortVersionString": release, "LSMinimumSystemVersion": "12.0"}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(plist))
        _write_launcher(app / "Terminal.sh", _linux_launcher(), executable=True)
        _write_launcher(app / "Terminal.cmd", _windows_launcher())
        files = _manifest_files(app)
        receipt = {"schema": SCHEMA, "release": release, "arches": arches, "files": files,
                   "targets": {k: "native-relocated" for k in TARGETS},
                   "signing": "unsigned-universal-layout; Apple signed app validation required separately"}
        (resources / "universal-manifest.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        partial = destination.with_name(destination.name + ".part")
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                                 allowZip64=True) as z:
                for path in sorted(app.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(Path(temp)).as_posix()
                    info = zipfile.ZipInfo(rel)
                    info.create_system = 3
                    info.external_attr = ((stat.S_IFREG | (0o755 if os.access(path, os.X_OK) else 0o644)) << 16)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with path.open("rb") as stream, z.open(info, "w", force_zip64=True) as sink:
                        shutil.copyfileobj(stream, sink)
            with zipfile.ZipFile(partial) as z:
                if z.testzip() is not None:
                    raise RuntimeError("Universal ZIP integrity failure")
                actual = set(z.namelist())
                required = {"CinterForgeTerminal.app/Contents/Info.plist", "CinterForgeTerminal.app/Contents/MacOS/CinterForgeTerminal",
                            "CinterForgeTerminal.app/Terminal.cmd", "CinterForgeTerminal.app/Terminal.sh"}
                if not required <= actual:
                    raise RuntimeError("Universal ZIP missing platform launchers")
            verify_universal_zip(partial)
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
    return destination



def verify_universal_zip(archive_path: Path) -> dict:
    """Read back the actual ZIP payload and verify every declared byte.

    Tests are not satisfied by a staging-tree manifest alone: ZIP extraction
    must preserve an unmodified three-platform app, with no hidden extra files.
    """
    prefix = "CinterForgeTerminal.app/"
    manifest_name = prefix + "Contents/Resources/Builds/universal-manifest.json"
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        if len(names) != len(set(names)):
            raise RuntimeError("Universal ZIP has duplicate entries")
        if manifest_name not in names or any(
            not n.startswith(prefix) or n.startswith("/") or ".." in Path(n).parts or "\\" in n
            for n in names
        ):
            raise RuntimeError("Universal ZIP has an unexpected or unsafe path")
        receipt = json.loads(archive.read(manifest_name))
        if receipt.get("schema") != SCHEMA or set(receipt.get("targets", {})) != set(TARGETS):
            raise RuntimeError("Universal ZIP manifest is incomplete")
        checksums = receipt.get("files")
        if not isinstance(checksums, dict) or not checksums:
            raise RuntimeError("Universal ZIP file checksums missing")
        expected = {prefix + key: digest for key, digest in checksums.items()}
        if set(names) != set(expected) | {manifest_name}:
            raise RuntimeError("Universal ZIP contains missing or untracked files")
        for name, expected_hash in expected.items():
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            if digest != expected_hash:
                raise RuntimeError(f"Universal ZIP file checksum mismatch: {name}")
        required = {prefix + "Contents/Info.plist",
                    prefix + "Contents/MacOS/CinterForgeTerminal",
                    prefix + "Terminal.cmd", prefix + "Terminal.sh"}
        if not required <= set(names):
            raise RuntimeError("Universal ZIP missing platform launchers")
        if archive.testzip() is not None:
            raise RuntimeError("Universal ZIP CRC validation failed")
    return receipt


# Current distribution contract (November 8): THREE independently launchable
# native applications plus ONE optional unified developer toolchain. Keep the
# older universal ZIP verifier above only for reading historical distributions.
# No Bedrock executable or EULA receipt is a build/stage prerequisite.
FOUR_SCHEMA = 'cinterforge.terminal.four-artifact-release.v1'
# Printed during Stage to prove the running checkout includes interrupted-install recovery.
MAC_STAGE_RECOVERY_PROTOCOL = 'restart-safe-v2'
FOUR_TARGETS = ('mac', 'linux', 'win', 'devtools')
FOUR_ARCH = {'mac': 'arm64', 'linux': 'x64', 'win': 'x64'}
STEAM_PLATFORMS = {'mac': 'macOS', 'linux': 'Linux', 'win': 'Windows', 'devtools': 'DevTools'}
APP_ROOTS = {'mac': 'CinterForge Terminal.app', 'linux': 'CinterForge Terminal',
             'win': 'CinterForge Terminal', 'devtools': 'DevTools'}

# These are runtime application files, not optional DevTools components.
WINDOWS_SHELL_PREFIX = 'CinterForge Terminal/Builds/CinterForgeTerminal/Runtime/MSYS2/usr/bin/'
WINDOWS_SHELL_REQUIRED = ('bash.exe', 'cygpath.exe', 'msys-2.0.dll')


def _require_windows_shell(files: dict[str, Path | bytes] | dict[str, dict], *,
                           zip_archive: zipfile.ZipFile | None = None) -> None:
    """Reject a Windows release without its actual packaged Bash closure."""
    missing = [name for name in WINDOWS_SHELL_REQUIRED
               if WINDOWS_SHELL_PREFIX + name not in files]
    if missing:
        raise RuntimeError('Windows release missing packaged MSYS2 Bash: ' + ', '.join(missing))
    for name in WINDOWS_SHELL_REQUIRED:
        key = WINDOWS_SHELL_PREFIX + name
        if zip_archive is not None:
            with zip_archive.open(key) as handle:
                magic = handle.read(2)
        else:
            value = files[key]
            if isinstance(value, bytes):
                magic = value[:2]
            else:
                with value.open('rb') as handle:
                    magic = handle.read(2)
        if magic != b'MZ':
            raise RuntimeError('Windows release MSYS2 executable/DLL is not PE: ' + key)


def _progress(label: str, done: int, total: int) -> None:
    percent = (100 * done / total) if total else 100.0
    print(f'[TERMINAL PROGRESS] {label} {percent:.1f}% '
          f'({done / 1048576:.1f}/{total / 1048576:.1f} MiB)', flush=True)

HOST_ASSETS = ('CinterForgeTerminal.py', 'if_titles.json', 'mac_games.json', 'index.html',
               'terminal_styles.json', 'terminal.js', 'terminal.css')


def _files_under(source: Path, prefix: str, *, require: bool = True) -> dict[str, Path]:
    """Flatten only in-tree symlinks into regular release files.

    Managed toolchains may use aliases to internal *directories* (for example
    mac/env/lib/icu/current). The ZIP is deliberately symlink-free: traverse
    those directories under the alias name, but reject escape paths, dangling
    links, special files, and directory cycles. Never follow the host's
    filesystem outside the single selected release component.
    """
    if source.is_symlink() or not source.is_dir():
        if require:
            raise FileNotFoundError('Required staged release component missing or symlinked: ' + str(source))
        return {}
    root = source.resolve(strict=True)
    files: dict[str, Path] = {}

    def visit(directory: Path, relative: Path, ancestors: frozenset[Path]) -> None:
        actual = directory.resolve(strict=True)
        if not actual.is_relative_to(root):
            raise RuntimeError('External release directory symlink: ' + str(directory))
        if actual in ancestors:
            raise RuntimeError('Cyclic release directory symlink: ' + str(directory))
        parents = ancestors | {actual}
        for item in sorted(actual.iterdir()):
            member = relative / item.name
            if item.is_symlink():
                try:
                    target = item.resolve(strict=True)
                except (OSError, RuntimeError) as error:
                    raise RuntimeError('Broken or cyclic release symlink: ' + str(item)) from error
                if not target.is_relative_to(root):
                    raise RuntimeError('External release symlink: ' + str(item))
            else:
                target = item
            if target.is_dir():
                visit(target, member, parents)
            elif target.is_file():
                # `target` is a physical file inside the selected component.
                # ZIP entries use the *alias* path but contain ordinary bytes.
                key = prefix + '/' + member.as_posix()
                if key in files:
                    raise RuntimeError('Duplicate release file entry: ' + key)
                files[key] = target
            else:
                raise RuntimeError('Non-file release component: ' + str(item))

    visit(root, Path(), frozenset())
    if require and not files:
        raise RuntimeError('Required staged release component is empty: ' + str(source))
    return files


def _provision_runtime_for_release(builds: Path, target: str) -> Path:
    """Use the native-verified runtime or unpack a pinned foreign runtime.

    Cross-target archives are SHA-256 checked, but are deliberately NOT
    marked native-executed until Windows/Linux runs their own acceptance.
    """
    from host import portable_python as pp
    arch = FOUR_ARCH[target]
    native = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}.get(__import__('platform').system())
    if native == target:
        runtime = pp.provision(builds, target, arch)
        if target == 'win':
            from host.windows_winpty import ensure as ensure_winpty
            ensure_winpty(builds, runtime)
        return runtime
    triplet, digest = pp.PACKAGES[(target, arch)]
    name = f'cpython-{pp.VERSION}+{pp.UPSTREAM}-{triplet}-install_only.tar.gz'
    archive = builds / 'Intermediates/CinterForgeTerminal/PythonPackages' / name
    pp._download_verified(archive, name, digest)
    runtime = builds / 'Intermediates/CinterForgeTerminal/ForeignPython' / target / arch / pp.VERSION
    binary = runtime / ('python.exe' if target == 'win' else 'bin/python3')
    if not runtime.exists():
        runtime.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='Portable-', dir=runtime.parent) as scratch:
            candidate = Path(scratch) / pp.VERSION
            pp._extract_portable(archive, candidate)
            if target != 'win' and not (candidate / 'bin/python').is_file():
                shutil.copy2(candidate / 'bin/python3', candidate / 'bin/python')
            exe = candidate / ('python.exe' if target == 'win' else 'bin/python')
            signature = exe.open('rb').read(4)
            if not (signature[:2] == b'MZ' if target == 'win' else signature == b'\x7fELF'):
                raise RuntimeError('Pinned foreign Python has wrong executable type: ' + str(exe))
            candidate.rename(runtime)
    if not binary.is_file() or (target == 'win' and binary.open('rb').read(2) != b'MZ'):
        raise RuntimeError('Incomplete verified foreign Python distribution: ' + str(runtime))
    if target == 'win':
        from host.windows_winpty import ensure as ensure_winpty
        ensure_winpty(builds, runtime)
    return runtime


def provision_release_runtimes(builds: Path) -> dict[str, Path]:
    """Setup-only acquisition/extraction of all four-package Python runtimes."""
    return {target: _provision_runtime_for_release(builds, target)
            for target in ('mac', 'linux', 'win')}


def _runtime_for_release(builds: Path, target: str) -> Path:
    """Build-only, read-only validation of a Setup-provisioned runtime."""
    from host import portable_python as pp
    arch = FOUR_ARCH[target]
    native = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}.get(__import__('platform').system())
    if target == native:
        runtime = builds / 'Python/Environments' / target / arch / 'cinterterminalenv'
        receipt = runtime / 'cinterforge-requirements.json'
        expected = pp.PACKAGES[target, arch][1]
        if not receipt.is_file() or receipt.is_symlink():
            raise RuntimeError(f'Setup prerequisite missing: {receipt}; run ./Cinter setup all')
        data = json.loads(receipt.read_text())
        if (data.get('schema') != pp.SCHEMA or data.get('source_sha256') != expected or
                data.get('platform') != target or data.get('arch') != arch):
            raise RuntimeError(f'Pinned Python Setup receipt mismatch: {receipt}')
    else:
        runtime = builds / 'Intermediates/CinterForgeTerminal/ForeignPython' / target / arch / pp.VERSION
    binary = runtime / ('python.exe' if target == 'win' else 'bin/python')
    if not binary.is_file() or binary.is_symlink():
        raise RuntimeError(f'Setup prerequisite missing: {binary}; run ./Cinter setup all')
    signature = binary.open('rb').read(4)
    if target == 'win' and signature[:2] != b'MZ':
        raise RuntimeError(f'Invalid Windows Python runtime: {binary}')
    if target == 'linux' and signature != b'\x7fELF':
        raise RuntimeError(f'Invalid Linux Python runtime: {binary}')
    if target == 'mac' and signature not in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe',
                                            b'\xfe\xed\xfa\xcf', b'\xbe\xba\xfe\xca'):
        raise RuntimeError(f'Invalid macOS Python runtime: {binary}')
    if target == 'win':
        from host.windows_winpty import verify as verify_winpty
        verify_winpty(runtime)
    return runtime


def _verify_mac_if_runtime(app: Path) -> None:
    """Reject a signed distribution with stale service code or broken ESM closure.

    The live HTTP server is copied from the immutable Host stage, not from
    webapp in the checkout. Checking only dist/index.js missed 404s
    for its transitive imports in previously packaged applications.
    """
    embedded = app / 'Contents/Resources/Builds'
    staged_service = embedded / f'Python/Host/mac/arm64/{HOST_RELEASE}/webapp/CinterForgeTerminal.py'
    current_service = Path(__file__).resolve().parents[1] / 'webapp/CinterForgeTerminal.py'
    if not staged_service.is_file() or staged_service.read_bytes() != current_service.read_bytes():
        raise RuntimeError('Mac ZIP refused: bundled Terminal HTTP server is missing or stale; '
                           'run ./Cinter host stage and rebuild the Mac app before Build all')
    # The shell resolves mac_games.json on *every* PTY startup. An app ZIP
    # containing a stale Host stage can pass server-only and ZMachine checks
    # yet fail with FileNotFoundError before a command is entered.
    shell = staged_service.parent
    authored = current_service.parent
    for name in HOST_ASSETS:
        member = shell / name
        source = authored / name
        if not member.is_file() or not source.is_file() or member.read_bytes() != source.read_bytes():
            raise RuntimeError('Mac ZIP refused: bundled Terminal shell asset missing or stale: ' + name)
    catalog = json.loads((shell / 'mac_games.json').read_text(encoding='utf-8'))
    if catalog.get('schema') != 'cinterforge.terminal.macgames.v1' or len(catalog.get('games', [])) != 67:
        raise RuntimeError('Mac ZIP refused: incomplete Macintosh game catalog')
    dist = embedded / 'CinterForgeTerminal/Dependencies/ZMachine/0.3.2/dist'
    if not dist.is_dir() or dist.is_symlink() or not (dist / 'index.js').is_file():
        raise RuntimeError('Mac ZIP refused: bundled ZMachine dist/index.js missing')
    # Capture static import/export-from, bare import, and dynamic import strings.
    # Only relative JS imports are resolved here; platform/browser imports are
    # neither rewritten nor interpreted as packaged files.
    specifiers = re.compile(r"(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)['\"](\.{1,2}/[^'\"]+)['\"]")
    errors = []
    for source in sorted(dist.rglob('*.js')):
        if source.is_symlink():
            errors.append(f'symlinked JS module: {source.relative_to(dist)}')
            continue
        for spec in specifiers.findall(source.read_text(encoding='utf-8')):
            target = (source.parent / spec).resolve()
            if not target.is_relative_to(dist.resolve()):
                errors.append(f'{source.relative_to(dist)} -> out-of-tree {spec}')
                continue
            options = [target] if target.suffix else [target.with_suffix('.js'), target / 'index.js']
            if not any(member.is_file() and not member.is_symlink() for member in options):
                errors.append(f'{source.relative_to(dist)} -> {spec}')
    if errors:
        raise RuntimeError('Mac ZIP refused: unresolved compiled ZMachine imports:\n' +
                           '\n'.join(errors[:40]))


def _app_assets(builds: Path, target: str, release: str) -> dict[str, Path | bytes]:
    """One relocatable app per OS. No SDK or server binary before consent."""
    arch = FOUR_ARCH[target]
    if target == 'mac':
        from host.mac_app import application
        app = application(builds)
        if not (app / 'Contents/MacOS/CinterForgeTerminal').is_file() or not (app / 'Contents/Info.plist').is_file():
            raise FileNotFoundError('Mac Release .app missing; stage the Mac application first: ' + str(app))
        _verify_mac_if_runtime(app)
        prefix = 'CinterForge Terminal.app'
        assets: dict[str, Path | bytes] = _files_under(app, prefix)
        builds_prefix = prefix + '/Contents/Resources/Builds'
        # CFBundleExecutable must be a real Mach-O. Native startup locates its
        # embedded Builds/Host/Python closure without a shell-script launcher.
        binary = app / 'Contents/MacOS/CinterForgeTerminal'
        with binary.open('rb') as binary_stream:
            magic = binary_stream.read(4)
        if magic not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                         b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
            raise RuntimeError('Mac CFBundleExecutable must be a Mach-O binary')
        # The macOS Stage owns the complete and already-signed .app. Never
        # add files inside Contents after the bundle has been sealed.
        from host.mac_app import _verify_signature
        _verify_signature(app)
    else:
        prefix = 'CinterForge Terminal'
        builds_prefix = prefix + '/Builds'
        assets = {}
        if target == 'linux':
            binary = builds / 'Intermediates/CinterForgeTerminal/LinuxNative/x64/cmake/CinterForgeTerminal'
        else:
            binary = builds / 'Intermediates/CinterForgeTerminal/WindowsCross/x64/cmake/CinterForgeTerminal.exe'
        signature = binary.open('rb').read(4) if binary.is_file() else b''
        expected = b'\x7fELF' if target == 'linux' else b'MZ'
        if not signature.startswith(expected):
            raise RuntimeError(f'Build a real {target} native launcher before packaging: {binary}')
        installed_binary = 'CinterForge Terminal.exe' if target == 'win' else 'CinterForge Terminal'
        assets[prefix + '/' + installed_binary] = binary
        if target == 'win':
            # The interactive shell needs MSYS2 Bash at runtime. The unified
            # DevTools package is optional and must NOT be required to launch
            # the lightweight Windows application. Ship its existing verified
            # shell runtime here, separate from the compiler/toolchain SDK.
            msys = builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
            for required in ('usr/bin/bash.exe', 'usr/bin/cygpath.exe',
                             'usr/bin/msys-2.0.dll'):
                if not (msys / required).is_file():
                    raise FileNotFoundError('Windows app requires staged MSYS2 shell runtime: '
                                            + str(msys / required))
            shell_prefix = builds_prefix + '/CinterForgeTerminal/Runtime/MSYS2'
            assets.update(_files_under(msys / 'usr/bin', shell_prefix + '/usr/bin'))
            _require_windows_shell(assets)
            for optional in ('usr/etc', 'etc', 'usr/share/terminfo'):
                folder = msys / optional
                if folder.is_dir():
                    assets.update(_files_under(folder, shell_prefix + '/' + optional,
                                               require=False))
            # All recursive non-system DLLs were resolved by cross_build.
            for dll in binary.parent.glob('*.dll'):
                assets[prefix + '/' + dll.name] = dll
            from host.windows_fixed_runtime import verify
            assets.update(_files_under(verify(builds),
                         builds_prefix + '/CinterForgeTerminal/Dependencies/WebView2FixedRuntime'))
            launch = r"""@echo off
setlocal EnableExtensions
set "BUILDS=%~dp0Builds"
set "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER=%BUILDS%\CinterForgeTerminal\Dependencies\WebView2FixedRuntime"
"%~dp0CinterForge Terminal.exe" --builds "%BUILDS%" --python "%BUILDS%\Python\Environments\win\x64\cinterterminalenv\python.exe" --worker "%BUILDS%\Python\Host\win\x64\RELEASE\python\worker.py" --service "%BUILDS%\Python\Host\win\x64\RELEASE\examples\shell\CinterForgeTerminal.py" %*
exit /b %ERRORLEVEL%
""".replace('RELEASE', release)
            assets[prefix + '/Terminal.cmd'] = launch.encode()
        else:
            launch = """#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
BUILDS="$HERE/Builds"
export CINTER_BUILDS_ROOT="$BUILDS"
exec "$HERE/CinterForge Terminal" --builds "$BUILDS" --python "$BUILDS/Python/Environments/linux/x64/cinterterminalenv/bin/python" --worker "$BUILDS/Python/Host/linux/x64/RELEASE/python/worker.py" --service "$BUILDS/Python/Host/linux/x64/RELEASE/webapp/CinterForgeTerminal.py" "$@"
""".replace('RELEASE', release)
            assets[prefix + '/Terminal.sh'] = launch.encode()
            desktop = '[Desktop Entry]\nType=Application\nName=CinterForge Terminal\nExec=Terminal.sh\nIcon=AppIcon\nTerminal=false\nCategories=Development;System;\n'
            assets[prefix + '/CinterForge Terminal.desktop'] = desktop.encode()
            icon = Path(__file__).resolve().parents[1] / 'build/resources/AppIcon.png'
            if not icon.is_file():
                raise FileNotFoundError('Authored Terminal build/resources/AppIcon.png missing: ' + str(icon))
            assets[prefix + '/AppIcon.png'] = icon
    # The same Code-OSS web workbench and Quadra 650 disc must be served from
    # each app's own verified Builds payload. No sibling-repo source access.
    if target != 'mac':
        assets.update(_files_under(builds / 'CinterForgeTerminal/Dependencies/ZMachine/0.3.2',
                                   builds_prefix + '/CinterForgeTerminal/Dependencies/ZMachine/0.3.2'))
        assets.update(_files_under(builds / 'InteractiveFiction/CodeOSS/Web',
                                   builds_prefix + '/CodeOSS'))
        rel = 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph'
        assets.update(_files_under(builds / rel, builds_prefix + '/' + rel))
    if target != 'mac':
        runtime = _runtime_for_release(builds, target)
        assets.update(_files_under(runtime, builds_prefix + f'/Python/Environments/{target}/{arch}/cinterterminalenv'))
        # Stage authored service code (no CPH SDK/toolchain in lightweight apps).
        host_root = builds_prefix + f'/Python/Host/{target}/{arch}/{release}'
        repo = Path(__file__).resolve().parents[1]
        assets[host_root + '/python/worker.py'] = repo / 'host/worker.py'
        for name in HOST_ASSETS:
            assets[host_root + '/webapp/' + name] = repo / 'webapp' / name
        assets.update(_files_under(repo / 'webapp/joshua',
                                   host_root + '/webapp/joshua'))
    if any('/Servers/Bedrock/' in k or k.endswith('/bedrock_server') or k.endswith('/bedrock_server.exe')
           for k in assets):
        raise RuntimeError('Bedrock server must never ship in Setup/Build/Stage')
    return assets


def _devtools_assets(builds: Path) -> dict[str, Path | bytes]:
    """One three-OS SDK closure, without live Colima VM disks or Bedrock."""
    required = (
        'CinterForgeTerminal/Toolchains/mac',
        'CinterForgeTerminal/Toolchains/LinuxSample',
        'CinterForgeTerminal/Toolchains/WindowsCross',
        'CinterForgeTerminal/Toolchains/Windows/MSYS2',
        'Toolchains/WebView',
        'Toolchains/WebView2',
    )
    assets: dict[str, Path | bytes] = {}
    for rel in required:
        assets.update(_files_under(builds / rel, 'DevTools/Builds/' + rel))
    assets['DevTools/README.txt'] = (
        'CinterForgeTerminal optional unified development tools for macOS, Linux and Windows.\n'
        'The macOS platform compiler is supplied by Xcode on macOS; it cannot be redistributed.\n'
        'Toolchain containers are immutable archives. Do not import running Colima/Lima disk state.\n'
        'Installed into steamapps by Stage; end-user acquisition remains optional.\n').encode()
    return assets


def _release_file_mode(name: str, value: Path | bytes) -> int:
    """Preserve source execute bits, including bundled runtimes and SDK tools.

    The old suffix-only allowlist silently demoted every other executable to
    0644. Keep explicit execute bits for generated launchers and runtime Python
    entrypoints even when an upstream unpacker did not preserve their modes.
    """
    source_executable = isinstance(value, Path) and bool(value.stat().st_mode & 0o111)
    launcher = name.endswith(('/Terminal.sh', '/CinterForgeTerminal',
                              '/CinterForge Terminal', '/CinterForgeTerminal.exe',
                              '/CinterForge Terminal.exe'))
    parts = Path(name).parts
    python_entrypoint = ('Python' in parts and 'Environments' in parts and
                         'bin' in parts and parts[-1] in
                         ('python', 'python3', 'python3.11', 'python3.12', 'python3.13'))
    return 0o755 if source_executable or launcher or python_entrypoint else 0o644


def _write_release_zip(path: Path, files: dict[str, Path | bytes], *, target: str,
                       release: str, root: str) -> dict:
    if not files or any(not n.startswith(root + '/') or '..' in Path(n).parts or '\\' in n
                        for n in files):
        raise RuntimeError('Unsafe or empty release file list')
    details = {}
    total_bytes = sum(len(value) if isinstance(value, bytes) else value.stat().st_size
                      for value in files.values())
    written_bytes = 0
    last_report = time.monotonic()
    _progress(f'ZIP {target} start ({len(files)} files)', 0, total_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + '.part')
    try:
        with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=3, allowZip64=True) as archive:
            for name, value in sorted(files.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                mode = _release_file_mode(name, value)
                info.create_system = 3
                info.external_attr = ((stat.S_IFREG | mode) << 16)
                info.compress_type = zipfile.ZIP_DEFLATED
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, 'w', force_zip64=True) as dst:
                    if isinstance(value, bytes):
                        dst.write(value); digest.update(value); size = len(value)
                        written_bytes += size
                    else:
                        if not value.is_file():
                            raise FileNotFoundError('Missing release input: ' + str(value))
                        with value.open('rb') as src:
                            for block in iter(lambda: src.read(1024 * 1024), b''):
                                dst.write(block); digest.update(block); size += len(block)
                                written_bytes += len(block)
                                now = time.monotonic()
                                if now - last_report >= 2:
                                    _progress(f'ZIP {target}', written_bytes, total_bytes)
                                    last_report = now
                details[name] = {'sha256': digest.hexdigest(), 'bytes': size,
                                 'mode': mode}
            receipt = {'schema': FOUR_SCHEMA, 'target': target, 'release': release,
                       'root': root, 'files': details,
                       'native_acceptance': 'not-claimed-by-package'}
            archive.writestr('release-manifest.json', json.dumps(receipt, sort_keys=True) + '\n')
        _verify_release_zip(partial, target=target, release=release, hash_files=True, progress_label=f'Verify ZIP {target}')
        partial.replace(path)
        _progress(f'ZIP {target} complete', written_bytes, total_bytes)
    finally:
        partial.unlink(missing_ok=True)
    return {'filename': path.name, 'sha256': _hash(path), 'bytes': path.stat().st_size,
            'files': len(details), 'target': target}


def _verify_release_zip(path: Path, *, target: str, release: str,
                        hash_files: bool = True, progress_label: str = '') -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or 'release-manifest.json' not in names:
            raise RuntimeError('Release ZIP missing manifest or contains duplicate names')
        manifest = json.loads(archive.read('release-manifest.json'))
        root = {'mac': 'CinterForge Terminal.app', 'linux': 'CinterForge Terminal',
                'win': 'CinterForge Terminal', 'devtools': 'DevTools'}[target]
        if (manifest.get('schema') != FOUR_SCHEMA or manifest.get('target') != target or
                manifest.get('release') != release or manifest.get('root') != root):
            raise RuntimeError(f'Release ZIP identity mismatch: {path}')
        files = manifest.get('files')
        if not isinstance(files, dict) or set(names) != set(files) | {'release-manifest.json'}:
            raise RuntimeError('Release ZIP contains untracked or missing files')
        if target == 'win':
            _require_windows_shell(files, zip_archive=archive)
        if any(not name.startswith(root + '/') or '\\' in name or '..' in Path(name).parts or
               '/Servers/Bedrock/' in name or name.endswith(('/bedrock_server', '/bedrock_server.exe'))
               for name in files):
            raise RuntimeError('Release ZIP has unsafe, foreign or Bedrock-server paths')
        verified = 0
        total = sum(x['bytes'] for x in files.values())
        last_report = time.monotonic()
        for name, entry in files.items():
            zi = archive.getinfo(name)
            if entry.get('mode') not in (0o644, 0o755) or not isinstance(entry.get('sha256'), str) or len(entry['sha256']) != 64:
                raise RuntimeError('Invalid release permissions/checksum entry: ' + name)
            if zi.is_dir() or (zi.external_attr >> 16) & 0o170000 not in (0, stat.S_IFREG):
                raise RuntimeError('Release ZIP contains a nonregular entry: ' + name)
            if (zi.external_attr >> 16) & 0o777 != entry['mode']:
                raise RuntimeError('Release ZIP executable mode differs from manifest: ' + name)
            if zi.file_size != entry['bytes']:
                raise RuntimeError('Release file size mismatch: ' + name)
            if hash_files:
                digest = hashlib.sha256()
                with archive.open(name) as src:
                    for block in iter(lambda: src.read(1024 * 1024), b''):
                        digest.update(block)
                        verified += len(block)
                        now = time.monotonic()
                        if progress_label and now - last_report >= 2:
                            _progress(progress_label, verified, total)
                            last_report = now
                if digest.hexdigest() != entry['sha256']:
                    raise RuntimeError('Release ZIP file checksum mismatch: ' + name)
        # archive.open() already validates each member's CRC on full read.
        # A second testzip() pass would reread ~4 GiB without visible progress.
        if not hash_files and archive.testzip() is not None:
            raise RuntimeError('Release ZIP CRC mismatch')
        if progress_label:
            _progress(progress_label + ' verified', verified, total)
    return manifest


def _replace_generated_output(source: Path, destination: Path, builds: Path) -> None:
    """Replace build-owned output; save unexpected directories under Builds."""
    if destination.is_dir() and not destination.is_symlink():
        quarantine = builds / 'Intermediates/CinterForgeTerminal/OutputConflicts'
        quarantine.mkdir(parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='previous-', dir=quarantine)) / destination.name
        destination.replace(backup)
    source.replace(destination)


# Metadata is a cheap *dirty check*, not a replacement for the release's
# cryptographic SHA-256 manifest. Files whose identity/content metadata changes
# are fully rehashed against the existing ZIP. Publishing continues to verify
# the release artifacts independently.
RELEASE_BUILD_CACHE_SCHEMA = 'cinterforge.terminal.build-input-cache.v1'


def _release_input_fingerprint(assets: dict[str, Path | bytes]) -> str:
    """Fingerprint the source snapshot without reading multi-gigabyte toolchains.

    Include paths, permission bits, device/inode, size, mtime and ctime (ns).
    ctime also invalidates a cache when a writer restores an old mtime. The
    content-hash fallback runs when anything changes. This cache is an
    optimization for ordinary repeated lifecycle builds, not a trust boundary.
    """
    digest = hashlib.sha256()
    for name, value in sorted(assets.items()):
        if isinstance(value, bytes):
            identity = ['inline', len(value), hashlib.sha256(value).hexdigest()]
        else:
            info = value.stat()
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('Release input is not a regular file: ' + str(value))
            identity = [str(value.resolve()), info.st_dev, info.st_ino, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns,
                        stat.S_IMODE(info.st_mode)]
        digest.update(json.dumps([name, identity], separators=(',', ':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def _release_archive_identity(archive: Path) -> dict:
    """Cheaply identify a specific output ZIP before trusting cached inputs."""
    info = archive.stat()
    if not stat.S_ISREG(info.st_mode) or archive.is_symlink():
        raise ValueError('Release archive is not a regular file')
    return {'bytes': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'ctime_ns': info.st_ctime_ns, 'inode': info.st_ino,
            'device': info.st_dev}


def _release_cache_file(builds: Path, release: str, target: str) -> Path:
    return (builds / 'Intermediates/CinterForgeTerminal/ReleaseInputCache' /
            release / (target + '.json'))


def _release_cache_matches(builds: Path, release: str, target: str,
                           fingerprint: str, archive: Path, old: dict) -> bool:
    """Accept a previous verified build only if its inputs and ZIP are unchanged."""
    cache_file = _release_cache_file(builds, release, target)
    if cache_file.is_symlink() or not cache_file.is_file():
        return False
    try:
        cached = json.loads(cache_file.read_text(encoding='utf-8'))
        return (cached.get('schema') == RELEASE_BUILD_CACHE_SCHEMA and
                cached.get('release') == release and
                cached.get('target') == target and
                cached.get('input_fingerprint') == fingerprint and
                cached.get('filename') == old['filename'] and
                cached.get('sha256') == old['sha256'] and
                cached.get('archive') == _release_archive_identity(archive))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _write_release_cache(builds: Path, release: str, target: str,
                         fingerprint: str, archive: Path, receipt: dict) -> None:
    """Commit the cache after successful build/verification; never inside source."""
    cache_file = _release_cache_file(builds, release, target)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if cache_file.is_symlink():
        raise RuntimeError('Unsafe release cache symlink: ' + str(cache_file))
    record = {'schema': RELEASE_BUILD_CACHE_SCHEMA, 'release': release,
              'target': target, 'input_fingerprint': fingerprint,
              'filename': receipt['filename'], 'sha256': receipt['sha256'],
              'archive': _release_archive_identity(archive)}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                     prefix=target + '-', suffix='.json',
                                     dir=cache_file.parent, delete=False) as file:
        partial = Path(file.name)
        try:
            json.dump(record, file, indent=2, sort_keys=True)
            file.write('\n')
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    try:
        partial.replace(cache_file)
    finally:
        partial.unlink(missing_ok=True)


def _refresh_release_caches(builds: Path, release: str, fingerprints: dict,
                             artifacts: dict, out: Path) -> None:
    """A missing/corrupt cache must never invalidate an otherwise good build."""
    for target, fingerprint in fingerprints.items():
        try:
            receipt = artifacts[target]
            _write_release_cache(builds, release, target, fingerprint,
                                 out / receipt['filename'], receipt)
        except OSError as error:
            print(f'[TERMINAL BUILD] {target} cache unavailable: {error}', flush=True)


def _adopt_legacy_release_zips(builds: Path, release: str, destination: Path) -> None:
    """One-time adoption of already verified date-named ZIPs without recompressing.

    An existing release is immutable input. Keep it in place for rollback and
    hard-link its validated archives into the new stable-name distribution. If
    verification fails, Build falls back to its normal fresh artifact builder.
    Nothing is adopted when an operator already has stable ZIPs or a manifest.
    """
    legacy = destination / release
    manifest = legacy / 'four-artifacts.json'
    if (not legacy.is_dir() or legacy.is_symlink() or
            not manifest.is_file() or manifest.is_symlink() or
            (destination / 'four-artifacts.json').exists() or
            any((destination / f'CinterForgeTerminal-{target}.zip').exists()
                for target in FOUR_TARGETS)):
        return
    try:
        record = json.loads(manifest.read_text(encoding='utf-8'))
        if (record.get('schema') != FOUR_SCHEMA or record.get('release') != release or
                set(record.get('artifacts', {})) != set(FOUR_TARGETS)):
            return
        candidates = {}
        for target, entry in record['artifacts'].items():
            name = f'CinterForgeTerminal-{target}-{release}.zip'
            archive = legacy / name
            if (entry.get('filename') != name or archive.is_symlink() or
                    not archive.is_file() or archive.stat().st_size != entry.get('bytes') or
                    _hash(archive) != entry.get('sha256')):
                raise RuntimeError(f'Previous {target} release ZIP is not trustworthy')
            _verify_release_zip(archive, target=target, release=release, hash_files=False)
            candidates[target] = archive
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as error:
        print(f'[TERMINAL BUILD] Cannot adopt old ZIPs: {error}; rebuilding normally', flush=True)
        return

    adopted = []
    try:
        changed = dict(record)
        changed['artifacts'] = {name: dict(detail) for name, detail in record['artifacts'].items()}
        for target, archive in candidates.items():
            filename = f'CinterForgeTerminal-{target}.zip'
            target_path = destination / filename
            # Same filesystem: this creates no second multi-gigabyte copy.
            os.link(archive, target_path)
            adopted.append(target_path)
            changed['artifacts'][target]['filename'] = filename
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='manifest-',
                                         suffix='.json', dir=destination, delete=False) as handle:
            staged_manifest = Path(handle.name)
            json.dump(changed, handle, indent=2, sort_keys=True)
            handle.write('\n')
        try:
            staged_manifest.replace(destination / 'four-artifacts.json')
        finally:
            staged_manifest.unlink(missing_ok=True)
    except BaseException:
        for path in adopted:
            path.unlink(missing_ok=True)
        raise
    print(f'[TERMINAL BUILD] Adopted {len(adopted)} verified historical ZIPs '
          'under stable filenames without recompression', flush=True)


def build_four(builds: Path, *, release: str = HOST_RELEASE) -> dict:
    """Build precisely three native app ZIPs and one unified DevTools ZIP.

    This is a repeatable Build working directory: changed source inputs,
    outdated ZIPs, and stray existing files never require a version bump.
    Release ZIP contents are still hashed and checked before publication.
    """
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', release):
        raise ValueError('Unsafe release identifier')
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('Build requires the existing sibling Builds directory')
    parent = builds / 'CinterForgeTerminal/Distribution'
    parent.mkdir(parents=True, exist_ok=True)
    out = parent
    _adopt_legacy_release_zips(builds, release, out)
    files_by_target = {}
    for target in FOUR_TARGETS:
        print(f'[TERMINAL BUILD] Gathering {target} release inputs...', flush=True)
        assets = _devtools_assets(builds) if target == 'devtools' else _app_assets(builds, target, release)
        files_by_target[target] = assets
        size = sum(len(v) if isinstance(v, bytes) else v.stat().st_size for v in assets.values())
        print(f'[TERMINAL BUILD] {target} inputs {len(assets)} files, {size / 1048576:.1f} MiB', flush=True)
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise RuntimeError('Unsafe release output directory: ' + str(out))
    # Independently compare each target. Changing one app must never rezip
    # the (much larger) unified DevTools package.
    previous = {}
    if out.is_dir():
        try:
            record = out / 'four-artifacts.json'
            if record.is_symlink():
                raise ValueError('Unsafe release receipt')
            previous = json.loads(record.read_text())
            if previous.get('schema') != FOUR_SCHEMA or previous.get('release') != release:
                previous = {}
        except (OSError, ValueError, TypeError):
            previous = {}
    artifacts = {}
    dirty = {}
    input_fingerprints = {}
    for target, assets in files_by_target.items():
        # All target checks are independent. In particular, touching source for
        # a lightweight app cannot force the huge DevTools archive to rebuild.
        fingerprint = _release_input_fingerprint(assets)
        input_fingerprints[target] = fingerprint
        old = previous.get('artifacts', {}).get(target, {})
        try:
            name = f'CinterForgeTerminal-{target}.zip'
            if old.get('filename') != name:
                raise ValueError('Missing or outdated target receipt')
            archive = out / name
            if not archive.is_file() or archive.is_symlink() or archive.stat().st_size != old['bytes']:
                raise ValueError('Missing or changed target ZIP')
            if _release_cache_matches(builds, release, target, fingerprint, archive, old):
                artifacts[target] = old
                print(f'[TERMINAL BUILD] {target} clean (cached inputs and ZIP): '
                      f'reusing {archive}', flush=True)
                continue
            print(f'[TERMINAL BUILD] {target} cache miss: verifying release '
                  'ZIP and input checksums', flush=True)
            existing = _verify_release_zip(archive, target=target, release=release,
                                           progress_label=f'Verify ZIP {target}')
            expected = {key: {'sha256': (_hash(value) if isinstance(value, Path) else
                                                  hashlib.sha256(value).hexdigest()),
                              'mode': _release_file_mode(key, value)}
                        for key, value in assets.items()}
            recorded = {key: {'sha256': item['sha256'], 'mode': item['mode']}
                        for key, item in existing['files'].items()}
            if recorded != expected or _hash(archive) != old['sha256']:
                raise ValueError('Target inputs or ZIP changed')
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as error:
            dirty[target] = str(error)
            print(f'[TERMINAL BUILD] {target} dirty: {error}', flush=True)
        else:
            artifacts[target] = old
            print(f'[TERMINAL BUILD] {target} clean: reusing {archive}', flush=True)
    if not dirty and set(artifacts) == set(FOUR_TARGETS):
        _refresh_release_caches(builds, release, input_fingerprints, artifacts, out)
        return previous
    with tempfile.TemporaryDirectory(prefix='Staging-', dir=parent) as temporary:
        staging = Path(temporary)
        for target in dirty:
            root = APP_ROOTS[target]
            name = f'CinterForgeTerminal-{target}.zip'
            archive = staging / name
            artifacts[target] = _write_release_zip(archive, files_by_target[target],
                                                    target=target, release=release, root=root)
        report = {'schema': FOUR_SCHEMA, 'release': release, 'artifacts': artifacts,
                  'bedrock': 'excluded-until-app-eula-acceptance',
                  'native_acceptance': 'separate-target-tests-required'}
        out.mkdir(parents=True, exist_ok=True)
        for target in dirty:
            name = artifacts[target]['filename']
            _replace_generated_output(staging / name, out / name, builds)
        (staging / 'four-artifacts.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        _replace_generated_output(staging / 'four-artifacts.json', out / 'four-artifacts.json', builds)
    _refresh_release_caches(builds, release, input_fingerprints, artifacts, out)
    for target in FOUR_TARGETS:
        print('[TERMINAL BUILD] ' + target + ' -> ' + str(out / artifacts[target]['filename']), flush=True)
    return report


def _steam_library_entries(config: Path) -> list[Path]:
    """Read Steam's own libraryfolders.vdf, not just its default install root.

    Valve KeyValues stores a `path` for each numbered library; the associated
    `apps` mapping may omit locally-installed development apps. A Steam library
    path points to the library root, *not* to its steamapps subdirectory.
    """
    if not config.is_file() or config.is_symlink():
        return []
    try:
        text = config.read_text(encoding='utf-8-sig', errors='replace')
    except OSError:
        return []
    # Preserve a literal backslash in Windows paths; only unescape VDF's
    # doubled backslash and escaped quote. No shell or env-var expansion.
    values = re.findall(r'(?im)^\s*"path"\s*"((?:\\.|[^"\\])*)"', text)
    roots = []
    for value in values:
        value = value.replace('\\\\', '\\').replace('\\"', '"').strip()
        if not value:
            continue
        root = Path(value).expanduser()
        if root.is_absolute():
            roots.append(root)
    return roots


def _cinterforge_installed_in_library(steamapps: Path) -> bool:
    """Find Steam's recorded CinterForge installation, not an old Stage folder.

    Steam app display names and physical installdirs are not guaranteed to
    match. A matching appmanifest wins even if the application folder is named
    differently from our CinterForge/Applications staging hierarchy.
    """
    for manifest in steamapps.glob('appmanifest_*.acf'):
        if not manifest.is_file() or manifest.is_symlink():
            continue
        try:
            data = manifest.read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            continue
        installed = re.search(r'(?im)^\s*"installdir"\s*"([^"\r\n]+)"', data)
        name = re.search(r'(?im)^\s*"name"\s*"([^"\r\n]+)"', data)
        if ((installed and installed.group(1).casefold().startswith('cinterforge')) or
                (name and name.group(1).casefold().startswith('cinterforge'))):
            return True
    return False


def steamapps_root(explicit: Path | None = None) -> Path:
    """Find the Steam library with CinterForge installed; never invent one.

    Explicit CLI/environment selection remains supported. Otherwise read
    Steam's libraryfolders.vdf, look for an installed CinterForge appmanifest,
    then an existing CinterForge directory. Multiple matches are an error:
    silently choosing the default can install to the wrong physical drive.
    """
    import platform as host_platform
    configured = explicit or os.environ.get('CINTER_STEAMAPPS') or os.environ.get('CFOS_STEAMAPPS')
    if configured:
        path = Path(configured).expanduser()
        if path.name.casefold() != 'steamapps' or not path.is_dir() or path.is_symlink():
            raise RuntimeError('Provide an existing Steam steamapps root via CINTER_STEAMAPPS: ' + str(path))
        return path.resolve(strict=True)

    system = host_platform.system()
    if system in ('Darwin', 'Linux'):
        # Colima/DevTools intentionally redirects HOME into Builds. Never use
        # that temporary HOME to locate a real user's Steam installation.
        try:
            import pwd
            login_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            login_home = Path.home()
    else:
        login_home = Path.home()
    if system == 'Darwin':
        steam_homes = [login_home / 'Library/Application Support/Steam']
    elif system == 'Linux':
        steam_homes = [login_home / '.local/share/Steam',
                       login_home / '.steam/steam', login_home / '.steam/root']
    else:
        steam_homes = [Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Steam']

    candidates: list[Path] = []
    seen: set[Path] = set()
    def add(candidate: Path) -> None:
        steam = candidate if candidate.name.casefold() == 'steamapps' else candidate / 'steamapps'
        if steam.is_dir() and not steam.is_symlink():
            resolved = steam.resolve(strict=True)
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(resolved)

    for home in steam_homes:
        # Both configurations occur in the wild; prefer Steam's own records
        # over a guessed default, regardless of directory ordering.
        for file in (home / 'steamapps/libraryfolders.vdf',
                     home / 'config/libraryfolders.vdf'):
            for library in _steam_library_entries(file):
                add(library)
        add(home)

    recorded = [path for path in candidates if _cinterforge_installed_in_library(path)]
    if len(recorded) == 1:
        return recorded[0]
    if len(recorded) > 1:
        raise RuntimeError('Multiple Steam libraries report an installed CinterForge app: '
                           + ', '.join(map(str, recorded)) + '; select with CINTER_STEAMAPPS')
    # If the default Steam root has only a directory accidentally created by
    # an old Stage, do not treat it as evidence of an installed Steam app.
    # Prefer a library with actual Steam app manifests when that is unique.
    active = [path for path in candidates if any(manifest.is_file() and not manifest.is_symlink()
                for manifest in path.glob('appmanifest_*.acf'))]
    if len(active) == 1:
        return active[0]
    existing = [path for path in candidates if (path / 'common/CinterForge').is_dir() and
                not (path / 'common/CinterForge').is_symlink()]
    if len(existing) == 1:
        return existing[0]
    if len(existing) > 1:
        raise RuntimeError('CinterForge exists in multiple Steam libraries with no unique appmanifest: '
                           + ', '.join(map(str, existing)) + '; select with CINTER_STEAMAPPS')
    if len(candidates) == 1:
        # First installation into the sole real Steam library is unambiguous.
        return candidates[0]
    raise RuntimeError('Cannot uniquely locate the installed CinterForge Steam library: '
                       + (', '.join(map(str, candidates)) or 'no Steam libraries found')
                       + '; install CinterForge through Steam or select CINTER_STEAMAPPS')


def _steam_release_path(steam: Path, target: str, release: str) -> Path:
    # Identical platform/product/version hierarchy to CFOS Creative Suite.
    return (steam / 'common/CinterForge/Applications' / STEAM_PLATFORMS[target]
            / 'CinterForge Terminal' / release)


def _move_legacy_target(steam: Path, target: str, release: str, entry: dict,
                        destination: Path) -> bool:
    """Move the old single-root Stage without copying gigabytes or losing extras."""
    legacy = steam / 'common/CinterForge/Applications/CinterForge Terminal' / release
    old_receipt = legacy / 'stage-receipt.json'
    if destination.exists() or not old_receipt.is_file() or old_receipt.is_symlink():
        return False
    try:
        data = json.loads(old_receipt.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if (data.get('schema') != FOUR_SCHEMA or data.get('release') != release or
            data.get('artifacts', {}).get(target, {}).get('sha256') != entry['sha256']):
        return False
    old_target = legacy / target
    if not old_target.is_dir() or old_target.is_symlink():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(destination)
    print(f'[TERMINAL STAGE] Moved existing {target} from legacy layout -> {destination}', flush=True)
    return True


def _installed_metadata_fingerprint(root: Path, entries: dict) -> str | None:
    """Cheap dirty check: sizes, modes and nanosecond mtimes, no file reads.

    Recheck symlink ancestry before trusting any cached installed receipt.
    A changed file's metadata invalidates the cache and triggers SHA-256 repair.
    """
    digest = hashlib.sha256()
    for name in sorted(entries):
        path = root
        for component in Path(name).parts:
            path = path / component
            if path.is_symlink():
                return None
        try:
            info = path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size != entries[name]['bytes'] or \
                stat.S_IMODE(info.st_mode) != entries[name]['mode']:
            return None
        digest.update(json.dumps([name, info.st_size, stat.S_IMODE(info.st_mode),
                                  info.st_mtime_ns], separators=(',', ':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def _stage_devtools_is_clean(root: Path, archive: Path, entry: dict,
                             *, release: str) -> bool:
    receipt = root / 'stage-receipt.json'
    if not receipt.is_file() or receipt.is_symlink() or not archive.is_file() or archive.is_symlink():
        return False
    try:
        previous = json.loads(receipt.read_text())
        st = archive.stat()
        if (previous.get('schema') != FOUR_SCHEMA or previous.get('release') != release or
                previous.get('target') != 'devtools' or
                previous.get('artifact', {}).get('sha256') != entry['sha256'] or
                previous.get('archive_mtime_ns') != st.st_mtime_ns or
                previous.get('archive_bytes') != st.st_size or
                not isinstance(previous.get('installed_state_sha256'), str)):
            return False
        with zipfile.ZipFile(archive) as z:
            manifest = json.loads(z.read('release-manifest.json'))
        if manifest.get('release') != release or manifest.get('target') != 'devtools':
            return False
        return (_installed_metadata_fingerprint(root, manifest['files']) ==
                previous['installed_state_sha256'])
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        return False


def _recover_interrupted_mac_stage(destination: Path, app_name: str, builds: Path) -> None:
    """Resolve the fixed-name backup left by an interrupted macOS Stage.

    A crash can occur immediately after renaming the previous app to
    ``.previous-stage``, or after promoting the new app but before removing
    that backup. Neither state should require user intervention. Never discard
    an ambiguous old backup: preserve it under the sibling Builds directory.

    Called only after the incoming archive has been fully extracted, hashed,
    and (on macOS) signature-verified. No existing app is touched beforehand.
    """
    installed_app = destination / app_name
    backup = destination / (app_name + '.previous-stage')
    if not backup.exists() and not backup.is_symlink():
        return
    if backup.is_symlink() or not backup.is_dir():
        raise RuntimeError('Unsafe macOS Stage recovery backup: ' + str(backup))
    if installed_app.is_symlink() or (installed_app.exists() and not installed_app.is_dir()):
        raise RuntimeError('Unsafe installed macOS app during Stage recovery: ' + str(installed_app))

    if not installed_app.exists():
        # The previous Stage stopped between moving the old app aside and
        # promoting the replacement. Restore the last installed app first so
        # any subsequent installation error leaves a usable application.
        backup.replace(installed_app)
        print('[TERMINAL STAGE] RECOVERED interrupted macOS Stage: restored previous app '
              f'from {backup}', flush=True)
        return

    # Both names exist: a previous Stage promoted its new app but did not
    # finish cleanup. Retain the ambiguous rollback copy outside the live app
    # directory instead of removing possibly valuable user data.
    recovery_root = builds / 'Intermediates/CinterForgeTerminal/StageRecovery/macOS'
    recovery_root.mkdir(parents=True, exist_ok=True)
    recovery_dir = Path(tempfile.mkdtemp(prefix='previous-mac-', dir=recovery_root))
    recovered_backup = recovery_dir / backup.name
    try:
        # The Steam installation and sibling Builds directory are not
        # necessarily on the same filesystem. ``Path.replace`` fails with
        # EXDEV in that case; ``shutil.move`` first attempts an atomic rename
        # and falls back to a complete copy followed by source removal.
        # On failure, preserve the original backup for another Stage attempt.
        shutil.move(str(backup), str(recovered_backup))
    except BaseException:
        # Leave a partially copied destination visible for diagnosis, but do
        # not touch the original backup or the installed application.
        if not recovered_backup.exists():
            recovery_dir.rmdir()
        raise
    print('[TERMINAL STAGE] RECOVERED stale macOS Stage backup; retained original at '
          f'{recovered_backup}', flush=True)


def _extract_mac_release(archive: Path, destination: Path, files: dict,
                         *, builds: Path) -> int:
    """Install a verified signed macOS ZIP with restart-safe rollback.

    Build produces a complete immutable bundle. Stage never patches, rebuilds,
    or resigns it; recovery and replacement work through same-volume renames.
    """
    import platform
    app_name = 'CinterForge Terminal.app'
    if destination.is_symlink():
        raise RuntimeError('Unsafe macOS Steam destination: ' + str(destination))
    print(f'[TERMINAL STAGE] macOS recovery protocol: {MAC_STAGE_RECOVERY_PROTOCOL}', flush=True)
    destination.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix='Unzip-mac-', dir=destination))
    old_app = destination / app_name
    backup = destination / (app_name + '.previous-stage')
    old_app_moved = False
    promoted = False
    try:
        total = sum(item['bytes'] for item in files.values())
        with zipfile.ZipFile(archive) as source:
            for name, detail in files.items():
                # _verify_release_zip already checked the complete archive.
                relative = Path(name)
                if relative.parts[0] != app_name or '..' in relative.parts:
                    raise RuntimeError('Unexpected macOS ZIP member: ' + name)
                output = candidate / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with source.open(name) as src, output.open('wb') as dst:
                    for chunk in iter(lambda: src.read(4 * 1024 * 1024), b''):
                        dst.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != detail['sha256']:
                    raise RuntimeError('macOS ZIP extraction SHA-256 mismatch: ' + name)
                output.chmod(detail['mode'])
        if platform.system() == 'Darwin':
            from host.mac_app import _verify_signature
            _verify_signature(candidate / app_name)

        _recover_interrupted_mac_stage(destination, app_name, builds)
        if old_app.exists() or old_app.is_symlink():
            if old_app.is_symlink() or not old_app.is_dir():
                raise RuntimeError('Unsafe existing macOS app: ' + str(old_app))
            old_app.replace(backup)
            old_app_moved = True
        (candidate / app_name).replace(old_app)
        promoted = True
        if platform.system() == 'Darwin':
            # A successful candidate check alone is not proof that the final
            # installed path still verifies; check the actual installed app.
            from host.mac_app import _verify_signature
            _verify_signature(old_app)

        if old_app_moved:
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                # The newly installed app is valid; a cleanup interruption
                # must not turn a successful install into a failed lifecycle.
                # The next Stage automatically resolves this leftover backup.
                print(f'[TERMINAL STAGE] WARNING: deferred old macOS backup cleanup: '
                      f'{exc}', flush=True)
        _progress('Stage mac complete (fresh signed app)', total, total)
        return len(files)
    except BaseException:
        if old_app_moved and backup.is_dir() and not backup.is_symlink():
            # Undo both a failed promotion and a failure of the installed
            # signature check. The partially promoted candidate is discarded.
            if promoted and old_app.is_dir() and not old_app.is_symlink():
                old_app.replace(candidate / app_name)
            if not old_app.exists() and not old_app.is_symlink():
                backup.replace(old_app)
                print('[TERMINAL STAGE] ROLLBACK restored previous signed macOS app',
                      flush=True)
        raise
    finally:
        shutil.rmtree(candidate, ignore_errors=True)


def stage_four(builds: Path, *, release: str = HOST_RELEASE,
               steamapps: Path | None = None, targets: tuple[str, ...] = FOUR_TARGETS) -> Path:
    """Reconcile four ZIPs in CFOS platform/product/version Steam directories."""
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', release):
        raise ValueError('Unsafe release identifier')
    builds = builds.resolve()
    out = builds / 'CinterForgeTerminal/Distribution'
    receipt_path = out / 'four-artifacts.json'
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise FileNotFoundError('Run ./Cinter build all before stage all: ' + str(receipt_path))
    report = json.loads(receipt_path.read_text())
    entries = report.get('artifacts', {})
    if (report.get('schema') != FOUR_SCHEMA or report.get('release') != release or
            set(entries) != set(FOUR_TARGETS)):
        raise RuntimeError('Stage requires exactly three native apps and one unified DevTools archive')
    if not targets or len(set(targets)) != len(targets) or not set(targets) <= set(FOUR_TARGETS):
        raise ValueError("Invalid Stage target selection")
    steam = steamapps_root(steamapps)
    print(f'[TERMINAL STAGE] Steam installation library: {steam}', flush=True)
    # Validate the selected final Build ZIPs before extracting anything.
    manifests = {}
    for target in targets:
        entry = entries[target]
        path = out / entry['filename']
        if target == 'devtools' and _stage_devtools_is_clean(
                _steam_release_path(steam, target, release), path, entry, release=release):
            manifests[target] = None  # Already verified and unmodified.
            print('[TERMINAL STAGE] devtools clean: skipping ZIP verification and extraction', flush=True)
            continue
        print(f'[TERMINAL STAGE] Checking {target} ZIP ({entry["bytes"] / 1048576:.1f} MiB)', flush=True)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != entry['bytes']:
            raise RuntimeError('Release ZIP size mismatch or missing: ' + str(path))
        if _hash(path, progress_label=f'Hash {target} ZIP') != entry['sha256']:
            raise RuntimeError('Release ZIP checksum mismatch: ' + str(path))
        manifests[target] = _verify_release_zip(path, target=target, release=release,
                                                progress_label=f'Verify {target}')
    total_updated = 0
    for target in targets:
        entry = entries[target]
        destination = _steam_release_path(steam, target, release)
        if destination.is_symlink():
            raise RuntimeError('Unsafe Steam installation destination: ' + str(destination))
        _move_legacy_target(steam, target, release, entry, destination)
        destination.mkdir(parents=True, exist_ok=True)
        root = destination
        if target == 'devtools' and manifests[target] is None:
            print(f'[TERMINAL STAGE] devtools unchanged -> {destination}', flush=True)
            continue
        files = manifests[target]['files']
        if target == 'mac':
            # Stage consumes the complete Build ZIP. Never merge an old signed
            # bundle's leftover resources into a new signed application.
            updated = _extract_mac_release(out / entry['filename'], root, files, builds=builds)
            total_updated += updated
            target_receipt = {'schema': FOUR_SCHEMA, 'release': release,
                              'target': target, 'artifact': entry,
                              'bedrock': 'excluded-until-app-eula-acceptance'}
            (root / 'stage-receipt.json').write_text(json.dumps(target_receipt, indent=2, sort_keys=True) + '\n')
            print(f'[TERMINAL STAGE] installed mac -> {destination}', flush=True)
            continue
        total_bytes = sum(item['bytes'] for item in files.values())
        scanned = 0
        updated = 0
        last_report = time.monotonic()
        _progress(f'Stage {target} start ({len(files)} files)', 0, total_bytes)
        with zipfile.ZipFile(out / entry['filename']) as source:
            for name, detail in files.items():
                installed = root / name
                directory = root
                for part in Path(name).parts[:-1]:
                    directory = directory / part
                    if directory.is_symlink():
                        raise RuntimeError('Unsafe Steam path redirect: ' + str(directory))
                    if directory.exists() and not directory.is_dir():
                        quarantine = builds / 'Intermediates/CinterForgeTerminal/SteamConflicts'
                        quarantine.mkdir(parents=True, exist_ok=True)
                        backup = Path(tempfile.mkdtemp(prefix='previous-', dir=quarantine)) / part
                        directory.replace(backup)
                    directory.mkdir(exist_ok=True)
                good = (installed.is_file() and not installed.is_symlink() and
                        installed.stat().st_size == detail['bytes'] and
                        _hash(installed) == detail['sha256'])
                if good and (installed.stat().st_mode & 0o777) != detail['mode']:
                    installed.chmod(detail['mode'])
                    updated += 1
                if not good:
                    if installed.is_dir() and not installed.is_symlink():
                        quarantine = builds / 'Intermediates/CinterForgeTerminal/SteamConflicts'
                        quarantine.mkdir(parents=True, exist_ok=True)
                        backup = Path(tempfile.mkdtemp(prefix='previous-', dir=quarantine)) / installed.name
                        installed.replace(backup)
                    elif installed.is_symlink():
                        installed.unlink()
                    fd, name_temp = tempfile.mkstemp(prefix='Staging-', dir=installed.parent)
                    os.close(fd)
                    partial = Path(name_temp)
                    try:
                        with source.open(name) as src, partial.open('wb') as dst:
                            digest = hashlib.sha256()
                            for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
                                dst.write(block)
                                digest.update(block)
                                now = time.monotonic()
                                if now - last_report >= 2:
                                    _progress(f'Stage {target}', scanned + dst.tell(), total_bytes)
                                    last_report = now
                        if digest.hexdigest() != detail['sha256']:
                            raise RuntimeError('Steam extraction SHA-256 mismatch: ' + name)
                        partial.chmod(detail['mode'])
                        partial.replace(installed)
                    finally:
                        partial.unlink(missing_ok=True)
                    updated += 1
                scanned += detail['bytes']
                now = time.monotonic()
                if now - last_report >= 2:
                    _progress(f'Stage {target}', scanned, total_bytes)
                    last_report = now
        if target == 'mac' and __import__('platform').system() == 'Darwin':
            # Ensure the extracted installed bundle remains signed. Archive
            # generation and extraction must not alter its resource seal.
            from host.mac_app import _verify_signature
            _verify_signature(root / 'CinterForge Terminal.app')
        target_receipt = {'schema': FOUR_SCHEMA, 'release': release,
                          'target': target, 'artifact': entry,
                          'bedrock': 'excluded-until-app-eula-acceptance'}
        if target == 'devtools':
            archive_stat = (out / entry['filename']).stat()
            target_receipt['archive_mtime_ns'] = archive_stat.st_mtime_ns
            target_receipt['archive_bytes'] = archive_stat.st_size
            target_receipt['installed_state_sha256'] = _installed_metadata_fingerprint(root, files)
            if not target_receipt['installed_state_sha256']:
                raise RuntimeError('DevTools installation changed during Stage')
        fd, name_temp = tempfile.mkstemp(prefix='Staging-', dir=root)
        os.close(fd)
        partial = Path(name_temp)
        try:
            partial.write_text(json.dumps(target_receipt, indent=2, sort_keys=True) + '\n')
            if (root / 'stage-receipt.json').is_dir():
                quarantine = builds / 'Intermediates/CinterForgeTerminal/SteamConflicts'
                quarantine.mkdir(parents=True, exist_ok=True)
                backup = Path(tempfile.mkdtemp(prefix='previous-', dir=quarantine)) / 'stage-receipt.json'
                (root / 'stage-receipt.json').replace(backup)
            partial.replace(root / 'stage-receipt.json')
        finally:
            partial.unlink(missing_ok=True)
        total_updated += updated
        _progress(f'Stage {target} complete ({updated} refreshed files)', total_bytes, total_bytes)
        print(f'[TERMINAL STAGE] installed {target} -> {destination}', flush=True)
    # Retire the *old receipt* only after selected archives are verified.
    # Never delete untracked old installation content; retain it if present.
    legacy = steam / 'common/CinterForge/Applications/CinterForge Terminal' / release
    old_receipt = legacy / 'stage-receipt.json'
    if old_receipt.is_file() and not old_receipt.is_symlink():
        try:
            old_data = json.loads(old_receipt.read_text())
            if (old_data.get('schema') == FOUR_SCHEMA and old_data.get('release') == release
                    and old_data.get('artifacts') == entries):
                old_receipt.unlink()
                if not any(legacy.iterdir()):
                    legacy.rmdir()
                    if not any(legacy.parent.iterdir()):
                        legacy.parent.rmdir()
        except (OSError, ValueError):
            pass
    print(f'[TERMINAL STAGE] complete: 3 apps + DevTools; {total_updated} refreshed files', flush=True)
    return steam / 'common/CinterForge/Applications'


def status_four(builds: Path, *, release: str = HOST_RELEASE,
                steamapps: Path | None = None) -> dict:
    """Read-only four-ZIP and platform-correct Steam installation audit."""
    directory = builds.resolve() / 'CinterForgeTerminal/Distribution'
    receipt = directory / 'four-artifacts.json'
    entries = {}
    if receipt.is_file() and not receipt.is_symlink():
        try:
            report = json.loads(receipt.read_text())
            if report.get('schema') == FOUR_SCHEMA and report.get('release') == release:
                entries = report.get('artifacts', {})
        except (OSError, ValueError):
            pass
    try:
        steam = steamapps_root(steamapps)
    except (OSError, ValueError, RuntimeError):
        steam = None
    state = {}
    for target in FOUR_TARGETS:
        entry = entries.get(target, {}) if isinstance(entries, dict) else {}
        filename = entry.get('filename') if isinstance(entry, dict) else None
        archive = directory / filename if isinstance(filename, str) and Path(filename).name == filename else None
        present = bool(archive and archive.is_file() and not archive.is_symlink())
        installed = _steam_release_path(steam, target, release) if steam else None
        stage_receipt = installed / 'stage-receipt.json' if installed else None
        stage_ok = False
        if stage_receipt and stage_receipt.is_file() and not stage_receipt.is_symlink():
            try:
                data = json.loads(stage_receipt.read_text())
                stage_ok = (data.get('schema') == FOUR_SCHEMA and data.get('release') == release
                            and data.get('target') == target and data.get('artifact', {}).get('sha256') == entry.get('sha256'))
            except (OSError, ValueError):
                pass
        payload = installed / APP_ROOTS[target] if installed else None
        state[target] = {'archive': str(archive) if archive else None,
                         'archive_present': present, 'bytes': archive.stat().st_size if present else None,
                         'size_matches_receipt': bool(present and archive.stat().st_size == entry.get('bytes')),
                         'steam_root': str(payload) if payload else None,
                         'steam_receipt_matches': stage_ok,
                         'steam_app_root_present': bool(stage_ok and payload.is_dir() and not payload.is_symlink())}
    return {'release': release, 'distribution': str(directory),
            'steam_destination': str(steam / 'common/CinterForge/Applications') if steam else None,
            'targets': state, 'complete_build_receipt': set(entries) == set(FOUR_TARGETS),
            'complete_stage_receipt': all(x['steam_receipt_matches'] for x in state.values())}


CLOUDFLARE_OBJECT_ROOT = 'CinterForgeTerminal/Distribution'
# This section is intentionally stable: ./Cinter updates it only after a
# fully verified PUBLIC publish or subsequent public HTTP verification.
README_DOWNLOADS_START = '<!-- CINTERFORGE:DOWNLOADS:START -->'
README_DOWNLOADS_END = '<!-- CINTERFORGE:DOWNLOADS:END -->'
LAGOMORPH_PUBLIC_NAME = 'Lagomorph-Quadra650.iso'
LAGOMORPH_FLOPPIES = ('Lagomorph-Application-1440K.img', 'Lagomorph-Battles-1440K.img')
LAGOMORPH_STAGE = Path('Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph')


def _cloudflare_config(config: Path | None = None, *, channel: str = 'dev') -> dict:
    """Companion-compatible private R2 default; public is an explicit choice."""
    if channel not in ('dev', 'public'):
        raise ValueError('Unknown publishing channel: ' + str(channel))
    from host.publishing import cloudflare_r2_sync as r2
    selected = (config or Path(os.environ.get('CINTERFORGE_CLOUDFLARE_CONFIG') or
                               os.environ.get('CINTER_CLOUDFLARE_CONFIG') or
                               str(Path.home() / 'cfdev/config.json'))).expanduser()
    bucket = 'cdn-assets-dev' if channel == 'dev' else 'cinterforge'
    return r2._load_config(selected, 'auto', required_bucket=bucket)


def _distribution_files(builds: Path, release: str) -> tuple[Path, dict, dict[str, Path]]:
    root = builds.resolve() / CLOUDFLARE_OBJECT_ROOT
    receipt_path = root / 'four-artifacts.json'
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise FileNotFoundError('Four-ZIP distribution must be built before publish: ' + str(receipt_path))
    report = json.loads(receipt_path.read_text())
    entries = report.get('artifacts', {})
    if report.get('schema') != FOUR_SCHEMA or report.get('release') != release or set(entries) != set(FOUR_TARGETS):
        raise RuntimeError('Publish requires exactly 3 native ZIPs and 1 unified DevTools ZIP')
    files = {'four-artifacts.json': receipt_path}
    for target, entry in entries.items():
        name = entry.get('filename')
        if not isinstance(name, str) or name != f'CinterForgeTerminal-{target}.zip':
            raise RuntimeError('Unsafe release ZIP name for ' + target)
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != entry.get('bytes'):
            raise RuntimeError('Missing or changed release ZIP: ' + str(path))
        if _hash(path) != entry.get('sha256'):
            raise RuntimeError('Release ZIP checksum mismatch: ' + str(path))
        _verify_release_zip(path, target=target, release=release, hash_files=False)
        files[name] = path
    return root, report, files


def _download_four_to(builds: Path, release: str, cfg: dict, destination: Path) -> dict:
    """R2 -> Builds with SHA checks and no partial/invalid live distribution."""
    from host.publishing import cloudflare_r2_sync as r2
    prefix = CLOUDFLARE_OBJECT_ROOT
    parent = builds.resolve() / 'Intermediates/CinterForgeTerminal/CloudflareDownloads'
    parent.mkdir(parents=True, exist_ok=True)
    remote = r2._list_remote(cfg, prefix)
    expected_names = {'four-artifacts.json'} | {f'CinterForgeTerminal-{t}.zip' for t in FOUR_TARGETS}
    if not expected_names <= set(remote):
        raise RuntimeError('Cloudflare release incomplete: ' + ', '.join(sorted(expected_names - set(remote))))
    staged = parent / release
    if staged.is_symlink():
        raise RuntimeError('Unsafe resumable download working directory: ' + str(staged))
    staged.mkdir(exist_ok=True)
    # Manifest first, no remote file paths are trusted as local paths.
    (staged / 'four-artifacts.json').unlink(missing_ok=True)
    r2._get_file(cfg, f'{prefix}/four-artifacts.json', staged / 'four-artifacts.json',
                 expected_size=int(remote['four-artifacts.json']['size']))
    record = json.loads((staged / 'four-artifacts.json').read_text())
    entries = record.get('artifacts', {})
    if record.get('schema') != FOUR_SCHEMA or record.get('release') != release or set(entries) != set(FOUR_TARGETS):
        raise RuntimeError('Cloudflare four-artifact manifest invalid')
    for index, target in enumerate(FOUR_TARGETS, 1):
        entry = entries[target]
        name = f'CinterForgeTerminal-{target}.zip'
        if entry.get('filename') != name or int(entry.get('bytes', -1)) != int(remote[name]['size']):
            raise RuntimeError('Cloudflare ZIP manifest/object size mismatch: ' + target)
        print(f'[TERMINAL PUBLISH] download {target} ({index}/4) '
              f'{entry["bytes"] / 1048576:.1f} MiB', flush=True)
        result = staged / name
        if result.is_file() and (result.stat().st_size != entry['bytes'] or
                                 _hash(result) != entry.get('sha256')):
            result.unlink()
        r2._get_file(cfg, f'{prefix}/{name}', result, expected_size=entry['bytes'])
        if _hash(result, progress_label=f'Cloudflare verify {target}') != entry.get('sha256'):
            raise RuntimeError('Cloudflare round-trip ZIP checksum mismatch: ' + name)
        _verify_release_zip(result, target=target, release=release, hash_files=False)
        print(f'[TERMINAL PUBLISH] verified {target} round-trip SHA-256', flush=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise RuntimeError('Unsafe Cloudflare download destination: ' + str(destination))
    destination.mkdir(parents=True, exist_ok=True)
    # Update managed distribution files, keep unrelated artifacts untouched.
    for name in sorted(expected_names):
        _replace_generated_output(staged / name, destination / name, builds)
    return record



def _lagomorph_public_disk(builds: Path) -> Path:
    """Select only the HFS content disk produced by Lagomorph Setup.

    This optional classic-game content is an *explicit public* publishing
    choice, not part of the four application/DevTools ZIPs or private dev sync.
    Never publish the original source, Apple OS/ROM, or neighboring assets.
    """
    from host.lagomorph_setup import IMAGE_NAME, MANIFEST_NAME, SCHEMA, _classic_hfs_signature
    folder = builds.resolve() / LAGOMORPH_STAGE
    disk = folder / IMAGE_NAME
    manifest = folder / MANIFEST_NAME
    if (folder.is_symlink() or disk.is_symlink() or manifest.is_symlink() or
            not disk.is_file() or not manifest.is_file()):
        raise FileNotFoundError('Public Lagomorph publishing requires a staged regular HFS disk and '
                                f'manifest in {folder}; run ./Cinter stage mac')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if (data.get('schema') != SCHEMA or data.get('image') != LAGOMORPH_PUBLIC_NAME or
            data.get('image_size') != disk.stat().st_size or
            data.get('image_sha256') != _hash(disk) or disk.stat().st_size < 32768):
        raise RuntimeError('Lagomorph staged ISO and image manifest do not match: ' + str(disk))
    if data.get('filesystem') != 'HFS Standard' or data.get('image_format') != 'raw-HFS-volume':
        raise RuntimeError('Lagomorph disk is the older incompatible HFS+ hybrid. '
                           'Run ./Cinter stage mac to create the classic-HFS replacement before publishing.')
    _classic_hfs_signature(disk)
    return disk



def _lagomorph_public_floppies(builds: Path) -> dict[str, Path]:
    """Only verified, owner-staged 1.44 MB HFS media; never an Apple OS image."""
    from host.lagomorph_setup import FLOPPY_BYTES, MANIFEST_NAME, _classic_hfs_signature
    folder = builds.resolve() / LAGOMORPH_STAGE
    manifest = folder / MANIFEST_NAME
    if manifest.is_symlink() or not manifest.is_file():
        raise FileNotFoundError('Lagomorph floppy manifest is missing; run ./Cinter stage mac')
    record = json.loads(manifest.read_text(encoding='utf-8'))
    entries = record.get('floppies')
    if not isinstance(entries, dict) or set(entries) != set(LAGOMORPH_FLOPPIES):
        raise RuntimeError('Lagomorph staged floppy manifest is incomplete')
    files = {}
    for name in LAGOMORPH_FLOPPIES:
        source = folder / name
        entry = entries[name]
        if (source.is_symlink() or not source.is_file() or
                source.stat().st_size != FLOPPY_BYTES or
                entry.get('bytes') != FLOPPY_BYTES or
                entry.get('sha256') != _hash(source)):
            raise RuntimeError('Lagomorph floppy bytes or SHA-256 do not match manifest: ' + str(source))
        _classic_hfs_signature(source)
        files[name] = source
    return files


def _download_lagomorph_public(cfg: dict, prefix: str, source_file: Path,
                               download_dir: Path) -> None:
    """R2 round-trip verification for the additional, public-only content disk."""
    from host.publishing import cloudflare_r2_sync as r2
    name = LAGOMORPH_PUBLIC_NAME
    remote = r2._list_remote(cfg, prefix)
    size = source_file.stat().st_size
    if name not in remote or int(remote[name].get('size', -1)) != size:
        raise RuntimeError('Public Lagomorph R2 object missing or size mismatch')
    download_dir.mkdir(parents=True, exist_ok=True)
    target = download_dir / name
    if target.is_symlink():
        raise RuntimeError('Unsafe Lagomorph round-trip target: ' + str(target))
    if target.is_file() and (target.stat().st_size != size or _hash(target) != _hash(source_file)):
        target.unlink()
    r2._get_file(cfg, f'{prefix}/{name}', target, expected_size=size)
    if _hash(target, progress_label='Cloudflare verify Lagomorph') != _hash(source_file):
        raise RuntimeError('Public Lagomorph R2 round-trip checksum mismatch')
    print(f'[TERMINAL PUBLISH] verified public Lagomorph disk SHA-256 ({size / 1048576:.1f} MiB)', flush=True)


def _verify_public_lagomorph_range(url: str, expected_size: int) -> None:
    """Check the browser-visible cross-origin single-byte HTTP range request.

    A successful R2 upload does not guarantee that Infinite Mac can fetch the
    disk from the public domain. The public origin must permit ranged reads
    from infinitemac.org, not just authenticated API access.
    """
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={
        'Origin': 'https://infinitemac.org', 'Range': 'bytes=0-0',
        'User-Agent': 'CinterForgeTerminal-CDROM-check/1',
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = getattr(response, 'status', response.getcode())
            headers = response.headers
            payload = response.read(2)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f'Public Lagomorph CD-ROM range fetch failed: {url}: {error}') from error
    allowed = headers.get('Access-Control-Allow-Origin', '')
    if (status != 206 or headers.get('Content-Range') != f'bytes 0-0/{expected_size}' or
            payload != b'' and len(payload) != 1 or
            allowed not in ('*', 'https://infinitemac.org')):
        raise RuntimeError('Public Lagomorph CD-ROM requires HTTP 206 byte ranges and '
                           f'CORS access for https://infinitemac.org: {url}')
    print(f'[TERMINAL PUBLISH] public Lagomorph range/CORS verified: {url}', flush=True)


def _release_public_urls(release: str, *, include_lagomorph: bool = False) -> dict[str, str]:
    """Use the published production origin already defined by the R2 publisher."""
    from host.publishing.release import PUBLIC_URLS
    base = PUBLIC_URLS['production'].rstrip('/')
    prefix = CLOUDFLARE_OBJECT_ROOT
    urls = {'manifest': f'{base}/{prefix}/four-artifacts.json'}
    urls.update({target: f'{base}/{prefix}/CinterForgeTerminal-{target}.zip'
                 for target in FOUR_TARGETS})
    if include_lagomorph:
        # Classic-Mac media keeps the existing versioned URL and is not one of the four ZIPs.
        urls['lagomorph'] = f'{base}/{prefix}/{release}/{LAGOMORPH_PUBLIC_NAME}'
    return urls


def update_readme_public_downloads(readme_path: Path, receipt: dict) -> bool:
    """Synchronize all four stable public ZIP links after verified publication.

    Called only by the public ./Cinter publishing commands. Dev/private R2
    uploads, pending CDN propagation, failed publishes, and a mere local Build
    must never advertise a new release. Source README changes are atomic and
    preserve every user-edited section outside the bounded download block.

    Returns True when the file changed, False when already current. No Git
    commit or push is performed by publication.
    """
    release = receipt.get('release')
    if (receipt.get('channel') != 'public' or
            receipt.get('bucket') != 'cinterforge' or
            receipt.get('public_http') != 'verified' or
            receipt.get('roundtrip') != 'sha256-verified' or
            not isinstance(release, str) or
            not re.fullmatch(r'[0-9]{4}\.[0-9]{2}\.[0-9]+', release)):
        raise ValueError('Refusing README update without a verified public release receipt')

    expected_urls = _release_public_urls(release, include_lagomorph=True)
    published_urls = receipt.get('public_urls')
    if not isinstance(published_urls, dict) or any(
            published_urls.get(target) != expected_urls[target]
            for target in ('manifest', 'mac', 'linux', 'win', 'devtools')):
        raise ValueError('Public receipt URL mismatch; README was not changed')

    readme_path = Path(readme_path)
    if not readme_path.is_file() or readme_path.is_symlink():
        raise FileNotFoundError('README is missing or unsafe: ' + str(readme_path))
    before = readme_path.read_text(encoding='utf-8')
    block = '\n'.join((
        README_DOWNLOADS_START,
        '## Download CinterForgeTerminal',
        '',
        f'Current public application archives (release `{release}`):',
        '',
        f'- [macOS — Apple silicon]({expected_urls["mac"]})',
        f'- [Windows — x64]({expected_urls["win"]})',
        f'- [Linux — x64]({expected_urls["linux"]})',
        f'- [Optional DevTools — all platforms]({expected_urls["devtools"]})',
        '',
        f'The [release manifest]({expected_urls["manifest"]}) contains checksums and includes the separate, optional DevTools archive. '
        '`./Cinter publish public` updates these links after public download verification; '
        'if CDN propagation is delayed, `./Cinter verify public` completes the update once verification succeeds.',
        README_DOWNLOADS_END,
    ))
    starts = before.count(README_DOWNLOADS_START)
    ends = before.count(README_DOWNLOADS_END)
    if starts == 1 and ends == 1:
        begin = before.index(README_DOWNLOADS_START)
        finish = before.index(README_DOWNLOADS_END)
        if begin >= finish:
            raise ValueError('Misordered README download markers')
        finish += len(README_DOWNLOADS_END)
        after = before[:begin] + block + before[finish:]
    elif starts == 0 and ends == 0:
        title = '# CinterForge Terminal\n'
        if not before.startswith(title):
            raise ValueError('Cannot insert download links: README heading differs')
        after = before.replace(title, title + '\n' + block + '\n', 1)
    else:
        raise ValueError('Incomplete or repeated README download markers')
    if before == after:
        return False

    # Stage the exact new text beside the README for an atomic, permissions-
    # preserving replacement. Do not create persistent caches in source.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                     prefix='CinterForgeTerminal-README-', suffix='.tmp',
                                     dir=readme_path.parent, delete=False) as handle:
        staged = Path(handle.name)
        try:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        staged.chmod(stat.S_IMODE(readme_path.stat().st_mode))
        staged.replace(readme_path)
    finally:
        staged.unlink(missing_ok=True)
    return True


def _public_http_headers() -> dict[str, str]:
    """Use conventional unsigned download headers, without R2 credentials."""
    return {
        'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/131.0.0.0 Safari/537.36'),
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }


def _public_http_attempt(label: str, url: str, expected_size: int,
                         expected_manifest_sha: str | None = None) -> None:
    """GET the manifest or perform a one-byte GET of an artifact.

    HEAD is insufficient: Cloudflare can allow HEAD while denying GET (or vice
    versa). Never download a whole release ZIP just to check its public URL.
    """
    import urllib.error
    import urllib.request
    headers = _public_http_headers()
    if label == 'manifest':
        headers['Cache-Control'] = 'no-cache'
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            status = getattr(response, 'status', response.getcode())
            blob = response.read(1024 * 1024 + 1)
            if status != 200 or len(blob) != expected_size or len(blob) > 1024 * 1024:
                raise RuntimeError(f'Public manifest HTTP {status} or size mismatch: {url}')
            if hashlib.sha256(blob).hexdigest() != expected_manifest_sha:
                raise RuntimeError('Public CDN returned a stale/incorrect release manifest: ' + url)
        return
    if label == 'lagomorph':
        _verify_public_lagomorph_range(url, expected_size)
        return
    headers['Range'] = 'bytes=0-0'
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        status = getattr(response, 'status', response.getcode())
        if status == 206:
            expected_range = f'bytes 0-0/{expected_size}'
            if response.headers.get('Content-Range') != expected_range:
                raise RuntimeError(f'Public ZIP range/length mismatch ({label}): {url}')
        elif status == 200:
            # Servers that ignore Range may still be valid download endpoints.
            # We read at most 2 bytes and require a matching full length.
            if response.headers.get('Content-Length') != str(expected_size):
                raise RuntimeError(f'Public ZIP full length missing/incorrect ({label}): {url}')
        else:
            raise RuntimeError(f'Public ZIP HTTP {status} ({label}): {url}')
        first = response.read(2)
        if not first or first[0:1] != b'P':
            raise RuntimeError(f'Public ZIP returned non-ZIP content ({label}): {url}')


def _verify_public_urls(urls: dict[str, str], report: dict, files: dict[str, Path],
                        *, retry_delays: tuple[float, ...] = (2, 4, 8, 12, 12, 12, 12)) -> None:
    """Verify *unsigned* public GET access, retrying short-lived CDN denials.

    All failed checks remain a publish failure (not a false success). Each
    attempt transfers the manifest or at most two ZIP bytes, never multi-GB.
    Public retries never call authenticated R2 upload/download functions.
    """
    import urllib.error
    from urllib.parse import urlsplit
    origin = urlsplit(urls['manifest'])
    if origin.scheme != 'https' or not origin.hostname:
        raise RuntimeError('Public release requires an HTTPS custom domain')
    for label, url in urls.items():
        dest = urlsplit(url)
        if (dest.scheme, dest.netloc) != (origin.scheme, origin.netloc):
            raise RuntimeError('Release public URL does not share verified origin')
    for label, url in urls.items():
        name = ('four-artifacts.json' if label == 'manifest' else
                LAGOMORPH_PUBLIC_NAME if label == 'lagomorph' else report['artifacts'][label]['filename'])
        expected_size = files[name].stat().st_size
        for attempt in range(len(retry_delays) + 1):
            try:
                _public_http_attempt(label, url, expected_size,
                                     _hash(files[name]) if label == 'manifest' else None)
                print(f'[TERMINAL PUBLISH] public HTTP GET verified: {label} {url}', flush=True)
                break
            except (OSError, urllib.error.URLError, ValueError, RuntimeError) as error:
                if attempt == len(retry_delays):
                    raise RuntimeError(
                        f'Public URL not verified after {attempt + 1} attempts: {label} {url}: {error}') from error
                delay = retry_delays[attempt]
                print(f'[TERMINAL PUBLISH] public {label} pending (attempt {attempt + 1}/'
                      f'{len(retry_delays) + 1}): {error}; retry in {delay:g}s', flush=True)
                time.sleep(delay)


def _write_publish_receipt(path: Path, receipt: dict) -> None:
    """Persist SHA-verified R2 state before potentially delayed public HTTP."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='cloudflare-',
                                     suffix='.json', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write('\n')
    temporary.replace(path)


def _verified_r2_cache(previous: dict, files: dict[str, Path],
                       remote: dict, cfg: dict, prefix: str, downloaded: Path) -> bool:
    """Reuse prior fully SHA-checked R2 round-trip, including pre-fix cache.

    A retry after an HTTP 403 must not re-upload (or re-download) a 3.6 GiB
    archive. A previous receipt or a complete, matching round-trip download
    establishes the bytes. R2 must still list every expected object at its
    unchanged size. Never reuse a cache for changed source bytes.
    """
    if any(name not in remote or int(remote[name].get('size', -1)) != source.stat().st_size
           for name, source in files.items()):
        return False
    if (previous.get('bucket') == cfg['bucket'] and previous.get('prefix') == prefix and
            previous.get('roundtrip') == 'sha256-verified' and
            previous.get('files') == {name: _hash(path) for name, path in files.items()}):
        return True
    # Older publisher versions verified the R2 downloads but lost their receipt
    # when the final public GET failed. They left their verified downloads here.
    for name, source in files.items():
        cached = downloaded / name
        if (not cached.is_file() or cached.is_symlink() or
                cached.stat().st_size != source.stat().st_size or _hash(cached) != _hash(source)):
            return False
    return True


def _finalize_public_receipt(receipt_path: Path, receipt: dict, report: dict,
                             files: dict[str, Path]) -> dict:
    urls = receipt['public_urls']
    try:
        _verify_public_urls(urls, report, files)
    except (OSError, ValueError, RuntimeError) as error:
        receipt['public_http'] = 'pending'
        receipt['public_error'] = str(error)
        _write_publish_receipt(receipt_path, receipt)
        print('[TERMINAL PUBLISH] R2 upload and SHA-256 round-trip VERIFIED. '
              'Public HTTP is pending; no re-upload is needed.', flush=True)
        raise RuntimeError(f'Public verification pending: {error}. '
                           'Retry without uploading with ./Cinter verify public') from error
    receipt['public_http'] = 'verified'
    receipt.pop('public_error', None)
    _write_publish_receipt(receipt_path, receipt)
    return receipt


def verify_public_four(builds: Path, *, release: str = HOST_RELEASE) -> dict:
    """Repeat only public HTTP checks for an already SHA-verified R2 release.

    This operation performs no R2 API calls, uploads, or large downloads.
    It requires the existing local verified release and publisher receipt.
    """
    builds = builds.resolve()
    receipt_path = builds / 'CinterForgeTerminal/Publish/Receipts/public' / release / 'cloudflare.json'
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RuntimeError('Missing SHA-verified public R2 receipt; publish first') from error
    if (receipt.get('schema') != FOUR_SCHEMA or receipt.get('channel') != 'public' or
            receipt.get('bucket') != 'cinterforge' or receipt.get('roundtrip') != 'sha256-verified' or
            receipt.get('release') != release):
        raise RuntimeError('Public R2 receipt does not represent a verified release')
    root = builds / CLOUDFLARE_OBJECT_ROOT
    manifest = root / 'four-artifacts.json'
    if not manifest.is_file() or manifest.is_symlink() or _hash(manifest) != receipt.get('files', {}).get(manifest.name):
        raise RuntimeError('Local release manifest does not match verified R2 receipt')
    report = json.loads(manifest.read_text())
    entries = report.get('artifacts', {})
    if report.get('schema') != FOUR_SCHEMA or report.get('release') != release or set(entries) != set(FOUR_TARGETS):
        raise RuntimeError('Invalid local four-ZIP release manifest')
    files = {manifest.name: manifest}
    for target, item in entries.items():
        name = f'CinterForgeTerminal-{target}.zip'
        path = root / name
        if (item.get('filename') != name or not path.is_file() or path.is_symlink() or
                path.stat().st_size != item.get('bytes') or
                receipt.get('files', {}).get(name) != item.get('sha256')):
            raise RuntimeError('Local ZIP does not match verified R2 receipt: ' + name)
        files[name] = path
    iso = builds / LAGOMORPH_STAGE / LAGOMORPH_PUBLIC_NAME
    if (not iso.is_file() or iso.is_symlink() or
            _hash(iso) != receipt.get('files', {}).get(iso.name)):
        raise RuntimeError('Lagomorph ISO does not match verified R2 receipt')
    files[iso.name] = iso
    receipt['public_urls'] = _release_public_urls(release, include_lagomorph=True)
    return _finalize_public_receipt(receipt_path, receipt, report, files)


def publish_four(builds: Path, *, release: str = HOST_RELEASE,
                 config: Path | None = None, channel: str = 'dev') -> dict:
    """Explicit channel: private dev by default, public only by command choice.

    The public command uploads the same four archives including DevTools;
    the release operator is responsible for redistribution rights and native
    acceptance before publishing to the production bucket.
    """
    from host.publishing import cloudflare_r2_sync as r2
    builds = builds.resolve()
    source, report, files = _distribution_files(builds, release)
    if channel == 'public':
        # Explicit user-authorized publication of only the staged content disk.
        # Keep four-artifacts.json exactly four ZIPs; the ISO is a sidecar.
        files[LAGOMORPH_PUBLIC_NAME] = _lagomorph_public_disk(builds)
    if os.environ.get('CINTERFORGE_SETUP_DISABLE_UPLOADS') == '1':
        raise RuntimeError('Publishing disabled on this setup/test machine')
    cfg = _cloudflare_config(config, channel=channel)
    prefix = CLOUDFLARE_OBJECT_ROOT
    remote = r2._list_remote(cfg, prefix)
    if channel == 'public':
        # The Lagomorph ISO is a separate versioned sidecar, not a stable app ZIP.
        legacy_media_prefix = f'{prefix}/{release}'
        media_remote = r2._list_remote(cfg, legacy_media_prefix)
    else:
        legacy_media_prefix = None
        media_remote = {}
    print(f'[TERMINAL PUBLISH] upload 4 ZIPs + manifest'
          f'{" + Lagomorph ISO" if channel == "public" else ""} -> {cfg["bucket"]}/{prefix}', flush=True)
    total_bytes = sum(x.stat().st_size for x in files.values())
    uploaded = 0
    previous_receipt = builds / 'CinterForgeTerminal/Publish/Receipts' / channel / release / 'cloudflare.json'
    try:
        previous = json.loads(previous_receipt.read_text())
    except (OSError, ValueError):
        previous = {}
    downloaded = builds / 'CinterForgeTerminal/Publish/Downloaded' / channel / release
    remote_sources = dict(remote)
    if channel == 'public':
        remote_sources[LAGOMORPH_PUBLIC_NAME] = media_remote.get(LAGOMORPH_PUBLIC_NAME, {})
    reusable = _verified_r2_cache(previous, files, remote_sources, cfg, prefix, downloaded)
    if reusable:
        print('[TERMINAL PUBLISH] unchanged release has an authenticated SHA-256 R2 '
              'round-trip; skipping all uploads/downloads', flush=True)
        verified = report
    else:
        # Always upload the manifest last: clients never see a new manifest
        # pointing to ZIPs that have not yet been uploaded.
        ordered = [(name, path) for name, path in sorted(files.items())
                   if name != 'four-artifacts.json']
        ordered.append(('four-artifacts.json', files['four-artifacts.json']))
        for index, (name, source_file) in enumerate(ordered, 1):
            object_prefix = (legacy_media_prefix if name == LAGOMORPH_PUBLIC_NAME
                             else prefix)
            object_remote = (media_remote if name == LAGOMORPH_PUBLIC_NAME
                             else remote)
            size = source_file.stat().st_size
            md5 = str(object_remote.get(name, {}).get('etag', '')).strip('"').lower()
            prior_sha = previous.get('files', {}).get(name)
            same_verified = (previous.get('bucket') == cfg['bucket'] and
                             previous.get('prefix') == prefix and prior_sha == _hash(source_file) and
                             int(object_remote.get(name, {}).get('size', -1)) == size)
            same_md5 = (len(md5) == 32 and all(ch in '0123456789abcdef' for ch in md5)
                        and r2._md5_file(source_file) == md5)
            if not same_verified and not same_md5:
                print(f'[TERMINAL PUBLISH] upload {index}/{len(files)} {name} '
                      f'{size / 1048576:.1f} MiB', flush=True)
                r2._put_file(cfg, f'{object_prefix}/{name}', source_file,
                             uploaded_before=uploaded, upload_total=total_bytes)
            else:
                print(f'[TERMINAL PUBLISH] remote object verified/reusable: {name}', flush=True)
            uploaded += size
            _progress('Cloudflare upload accounted', uploaded, total_bytes)
        # R2 round-trip SHA-256 must complete before recording a reusable receipt.
        verified = _download_four_to(builds, release, cfg, downloaded)
        if verified != report:
            raise RuntimeError('Cloudflare round-trip release manifest differs')
        if channel == 'public':
            _download_lagomorph_public(cfg, legacy_media_prefix, files[LAGOMORPH_PUBLIC_NAME], downloaded)
    public_urls = _release_public_urls(release, include_lagomorph=True) if channel == 'public' else {}
    receipt = {'schema': FOUR_SCHEMA, 'release': release, 'channel': channel, 'bucket': cfg['bucket'],
               'prefix': prefix, 'files': {name: _hash(path) for name, path in files.items()},
               'roundtrip': 'sha256-verified', 'downloaded': str(downloaded),
               'public_urls': public_urls,
               'public_http': 'pending' if channel == 'public' else 'not-applicable'}
    # Commit R2 success BEFORE the public custom-domain probe. On an HTTP 403
    # the operator must be able to retry without another 3.6 GiB transfer.
    _write_publish_receipt(previous_receipt, receipt)
    if channel == 'public':
        receipt = _finalize_public_receipt(previous_receipt, receipt, report, files)
    print(f'[TERMINAL PUBLISH] VERIFIED 4 ZIPs + manifest'
          f'{" + Lagomorph ISO" if channel == "public" else ""} in {cfg["bucket"]}/{prefix}', flush=True)
    if channel == 'public':
        print('[TERMINAL PUBLISH] PUBLIC TEST URLS (verified unsigned HTTP):', flush=True)
        for target, url in public_urls.items():
            print(f'  {target}: {url}', flush=True)
    return receipt



def publish_lagomorph(builds: Path, *, release: str = HOST_RELEASE,
                      config: Path | None = None) -> dict:
    """Explicit Lagomorph HFS-media publish. Never transfers four app ZIPs.

    Upload the owner-staged content disk, verify the R2 round-trip by SHA-256,
    then probe the *unsigned* public URL for the byte ranges and CORS needed by
    the browser emulator. A receipt is separate from the four-package release.
    """
    from host.publishing import cloudflare_r2_sync as r2
    if os.environ.get('CINTERFORGE_SETUP_DISABLE_UPLOADS') == '1':
        raise RuntimeError('Publishing disabled on this setup/test machine')
    builds = builds.resolve()
    source = _lagomorph_public_disk(builds)
    floppies = _lagomorph_public_floppies(builds)
    # Source belongs ONLY on the internal CDN. The public bucket continues to
    # receive the original verified ISO and two HFS floppies, never source.
    from host.lagomorph_cloudflare import publish_private_source
    publish_private_source(builds, release=release, config=config,
                           media={source.name: source, **floppies})
    source_size = source.stat().st_size
    source_sha = _hash(source, progress_label='Hash Lagomorph ISO')
    cfg = _cloudflare_config(config, channel='public')
    prefix = f'{CLOUDFLARE_OBJECT_ROOT}/{release}'
    key = f'{prefix}/{LAGOMORPH_PUBLIC_NAME}'
    from host.publishing.release import PUBLIC_URLS
    url = f'{PUBLIC_URLS["production"].rstrip("/")}/{key}'
    receipt_path = builds / 'CinterForgeTerminal/Publish/Receipts/public' / release / 'lagomorph.json'
    try:
        prior = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        prior = {}
    remote = r2._list_remote(cfg, prefix)
    entry = remote.get(LAGOMORPH_PUBLIC_NAME, {})
    same_size = int(entry.get('size', -1)) == source_size
    etag = str(entry.get('etag', '')).strip('"').lower()
    known_sha = (prior.get('bucket') == cfg['bucket'] and prior.get('key') == key and
                 prior.get('sha256') == source_sha and prior.get('bytes') == source_size)
    etag_md5 = (len(etag) == 32 and all(c in '0123456789abcdef' for c in etag) and
                 r2._md5_file(source) == etag)
    if same_size and (known_sha or etag_md5):
        print(f'[LAGOMORPH PUBLISH] reusing matching public R2 object ({source_size / 1048576:.1f} MiB)', flush=True)
    else:
        print(f'[LAGOMORPH PUBLISH] uploading HFS disk ({source_size / 1048576:.1f} MiB) '
              f'-> {cfg["bucket"]}/{key}', flush=True)
        r2._put_file(cfg, key, source, uploaded_before=0, upload_total=source_size)
    downloaded = builds / 'CinterForgeTerminal/Publish/Downloaded/public' / release
    _download_lagomorph_public(cfg, prefix, source, downloaded)
    # Re-read the object over the public HTTP origin; authenticated R2 alone is
    # insufficient for cross-origin Infinite Mac media attachment.
    print(f'[LAGOMORPH PUBLISH] checking public URL: {url}', flush=True)
    _verify_public_lagomorph_range(url, source_size)
    # Publishing Lagomorph explicitly also publishes its TWO separate floppy
    # tests. The four application/DevTools ZIPs are never touched.
    floppy_receipts = {}
    for name, floppy in floppies.items():
        floppy_key = f'{prefix}/{name}'
        floppy_url = f'{PUBLIC_URLS["production"].rstrip("/")}/{floppy_key}'
        size = floppy.stat().st_size
        digest = _hash(floppy)
        r2._put_file(cfg, floppy_key, floppy, uploaded_before=0, upload_total=size)
        downloaded.mkdir(parents=True, exist_ok=True)
        roundtrip = downloaded / name
        if roundtrip.is_symlink():
            raise RuntimeError('Unsafe Lagomorph floppy download target: ' + str(roundtrip))
        r2._get_file(cfg, floppy_key, roundtrip, expected_size=size)
        if _hash(roundtrip) != digest:
            raise RuntimeError('Lagomorph floppy failed R2 SHA-256 round-trip: ' + name)
        _verify_public_lagomorph_range(floppy_url, size)
        floppy_receipts[name] = {'url': floppy_url, 'bytes': size, 'sha256': digest}
        print(f'[LAGOMORPH PUBLISH] verified 1.44 MB HFS floppy: {floppy_url}', flush=True)
    receipt = {'release': release, 'bucket': cfg['bucket'], 'key': key, 'url': url,
               'floppies': floppy_receipts,
               'bytes': source_size, 'sha256': source_sha,
               'downloaded': str(downloaded / LAGOMORPH_PUBLIC_NAME),
               'public_http': 'verified-206-range-cors'}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='lagomorph-', suffix='.json',
                                     dir=receipt_path.parent, delete=False) as handle:
        temp = Path(handle.name)
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write('\n')
    temp.replace(receipt_path)
    print(f'[LAGOMORPH PUBLISH] VERIFIED public ISO URL: {url}', flush=True)
    print(f'[LAGOMORPH PUBLISH] receipt: {receipt_path}', flush=True)
    return receipt


def fetch_four(builds: Path, *, release: str = HOST_RELEASE,
               config: Path | None = None, channel: str = 'dev') -> dict:
    """Companion-style fetch: chosen Cloudflare bucket -> Builds Distribution."""
    cfg = _cloudflare_config(config, channel=channel)
    downloaded = builds.resolve() / CLOUDFLARE_OBJECT_ROOT
    return _download_four_to(builds, release, cfg, downloaded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--builds", type=Path, required=True)
    parser.add_argument("--stage", action="store_true", help="verify and install the four ZIPs into Steam")
    parser.add_argument("--steamapps", type=Path, help="existing Steam steamapps root")
    args = parser.parse_args()
    result = (stage_four(args.builds, steamapps=args.steamapps) if args.stage
              else build_four(args.builds))
    print(f"[TERMINAL FOUR ARTIFACTS] {result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
