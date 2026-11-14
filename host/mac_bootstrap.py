"""Clean macOS bootstrap: Xcode is the *only* presumed preinstallation.

The Python interpreter used to start ./Cinter is the Xcode Python shim.
During Setup, a conventional micromamba-managed prefix in sibling Builds
provisions pinned Python, Node, CMake, Ninja, pkg-config, Colima,
a Docker client and the Buildx/BuildKit CLI plugin. A verified Lima distribution supplies the macOS VM backend.
VM state, tools, caches and installer archives remain inside sibling Builds.
Docker Desktop and Homebrew are not prerequisites. Never tell a user to run brew or install MinGW/Wine.
"""
from __future__ import annotations

import json
import base64
import hashlib
import os
import plistlib
import shlex
from pathlib import Path
import platform
import shutil
import sys
import subprocess
import tarfile
import tempfile
import time
import urllib.request

MAMBA_URL = 'https://micro.mamba.pm/api/micromamba/osx-arm64/latest'
LIMA_VERSION = '2.3.0'
LIMA_URL = f'https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}/lima-{LIMA_VERSION}-Darwin-arm64.tar.gz'
LIMA_SHA256 = 'b7af444bd8f91d3c307477e562f2cf3bd7595d62b9429185e4fa841563578cfc'
LIMA_AGENTS_URL = f'https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}/lima-additional-guestagents-{LIMA_VERSION}-Darwin-arm64.tar.gz'
LIMA_AGENTS_SHA256 = 'f43a1523a3a0a45692ca9cb3e5c61ae11b69ef04ca80081e8e05fe4df816a35a'
# Prebuilt native macOS ARM64 QEMU closure; this specific immutable npm release
# ships both qemu-system-x86_64 (TCG) and qemu-img, plus bundled dylibs/firmware.
# Integrity taken from the publisher's versioned package-lock.json, not latest.
QEMU_PACKAGE = 'qemu-portable-darwin-arm64'
QEMU_VERSION = '0.2.1'
QEMU_URL = (f'https://registry.npmjs.org/{QEMU_PACKAGE}/-/'
            f'{QEMU_PACKAGE}-{QEMU_VERSION}.tgz')
QEMU_SHA512_BASE64 = 'e0ZfgaL9NzYBmVV8TNFH1ZJqUFfyO4vvpbWAO7AizN6m3nwlM3a6/uiQYisVBUa/LSJK8+JzbJjw3QsnQu+0MQ=='
COLIMA_PROFILE = 'c'
# The one disposable producer is the LinuxKit c backend. Never start the old w VM.
# Mac's sockaddr_un.sun_path is 104 bytes INCLUDING the terminal NUL.
SOCKET_PATH_LIMIT = 104
# Colima/Lima must use physical, short paths, not symlink aliases.  Keep the
# guest/state closure under sibling Builds, separate from the larger toolchain.
VM_STATE_FOLDER = 'CFTVM/U'
PACKAGES = ('python=3.11', 'cmake=3.31', 'ninja=1.12', 'nodejs=22',
            'pkg-config', 'go=1.24', 'colima=0.10.3', 'docker-cli=29.8.1',
            'docker-buildx=0.35.0', 'git-lfs=3.8.0', '7zip=26.01',
            'skopeo=1.24.1', 'msitools=0.106')
SCHEMA = 'cinterforge.terminal.mac-bootstrap.v1'


