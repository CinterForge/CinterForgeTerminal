"""CinterForgeVM — ownership of the one managed Colima ``t`` VM bootstrap.

PURPOSE
    Keep the existing developer/installed VM state root, isolated Colima and
    Docker environment, profile lock, host mount discovery/YAML normalization,
    runtime emulation settings and VM/Docker start/reuse decision together.
    This is an extraction of the production implementations, not a replacement
    VM driver, a new daemon, or an alternate architecture.

CLASS / METHODS
    CinterForgeVM.state_root: developer Builds versus installed user-cache
        state selection, symlink checks and short Lima socket path constraints.
    environment: construct the process-local HOME, COLIMA_HOME, LIMA_HOME,
        Docker socket and pinned Setup toolchain PATH for the owned profile.
    managed_vm_lock: cross-process flock protecting profile mutations.
    mac_mounted_volumes / mac_host_mounts: macOS mount discovery and the
        existing single writable root mount under /host/mac.
    colima_mount_entries / configure_colima_host_mounts: compare and update
        only the owned t-profile's mounted directory topology.
    configure_colima_runtime_emulation: preserve QEMU x86_64 and Wine/MSYS2
        settings, leaving Setup's independent c/w profiles alone.
    ensure_docker_locked: retain current Docker health, foreign-container
        guard, existing-profile reuse, Colima startup and readiness checks.

STARTUP / SHUTDOWN / DEPENDENCIES
    The canonical webapp/CinterForgeTerminal.py service owns exactly one
    CinterForgeVM instance, called directly by the HTTP/runtime owners.
    No lifecycle code calls it and no forwarding compatibility functions remain.
    No import of that service occurs here (no circular imports or side effects).
    Live logging, cancellation and guest-container ownership operations are
    supplied by the service as explicit callbacks at invocation time. VM stop,
    watchdog lifetime and guest Bash/VNC remain in the existing service until
    their ownership contracts can be extracted independently.

CRITICAL INVARIANTS
    No Setup/download/recovery on a normal app launch; no extra VM/profile;
    never kill unrelated containers or remove a saved VM disk; preserve the
    actual Steam library logs and verified developer Builds routing. A failed
    cloud-final unit is not automatically proof of failed SSH. No behavior or
    boot timeout changes are intended by this source-only extraction.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import threading

if os.name != 'nt':
    import fcntl


class CinterForgeVM:
    """Boot/reuse one runtime VM; its caller retains worker and guest ownership."""

    def __init__(self, profile: str, *, real_login_home):
        self.profile = profile
        self.real_login_home = real_login_home

    def state_root(self) -> Path:
        """VM scratch: sibling Builds for development, macOS cache when installed.

        Steam is strictly applications and persistent user data. No Lima/Colima
        disks, Docker images, caches, or temporary files may be created there.
        Normal installed-app startup must not create a developer Builds directory.
        """
        installed = os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed'
        if installed:
            value = os.environ.get('CINTER_STEAMAPPS', '')
            if not value:
                raise RuntimeError('Installed DevTools require the real Steam library')
            steam = Path(value).expanduser()
            if steam.name.lower() != 'steamapps' or steam.is_symlink() or not steam.is_dir():
                raise RuntimeError('Installed DevTools require an existing real steamapps directory')
            if platform.system() != 'Darwin':
                raise RuntimeError('Managed macOS guest VM requires a macOS runtime')
            # A short Lima socket path is mandatory on macOS. Cache contains only
            # disposable app-owned VM state; installed DevTools live in selected Builds.
            roots = [self.real_login_home() / 'Library/Caches/CFTVM/T']
        else:
            configured = os.environ.get('CINTER_BUILDS_ROOT', '')
            if not configured:
                raise RuntimeError('Developer VM state requires explicit CINTER_BUILDS_ROOT')
            source = Path(configured).expanduser()
            if source.name != 'Builds' or source.is_symlink() or not source.is_dir():
                raise RuntimeError('CINTER_BUILDS_ROOT must identify the existing developer Builds directory')
            # The explicit repository Builds is authoritative. Older developer
            # stages can also contain nested signed .app/Contents/Resources/Builds;
            # those copies are read-only and must not hold VM state. Prefer the
            # shortest *containing* Builds, not a home-directory fallback.
            roots = [parent / 'CFTVM/T' for parent in reversed((source, *source.parents))
                     if parent.name == 'Builds' and parent.is_dir() and not parent.is_symlink()]
        tail = '/l/colima-t/ssh.sock.1234567890123456'
        for root in roots:
            if len(os.fsencode(str(root) + tail)) < 96:
                if root.is_symlink() or (root.exists() and not root.is_dir()):
                    raise RuntimeError('Unsafe managed VM state: ' + str(root))
                root.mkdir(parents=True, exist_ok=True)
                return root
        raise RuntimeError('Managed VM socket root too long under developer Builds or user cache')


    def environment(self, sdk: Path, *, state_root) -> dict[str, str]:
        root = state_root()
        for rel in ('h', 'c', 'l', 'cache', 'cache/colima',
                    'd', 'config', 'tmp'):
            path = root / rel
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise RuntimeError('Unsafe managed VM directory: ' + str(path))
            path.mkdir(parents=True, exist_ok=True)
        lima_home = root / 'l'
        socket = lima_home / ('colima-' + self.profile) / 'ssh.sock.1234567890123456'
        if len(os.fsencode(socket)) >= 100:
            raise RuntimeError('Managed VM socket path too long within developer Builds or user cache: ' + str(socket))
        env = dict(os.environ)
        for key in ('DOCKER_CONTEXT', 'DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
            env.pop(key, None)
        # Colima owns the single t daemon socket.  Connect to it directly: the
        # Docker context JSON is disposable and may be absent after VM restart,
        # particularly in the installed-app cache.  DOCKER_CONTEXT would override
        # DOCKER_HOST and fail with `context colima-t not found` despite a healthy VM.
        socket_url = 'unix://' + str(root / 'c' / self.profile / 'docker.sock')
        env.update(HOME=str(root/'h'), COLIMA_HOME=str(root/'c'),
                   LIMA_HOME=str(lima_home), COLIMA_CACHE_HOME=str(root/'cache/colima'),
                   DOCKER_CONFIG=str(root/'d'), XDG_CONFIG_HOME=str(root/'config'),
                   XDG_CACHE_HOME=str(root/'cache'), TMPDIR=str(root/'tmp'),
                   DOCKER_HOST=socket_url,
                   PATH=os.pathsep.join([str(sdk/'mac/qemu/bin'),
                                         str(sdk/'mac/lima/bin'), str(sdk/'mac/env/bin'),
                                         os.environ.get('PATH', '')]))
        return env


    @contextmanager
    def managed_vm_lock(self, env: dict[str, str]):
        """Serialize Colima mutations across native and Code-OSS Session processes.

        Linux and Windows guest shells start as independent children. A thread lock
        cannot protect against two concurrent `colima start` invocations, which can
        race for the same Lima host-agent PID/socket and leave a stale Docker context.
        The lock resides in the permitted app-owned cache/Builds VM state only.
        """
        home = Path(env['COLIMA_HOME'])
        lock_file = home / 'CinterForgeTerminalGuestVM.lock'
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(lock_file, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


    def mac_mounted_volumes(self) -> list[Path]:
        """Discover visible host volumes; preserve spaces in the *host* paths.

        Sharing /Volumes itself covers volume names containing spaces, which
        Colima cannot accept as individual mount locations. Extra mounts are
        provided for volumes with space-free names and nonstandard mount points.
        """
        if platform.system() != 'Darwin':
            return []
        try:
            result = subprocess.run(['/sbin/mount'], capture_output=True, text=True,
                                    check=False, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode or not isinstance(getattr(result, 'stdout', None), str):
            return []
        found: set[Path] = set()
        for line in result.stdout.splitlines():
            match = re.search(r' on (.+?) \(([^,)]+)', line)
            if not match:
                continue
            location = Path(match.group(1).replace('\\040', ' '))
            filesystem = match.group(2).lower()
            # Pseudo-filesystems are not user drives, and exposing /dev would
            # obscure Linux's actual device filesystem.
            if (location == Path('/') or not location.is_dir() or
                    filesystem in ('devfs', 'procfs', 'autofs', 'fdesc', 'synthfs') or
                    location == Path('/dev')):
                continue
            found.add(location)
        return sorted(found, key=str)


    def mac_host_mounts(self) -> list[tuple[Path, str]]:
        """Share the entire Mac filesystem once, subject to macOS permissions.

        Lima/virtiofs does not support overlapping host mounts. Sharing both `/`
        and `/Volumes` (or individual `/Volumes/*` disks) creates overlapping
        locations, even when the guest destinations are distinct. One writable
        root share includes the host's mounted-volume namespace and preserves
        paths containing spaces without any app-defined drive or folder allowlist.
        This does not bypass macOS privacy permissions or Virtualization.framework
        limitations on nested host mounts.
        """
        return [(Path('/'), '/host/mac')]


    def colima_mount_entries(self, yaml: str) -> list[tuple[str, str, bool]] | None:
        """Read only the narrow mount schema that our Colima profile uses.

        Colima may serialize unquoted scalars when saving its configuration. Treat
        semantically equivalent settings as equal instead of restarting a running
        VM merely because YAML formatting changed. Unknown mount syntax is never
        assumed equivalent to the requested full-root share.
        """
        match = re.search(r'(?ms)^mounts:[^\n]*\n(.*?)(?=^[a-zA-Z][a-zA-Z0-9_-]*:|\Z)', yaml)
        if match is None:
            return None

        def scalar(raw: str) -> str:
            raw = raw.strip()
            if raw.startswith('"'):
                try:
                    result = json.loads(raw)
                    return result if isinstance(result, str) else raw
                except ValueError:
                    return raw
            if len(raw) >= 2 and raw[0] == raw[-1] == "'":
                return raw[1:-1].replace("''", "'")
            return raw

        records: list[dict[str, str]] = []
        for line in match.group(1).splitlines():
            item = re.match(r'^\s*-\s+location:\s*(.*?)\s*$', line)
            if item:
                records.append({'location': scalar(item.group(1))})
                continue
            attribute = re.match(r'^\s+(mountPoint|writable):\s*(.*?)\s*$', line)
            if attribute and records:
                records[-1][attribute.group(1)] = scalar(attribute.group(2))
            elif line.strip() and not line.lstrip().startswith('#'):
                return None
        if not records or any(set(item) != {'location', 'mountPoint', 'writable'} for item in records):
            return None
        if any(item['writable'] not in ('true', 'false') for item in records):
            return None
        return [(item['location'], item['mountPoint'], item['writable'] == 'true')
                for item in records]


    def configure_colima_host_mounts(self, env: dict[str, str], mounts: list[tuple[Path, str]],
                                      *, dry_run: bool = False) -> bool:
        """Change only the owned t-profile's mount list, never another profile.

        Returns whether the mount topology changed. A live VM must be restarted
        *only after* its running containers have been checked for foreign owners.
        """
        profile = Path(env['COLIMA_HOME']) / self.profile
        config = profile / 'colima.yaml'
        if profile.is_symlink() or config.is_symlink():
            raise RuntimeError('Managed Terminal VM configuration must not be a symlink')
        if not dry_run:
            profile.mkdir(parents=True, exist_ok=True)
        original = config.read_text('utf-8') if config.is_file() else ''
        desired = [(str(host), guest, True) for host, guest in mounts]
        if self.colima_mount_entries(original) == desired:
            return False
        block = 'mounts:\n' + ''.join(
            '  - location: ' + json.dumps(str(host)) + '\n'
            '    mountPoint: ' + json.dumps(guest) + '\n'
            '    writable: true\n'
            for host, guest in mounts)
        keys = list(re.finditer(r'(?m)^[a-zA-Z][a-zA-Z0-9_-]*:[^\n]*$', original))
        entry = next((idx for idx, item in enumerate(keys)
                      if item.group().split(':', 1)[0] == 'mounts'), None)
        if entry is None:
            updated = original.rstrip() + ('\n\n' if original.strip() else '') + block
        else:
            beginning = keys[entry].start()
            ending = keys[entry + 1].start() if entry + 1 < len(keys) else len(original)
            updated = original[:beginning] + block + original[ending:]
        if dry_run:
            return True
        temporary = config.with_name('colima.yaml.pending')
        if temporary.is_symlink():
            raise RuntimeError('Unsafe managed Terminal VM pending configuration')
        temporary.write_text(updated, encoding='utf-8')
        os.replace(temporary, config)
        return True


    def configure_colima_runtime_emulation(self, env: dict[str, str], *, dry_run: bool = False) -> bool:
        """Ensure the *runtime t profile* uses QEMU binfmt, never Rosetta.

        Apple Silicon Rosetta can abort Wine/MSYS2 Bash during rt_sigreturn
        (x86 AVX-state assertion, exit 133). Do not alter the working Setup w/c
        profiles or the separate LinuxKit Bedrock appliance. For existing t
        profiles, normalize only explicit stale flags; a new Colima profile is
        configured by the matching flags on `colima start`.
        """
        profile = Path(env['COLIMA_HOME']) / self.profile
        config = profile / 'colima.yaml'
        if profile.is_symlink() or config.is_symlink():
            raise RuntimeError('Managed Terminal VM configuration must not be a symlink')
        if not config.is_file():
            return False
        original = config.read_text(encoding='utf-8')
        desired = {'rosetta': 'false', 'binfmt': 'false',
                   'arch': 'x86_64', 'vmType': 'qemu', 'mountType': '9p'}
        updated = original
        for key, value in desired.items():
            pattern = r'(?m)^(' + re.escape(key) + r':)[^\n]*$'
            if re.search(pattern, updated):
                updated = re.sub(pattern, lambda match: match.group(1) + ' ' + value, updated)
            else:
                # An earlier ARM64/VZ profile may omit explicit architecture
                # fields. Leaving them absent lets Colima reuse the wrong ABI.
                updated = key + ': ' + value + '\n' + updated
        if updated == original:
            return False
        if dry_run:
            return True
        pending = config.with_name('colima.yaml.emulation.pending')
        if pending.is_symlink():
            raise RuntimeError('Unsafe managed Terminal VM pending configuration')
        pending.write_text(updated, encoding='utf-8')
        os.replace(pending, config)
        return True


    def ensure_docker_locked(self, sdk: Path, env: dict[str, str], workspace: Path | None,
                              cancel_event: threading.Event | None = None, *,
                              host_mounts, configure_host_mounts,
                              configure_runtime_emulation, run_checked,
                              progress, guest_name, stop_owned_guests_locked) -> str:
        colima = str(sdk / 'mac/env/bin/colima')
        docker = str(sdk / 'mac/env/bin/docker')
        mounts = host_mounts()

        def call(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
            return subprocess.run(argv, env=env, check=False, capture_output=True,
                                  text=True, timeout=timeout)

        def guest_directory(directory: str) -> bool:
            probe = call([colima, 'ssh', '--profile', self.profile, '--', 'test', '-d', directory])
            return probe.returncode == 0

        def active_containers(via_guest: bool) -> bool:
            argv = ([colima, 'ssh', '--profile', self.profile, '--', 'docker', 'ps', '--quiet']
                    if via_guest else [docker, 'ps', '--quiet'])
            result = call(argv, timeout=35)
            if result.returncode:
                raise RuntimeError('Managed Terminal VM state cannot be verified; refusing '
                                   'to restart a potentially active guest: ' +
                                   (result.stderr or result.stdout).strip()[:240])
            return bool(result.stdout.strip())

        def recover_stale_owned_guests() -> None:
            # A crashed Terminal may leave its owned container
            # running inside this dedicated VM. Never sacrifice foreign workloads.
            listing = call([docker, 'ps', '--format',
                            '{{.Names}}|{{.Label "io.cinterforge.terminal"}}'], timeout=35)
            if listing.returncode:
                raise RuntimeError('Cannot inspect managed VM container ownership')
            foreign = []
            for row in listing.stdout.splitlines():
                name, _, owner = row.partition('|')
                if name not in (guest_name('linux'), guest_name('win')) or owner != self.profile:
                    foreign.append(name)
            if foreign:
                raise RuntimeError('Cannot reconfigure Terminal VM while unrelated containers run: ' +
                                   ', '.join(foreign))
            stop_owned_guests_locked(docker, env)

        # Setup verifies the native amd64 Docker daemon, but installed-app boot
        # previously only checked its VERSION. Colima ignores VM type/architecture
        # flags once a profile exists; an old arm64/VZ cache can therefore appear
        # healthy while immediately killing every linux/amd64 container.
        installed = os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed'

        def verified_installed_architecture() -> str:
            probe = call([docker, 'info', '--format', '{{.Architecture}}'], timeout=30)
            if probe.returncode:
                raise RuntimeError('Cannot verify installed Terminal guest Docker architecture: ' +
                                   (probe.stderr or probe.stdout).strip()[-400:])
            return probe.stdout.strip().lower()

        def require_native_installed_architecture() -> None:
            actual = verified_installed_architecture()
            if actual not in ('amd64', 'x86_64'):
                # Colima's VM architecture is immutable after profile creation.
                # Deleting it from normal app launch destroys user cache and makes
                # Colima download a fresh Ubuntu disk behind the splash screen.
                # Preserve it for an explicit, separately consented repair; never
                # try to run amd64 Debian/Wine against the wrong VM.
                raise RuntimeError('Installed Terminal t guest is ' + actual +
                                   ', not native x86-64. Preserving the existing '
                                   'Colima profile; startup will not delete it or '
                                   'download a replacement VM. Repair the managed '
                                   'guest explicitly before using Linux/Windows Bash')

        # Check the app-owned profile's *saved* topology as well as live paths.
        # A legacy t-profile may still be running with the old overlapping mount
        # config. A healthy Docker socket alone cannot prove that all host drives
        # will survive the next Colima start.
        required = ['/host/mac', '/host/mac' + str(sdk)]
        if workspace is not None:
            required.append('/host/mac' + str(workspace))
        installed_profile = bool(env.get('COLIMA_HOME')) and (
            Path(env['COLIMA_HOME']) / self.profile / 'colima.yaml').is_file()
        mount_changed = configure_host_mounts(env, mounts, dry_run=True)
        # Installed profiles have immutable VM type/architecture. Updating their
        # YAML cannot convert an already-created ARM/VZ disk to AMD64/QEMU.
        # Inspect the *running* engine rather than rewriting these settings.
        emulation_changed = (False if installed and installed_profile else
                             configure_runtime_emulation(env, dry_run=True))
        config_changed = mount_changed or emulation_changed
        ping = call([docker, 'info', '--format', '{{.ServerVersion}}'], timeout=20)
        if ping.returncode == 0 and installed:
            require_native_installed_architecture()
        if ping.returncode == 0:
            missing = [path for path in required if not guest_directory(path)]
            if not missing and not config_changed:
                return docker  # One already-ready VM; every tab uses docker exec.
            if active_containers(via_guest=False):
                recover_stale_owned_guests()  # Verifies labels before stopping.
            reason = ('Rosetta translator disabled for real Wine/MSYS2 Bash'
                      if emulation_changed else
                      'outdated overlapping host mount configuration' if mount_changed else
                      'inaccessible host paths: ' + ', '.join(missing))
            progress('Reconfiguring owned Terminal VM: ' + reason)
            run_checked([colima, 'stop', self.profile], env,
                         title='Stopping idle guest VM for corrected host mounts', timeout=120)
        else:
            status = call([colima, 'status', self.profile])
            if status.returncode == 0:
                if active_containers(via_guest=True):
                    raise RuntimeError('Managed Terminal VM has live guest containers '
                                       'but its Docker socket is unavailable; refusing restart')
                run_checked([colima, 'stop', self.profile], env,
                             title='Recovering idle VM with stale Docker socket', timeout=120)

        configure_host_mounts(env, mounts)
        if not installed or not installed_profile:
            configure_runtime_emulation(env)
        progress('Sharing accessible Mac filesystem under /host/mac')
        # Only the terminal-owned runtime profile t changes. Boot the actual
        # QEMU x86_64 Linux guest; Wine/MSYS2 runs natively inside that guest.
        # Do not run amd64 Wine through ARM64 binfmt or Rosetta user translation.
        # Pass the full-root share explicitly. On first profile creation Colima
        # constructs its configuration from CLI flags; --save-config otherwise
        # writes the default mounts over the YAML edited above. The three-part
        # flag preserves the nondefault guest mount point and write permission.
        # Keep the YAML normalization for existing profiles and the live path
        # probes below: neither a successful VM boot nor a saved config proves
        # that VirtioFS actually mounted the host's source tree.
        # Only initial profile creation needs immutable QEMU/architecture flags.
        # Reusing a saved installed profile with those flags causes warnings and
        # cannot change its VM. Do not rebuild an existing profile at app launch.
        args = ([colima, 'start', self.profile, '--runtime', 'docker']
                if installed and installed_profile else
                [colima, 'start', self.profile, '--runtime', 'docker',
                 '--vm-type', 'qemu', '--mount-type', '9p',
                 '--mount', '/:/host/mac:w', '--arch', 'x86_64',
                 '--cpus', '4', '--memory', '8', '--disk', '32', '--save-config'])
        if installed and not installed_profile:
            progress('Initializing new x86-64 t profile (no existing VM to replace)')
        # The saved installed t profile may require several minutes to bring SSH
        # up even when cloud-init reports a failed final-stage unit. Let Colima's
        # actual SSH result, not that message alone, determine readiness.
        # Do not replace its disk or repeatedly boot the same broken guest.
        run_checked(args, env, title='Starting managed guest VM with host volumes',
                     timeout=600, cancel_event=cancel_event)
        daemon = call([docker, 'info', '--format', '{{.ServerVersion}}'], timeout=45)
        if daemon.returncode:
            raise RuntimeError('Managed Terminal Docker socket still unavailable after VM start: ' +
                               (daemon.stderr or daemon.stdout).strip()[:240])
        if installed:
            require_native_installed_architecture()
        missing = [path for path in required if not guest_directory(path)]
        if missing:
            raise RuntimeError('Managed Terminal VM cannot access shared Mac paths ' +
                               ', '.join(missing) + '; refusing a broken Docker bind mount')
        return docker

