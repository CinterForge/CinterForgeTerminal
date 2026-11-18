"""CinterForgeLifecycleVM — dedicated Setup/Build/Test Colima and Docker owner.

PURPOSE
    Owns the developer t profile under the SAME short Builds/CFTVM/T path
    used before the refactor. This code is deliberately independent of
    webapp/runtime/CinterForgeVM.py: lifecycle must never import or start
    the application's HTTP service just to build/test a VM.

METHODS / ENTRY POINTS
    environment/state_root         Isolated developer Colima/Docker paths.
    managed_vm_lock                Cross-process profile mutation lock.
    mac_host_mounts/configure_*    Pinned QEMU x86-64 topology and /host/mac.
    ensure_docker_locked           Existing VM reuse / startup and Docker probe.
    native_owner_active            Reject collision with live native watchdog.
    stop                           Stop only labeled guests and owned t QEMU.
    run_checked/progress           Live command output and bounded heartbeat.

LIFECYCLE CONTRACT
    Setup alone provisions. Build/Test reuse pinned tools and state, never
    download/install or repair Build dependencies. Every lifecycle invocation
    that starts t releases it on success, exception and interruption.
    It is intentional that some implementation is duplicated with the
    installed-app runtime: future disk locations may diverge, but there must
    be NO source imports or calls between the two ownership layers.
"""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import threading
import time

if os.name != 'nt':
    import fcntl

