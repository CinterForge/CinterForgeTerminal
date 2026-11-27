"""Terminal dependency selection and verified CraftMaster Stage integration.

Only an existing developer sibling may be built. Otherwise Setup restores the
owner's published native Stage into the same shared Builds layout. Build never
clones, downloads or rebuilds an absent CraftMaster repository.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from zipfile import ZipFile

TARGETS = ('mac', 'linux', 'win')
WINDOWS_RUNTIME_SCHEMA = 'cinterforge.terminal.craftmaster.windows-runtime.v1'
SOURCES = {
    'cinterforge-craftmaster': ('Cinter', 'CMakeLists.txt', 'lifecycle/driver.py', 'data/MCTemplate.mcworld'),
    'cinterforge-leveldb': ('CMakeLists.txt', 'include/leveldb/db.h'),
}


# Existing local-source validation is shared by Terminal's maintained dependencies.
# URLs agree with WebApp's existing web_setup.py source registry.
TERMINAL_SOURCES = {
    **SOURCES,
    'CinterForgeServer': ('Cinter', 'cf/io/CinterIO.py', 'requirements.txt'),
    'CinterForgeCode': ('package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.nvmrc'),
    'CinterForgeVoxel': ('package.json', 'package-lock.json'),
}


def dependency_remote(name: str) -> str:
    if name not in TERMINAL_SOURCES:
        raise ValueError('Unknown Terminal source dependency: ' + name)
    return 'https://github.com/CinterForge/' + name + '.git'


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _git_output(target: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(target), *args],
                                   text=True, stderr=subprocess.STDOUT).strip()


def _dependency_root(builds: Path, name: str) -> tuple[Path, bool]:
    """Only an actual sibling checkout is local source; ignore managed clones."""
    if name not in TERMINAL_SOURCES:
        raise ValueError('Unknown Terminal source dependency: ' + name)
    repo = Path(__file__).resolve().parents[2]
    configured = os.environ.get('CINTER_LEVELDB_SOURCE', '').strip() if name == 'cinterforge-leveldb' else ''
    sibling = Path(configured).expanduser().resolve() if configured else repo.parent / name
    if sibling.exists() or sibling.is_symlink():
        if (sibling.is_symlink() or not sibling.is_dir() or
                any(not (sibling / relative).is_file() for relative in TERMINAL_SOURCES[name])):
            raise RuntimeError('Incomplete or linked sibling dependency checkout: ' + str(sibling))
        return sibling, True
    return sibling, False


def _local_craftmaster(builds: Path) -> bool:
    return _dependency_root(builds, 'cinterforge-craftmaster')[1]


def _craftmaster_channel() -> str:
    channel = os.environ.get('CINTER_CRAFTMASTER_DISTRIBUTION_CHANNEL', 'beta')
    if channel not in ('beta', 'release'):
        raise ValueError('CraftMaster dependency channel must be beta or release')
    return channel


def _publication_receipt(builds: Path, target: str) -> Path:
    if target not in TARGETS:
        raise ValueError('Unknown CraftMaster target: ' + target)
    return builds / 'CinterForgeCraftMaster/Setup/Published' / (target + '.json')


def _publication_entry(doc: dict, target: str, channel: str) -> dict:
    if (not isinstance(doc, dict) or doc.get('schema') != 'cinterforge.craftmaster.distribution.v1'
            or doc.get('channel') != channel or not isinstance(doc.get('artifacts'), dict)):
        raise RuntimeError('Invalid CraftMaster published distribution manifest')
    entry = doc['artifacts'].get(target)
    if not isinstance(entry, dict):
        raise RuntimeError('CraftMaster has no published ' + target + ' artifact in ' + channel)
    digest = entry.get('sha256')
    version = entry.get('version')
    if (entry.get('filename') != 'CinterForgeCraftMaster-' + target + '.zip'
            or entry.get('platform') != target or entry.get('converter_linked') is not True
            or not isinstance(version, str) or not version or version != doc.get('version')
            or type(entry.get('bytes')) is not int or not 0 < entry['bytes'] <= 8 * 1024**3
            or not isinstance(digest, str) or len(digest) != 64
            or any(ch not in '0123456789abcdef' for ch in digest)):
        raise RuntimeError('Invalid CraftMaster published artifact metadata for ' + target)
    return entry


def _published_stage(builds: Path, target: str) -> Path:
    """Read-only: a local Stage is not a published dependency without its receipt."""
    receipt = _publication_receipt(builds, target)
    if (receipt.is_symlink() or not receipt.is_file() or
            receipt.resolve() != receipt.absolute()):
        raise RuntimeError('Missing verified published CraftMaster ' + target + '; run Terminal Setup')
    doc = json.loads(receipt.read_text('utf-8'))
    channel = _craftmaster_channel()
    if doc.get('schema') != 'cinterforge.terminal.craftmaster.publication.v1':
        raise RuntimeError('Invalid CraftMaster publication receipt')
    entry = _publication_entry(doc.get('distribution'), target, channel)
    stage = staged_craftmaster(builds, target, candidate=builds / 'CinterForgeCraftMaster/Stage' / target)
    manifest = stage / 'manifest.json'
    if (_digest(manifest) != doc.get('stage_sha256') or
            json.loads(manifest.read_text('utf-8')).get('version') != entry['version']):
        raise RuntimeError('Published CraftMaster Stage changed; rerun Setup for ' + target)
    return stage


# Scope of the current Setup invocation only; no new persistent state.
_published_setup_targets: tuple[str, ...] | None = None


def _published_data(builds: Path) -> Path:
    # All target Stages contain the owner-authored Python data modules. Scoped
    # Setup must use a target from the manifest it just acquired, never an old
    # foreign Stage left over from an earlier release.
    native = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}.get(platform.system())
    targets = _published_setup_targets or TARGETS
    order = tuple(dict.fromkeys((native, *targets))) if native in targets else targets
    problems = []
    for target in order:
        if _publication_receipt(builds, target).is_file():
            try:
                return _published_stage(builds, target) / 'data'
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                problems.append(str(exc))
    raise RuntimeError('No verified published CraftMaster data available; run Terminal Setup. ' + '; '.join(problems))


def _extract_published_stage(archive: Path, candidate: Path) -> None:
    """Extract the existing owner ZIP layout, rejecting aliases and special files."""
    with ZipFile(archive) as package:
        seen = set()
        total = 0
        for info in package.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            mode = info.external_attr >> 16
            if (not name or '\\' in name or ':' in name or name.startswith('/')
                    or len(parts) < 2 or parts[0] != 'CinterForgeCraftMaster'
                    or any(part in ('', '.', '..') or part.endswith((' ', '.'))
                           or any(ord(ch) < 32 for ch in part) for part in name.split('/'))
                    or (parts[1] not in ('bin', 'data') and parts[1:] != ('manifest.json',))
                    or info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise RuntimeError('Unsafe CraftMaster archive member: ' + name)
            relative = PurePosixPath(*parts[1:]).as_posix()
            if relative.casefold() in seen:
                raise RuntimeError('Duplicate CraftMaster archive member: ' + name)
            seen.add(relative.casefold())
            total += info.file_size
            if total > 16 * 1024**3:
                raise RuntimeError('Oversized CraftMaster archive extraction')
            output = candidate.joinpath(*parts[1:])
            output.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as src, output.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            output.chmod(0o755 if mode & 0o111 else 0o644)


def acquire_published_craftmaster(builds: Path, targets: tuple[str, ...]) -> None:
    """Setup-only acquisition. Verify remote metadata, bytes and exact Stage."""
    global _published_setup_targets
    if _local_craftmaster(builds):
        raise RuntimeError('Published CraftMaster acquisition must not replace a local source build')
    if not targets or len(set(targets)) != len(targets) or any(target not in TARGETS for target in targets):
        raise ValueError('Invalid CraftMaster published target selection')
    channel = _craftmaster_channel()
    base = 'https://cinterforge.ucreg.io/CinterForgeCraftMaster/Distribution' + ('/beta' if channel == 'beta' else '')
    def request(url):
        return urllib.request.Request(url, headers={'User-Agent': 'CinterForgeTerminal', 'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(request(base + '/manifest.json'), timeout=90) as response:
            raw = response.read(1024 * 1024 + 1)
    except OSError as exc:
        raise RuntimeError('CraftMaster owner publication unavailable: ' + base + '/manifest.json. '
                           'Publish the accepted owner builds; no source fallback.') from exc
    if len(raw) > 1024 * 1024:
        raise RuntimeError('Oversized CraftMaster distribution manifest')
    doc = json.loads(raw)
    # Validate every requested target before replacing any existing output.
    entries = {target: _publication_entry(doc, target, channel) for target in targets}
    for target, entry in entries.items():
        receipt = _publication_receipt(builds, target)
        try:
            _published_stage(builds, target)
            previous = json.loads(receipt.read_text('utf-8'))
            if _publication_entry(previous['distribution'], target, channel) == entry:
                print('[CRAFTMASTER SETUP] Reusing verified published ' + target, flush=True)
                continue
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            pass
        stage = builds / 'CinterForgeCraftMaster/Stage' / target
        if any(path.is_symlink() for path in (builds, stage, stage.parent, stage.parent.parent, receipt, receipt.parent, receipt.parent.parent)):
            raise RuntimeError('Unsafe CraftMaster output or publication receipt')
        stage.parent.mkdir(parents=True, exist_ok=True)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='Published-', dir=stage.parent) as temp:
            scratch = Path(temp)
            archive = scratch / entry['filename']
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(request(base + '/' + entry['filename']), timeout=90) as response, archive.open('xb') as out:
                for block in iter(lambda: response.read(1024 * 1024), b''):
                    size += len(block)
                    if size > entry['bytes']:
                        raise RuntimeError('CraftMaster download exceeds declared bytes')
                    digest.update(block)
                    out.write(block)
            if size != entry['bytes'] or digest.hexdigest() != entry['sha256']:
                raise RuntimeError('CraftMaster published archive checksum mismatch: ' + target)
            candidate = scratch / 'Stage'
            candidate.mkdir()
            _extract_published_stage(archive, candidate)
            staged_craftmaster(builds, target, candidate=candidate)
            manifest = candidate / 'manifest.json'
            if json.loads(manifest.read_text('utf-8')).get('version') != entry['version']:
                raise RuntimeError('Published CraftMaster Stage version mismatch')
            record = {'schema': 'cinterforge.terminal.craftmaster.publication.v1',
                      'distribution': doc, 'stage_sha256': _digest(manifest)}
            pending = scratch / 'publication.json'
            pending.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            # Keep the previously validated output until the complete candidate
            # is verified. Roll back on an interrupted filesystem replacement.
            retired = scratch / 'Retired'
            if stage.exists():
                stage.rename(retired)
            try:
                candidate.rename(stage)
                pending.replace(receipt)
            except BaseException:
                if stage.exists():
                    shutil.rmtree(stage)
                if retired.exists():
                    retired.rename(stage)
                raise
        _published_stage(builds, target)
        print('[CRAFTMASTER SETUP] Installed verified published ' + target + ': ' + str(stage), flush=True)
    _published_setup_targets = targets


def _local_source_fingerprint(source: Path) -> str:
    """Read file metadata, not file contents or a trial build.

    Include tracked and nonignored untracked files, including deleted tracked
    paths. Nanosecond mtime/ctime, size, mode and path catch ordinary local
    edits, additions, deletions and renames, even without a Git commit.
    """
    digest = hashlib.sha256()
    digest.update(str(source.resolve()).encode())
    if (source / '.git').exists():
        digest.update(_git_output(source, 'rev-parse', 'HEAD').encode())
        raw = subprocess.check_output(['git', '--no-optional-locks', '-C', str(source),
                                       'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
        names = sorted(set(os.fsdecode(name) for name in raw.split(b'\0') if name))
    else:
        # Source archives are also valid local development inputs. Do not
        # follow symlinked directories or include interpreter/editor caches.
        names = []
        for folder, directories, files in os.walk(source, followlinks=False):
            directories[:] = sorted(d for d in directories if d not in
                                     {'.git', 'Builds', '__pycache__', '.pytest_cache', '.mypy_cache'})
            for name in files + [d for d in directories if (Path(folder) / d).is_symlink()]:
                if name != '.DS_Store' and not name.endswith(('.pyc', '.pyo')):
                    names.append((Path(folder) / name).relative_to(source).as_posix())
        names.sort()
    for name in names:
        path = source / name
        digest.update(os.fsencode(name) + b'\0')
        try:
            meta = path.lstat()
        except FileNotFoundError:
            digest.update(b'missing\0')
            continue
        digest.update(f'{meta.st_mode}:{meta.st_size}:{meta.st_mtime_ns}:{meta.st_ctime_ns}'.encode() + b'\0')
        if stat.S_ISLNK(meta.st_mode):
            digest.update(os.fsencode(os.readlink(path)) + b'\0')
    return digest.hexdigest()


def craftmaster_source_identity(builds: Path, *, setup: bool = False) -> dict[str, str]:
    if not _local_craftmaster(builds):
        identities = {'cinterforge-craftmaster': 'published:' + _craftmaster_channel()}
        for target in TARGETS:
            receipt = _publication_receipt(builds, target)
            if receipt.is_file() and not receipt.is_symlink():
                identities['publication:' + target] = _digest(receipt)
        return identities
    identities = {}
    for name in SOURCES:
        target = dependency_source(builds, name)
        identities[name] = ('local:' + str(target.resolve()) if setup else
                            'local:' + _local_source_fingerprint(target))
        if setup and name == 'cinterforge-craftmaster':
            for relative in ('data/CinterForgeBedrockAccess.py', 'data/CinterForgeLifecycleBedrock.py', 'data/CinterForgeJava.py'):
                identities[name] += ':' + _digest(target / relative)
    return identities


def _ensure_dependency_sources(builds: Path, names) -> dict[str, Path]:
    """Existing local source only. The consumer never acquires another checkout."""
    repo = Path(__file__).resolve().parents[2]
    if builds.resolve() != (repo.parent / 'Builds').resolve():
        raise RuntimeError('Dependencies require the same shared Builds as Terminal')
    paths = {name: dependency_source(builds, name) for name in names}
    for target in paths.values():
        print('[TERMINAL SOURCE] Using local sibling: ' + str(target), flush=True)
    return paths


def dependency_source(builds: Path, name: str) -> Path:
    target, local = _dependency_root(builds, name)
    if not local:
        raise RuntimeError(name + ' has no local sibling checkout; published product integration is required. '
                           'Terminal will not clone or build missing source.')
    return target


def craftmaster_bedrock_module(builds: Path, name: str):
    """Load the acquired owner source for host lifecycle operations only.

    Installed Terminal uses verified CraftMaster Stage data instead. A distinct
    package name avoids importing an unrelated top-level ``data`` package.
    """
    import importlib
    import sys
    import types
    if name not in ('CinterForgeBedrockAccess', 'CinterForgeLifecycleBedrock', 'CinterForgeJava'):
        raise ValueError('Unknown CraftMaster Minecraft module: ' + name)
    source = (dependency_source(builds, 'cinterforge-craftmaster') / 'data'
              if _local_craftmaster(builds) else _published_data(builds))
    package_name = '_cinterforge_craftmaster_bedrock'
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(source.resolve())]
        sys.modules[package_name] = package
    elif package.__path__ != [str(source.resolve())]:
        raise RuntimeError('CraftMaster source changed within the lifecycle process')
    return importlib.import_module(package_name + '.' + name)


def _prepare_local_leveldb(builds: Path) -> None:
    """Delegate this source-library acquisition to the local CraftMaster owner.

    Guest Setup mounts the verified checkout read-only; it cannot clone it.
    """
    source = dependency_source(builds, 'cinterforge-craftmaster')
    subprocess.run([sys.executable, '-B', '-c',
                    'from lifecycle.driver import leveldb_source; leveldb_source(acquire=True)'],
                   cwd=source, env=_host_environment(builds), check=True)


def ensure_craftmaster_sources(builds: Path) -> tuple[Path, Path]:
    _prepare_local_leveldb(builds)
    paths = _ensure_dependency_sources(builds, SOURCES)
    return tuple(paths[name] for name in SOURCES)


def ensure_terminal_sources(builds: Path, targets: tuple[str, ...] = TARGETS) -> dict[str, Path]:
    """Setup: resolve CraftMaster before any host lifecycle consumes its data.

    Other product handoffs still require local siblings until their owner
    publication contracts are implemented. Never revive managed source clones.
    """
    if _local_craftmaster(builds):
        _prepare_local_leveldb(builds)
        names = TERMINAL_SOURCES
    else:
        acquire_published_craftmaster(builds, targets)
        names = (name for name in TERMINAL_SOURCES if name not in SOURCES)
    return _ensure_dependency_sources(builds, names)


def _host_user_home() -> Path:
    """The actual login home, not the synthetic Colima/Lima toolchain HOME."""
    if os.name == 'posix':
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    return Path.home()


def _host_saves_root(builds: Path) -> Path:
    override = os.environ.get('CINTER_CRAFTMASTER_SAVES_ROOT')
    home = _host_user_home()
    if override:
        saves = Path(override).expanduser()
    elif platform.system() == 'Darwin':
        saves = home / 'Library/Application Support/minecraft/saves'
    elif platform.system() == 'Windows':
        saves = Path(os.environ.get('APPDATA') or str(home / 'AppData/Roaming')) / '.minecraft/saves'
    else:
        saves = home / '.minecraft/saves'
    saves = saves.resolve()
    if saves == builds.resolve() or builds.resolve() in saves.parents:
        raise RuntimeError('CraftMaster saves must not be inside Builds: ' + str(saves))
    return saves


def _host_environment(builds: Path) -> dict[str, str]:
    # Preserve managed tools and Docker selectors. Override only CraftMaster's
    # explicit data location (and default credential lookup), not Colima HOME.
    env = dict(os.environ, CINTER_BUILDS_ROOT=str(builds),
               CINTER_CRAFTMASTER_SAVES_ROOT=str(_host_saves_root(builds)))
    # A compiler-only reference mount is never a host world location.
    env.pop('CINTER_CRAFTMASTER_REFERENCE_ROOT', None)
    env.pop('CINTER_CRAFTMASTER_DATA_OFFLINE', None)
    if not (env.get('CINTERFORGE_CLOUDFLARE_CONFIG') or env.get('CINTER_CLOUDFLARE_CONFIG')):
        env['CINTERFORGE_CLOUDFLARE_CONFIG'] = str(_host_user_home() / 'cfdev/config.json')
    return env



SERVER_TEST_NAME = 'CinterForge TestGrounds'


def _world_artifacts(builds: Path):
    # Delegate package validation and installation to the owning CraftMaster
    # implementation; never implement a second world generator or converter.
    import importlib.util
    source = (dependency_source(builds, 'cinterforge-craftmaster') / 'lifecycle/world_artifacts.py'
              if _local_craftmaster(builds) else _published_data(builds) / 'world_artifacts.py')
    spec = importlib.util.spec_from_file_location('_terminal_test_world_artifacts', source)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)
    return module


def server_test_fixture(builds: Path) -> tuple[Path, dict]:
    """Verify the one immutable Setup snapshot; Test never downloads or generates."""
    root = builds / 'Acceptance/CinterForgeTerminal/TestGrounds'
    path = root / 'fixture.json'
    if root.is_symlink() or root.resolve() != root.absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError('Server test fixture missing; run ./Cinter setup testgrounds: ' + str(path))
    receipt = json.loads(path.read_text('utf-8'))
    java = root / 'Java'
    bedrock = root / 'TestGrounds.mcworld'
    artifacts = _world_artifacts(builds)
    if (receipt.get('schema') != 'cinterforge.terminal.server-fixture.v1' or
            receipt.get('name') != SERVER_TEST_NAME or receipt.get('channel') != 'beta' or
            not str(receipt.get('source', '')).startswith('beta publication ') or java.is_symlink() or
            not (java / 'level.dat').is_file() or bedrock.is_symlink() or
            not bedrock.is_file() or
            artifacts.java_content_hashes(java) != receipt.get('java_files') or
            _digest(bedrock) != receipt.get('mcworld_sha256')):
        raise RuntimeError('Server test fixture changed; run ./Cinter setup testgrounds: ' + str(root))
    return root, receipt


def read_native_test_fixture_receipt(path: Path) -> dict:
    """Read the native-install binding; never promote an unverified snapshot."""
    repair = 'run ./Cinter setup testgrounds (world-only Setup)'
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('TestGrounds fixture receipt missing; ' + repair)
    receipt = json.loads(path.read_text('utf-8'))
    entry = receipt.get('catalog_entry')
    if (receipt.get('schema') != 'cinterforge.terminal.server-fixture.v1' or
            receipt.get('name') != SERVER_TEST_NAME or receipt.get('channel') != 'beta' or
            not isinstance(entry, dict) or not entry.get('folder') or not entry.get('sha256') or
            receipt.get('source') != 'beta publication ' + str(entry['sha256']) or
            not isinstance(receipt.get('installed_java'), str) or not receipt['installed_java']):
        raise RuntimeError('TestGrounds native install receipt missing or outdated; ' + repair)
    return receipt


def _verify_installed_testgrounds(artifacts, installed: Path, entry: dict, receipt: dict) -> None:
    """Bind the Setup snapshot to the existing verified native publication.

    Compare publisher hashes, not the possibly played Java save's current bytes.
    Neither this check nor a receipt upgrade replaces local gameplay.
    """
    publication_path = installed / artifacts.INSTALL_RECEIPT
    manifest_path = installed / artifacts.PACKAGE_MANIFEST
    for path in (publication_path, manifest_path):
        if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))):
            raise RuntimeError('Installed beta TestGrounds artifact missing or linked: ' + str(path))
    publication = json.loads(publication_path.read_text('utf-8'))
    manifest = json.loads(manifest_path.read_text('utf-8'))
    export = manifest.get('conversion_export', SERVER_TEST_NAME)
    if export not in (SERVER_TEST_NAME, installed.name):
        raise RuntimeError('Invalid published TestGrounds conversion export name: ' + str(export))
    archive = installed / 'CinterForge/Worlds' / export / (export + '.mcworld')
    if (not archive.is_file() or any(p.is_symlink() for p in (archive, *archive.parents))):
        raise RuntimeError('Installed beta TestGrounds archive missing or linked: ' + str(archive))
    record = manifest.get('files', {}).get(archive.relative_to(installed).as_posix(), {})
    if (publication.get('schema') != 'cinterforge.craftmaster.java-install.v1' or
            publication.get('world') != SERVER_TEST_NAME or publication.get('channel') != 'beta' or
            publication.get('folder') != entry['folder'] or publication.get('sha256') != entry['sha256'] or
            publication.get('java_files') != receipt['java_files'] or
            manifest.get('schema') != 'cinterforge.craftmaster.java-package.v1' or
            manifest.get('world') != SERVER_TEST_NAME or manifest.get('folder') != entry['folder'] or
            record.get('sha256') != receipt['mcworld_sha256'] or
            _digest(archive) != receipt['mcworld_sha256']):
        raise RuntimeError('Installed beta TestGrounds does not match the verified fixture: ' + str(installed))


def prepare_server_test_fixture(builds: Path) -> None:
    """Acquire the single published beta smoke world; never generate a replacement."""
    artifacts = _world_artifacts(builds)
    print('[SERVER FIXTURE] Checking beta ' + SERVER_TEST_NAME, flush=True)
    entries = artifacts.fetch_public_catalog('beta')
    if SERVER_TEST_NAME not in entries:
        raise RuntimeError('Publish the successful CraftMaster TestGrounds to beta first')
    entry = entries[SERVER_TEST_NAME]
    provenance = 'beta publication ' + entry['sha256']
    try:
        root, receipt = server_test_fixture(builds)
    except (OSError, ValueError, RuntimeError, KeyError):
        pass
    else:
        installed = _host_saves_root(builds) / entry['folder']
        if (receipt['source'] == provenance and
                artifacts.installed_world_current(installed, entry, allow_exact_local=True)):
            _verify_installed_testgrounds(artifacts, installed, entry, receipt)
            # Older Setup snapshots have the verified Java and MCWorld bytes
            # but no binding to the actual native install. Only upgrade when
            # the existing native publication independently matches them.
            binding = {'catalog_entry': entry, 'installed_java': str(installed.resolve())}
            if any(receipt.get(key) != value for key, value in binding.items()):
                receipt.update(binding)
                with tempfile.TemporaryDirectory(prefix='Receipt-', dir=root.parent) as temp:
                    updated = Path(temp) / 'fixture.json'
                    updated.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
                    os.replace(updated, root / 'fixture.json')
                print('[SERVER FIXTURE] Upgraded existing verified native install binding', flush=True)
            print('[SERVER FIXTURE] Reusing verified ' + str(root) + ' (' + provenance + ')', flush=True)
            return
    root = builds / 'Acceptance/CinterForgeTerminal/TestGrounds'
    if root.is_symlink() or root.resolve() != root.absolute() or (root.exists() and not root.is_dir()):
        raise RuntimeError('Unsafe server fixture destination: ' + str(root))
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='TestGrounds-', dir=root.parent) as temp:
        pending = Path(temp) / 'Fixture'
        pending.mkdir()
        package = Path(temp) / entry['archive']
        artifacts.download_public_package('beta', SERVER_TEST_NAME, entry, package, report=lambda line: print(line, flush=True))
        world = artifacts.install_java_package(package, Path(temp)/'Published', SERVER_TEST_NAME,
                                                entry, 'beta', report=lambda line: print(line, flush=True))
        manifest = json.loads((world / artifacts.PACKAGE_MANIFEST).read_text('utf-8'))
        export = manifest.get('conversion_export', SERVER_TEST_NAME)
        archive = world / 'CinterForge/Worlds' / export / (export + '.mcworld')
        record = manifest['files'].get(archive.relative_to(world).as_posix())
        if not record or not archive.is_file() or _digest(archive) != record['sha256']:
            raise RuntimeError('Published TestGrounds lacks its verified Bedrock conversion')
        # Install the same verified publication in the normal native Java saves
        # directory. Test and the installed app must resolve the same source.
        installed = artifacts.install_catalog_world(
            _host_saves_root(builds), SERVER_TEST_NAME, entry, 'beta',
            report=lambda line: print(line, flush=True),
            download=lambda target: shutil.copy2(package, target))
        selected = (world, archive, artifacts.java_content_hashes(world), record['sha256'], provenance)
        world, archive, files, digest, provenance = selected
        _verify_installed_testgrounds(artifacts, installed, entry,
                                     {'java_files': files, 'mcworld_sha256': digest})
        print('[SERVER FIXTURE] Copying verified Java and Bedrock saves: ' + provenance, flush=True)
        for relative in files:
            target = pending / 'Java' / artifacts.safe_member(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(world / relative, target)
        shutil.copy2(archive, pending / 'TestGrounds.mcworld')
        if (artifacts.java_content_hashes(pending / 'Java') != files or
                _digest(pending / 'TestGrounds.mcworld') != digest):
            raise RuntimeError('Server fixture source changed during copy')
        (pending / 'fixture.json').write_text(json.dumps({
            'schema': 'cinterforge.terminal.server-fixture.v1', 'name': SERVER_TEST_NAME,
            'source': provenance, 'channel': 'beta', 'java_files': files, 'mcworld_sha256': digest,
            'catalog_entry': entry, 'installed_java': str(installed.resolve()),
        }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        if root.exists():
            # Only replace this generated snapshot, never the source save.
            retired = Path(temp) / 'Retired'
            root.rename(retired)
            try:
                pending.rename(root)
            except BaseException:
                retired.rename(root)
                raise
        else:
            pending.rename(root)
    server_test_fixture(builds)
    print('[SERVER FIXTURE] Verified reusable snapshot: ' + str(root), flush=True)

def setup_receipts_present(builds: Path, targets: tuple[str, ...]) -> bool:
    """Ask the owning lifecycle to validate current shared data hashes.

    This runs on the host without booting a guest. A stale or missing guest
    receipt invalidates Terminal Setup, so the managed guest Setup runs before
    Build. Never repair a Setup receipt during Build.
    """
    if not targets:
        return True
    if any(target not in TARGETS for target in targets):
        return False
    try:
        local = _local_craftmaster(builds)
        if not local:
            for target in targets:
                _published_stage(builds, target)
            owner = craftmaster_bedrock_module(builds, 'CinterForgeJava')
            cm = owner.provider(_published_data(builds))
            root = builds / 'CinterForgeCraftMaster/Server/Java'
            for target in targets:
                owner.ensure_runtime(root, cm, target=target,
                                     arch=owner.native_target()[1] if target == 'mac' else 'x64',
                                     offline=True, setup_mac_builds=builds if target == 'mac' else None)
            return True
        source, _ = _source_roots(builds)
        result = subprocess.run(
            [sys.executable, '-B', str(source / 'Cinter'), 'setup', 'status', *targets],
            cwd=source, env=_host_environment(builds), check=False)
        if result.returncode != 0:
            return False
        owner = craftmaster_bedrock_module(builds, 'CinterForgeJava')
        cm = owner.provider(source / 'lifecycle', staged=False)
        root = builds / 'CinterForgeCraftMaster/Server/Java'
        if not cm.java_eula_accepted(root):
            return False
        for target in targets:
            owner.ensure_runtime(root, cm, target=target,
                           arch=owner.native_target()[1] if target == 'mac' else 'x64',
                           offline=True, setup_mac_builds=builds if target == 'mac' else None)
        return True
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


def _source_roots(builds: Path) -> tuple[Path, Path]:
    return tuple(dependency_source(builds, name) for name in SOURCES)


def _mac_lifecycle(builds: Path, action: str) -> None:
    source, leveldb = _source_roots(builds)
    if platform.system() != 'Darwin':
        raise RuntimeError('Mac CraftMaster native target requires Xcode/macOS')
    env = dict(_host_environment(builds),
               CINTER_LEVELDB_SOURCE=str(leveldb), CINTER_CRAFTMASTER_SKIP_WORLD_SETUP='1')
    from host.lifecycle.setup_progress import progress_step
    with progress_step('CraftMaster mac ' + action, prefix='[CRAFTMASTER]'):
        subprocess.run([sys.executable, '-B', str(source / 'Cinter'), action, 'mac'],
                       cwd=source, env=env, check=True)


def _prepare_guest_cmake_generator(builds: Path, target: str) -> None:
    """Keep generated CraftMaster CMake trees compatible with the t guest.

    The AMD64 image provisions Ninja, not GNU Make. A previous failed
    configure may have persisted the default 'Unix Makefiles' generator;
    simply setting CMAKE_GENERATOR=Ninja cannot reuse that cache. Only the
    target's disposable CMake intermediates are invalidated, never sources,
    build receipts, staged assets or any other platform's generated files.
    """
    root = builds / 'CinterForgeCraftMaster' / 'Intermediates' / target
    if root.is_symlink():
        raise RuntimeError('Refusing symlinked CraftMaster CMake tree: ' + str(root))
    if not root.is_dir():
        return
    cache = root / 'CMakeCache.txt'
    if cache.is_symlink():
        raise RuntimeError('Refusing symlinked CraftMaster CMake cache: ' + str(cache))
    if cache.is_file():
        cached = next((line.partition('=')[2].strip()
                       for line in cache.read_text(encoding='utf-8', errors='replace').splitlines()
                       if line.startswith('CMAKE_GENERATOR:INTERNAL=')), '')
    else:
        cached = ''
    if cached == 'Ninja':
        return
    if cache.is_file() or (root / 'CMakeFiles').exists():
        print('[CRAFTMASTER BUILD] Resetting only ' + target +
              ' CMake intermediates (cached generator=' + (cached or 'unknown') +
              '; t guest requires Ninja)', flush=True)
        shutil.rmtree(root)


def _guest_lifecycle(builds: Path, action: str, target: str, *, docker: str | None = None,
                     cross=None, acceptance: Path | None = None, test_python: Path | None = None) -> None:
    """Run standalone Cinter in Terminal's pre-existing AMD64 Debian image.

    Both source trees mount readonly; software outputs remain under /builds.
    Only the host's non-world ReferenceData directory is mounted for compilation.
    No user saves, maps or annotations are exposed as guest saves. Compiler
    smoke fixtures use the disposable Linux home. Explicit standalone Test
    mounts only its owned fixture directory, with no access to user worlds.
    """
    if target not in ('linux', 'win'):
        raise ValueError('Unexpected CraftMaster guest platform: ' + target)
    source, leveldb = _source_roots(builds)
    if action in ('setup', 'build'):
        # Data acquisition and mapping generation are host-owned, respectively
        # Setup and Build. The existing compiler guest has no network.
        subprocess.run([sys.executable, '-B', str(source / 'Cinter'), action, 'data'],
                       cwd=source, env=_host_environment(builds), check=True)
    from host.windows.windows_environment import WindowsCrossEnvironment
    from host.lifecycle.amd64_backend import docker_bind_source
    # Setup alone created the pinned Windows/Linux closure. Build may import its
    # saved image but must never download or alter its package recipe.
    if cross is None:
        if action != 'setup':
            WindowsCrossEnvironment.validate_staged(builds)
        cross = WindowsCrossEnvironment(builds).ensure_portable_image()
    docker = docker or cross.docker
    if not docker or not cross.image:
        raise RuntimeError('Setup-provisioned AMD64 compiler image unavailable')
    # Share only CraftMaster's non-world data workspace with the compiler guest.
    saves = _host_saves_root(builds)
    workspace = saves / 'CinterForge'
    shared = workspace / 'ReferenceData'
    if workspace.is_symlink() or shared.is_symlink() or (workspace / 'level.dat').exists():
        raise RuntimeError('Unsafe CraftMaster reference workspace: ' + str(shared))
    if not shared.is_dir():
        raise RuntimeError('CraftMaster reference data missing; run Terminal Setup')
    mounts = [('--mount', f'type=bind,src={docker_bind_source(source)},dst=/craftmaster,readonly'),
              ('--mount', f'type=bind,src={docker_bind_source(leveldb)},dst=/leveldb,readonly'),
              ('--mount', f'type=bind,src={docker_bind_source(builds)},dst=/builds'),
              ('--mount', f'type=bind,src={docker_bind_source(shared)},dst=/craftmaster-reference')]
    if action == 'test':
        expected = workspace / 'Acceptance' / target
        if (acceptance != expected or not expected.is_dir() or
                any(p.is_symlink() for p in (expected, expected.parent, workspace)) or
                (target == 'win') != (test_python is not None)):
            raise RuntimeError('Invalid isolated CraftMaster guest acceptance inputs')
        mounts.append(('--mount', f'type=bind,src={docker_bind_source(expected)},dst=/craftmaster-saves/CinterForge/Acceptance/{target}'))
    elif acceptance is not None or test_python is not None:
        raise RuntimeError('Guest acceptance inputs are only valid for Test')
    cmd = [docker, 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none']
    for item in mounts:
        cmd.extend(item)
    wine_test = action == 'test' and target == 'win'
    def target_path(path):
        return 'Z:' + path.replace('/', '\\') if wine_test else path
    cmd.extend(['--workdir', '/craftmaster', '--env', 'CINTER_BUILDS_ROOT=' + target_path('/builds'),
                '--env', 'CINTER_LEVELDB_SOURCE=' + target_path('/leveldb'),
                '--env', 'PYTHONUNBUFFERED=1', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--env', 'CINTER_CRAFTMASTER_SKIP_WORLD_SETUP=1',
                '--env', 'CINTER_CRAFTMASTER_DATA_OFFLINE=1',
                '--env', 'CINTER_CRAFTMASTER_REFERENCE_ROOT=' + target_path('/craftmaster-reference')])
    if action == 'test':
        temp = '/builds/Intermediates/CinterForgeCraftMaster/CompilerTemp'
        (builds / temp.removeprefix('/builds/')).mkdir(parents=True, exist_ok=True)
        cmd.extend(['--env', 'TMPDIR=' + temp, '--env', 'TEMP=' + target_path(temp), '--env', 'TMP=' + target_path(temp)])
    if wine_test:
        # Wine translates Windows Unicode paths to the Unix filesystem using
        # the host locale. The pinned Debian image has glibc's C.UTF-8 locale;
        # do not inherit Docker's potentially ASCII C locale. Keep redirected
        # Windows Python diagnostics UTF-8 for the host lifecycle log as well.
        cmd.extend(['--mount', f'type=volume,src={cross._require_wine_volume()},dst=/cinterwine',
                    '--env', 'HOME=/cinterwine/home', '--env', 'XDG_CACHE_HOME=/cinterwine/home/.cache',
                    '--env', 'LANG=C.UTF-8', '--env', 'LC_ALL=C.UTF-8',
                    '--env', 'PYTHONUTF8=1', '--env', 'PYTHONIOENCODING=utf-8',
                    '--env', 'WINEPREFIX=/cinterwine/prefix', '--env', 'WINEARCH=win64', '--env', 'WINEDEBUG=-all'])
    else:
        cmd.extend(['--env', 'HOME=/tmp/craftmaster-compiler-home'])
    if action == 'test':
        cmd.extend(['--env', 'CINTER_CRAFTMASTER_SAVES_ROOT=' + target_path('/craftmaster-saves')])
    if action == 'build':
        # Debian t guest contains ninja-build and GCC; it deliberately has no
        # GNU Make. CraftMaster's CMake defaults to Unix Makefiles otherwise.
        _prepare_guest_cmake_generator(builds, target)
        cmd.extend(['--env', 'CMAKE_GENERATOR=Ninja'])
    if target == 'win' and not wine_test:
        from host.lifecycle.webview import TAG
        toolchain = ('/builds/Toolchains/WebView/' + TAG +
                     '/source/cmake/toolchains/x86_64-w64-mingw32.cmake')
        cmd.extend(['--env', 'CINTER_CRAFTMASTER_WIN_CMAKE_TOOLCHAIN_FILE=' + toolchain])
    if hasattr(os, 'getuid'):
        cmd.extend(['--user', str(os.getuid()) + ':' + str(os.getgid())])
    if wine_test:
        assert test_python is not None
        cmd.extend([cross.image, 'wine', cross.path(test_python), '-B', '-u', target_path('/craftmaster/Cinter'), action, target])
    else:
        cmd.extend([cross.image, 'python3', '-B', '-u', '/craftmaster/Cinter', action, target])
    from host.lifecycle.setup_progress import progress_step
    with progress_step('CraftMaster ' + target + ' ' + action, prefix='[CRAFTMASTER]'):
        subprocess.run(cmd, check=True)



def craftmaster_world_command(builds: Path, args: list[str], *, optional: bool = False) -> None:
    """Delegate world publication/installation to its owning checkout on the host.

    Explicit world commands invoke the documented CraftMaster CLI, never a guest
    or an embedded duplicate. Ordinary Setup acquires only the server fixture.
    """
    if not _local_craftmaster(builds):
        # Public world Setup/list uses the exact same packaged installer as the
        # native owner. Private publication remains an explicit owner action.
        if args[0] == 'publish' or (args[0] == 'worlds' and len(args) > 1 and args[1] in ('local', 'internal')):
            raise RuntimeError('Run this explicit world action in the CraftMaster owner repository.')
        if args[:2] == ['setup', 'worlds']:
            if len(args) > 5:
                raise ValueError('Usage: ./Cinter setup worlds [beta|release ["<worldname>" [<sha256>]]]')
            channel = args[2] if len(args) >= 3 else 'release'
            name = args[3] if len(args) >= 4 else None
            pin = args[4] if len(args) >= 5 else None
        elif args[0] == 'worlds' and len(args) <= 2:
            channel = args[1] if len(args) == 2 else 'release'
            name = pin = None
        else:
            raise ValueError('Unsupported CraftMaster world command')
        if channel not in ('beta', 'release'):
            raise RuntimeError('Internal world actions require the CraftMaster owner repository.')
        if args[0] == 'setup':
            native = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}[platform.system()]
            acquire_published_craftmaster(builds, (native,))
        artifacts = _world_artifacts(builds)
        if not hasattr(artifacts, 'install_public_worlds'):
            raise RuntimeError('Publish current CraftMaster owner builds with the public world installer first.')
        return artifacts.install_public_worlds(_host_saves_root(builds), channel, name=name,
                     expected_sha256=pin, list_only=args[0] == 'worlds', optional=optional,
                     report=lambda line: print(line, flush=True))
    source = dependency_source(builds, 'cinterforge-craftmaster')
    env = dict(_host_environment(builds), PYTHONDONTWRITEBYTECODE='1')
    if optional:
        env['CINTER_CRAFTMASTER_OPTIONAL_WORLD_SETUP'] = '1'
    else:
        env.pop('CINTER_CRAFTMASTER_OPTIONAL_WORLD_SETUP', None)
    subprocess.run([sys.executable, '-B', str(source / 'Cinter'), *args], cwd=source, env=env, check=True)

def setup_craftmaster(builds: Path, *, targets: tuple[str, ...] = TARGETS,
                      guest: bool = False, docker: str | None = None) -> None:
    """Provision selected local builds, or only runtime prerequisites for published Stage."""
    if not _local_craftmaster(builds):
        for target in targets:
            _published_stage(builds, target)
        if targets:
            owner = craftmaster_bedrock_module(builds, 'CinterForgeJava')
            cm = owner.provider(_published_data(builds))
            root = builds / 'CinterForgeCraftMaster/Server/Java'
            cm.require_java_eula(root)
            if 'mac' in targets:
                cm.setup_mac_jdk(builds, print)
                cached = _host_saves_root(builds) / 'CinterForge/ReferenceData/Java' / cm.JAVA_VERSION / f'minecraft_server_{cm.JAVA_VERSION}.jar'
                owner.server_jar(root, cm, acquire=True, cached=cached)
            for target in targets:
                owner.ensure_runtime(root, cm, target=target,
                    arch=owner.native_target()[1] if target == 'mac' else 'x64',
                    offline=(target == 'mac'), setup_mac_builds=builds if target == 'mac' else None)
        return
    for target in targets:
        if target not in TARGETS:
            raise ValueError('Unknown CraftMaster target: ' + target)
        if target == 'mac':
            if guest:
                raise RuntimeError('Mac CraftMaster cannot compile in the Linux guest')
            _mac_lifecycle(builds, 'setup')
        else:
            if not guest:
                raise RuntimeError('Linux/Windows CraftMaster Setup requires the managed AMD64 guest')
            _guest_lifecycle(builds, 'setup', target, docker=docker)
    # Host-side acquisition for foreign targets uses the CraftMaster owner; compiler guests stay offline.
    if targets:
        owner = craftmaster_bedrock_module(builds, 'CinterForgeJava')
        source, _ = _source_roots(builds)
        cm = owner.provider(source / 'lifecycle', staged=False)
        root = builds / 'CinterForgeCraftMaster/Server/Java'
        cm.require_java_eula(root)
        cache = _host_saves_root(builds) / 'CinterForge/ReferenceData/Java' / cm.JAVA_VERSION
        cached = cache / f'minecraft_server_{cm.JAVA_VERSION}.jar'
        if 'mac' in targets:
            # CraftMaster already provisioned the shared Mac JDK above.
            owner.server_jar(root, cm, acquire=True, cached=cached)
            owner.ensure_runtime(root, cm, target='mac', arch=owner.native_target()[1],
                           offline=True, setup_mac_builds=builds)
        for target in targets:
            if target != 'mac':
                owner.ensure_runtime(root, cm, target=target, arch='x64', cached=cached)


def build_craftmaster(builds: Path, *, targets: tuple[str, ...] = TARGETS,
                      docker: str | None = None) -> None:
    """Build changed local native dependencies; never compile a published product."""
    if not _local_craftmaster(builds):
        identities = craftmaster_source_identity(builds, setup=True)
        for target in targets:
            scopes = ('win', 'all') if target == 'win' else ('all',)
            caches = (builds / 'Intermediates/CinterForgeTerminal/SetupCache' / (scope + '.json') for scope in scopes)
            if not any(path.is_file() and not path.is_symlink() and
                       json.loads(path.read_text('utf-8')).get('craftmaster_sources') == identities for path in caches):
                raise RuntimeError('Published CraftMaster selection changed; run Terminal Setup for ' + target)
            stage = _published_stage(builds, target)
            if target == 'win':
                # Current Terminal packaging still owns supplemental PE DLL
                # closure; this is distribution assembly, not owner compilation.
                _build_windows_runtime(builds)
            print('[CRAFTMASTER BUILD] Using verified published ' + target + ': ' + str(stage), flush=True)
        return
    owner_root = builds / 'CinterForgeCraftMaster'
    receipt_root = builds / 'CinterForgeTerminal/Dependencies/CraftMaster'
    receipt_root.mkdir(parents=True, exist_ok=True)
    sources = craftmaster_source_identity(builds)
    for target in targets:
        if target not in TARGETS:
            raise ValueError('Unknown CraftMaster target: ' + target)
        receipt = receipt_root / ('build-' + target + '.json')
        if receipt.is_symlink():
            raise RuntimeError('Unsafe dependency receipt: ' + str(receipt))
        setup = owner_root / 'Setup' / (target + '.json')
        # Require the owner Setup and record the existing Terminal Setup
        # fingerprint, including the verified toolchain closure. A changed
        # setup, source revision or wrapper invalidates the prior reuse gate.
        scopes = ('win', 'all') if target == 'win' else ('all',)
        cache = next((builds / 'Intermediates/CinterForgeTerminal/SetupCache' / (scope + '.json')
                      for scope in scopes if (builds / 'Intermediates/CinterForgeTerminal/SetupCache' / (scope + '.json')).is_file()), None)
        if not setup.is_file() or cache is None:
            raise RuntimeError('CraftMaster requires successful Terminal Setup for ' + target)
        setup_cache = json.loads(cache.read_text(encoding='utf-8'))
        if setup_cache.get('craftmaster_sources') != craftmaster_source_identity(builds, setup=True):
            raise RuntimeError('CraftMaster local source selection changed after Setup; rerun Terminal Setup')
        inputs = {'sources': sources, 'setup_sha256': _digest(setup),
                  'terminal_setup_sha256': _digest(cache), 'integration_sha256': _digest(Path(__file__))}

        def record():
            stage = staged_craftmaster(builds, target)
            built = owner_root / 'Build' / target / 'receipt.json'
            result = {'inputs': inputs, 'build_sha256': _digest(built),
                      'stage_sha256': _digest(stage / 'manifest.json')}
            if target == 'win':
                runtime, _ = _verified_windows_runtime(builds, stage / 'bin/CinterForgeCraftMaster.exe')
                result['windows_runtime_sha256'] = _digest(runtime / 'manifest.json')
            return result

        if receipt.is_file():
            try:
                if json.loads(receipt.read_text(encoding='utf-8')) == record():
                    print('[CRAFTMASTER BUILD] CURRENT ' + target + ': verified dependency outputs', flush=True)
                    continue
            except (OSError, ValueError, KeyError, RuntimeError):
                pass
        print('[CRAFTMASTER BUILD] DIRTY ' + target + ': source, inputs or outputs changed', flush=True)
        if not setup_receipts_present(builds, (target,)):
            raise RuntimeError('CraftMaster owner Setup is no longer current for ' + target)
        receipt.unlink(missing_ok=True)
        # Build and internal Stage are delegated to the owner. Terminal's
        # public Stage later installs only the completed Terminal application.
        for action in ('build', 'stage'):
            if target == 'mac':
                _mac_lifecycle(builds, action)
            else:
                _guest_lifecycle(builds, action, target, docker=docker)
        if target == 'win':
            _build_windows_runtime(builds)
        if craftmaster_source_identity(builds) != sources:
            raise RuntimeError('CraftMaster source changed during Build')
        pending = receipt.with_suffix('.part')
        pending.write_text(json.dumps(record(), indent=2, sort_keys=True) + '\n', encoding='utf-8')
        pending.replace(receipt)
        print('[CRAFTMASTER BUILD] PASS new native target ' + target, flush=True)


def staged_craftmaster(builds: Path, target: str, *, candidate: Path | None = None) -> Path:
    """Verify the identical native Stage layout for local or published output."""
    if target not in TARGETS:
        raise ValueError('Unsupported CraftMaster target: ' + target)
    if candidate is None and not _local_craftmaster(builds):
        return _published_stage(builds, target)
    candidates = (candidate if candidate is not None else builds / 'CinterForgeCraftMaster/Stage' / target,)
    problems = []
    for root in candidates:
        if not root.is_dir() or root.is_symlink() or root.resolve() != root.absolute():
            continue
        try:
            manifest_file = root / 'manifest.json'
            if manifest_file.is_symlink() or not manifest_file.is_file():
                raise RuntimeError('No regular Stage manifest')
            manifest = json.loads(manifest_file.read_text('utf-8'))
            if (manifest.get('schema') != 'cinterforge.craftmaster.stage.v1' or
                    manifest.get('platform') != target):
                raise RuntimeError('Wrong platform or Stage schema')
            files = manifest.get('files')
            if not isinstance(files, dict) or not files:
                raise RuntimeError('Stage has no file inventory')
            if not {'data/world_artifacts.py', 'data/minecraft_data.py', 'data/java_server.py', 'data/CinterForgeJava.py', 'data/JavaGenerate.mcfunction', 'data/ResourcesNew.json', 'data/AuxValueToBlockStateMap.md',
                    'data/IntrinsicBlockStatesList.md', 'data/BlockStateAndTraitListings.md',
                    'data/CinterForgeBedrockAccess.py', 'data/CinterForgeBedrockProperties.py', 'data/CinterForgeLifecycleBedrock.py',
                    'data/bedrock_status_bridge/manifest.json', 'data/bedrock_status_bridge/scripts/main.js',
                    'data/bedrock_terminal_resources/manifest.json', 'data/bedrock_testgrounds/manifest.json',
                    'data/bedrock_testgrounds/scripts/main.js', 'data/BedrockReport/views/BedrockReport.html',
                    'data/BedrockReport/client/minecraft/CinterForgeBedrockReport.js'} <= set(files):
                raise RuntimeError('Stage lacks restored Bedrock mappings or the Java publication installer; rebuild CraftMaster')
            expected = {'manifest.json', *files}
            # Re-evaluate the manifest and all file bytes, not merely a receipt
            # or directory existence. Never accept symlinks or extra files.
            observed = set()
            for item in root.rglob('*'):
                if item.is_symlink():
                    raise RuntimeError('Stage contains a symlink: ' + str(item))
                if item.is_file():
                    observed.add(item.relative_to(root).as_posix())
                elif not item.is_dir():
                    raise RuntimeError('Stage contains a nonregular entry')
            if observed != expected and not (candidate is None and observed == expected | {'publication.json'}):
                raise RuntimeError('Stage inventory differs from manifest')
            for name, digest in files.items():
                relative = Path(name)
                if (not isinstance(name, str) or not name or relative.is_absolute()
                        or '..' in relative.parts or '\\' in name or
                        not isinstance(digest, str) or len(digest) != 64):
                    raise RuntimeError('Unsafe Stage file member: ' + str(name))
                file = root / relative
                if not file.is_file() or _digest(file) != digest:
                    raise RuntimeError('Stage file hash mismatch: ' + name)
            binary = root / 'bin' / ('CinterForgeCraftMaster.exe' if target == 'win' else 'CinterForgeCraftMaster')
            if not binary.is_file() or binary.stat().st_size < 1024 or binary.relative_to(root).as_posix() not in files:
                raise RuntimeError('CraftMaster binary missing from Stage receipt')
            if target != 'win' and os.name != 'nt' and not os.access(binary, os.X_OK):
                raise RuntimeError('CraftMaster Unix executable permissions missing')
            magic = binary.open('rb').read(4)
            if target == 'win' and magic[:2] != b'MZ':
                raise RuntimeError('CraftMaster Windows binary is not PE')
            if target == 'linux' and magic != b'\x7fELF':
                raise RuntimeError('CraftMaster Linux binary is not ELF')
            if target == 'mac' and magic not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
                raise RuntimeError('CraftMaster Mac binary is not Mach-O')
            return root
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            problems.append(str(root) + ': ' + str(error))
    raise RuntimeError('Missing verified native CraftMaster ' + target + ' Build Stage. '
                       'Run the owning local Build or Terminal Setup for published artifacts. '
                       + '; '.join(problems))



def _windows_runtime_root(builds: Path) -> Path:
    # Build-generated files belong to Terminal, never to CraftMaster's verified Stage.
    return builds / 'CinterForgeTerminal/Dependencies/CraftMaster/win'


def _build_windows_runtime(builds: Path) -> None:
    """Close CraftMaster's recursive PE DLL imports using Setup's pinned toolchain.

    CraftMaster Stage owns its own receipt and cannot be modified by Terminal.
    Terminal Build therefore stages only the additional, verified Windows DLLs
    that must sit *beside* its executable after Terminal Stage installs it.
    """
    stage = staged_craftmaster(builds, 'win')
    binary = stage / 'bin/CinterForgeCraftMaster.exe'
    from host.windows.windows_environment import WindowsCrossEnvironment
    from host.windows.windows_runtime_dlls import stage_cross
    cross = WindowsCrossEnvironment(builds).ensure_portable_image()
    destination = _windows_runtime_root(builds)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError('Refusing linked CraftMaster Windows DLL closure')
    with tempfile.TemporaryDirectory(prefix='craftmaster-dlls-', dir=destination.parent) as tmp:
        candidate = Path(tmp) / 'runtime'
        candidate.mkdir()
        dlls = stage_cross(builds, cross, candidate, (binary,))
        for name, digest in dlls.items():
            staged = stage / 'bin' / name
            if staged.is_file() and _digest(staged) != digest:
                raise RuntimeError('CraftMaster Stage DLL conflicts with pinned Windows SDK: ' + name)
        manifest = {'schema': WINDOWS_RUNTIME_SCHEMA,
                    'binary_sha256': _digest(binary), 'files': dlls}
        (candidate / 'manifest.json').write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        # Never leave an old DLL set reachable after a new executable is built.
        # The validated old output may be discarded: both directories are generated.
        if destination.exists():
            shutil.rmtree(destination)
        candidate.rename(destination)
    print('[CRAFTMASTER WINDOWS DLL] PASS ' + str(len(dlls)) +
          ' recursive DLLs packaged for Windows CraftMaster', flush=True)


def _verified_windows_runtime(builds: Path, binary: Path) -> tuple[Path, dict[str, str]]:
    root = _windows_runtime_root(builds)
    manifest_file = root / 'manifest.json'
    if root.is_symlink() or not root.is_dir() or manifest_file.is_symlink() or not manifest_file.is_file():
        raise RuntimeError('Missing CraftMaster Windows DLL closure; run ./Cinter build win')
    doc = json.loads(manifest_file.read_text(encoding='utf-8'))
    files = doc.get('files')
    if (doc.get('schema') != WINDOWS_RUNTIME_SCHEMA or
            doc.get('binary_sha256') != _digest(binary) or not isinstance(files, dict)):
        raise RuntimeError('Stale CraftMaster Windows DLL closure; run ./Cinter build win')
    from host.windows.windows_runtime_dlls import DLL_SAFE
    if any(not isinstance(name, str) or not DLL_SAFE.fullmatch(name) or
           not isinstance(digest, str) or len(digest) != 64 or
           not all(c in '0123456789abcdef' for c in digest)
           for name, digest in files.items()):
        raise RuntimeError('Invalid CraftMaster Windows DLL receipt')
    observed = {entry.name for entry in root.iterdir() if entry.is_file()}
    if (observed != set(files) | {'manifest.json'} or
            any(item.is_symlink() or not item.is_file() for item in root.iterdir())):
        raise RuntimeError('CraftMaster Windows DLL closure inventory mismatch')
    for name, digest in files.items():
        file = root / name
        if (not file.is_file() or file.is_symlink() or
                file.open('rb').read(2) != b'MZ' or _digest(file) != digest):
            raise RuntimeError('CraftMaster Windows DLL missing/corrupt: ' + name)
    return root, files


def verify_packaged_windows_runtime(folder: Path, binary: Path) -> None:
    """Read-only validation of Terminal's installed supplemental PE closure."""
    manifest = folder / 'windows-runtime.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError('Installed CraftMaster Windows DLL receipt missing')
    doc = json.loads(manifest.read_text('utf-8'))
    files = doc.get('files')
    if (doc.get('schema') != WINDOWS_RUNTIME_SCHEMA or
            doc.get('binary_sha256') != _digest(binary) or not isinstance(files, dict)):
        raise RuntimeError('Installed CraftMaster Windows DLL receipt mismatch')
    from host.windows.windows_runtime_dlls import DLL_SAFE
    for name, digest in files.items():
        if (not isinstance(name, str) or not DLL_SAFE.fullmatch(name) or
                not isinstance(digest, str) or len(digest) != 64 or
                not all(c in '0123456789abcdef' for c in digest)):
            raise RuntimeError('Invalid installed CraftMaster Windows DLL receipt')
        file = binary.parent / name
        if (file.is_symlink() or not file.is_file() or
                file.open('rb').read(2) != b'MZ' or _digest(file) != digest):
            raise RuntimeError('Installed CraftMaster Windows DLL missing/corrupt: ' + name)



