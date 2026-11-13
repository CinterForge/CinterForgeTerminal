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
COLIMA_PROFILE = 'w'
# Mac's sockaddr_un.sun_path is 104 bytes INCLUDING the terminal NUL.
SOCKET_PATH_LIMIT = 104
# Colima/Lima must use physical, short paths, not symlink aliases.  Keep the
# guest/state closure under sibling Builds, separate from the larger toolchain.
VM_STATE_FOLDER = 'CFTVM'
PACKAGES = ('python=3.11', 'cmake=3.31', 'ninja=1.12', 'nodejs=22',
            'pkg-config', 'go=1.24', 'colima=0.10.3', 'docker-cli=29.8.1',
            'docker-buildx=0.35.0', '7zip=26.01')
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
            shutil.copyfileobj(source, sink)
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
                'buildx': prefix / 'bin/docker-buildx'}
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
        _run([str(path), 'version' if name in ('go', 'buildx') else '--version'])
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
    """Choose a *physical* socket-safe guest home under the real sibling Builds.

    The full SSH socket path includes ``colima-<profile>`` and a Lima suffix
    even before a VM disk is created.  Moving only the Lima binary, shortening
    the profile, or adding a symlink inside the toolchain does not fix it.
    Check the real filesystem path before starting Colima or touching VM data.
    """
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError(f'Managed Colima requires an existing sibling Builds directory: {builds}')
    state = builds / VM_STATE_FOLDER
    socket = state / 'lima' / ('colima-' + COLIMA_PROFILE) / 'ssh.sock.1234567890123456'
    if len(os.fsencode(socket)) >= SOCKET_PATH_LIMIT:
        raise RuntimeError(
            f'Managed Lima socket would be {len(os.fsencode(socket))} bytes '
            f'(macOS requires fewer than {SOCKET_PATH_LIMIT}): {socket}. '
            'The physical Builds path is too long for this profile; no VM state '
            'was changed. Place the workspace at a shorter physical path '
            '(do not use an external or symlinked Lima home).'
        )
    for path in (state, *(state / relative for relative in
                            ('home', 'colima', 'cache', 'cache/colima',
                             'lima', 'docker', 'config', 'tmp'))):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RuntimeError(f'Managed Lima state must be a real directory: {path}')
    return state


def managed_environment(builds: Path) -> dict[str, str]:
    """Recreate the same short, Builds-owned VM paths for every child stage."""
    root = builds.resolve() / 'CinterForgeTerminal/Toolchains/mac'
    state = managed_state_root(builds)
    env = dict(os.environ)
    # A developer's exported Docker host must not redirect engine health
    # checks to an unrelated machine/global Docker Desktop installation.
    for key in ('DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    env.update(HOME=str(state / 'home'), COLIMA_HOME=str(state / 'colima'),
               COLIMA_CACHE_HOME=str(state / 'cache/colima'),
               LIMA_HOME=str(state / 'lima'),
               DOCKER_CONFIG=str(state / 'docker'),
               XDG_CONFIG_HOME=str(state / 'config'),
               XDG_CACHE_HOME=str(state / 'cache'),
               TMPDIR=str(state / 'tmp'),
               DOCKER_CONTEXT='colima-' + COLIMA_PROFILE,
               PATH=os.pathsep.join((str(root / 'linuxkit/bin'), str(root / 'lima/bin'), str(root / 'env/bin'),
                                     os.environ.get('PATH', ''))))
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


def _vm_source_mounts_ready(colima: Path, builds: Path, env: dict[str, str]) -> bool:
    """Check the actual Lima guest paths used by Docker bind mounts.

    With HOME redirected into Builds, Colima's default home mount does not
    contain the sibling source checkout or Builds itself. A healthy Docker
    daemon therefore does not imply Docker containers can see either path.
    """
    source = Path(__file__).resolve().parents[1]
    for command in (('test', '-f', str(source / 'Cinter')),
                    ('test', '-w', str(builds.resolve()))):
        result = _probe([str(colima), 'ssh', '--profile', COLIMA_PROFILE,
                         '--', *command], env=env, timeout=45)
        if result.returncode:
            return False
    return True


