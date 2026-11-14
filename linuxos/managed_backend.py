"""One reusable Colima backend for the CinterForgeOS headless Ubuntu build.

Colima and Lima need short real filesystem paths on macOS. Their VM disks,
Docker storage and caches live on the build volume when the CinterForge checkout
is on an external volume; never silently fill the internal disk.
Application build products remain under Builds/CreativeSuite/Built.
"""
from __future__ import annotations

import codecs
import os
import shlex
import signal
import sys
import time
from pathlib import Path
import platform
import shutil
import subprocess

# Lima's macOS socket address is limited to 104 bytes. Keep the managed
# runtime *inside Builds* but avoid long, product-facing names on its sockets.
SHORT_PROFILE = 'c'
LEGACY_PROFILE = 'cinterforge-linux'
SOCKET_PATH_LIMIT = 104


def host_executable(name: str) -> str | None:
    """Find a host tool when Finder launches Swift without Homebrew on PATH.

    User overrides support nonstandard package-manager prefixes. These are
    host executable paths, never guesses about repository/build locations.
    """
    configured = os.environ.get('CFOS_' + name.upper())
    if configured:
        executable = Path(configured).expanduser()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError(f'Configured CFOS_{name.upper()} is not executable: {executable}')
        return str(executable.resolve())
    found = shutil.which(name)
    if found:
        return found
    for prefix in ('/opt/homebrew/bin', '/usr/local/bin'):
        executable = Path(prefix) / name
        if executable.is_file() and os.access(executable, os.X_OK):
            return str(executable)
    return None


def storage_root(source: Path) -> Path:
    """Isolate Terminal's disposable Ubuntu/guest builder from the Windows VM.

    The copied CFOS adapter still receives its historical Source path, but
    the VM is owned by Terminal Setup, not by a persistent CFOS installation.
    Both managed VMs are rebuildable inside the sibling Builds/CFTVM root.
    """
    source = Path(source)
    if not source.is_absolute():
        raise RuntimeError(f'CFOS Linux Source must be absolute: {source}')
    source = source.resolve()
    if (source.name != 'Source' or source.parent.name != 'linux'
            or source.parent.parent.name != 'cinterforge-os'
            or source.parents[2].name != 'Built'
            or source.parents[3].name != 'CreativeSuite'
            or source.parents[4].name != 'Builds'):
        raise RuntimeError('Unexpected CFOS Linux build Source path: '
                           f'{source}; expected <workspace>/Builds/CreativeSuite/'
                           'Built/cinterforge-os/linux/Source')
    return source.parents[4] / 'CFTVM/U'


def source_directory(repo: Path) -> Path:
    """Keep the original Ubuntu Source contract without sharing VM state."""
    if not (repo / 'Cinter').is_file():
        raise RuntimeError(f'Expected CinterForgeTerminal repository: {repo}')
    return repo.parent / 'Builds/CreativeSuite/Built/cinterforge-os/linux/Source'


def managed_colima_settings(source: Path) -> tuple[Path, Path, str]:
    """Always use a short, independent, disposable ``c`` profile.

    Never revive CFOS's older ``CreativeSuite/CFOS/Colima`` instance. A
    clean Terminal Setup must not depend on state outside its managed VM root.
    """
    root = storage_root(source)
    home, lima, profile = root / 'c', root / 'l', SHORT_PROFILE
    socket = lima / f'colima-{profile}' / 'ssh.sock.1234567890123456'
    if len(os.fsencode(socket)) >= SOCKET_PATH_LIMIT:
        raise RuntimeError(
            f'Lima Unix socket exceeds macOS {SOCKET_PATH_LIMIT}-byte limit: {socket}; '
            'use a shorter physical workspace path, not a symlink')
    for directory in (root, home, lima):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise RuntimeError(f'Invalid managed VM state directory: {directory}')
    return home, lima, profile


def managed_colima_home(source: Path) -> Path:
    if platform.system() != 'Darwin':
        raise RuntimeError('Managed Colima storage requires macOS')
    home, lima, _ = managed_colima_settings(source)
    home.mkdir(parents=True, exist_ok=True)
    lima.mkdir(parents=True, exist_ok=True)
    return home


