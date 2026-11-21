"""CinterForgeTerminal-owned versioned Asset Engine and pinned browser runtime.

Setup provisions Babylon; Build assembles immutable assets under sibling Builds;
Stage installs ONLY receipt-verified Build outputs. Converter, WebApp and Server
consume this Terminal-produced bundle, never the opposite direction.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import time
import urllib.request

from host.lifecycle.universal_package import _python_cache_member

VERSION = '2026'
SCHEMA = 'cinterforge.terminal.assets.v1'
ENGINE_SCHEMA = 'cinterforge.blockify.browser.v1'
MODULES = ('CIFBlockify.js', 'CIFBlockifyPreview.js',
           'CIFBlockifyBatch.js', 'CIFBlockifyMinecraftServer.js')
CLASS_NAMES = ('CIFBlockify', 'CIFBlockifyPreview', 'CIFBlockifyBatch',
               'CIFBlockifyMinecraftServer')
PIN_FILES = ('babylon.js', 'babylonjs.loaders.min.js')
_ORIGINAL_SOURCE = Path(__file__).resolve().parents[2] / 'webapp/data/asset_engine/original'
ORIGINAL_FILES = tuple(
    'original/' + file.relative_to(_ORIGINAL_SOURCE).as_posix()
    for file in sorted(_ORIGINAL_SOURCE.rglob('*'))
    if file.is_file() and not _python_cache_member(file.relative_to(_ORIGINAL_SOURCE))
)
BUNDLE_FILES = (
    'api/asset_gallery.py', 'api/asset_packaging.py',
    'api/asset_minecraft.py',
    'build/browser-dependencies.json',
    *(f'blockify/{name}' for name in MODULES),
    *(f'vendor/{name}' for name in PIN_FILES),
    'blockify/manifest.json',
    *ORIGINAL_FILES,
)


def source_root() -> Path:
    return Path(__file__).resolve().parents[2] / 'webapp/data/asset_engine'


def release_root(builds: Path) -> Path:
    return Path(builds) / 'Shared/CinterForgeTerminal/Assets' / VERSION


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _pins() -> dict:
    record = json.loads((source_root() / 'build/browser-dependencies.json').read_text(encoding='utf-8'))
    if (record.get('schema') != 'cinterforge.converter.browser.dependencies.v1'
            or record.get('version') != '9.26.1'
            or set(record.get('assets', {})) != set(PIN_FILES)):
        raise RuntimeError('Unsupported Terminal Babylon dependency pins')
    for name in PIN_FILES:
        entry = record['assets'][name]
        if not entry['url'].startswith('https://cdn.jsdelivr.net/npm/') or len(entry['sha256']) != 64:
            raise RuntimeError('Unapproved browser dependency: ' + name)
    return record


def asset_dependencies_ready(builds: Path) -> bool:
    """Read-only Setup-cache gate: refuse stale/missing browser dependencies."""
    pins = _pins()
    root = Path(builds) / 'Dependencies/Babylon' / pins['version']
    return all((root / name).is_file() and not (root / name).is_symlink()
               and digest(root / name) == pins['assets'][name]['sha256']
               for name in PIN_FILES)


# These refs describe immutable fallbacks for a clean Setup without sibling
# source checkouts. Existing sibling repositories are READ-ONLY inputs; their
# current clean HEADs are recorded in Terminal's own artifact receipt.
_VOXEL_INPUTS = {
    'CinterForgeVoxel': ('https://github.com/CinterForge/CinterForgeVoxel.git',
                          'ec0b2827104d0799c153bfb5f867579c0c44652f'),
    'CinterForgeServer': ('https://github.com/CinterForge/CinterForgeServer.git',
                           'd22fa1dec4f0e3c742db4fd56c2f82e6fe427110'),
    'CinterForgeWebApp': ('https://github.com/CinterForge/CinterForgeWebApp.git',
                           'ff21028675e00d14a8959b6b7c17222beb0a0770'),
}
_VOXEL_SCHEMA = 'cinterforge.terminal.voxel-browser.v1'
_VOXEL_BRIDGE = 'static/Code/IFPlayer/Authoring/blockbench/cinterforge_bridge.js'
_VOXEL_PRODUCER = 'cf/ifserver/maintenance/install/blockbench_install.py'


def voxel_root(builds: Path) -> Path:
    """Keep this product's generated browser editor out of WebApp's outputs."""
    return Path(builds) / 'CinterForgeTerminal/Dependencies/Voxel'


def _git_read(*args: str, cwd: Path) -> str:
    result = subprocess.run(['git', *args], cwd=cwd, check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


def _voxel_input_source(builds: Path, name: str) -> Path:
    """Consume a sibling repository without writing to it; clone missing pins into Builds."""
    if name not in _VOXEL_INPUTS:
        raise ValueError('Unknown Voxel source dependency: ' + name)
    sibling = Path(builds).resolve().parent / name
    if sibling.is_symlink():
        raise RuntimeError('Refusing symlinked Voxel dependency: ' + str(sibling))
    if (sibling / '.git').exists():
        return sibling
    root = voxel_root(builds) / 'Sources'
    root.mkdir(parents=True, exist_ok=True)
    local = root / name
    remote, pin = _VOXEL_INPUTS[name]
    if not local.exists():
        if not shutil.which('git'):
            raise RuntimeError('Voxel Setup requires git')
        print('[TERMINAL VOXEL SETUP] Acquiring pinned ' + name, flush=True)
        # Clone into Builds only, never into or over another product checkout.
        subprocess.run(['git', 'clone', remote, str(local)], check=True)
        subprocess.run(['git', 'checkout', '--detach', pin], cwd=local, check=True)
    if local.is_symlink() or not (local / '.git').is_dir():
        raise RuntimeError('Invalid Voxel dependency checkout: ' + str(local))
    if _git_read('rev-parse', 'HEAD', cwd=local) != pin:
        raise RuntimeError('Pinned Voxel dependency checkout changed: ' + str(local))
    return local


def _voxel_inputs(builds: Path) -> tuple[Path, Path, Path, str]:
    source = _voxel_input_source(builds, 'CinterForgeVoxel')
    server = _voxel_input_source(builds, 'CinterForgeServer')
    webapp = _voxel_input_source(builds, 'CinterForgeWebApp')
    for repo in (source, server, webapp):
        if not (repo / '.git').is_dir():
            raise RuntimeError('Voxel dependency must be a real Git checkout: ' + str(repo))
    if _git_read('status', '--porcelain', '--untracked-files=no', cwd=source):
        raise RuntimeError('CinterForgeVoxel contains uncommitted tracked changes; refusing to ignore authored work')
    commit = _git_read('rev-parse', 'HEAD', cwd=source)
    if len(commit) != 40 or not all(c in '0123456789abcdef' for c in commit):
        raise RuntimeError('CinterForgeVoxel HEAD is not a full Git commit')
    if not (server / _VOXEL_PRODUCER).is_file() or not (webapp / _VOXEL_BRIDGE).is_file():
        raise FileNotFoundError('Original Server Voxel builder or WebApp bridge is missing')
    return source, server, webapp, commit


def voxel_browser_ready(builds: Path) -> bool:
    """Read-only, content-hash verification of Terminal's own staged editor."""
    root = voxel_root(builds)
    web = root / 'Web'
    if root.is_symlink() or web.is_symlink():
        return False
    try:
        manifest = json.loads((root / 'voxel.json').read_text(encoding='utf-8'))
        if (manifest.get('schema') != _VOXEL_SCHEMA
                or manifest.get('repository') != _VOXEL_INPUTS['CinterForgeVoxel'][0]
                or len(str(manifest.get('commit') or '')) != 40):
            return False
        files = manifest.get('files') or {}
        required = ('index.html', 'dist/bundle.js',
                    'cinterforge/cinterforge_bridge.js', 'service_worker.js')
        if not isinstance(files, dict) or not set(required).issubset(files):
            return False
        actual = {p.relative_to(web).as_posix() for p in web.rglob('*') if p.is_file()}
        if actual != set(files) or web.is_symlink():
            return False
        for rel, expected in files.items():
            path = web / rel
            if (path.is_symlink() or not path.is_file() or
                    not path.resolve().is_relative_to(web.resolve()) or digest(path) != expected):
                return False
        # Detect a changed developer input even when Setup's own source files
        # and cached outputs have not changed. Installed apps have no siblings.
        workspace = Path(builds).resolve().parent
        source, server, webapp = (workspace / name for name in
                                  ('CinterForgeVoxel', 'CinterForgeServer', 'CinterForgeWebApp'))
        if (source / '.git').is_dir() and (
                _git_read('rev-parse', 'HEAD', cwd=source) != manifest['commit'] or
                _git_read('status', '--porcelain', '--untracked-files=no', cwd=source)):
            return False
        if (server / _VOXEL_PRODUCER).is_file() and digest(server / _VOXEL_PRODUCER) != manifest.get('builder_sha256'):
            return False
        if (webapp / _VOXEL_BRIDGE).is_file() and digest(webapp / _VOXEL_BRIDGE) != manifest.get('bridge_sha256'):
            return False
        return True
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        return False