def _run(command: list[str], **kwargs) -> None:
    print('[MAC BOOTSTRAP] $ ' + ' '.join(map(str, command)), flush=True)
    subprocess.run([str(c) for c in command], check=True, **kwargs)


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + '.part')
    print('[MAC BOOTSTRAP] acquiring ' + url, flush=True)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'CinterForgeTerminal-Bootstrap/1.0'})
        with urllib.request.urlopen(req, timeout=120) as source, temp.open('wb') as sink:
            total = int(source.headers.get('Content-Length') or '0')
            received = 0
            last_report = time.monotonic()
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                sink.write(block)
                received += len(block)
                now = time.monotonic()
                if now - last_report >= 3:
                    status = (f'{100 * received / total:.1f}%' if total else
                              f'{received / (1024 * 1024):.1f} MiB')
                    print('[MAC BOOTSTRAP] download ' + status + ' — ' + destination.name,
                          flush=True)
                    last_report = now
            print('[MAC BOOTSTRAP] download complete: ' + destination.name +
                  f' ({received / (1024 * 1024):.1f} MiB)', flush=True)
        if not temp.stat().st_size:
            raise RuntimeError('Downloaded archive is empty: ' + url)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _sha512_matches(path: Path, expected: str) -> bool:
    digest = hashlib.sha512()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.digest() == base64.b64decode(expected, validate=True)


def _verify_qemu(qemu_root: Path) -> dict[str, str]:
    """Execute both prebuilt emulators; don't accept empty npm placeholders."""
    binaries = {'qemu-img': qemu_root / 'bin/qemu-img',
                'qemu-system-x86_64': qemu_root / 'bin/qemu-system-x86_64'}
    if not (qemu_root / 'share/qemu').is_dir():
        raise RuntimeError('Prebuilt QEMU is missing its firmware/data closure')
    for name, path in binaries.items():
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 1024:
            raise RuntimeError(f'Prebuilt QEMU missing real native executable: {path}')
        if not os.access(path, os.X_OK):
            raise RuntimeError(f'Prebuilt QEMU executable has no execute permission: {path}')
        # macOS arm64 prebuilt binaries must really execute on Apple Silicon.
        # We don't assume the npm package's description is proof of support.
        _run([str(path), '--version'], timeout=35)
    _run([str(binaries['qemu-system-x86_64']), '-machine', 'help'], timeout=35)
    return {name: str(path) for name, path in binaries.items()}


def ensure_qemu(builds: Path) -> dict[str, str]:
    """Install pinned, hash-verified prebuilt QEMU into sibling Builds only.

    Do not install Homebrew, build QEMU, start a VM, or modify system paths.
    The entire portable bin/lib/share closure is retained for unified DevTools.
    """
    root = builds / 'CinterForgeTerminal/Toolchains/mac'
    stage = root / 'qemu'
    receipt = stage / 'cinter-qemu-receipt.json'
    expected_receipt = {'package': QEMU_PACKAGE, 'version': QEMU_VERSION,
                        'integrity': 'sha512-' + QEMU_SHA512_BASE64}
    if receipt.is_file():
        try:
            current = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            current = None
        if current == expected_receipt:
            return _verify_qemu(stage)
    archive = root / 'downloads' / f'{QEMU_PACKAGE}-{QEMU_VERSION}.tgz'
    if not archive.is_file():
        from host.setup_progress import progress_step
        with progress_step('download prebuilt macOS ARM64 QEMU package',
                           prefix='[MAC BOOTSTRAP]', interval_seconds=10):
            _download(QEMU_URL, archive)
    if not _sha512_matches(archive, QEMU_SHA512_BASE64):
        raise RuntimeError('Pinned macOS ARM64 QEMU package SHA-512 mismatch: ' + str(archive))
    root.mkdir(parents=True, exist_ok=True)
    # Extract only regular files in package/, rejecting symlinks, hardlinks,
    # absolute paths and parent traversal. Never run npm installation hooks.
    print('[MAC BOOTSTRAP] extracting verified QEMU package into Setup toolchain',
          flush=True)
    temporary = Path(tempfile.mkdtemp(prefix='qemu-stage-', dir=str(root)))
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar:
                rel = Path(member.name)
                if len(rel.parts) < 2 or rel.parts[0] != 'package' or '..' in rel.parts:
                    raise RuntimeError('Unsafe QEMU archive member: ' + member.name)
                destination = temporary.joinpath(*rel.parts[1:])
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    content = tar.extractfile(member)
                    if content is None:
                        raise RuntimeError('Cannot extract QEMU package member: ' + member.name)
                    with content, destination.open('wb') as sink:
                        shutil.copyfileobj(content, sink, length=1024 * 1024)
                    destination.chmod(member.mode & 0o777)
                else:
                    raise RuntimeError('QEMU package has unsupported links or special files: ' + member.name)
        package_file = temporary / 'package.json'
        if not package_file.is_file():
            raise RuntimeError('Prebuilt QEMU archive has no package metadata')
        metadata = json.loads(package_file.read_text(encoding='utf-8'))
        if metadata.get('name') != QEMU_PACKAGE or metadata.get('version') != QEMU_VERSION:
            raise RuntimeError('Prebuilt QEMU archive package/version mismatch')
        _verify_qemu(temporary)
        if stage.exists():
            if stage.is_symlink() or not stage.is_dir():
                raise RuntimeError('Unsafe managed QEMU stage directory: ' + str(stage))
            shutil.rmtree(stage)  # Setup-owned, generated, verified from pinned tarball
        temporary.replace(stage)
        receipt.write_text(json.dumps(expected_receipt, indent=2) + '\n', encoding='utf-8')
        print('[MAC BOOTSTRAP] PASS: pinned prebuilt QEMU ARM64 hosts x86-64 TCG guest; '
              'both qemu-img and qemu-system-x86_64 execute', flush=True)
        return {name: str(stage / 'bin' / name) for name in ('qemu-img', 'qemu-system-x86_64')}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _mamba(builds: Path) -> Path:
    toolchain = builds / 'CinterForgeTerminal/Toolchains/mac'
    executable = toolchain / 'mamba/bin/micromamba'
    if executable.is_file():
        return executable
    archive = toolchain / 'downloads/micromamba-osx-arm64.tar.bz2'
    if not archive.is_file():
        _download(MAMBA_URL, archive)
    executable.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:bz2') as tar:
        members = [m for m in tar.getmembers() if m.name == 'bin/micromamba' and m.isfile()]
        if len(members) != 1:
            raise RuntimeError('Official micromamba archive has no bin/micromamba')
        stream = tar.extractfile(members[0])
        if stream is None:
            raise RuntimeError('Cannot read micromamba executable')
        with stream, executable.open('wb') as target:
            shutil.copyfileobj(stream, target)
    executable.chmod(0o755)
    return executable