def backend_environment(source: Path, *, create: bool = True) -> dict[str, str]:
    """All child commands use the isolated Ubuntu builder and its own socket."""
    home, lima, profile = managed_colima_settings(source)
    root = home.parent
    folders = {'home': root / 'home', 'config': root / 'config',
               'cache': root / 'cache', 'tmp': root / 'tmp',
               'docker': root / 'docker'}
    if create:
        managed_colima_home(source)
        for directory in folders.values():
            directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(key, None)
    environment.update({
        'CFOS_COLIMA_PROFILE': profile,
        'COLIMA_HOME': str(home),
        'LIMA_HOME': str(lima),
        'COLIMA_CACHE_HOME': str(folders['cache'] / 'colima'),
        'DOCKER_CONFIG': str(folders['docker']),
        'DOCKER_HOST': f'unix://{home / profile / "docker.sock"}',
        'HOME': str(folders['home']),
        'XDG_CONFIG_HOME': str(folders['config']),
        'XDG_CACHE_HOME': str(folders['cache']),
        'TMPDIR': str(folders['tmp']),
    })
    if create:
        (folders['cache'] / 'colima').mkdir(parents=True, exist_ok=True)
    return environment


def print_shell_environment(repo: Path) -> None:
    """Expose one owner for paths/profile across bash and Python consumers."""
    environment = backend_environment(source_directory(repo), create=False)
    for key in ('CFOS_COLIMA_PROFILE', 'COLIMA_HOME', 'LIMA_HOME',
                'COLIMA_CACHE_HOME', 'DOCKER_CONFIG', 'DOCKER_HOST'):
        print(f'export {key}={shlex.quote(environment[key])}')
    print('unset DOCKER_CONTEXT')

def _managed_startup_diagnostics(environment: dict[str, str], profile: str) -> str:
    """Report the actual Lima/VZ failure, not just Colima's generic exit 1.

    Lima writes the useful macOS VZ/host-agent exception to ha.stderr.log,
    which is absent from Colima's captured stdout/stderr. Read only files in
    this owned profile's state directory; never inspect unrelated VM profiles.
    """
    instance = Path(environment['LIMA_HOME']) / f'colima-{profile}'
    if instance.is_symlink() or not instance.is_dir():
        return ''  # Never follow a redirected managed profile into another VM.
    reports = []
    for name in ('ha.stderr.log', 'serial.log'):
        path = instance / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open('rb') as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 8192))
                tail = stream.read().decode('utf-8', errors='replace').strip()
            if tail:
                reports.append(f'{path}:\n{tail[-4096:]}')
        except OSError as error:
            reports.append(f'Cannot inspect managed VM log {path}: {error}')
    return '\n'.join(reports)



