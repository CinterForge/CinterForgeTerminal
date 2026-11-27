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
from host.lifecycle.craftmaster_dependency import dependency_remote, dependency_source, _local_source_fingerprint

VERSION = '2026'
SCHEMA = 'cinterforge.terminal.assets.v1'
ENGINE_SCHEMA = 'cinterforge.blockify.browser.v1'
MODULES = ('CIFBlockify.js', 'CIFBlockifyPreview.js',
           'CIFBlockifyBatch.js', 'CIFBlockifyMinecraftServer.js')
CLASS_NAMES = ('CIFBlockify', 'CIFBlockifyPreview', 'CIFBlockifyBatch',
               'CIFBlockifyMinecraftServer')
PIN_FILES = ('babylon.js', 'babylonjs.loaders.min.js')
# Retain the public asset Build receipt layout. Its authored sources are now in
# webapp/runtime/assets, webapp/client/minecraft and webapp/data, never the
# removed data/asset_engine/original checkout.
BUNDLE_FILES = (
    'api/asset_gallery.py', 'api/asset_packaging.py',
    'api/asset_minecraft.py', 'build/browser-dependencies.json',
    *(f'blockify/{name}' for name in MODULES),
    *(f'vendor/{name}' for name in PIN_FILES),
    'blockify/manifest.json',
)
_SOURCE_LOCATIONS = {
    **{f'api/{name}': f'runtime/assets/{name}' for name in
       ('asset_gallery.py', 'asset_packaging.py', 'asset_minecraft.py')},
    'build/browser-dependencies.json': 'data/browser-dependencies.json',
    **{f'blockify/{name}': f'client/minecraft/{name}' for name in MODULES},
}


def source_root() -> Path:
    return Path(__file__).resolve().parents[2] / 'webapp'


def release_root(builds: Path) -> Path:
    return Path(builds) / 'Shared/CinterForgeTerminal/Assets' / VERSION


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _pins() -> dict:
    record = json.loads((source_root() / 'data/browser-dependencies.json').read_text(encoding='utf-8'))
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


_VOXEL_SCHEMA = 'cinterforge.terminal.voxel-browser.v2'
_VOXEL_BRIDGE = 'static/Code/IFPlayer/Authoring/blockbench/cinterforge_bridge.js'
_VOXEL_PRODUCER = 'cf/ifserver/maintenance/install/blockbench_install.py'


def voxel_root(builds: Path) -> Path:
    """Keep this product's generated browser editor out of WebApp's outputs."""
    return Path(builds) / 'CinterForgeTerminal/Dependencies/Voxel'


def _git_read(*args: str, cwd: Path) -> str:
    result = subprocess.run(['git', *args], cwd=cwd, check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


def _voxel_input_source(builds: Path, name: str, *, acquire: bool = True) -> Path:
    """Use existing local source only until the owner browser product is published."""
    if name in ('CinterForgeServer', 'CinterForgeVoxel'):
        return dependency_source(builds, name)
    if name != 'CinterForgeWebApp':
        raise ValueError('Unknown Voxel source dependency: ' + name)
    sibling = Path(builds).resolve().parent / name
    if sibling.is_symlink() or not (sibling / _VOXEL_BRIDGE).is_file():
        raise RuntimeError('Missing local WebApp bridge; owner publication required. Terminal will not clone source.')
    return sibling


def _voxel_inputs(builds: Path, *, acquire: bool = True) -> tuple[Path, Path, Path, str]:
    source = _voxel_input_source(builds, 'CinterForgeVoxel')
    server = _voxel_input_source(builds, 'CinterForgeServer')
    webapp = _voxel_input_source(builds, 'CinterForgeWebApp', acquire=acquire)
    for repo in (source, server, webapp):
        if not (repo / '.git').is_dir():
            raise RuntimeError('Voxel dependency must be a real Git checkout: ' + str(repo))
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
                or manifest.get('repository') != dependency_remote('CinterForgeVoxel')
                or len(str(manifest.get('commit') or '')) != 40):
            return False
        # Compare the actual selected checkout, including uncommitted edits and
        # managed branch updates. This is a metadata scan, not a trial build.
        source, server, webapp, commit = _voxel_inputs(builds, acquire=False)
        if (commit != manifest['commit'] or
                _local_source_fingerprint(source) != manifest.get('source_fingerprint') or
                digest(server / _VOXEL_PRODUCER) != manifest.get('builder_sha256') or
                digest(webapp / _VOXEL_BRIDGE) != manifest.get('bridge_sha256')):
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
        return True
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError):
        return False