def provision_voxel_browser(builds: Path) -> Path:
    """Build Voxel in Terminal-owned Builds using Server/WebApp READ-ONLY inputs.

    This uses the original Server compiler and stage helpers, rather than its
    upstream Blockbench acquisition entry point, which builds a different repo.
    No source or generated files are written into Server/WebApp/Voxel checkouts.
    """
    root = voxel_root(builds)
    if voxel_browser_ready(builds):
        print('[TERMINAL VOXEL SETUP] Reusing verified browser editor: ' + str(root / 'Web'), flush=True)
        return root / 'Web'
    source, server, webapp, commit = _voxel_inputs(builds)
    # Never run npm in the authored source repo; its generated state belongs
    # exclusively in Terminal's Builds tree.
    clone = root / 'Source'
    if clone.is_symlink():
        raise RuntimeError('Refusing symlinked Voxel Build input')
    if clone.exists() and (not (clone / '.git').is_dir() or
                           _git_read('rev-parse', 'HEAD', cwd=clone) != commit or
                           _git_read('status', '--porcelain', '--untracked-files=no', cwd=clone)):
        shutil.rmtree(clone)
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        print('[TERMINAL VOXEL SETUP] Copying Voxel source into Builds for compilation', flush=True)
        subprocess.run(['git', 'clone', '--no-hardlinks', str(source), str(clone)], check=True)
        subprocess.run(['git', 'checkout', '--detach', commit], cwd=clone, check=True)
    if _git_read('rev-parse', 'HEAD', cwd=clone) != commit:
        raise RuntimeError('Voxel Build checkout revision mismatch')
    if str(server) not in sys.path:
        sys.path.insert(0, str(server))
    from cf.ifserver.maintenance.install import blockbench_install as producer
    if Path(producer.__file__).resolve() != (server / _VOXEL_PRODUCER).resolve():
        raise RuntimeError('Voxel builder loaded from an unexpected CinterForgeServer')
    print('[TERMINAL VOXEL SETUP] Building original CinterForgeVoxel browser editor', flush=True)
    producer._build_web(clone)
    producer._stage_web(clone, root / 'Web', webapp / _VOXEL_BRIDGE)
    web = root / 'Web'
    files = {}
    for file in sorted(web.rglob('*')):
        if file.is_symlink():
            raise RuntimeError('Refusing symlink in staged CinterForgeVoxel browser output')
        if file.is_file():
            files[file.relative_to(web).as_posix()] = digest(file)
    manifest = {
        'schema': _VOXEL_SCHEMA,
        'repository': _VOXEL_INPUTS['CinterForgeVoxel'][0],
        'commit': commit,
        'builder_sha256': digest(server / _VOXEL_PRODUCER),
        'bridge_sha256': digest(webapp / _VOXEL_BRIDGE),
        'files': files,
    }
    root.mkdir(parents=True, exist_ok=True)
    destination = root / 'voxel.json'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
             prefix='voxel-', suffix='.json', dir=root, delete=False) as output:
        json.dump(manifest, output, sort_keys=True, indent=2)
        temporary = Path(output.name)
    os.replace(temporary, destination)
    if not voxel_browser_ready(builds):
        raise RuntimeError('CinterForgeVoxel browser build failed Terminal verification')
    return web


