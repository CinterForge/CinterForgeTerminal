"""Conservative, read-only dirty gate for an already successful developer Setup.

An unchanged Setup must not boot the c LinuxKit or t AMD64 producer merely to
re-probe its existing closure.  This *does not* replace first-run Setup or its
real probes: the cache is written only after they succeed, and a missing,
changed, or unrecognized input always falls through to normal Setup.

The receipt lives in sibling Builds/Intermediates, not in the packaged SDK or
repository.  Metadata inventories detect normal additions, removals and writes
without rehashing several GiB of verified runtime files on every invocation.
Build/Stage/Test still perform their own strict content checks.
"""
# CINTERFORGE SOURCE HEADER — host/lifecycle/setup_dirty.py
# PURPOSE: Tracks authored Setup inputs and dependency provenance to avoid unnecessary reprovisioning.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   _excluded: Handle excluded through this component's established interfaces.
#   _sources: Handle sources through this component's established interfaces.
#   _output_state: Inventory the actual closure using lstat; never follow outside links.
#   _receipt: Handle receipt through this component's established interfaces.
#   is_current: Return True only for a prior fully accepted and unchanged Setup.
#   invalidate: A dirty or interrupted retry cannot reuse an earlier success receipt.
#   commit: Persist a positive gate only after *every* existing Setup step passed.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import stat
import subprocess
import tempfile
import time

SCHEMA = 'cinterforge.terminal.setup-dirty.v1'
# This deliberately includes the Setup implementations, pinned recipes,
# bundled extensions and LinuxKit inputs, but NOT UI changes, tests, docs,
# Git objects, or generated source-checkout metadata.
SOURCE_TREES = ('host', 'webapp/codeoss', 'webapp/client', 'webapp/runtime', 'webapp/data', 'webapp/views', 'app/build', 'app/include', 'app/src')
SOURCE_FILES = ('Cinter', 'Cinter.cmd', 'webapp/CinterForgeTerminal.py')
# These are Setup-produced, BUILD-READ-ONLY closures.  No Builds/Logs,
# Intermediates compiler trees, application builds, or Steam stages are here.
ALL_OUTPUTS = (
    'Toolchains/mac',
    'Toolchains/WindowsCross',
    'Toolchains/Windows',
    'Toolchains/Linux',
    'Toolchains/Retro68',
    'Toolchains/WebView',
    'Toolchains/WebView2',
    'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64',
    'CinterForgeTerminal/Dependencies/BedrockDisplay',
    'CinterForgeTerminal/Dependencies/ZMachine/0.3.2',
    'CreativeSuite/CFOS/LinuxKit',
    'Intermediates/CinterForgeTerminal/ForeignPython',
    'CinterForgeCraftMaster/Server/Bedrock/Downloads/bedrock-server-linux.zip',
    'CinterForgeCraftMaster/Server/Bedrock/Downloads/bedrock-server-win.zip',
    'Python/Environments/mac/arm64/cinterterminalenv',
    'InteractiveFiction/CodeOSS/code-setup.json',
    'CinterForgeTerminal/Dependencies/Voxel/voxel-setup.json',
    'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph',
    'Lagomorph',
    'CinterForgeCraftMaster/Setup',
    'Run/CinterForgeTerminal/WindowsCross/WineVolume.json',
)
WIN_OUTPUTS = (
    'InteractiveFiction/CodeOSS/code-setup.json',
    'CinterForgeCraftMaster/Server/Bedrock/Downloads/bedrock-server-win.zip',
    'CinterForgeTerminal/Dependencies/BedrockDisplay/win',
    'Toolchains/mac',
    'Toolchains/WindowsCross',
    'Toolchains/Windows',
    'Toolchains/WebView',
    'Toolchains/WebView2',
    'Run/CinterForgeTerminal/WindowsCross/WineVolume.json',
    'CinterForgeCraftMaster/Setup',
    'CinterForgeTerminal/Dependencies/Voxel/voxel-setup.json',
)
# Runtime/test activity is allowed to write ephemeral caches; those bytes are
# not part of the Setup closure.  Everything else remains a dirty input.
IGNORED_PARTS = frozenset({'__pycache__', '.DS_Store', '.git', '.pytest_cache', '.mypy_cache'})
IGNORED_PREFIXES = (
    'Toolchains/mac/mamba-cache/',
    'Toolchains/Windows/MSYS2/tmp/',
    'Toolchains/Windows/MSYS2/var/log/',
)


