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
import shutil
import subprocess
import time
import uuid

SCHEMA = 'cinterforge.terminal.windows-cross-environment.v1'
IMAGE_NAME = 'cinterforge-terminal-windows-cross'
PINNED_WINE_VERSION = 'wine-11.15'
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
    if not quiet:
        print('[WINDOWS CROSS] ' + ' '.join(map(str, command)), flush=True)
    if output_log is not None:
        # Persist Wine diagnostics as the child emits them. A Ctrl-C/timeout
        # must not discard the only evidence of a stalled wineboot process.
        output_log.parent.mkdir(parents=True, exist_ok=True)
        with output_log.open('w', encoding='utf-8', errors='replace') as stream:
            stream.write('$ ' + ' '.join(map(str, command)) + '\n')
            stream.flush()
            result = subprocess.run([str(c) for c in command], cwd=cwd, text=True,
                                    stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False, env=env)
        # The caller only needs a diagnostic tail, not a huge log in memory.
        with output_log.open('rb') as stream:
            stream.seek(max(0, output_log.stat().st_size - 20000))
            tail = stream.read().decode('utf-8', errors='replace')
        return subprocess.CompletedProcess(result.args, result.returncode, tail)
    return subprocess.run([str(c) for c in command], cwd=cwd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, check=False, env=env)


def _logged(command: list[str], *, cwd: Path | None = None, timeout: int = 600) -> None:
    print('[WINDOWS CROSS] ' + ' '.join(map(str, command)), flush=True)
    # Stream Docker installation/image-build output to the visible console.
    subprocess.run([str(c) for c in command], cwd=cwd, timeout=timeout, check=True)


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
        if platform.system() == 'Darwin' and bundled_cli.is_file():
            engines.append(('docker', str(bundled_cli)))
        for kind in ('docker', 'podman'):
            executable = shutil.which(kind)
            if executable:
                if not any(k == kind for k, _ in engines):
                    engines.append((kind, executable))
        failures = []
        for kind, engine in engines:
            command = ([engine, 'info', '--format', '{{.ServerVersion}}'] if kind == 'docker'
                       else [engine, 'info', '--format', '{{.Host.Arch}}'])
            health = _run(command, quiet=True, timeout=25)
            if health.returncode == 0:
                self.container_kind = kind
                print(f'[WINDOWS CROSS] {kind} engine ready: {health.stdout.strip()}', flush=True)
                return engine
            failures.append(f'{kind}: {health.stdout.strip()[-300:]}')
        if platform.system() == 'Darwin':
            from host.mac_bootstrap import ensure_docker
            engine = ensure_docker(self.builds)
            from host.mac_bootstrap import managed_environment
            os.environ.update(managed_environment(self.builds))
            health = _run([engine, 'info', '--format', '{{.ServerVersion}}'], quiet=True, timeout=25)
            if health.returncode == 0:
                self.container_kind = 'docker'
                return engine
            failures.append('managed Docker installation did not yield a running engine')
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
            command += ['--tag', image, '--file', str(DOCKERFILE), str(DOCKERFILE.parent)]
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
            return _run(command, cwd=cwd, timeout=timeout, quiet=True, env=env,
                        output_log=diagnostic_log)
        if not self.docker or not self.image:
            raise RuntimeError('Windows cross environment was not successfully provisioned')
        # Native Linux filesystems are required for Wine's registry, DLL
        # cache, and drive mapping. Only non-Wine build output uses /builds.
        # Setup must have provisioned this named volume; other lifecycle
        # phases cannot create or repair it implicitly.
        uses_wine = self._uses_wine_prefix(command)
        docker_command = [self.docker, 'run', '--rm', '--platform', 'linux/amd64',
                          '--network', ('bridge' if network else 'none'),
                          '--mount', f'type=bind,src={self.repository},dst=/source,readonly',
                          '--mount', f'type=bind,src={self.builds},dst=/builds',
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