def media_toolchains_ready(builds: Path) -> bool:
    """Read-only check so a pre-asset Setup receipt cannot hide missing tools."""
    import platform
    if platform.system() == 'Darwin':
        mac = Path(builds) / 'Toolchains/mac/env/bin'
        if not all((mac / name).is_file() for name in ('ffmpeg', 'ffprobe', 'magick')):
            return False
        from host.devtools.assimp_library import assimp_ready
        if not assimp_ready(mac.parent):
            return False
    from host.windows.windows_install import REQUIRED
    root = Path(builds) / 'Toolchains/Windows/MSYS2'
    if not all((root / name).is_file() for name in REQUIRED):
        return False
    from host.windows.windows_environment import WindowsCrossEnvironment
    cross = WindowsCrossEnvironment(Path(builds))
    try:
        _, recipe_digest = cross._image_identity()
    except (RuntimeError, ValueError, OSError):
        return False
    return (cross.stage / (recipe_digest[:16] + '.tar')).is_file()


def provision_asset_dependencies(builds: Path) -> Path:
    """Setup-only: acquire the pinned browser JS, verify all bytes, reuse valid cache."""
    pins = _pins()
    target = Path(builds) / 'Dependencies/Babylon' / pins['version']
    target.mkdir(parents=True, exist_ok=True)
    for name in PIN_FILES:
        destination = target / name
        expected = pins['assets'][name]['sha256']
        if destination.is_symlink():
            raise RuntimeError('Refusing symlinked Babylon dependency: ' + str(destination))
        if destination.is_file():
            if digest(destination) != expected:
                raise RuntimeError('Modified pinned Babylon dependency: ' + str(destination))
            continue
        partial = destination.with_name(name + '.partial')
        if partial.exists() or partial.is_symlink():
            raise RuntimeError('Incomplete Babylon download requires recovery: ' + str(partial))
        print('[TERMINAL ASSETS SETUP] Downloading ' + name, flush=True)
        try:
            request = urllib.request.Request(pins['assets'][name]['url'], headers={
                'User-Agent': 'CinterForgeTerminal/2026'})
            with urllib.request.urlopen(request, timeout=60) as response, partial.open('wb') as out:
                size = 0
                checkpoint = time.monotonic()
                while part := response.read(1024 * 1024):
                    out.write(part)
                    size += len(part)
                    if time.monotonic() - checkpoint >= 5:
                        print(f'[TERMINAL ASSETS SETUP] {name}: {size / 1048576:.1f} MiB', flush=True)
                        checkpoint = time.monotonic()
            if digest(partial) != expected:
                raise RuntimeError('Pinned Babylon upstream SHA-256 mismatch: ' + name)
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    return target