def _excluded(relative: str) -> bool:
    path = Path(relative)
    return (bool(IGNORED_PARTS.intersection(path.parts)) or
            path.suffix.casefold() in ('.pyc', '.pyo') or
            any(relative.startswith(prefix) for prefix in IGNORED_PREFIXES))


def _sources(repo: Path, scope: str) -> str:
    digest = hashlib.sha256()
    paths = []
    for name in SOURCE_FILES:
        paths.append(repo / name)
    for tree in SOURCE_TREES:
        base = repo / tree
        if not base.is_dir() or base.is_symlink():
            raise FileNotFoundError(f'Setup source tree unavailable: {base}')
        paths.extend(file for file in base.rglob('*')
                     if file.is_file() and not _excluded(file.relative_to(repo).as_posix()))
    digest.update(SCHEMA.encode())
    digest.update(scope.encode())
    digest.update(str(repo.resolve()).encode())
    digest.update(platform.system().encode())
    digest.update(platform.machine().encode())
    digest.update(str(os.environ.get('CINTER_LAGOMORPH_SOURCE', '')).encode())
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f'Setup authored input is missing or linked: {path}')
        digest.update(path.relative_to(repo).as_posix().encode())
        digest.update(b'\0')
        with path.open('rb') as input_file:
            for block in iter(lambda: input_file.read(1024 * 1024), b''):
                digest.update(block)
        digest.update(b'\0')
    return digest.hexdigest()


def _output_state(builds: Path, scope: str) -> dict[str, dict]:
    """Inventory the actual closure using lstat; never follow outside links.

    No image extraction, Docker access, network access, or write is permitted.
    Missing roots cause a cache miss, not a misleading successful Setup.
    """
    roots = ALL_OUTPUTS if scope == 'all' else WIN_OUTPUTS
    if scope == 'win' and platform.system() != 'Darwin':
        roots = tuple(name for name in roots if name != 'Toolchains/mac')
    result = {}
    started = time.monotonic()
    last_heartbeat = started
    for name in roots:
        path = builds / name
        if path.is_symlink() or not path.exists():
            raise FileNotFoundError(f'Missing Setup output: {path}')
        digest = hashlib.sha256()
        count = 0

        def record(child: Path, label: str) -> None:
            nonlocal count
            meta = child.lstat()
            kind = ('link' if stat.S_ISLNK(meta.st_mode) else
                    'file' if stat.S_ISREG(meta.st_mode) else
                    'dir' if stat.S_ISDIR(meta.st_mode) else 'other')
            # Some pinned SDKs contain optional or platform-specific links.
            # Setup itself has already accepted the closure. Inventory whether
            # such links resolve instead of invalidating otherwise valid SDKs.
            digest.update((label + '\0' + kind + '\0').encode('utf-8'))
            if kind == 'link':
                digest.update(os.readlink(child).encode('utf-8'))
                digest.update(b':valid' if child.exists() else b':dangling')
            if kind == 'other':
                digest.update(str(meta.st_mode).encode())
            if kind == 'file':
                count += 1
                digest.update((f'{meta.st_size}:{meta.st_mtime_ns}:'
                               f'{meta.st_ctime_ns}:{stat.S_IMODE(meta.st_mode)}').encode())
            digest.update(b'\n')

        if path.is_file():
            record(path, '.')
        elif path.is_dir():
            # Sorted iteration produces a deterministic digest across runs.
            stack = [(path, '')]
            while stack:
                folder, rel = stack.pop()
                with os.scandir(folder) as entries:
                    children = sorted(entries, key=lambda e: e.name)
                for entry in children:
                    child_rel = f'{rel}/{entry.name}'.lstrip('/')
                    if _excluded(f'{name}/{child_rel}'):
                        continue
                    if time.monotonic() - last_heartbeat >= 10:
                        print(f'[TERMINAL SETUP] Checking cache closure {name} '
                              f'({int(time.monotonic() - started)}s elapsed)', flush=True)
                        last_heartbeat = time.monotonic()
                    child = Path(entry.path)
                    record(child, child_rel)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((child, child_rel))
        else:
            raise RuntimeError(f'Invalid Setup output: {path}')
        if not count:
            raise RuntimeError(f'Setup output has no files: {path}')
        result[name] = {'fingerprint': digest.hexdigest(), 'files': count}
    return result