def ensure_macos_tools(builds: Path) -> dict[str, str]:
    if platform.system() != 'Darwin':
        raise RuntimeError('Mac bootstrapper is macOS-only')
    if not shutil.which('xcrun'):
        raise RuntimeError('Full Xcode/Command Line Tools required before setup')
    tools = builds / 'CinterForgeTerminal/Toolchains/mac'
    prefix = tools / 'env'
    mamba = _mamba(builds)
    expected = {'cmake': prefix / 'bin/cmake', 'ninja': prefix / 'bin/ninja',
                'python': prefix / 'bin/python3', 'node': prefix / 'bin/node',
                'go': prefix / 'bin/go',
                'pkg-config': prefix / 'bin/pkg-config',
                'colima': prefix / 'bin/colima', 'docker': prefix / 'bin/docker',
                'buildx': prefix / 'bin/docker-buildx',
                'git-lfs': prefix / 'bin/git-lfs',
                'skopeo': prefix / 'bin/skopeo',
                'wixl': prefix / 'bin/wixl', 'msiinfo': prefix / 'bin/msiinfo',
                'msiextract': prefix / 'bin/msiextract'}
    if not all(binary.is_file() for binary in expected.values()):
        if prefix.exists():
            # Let the package manager reconcile a partially populated prefix;
            # do not delete the user's Python/cache state on retries.
            command = ['install', '--yes', '--prefix', str(prefix)]
        else:
            command = ['create', '--yes', '--prefix', str(prefix)]
        _run([str(mamba), *command, '--channel', 'conda-forge', *PACKAGES],
             env=dict(os.environ, MAMBA_ROOT_PREFIX=str(tools / 'mamba-cache')))
    for name, path in expected.items():
        if not path.is_file():
            raise RuntimeError(f'Clean-Mac tool provisioning incomplete: {name} absent at {path}')
        # Go and Docker Buildx use the `version` subcommand, not `--version`.
        _run([str(path), 'version' if name in ('go', 'buildx') else
              '--help' if name in ('msiinfo', 'msiextract') else '--version'])
    if not any((prefix / 'bin' / name).is_file() for name in ('7zz', '7z')):
        # Some 7zip builds use 7zz and others use 7z. Both can expand CAB.
        _run([str(mamba), 'install', '--yes', '--prefix', str(prefix),
              '--channel', 'conda-forge', '7zip=26.01'],
             env=dict(os.environ, MAMBA_ROOT_PREFIX=str(tools / 'mamba-cache')))
    if not any((prefix / 'bin' / name).is_file() for name in ('7zz', '7z')):
        raise RuntimeError('Windows Fixed Runtime CAB extraction needs staged 7zip executable')
    _run(['xcrun', '--find', 'clang++'])
    result = {name: str(path) for name, path in expected.items()}
    # Persist the precise resolved transitive Conda dependency set: requested
    # top-level packages alone are not an offline closure.
    locked = subprocess.run([str(mamba), 'env', 'export', '--explicit',
                             '--prefix', str(prefix)],
                            check=True, text=True, stdout=subprocess.PIPE,
                            env=dict(os.environ, MAMBA_ROOT_PREFIX=str(tools / 'mamba-cache')))
    if '@EXPLICIT' not in locked.stdout or 'conda' not in locked.stdout.lower():
        raise RuntimeError('Micromamba did not produce an explicit transitive package lock')
    (tools / 'mac-packages-explicit.txt').write_text(locked.stdout, encoding='utf-8')
    output = tools / 'bootstrap.json'
    output.write_text(json.dumps({'schema': SCHEMA, 'tools': result,
        'packages': PACKAGES, 'xcode_assumed': True}, indent=2) + '\n')
    return result