def _stop_colima_profile(colima: Path, profile: str, env: dict[str, str],
                         *, unhealthy: bool = False) -> None:
    """Stop only this managed profile; never delete its image or disk.

    When Docker has already failed its health check, graceful Colima stop may
    block indefinitely in the guest runtime teardown. Skip that known-broken
    path and ask Colima to stop its VM directly. For a healthy session, allow
    a brief graceful shutdown before forcing VM shutdown.
    """
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


# SSH readiness is only Lima step 1/5. Do not let Colima wait 30 minutes if
# provisioning never writes /run/lima-boot-done (observed in clean Setup).
COLIMA_START_TOTAL_SECONDS = 900
COLIMA_BOOT_AFTER_SSH_SECONDS = 300


def _guest_ssh(state: Path, script: str, *, timeout: int = 20) -> str:
    """Read the booting VM through Lima's generated SSH config (steps 1-4).

    limactl shell can reject an instance that has not finished step 5. Keep
    host-key checking and authentication from the managed Lima SSH config.
    """
    config = state / 'lima' / ('colima-' + COLIMA_PROFILE) / 'ssh.config'
    if not config.is_file():
        return 'SSH config not available yet\n'
    command = ['/usr/bin/ssh', '-F', str(config), '-T',
               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
               'lima-colima-' + COLIMA_PROFILE,
               'sudo -n /bin/sh -c ' + shlex.quote(script)]
    try:
        result = _probe(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 'Guest SSH command timed out\n'
    return f'rc={result.returncode}\n{result.stdout}'


def _colima_boot_diagnostics(builds: Path, reason: str) -> tuple[Path, str]:
    """Capture guest cloud-init and Lima boot failures before stopping our VM."""
    state = managed_state_root(builds)
    folder = builds.resolve() / 'Logs/CinterForgeTerminal'
    folder.mkdir(parents=True, exist_ok=True)
    logfile = folder / 'mac-colima-w-boot.log'
    pieces = [f'profile={COLIMA_PROFILE}; reason={reason}\n']
    guest_cmd = '''set +e
printf '\\n=== Lima markers ===\\n'
for f in /run/lima-boot-done /run/lima-ssh-ready /run/lima-reboot-required; do
  printf '%s: ' "$f"; if test -f "$f"; then cat "$f"; else echo ABSENT; fi
done
printf '\\n=== cloud-init ===\\n'; cloud-init status --long 2>&1
printf '\\n=== failed services ===\\n'; systemctl --failed --no-pager 2>&1
printf '\\n=== cloud-final ===\\n'; systemctl status cloud-final.service --no-pager -l 2>&1
printf '\\n=== cloud-final journal ===\\n'; journalctl -b -u cloud-final.service --no-pager -n 60 2>&1
printf '\\n=== guest logs ===\\n'
for f in /var/log/lima-init.log /var/log/cloud-init-output.log; do
  if test -r "$f"; then echo "--- $f"; tail -n 90 "$f"; fi
done
printf '\\n=== boot processes ===\\n'
ps -eo pid,ppid,stat,etime,args | grep -E 'cloud-init|lima.*boot|dockerd|containerd|apt-get|dpkg' | head -n 40
'''
    guest = _guest_ssh(state, guest_cmd, timeout=24)
    pieces.append('===== Guest boot diagnostics =====\n' + guest)
    for name in ('ha.stderr.log', 'serial.log', 'serialv.log'):
        path = state / 'lima' / ('colima-' + COLIMA_PROFILE) / name
        if path.is_file():
            pieces.append(f'===== {name}: last 60 lines =====\n'
                          + '\n'.join(path.read_text(encoding='utf-8', errors='replace')
                                      .splitlines()[-60:]))
    logfile.write_text('\n'.join(pieces) + '\n', encoding='utf-8')
    return logfile, guest


def _retry_failed_cloud_final(state: Path, diagnostics: str) -> bool:
    """Retry a failed (never active) guest provisioning service just once."""
    if ('cloud-final.service' not in diagnostics or 'Active: failed' not in diagnostics
            or '/run/lima-reboot-required: ABSENT' not in diagnostics):
        return False
    print('[MAC BOOTSTRAP] cloud-final failed; retrying once inside managed VM', flush=True)
    result = _guest_ssh(state,
        'systemctl reset-failed cloud-final.service; '
        'systemctl restart cloud-final.service', timeout=60)
    print('[MAC BOOTSTRAP] cloud-final retry result: ' + result[-800:], flush=True)
    return True


def _start_docker_after_lima_boot(state: Path) -> bool:
    """Recover Colima's known persistent-disk case where Docker stays stopped.

    Never start the daemon before Lima has completed the *current* boot, or
    during a required reboot. This is only a Setup-owned Linux VM service.
    """
    result = _guest_ssh(state, """if test -s /run/lima-boot-done && test -s /run/lima-ssh-ready \
    && cmp -s /run/lima-boot-done /run/lima-ssh-ready \
    && ! test -e /run/lima-reboot-required \
    && ! systemctl is-active --quiet docker.service; then
  systemctl start docker.service && echo CINTER_DOCKER_STARTED
fi
""", timeout=30)
    if 'CINTER_DOCKER_STARTED' in result:
        print('[MAC BOOTSTRAP] Lima boot complete; restarted stopped Docker service',
              flush=True)
        return True
    return False


def _start_colima_checked(command: list[str], *, builds: Path,
                          env: dict[str, str],
                          total_timeout: float = COLIMA_START_TOTAL_SECONDS,
                          boot_timeout: float = COLIMA_BOOT_AFTER_SSH_SECONDS,
                          poll_interval: float = 10) -> None:
    """Run Colima with a bound on Lima's boot scripts, not only the VM process."""
    print('[MAC BOOTSTRAP] $ ' + ' '.join(map(str, command)), flush=True)
    process = subprocess.Popen([str(c) for c in command], env=env)
    started = time.monotonic()
    ssh_at = None
    last_report = None
    retried_cloud_final = False
    state = managed_state_root(builds)
    try:
        while True:
            try:
                result = process.wait(timeout=poll_interval)
            except subprocess.TimeoutExpired:
                result = None
            if result is not None:
                if result:
                    report, _ = _colima_boot_diagnostics(builds, f'start exit={result}')
                    raise RuntimeError(f'Managed Colima startup failed (exit {result}); '
                                       f'guest diagnostic log: {report}')
                return
            now = time.monotonic()
            if ssh_at is None:
                probe = _guest_ssh(state, 'echo CINTER_SSH_READY', timeout=7)
                if 'CINTER_SSH_READY' in probe:
                    ssh_at = now
                    print('[MAC BOOTSTRAP] Lima SSH ready; awaiting boot scripts (5/5)', flush=True)
            age = now - started
            boot_age = now - ssh_at if ssh_at is not None else 0
            if (ssh_at is not None and boot_age >= 60
                    and (last_report is None or now - last_report >= 60)):
                report, guest = _colima_boot_diagnostics(
                    builds, f'boot step 5 incomplete after {int(boot_age)}s')
                last_report = now
                print('[MAC BOOTSTRAP] Lima boot incomplete; diagnostics: '
                      + str(report), flush=True)
                if not retried_cloud_final and _retry_failed_cloud_final(state, guest):
                    retried_cloud_final = True
                # Colima's persistent data-disk provisioning may leave Docker
                # stopped even after an otherwise complete Lima boot.
                _start_docker_after_lima_boot(state)
            if age >= total_timeout or (ssh_at is not None and boot_age >= boot_timeout):
                report, _ = _colima_boot_diagnostics(
                    builds, f'boot timeout total={int(age)}s post-SSH={int(boot_age)}s')
                raise RuntimeError('Managed Colima guest did not complete Lima boot scripts '
                                   '(5/5). Setup stopped the owned VM. '
                                   f'Cloud-init/Lima diagnostics: {report}')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)