def _receipt(builds: Path, scope: str) -> Path:
    if scope not in ('all', 'win'):
        raise ValueError('Unknown Setup scope: ' + scope)
    return builds / 'Intermediates/CinterForgeTerminal/SetupCache' / f'{scope}.json'



# VM state is intentionally not recursively included in ALL_OUTPUTS: every
# normal boot mutates its disk. Record the identity of the *verified* t profile
# instead, so a deleted/replaced guest cannot inherit an unrelated Setup PASS.
T_GUEST_SCHEMA = 'cinterforge.terminal.setup-t-guest-rsync.v1'


def _t_guest_state(builds: Path) -> dict[str, str | int]:
    """Read identity of the managed developer VM, without starting it."""
    vm_root = builds / 'CFTVM/T'
    colima_profile = vm_root / 'c/t'
    lima_instance = vm_root / 'l/colima-t'
    config = colima_profile / 'colima.yaml'
    guest_identity = lima_instance / 'CinterForgeSetupGuest.id'
    if (vm_root.is_symlink() or colima_profile.is_symlink() or
            lima_instance.is_symlink() or config.is_symlink() or
            guest_identity.is_symlink() or not guest_identity.is_file() or
            not colima_profile.is_dir() or not lima_instance.is_dir() or
            not config.is_file()):
        raise RuntimeError('Setup-provisioned t VM profile is missing or unsafe')
    # Inodes change when an instance is deleted and recreated, but not when
    # normal VM boot writes to its virtual disk or Docker volumes.
    identity = guest_identity.read_text(encoding='ascii').strip()
    if len(identity) != 32 or any(ch not in '0123456789abcdef' for ch in identity):
        raise RuntimeError('Managed t Lima instance lacks a valid Setup identity')
    colima_metadata = colima_profile.stat()
    lima_metadata = lima_instance.stat()
    return {
        'vm_root': str(vm_root.resolve(strict=True)),
        'lima_instance_identity': identity,
        'colima_directory_device': colima_metadata.st_dev,
        'colima_directory_inode': colima_metadata.st_ino,
        'lima_directory_device': lima_metadata.st_dev,
        'lima_directory_inode': lima_metadata.st_ino,
    }


def _t_guest_receipt(builds: Path) -> Path:
    return builds / 'CFTVM/T/config/DevToolsGuestSetup.json'


def record_verified_t_guest(builds: Path) -> None:
    """Seal a Setup-only rsync probe against the precise developer VM."""
    vm_root = builds / 'CFTVM/T'
    identity = vm_root / 'l/colima-t/CinterForgeSetupGuest.id'
    if identity.is_symlink():
        raise RuntimeError('Refusing linked Lima instance Setup identity')
    if not identity.exists():
        if not identity.parent.is_dir() or identity.parent.is_symlink():
            raise RuntimeError('Cannot identify missing managed Lima t instance')
        identity.write_text(secrets.token_hex(16) + '\n', encoding='ascii')
    state = _t_guest_state(builds)
    receipt = _t_guest_receipt(builds)
    if receipt.is_symlink():
        raise RuntimeError('Refusing linked DevTools guest receipt: ' + str(receipt))
    receipt.parent.mkdir(parents=True, exist_ok=True)
    payload = {'schema': T_GUEST_SCHEMA, 'profile': 't',
               'required_guest_executable': 'rsync', 'state': state}
    with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=receipt.parent,
            prefix='DevToolsGuestSetup-', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, receipt)
    finally:
        temporary.unlink(missing_ok=True)