def _probe(command: list[str], *, env: dict[str, str] | None = None,
           timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run([str(part) for part in command], check=False,
                          env=env, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


def managed_state_root(builds: Path) -> Path:
    """Validate the *actual* short LinuxKit c state tree in sibling Builds.

    Keep the root independent of a live VM so Setup can prove its socket
    budget before starting Colima or allocating any generated state.
    """
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError(f'Expected existing sibling Builds: {builds}')
    state = builds / VM_STATE_FOLDER
    socket = state / 'l' / ('colima-' + COLIMA_PROFILE) / 'ssh.sock.1234567890123456'
    if len(os.fsencode(socket)) >= SOCKET_PATH_LIMIT:
        raise RuntimeError(f'Managed Lima socket would be {len(os.fsencode(socket))} bytes '
                           f'(limit {SOCKET_PATH_LIMIT}): {socket}')
    for part in (builds / 'CFTVM', state, state / 'l', state / 'c',
                 state / 'docker'):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise RuntimeError(f'Managed VM state must be a real directory: {part}')
    return state


def managed_environment(builds: Path) -> dict[str, str]:
    """Expose LinuxKit's c producer Docker context to all Setup/Build clients.

    The shared producer is the only container *builder*: Windows and Linux
    cross images are built here sequentially after Bedrock. The installed
    Terminal uses its independent qemu/x86-64 t runtime, never another w VM.
    """
    from linuxos.managed_backend import backend_environment, source_directory
    repo = Path(__file__).resolve().parents[1]
    state = managed_state_root(builds)
    env = backend_environment(source_directory(repo), create=False)
    if (Path(env['LIMA_HOME']) != state / 'l' or
            Path(env['COLIMA_HOME']) != state / 'c' or
            Path(env['DOCKER_CONFIG']) != state / 'docker'):
        raise RuntimeError('LinuxKit c backend does not match the supplied sibling Builds root')
    tools = builds.resolve() / 'CinterForgeTerminal/Toolchains/mac'
    env['PATH'] = os.pathsep.join((str(tools / 'qemu/bin'),
                                  str(tools / 'linuxkit/bin'),
                                  str(tools / 'lima/bin'), str(tools / 'env/bin'),
                                  env.get('PATH', '')))
    return env


def _stage_lima(builds: Path) -> Path:
    """Install official SHA-256 pinned Lima + guest agents entirely in Builds."""
    root = builds / 'CinterForgeTerminal/Toolchains/mac'
    stage = root / 'lima'
    binpath = stage / 'bin/limactl'
    sources = ((LIMA_URL, LIMA_SHA256, 'lima'),
               (LIMA_AGENTS_URL, LIMA_AGENTS_SHA256, 'guestagents'))
    for url, expected, label in sources:
        archive = root / 'downloads' / (f'{label}-{LIMA_VERSION}-arm64.tar.gz')
        if not archive.is_file():
            _download(url, archive)
        if _file_sha256(archive) != expected:
            raise RuntimeError(f'{label} archive integrity failure: {archive}')
        marker = stage / (label + '.sha256')
        if marker.is_file() and marker.read_text().strip() == expected:
            continue
        stage.mkdir(parents=True, exist_ok=True)
        # Trusted, pinned official release; tar preserves its executable modes,
        # templates and helper locations required by Lima (not just limactl).
        _run(['tar', '-xzf', str(archive), '-C', str(stage)])
        marker.write_text(expected + '\n', encoding='utf-8')
    if not binpath.is_file():
        raise RuntimeError(f'Complete Lima executable missing: {binpath}')
    _ensure_lima_vz_entitlement(binpath)
    return binpath


def _lima_signature_state(binary: Path) -> tuple[bool, dict[str, object]]:
    """Inspect both the signature and effective entitlements of managed limactl."""
    codesign = '/usr/bin/codesign'
    check = subprocess.run([codesign, '--verify', '--strict', str(binary)],
                           capture_output=True, check=False)
    display = subprocess.run([codesign, '--display', '--entitlements', ':-', str(binary)],
                             capture_output=True, check=False)
    entitlements: dict[str, object] = {}
    if display.returncode == 0 and display.stdout.strip():
        try:
            parsed = plistlib.loads(display.stdout)
        except (ValueError, TypeError, plistlib.InvalidFileException) as error:
            raise RuntimeError(f'Cannot inspect Lima entitlements: {binary}') from error
        if not isinstance(parsed, dict):
            raise RuntimeError(f'Unexpected Lima entitlements format: {binary}')
        entitlements = parsed
    return check.returncode == 0, entitlements


def _ensure_lima_vz_entitlement(binary: Path) -> None:
    """Setup owns the entitlement required by the Lima VZ host-agent process.

    The pinned official archive is SHA-256 verified before this routine runs.
    macOS does not grant Virtualization.framework access based on filesystem
    location, executable permission, or another application's entitlements.
    The actual limactl host-agent executable must carry the entitlement.
    Repair only the Builds-owned Lima executable, preserving other entitlements;
    never modify a system/Homebrew Lima installation or a release package.
    """
    if not binary.is_file() or binary.is_symlink():
        raise RuntimeError(f'Managed Lima limactl must be a regular file: {binary}')
    verified, existing = _lima_signature_state(binary)
    if verified and existing.get('com.apple.security.virtualization') is True:
        return

    # Lima's upstream vz.entitlements also contains these network permissions.
    # Preserve additional existing keys while adding the missing VZ permission.
    requested = {
        'com.apple.security.network.client': True,
        'com.apple.security.network.server': True,
        **existing,
        'com.apple.security.virtualization': True,
    }
    entitlements_file = binary.parent.parent / 'vz-entitlements.plist'
    if entitlements_file.is_symlink() or (entitlements_file.exists() and not entitlements_file.is_file()):
        raise RuntimeError(f'Unsafe managed Lima entitlement file: {entitlements_file}')
    entitlements_file.write_bytes(plistlib.dumps(requested))
    print('[MAC BOOTSTRAP] Signing managed Lima VZ host with virtualization entitlement', flush=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none',
                    '--entitlements', str(entitlements_file), str(binary)],
                   check=True, capture_output=True)
    verified, effective = _lima_signature_state(binary)
    if not verified or effective.get('com.apple.security.virtualization') is not True:
        raise RuntimeError('Managed Lima limactl is not signed with '
                           'com.apple.security.virtualization; refusing to start VZ')
    print('[MAC BOOTSTRAP] Managed Lima VZ signature and entitlement verified', flush=True)


def _ensure_buildx_plugin(builds: Path, env: dict[str, str]) -> Path:
    """Register the pinned Conda Buildx executable with the managed Docker CLI.

    conda-forge installs ``bin/docker-buildx`` but Docker discovers CLI
    plugins beneath DOCKER_CONFIG/cli-plugins (which must remain in Builds).
    Do not use a user's ~/.docker plugins, Docker Desktop or legacy builder.
    A retry replaces an outdated managed plugin atomically without removing
    the Conda-managed executable or any cached VM state.
    """
    source = builds.resolve() / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker-buildx'
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f'Mac Setup missing pinned docker-buildx executable: {source}')
    config = Path(env['DOCKER_CONFIG'])
    if not config.is_relative_to(builds.resolve()):
        raise RuntimeError(f'Managed Docker configuration escaped Builds: {config}')
    plugins = config / 'cli-plugins'
    if plugins.is_symlink() or (plugins.exists() and not plugins.is_dir()):
        raise RuntimeError(f'Managed Docker CLI plugin directory is not a real directory: {plugins}')
    plugins.mkdir(parents=True, exist_ok=True)
    target = plugins / 'docker-buildx'
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RuntimeError(f'Managed Buildx plugin path is not a regular file: {target}')
    if not target.is_file() or _file_sha256(target) != _file_sha256(source):
        temporary = plugins / 'docker-buildx.part'
        if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
            raise RuntimeError(f'Untrusted managed Buildx staging path: {temporary}')
        try:
            shutil.copy2(source, temporary)
            temporary.chmod(temporary.stat().st_mode | 0o111)
            if _file_sha256(temporary) != _file_sha256(source):
                raise RuntimeError('Managed Buildx plugin copy failed SHA-256 verification')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    docker = builds.resolve() / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker'
    probe = _probe([str(docker), 'buildx', 'version'], env=env)
    if probe.returncode or 'buildx' not in probe.stdout.lower():
        raise RuntimeError('Managed Docker Buildx plugin is not operational: ' + probe.stdout[-800:])
    print('[MAC BOOTSTRAP] Managed Buildx=' + probe.stdout.strip(), flush=True)
    return target