def craftmaster_minecraft_runtime(builds: Path, target: str) -> dict[str, Path]:
    """Embed owner-built modules at existing Terminal import locations.

    This is distribution assembly, never a second authored implementation.
    """
    return craftmaster_minecraft_files(staged_craftmaster(builds, target) / 'data')


def craftmaster_minecraft_files(data: Path) -> dict[str, Path]:
    """Exact owner-to-desktop assembly mapping for an already verified data tree."""
    import ast
    owner = ast.parse((data / 'CinterForgeBedrockAccess.py').read_bytes())
    exports = {node.name for node in owner.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    required = {'copy_managed_world', 'install_cinter_utility_pack', 'sync_package_run_tree', '_checked_url',
                '_curl_download', 'discover_lan_bedrock', 'mac_bedrock_run_command',
                'compatible_bds_entry', 'verified_bds_receipt', 'prepare_display_provider',
                'install_display_native_plugin'}
    if not required <= exports:
        raise RuntimeError('CraftMaster Stage lacks the required Bedrock hosting interface: '
                           + ', '.join(sorted(required - exports))
                           + '; rebuild the local owner or publish an updated CraftMaster package')
    files = {'runtime/' + name: data / name for name in (
        'CinterForgeBedrockAccess.py', 'CinterForgeBedrockProperties.py')}
    # Use the actual owner report, not a second authored desktop copy.
    for relative in ('views/BedrockReport.html', 'client/minecraft/CinterForgeBedrockReport.js'):
        source = data / 'BedrockReport' / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError('CraftMaster Stage missing report asset: ' + relative)
        files[relative] = source
    for pack in ('bedrock_status_bridge', 'bedrock_terminal_resources', 'bedrock_testgrounds'):
        if not (data / pack / 'manifest.json').is_file():
            raise RuntimeError('CraftMaster Stage missing ' + pack)
        files.update({p.relative_to(data).as_posix(): p for p in (data / pack).rglob('*') if p.is_file()})
    return files

def packaged_craftmaster_assets(builds: Path, target: str, prefix: str) -> dict[str, Path]:
    """Return prevalidated regular files for Terminal Build's existing inventory."""
    stage = staged_craftmaster(builds, target)
    assets = {prefix + '/' + file.relative_to(stage).as_posix(): file
              for file in sorted(stage.rglob('*')) if file.is_file()}
    if target == 'win':
        root, files = _verified_windows_runtime(builds, stage / 'bin/CinterForgeCraftMaster.exe')
        assets[prefix + '/windows-runtime.json'] = root / 'manifest.json'
        for name in files:
            asset = prefix + '/bin/' + name
            if asset in assets and _digest(assets[asset]) != files[name]:
                raise RuntimeError('CraftMaster Windows DLL collides with signed Stage: ' + name)
            assets[asset] = root / name
    return assets


def verify_embedded_craftmaster(app_root: Path, target: str) -> Path:
    """Test the installed Terminal release contains the exact native CLI/data.

    This is read-only. It does not accept a source checkout or download anything.
    """
    if target not in TARGETS:
        raise ValueError('Invalid CraftMaster platform')
    base = (app_root / 'Contents/Resources/Builds' if target == 'mac'
            else app_root / 'Builds')
    folder = base / 'CinterForgeCraftMaster' / target
    if folder.is_symlink() or not folder.is_dir():
        raise RuntimeError('Installed Terminal missing CraftMaster: ' + str(folder))
    manifest_file = folder / 'manifest.json'
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise RuntimeError('Installed CraftMaster has no Stage receipt')
    doc = json.loads(manifest_file.read_text('utf-8'))
    if (doc.get('schema') != 'cinterforge.craftmaster.stage.v1'
            or doc.get('platform') != target or not isinstance(doc.get('files'), dict)):
        raise RuntimeError('Installed CraftMaster receipt mismatch')
    files = doc['files']
    if not files:
        raise RuntimeError('Installed CraftMaster receipt is empty')
    for name, digest in files.items():
        relative = Path(name)
        if (not isinstance(name, str) or relative.is_absolute() or '..' in relative.parts or
                '\\' in name or not isinstance(digest, str) or len(digest) != 64):
            raise RuntimeError('Unsafe installed CraftMaster path')
        item = folder / relative
        if item.is_symlink() or not item.is_file() or _digest(item) != digest:
            raise RuntimeError('Installed CraftMaster file failed SHA-256: ' + name)
    binary_name = 'CinterForgeCraftMaster.exe' if target == 'win' else 'CinterForgeCraftMaster'
    binary = folder / 'bin' / binary_name
    if 'bin/' + binary_name not in files or not binary.is_file():
        raise RuntimeError('Installed CraftMaster native CLI missing')
    if not all('data/' + data in files for data in
               ('Resources.json', 'Contours.json', 'MCTemplate.mcworld',
                'world-template.htm', 'zoomify-viewer.min.js', 'CinterForgeAtlas.js', 'TownNames.json', 'preview.py', 'world_artifacts.py',
                'AuxValueToBlockStateMap.md', 'IntrinsicBlockStatesList.md', 'BlockStateAndTraitListings.md',
                'ResourcesNew.json', 'minecraft_data.py', 'java_server.py', 'JavaGenerate.mcfunction')):
        raise RuntimeError('Installed CraftMaster runtime data is incomplete')
    if target == 'win':
        verify_packaged_windows_runtime(folder, binary)
    return binary