class CinterForgeLifecycleVM:
    """Boot/reuse one runtime VM; its caller retains worker and guest ownership."""

    def __init__(self, builds: Path):
        """Own only the Setup/Build/Test t backend in the canonical Builds tree."""
        self.builds = builds.resolve(strict=True)
        if self.builds.name != 'Builds' or self.builds.is_symlink():
            raise RuntimeError('Lifecycle VM requires the real sibling Builds directory')
        self.profile = 't'

    def state_root(self) -> Path:
        """Use the very same short Builds/CFTVM/T location as Phase 3.

        No installed-app Steam SDK lookup, app cache, HTTP server or DevTools
        receipt participates in lifecycle execution. A native app's active
        owner is checked separately before mutation.
        """
        roots = [parent / 'CFTVM/T'
                 for parent in reversed((self.builds, *self.builds.parents))
                 if parent.name == 'Builds' and parent.is_dir() and not parent.is_symlink()]
        tail = '/l/colima-t/ssh.sock.1234567890123456'
        for root in roots:
            if len(os.fsencode(str(root) + tail)) < 96:
                if root.is_symlink() or (root.exists() and not root.is_dir()):
                    raise RuntimeError('Unsafe lifecycle VM state: ' + str(root))
                root.mkdir(parents=True, exist_ok=True)
                return root
        raise RuntimeError('Managed lifecycle VM socket root too long within Builds')

    def environment(self, sdk: Path) -> dict[str, str]:
        """Build-owned isolated Colima/Docker env, independent of app runtime."""
        root = self.state_root()
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
                             cancel_event: threading.Event | None = None,
                             min_disk_gib: int | None = None) -> str:
        """Boot/reuse t; disk growth is authorized by Setup, never Build/runtime."""
        if min_disk_gib is not None and (min_disk_gib < 32 or min_disk_gib > 1024):
            raise ValueError('Invalid managed t disk capacity requested')
        configure_host_mounts = self.configure_colima_host_mounts
        configure_runtime_emulation = self.configure_colima_runtime_emulation
        run_checked = self.run_checked
        progress = self.progress
        guest_name = self.guest_name
        stop_owned_guests_locked = self.stop_owned_guests_locked
        colima = str(sdk / 'mac/env/bin/colima')
        docker = str(sdk / 'mac/env/bin/docker')
        mounts = self.mac_host_mounts()

        def call(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
            return subprocess.run(argv, env=env, check=False, capture_output=True,
                                  text=True, timeout=timeout)

        def guest_directory(directory: str) -> bool:
            probe = call([colima, 'ssh', '--profile', self.profile, '--', 'test', '-d', directory])
            return probe.returncode == 0

        def verify_setup_disk_space() -> None:
            if min_disk_gib is None:
                return
            # Catch an unexpanded/full t filesystem before a long Docker image
            # build or Wine preflight.  Build/runtime never repair storage.
            space = call([colima, 'ssh', '--profile', self.profile, '--',
                          'df', '-Pk', '/var/lib/docker'], timeout=35)
            rows = space.stdout.strip().splitlines() if space.returncode == 0 else []
            try:
                fields = rows[-1].split()
                capacity_kib, available_kib = int(fields[1]), int(fields[3])
            except (IndexError, ValueError) as exc:
                raise RuntimeError('Cannot measure managed t Docker filesystem: '
                                   + (space.stderr or space.stdout)[-600:]) from exc
            progress(f'Owned t Docker storage: {capacity_kib // 1048576} GiB capacity, '
                     f'{available_kib // 1048576} GiB free')
            if capacity_kib < min_disk_gib * 1048576 * 3 // 4:
                raise RuntimeError('Managed t Docker disk did not grow to the Setup '
                                   f'capacity of {min_disk_gib} GiB; preserving VM state')
            if available_kib < 4 * 1048576:
                raise RuntimeError('Managed t Docker disk has under 4 GiB free after '
                                   'Setup disk provisioning; preserving VM state')

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
        installed = False  # Setup/Build/Test never consume installed-app cache

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
        profile_file = Path(env['COLIMA_HOME']) / self.profile / 'colima.yaml'
        installed_profile = bool(env.get('COLIMA_HOME')) and profile_file.is_file()
        disk_growth_needed = False
        if min_disk_gib is not None and installed_profile:
            # Colima can increase an existing disk on start; do not delete the
            # VM or its Docker volumes.  Never shrink a larger existing disk.
            match = re.search(r'(?m)^disk:\s*(\d+)\s*(?:#.*)?$',
                              profile_file.read_text(encoding='utf-8'))
            if match is None:
                raise RuntimeError('Cannot verify managed t disk size in ' + str(profile_file))
            disk_growth_needed = int(match.group(1)) < min_disk_gib
        mount_changed = configure_host_mounts(env, mounts, dry_run=True)
        # Installed profiles have immutable VM type/architecture. Updating their
        # YAML cannot convert an already-created ARM/VZ disk to AMD64/QEMU.
        # Inspect the *running* engine rather than rewriting these settings.
        emulation_changed = (False if installed and installed_profile else
                             configure_runtime_emulation(env, dry_run=True))
        config_changed = mount_changed or emulation_changed or disk_growth_needed
        ping = call([docker, 'info', '--format', '{{.ServerVersion}}'], timeout=20)
        if ping.returncode == 0 and installed:
            require_native_installed_architecture()
        if ping.returncode == 0:
            missing = [path for path in required if not guest_directory(path)]
            if not missing and not config_changed:
                verify_setup_disk_space()
                return docker  # One already-ready VM; every tab uses docker exec.
            if active_containers(via_guest=False):
                recover_stale_owned_guests()  # Verifies labels before stopping.
            reason = ('expanding Setup-owned t disk to ' + str(min_disk_gib) + ' GiB'
                      if disk_growth_needed else
                      'Rosetta translator disabled for real Wine/MSYS2 Bash'
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
        if installed_profile:
            args = [colima, 'start', self.profile, '--runtime', 'docker']
            if disk_growth_needed:
                # Existing x86-64 QEMU profile: grow in place, retaining its
                # images, volumes, guest identity, and all owned resources.
                args += ['--disk', str(min_disk_gib), '--save-config']
        else:
            args = [colima, 'start', self.profile, '--runtime', 'docker',
                    '--vm-type', 'qemu', '--mount-type', '9p',
                    '--mount', '/:/host/mac:w', '--arch', 'x86_64',
                    '--cpus', '4', '--memory', '8',
                    '--disk', str(min_disk_gib if min_disk_gib is not None else 32),
                    '--save-config']
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
        verify_setup_disk_space()
        return docker

    @staticmethod
    def progress(message: str) -> None:
        print('[TERMINAL AMD64] ' + message, flush=True)

    @staticmethod
    def guest_name(kind: str) -> str:
        if kind not in ('linux', 'win'):
            raise ValueError('Invalid managed lifecycle guest')
        return 'cinterforge-terminal-t-debian'

    @staticmethod
    def docker_call(docker: str, env: dict[str, str], *args: str,
                    timeout: int = 90) -> subprocess.CompletedProcess:
        return subprocess.run([docker, *args], env=env, text=True,
                              capture_output=True, check=False, timeout=timeout)

    def run_checked(self, command: list[str], env: dict[str, str], *, title: str,
                    timeout: int, cancel_event=None) -> None:
        """Show native Colima/Docker output live; never buffer behind a file.

        Any lifecycle runner already tees stdout/stderr into persistent Logs.
        This function only runs already-provisioned tools and retains their
        true exit codes. The caller owns cleanup after a failed boot.
        """
        started = time.monotonic()
        last = started
        self.progress(title)
        with subprocess.Popen(command, env=env) as process:
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError(title + ' cancelled')
                    elapsed = time.monotonic() - started
                    if elapsed > timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    if elapsed - (last - started) >= 15:
                        self.progress('WORKING ' + title + ' (' + str(int(elapsed)) + 's)')
                        last = time.monotonic()
                    time.sleep(0.25)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise
            if process.returncode:
                raise RuntimeError(title + ' failed (exit ' + str(process.returncode) + ')')
        self.progress('PASS ' + title + ' (' + str(round(time.monotonic() - started,1)) + 's)')

    def watchdog_owner(self, env: dict[str, str]) -> Path:
        return Path(env['COLIMA_HOME']) / 'CinterForgeTerminalLifetime.owner.json'

    def live_runtime_watchdog(self, native_pid: int, env: dict[str, str]) -> bool:
        """Read the app's ownership receipt, without importing app runtime."""
        receipt = self.watchdog_owner(env)
        if receipt.is_symlink() or not receipt.is_file():
            return False
        try:
            record = json.loads(receipt.read_text(encoding='utf-8'))
            pid = record.get('watchdog_pid')
            if (record.get('native_pid') != native_pid or
                    not isinstance(pid, int) or pid <= 1):
                return False
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, TypeError, PermissionError):
            return False

    def native_owner_active(self, env: dict[str, str]) -> bool:
        owner = self.watchdog_owner(env)
        if not owner.is_file() or owner.is_symlink():
            return False
        try:
            record = json.loads(owner.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError('Cannot read native guest ownership record; refusing collision') from exc
        pid = record.get('native_pid')
        return isinstance(pid, int) and self.live_runtime_watchdog(pid, env)

    def stop_owned_guests_locked(self, docker: str, env: dict[str, str]) -> None:
        """Stop only the shared Terminal-labeled container; never foreign jobs."""
        for name in {self.guest_name('linux'), self.guest_name('win')}:
            result = self.docker_call(docker, env, 'container', 'inspect', '--format',
                                      '{{index .Config.Labels "io.cinterforge.terminal"}}', name)
            if result.returncode:
                continue
            if result.stdout.strip() != self.profile:
                raise RuntimeError('Refusing to remove unrelated Docker container: ' + name)
            stopped = self.docker_call(docker, env, 'stop', '--time', '5', name, timeout=45)
            if stopped.returncode:
                running = self.docker_call(docker, env, 'inspect', '--format',
                                           '{{.State.Running}}', name)
                if running.returncode == 0 and running.stdout.strip() == 'true':
                    raise RuntimeError('Cannot stop owned Docker guest: ' + name)
            removed = self.docker_call(docker, env, 'rm', '-f', name)
            if removed.returncode:
                raise RuntimeError('Cannot remove owned Docker guest: ' + name)

    def owned_qemu_pids(self, env: dict[str, str], sdk: Path) -> list[int]:
        """Inspect only QEMU matching both the bundled binary and t state."""
        binary = str(sdk / 'mac/qemu/bin/qemu-system-x86_64')
        state = str(Path(env['LIMA_HOME']) / ('colima-' + self.profile))
        listing = subprocess.run(['/bin/ps', '-ww', '-axo', 'pid=,command='],
                                 check=False, text=True, capture_output=True, timeout=15)
        if listing.returncode:
            raise RuntimeError('Cannot inspect owned QEMU: ' + listing.stderr[-300:])
        found = []
        for row in listing.stdout.splitlines():
            pid, sep, command = row.strip().partition(' ')
            if (sep and pid.isdigit() and command.lstrip().startswith(binary + ' ')
                    and state in command):
                found.append(int(pid))
        return found

    def verify_owned_qemu_stopped(self, env: dict[str, str], sdk: Path) -> None:
        """End only this build-owned QEMU after the named Lima stop fails."""
        pids = self.owned_qemu_pids(env, sdk)
        if not pids:
            return
        lima = str(sdk / 'mac/lima/bin/limactl')
        result = subprocess.run([lima, 'stop', '--force', 'colima-' + self.profile],
                                env=env, check=False, text=True, capture_output=True, timeout=60)
        if result.returncode:
            self.progress('Lima t force stop: ' + (result.stderr or result.stdout)[-300:])
        for _ in range(10):
            if not self.owned_qemu_pids(env, sdk):
                return
            time.sleep(0.5)
        for pid in set(pids).intersection(self.owned_qemu_pids(env, sdk)):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _ in range(16):
            if not self.owned_qemu_pids(env, sdk):
                return
            time.sleep(0.5)
        for pid in self.owned_qemu_pids(env, sdk):
            if pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for _ in range(10):
            if not self.owned_qemu_pids(env, sdk):
                return
            time.sleep(0.5)
        raise RuntimeError('Lifecycle-owned t QEMU still running: ' +
                           ', '.join(map(str, self.owned_qemu_pids(env, sdk))))

    def stop(self, sdk: Path) -> None:
        """Release only this invocation's t VM, including interrupted starts.

        Before touching t, reject an active native owner. Colima may fail its
        status probe while QEMU is alive; the positively identified process
        is checked after Colima exits. No disk deletion or foreign kill.
        """
        if platform.system() != 'Darwin':
            return
        env = self.environment(sdk)
        colima = str(sdk / 'mac/env/bin/colima')
        docker = str(sdk / 'mac/env/bin/docker')
        with self.managed_vm_lock(env):
            if self.native_owner_active(env):
                raise RuntimeError('Native app owns t VM; lifecycle will not stop it')
            status = subprocess.run([colima, 'status', self.profile], env=env,
                                    check=False, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=25)
            if status.returncode == 0:
                engine = self.docker_call(docker, env, 'info', '--format',
                                          '{{.ServerVersion}}', timeout=20)
                if engine.returncode == 0:
                    self.stop_owned_guests_locked(docker, env)
                    foreign = self.docker_call(docker, env, 'ps', '--quiet', timeout=25)
                    if foreign.returncode or foreign.stdout.strip():
                        raise RuntimeError('Leaving t running: unknown containers may be active')
                else:
                    listing = subprocess.run(
                        [colima, 'ssh', '--profile', self.profile, '--', 'docker', 'ps',
                         '--format', '{{.Names}}|{{.Label "io.cinterforge.terminal"}}'],
                        env=env, text=True, capture_output=True, timeout=35, check=False)
                    if listing.returncode == 0:
                        for line in listing.stdout.splitlines():
                            name, sep, owner = line.partition('|')
                            if not sep or name != self.guest_name('linux') or owner != self.profile:
                                raise RuntimeError('Leaving VM running: unowned container ' + name)
                        for line in listing.stdout.splitlines():
                            name = line.partition('|')[0]
                            removed = subprocess.run(
                                [colima, 'ssh', '--profile', self.profile, '--',
                                 'docker', 'stop', '--time', '5', name],
                                env=env, capture_output=True, text=True, timeout=45)
                            if removed.returncode:
                                raise RuntimeError('Cannot stop owned guest ' + name)
                    elif not self.owned_qemu_pids(env, sdk):
                        return
                    else:
                        self.progress('Owned t QEMU has no Docker/SSH; stopping private profile')
            elif not self.owned_qemu_pids(env, sdk) and not (
                    Path(env['COLIMA_HOME']) / self.profile / 'colima.yaml').is_file():
                return
            try:
                self.run_checked([colima, 'stop', self.profile], env,
                                 title='Stopping lifecycle-owned t VM', timeout=120)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                self.progress('Colima t stop incomplete: ' + str(error))
            self.verify_owned_qemu_stopped(env, sdk)