def _stop_colima_profile(colima: Path, profile: str, env: dict[str, str],
                         *, unhealthy: bool = False) -> None:
    """Stop only a project-owned Colima profile, preserving generated disks."""
    command = [str(colima), 'stop', profile]
    if not unhealthy:
        try:
            _run(command, env=env, timeout=8)
            return
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
            print('[MAC BOOTSTRAP] Colima graceful stop failed ('
                  + type(error).__name__ + '); trying force-stop for ' + profile,
                  flush=True)
    _run(command + ['--force'], env=env, timeout=45)


def ensure_docker(builds: Path) -> str:
    """Start/reuse the canonical LinuxKit c producer; never create w.

    The ARM64 c backend builds the LinuxKit kernel and both AMD64 cross
    toolchain images. The separate Terminal runtime t is started only for
    the Linux/Windows Bash and VNC acceptance after Setup provisioning.
    """
    if platform.system() != 'Darwin':
        raise RuntimeError('Managed Mac container producer requires macOS')
    _stage_lima(builds)
    env = managed_environment(builds)
    buildx = _ensure_buildx_plugin(builds, env)
    from linuxos.managed_backend import ensure_backend, source_directory
    docker, backend_env = ensure_backend(source_directory(Path(__file__).resolve().parents[1]))
    # `ensure_backend` owns all guest startup, live progress and readiness.
    # Do not introduce a second Colima profile or second bootstrap path.
    health = _probe([docker, 'info', '--format', '{{.ServerVersion}}'],
                    env=backend_env, timeout=45)
    if health.returncode:
        raise RuntimeError('LinuxKit c Docker engine is not operational: '
                           + health.stdout[-1200:])
    state = managed_state_root(builds)
    state.mkdir(parents=True, exist_ok=True)
    root = builds / 'CinterForgeTerminal/Toolchains/mac'
    manifest = state / 'backend.json'
    paths = ('lima/bin/limactl', 'env/bin/colima', 'env/bin/docker',
             'env/bin/docker-buildx')
    manifest.write_text(json.dumps({
        'schema': 'cinterforge.terminal.managed-mac-oci.v2',
        'backend': 'colima-lima-vz-arm64', 'profile': COLIMA_PROFILE,
        'executables': {name: _file_sha256(root / name) for name in paths},
        'state_root': VM_STATE_FOLDER, 'engine_version': health.stdout.strip(),
        'buildx_plugin_sha256': _file_sha256(buildx),
        'macos_native_assumption': 'Xcode',
    }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('[MAC BOOTSTRAP] Reusing LinuxKit c Docker producer: ' + health.stdout.strip(),
          flush=True)
    return docker


def stop_linuxkit_build_backend(builds: Path) -> None:
    """Stop the single Setup-owned Docker producer c.

    Resolve exactly the backend's own workspace paths and profile name; do
    not stop the default Colima VM or remove images/disks. A profile that has
    never been created needs no cleanup.
    """
    if platform.system() != 'Darwin':
        return
    from linuxos import managed_backend
    source = managed_backend.source_directory(Path(__file__).resolve().parents[1])
    env = managed_backend.backend_environment(source, create=False)
    profile = env['CFOS_COLIMA_PROFILE']
    instance = Path(env['LIMA_HOME']) / ('colima-' + profile)
    profile_home = Path(env['COLIMA_HOME']) / profile
    if not instance.exists() and not profile_home.exists():
        return
    root = builds / 'CinterForgeTerminal/Toolchains/mac'
    colima = root / 'env/bin/colima'
    if not colima.is_file():
        raise RuntimeError('LinuxKit managed VM exists but Colima executable is missing')
    # Always target the Setup-verified Lima host, not a Conda limactl that
    # may shadow it and lack Virtualization.framework entitlement.
    env['PATH'] = os.pathsep.join((str(root / 'lima/bin'), str(root / 'env/bin'),
                                  env.get('PATH', '')))
    print('[MAC BOOTSTRAP] Shutting down managed LinuxKit builder VM ' + profile, flush=True)
    try:
        _stop_colima_profile(colima, profile, env)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Managed LinuxKit VM cleanup failed: ' + str(exc)) from exc


def stop_setup_build_vms(builds: Path) -> None:
    """Stop the single Setup/Build-owned c producer, including on failure."""
    stop_linuxkit_build_backend(builds)


def cleanup_watchdog(builds: Path) -> int:
    """Survive SIGKILL of the outer command and stop its managed Colima VM.

    Only the outer Cinter command owns the pipe's write end. Child tools do
    not inherit it. 'finished' means the owner performed normal cleanup.
    Unexpected EOF means that owner crashed or was force-quit.
    """
    completed = sys.stdin.buffer.read(64)
    if completed != b'finished\n':
        try:
            # The owner may have been killed after Bedrock and while the
            # single x86-64 t image was building. Recover that VM as well;
            # never stop a live native-app owner's session or foreign guests.
            state = builds / 'CFTVM/T'
            if state.is_dir() and (state / 'c/t/colima.yaml').is_file():
                from webapp import CinterForgeTerminal as runtime
                previous = {k: os.environ.get(k) for k in
                            ('CINTER_BUILDS_ROOT', 'CINTER_TERMINAL_RUNTIME_MODE')}
                os.environ['CINTER_BUILDS_ROOT'] = str(builds)
                os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'developer'
                try:
                    sdk = builds / 'CinterForgeTerminal/Toolchains'
                    env = runtime._env(sdk)
                    owner = runtime._runtime_watchdog_owner(env)
                    occupied = False
                    if owner.is_file():
                        record = json.loads(owner.read_text(encoding='utf-8'))
                        pid = record.get('native_pid')
                        occupied = (isinstance(pid, int) and
                                    runtime._live_runtime_watchdog(pid, env))
                    if not occupied:
                        print('[MAC BOOTSTRAP] Recovering interrupted x86-64 t Setup VM',
                              flush=True)
                        runtime.stop_runtime_vm(setup_sdk=sdk)
                finally:
                    for key, value in previous.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
        finally:
            stop_setup_build_vms(builds)
    return 0


def ensure_linuxkit_cli(builds: Path) -> Path:
    """Compile pinned LinuxKit v1.3.0 CLI from its nested Go module in Setup.

    LinuxKit's root v1.3.0 Go module does NOT contain src/cmd/linuxkit.
    Work only under sibling Builds; no Homebrew/global installs.
    """
    root = builds / 'CinterForgeTerminal/Toolchains/mac'
    executable = root / 'linuxkit/bin/linuxkit'
    if executable.is_file():
        _run([str(executable), 'version'])
        return executable
    go = root / 'env/bin/go'
    if not go.is_file():
        raise RuntimeError('Mac Setup missing managed Go compiler for LinuxKit')
    checkout = root / 'linuxkit/source'
    if not (checkout / 'src/cmd/linuxkit/go.mod').is_file():
        if checkout.exists():
            raise RuntimeError('Incomplete LinuxKit source checkout: ' + str(checkout))
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _run(['git', 'clone', '--depth', '1', '--branch', 'v1.3.0',
              'https://github.com/linuxkit/linuxkit.git', str(checkout)], timeout=1800)
    tag = subprocess.run(['git', '-C', str(checkout), 'describe', '--tags', '--exact-match', 'HEAD'],
                         capture_output=True, text=True, check=True, timeout=15)
    if tag.stdout.strip() != 'v1.3.0':
        raise RuntimeError('LinuxKit source is not the pinned v1.3.0 checkout')
    executable.parent.mkdir(parents=True, exist_ok=True)
    cache = root / 'go'
    # The LinuxKit c environment is prepared before Colima starts. Its
    # advertised CFTVM/U/tmp does not yet exist on a clean Setup, and Go
    # fails before compilation when TMPDIR points there. Give the native
    # LinuxKit compiler its own reproducible Setup-owned scratch directory.
    for name in ('cache', 'modules', 'path', 'tmp'):
        directory = cache / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise RuntimeError(f'Invalid managed Go build directory: {directory}')
        directory.mkdir(parents=True, exist_ok=True)
    env = managed_environment(builds)
    env.update(GOBIN=str(executable.parent), GOPATH=str(cache / 'path'),
               GOCACHE=str(cache / 'cache'), GOMODCACHE=str(cache / 'modules'),
               TMPDIR=str(cache / 'tmp'), GOTMPDIR=str(cache / 'tmp'))
    print('[MAC BOOTSTRAP] Building LinuxKit with managed Go scratch: '
          + env['GOTMPDIR'], flush=True)
    _run([str(go), 'build', '-o', str(executable), '.'],
         env=env, cwd=str(checkout / 'src/cmd/linuxkit'), timeout=1800)
    if not executable.is_file():
        raise RuntimeError('Pinned LinuxKit builder did not produce ' + str(executable))
    _run([str(executable), 'version'], env=env)
    return executable


def ensure(builds: Path) -> dict[str, str]:
    tools = ensure_macos_tools(builds)
    tools['lima'] = str(_stage_lima(builds))
    tools.update(ensure_qemu(builds))
    # Register Buildx in c's Docker config without starting any VM.
    tools['buildx'] = str(_ensure_buildx_plugin(builds, managed_environment(builds)))
    tools['linuxkit'] = str(ensure_linuxkit_cli(builds))
    return tools


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', required=True, type=Path)
    if len(sys.argv) > 1 and sys.argv[1] == '--cleanup-watchdog':
        parser.add_argument('--cleanup-watchdog', action='store_true')
        args = parser.parse_args()
        return cleanup_watchdog(args.builds.resolve())
    args = parser.parse_args()
    ensure(args.builds.resolve())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