def ensure_docker(builds: Path) -> str:
    """Provision Colima + Lima VM and Docker daemon without Docker Desktop.

    On an Apple Silicon host, VZ with Rosetta executes linux/amd64 images in
    an ARM64 guest. Only Xcode is assumed installed; all virtual-machine state
    and Docker configuration live in Builds. No sudo or /Applications install.
    """
    if platform.system() != 'Darwin':
        raise RuntimeError('Managed Mac container backend requires macOS')
    root = builds / 'CinterForgeTerminal/Toolchains/mac'
    _stage_lima(builds)
    env = managed_environment(builds)
    state = managed_state_root(builds)
    for name in ('home', 'colima', 'cache', 'cache/colima', 'lima', 'docker', 'config', 'tmp'):
        (state / name).mkdir(parents=True, exist_ok=True)
    docker = root / 'env/bin/docker'
    colima = root / 'env/bin/colima'
    if not (docker.is_file() and colima.is_file()):
        raise RuntimeError('Mac Setup did not provision Colima and docker-cli into Builds')
    buildx = _ensure_buildx_plugin(builds, env)
    _run([str(colima), 'version'], env=env)
    try:
        health = _probe([str(docker), 'info', '--format', '{{.ServerVersion}}'], env=env, timeout=12)
    except subprocess.TimeoutExpired:
        print('[MAC BOOTSTRAP] Docker health probe timed out; recovering managed Colima profile', flush=True)
        health = subprocess.CompletedProcess([str(docker), 'info'], 124, stdout='Docker health timeout')
    if health.returncode == 0 and not _vm_source_mounts_ready(colima, builds, env):
        # Colima was already started without these explicit mounts. Stop and
        # reconfigure the existing profile; never delete VM disks or images.
        print('[MAC BOOTSTRAP] Adding missing source/Builds mounts to existing VM', flush=True)
        _stop_colima_profile(colima, COLIMA_PROFILE, env)
        # Restart this profile with the required mounts; VM disks and saved
        # OCI images are preserved across a normal stop/start.
        needs_start = True
    else:
        needs_start = health.returncode != 0
    if needs_start:
        if health.returncode != 0:
            # An existing Colima VM may be running with a dead Docker daemon.
            # `colima start` silently ignores an already-running VM, leaving
            # Docker unusable. Stop ONLY our managed profile before starting.
            # Stop preserves the VM disk, cached images and all build inputs.
            print('[MAC BOOTSTRAP] Docker unavailable; restarting managed Colima profile', flush=True)
            try:
                _stop_colima_profile(colima, COLIMA_PROFILE, env, unhealthy=True)
            except subprocess.CalledProcessError:
                # A fresh Setup may not have a profile to stop yet.
                print('[MAC BOOTSTRAP] No active managed profile to stop', flush=True)
        source = Path(__file__).resolve().parents[1]
        command = [str(colima), 'start', COLIMA_PROFILE,
                   '--runtime', 'docker', '--vm-type', 'vz', '--vz-rosetta',
                   '--arch', 'aarch64', '--cpus', '4', '--memory', '8', '--disk', '64',
                   '--mount', str(source), '--mount', str(builds.resolve()) + ':w',
                   '--save-config']
        print('[MAC BOOTSTRAP] Starting managed Windows-cross container VM', flush=True)
        try:
            _start_colima_checked(command, builds=builds, env=env)
        except BaseException:
            # Failed or interrupted Setup must stop its owned VM after logging.
            try:
                _stop_colima_profile(colima, COLIMA_PROFILE, env, unhealthy=True)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                print('[MAC BOOTSTRAP] Incomplete VM cleanup failed: ' + str(error), flush=True)
            raise
        health = _probe([str(docker), 'info', '--format', '{{.ServerVersion}}'],
                        env=env, timeout=45)
    if health.returncode:
        raise RuntimeError('Managed Colima Docker engine is not operational: '
                           + health.stdout[-1200:])
    if not _vm_source_mounts_ready(colima, builds, env):
        raise RuntimeError('Managed Colima VM cannot access the source checkout and writable '
                           'Builds; Windows cross-container bind mounts would fail')
    # Cross-architecture executable execution, not just engine responsiveness.
    # Pull and cache the actual linux/amd64 guest image during Windows Setup;
    # that image, including MinGW/Wine libraries, is sealed separately.
    manifest = state / 'backend.json'
    paths = ('lima/bin/limactl', 'env/bin/colima', 'env/bin/docker',
             'env/bin/docker-buildx')
    manifest.write_text(json.dumps({
        'schema': 'cinterforge.terminal.managed-mac-oci.v2',
        'backend': 'colima-lima-vz-rosetta', 'profile': COLIMA_PROFILE,
        'executables': {name: _file_sha256(root / name) for name in paths},
        'state_root': VM_STATE_FOLDER, 'engine_version': health.stdout.strip(),
        'buildx_plugin_sha256': _file_sha256(buildx),
        'macos_native_assumption': 'Xcode',
    }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('[MAC BOOTSTRAP] Managed Docker engine=' + health.stdout.strip(), flush=True)
    return str(docker)


def stop_managed_colima(builds: Path) -> None:
    """Shut down only this project's managed cross-build VM profile.

    Profile state may be unhealthy, so a failed status probe must never be
    treated as permission to skip cleanup. Never delete the VM or its disk.
    """
    if platform.system() != 'Darwin':
        return
    colima = builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/colima'
    if not colima.is_file():
        return
    env = managed_environment(builds)
    try:
        state = _probe([str(colima), 'status', COLIMA_PROFILE], env=env, timeout=10)
    except subprocess.TimeoutExpired:
        state = None  # A stuck profile must still receive the stop request.
    if state is not None and state.returncode != 0:
        message = state.stdout.lower()
        if 'does not exist' in message or 'not found' in message:
            return
    print('[MAC BOOTSTRAP] Shutting down managed cross-build VM', flush=True)
    try:
        _stop_colima_profile(colima, COLIMA_PROFILE, env)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Managed Colima VM cleanup failed: ' + str(exc)) from exc


def stop_linuxkit_build_backend(builds: Path) -> None:
    """Stop the separate LinuxKit/CFOS Colima profile created during Setup.

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
    """Attempt both independent owned profiles on every Setup/Build exit."""
    failure = None
    for cleanup in (stop_managed_colima, stop_linuxkit_build_backend):
        try:
            cleanup(builds)
        except Exception as error:
            print('[MAC BOOTSTRAP] Cleanup error: ' + str(error), flush=True)
            if failure is None:
                failure = error
    if failure is not None:
        raise RuntimeError('One or more managed VM profiles did not shut down') from failure


def cleanup_watchdog(builds: Path) -> int:
    """Survive SIGKILL of the outer command and stop its managed Colima VM.

    Only the outer Cinter command owns the pipe's write end. Child tools do
    not inherit it. 'finished' means the owner performed normal cleanup.
    Unexpected EOF means that owner crashed or was force-quit.
    """
    completed = sys.stdin.buffer.read(64)
    if completed != b'finished\n':
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
    for name in ('cache', 'modules', 'path'):
        (cache / name).mkdir(parents=True, exist_ok=True)
    env = managed_environment(builds)
    env.update(GOBIN=str(executable.parent), GOPATH=str(cache / 'path'),
               GOCACHE=str(cache / 'cache'), GOMODCACHE=str(cache / 'modules'))
    _run([str(go), 'build', '-o', str(executable), '.'],
         env=env, cwd=str(checkout / 'src/cmd/linuxkit'), timeout=1800)
    if not executable.is_file():
        raise RuntimeError('Pinned LinuxKit builder did not produce ' + str(executable))
    _run([str(executable), 'version'], env=env)
    return executable


def ensure(builds: Path) -> dict[str, str]:
    tools = ensure_macos_tools(builds)
    tools['docker'] = ensure_docker(builds)
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