def _voxel_web_files(builds: Path) -> dict[str, Path]:
    if not voxel_browser_ready(builds):
        raise FileNotFoundError('Setup must stage the CinterForgeVoxel browser build')
    source = voxel_root(builds) / 'Web'
    files = {}
    for file in sorted(source.rglob('*')):
        if file.is_symlink():
            raise RuntimeError('Unsafe symlink in Voxel browser build: ' + str(file))
        if file.is_file():
            files['voxel/' + file.relative_to(source).as_posix()] = file
    if not files:
        raise RuntimeError('Empty CinterForgeVoxel browser build')
    return files


def _check_bundle(folder: Path, *, previous: bool = False) -> dict:
    if folder.is_symlink() or not folder.is_dir():
        raise FileNotFoundError('Terminal Asset Engine Build missing: ' + str(folder))
    manifest = folder / 'manifest.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError('Terminal asset engine receipt missing')
    record = json.loads(manifest.read_text(encoding='utf-8'))
    if (record.get('schema') != SCHEMA or record.get('version') != VERSION
            or record.get('owner') != 'CinterForgeTerminal'
            or (not previous and (not set(BUNDLE_FILES).issubset(record.get('files') or {})
                                  or not any(name.startswith('voxel/') for name in record.get('files') or {})))):
        raise RuntimeError('Unsupported Terminal asset engine Build receipt')
    if {f.relative_to(folder).as_posix() for f in folder.rglob('*') if f.is_file()} != (set(record.get('files') or {}) | {'manifest.json'}):
        raise RuntimeError('Terminal asset engine has unexpected/missing files')
    if not previous:
        for needed in ('voxel/index.html', 'voxel/dist/bundle.js',
                       'voxel/cinterforge/cinterforge_bridge.js'):
            if needed not in record['files']:
                raise RuntimeError('Terminal Asset Engine is missing Voxel: ' + needed)
    for relative, expected in record['files'].items():
        file = folder / relative
        if file.is_symlink() or not file.is_file() or digest(file) != expected:
            raise RuntimeError('Terminal asset engine SHA-256 mismatch: ' + relative)
    engine = json.loads((folder / 'blockify/manifest.json').read_text(encoding='utf-8'))
    if (engine.get('schema') != ENGINE_SCHEMA or engine.get('owner') != 'CinterForgeTerminal'
            or engine.get('module_order') != list(MODULES)
            or engine.get('class_names') != list(CLASS_NAMES)
            or set(engine.get('files') or {}) != set(MODULES)):
        raise RuntimeError('Invalid Terminal Blockify manifest')
    for name in MODULES:
        entry = engine['files'][name]
        file = folder / 'blockify' / name
        if entry['sha256'] != digest(file) or entry['bytes'] != file.stat().st_size:
            raise RuntimeError('Corrupted Terminal Blockify: ' + name)
    pins = _pins()
    for name in PIN_FILES:
        if digest(folder / 'vendor' / name) != pins['assets'][name]['sha256']:
            raise RuntimeError('Corrupted Terminal Babylon runtime: ' + name)
    return record


