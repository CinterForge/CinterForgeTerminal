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

SCHEMA = 'cinterforge.terminal.windows-cross-environment.v1'
IMAGE_NAME = 'cinterforge-terminal-windows-cross'
DOCKERFILE = Path(__file__).resolve().parent / 'docker/windows-cross.Dockerfile'
TOOL_VERSIONS = {
    'mingw-w64': ['x86_64-w64-mingw32-g++', '--version'],
    'mingw-w64-c': ['x86_64-w64-mingw32-gcc', '--version'],
    'wine': ['wine', '--version'],
    'windows-cmake': ['cmake', '--version'],
    'windows-ninja': ['ninja', '--version'],
}


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 600,
         quiet: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if not quiet:
        print('[WINDOWS CROSS] ' + ' '.join(map(str, command)), flush=True)
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
        return all(shutil.which(command, path=self.native_path) for command in required)

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
        digest = hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()
        return f'{IMAGE_NAME}:{digest[:16]}', digest

    def _has_image(self, image: str) -> bool:
        assert self.docker
        return _run([self.docker, 'image', 'inspect', image], quiet=True, timeout=30).returncode == 0

    @classmethod
    def validate_staged(cls, builds: Path) -> tuple[Path, dict]:
        """Verify the offline Docker image without consulting the Docker daemon."""
        digest = hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()
        staged = builds / 'CinterForgeTerminal/Toolchains/WindowsCross'
        archive = staged / f'{digest[:16]}.tar'
        manifest = staged / f'{digest[:16]}.json'
        if not archive.is_file() or not manifest.is_file() or archive.is_symlink():
            raise RuntimeError(f'Verified offline Windows cross image missing: {archive}')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('schema') != SCHEMA or data.get('dockerfile_sha256') != digest or
                data.get('image') != f'{IMAGE_NAME}:{digest[:16]}' or
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

    def execute(self, command: list[str], *, cwd: Path, timeout: int = 600,
                graphical: bool = False, network: bool = False) -> subprocess.CompletedProcess:
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
                       WINEPREFIX=str(prefix), WINEDEBUG='-all', TMPDIR=str(temp),
                       XDG_CACHE_HOME=str(home / '.cache'),
                       XDG_CONFIG_HOME=str(home / '.config'))
            if graphical and platform.system() == 'Linux':
                xvfb = shutil.which('xvfb-run', path=self.native_path)
                if not xvfb:
                    raise RuntimeError('Wine WebView requires xvfb-run on the Linux host')
                command = [xvfb, '-a', '--server-args=-screen 0 1280x800x24', *command]
            return _run(command, cwd=cwd, timeout=timeout, quiet=True, env=env)
        if not self.docker or not self.image:
            raise RuntimeError('Windows cross environment was not successfully provisioned')
        home = self.builds / 'Run/CinterForgeTerminal/WindowsCross/Home'
        wineprefix = self.builds / 'Run/CinterForgeTerminal/WindowsCross/WinePrefix'
        home.mkdir(parents=True, exist_ok=True)
        wineprefix.mkdir(parents=True, exist_ok=True)
        docker_command = [self.docker, 'run', '--rm', '--platform', 'linux/amd64',
                          '--network', ('bridge' if network else 'none'), '--mount', f'type=bind,src={self.repository},dst=/source,readonly',
                          '--mount', f'type=bind,src={self.builds},dst=/builds',
                          '--workdir', self.path(cwd),
                          '--env', 'HOME=/builds/Run/CinterForgeTerminal/WindowsCross/Home',
                          '--env', 'WINEPREFIX=/builds/Run/CinterForgeTerminal/WindowsCross/WinePrefix',
                          '--env', 'WINEDEBUG=-all']
        if hasattr(os, 'getuid'):
            docker_command.extend(['--user', f'{os.getuid()}:{os.getgid()}'])
        guest_command = (['xvfb-run', '-a', '--server-args=-screen 0 1280x800x24']
                         if graphical else []) + [str(part) for part in command]
        return _run(docker_command + [self.image] + guest_command,
                    cwd=cwd, timeout=timeout, quiet=True)

    def version_probes(self, cwd: Path) -> dict[str, dict]:
        result = {}
        for name, command in TOOL_VERSIONS.items():
            execution = self.execute(command, cwd=cwd, timeout=180)
            output = execution.stdout.strip()
            if execution.returncode or not output:
                raise RuntimeError(f'{name}: cross-tool sign-of-life failed rc={execution.returncode}: {output[-600:]}')
            resolved = ('docker://' + str(self.image) + '/' + command[0]
                        if self.provider != 'host' else shutil.which(command[0], path=self.native_path))
            result[name] = {'executable': resolved, 'command': command,
                            'exit_code': execution.returncode, 'output': output[:3000]}
            print(f'[TOOLCHAIN {name}] executable={resolved} rc=0 version={output.splitlines()[0]}', flush=True)
        return result