def _run_colima_start_live(command: list[str], environment: dict[str, str],
                           log_path: Path, *, timeout: float = 600,
                           heartbeat: float = 20) -> subprocess.CompletedProcess[str]:
    """Stream the managed c VM's actual boot output with a bounded wait.

    Do not pipe stdout into ``subprocess.run(capture_output=True)``: cold
    Lima/Colima boots can take minutes, and that hid every useful diagnostic.
    Keep a dedicated Setup log under sibling Builds/Logs, and terminate only
    the start command's process group if interrupted or timed out. The outer
    lifecycle remains responsible for stopping the owned VM itself.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f'[Ubuntu backend] Colima startup output: {log_path}', flush=True)
    with log_path.open('wb', buffering=0) as writer:
        process = subprocess.Popen(
            command, env=environment, stdout=writer, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.monotonic()
        next_heartbeat = started + heartbeat
        last_bytes = b''
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        with log_path.open('rb', buffering=0) as reader:
            def drain() -> None:
                nonlocal last_bytes
                data = reader.read()
                if data:
                    last_bytes = (last_bytes + data)[-32768:]
                    visible = decoder.decode(data)
                    if visible:
                        print(visible, end='', flush=True)

            try:
                while True:
                    drain()
                    returncode = process.poll()
                    if returncode is not None:
                        drain()
                        remaining = decoder.decode(b'', final=True)
                        if remaining:
                            print(remaining, end='', flush=True)
                        break
                    now = time.monotonic()
                    if now - started >= timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    if now >= next_heartbeat:
                        print(f'[Ubuntu backend] Waiting for Colima c boot '
                              f'({int(now - started)}s elapsed; limit {int(timeout)}s); '
                              f'live details: {log_path}', flush=True)
                        next_heartbeat = now + heartbeat
                    time.sleep(min(0.25, max(0, timeout - (now - started))))
            except BaseException:
                # Interrupt/timeout must not orphan the CLI or its attached
                # helpers. Its dedicated session is ours, not another VM's.
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                drain()
                raise
    return subprocess.CompletedProcess(
        command, returncode, stdout=last_bytes.decode('utf-8', errors='replace'),
        stderr='',
    )


def ensure_backend(source: Path) -> tuple[str, dict[str, str]]:
    """Start or reuse one headless Apple Silicon Colima guest-input VM."""
    if platform.system() == 'Linux':
        docker = host_executable('docker')
        if not docker:
            raise RuntimeError('CFOS Linux backend requires Docker')
        # Do not replace the caller's Linux Docker context, daemon, or storage.
        environment = os.environ.copy()
        subprocess.run([docker, 'info'], env=environment, check=True, stdout=subprocess.DEVNULL, timeout=45)
        return docker, environment
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('CFOS Linux backend requires Linux or Apple Silicon macOS')
    # Terminal Setup has already installed these tools. Never silently
    # adopt Homebrew/Docker Desktop or an unrelated Colima instance.
    builds = storage_root(source).parents[1]
    mac = builds / 'CinterForgeTerminal/Toolchains/mac'
    docker = mac / 'env/bin/docker'
    colima = mac / 'env/bin/colima'
    lima_cli = mac / 'lima/bin/limactl'
    for tool in (docker, colima, lima_cli):
        if not tool.is_file():
            raise RuntimeError(f'Terminal Setup must provision managed VM tool: {tool}')
    docker, colima = str(docker), str(colima)

    environment = backend_environment(source)
    # Finder does not inherit the interactive shell's Homebrew PATH. Colima
    # may itself invoke limactl and other helpers by name; resolve their
    # executable parents for the CHILD environment, never change the user's
    # global PATH or tool installation.
    host_bins = []
    # The pinned, Setup-signed Lima CLI must precede any Conda-provided
    # limactl; only the staged binary has verified VZ entitlements.
    # This c producer is also the shared Mac cross-build backend.
    for binary in (str(lima_cli), docker, colima):
        if binary:
            directory = str(Path(binary).parent)
            if directory not in host_bins:
                host_bins.append(directory)
    inherited = environment.get('PATH', '').split(os.pathsep)
    environment['PATH'] = os.pathsep.join(host_bins + [p for p in inherited if p and p not in host_bins])
    profile = environment['CFOS_COLIMA_PROFILE']
    status = subprocess.run(
        [colima, 'status', '--profile', profile],
        env=environment, capture_output=True, text=True, check=False, timeout=30,
    )
    if status.returncode:
        print(f'[Ubuntu backend] Starting {profile}; VM storage at {environment["COLIMA_HOME"]}', flush=True)
        managed_instance = Path(environment['LIMA_HOME']) / f'colima-{profile}'
        # Reuse an intact disposable builder; only a fresh profile gets sizing.
        if managed_instance.is_dir():
            start_command = [colima, 'start', '--profile', profile, '--activate=false']
        else:
            # The guest glibc/probe producer bind-mounts repository sources
            # and writes its result into Builds; c must have its own mounts.
            repo = Path(__file__).resolve().parents[1]
            builds = storage_root(source).parents[1]
            if not (repo / 'Cinter').is_file():
                raise RuntimeError(f'Guest provisioning requires Terminal source: {repo}')
            start_command = [
                colima, 'start', '--profile', profile, '--vm-type', 'vz',
                '--arch', 'aarch64', '--vz-rosetta=false', '--cpu', '4', '--memory', '8', '--disk', '96',
                '--mount', str(repo), '--mount', str(builds) + ':w',
                '--activate=false',
            ]
        # A partially deleted disposable VM can leave either a stale Lima
        # instance, a detached disk, or both. Handle either failure at *any*
        # startup attempt; do not assume they arrive in a particular order.
        # Recovery is bounded and scoped to this profile's external Lima home.
        external_home = Path(environment['COLIMA_HOME']).resolve()
        managed_disk = Path(environment['LIMA_HOME']) / '_disks' / f'colima-{profile}'
        for attempt in range(4):
            try:
                start = _run_colima_start_live(
                    start_command, environment,
                    builds / 'Logs/CinterForgeTerminal/linuxkit-colima-c-start.log',
                    timeout=600,
                )
            except subprocess.TimeoutExpired as exc:
                details = _managed_startup_diagnostics(environment, profile)
                raise RuntimeError(
                    f'Managed Ubuntu builder {profile} startup timed out after 600s; '
                    f'Colima output: {builds / "Logs/CinterForgeTerminal/linuxkit-colima-c-start.log"}'
                    + ('\nLima/VZ diagnostics:\n' + details if details else
                       f'; inspect {Path(environment["LIMA_HOME"]) / ("colima-" + profile) / "ha.stderr.log"}')
                ) from exc
            if start.returncode == 0:
                break

            diagnostics = start.stdout + start.stderr
            missing_agent = (
                'ha.sock' in diagnostics
                and 'no such file or directory' in diagnostics
                and 'errors inspecting instance' in diagnostics
                and str(managed_instance) in diagnostics
            )
            # Lima also reports this exact broken VZ/host-agent state without
            # mentioning ha.sock or the instance pathname. The VM is unusable;
            # only Setup may recreate this explicitly managed disposable profile.
            broken_vz_agent = (
                'vz driver is running but host agent is not' in diagnostics
                and 'errors inspecting instance' in diagnostics
                and f'colima-{profile}' in diagnostics
            )
            disk_conflict = (
                f'disk `colima-{profile}` already exists' in diagnostics
                and str(managed_disk) in diagnostics
            )
            if attempt == 3 or not (missing_agent or broken_vz_agent or disk_conflict):
                # Preserve and surface the first actionable VZ host-agent error
                # before outer Setup cleans up this disposable VM. Do not
                # automatically delete a fresh disk for an unknown boot error.
                lima_details = _managed_startup_diagnostics(environment, profile)
                raise RuntimeError(
                    f'Managed Colima startup failed (exit={start.returncode}); '
                    'no further safe managed-profile recovery available. '
                    f'{diagnostics.strip()[-1400:]}'
                    + ('\nLima/VZ diagnostics:\n' + lima_details if lima_details else
                       f'\nInspect {Path(environment["LIMA_HOME"]) / ("colima-" + profile) / "ha.stderr.log"}')
                )
            if missing_agent or broken_vz_agent:
                print(f'[Ubuntu backend] Discarding incomplete disposable {profile} '
                      f'instance under {external_home}; no migration', flush=True)
                delete = subprocess.run(
                    [colima, 'delete', '--force', '--profile', profile],
                    env=environment, capture_output=True, text=True, check=False, timeout=60,
                )
                if delete.returncode:
                    raise RuntimeError(
                        f'Could not delete incomplete managed Colima profile '
                        f'(exit={delete.returncode}): '
                        f'{(delete.stderr or delete.stdout).strip()}'
                    )
            else:
                lima = str(lima_cli)
                if not lima:
                    raise RuntimeError(
                        f'Orphaned managed Lima disk {managed_disk}; limactl is '
                        'unavailable. Refusing filesystem-level deletion.'
                    )
                print(f'[Ubuntu backend] Discarding orphaned disposable '
                      f'colima-{profile} disk under {external_home}; no migration', flush=True)
                remove_disk = subprocess.run(
                    [lima, 'disk', 'delete', f'colima-{profile}'],
                    env=environment, capture_output=True, text=True, check=False, timeout=60,
                )
                if remove_disk.returncode:
                    raise RuntimeError(
                        f'Could not delete orphaned managed Lima disk '
                        f'(exit={remove_disk.returncode}): '
                        f'{(remove_disk.stderr or remove_disk.stdout).strip()}'
                    )
                # Lima can report success while leaving the on-disk directory
                # behind (for example after manual cleanup of VM metadata).
                # That directory prevents every subsequent clean VM creation.
                # Remove ONLY this disposable, disconnected disk, never the
                # containing Lima storage or another profile's data.
                if managed_disk.exists() or managed_disk.is_symlink():
                    if managed_instance.exists() or managed_instance.is_symlink():
                        raise RuntimeError(
                            f'Orphaned disk still exists, but Lima instance '
                            f'{managed_instance} is also present; refusing deletion'
                        )
                    if managed_disk.is_symlink() or not managed_disk.is_dir():
                        raise RuntimeError(
                            f'Unexpected managed disk path type: {managed_disk}; '
                            'refusing deletion'
                        )
                    if managed_disk.parent.resolve() != (Path(environment['LIMA_HOME']) / '_disks').resolve():
                        raise RuntimeError('Managed disk escaped its external storage root')
                    print(f'[Ubuntu backend] Removing residual disposable disk directory '
                          f'{managed_disk}', flush=True)
                    shutil.rmtree(managed_disk)
                    if managed_disk.exists():
                        raise RuntimeError(f'Managed disk still exists: {managed_disk}')
    else:
        print(f'[Ubuntu backend] Reusing {profile}; VM storage at {environment["COLIMA_HOME"]}', flush=True)

    socket = Path(environment['COLIMA_HOME']) / profile / 'docker.sock'
    if not socket.is_socket():
        raise RuntimeError(f'Managed Colima Docker socket not found: {socket}')
    subprocess.run([docker, 'info'], env=environment, check=True, stdout=subprocess.DEVNULL, timeout=45)
    return docker, environment


def ensure_buildx(docker: str, environment: dict[str, str]) -> None:
    """Expose the existing Docker buildx plugin inside the managed CFOS config.

    Docker searches DOCKER_CONFIG/cli-plugins, not ~/.docker/cli-plugins when
    DOCKER_CONFIG is overridden. Link the installed plugin, never copy it or
    mutate the user's global Docker configuration.
    """
    check = subprocess.run(
        [docker, 'buildx', 'version'],
        env=environment, capture_output=True, text=True, check=False,
    )
    if check.returncode == 0:
        print(f'[Ubuntu backend] {check.stdout.strip()}', flush=True)
        return
    if platform.system() == 'Linux':
        raise RuntimeError('Docker Buildx missing on Linux; run CFOS Linux Setup and install the Docker Buildx plugin')

    candidates = [
        Path.home() / '.docker/cli-plugins/docker-buildx',
        Path('/opt/homebrew/lib/docker/cli-plugins/docker-buildx'),
        Path('/usr/local/lib/docker/cli-plugins/docker-buildx'),
    ]
    brew = host_executable('brew')
    if brew:
        prefix = subprocess.run(
            [brew, '--prefix', 'docker-buildx'],
            capture_output=True, text=True, check=False,
        )
        if prefix.returncode == 0 and prefix.stdout.strip():
            candidates.insert(0, Path(prefix.stdout.strip()) / 'lib/docker/cli-plugins/docker-buildx')

    plugin = next(
        (candidate.resolve() for candidate in candidates
         if candidate.is_file() and os.access(candidate, os.X_OK)),
        None,
    )
    if plugin is None:
        # Build can be invoked without a preceding Setup (including on a
        # separate build host). Provision the missing host plugin using the
        # same Homebrew contract as Setup, then register it in the managed
        # Docker configuration. Never create a plugin under the Cinter tree.
        if not brew:
            raise RuntimeError(
                'Docker buildx is unavailable and Homebrew was not found. '
                'Run ./CFOS setup cinterforge-os linux Release on this build host.'
            )
        print('[Ubuntu backend] Provisioning missing docker-buildx via Homebrew', flush=True)
        subprocess.run([brew, 'install', 'docker-buildx'], check=True)
        prefix = subprocess.run(
            [brew, '--prefix', 'docker-buildx'], capture_output=True, text=True, check=True,
        ).stdout.strip()
        if not prefix:
            raise RuntimeError('Homebrew installed docker-buildx but returned no formula prefix')
        plugin_path = Path(prefix) / 'lib/docker/cli-plugins/docker-buildx'
        if not plugin_path.is_file() or not os.access(plugin_path, os.X_OK):
            raise RuntimeError(f'Homebrew docker-buildx executable missing: {plugin_path}')
        plugin = plugin_path.resolve()

    plugin_directory = Path(environment['DOCKER_CONFIG']) / 'cli-plugins'
    plugin_directory.mkdir(parents=True, exist_ok=True)
    link = plugin_directory / 'docker-buildx'
    if link.is_symlink():
        if link.resolve() != plugin:
            raise RuntimeError(f'Conflicting managed Docker buildx plugin: {link}')
    elif link.exists():
        raise RuntimeError(f'Refusing to replace managed Docker buildx file: {link}')
    else:
        link.symlink_to(plugin)
        print(f'[Ubuntu backend] Registered buildx: {link} -> {plugin}', flush=True)

    verify = subprocess.run(
        [docker, 'buildx', 'version'],
        env=environment, capture_output=True, text=True, check=False,
    )
    if verify.returncode:
        raise RuntimeError(
            'Docker buildx remains unavailable after Homebrew provisioning in managed CFOS: '
            + (verify.stderr or verify.stdout).strip()
        )
    print(f'[Ubuntu backend] {verify.stdout.strip()}', flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--shell-env':
        print_shell_environment(Path(sys.argv[2]).resolve())
    else:
        raise SystemExit('Usage: managed_backend.py --shell-env <CinterForgeOS repo>')