def build_asset_engine(builds: Path) -> Path:
    """Build-only: package original unmodified algorithms, viewer and assembler."""
    src = source_root()
    target = release_root(builds)
    pins = _pins()
    voxel_files = _voxel_web_files(builds)
    browser = Path(builds) / 'Dependencies/Babylon' / pins['version']
    for name in PIN_FILES:
        file = browser / name
        if file.is_symlink() or not file.is_file() or digest(file) != pins['assets'][name]['sha256']:
            raise FileNotFoundError('Setup-provisioned pinned Babylon missing/mismatched: ' + str(file))
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cinter-terminal-assets-', dir=target.parent) as temp:
        candidate = Path(temp) / VERSION
        records = {}
        for relative in BUNDLE_FILES:
            if relative == 'blockify/manifest.json':
                continue
            source = browser / Path(relative).name if relative.startswith('vendor/') else src / relative
            if source.is_symlink() or not source.is_file():
                raise FileNotFoundError('Authored Terminal asset engine module missing: ' + str(source))
            destination = candidate / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for relative, source in voxel_files.items():
            destination = candidate / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        engine = {'schema': ENGINE_SCHEMA, 'version': VERSION,
                  'owner': 'CinterForgeTerminal', 'module_order': list(MODULES),
                  'class_names': list(CLASS_NAMES),
                  'host_requirements': ['Babylon.js and loaders', 'GLTF2Export serializer',
                                        'CIFRuntime assetCommand', 'ensureBabylon'],
                  'files': {name: {'sha256': digest(candidate / 'blockify' / name),
                                   'bytes': (candidate / 'blockify' / name).stat().st_size}
                            for name in MODULES}}
        (candidate / 'blockify/manifest.json').write_text(json.dumps(engine, sort_keys=True, indent=2) + '\n')
        members = (*BUNDLE_FILES, *voxel_files)
        records = {rel: digest(candidate / rel) for rel in members}
        (candidate / 'manifest.json').write_text(json.dumps({
            'schema': SCHEMA, 'owner': 'CinterForgeTerminal', 'version': VERSION,
            'files': records}, sort_keys=True, indent=2) + '\n')
        _check_bundle(candidate)
        if target.exists():
            _check_bundle(target, previous=True)
            if set(json.loads((target / 'manifest.json').read_text())['files']) == set(members) and {rel: digest(target / rel) for rel in members} == records:
                print('[TERMINAL ASSETS BUILD] Unchanged verified engine', flush=True)
                return target
        previous = target.with_name(VERSION + '.previous-build')
        if previous.exists():
            raise RuntimeError('Unreconciled Terminal asset engine build: ' + str(previous))
        if target.exists():
            target.rename(previous)
        try:
            candidate.rename(target)
        except Exception:
            if previous.exists() and not target.exists():
                previous.rename(target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    _check_bundle(target)
    print('[TERMINAL ASSETS BUILD] Verified engine: ' + str(target), flush=True)
    return target


def staged_terminal_assets(builds: Path) -> dict[str, Path]:
    """Stage-only: reject absent or tampered Terminal-owned Build outputs."""
    root = release_root(builds)
    record = _check_bundle(root)
    return {relative: root / relative for relative in (*record['files'], 'manifest.json')}
