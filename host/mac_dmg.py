"""Reusable direct-distribution macOS drag-to-Applications DMG packaging.

Publish owns DMG creation and notarization. Setup owns verified extraction.
Neither Build, Stage nor Test calls this module. The OS/keychain own signing
credentials; never copy profiles or private keys into a release image.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import plistlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


INSTALL_README = """# Install CinterForge Terminal

1. Drag **CinterForge Terminal.app** onto the **Applications** shortcut.
2. Open CinterForge Terminal from Applications.
3. macOS may ask for permission to open a downloaded application or access files.
   Review these requests in the normal macOS interface.

CinterForge DevTools are optional and are installed separately from the app.
For information and support, visit https://www.cinterforge.com/.
"""


def _require_mac() -> None:
    if sys.platform != 'darwin':
        raise RuntimeError('DMG creation or extraction requires macOS and hdiutil')


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, timeout=1800)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _validate_app(app: Path) -> None:
    if not app.is_dir() or app.is_symlink() or app.suffix != '.app':
        raise RuntimeError('DMG input must be a real signed macOS .app directory')
    if not (app / 'Contents/Info.plist').is_file() or not (app / 'Contents/MacOS').is_dir():
        raise RuntimeError('DMG application bundle is incomplete: ' + str(app))
    with (app / 'Contents/Info.plist').open('rb') as plist_file:
        info = plistlib.load(plist_file)
    icon_name = str(info.get('CFBundleIconFile') or 'AppIcon.icns')
    if '/' in icon_name or '\\' in icon_name or icon_name in ('.', '..'):
        raise RuntimeError('Invalid macOS bundle icon path')
    if not icon_name.endswith('.icns'):
        icon_name += '.icns'
    if not (app / 'Contents/Resources' / icon_name).is_file():
        raise RuntimeError('Mac application is missing its declared product icon: ' + str(app))
    for item in app.rglob('*'):
        if item.is_symlink():
            # Legitimate bundle symlinks are relative and resolve inside the app.
            link = os.readlink(item)
            if os.path.isabs(link) or not (item.parent / link).resolve().is_relative_to(app.resolve()):
                raise RuntimeError('Unsafe application bundle symlink: ' + str(item))
    # Product-neutral Developer ID/ad-hoc signature validation. CinterForge
    # Terminal also uses its native Host signing check in the Build phase.
    _run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)])


@contextmanager
def mounted_dmg(dmg: Path, builds: Path):
    """Mount only inside Builds, always detach; do not touch global /Volumes."""
    _require_mac()
    if not dmg.is_file() or dmg.is_symlink():
        raise FileNotFoundError('Missing or unsafe macOS DMG: ' + str(dmg))
    scratch = builds / 'Intermediates/CinterForgeTerminal/DMG'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='Mount-', dir=scratch) as directory:
        mount = Path(directory) / 'Volume'
        mount.mkdir()
        attached = False
        try:
            _run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen',
                  '-mountpoint', str(mount), str(dmg)])
            attached = True
            yield mount
        finally:
            if attached:
                _run(['/usr/bin/hdiutil', 'detach', str(mount)])


def verify_dmg(dmg: Path, builds: Path, *, app_name: str, public: bool = False) -> None:
    """Verify actual mounted contents; for public DMGs also verify Apple ticket."""
    _require_mac()
    _run(['/usr/bin/hdiutil', 'verify', str(dmg)])
    with mounted_dmg(dmg, builds) as volume:
        app = volume / app_name
        _validate_app(app)
        shortcut = volume / 'Applications'
        if not shortcut.is_symlink() or os.readlink(shortcut) != '/Applications':
            raise RuntimeError('DMG missing the drag-to-Applications shortcut')
        readme = volume / 'README.md'
        if not readme.is_file() or readme.is_symlink():
            raise RuntimeError('DMG missing its installation README')
        visible = {p.name for p in volume.iterdir() if not p.name.startswith('.')}
        if visible != {app_name, 'Applications', 'README.md'}:
            raise RuntimeError('DMG contains unexpected visible top-level payloads')
        if public:
            _run(['xcrun', 'stapler', 'validate', str(app)])
            _run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=4', str(app)])
    if public:
        _run(['xcrun', 'stapler', 'validate', str(dmg)])


def _notarize_dmg(dmg: Path, profile: str) -> None:
    import json
    if not profile:
        raise RuntimeError('Public macOS DMG requires the notarytool keychain profile')
    result = subprocess.run(['xcrun', 'notarytool', 'submit', str(dmg),
                             '--keychain-profile', profile, '--wait',
                             '--output-format', 'json'],
                            capture_output=True, text=True, timeout=3600)
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError):
        response = {}
    if result.returncode or response.get('status') != 'Accepted':
        raise RuntimeError('Mac DMG notarization rejected: ' + (result.stderr or result.stdout)[-5000:])
    _run(['xcrun', 'stapler', 'staple', str(dmg)])


def create_dmg(app: Path, output: Path, builds: Path, *,
               volume_name: str, readme: str = INSTALL_README,
               public: bool = False, notary_profile: str = '') -> dict:
    """Create app + Applications shortcut + README, optionally notarize DMG."""
    _require_mac()
    _validate_app(app)
    if not output.is_relative_to(builds) or output.suffix.lower() != '.dmg':
        raise RuntimeError('Publish DMG must be generated under canonical Builds')
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = builds / 'Intermediates/CinterForgeTerminal/DMG'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='Publish-', dir=scratch) as directory:
        root = Path(directory)
        content = root / 'Content'
        content.mkdir()
        shutil.copytree(app, content / app.name, symlinks=True)
        (content / 'Applications').symlink_to('/Applications', target_is_directory=True)
        (content / 'README.md').write_text(readme, encoding='utf-8')
        candidate = root / 'Package.dmg'
        _run(['/usr/bin/hdiutil', 'create', '-ov', '-format', 'UDZO', '-fs', 'HFS+',
              '-volname', volume_name, '-srcfolder', str(content), str(candidate)])
        if public:
            _notarize_dmg(candidate, notary_profile)
        verify_dmg(candidate, builds, app_name=app.name, public=public)
        # Only promote an independently verified complete image.
        candidate.replace(output)
    return {'filename': output.name, 'sha256': _sha(output),
            'bytes': output.stat().st_size, 'target': 'mac', 'format': 'dmg',
            'app_name': app.name, 'notarized': bool(public)}


def restore_dmg(dmg: Path, builds: Path, *, app_name: str, destination: Path) -> None:
    """Setup-only: safely restore an app from an already SHA-verified DMG."""
    _require_mac()
    if not destination.is_relative_to(builds) or destination.name != app_name:
        raise RuntimeError('Unsafe Setup DMG destination')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError('Unsafe existing Mac application symlink')
    with mounted_dmg(dmg, builds) as volume:
        source = volume / app_name
        _validate_app(source)
        incoming = destination.parent / (app_name + '.setup-incoming')
        backup = destination.parent / (app_name + '.setup-previous')
        # An interrupted older Setup must not leave the app missing or
        # permanently block all future retries. Recover an owned backup, then
        # discard only our own incomplete incoming candidate.
        if backup.is_symlink() or incoming.is_symlink():
            raise RuntimeError('Unsafe Setup application recovery symlink')
        if backup.is_dir():
            if destination.exists():
                raise RuntimeError('Conflicting Setup Mac application backup')
            backup.replace(destination)
        if incoming.is_dir():
            shutil.rmtree(incoming)
        try:
            shutil.copytree(source, incoming, symlinks=True)
            _validate_app(incoming)
            previous = destination.exists()
            if previous:
                if not destination.is_dir():
                    raise RuntimeError('Existing Setup Mac app is not a bundle directory')
                destination.replace(backup)
            try:
                incoming.replace(destination)
            except BaseException:
                if previous and backup.exists():
                    backup.replace(destination)
                raise
            if previous:
                shutil.rmtree(backup)
        finally:
            if incoming.exists() and not incoming.is_symlink():
                shutil.rmtree(incoming)
