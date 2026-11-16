"""Reusable direct-distribution macOS drag-to-Applications DMG packaging.

Publish owns DMG creation and notarization. Setup owns verified extraction.
Neither Build, Stage nor Test calls this module. The OS/keychain own signing
credentials; never copy profiles or private keys into a release image.
"""
# CINTERFORGE SOURCE HEADER — host/mac/mac_dmg.py
# PURPOSE: Implements the macOS mac dmg adapter, packaging, signing or bootstrap boundary used by the native Terminal lifecycle.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   _require_mac: Handle require mac through this component's established interfaces.
#   _run: Run run for this module.
#   _sha: Handle sha through this component's established interfaces.
#   _validate_app: Validate validate app for this module.
#   mounted_dmg: Mount only inside Builds, always detach; do not touch global /Volumes.
#   verify_dmg: Verify actual mounted contents; for public DMGs also verify Apple ticket.
#   _notarize_dmg: Handle notarize dmg through this component's established interfaces.
#   create_dmg: Create app + Applications shortcut + README, optionally notarize DMG.
#   restore_dmg: Setup-only: safely restore an app from an already SHA-verified DMG.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
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


# The authored installer README is shared by the Mac DMG and Windows MSI.
# Do not regenerate or embed a stale alternate copy in the Python producer.
INSTALLER_ASSETS = Path(__file__).resolve().parents[2] / 'app/build/resources/installer'
INSTALL_README_PATH = INSTALLER_ASSETS / 'README.md'
DMG_BACKGROUND_PATH = INSTALLER_ASSETS / 'DMGBackground.png'
DMG_LAYOUT_SCRIPT_PATH = INSTALLER_ASSETS / 'mac_dmg.applescript'
INSTALL_README = INSTALL_README_PATH.read_text(encoding='utf-8')



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


def verify_dmg(dmg: Path, builds: Path, *, app_name: str, public: bool = False,
               expected_readme: bytes | None = None,
               expected_background: bytes | None = None) -> None:
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
        if expected_readme is not None and readme.read_bytes() != expected_readme:
            raise RuntimeError('Compressed DMG contains a stale or altered installer README')
        if expected_background is not None:
            background = volume / '.background/DMGBackground.png'
            if not background.is_file() or background.read_bytes() != expected_background:
                raise RuntimeError('Compressed DMG missing the authored background')
            finder_layout = volume / '.DS_Store'
            if not finder_layout.is_file() or finder_layout.stat().st_size == 0:
                raise RuntimeError('Compressed DMG is missing its Finder icon layout')
        if public:
            _run(['xcrun', 'stapler', 'validate', str(app)])
            _run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=4', str(app)])
    if public:
        _run(['xcrun', 'stapler', 'validate', str(dmg)])


def _notarize_dmg(dmg: Path, profile: str) -> None:
    import json
    if not profile:
        raise RuntimeError('Public macOS DMG requires the notarytool keychain profile')
    from host.mac.xcode_signing import login_keychain_environment
    result = subprocess.run(['xcrun', 'notarytool', 'submit', str(dmg),
                             '--keychain-profile', profile, '--wait',
                             '--output-format', 'json'],
                            capture_output=True, text=True, timeout=3600,
                            env=login_keychain_environment())
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError):
        response = {}
    if result.returncode or response.get('status') != 'Accepted':
        raise RuntimeError('Mac DMG notarization rejected: ' + (result.stderr or result.stdout)[-5000:])
    _run(['xcrun', 'stapler', 'staple', str(dmg)])