def _copy_dependency_snapshot(source: Path, clone: Path, commit: str) -> None:
    """Build from the actual worktree, not just its last committed tree.

    Only the disposable Builds copy is modified. Git-ignored output and
    node_modules are not copied. Reject submodule worktrees or unsafe links
    instead of silently compiling a different source revision.
    """
    import stat
    before = _local_source_fingerprint(source)
    if clone.is_symlink():
        raise RuntimeError("Refusing symlinked dependency Build input")
    if clone.exists():
        shutil.rmtree(clone)
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(clone)], check=True)
    # Establish HEAD/index for version-aware build tools without checking out a
    # second copy of every file or reintroducing a deleted committed member.
    subprocess.run(["git", "update-ref", "--no-deref", "HEAD", commit], cwd=clone, check=True)
    subprocess.run(["git", "read-tree", commit], cwd=clone, check=True)

    def paths(root, *args):
        raw = subprocess.check_output(["git", "--no-optional-locks", "-C", str(root), "ls-files", "-z", *args])
        return {os.fsdecode(name) for name in raw.split(b"\0") if name}

    actual = paths(source, "--cached", "--others", "--exclude-standard")
    for name in sorted(actual):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Unsafe dependency source path: " + name)
        src, dst = source / relative, clone / relative
        if ".git" in relative.parts or not dst.parent.resolve().is_relative_to(clone.resolve()):
            raise RuntimeError("Unsafe dependency build path: " + name)
        if not (src.exists() or src.is_symlink()):
            # A locally deleted tracked file stays deleted.
            continue
        if not src.parent.resolve().is_relative_to(source.resolve()):
            raise RuntimeError("Unsafe dependency source path: " + name)
        mode = src.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise RuntimeError("Unsupported dependency source member: " + name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            raise RuntimeError("Conflicting dependency source paths: " + name)
        if stat.S_ISLNK(mode):
            target = os.readlink(src)
            if Path(target).is_absolute() or not src.resolve().is_relative_to(source.resolve()):
                raise RuntimeError("Unsafe dependency source link: " + name)
            dst.symlink_to(target)
        else:
            shutil.copy2(src, dst)
    if _local_source_fingerprint(source) != before:
        raise RuntimeError("dependency source changed during snapshot; rerun Setup")


def _voxel_producer(server: Path):
    if str(server) not in sys.path:
        sys.path.insert(0, str(server))
    from cf.ifserver.maintenance.install import blockbench_install as producer
    if Path(producer.__file__).resolve() != (server / _VOXEL_PRODUCER).resolve():
        raise RuntimeError('Voxel builder loaded from an unexpected CinterForgeServer')
    return producer


def _voxel_setup_inputs(builds: Path, source: Path, server: Path) -> dict:
    """Bind Setup to actual npm manifests and host tools, not source edits."""
    import platform
    manifests = {}
    raw = subprocess.check_output(['git', '--no-optional-locks', '-C', str(source),
                                   'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
    for name in sorted(set(os.fsdecode(part) for part in raw.split(b'\0') if part)):
        relative = Path(name)
        if relative.name not in ('package.json', 'package-lock.json', 'npm-shrinkwrap.json', '.npmrc'):
            continue
        path = source / relative
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise RuntimeError('Unsafe Voxel npm manifest: ' + name)
        manifests[name] = digest(path) if path.is_file() else None
    if not all(manifests.get(name) for name in ('package.json', 'package-lock.json')):
        raise RuntimeError('Voxel npm manifests missing or untracked')
    tools = {}
    for name in ('node', 'npm'):
        # Setup's dirty gate runs before managed PATH is activated. Use the
        # existing shared native DevTools installation on the Mac producer.
        executable = (builds / 'Toolchains/mac/env/bin' / name if platform.system() == 'Darwin'
                      else Path(shutil.which(name) or ''))
        if not executable.is_file():
            raise RuntimeError('Voxel Setup requires provisioned ' + name)
        meta = executable.stat()
        tools[name] = [str(executable.resolve()), meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns]
    return {'source': str(source.resolve()), 'builder': str((server / _VOXEL_PRODUCER).resolve()),
            'builder_sha256': digest(server / _VOXEL_PRODUCER), 'manifests': manifests,
            'host': [platform.system(), platform.machine()], 'tools': tools}


def _voxel_setup_receipt(builds: Path, source: Path, server: Path) -> dict:
    root = voxel_root(builds)
    receipt = root / 'voxel-setup.json'
    cache = root / 'npm-cache'
    if (root.is_symlink() or receipt.is_symlink() or cache.is_symlink() or
            not receipt.is_file() or not cache.is_dir() or not (cache / '_cacache').is_dir()):
        raise RuntimeError('Voxel npm acquisition missing; run Terminal Setup')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if data.get('schema') != 'cinterforge.terminal.voxel-setup.v1' or data.get('inputs') != _voxel_setup_inputs(builds, source, server):
        raise RuntimeError('Voxel npm inputs changed; run Terminal Setup')
    return data


def voxel_setup_ready(builds: Path) -> bool:
    try:
        source, server, _, _ = _voxel_inputs(builds, acquire=False)
        _voxel_setup_receipt(builds, source, server)
        return True
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        return False


def _write_voxel_receipt(destination: Path, manifest: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
             prefix='voxel-', suffix='.json', dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        json.dump(manifest, output, sort_keys=True, indent=2)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def provision_voxel_browser(builds: Path) -> Path:
    """Setup only: acquire the selected Voxel source's npm closure, not a browser build."""
    root = voxel_root(builds)
    if root.is_symlink() or not root.resolve().is_relative_to(builds.resolve()):
        raise RuntimeError('Unsafe Voxel build root: ' + str(root))
    source, server, _, commit = _voxel_inputs(builds)
    if voxel_setup_ready(builds):
        print('[TERMINAL VOXEL SETUP] Reusing acquired npm inputs', flush=True)
        return root / 'Source'
    inputs = _voxel_setup_inputs(builds, source, server)
    fingerprint = _local_source_fingerprint(source)
    clone, cache = root / 'Source', root / 'npm-cache'
    receipt = root / 'voxel-setup.json'
    receipt.unlink(missing_ok=True)
    if cache.is_symlink():
        raise RuntimeError('Unsafe Voxel npm cache: ' + str(cache))
    cache.mkdir(parents=True, exist_ok=True)
    _copy_dependency_snapshot(source, clone, commit)
    producer = _voxel_producer(server)
    print('[TERMINAL VOXEL SETUP] Acquiring lockfile dependencies', flush=True)
    producer._install_web_dependencies(clone, native_cache=cache)
    if (_local_source_fingerprint(source) != fingerprint or
            _voxel_setup_inputs(builds, source, server) != inputs):
        raise RuntimeError('Voxel source changed during Setup; rerun Setup')
    # The actual maintained Voxel has npm dependencies; its acquisition cache
    # must exist for a later offline dirty Build.
    if not (cache / '_cacache').is_dir():
        raise RuntimeError('Voxel npm acquisition did not produce an offline cache')
    _write_voxel_receipt(receipt, {'schema': 'cinterforge.terminal.voxel-setup.v1',
                                 'inputs': inputs, 'source_fingerprint': fingerprint})
    return clone


def build_voxel_browser(builds: Path) -> Path:
    """Build only: reuse an unchanged verified editor; otherwise compile actual source.

    The original Server compiler and WebApp bridge remain the transitional
    producer. No Git network operation or online npm resolution occurs here.
    """
    root = voxel_root(builds)
    if root.is_symlink() or not root.resolve().is_relative_to(builds.resolve()):
        raise RuntimeError('Unsafe Voxel build root: ' + str(root))
    if voxel_browser_ready(builds):
        print('[TERMINAL VOXEL BUILD] Unchanged source and verified output; skipping build', flush=True)
        return root / 'Web'
    source, server, webapp, commit = _voxel_inputs(builds, acquire=False)
    setup = _voxel_setup_receipt(builds, source, server)
    source_fingerprint = _local_source_fingerprint(source)
    bridge_sha256 = digest(webapp / _VOXEL_BRIDGE)
    clone, cache = root / 'Source', root / 'npm-cache'
    producer = _voxel_producer(server)
    # Re-create only the disposable compilation tree on a dirty build. npm ci
    # consumes Setup's exact lockfile cache with offline resolution; no fetch.
    print('[TERMINAL VOXEL BUILD] Compiling current Voxel worktree', flush=True)
    _copy_dependency_snapshot(source, clone, commit)
    try:
        producer._install_web_dependencies(clone, native_cache=cache, offline=True)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # An incomplete offline cache must not keep the next Setup on its
        # fast path. Preserve the cache itself for repair, invalidate receipt.
        (root / 'voxel-setup.json').unlink(missing_ok=True)
        raise RuntimeError('Voxel offline dependency install failed; rerun Terminal Setup') from exc
    producer._compile_web(clone, offline=True, native_cache=cache)
    if (_local_source_fingerprint(source) != source_fingerprint or
            _voxel_setup_inputs(builds, source, server) != setup['inputs'] or
            digest(webapp / _VOXEL_BRIDGE) != bridge_sha256):
        raise RuntimeError('Voxel source changed during compilation; rerun Build')
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
        'repository': dependency_remote('CinterForgeVoxel'),
        'commit': commit,
        'source_fingerprint': source_fingerprint,
        'builder_sha256': digest(server / _VOXEL_PRODUCER),
        'bridge_sha256': bridge_sha256,
        'files': files,
    }
    _write_voxel_receipt(root / 'voxel.json', manifest)
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
        raise FileNotFoundError('Build must produce the verified CinterForgeVoxel browser output')
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
    build_voxel_browser(builds)
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
            source = browser / Path(relative).name if relative.startswith('vendor/') else src / _SOURCE_LOCATIONS[relative]
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
