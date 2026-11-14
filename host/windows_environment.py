"""Actual Windows PE cross-compiler + Wine for Mac-hosted CinterForgeTerminal.

Prefer existing host MinGW/Wine tools. Otherwise provision one conventional
Linux/amd64 Docker image (MinGW-w64 + Wine + CMake + Ninja), export that exact
image into sibling Builds for offline reuse, and execute real PE binaries there.
The Wine smoke does NOT establish native Windows WebView2 acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import select
import sys
import shutil
import subprocess
import time
import uuid

SCHEMA = 'cinterforge.terminal.windows-cross-environment.v1'
IMAGE_NAME = 'cinterforge-terminal-windows-cross'
PINNED_WINE_VERSION = 'wine-11.15'
# Immutable AMD64 registry inputs. On Mac, fetch with the native (ARM64)
# Setup-owned skopeo and import them into the ONE x86-64 t Docker daemon.
# Docker/BuildKit inside QEMU must not depend on a short TLS handshake timeout
# while resolving registry manifests. The build still executes in x86-64 t.
AMD64_BASE_IMAGES = (
    ('ghcr.io/msys2/msys2-docker-build-base@sha256:19c2b7a96a437f71222656462e4fa5014245f2eb2633ac3988dd9cf208f51ed4',
     'cinterforge-terminal-msys2-base:19c2b7a96a437f71'),
    ('docker.io/library/debian@sha256:7378260c0a098d27ac206225db5e7d92b8c7c84ea7afccc86eeb513756bbd392',
     'cinterforge-terminal-debian-base:7378260c0a098d27'),
)

DOCKERFILE = Path(__file__).resolve().parent / 'docker/windows-cross.Dockerfile'
GUI_PREFLIGHT_SOURCE = DOCKERFILE.parent / 'gui_preflight.py'


def _image_recipe_sha256() -> str:
    # COPY source is a build input too; never silently reuse an older image.
    digest = hashlib.sha256(DOCKERFILE.read_bytes())
    digest.update(b'\0')
    digest.update(GUI_PREFLIGHT_SOURCE.read_bytes())
    return digest.hexdigest()

TOOL_VERSIONS = {
    'mingw-w64': ['x86_64-w64-mingw32-g++', '--version'],
    'mingw-w64-c': ['x86_64-w64-mingw32-gcc', '--version'],
    'wine': ['wine', '--version'],
    'windows-cmake': ['cmake', '--version'],
    'windows-ninja': ['ninja', '--version'],
}


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 600,
         quiet: bool = False, env: dict[str, str] | None = None,
         output_log: Path | None = None) -> subprocess.CompletedProcess:
    """Stream actual guest/Windows build output as it happens (and keep logs).

    Unlike subprocess.run(capture_output=True), a hung Wine or compiler child
    still shows its last activity and a 10-second elapsed-time heartbeat.
    """
    command = [str(c) for c in command]
    if not quiet:
        print('[WINDOWS CROSS] ' + ' '.join(command), flush=True)
    if output_log is not None:
        output_log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    last_heartbeat = started
    chunks: list[bytes] = []
    log = output_log.open('wb') if output_log is not None else None
    try:
        if log is not None:
            log.write(('$ ' + ' '.join(command) + '\n').encode())
            log.flush()
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0,
                                   start_new_session=(os.name == 'posix'))
        try:
            assert process.stdout is not None
            while True:
                now = time.monotonic()
                if now - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout, b''.join(chunks))
                ready, _, _ = select.select([process.stdout], [], [],
                                            min(1.0, max(0.0, timeout - (now - started))))
                if ready:
                    chunk = os.read(process.stdout.fileno(), 16384)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if log is not None:
                        log.write(chunk)
                        log.flush()
                    if not quiet:
                        output = getattr(sys.stdout, 'buffer', None)
                        if output is not None:
                            output.write(chunk)
                            output.flush()
                        else:
                            sys.stdout.write(chunk.decode('utf-8', errors='replace'))
                            sys.stdout.flush()
                if not quiet and time.monotonic() - last_heartbeat >= 10:
                    print(f'\n[WINDOWS CROSS] WORKING ({int(time.monotonic()-started)}s) '
                          + Path(command[0]).name + ' ' + ' '.join(command[1:4]), flush=True)
                    last_heartbeat = time.monotonic()
            result = process.wait(timeout=max(1, int(timeout - (time.monotonic()-started))))
        except BaseException:
            if process.poll() is None:
                if os.name == 'posix':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait(timeout=10)
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
        data = b''.join(chunks).decode('utf-8', errors='replace')
        # Native compiler output remains live and in Cinter's persistent log.
        # Preserve the last 2 MiB for caller validations and failure context.
        return subprocess.CompletedProcess(command, result, data[-2 * 1024 * 1024:])
    finally:
        if log is not None:
            log.close()


def _logged(command: list[str], *, cwd: Path | None = None, timeout: int = 600) -> None:
    """Stream and log native subprocess output with a 10-second heartbeat."""
    result = _run(command, cwd=cwd, timeout=timeout)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command,
                                            output=result.stdout[-4000:])


class WindowsCrossEnvironment:
    def __init__(self, builds: Path, *, repository: Path | None = None):
        self.builds = builds.resolve()
        self.repository = (repository or Path(__file__).resolve().parents[1]).resolve()
        if self.builds.name != 'Builds' or not self.builds.is_dir():
            raise RuntimeError(f'Windows toolchain requires the sibling Builds directory: {self.builds}')
        self.docker = None
        self.image = None
        self.provider = 'host'
        self.native_path = os.environ.get('PATH', '')
        self.container_kind = 'docker'
        self._wine_volume_checked = None

    def _native_ready(self) -> bool:
        """Find complete host tools in PATH OR their existing Builds stage.

        Reuse staged executables without introducing host-global installations.
        The native provider still validates tool versions before compilation.
        """
        candidates = (
            self.builds / 'CinterForgeTerminal/Toolchains/WindowsCross/bin',
            self.builds / 'CinterForgeTerminal/Toolchains/WindowsCross/usr/bin',
            self.builds / 'CinterForgeTerminal/Toolchains/Linux/WindowsCross/bin',
            self.builds / 'Toolchains/WindowsCross/bin',
            self.builds / 'Toolchains/MinGW/bin',
            self.builds / 'Toolchains/Wine/bin',
        )
        valid = [str(path) for path in candidates if path.is_dir() and not path.is_symlink()]
        self.native_path = os.pathsep.join([*valid, os.environ.get('PATH', '')])
        required = ('x86_64-w64-mingw32-gcc', 'x86_64-w64-mingw32-g++',
                    'wine', 'cmake', 'ninja')
        if not all(shutil.which(command, path=self.native_path) for command in required):
            return False
        # Do not silently select an older host Wine instead of patched Wine.
        # The optional host provider must match the exact Setup-managed version.
        version = _run(['wine', '--version'], quiet=True, timeout=15,
                       env=dict(os.environ, PATH=self.native_path))
        return version.returncode == 0 and version.stdout.strip() == PINNED_WINE_VERSION

    @property
    def stage(self) -> Path:
        return self.builds / 'CinterForgeTerminal/Toolchains/WindowsCross'

    def _docker_available(self) -> str:
        """Reuse an already operational Docker or Podman engine.

        Both can execute the exact same pinned Linux/amd64 build recipe and
        export the resulting image to Builds. On macOS the managed Docker
        bootstrap remains the final fallback; no manual Docker install is
        required if Setup can provision it.
        """
        engines = []
        bundled_cli = self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker'
        if platform.system() == 'Darwin':
            # The Mac bootstrap / LinuxKit c producer is ARM64 and must NEVER
            # build the Debian AMD64 Wine image. Setup enters the dedicated
            # QEMU x86-64 t backend only after Bedrock acceptance.
            if os.environ.get('DOCKER_CONTEXT') != 'colima-t':
                raise RuntimeError('Windows AMD64 Setup requires the t QEMU guest '
                                   'after Bedrock acceptance, not the ARM64 LinuxKit c producer')
            if not bundled_cli.is_file():
                raise RuntimeError('Mac Setup must stage its pinned managed Docker CLI first')
            engines.append(('docker', str(bundled_cli)))
        else:
            for kind in ('docker', 'podman'):
                executable = shutil.which(kind)
                if executable:
                    engines.append((kind, executable))
        failures = []
        for kind, engine in engines:
            command = ([engine, 'info', '--format', '{{.ServerVersion}}'] if kind == 'docker'
                       else [engine, 'info', '--format', '{{.Host.Arch}}'])
            health = _run(command, quiet=True, timeout=25)
            if health.returncode == 0:
                if platform.system() == 'Darwin':
                    architecture = _run([engine, 'info', '--format', '{{.Architecture}}'],
                                        quiet=True, timeout=25)
                    if (architecture.returncode != 0 or
                            architecture.stdout.strip().lower() not in ('x86_64', 'amd64')):
                        raise RuntimeError('Refusing to build AMD64 Wine under non-x86-64 Docker daemon: ' +
                                           architecture.stdout.strip() + (architecture.stderr or '').strip())
                self.container_kind = kind
                print(f'[WINDOWS CROSS] {kind} engine ready: {health.stdout.strip()}', flush=True)
                return engine
            failures.append(f'{kind}: {health.stdout.strip()[-300:]}')
        # No ARM64 `ensure_docker()` fallback. An absent t engine is a failed
        # Setup prerequisite, never permission to boot c and emulate AMD64.
        raise RuntimeError('No operational Docker/Podman engine and no complete MinGW/Wine '
                           'host or staged toolchain. Windows PE build cannot start. '
                           + ('; '.join(failures) if failures else
                              'No container executable was found.'))

    def _image_identity(self) -> tuple[str, str]:
        if not DOCKERFILE.is_file():
            raise RuntimeError(f'Windows cross image recipe missing: {DOCKERFILE}')
        digest = _image_recipe_sha256()
        return f'{IMAGE_NAME}:{digest[:16]}', digest

    def _has_image(self, image: str) -> bool:
        assert self.docker
        return _run([self.docker, 'image', 'inspect', image], quiet=True, timeout=30).returncode == 0

    @classmethod
    def validate_staged(cls, builds: Path) -> tuple[Path, dict]:
        """Verify the offline Docker image without consulting the Docker daemon."""
        digest = _image_recipe_sha256()
        staged = builds / 'CinterForgeTerminal/Toolchains/WindowsCross'
        archive = staged / f'{digest[:16]}.tar'
        manifest = staged / f'{digest[:16]}.json'
        if not archive.is_file() or not manifest.is_file() or archive.is_symlink():
            raise RuntimeError(f'Verified offline Windows cross image missing: {archive}')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('schema') != SCHEMA or data.get('dockerfile_sha256') != digest or
                data.get('image') != f'{IMAGE_NAME}:{digest[:16]}' or
                data.get('wine_version') != PINNED_WINE_VERSION or
                data.get('archive_sha256') != cls._hash_file(archive)):
            raise RuntimeError(f'Windows cross-image stage is modified or corrupt: {archive}')
        return staged, data

    def _mac_offline_base_dockerfile(self) -> Path:
        """Load pinned AMD64 base images via native Mac HTTPS, never via QEMU TLS.

        This is Setup-only. The generated Dockerfile uses exact, verified local
        Docker tags so BuildKit has no registry dependency for its FROM layers.
        No second VM/daemon is used, and no authored source is modified.
        """
        if os.environ.get('DOCKER_CONTEXT') != 'colima-t':
            raise RuntimeError('Windows registry import requires the x86-64 t guest')
        assert self.docker
        skopeo = self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/skopeo'
        if not skopeo.is_file() or skopeo.is_symlink():
            raise RuntimeError('Mac Bootstrap did not provision pinned native skopeo; '
                               're-run ./Cinter setup all')
        self.stage.mkdir(parents=True, exist_ok=True)
        source = DOCKERFILE.read_text(encoding='utf-8')
        for remote, local in AMD64_BASE_IMAGES:
            if source.count(remote) != 1:
                raise RuntimeError('Pinned Windows base reference missing/duplicated: ' + remote)
            probe = _run([self.docker, 'image', 'inspect', '--format',
                          '{{.Architecture}}', local], quiet=True, timeout=30)
            if probe.returncode != 0:
                # The download occurs on native macOS, where HTTPS/registry
                # handshakes are not slowed by QEMU x86 full-system emulation.
                cache = self.builds / 'Intermediates/CinterForgeTerminal/RegistryInputs'
                cache.mkdir(parents=True, exist_ok=True)
                # Conda's containers-common currently provides a legacy v1
                # registries.conf, which pinned Skopeo 1.24 rejects. Select
                # this explicit v2 config for native registry copies only;
                # leave the packaged Conda environment and host config alone.
                registry_conf = cache / 'registries-v2.conf'
                if registry_conf.is_symlink():
                    raise RuntimeError('Unsafe managed registry config path: ' + str(registry_conf))
                registry_conf.write_text('unqualified-search-registries = []\n', encoding='utf-8')
                archive = cache / (local.replace(':', '-') + '.tar')
                if archive.exists() and archive.is_symlink():
                    raise RuntimeError('Unsafe managed registry download path: ' + str(archive))
                archive.unlink(missing_ok=True)
                print('[WINDOWS CROSS] Downloading pinned AMD64 source on native Mac: '
                      + remote, flush=True)
                try:
                    _logged([str(skopeo), '--registries-conf', str(registry_conf),
                             '--override-os', 'linux', '--override-arch', 'amd64', 'copy',
                             'docker://' + remote,
                             'docker-archive:' + str(archive) + ':' + local], timeout=2400)
                    if not archive.is_file() or not archive.stat().st_size:
                        raise RuntimeError('Registry downloader produced no OCI archive: ' + remote)
                    print('[WINDOWS CROSS] Importing into the single x86-64 t guest: '
                          + local, flush=True)
                    _logged([self.docker, 'load', '--input', str(archive)], timeout=1800)
                finally:
                    # Downloaded inputs are disposable; the guest image layer
                    # cache and final verified toolchain image provide reuse.
                    archive.unlink(missing_ok=True)
                probe = _run([self.docker, 'image', 'inspect', '--format',
                              '{{.Architecture}}', local], quiet=True, timeout=30)
            if probe.returncode != 0 or probe.stdout.strip() != 'amd64':
                raise RuntimeError('Wrong/missing x86-64 Docker base image after native import '
                                   + local + ': ' + probe.stdout[-500:])
            print('[WINDOWS CROSS] PASS: pinned local AMD64 base ' + local, flush=True)
            source = source.replace(remote, local)
        output = self.stage / 'windows-cross.offline.Dockerfile'
        output.write_text(source, encoding='utf-8')
        return output

    def _ensure_image(self) -> None:
        assert self.docker
        self.stage.mkdir(parents=True, exist_ok=True)
        image, digest = self._image_identity()
        image_archive = self.stage / f'{digest[:16]}.tar'
        manifest = self.stage / f'{digest[:16]}.json'
        if image_archive.exists():
            self.validate_staged(self.builds)
            if not self._has_image(image):
                print(f'[WINDOWS CROSS] Restoring verified offline image from {image_archive}', flush=True)
                _logged([self.docker, 'load', '--input', str(image_archive)], timeout=900)
        elif not self._has_image(image):
            print('[WINDOWS CROSS] Provisioning pinned MinGW-w64, CMake, Ninja, and Wine image', flush=True)
            if self.container_kind == 'docker':
                # Docker's conda-forge CLI does not bundle Buildx. The macOS
                # bootstrap installs/activates its separate pinned plugin.
                # Fail closed instead of falling back to the deprecated
                # legacy builder, which rejects --progress and may disappear.
                buildx = _run([self.docker, 'buildx', 'version'], quiet=True, timeout=30)
                if buildx.returncode or 'buildx' not in buildx.stdout.lower():
                    raise RuntimeError('Windows cross image requires managed Docker Buildx: '
                                       + buildx.stdout[-800:])
                command = [self.docker, 'buildx', 'build', '--load', '--platform',
                           'linux/amd64', '--progress', 'plain']
            else:
                command = [self.docker, 'build', '--platform', 'linux/amd64']
            # The Mac path first imports both immutable AMD64 base images from
            # native HTTPS. BuildKit resolves only LOCAL base tags inside t.
            build_file = (self._mac_offline_base_dockerfile()
                          if platform.system() == 'Darwin' else DOCKERFILE)
            command += ['--tag', image, '--file', str(build_file), str(DOCKERFILE.parent)]
            _logged(command, timeout=2400)
        if not self._has_image(image):
            raise RuntimeError(f'Windows cross image was not created: {image}')
        if not image_archive.exists():
            print(f'[WINDOWS CROSS] Exporting offline toolchain image to {image_archive}', flush=True)
            temporary = image_archive.with_suffix('.tar.part')
            try:
                _logged([self.docker, 'save', '--output', str(temporary), image], timeout=900)
                archive_hash = self._hash_file(temporary)
                temporary.replace(image_archive)
                manifest.write_text(json.dumps({
                    'schema': SCHEMA, 'dockerfile_sha256': digest, 'image': image,
                    'archive_sha256': archive_hash, 'platform': 'linux/amd64',
                    'wine_version': PINNED_WINE_VERSION,
                }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            finally:
                temporary.unlink(missing_ok=True)
        self.image = image

    @staticmethod
    def _hash_file(path: Path) -> str:
        hashed = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hashed.update(block)
        return hashed.hexdigest()

    def ensure_portable_image(self) -> 'WindowsCrossEnvironment':
        """Require a transferrable Linux/amd64 MinGW+Wine image under Builds.

        A host-global compiler is not an acceptable substitute: an uploaded
        Toolchains tree must contain the complete cross-toolchain closure.
        """
        self.provider = 'docker-linux-amd64-wine'
        self.docker = self._docker_available()
        self._ensure_image()
        self.validate_staged(self.builds)
        return self

    def ensure(self) -> 'WindowsCrossEnvironment':
        # Setup produces a portable OCI closure first. Always USE the closure
        # that Setup verified, rather than silently switching to a different
        # Homebrew/global compiler and Wine installation afterwards.
        image, digest = self._image_identity()
        archive = self.stage / (digest[:16] + '.tar')
        if archive.is_file():
            return self.ensure_portable_image()
        if self._native_ready():
            print('[WINDOWS CROSS] Reusing host/staged MinGW-w64 and Wine', flush=True)
            self.provider = 'host'
            return self
        self.provider = 'docker-linux-amd64-wine'
        self.docker = self._docker_available()
        self._ensure_image()
        return self

    def path(self, path: Path) -> str:
        path = path.resolve()
        if self.provider == 'host':
            return str(path)
        if path == self.repository or path.is_relative_to(self.repository):
            return '/source/' + path.relative_to(self.repository).as_posix()
        if path == self.builds or path.is_relative_to(self.builds):
            return '/builds/' + path.relative_to(self.builds).as_posix()
        raise RuntimeError(f'Windows cross-build path is outside source/Builds: {path}')

    def _wine_volume_name(self) -> str:
        """Stable, private guest-native Wine state for this Builds tree/image.

        Docker stores named volumes on the managed Colima-w Linux disk, itself
        under the sibling Builds/CFTVM tree. Wine's many registry/drive writes
        must not be made through macOS VirtioFS into /builds.
        """
        if not self.image or ':' not in self.image:
            raise RuntimeError('Windows cross image has not been selected')
        image_key = self.image.rsplit(':', 1)[1]
        if not image_key or not all(c.isalnum() or c in '-_' for c in image_key):
            raise RuntimeError('Invalid Windows cross image identity')
        builds_key = hashlib.sha256(str(self.builds).encode('utf-8')).hexdigest()[:12]
        return f'cinterforge-terminal-wine-{builds_key}-{image_key}'

    def _wine_receipt(self) -> Path:
        return self.builds / 'Run/CinterForgeTerminal/WindowsCross/WineVolume.json'

    def _wine_volume_available(self) -> bool:
        if not self.docker:
            return False
        volume = self._wine_volume_name()
        result = _run([self.docker, 'volume', 'inspect', volume], quiet=True, timeout=30)
        return result.returncode == 0

    def prepare_wine_runtime(self) -> None:
        """Setup-only provisioning; Build/Publish may only reuse this volume."""
        if self.provider == 'host':
            return
        if not self.docker or not self.image:
            raise RuntimeError('Windows cross image must be provisioned before Wine Setup')
        volume = self._wine_volume_name()
        volume_was_missing = not self._wine_volume_available()
        if volume_was_missing:
            created = _run([self.docker, 'volume', 'create',
                            '--label', 'cinterforge.owner=CinterForgeTerminal',
                            '--label', f'cinterforge.image={self.image}', volume],
                           quiet=True, timeout=60)
            if created.returncode:
                raise RuntimeError('Cannot create managed guest-native Wine volume: '
                                   + created.stdout[-1000:])
        # The only root process here creates private folders with the host
        # user's numeric ownership. Wine itself always runs unprivileged.
        uid = os.getuid() if hasattr(os, 'getuid') else 1000
        gid = os.getgid() if hasattr(os, 'getgid') else 1000
        init = _run([self.docker, 'run', '--rm', '--platform', 'linux/amd64',
                     '--network', 'none', '--user', '0:0',
                     '--mount', f'type=volume,src={volume},dst=/cinterwine',
                     '--entrypoint', '/bin/sh', self.image, '-c',
                     f'install -d -m 700 -o {uid} -g {gid} '
                     '/cinterwine/home /cinterwine/prefix /cinterwine/home/.cache'],
                    quiet=True, timeout=90)
        if init.returncode:
            raise RuntimeError('Cannot prepare guest-native Wine directories: '
                               + init.stdout[-1000:])
        receipt = self._wine_receipt()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        old = {}
        try:
            old = json.loads(receipt.read_text('utf-8'))
        except (OSError, ValueError):
            pass
        initialized = bool(not volume_was_missing and old.get('initialized')
                           and old.get('image') == self.image
                           and old.get('volume') == volume)
        generation = (old.get('generation') if not volume_was_missing else None)
        if not isinstance(generation, str) or not generation:
            generation = uuid.uuid4().hex
        receipt.write_text(json.dumps({'schema': 'cinterforge.terminal.wine-volume.v1',
                                       'image': self.image, 'volume': volume,
                                       'builds': str(self.builds),
                                       'initialized': initialized,
                                       'generation': generation},
                                      indent=2, sort_keys=True) + '\n', encoding='utf-8')
        self._wine_volume_checked = volume
        print(f'[WINDOWS CROSS] Guest-native Wine prefix ready: {volume} '
              f'(initialized={initialized})', flush=True)

    def _require_wine_volume(self) -> str:
        """Never provision Wine state implicitly during Build or Publish."""
        volume = self._wine_volume_name()
        if self._wine_volume_checked == volume:
            return volume
        try:
            receipt = json.loads(self._wine_receipt().read_text('utf-8'))
        except (OSError, ValueError) as exc:
            raise RuntimeError('Managed Wine runtime missing; rerun ./Cinter setup win') from exc
        if (receipt.get('schema') != 'cinterforge.terminal.wine-volume.v1'
                or receipt.get('builds') != str(self.builds)
                or receipt.get('image') != self.image
                or receipt.get('volume') != volume
                or not self._wine_volume_available()):
            raise RuntimeError('Managed Wine volume unavailable or stale; '
                               'rerun ./Cinter setup win')
        self._wine_volume_checked = volume
        return volume

    def wine_volume_generation(self) -> str:
        self._require_wine_volume()
        receipt = json.loads(self._wine_receipt().read_text('utf-8'))
        generation = receipt.get('generation')
        if not isinstance(generation, str) or not generation:
            raise RuntimeError('Managed Wine volume generation missing; rerun Setup')
        return generation

    def wine_prefix_initialized(self) -> bool:
        if self.provider == 'host':
            return False
        self._require_wine_volume()
        receipt = json.loads(self._wine_receipt().read_text('utf-8'))
        return bool(receipt.get('initialized'))

    def mark_wine_prefix_initialized(self) -> None:
        if self.provider == 'host':
            return
        self._require_wine_volume()
        receipt = json.loads(self._wine_receipt().read_text('utf-8'))
        receipt['initialized'] = True
        self._wine_receipt().write_text(json.dumps(receipt, indent=2, sort_keys=True)
                                        + '\n', encoding='utf-8')

    @staticmethod
    def _uses_wine_prefix(command: list[str]) -> bool:
        names = {Path(str(part)).name.lower() for part in command}
        uses = bool(names.intersection({'wine', 'wine64', 'wineboot', 'winepath',
                                        'wineserver', 'msiexec'}))
        return uses and '--version' not in command

    def execute(self, command: list[str], *, cwd: Path, timeout: int = 600,
                graphical: bool = False, network: bool = False,
                diagnostic_log: Path | None = None) -> subprocess.CompletedProcess:
        if self.provider == 'host':
            # A host Wine run must not put its prefix, caches or runtime state
            # under ~/.wine, ~/.cache, or /tmp; keep all generated files in Builds.
            runtime = self.builds / 'Run/CinterForgeTerminal/WindowsCross'
            home = runtime / 'Home'
            prefix = runtime / 'WinePrefix'
            temp = self.builds / 'Intermediates/CinterForgeTerminal/WindowsCross/Temp'
            for directory in (home, prefix, temp, home / '.cache', home / '.config'):
                directory.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, PATH=self.native_path, HOME=str(home),
                       WINEPREFIX=str(prefix), WINEARCH='win64', WINEDEBUG='-all', TMPDIR=str(temp),
                       XDG_CACHE_HOME=str(home / '.cache'),
                       XDG_CONFIG_HOME=str(home / '.config'))
            if graphical and platform.system() == 'Linux':
                xvfb = shutil.which('xvfb-run', path=self.native_path)
                if not xvfb:
                    raise RuntimeError('Wine WebView requires xvfb-run on the Linux host')
                command = [xvfb, '-a', '--server-args=-screen 0 1280x800x24', *command]
            return _run(command, cwd=cwd, timeout=timeout, quiet=False, env=env,
                        output_log=diagnostic_log)
        if not self.docker or not self.image:
            raise RuntimeError('Windows cross environment was not successfully provisioned')
        # Native Linux filesystems are required for Wine's registry, DLL
        # cache, and drive mapping. Only non-Wine build output uses /builds.
        # Setup must have provisioned this named volume; other lifecycle
        # phases cannot create or repair it implicitly.
        uses_wine = self._uses_wine_prefix(command)
        from host.amd64_backend import docker_bind_source
        docker_command = [self.docker, 'run', '--rm', '--platform', 'linux/amd64',
                          '--network', ('bridge' if network else 'none'),
                          '--mount', f'type=bind,src={docker_bind_source(self.repository)},dst=/source,readonly',
                          '--mount', f'type=bind,src={docker_bind_source(self.builds)},dst=/builds',
                          '--workdir', self.path(cwd)]
        if uses_wine:
            volume = self._require_wine_volume()
            docker_command += [
                '--mount', f'type=volume,src={volume},dst=/cinterwine',
                '--env', 'HOME=/cinterwine/home',
                '--env', 'WINEPREFIX=/cinterwine/prefix',
                '--env', 'XDG_CACHE_HOME=/cinterwine/home/.cache',
                '--env', 'WINEARCH=win64',
                '--env', 'WINEDEBUG=-all',
            ]
        else:
            docker_command += [
                '--env', 'HOME=/builds/Run/CinterForgeTerminal/WindowsCross/Home',
            ]
        if hasattr(os, 'getuid'):
            docker_command.extend(['--user', f'{os.getuid()}:{os.getgid()}'])
        # The GUI wrapper verifies the real X11 display, x11vnc RFB handshake
        # and noVNC endpoint before executing Wine on *that same display*.
        # Do not use xvfb-run here: its fresh display would not be the one
        # which passed the Setup check.
        from host.gui_preflight import IMAGE_SCRIPT
        guest_command = (['python3', '-u', IMAGE_SCRIPT, '--platform', 'windows', '--']
                         if graphical else []) + [str(part) for part in command]
        return _run(docker_command + [self.image] + guest_command,
                    cwd=cwd, timeout=timeout, quiet=True, output_log=diagnostic_log)

    def version_probes(self, cwd: Path) -> dict[str, dict]:
        result = {}
        for name, command in TOOL_VERSIONS.items():
            execution = self.execute(command, cwd=cwd, timeout=180)
            output = execution.stdout.strip()
            if execution.returncode or not output:
                raise RuntimeError(f'{name}: cross-tool sign-of-life failed rc={execution.returncode}: {output[-600:]}')
            if name == 'wine' and output.splitlines()[0].strip() != PINNED_WINE_VERSION:
                raise RuntimeError(f'Windows Setup requires MSYS2-patched {PINNED_WINE_VERSION}, got {output[:200]!r}')
            resolved = ('docker://' + str(self.image) + '/' + command[0]
                        if self.provider != 'host' else shutil.which(command[0], path=self.native_path))
            result[name] = {'executable': resolved, 'command': command,
                            'exit_code': execution.returncode, 'output': output[:3000]}
            print(f'[TOOLCHAIN {name}] executable={resolved} rc=0 version={output.splitlines()[0]}', flush=True)
        return result