def _style_editable_dmg(editable: Path, root: Path, *,
                        volume_name: str, app_name: str,
                        expected_readme: bytes, expected_background: bytes) -> None:
    """Use Finder's own .DS_Store format; no new build dependency or VM."""
    mount = root / 'EditableMount'
    mount.mkdir()
    # The Finder layout is authored and reviewed as an installer resource.
    # Publish executes that file directly; it must not generate AppleScript.
    script = DMG_LAYOUT_SCRIPT_PATH
    if not script.is_file() or script.is_symlink():
        raise RuntimeError('DMG Publish requires checked-in installer/mac_dmg.applescript')
    attached = False
    try:
        # Unlike verification mounts, Publish's writable layout mount must be
        # visible in Finder: the user can watch the authored layout being applied.
        # Keep the mount in Builds and avoid an implicit extra Finder window;
        # the checked-in AppleScript opens the exact mounted path explicitly.
        print(f'[MAC DMG] Attaching writable Finder-visible volume: {mount}', flush=True)
        _run(['/usr/bin/hdiutil', 'attach', '-noautoopen',
              '-mountpoint', str(mount), str(editable)])
        attached = True
        if not (mount / app_name).is_dir() or not (mount / 'Applications').is_symlink():
            raise RuntimeError('Writable DMG was not mounted with the expected app and Applications shortcut')
        print('[MAC DMG] Finder will display and arrange the app, Applications, and README', flush=True)
        _run(['/usr/bin/osascript', str(script), volume_name, app_name, str(mount)])
        # A successful AppleScript exit alone does not prove that Finder saved
        # the presentation. Do not silently publish an unstyled image.
        metadata = mount / '.DS_Store'
        if not metadata.is_file() or metadata.stat().st_size == 0:
            raise RuntimeError('Finder did not write the DMG icon-view .DS_Store')
        background = mount / '.background/DMGBackground.png'
        readme = mount / 'README.md'
        if not background.is_file() or background.read_bytes() != expected_background:
            raise RuntimeError('DMG background differs from authored installer artwork')
        if not readme.is_file() or readme.read_bytes() != expected_readme:
            raise RuntimeError('DMG README differs from authored installer README.md')
    finally:
        if attached:
            _run(['/usr/bin/hdiutil', 'detach', str(mount)])


def create_dmg(app: Path, output: Path, builds: Path, *,
               volume_name: str, readme: str | None = None,
               public: bool = False, notary_profile: str = '') -> dict:
    """Publish-only: build a styled, verified, optionally notarized DMG."""
    _require_mac()
    _validate_app(app)
    if not output.is_relative_to(builds) or output.suffix.lower() != '.dmg':
        raise RuntimeError('Publish DMG must be generated under canonical Builds')
    if (not INSTALL_README_PATH.is_file() or INSTALL_README_PATH.is_symlink()
            or not DMG_BACKGROUND_PATH.is_file() or DMG_BACKGROUND_PATH.is_symlink()
            or not DMG_LAYOUT_SCRIPT_PATH.is_file() or DMG_LAYOUT_SCRIPT_PATH.is_symlink()):
        raise RuntimeError('DMG Publish requires installer/README.md, '
                           'installer/DMGBackground.png and installer/mac_dmg.applescript')
    # Use the real README from disk, not an inline generated template. Keep the
    # existing explicit readme override for callers that use it.
    readme_bytes = (INSTALL_README_PATH.read_bytes() if readme is None else
                    readme.encode('utf-8'))
    background_bytes = DMG_BACKGROUND_PATH.read_bytes()
    if not background_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        raise RuntimeError('Installer DMGBackground.png is not a PNG image')
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = builds / 'Intermediates/CinterForgeTerminal/DMG'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='Publish-', dir=scratch) as directory:
        root = Path(directory)
        content = root / 'Content'
        content.mkdir()
        shutil.copytree(app, content / app.name, symlinks=True)
        (content / 'Applications').symlink_to('/Applications', target_is_directory=True)
        (content / 'README.md').write_bytes(readme_bytes)
        background_dir = content / '.background'
        background_dir.mkdir()
        (background_dir / 'DMGBackground.png').write_bytes(background_bytes)

        editable = root / 'Editable.dmg'
        candidate = root / 'Package.dmg'
        print('[MAC DMG] Creating writable HFS+ layout source', flush=True)
        _run(['/usr/bin/hdiutil', 'create', '-ov', '-format', 'UDRW', '-fs', 'HFS+',
              '-volname', volume_name, '-srcfolder', str(content), str(editable)])
        _style_editable_dmg(editable, root, volume_name=volume_name,
                            app_name=app.name, expected_readme=readme_bytes,
                            expected_background=background_bytes)
        print('[MAC DMG] Compressing Finder-customized image', flush=True)
        _run(['/usr/bin/hdiutil', 'convert', str(editable), '-format', 'UDZO',
              '-o', str(candidate)])
        if public:
            _notarize_dmg(candidate, notary_profile)
        verify_dmg(candidate, builds, app_name=app.name, public=public,
                   expected_readme=readme_bytes, expected_background=background_bytes)
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