def _require_verified_t_guest(builds: Path, scope: str) -> None:
    """A cached Mac Setup may reuse only its own rsync-verified t instance."""
    if scope != 'all' or platform.system() != 'Darwin':
        return
    receipt = _t_guest_receipt(builds)
    if receipt.is_symlink() or not receipt.is_file():
        raise RuntimeError('DevTools t guest has no verified rsync Setup receipt')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if (data.get('schema') != T_GUEST_SCHEMA or
            data.get('profile') != 't' or
            data.get('required_guest_executable') != 'rsync' or
            data.get('state') != _t_guest_state(builds)):
        raise RuntimeError('DevTools t guest changed since rsync provisioning')
    # VM disks are mutable and deliberately excluded from the cache inventory,
    # but Colima's saved topology is a Setup-owned *configuration*. A cached
    # Setup cannot claim CURRENT if the next Build would reject that topology.
    from host.lifecycle.CinterForgeLifecycleVM import CinterForgeLifecycleVM
    vm = CinterForgeLifecycleVM(builds)
    differences = vm.colima_configuration_differences(
        {'COLIMA_HOME': str(builds / 'CFTVM/T/c')}, vm.mac_host_mounts())
    if differences:
        raise RuntimeError('DevTools t guest saved configuration changed: ' +
                           '; '.join(differences))

def is_current(repo: Path, builds: Path, scope: str = 'all') -> bool:
    """Return True only for a prior fully accepted and unchanged Setup."""
    path = _receipt(builds, scope)
    if not path.is_file() or path.is_symlink():
        print(f'[TERMINAL SETUP] DIRTY {scope}: no trusted completed Setup receipt', flush=True)
        return False
    try:
        data = json.loads(path.read_text('utf-8'))
        if data.get('schema') != SCHEMA or data.get('scope') != scope or \
                data.get('builds') != str(builds.resolve()):
            raise ValueError('Setup receipt schema/scope/builds differs')
        from host.lifecycle.craftmaster_dependency import craftmaster_source_identity
        if data.get('craftmaster_sources') != craftmaster_source_identity(builds, setup=True):
            raise ValueError('CraftMaster or LevelDB source selection or managed revision changed')
        if data.get('source_sha256') != _sources(repo, scope):
            raise ValueError('Setup authored inputs changed')
        _require_verified_t_guest(builds, scope)
        observed = _output_state(builds, scope)
        if data.get('outputs') != observed:
            changed = next((key for key in observed if
                            data['outputs'].get(key) != observed[key]), 'missing output receipt')
            raise ValueError(f'Setup output changed: {changed}')
        print(f'[TERMINAL SETUP] CURRENT {scope}: verified authored inputs and '
              f'{sum(i["files"] for i in observed.values())} staged files; '
              'skipping unchanged provisioning and VM startup', flush=True)
        return True
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'[TERMINAL SETUP] DIRTY {scope}: {exc}; normal Setup required', flush=True)
        return False


def invalidate(builds: Path, scope: str = 'all') -> None:
    """A dirty or interrupted retry cannot reuse an earlier success receipt."""
    path = _receipt(builds, scope)
    if path.is_symlink():
        raise RuntimeError(f'Unsafe Setup receipt symlink: {path}')
    path.unlink(missing_ok=True)


def commit(repo: Path, builds: Path, scope: str = 'all') -> Path:
    """Persist a positive gate only after *every* existing Setup step passed."""
    path = _receipt(builds, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f'Unsafe Setup receipt path: {path}')
    _require_verified_t_guest(builds, scope)
    from host.lifecycle.craftmaster_dependency import craftmaster_source_identity
    data = {'schema': SCHEMA, 'scope': scope,
            'craftmaster_sources': craftmaster_source_identity(builds, setup=True),
            'builds': str(builds.resolve()),
            'source_sha256': _sources(repo, scope),
            'outputs': _output_state(builds, scope)}
    payload = json.dumps(data, indent=2, sort_keys=True) + '\n'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=f'.{scope}-', suffix='.tmp', delete=False) as dest:
        temporary = Path(dest.name)
        dest.write(payload)
        dest.flush()
        os.fsync(dest.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'[TERMINAL SETUP] SEALED {scope}: {len(data["outputs"])} output closures; '
          f'receipt {path}', flush=True)
    return path
