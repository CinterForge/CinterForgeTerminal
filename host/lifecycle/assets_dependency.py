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
import tempfile
import time
import urllib.request

VERSION = '2026'
SCHEMA = 'cinterforge.terminal.assets.v1'
ENGINE_SCHEMA = 'cinterforge.blockify.browser.v1'
MODULES = ('CIFBlockify.js', 'CIFBlockifyPreview.js',
           'CIFBlockifyBatch.js', 'CIFBlockifyMinecraftServer.js')
CLASS_NAMES = ('CIFBlockify', 'CIFBlockifyPreview', 'CIFBlockifyBatch',
               'CIFBlockifyMinecraftServer')
PIN_FILES = ('babylon.js', 'babylonjs.loaders.min.js')
BUNDLE_FILES = (
    'api/asset_gallery.py', 'api/asset_packaging.py',
    'api/asset_minecraft.py',
    'build/browser-dependencies.json',
    *(f'blockify/{name}' for name in MODULES),
    *(f'vendor/{name}' for name in PIN_FILES),
    'blockify/manifest.json',
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


def _check_bundle(folder: Path) -> dict:
    if folder.is_symlink() or not folder.is_dir():
        raise FileNotFoundError('Terminal Asset Engine Build missing: ' + str(folder))
    manifest = folder / 'manifest.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError('Terminal asset engine receipt missing')
    record = json.loads(manifest.read_text(encoding='utf-8'))
    if (record.get('schema') != SCHEMA or record.get('version') != VERSION
            or record.get('owner') != 'CinterForgeTerminal'
            or set(record.get('files') or {}) != set(BUNDLE_FILES)):
        raise RuntimeError('Unsupported Terminal asset engine Build receipt')
    if {f.relative_to(folder).as_posix() for f in folder.rglob('*') if f.is_file()} != (set(BUNDLE_FILES) | {'manifest.json'}):
        raise RuntimeError('Terminal asset engine has unexpected/missing files')
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
        engine = {'schema': ENGINE_SCHEMA, 'version': VERSION,
                  'owner': 'CinterForgeTerminal', 'module_order': list(MODULES),
                  'class_names': list(CLASS_NAMES),
                  'host_requirements': ['Babylon.js and loaders', 'GLTF2Export serializer',
                                        'CIFRuntime assetCommand', 'ensureBabylon'],
                  'files': {name: {'sha256': digest(candidate / 'blockify' / name),
                                   'bytes': (candidate / 'blockify' / name).stat().st_size}
                            for name in MODULES}}
        (candidate / 'blockify/manifest.json').write_text(json.dumps(engine, sort_keys=True, indent=2) + '\n')
        records = {rel: digest(candidate / rel) for rel in BUNDLE_FILES}
        (candidate / 'manifest.json').write_text(json.dumps({
            'schema': SCHEMA, 'owner': 'CinterForgeTerminal', 'version': VERSION,
            'files': records}, sort_keys=True, indent=2) + '\n')
        _check_bundle(candidate)
        if target.exists():
            _check_bundle(target)
            if {rel: digest(target / rel) for rel in BUNDLE_FILES} == records:
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
    _check_bundle(root)
    return {relative: root / relative for relative in (*BUNDLE_FILES, 'manifest.json')}
