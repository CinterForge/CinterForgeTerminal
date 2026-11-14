"""Read macOS distribution signing choices from the existing Xcode project.

Xcode's Signing & Capabilities UI owns DEVELOPMENT_TEAM and the Release
certificate kind; secrets and private keys stay in the system Keychain.
Only the existing Cinter Build phase consumes these settings. No provisioning,
certificate generation, or notarization is done here.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
import re
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / 'CinterForgeTerminal.xcodeproj/project.pbxproj'
TARGET_NAME = 'CinterForge Terminal'
BUNDLE_ID = 'com.cinterforge.terminal'


def _project_release_settings() -> dict[str, str]:
    if not PROJECT.is_file():
        return {}
    # plutil reads Apple's OpenStep project format. Do not scrape regexes from
    # serialized project text or build the project just to inspect settings.
    result = subprocess.run(
        ['/usr/bin/plutil', '-convert', 'json', '-o', '-', str(PROJECT)],
        capture_output=True, text=True, check=False, timeout=20,
    )
    if result.returncode:
        raise RuntimeError('Cannot read Xcode signing project: ' +
                           (result.stderr or result.stdout).strip())
    data = json.loads(result.stdout)
    objects = data['objects']
    project = objects[data['rootObject']]
    target = next((item for item in project['targets']
                   if objects[item].get('name') == TARGET_NAME), None)
    if not target:
        raise RuntimeError('Xcode project does not contain the existing native Terminal target')

    def release_configuration(owner: dict) -> dict[str, str]:
        configurations = objects[owner['buildConfigurationList']]['buildConfigurations']
        for key in configurations:
            config = objects[key]
            if config.get('name') == 'Release':
                return config.get('buildSettings', {})
        raise RuntimeError('Xcode project is missing Release signing configuration')

    settings = release_configuration(project)
    settings.update(release_configuration(objects[target]))
    return settings


def _macos_release_setting(settings: dict[str, str], name: str) -> str:
    """Resolve the macOS SDK-specific Xcode setting before its generic value.

    The existing Xcode target uses a generic, empty DEVELOPMENT_TEAM and
    Apple Development identity, with the actual Release Developer ID/team in
    [sdk=macosx*] settings. Reading only the generic keys loses that choice.
    """
    for suffix in ('[sdk=macosx*]', '[sdk=macosx]'):
        key = name + suffix
        if key in settings:
            return str(settings[key]).strip()
    return str(settings.get(name, '')).strip()


def release_info_plist_bytes(settings: dict[str, str] | None = None) -> bytes:
    """Render the user-authored Xcode Info.plist; never maintain a second plist.

    Xcode resolves $(BUILD_SETTING) references when it builds the application.
    The standalone Cinter packager must do the same *without altering the
    authored file*. Resolve only values actually referenced by the template.
    """
    settings = settings if settings is not None else _project_release_settings()
    selected = _macos_release_setting(settings, 'INFOPLIST_FILE')
    if not selected:
        raise RuntimeError('Xcode Release INFOPLIST_FILE must select an authored plist')
    selected = selected.replace('$(PROJECT_DIR)', str(REPO))
    template = Path(selected) if Path(selected).is_absolute() else REPO / selected
    if not template.is_file() or template.is_symlink():
        raise FileNotFoundError('Authored Xcode Info.plist missing: ' + str(template))
    content = template.read_text(encoding='utf-8')
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = _macos_release_setting(settings, key)
        if not value or '$(' in value:
            raise RuntimeError('Unresolved Xcode Info.plist build setting: ' + key)
        return xml_escape(value, {'"': '&quot;', "'": '&apos;'})
    rendered = re.sub(r'\$\(([A-Za-z_][A-Za-z0-9_]*)\)', replace, content)
    info = plistlib.loads(rendered.encode('utf-8'))
    if (info.get('CFBundleIdentifier') != BUNDLE_ID or
            info.get('CFBundleExecutable') != 'CinterForgeTerminal'):
        raise RuntimeError('Authored Xcode Info.plist disagrees with Terminal executable/bundle ID')
    return rendered.encode('utf-8')


def release_sandbox_entitlements(settings: dict[str, str] | None = None) -> dict[str, bool]:
    """Use Xcode Signing & Capabilities as the single sandbox authority.

    Only the outer Terminal app gets these entitlements. Its nested VM has a
    separate, already-authored virtualization entitlement and stays distinct.
    """
    settings = settings if settings is not None else _project_release_settings()
    enabled = lambda key: _macos_release_setting(settings, key).upper() == 'YES'
    entitlements: dict[str, bool] = {}
    if enabled('ENABLE_APP_SANDBOX'):
        entitlements['com.apple.security.app-sandbox'] = True
    if enabled('ENABLE_INCOMING_NETWORK_CONNECTIONS'):
        entitlements['com.apple.security.network.server'] = True
    if enabled('ENABLE_OUTGOING_NETWORK_CONNECTIONS'):
        entitlements['com.apple.security.network.client'] = True
    for setting, entitlement_prefix in (
        ('ENABLE_USER_SELECTED_FILES', 'com.apple.security.files.user-selected.'),
        ('ENABLE_FILE_ACCESS_DOWNLOADS_FOLDER', 'com.apple.security.files.downloads.'),
        ('ENABLE_FILE_ACCESS_PICTURE_FOLDER', 'com.apple.security.assets.pictures.'),
    ):
        access = _macos_release_setting(settings, setting).lower()
        if access in ('readwrite', 'readonly'):
            suffix = 'read-write' if access == 'readwrite' else 'read-only'
            entitlements[entitlement_prefix + suffix] = True
    return entitlements


def _developer_id_identity(team: str) -> str:
    result = subprocess.run(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning'],
                            capture_output=True, text=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError('Cannot inspect installed macOS signing certificates: ' +
                           (result.stderr or result.stdout).strip())
    # Resolve the Xcode-selected Developer ID to its certificate SHA-1. Apple's
    # codesign accepts this fingerprint as --sign and avoids a second lookup
    # by a display name that may be duplicated across user keychains.
    # A duplicate listing of the *same* fingerprint is one identity.
    matches: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parsed = re.search(r'\b([0-9A-Fa-f]{40})\s+"(Developer ID Application: [^"]+)"', line)
        if parsed and parsed.group(2).endswith(f'({team})'):
            matches[parsed.group(1).upper()] = parsed.group(2)
    if not matches:
        raise RuntimeError('Xcode Release Team ' + team +
                           ' has no usable Developer ID Application certificate in Keychain')
    if len(matches) != 1:
        raise RuntimeError('Multiple distinct Developer ID Application certificates for Team ' + team +
                           '; choose the exact certificate in Xcode before publishing')
    return next(iter(matches))


def release_signing_environment() -> dict[str, str]:
    """Return inherited signing *references*, not credentials, for Build.

    No configured Team means the preexisting ad-hoc local build behavior.
    Explicit environment variables remain supported as the older CLI path.
    """
    if sys.platform != 'darwin':
        return {}
    settings = _project_release_settings()
    if not settings:
        return {}
    if str(settings.get('PRODUCT_BUNDLE_IDENTIFIER', '')).strip() != BUNDLE_ID:
        raise RuntimeError('Xcode Release bundle ID differs from packaged CinterForgeTerminal identity')
    team = _macos_release_setting(settings, 'DEVELOPMENT_TEAM')
    profile = str(settings.get('CINTER_MAC_NOTARY_PROFILE', '')).strip()
    if not team:
        if profile:
            raise RuntimeError('Xcode has a notary profile but no Development Team')
        return {}
    if not re.fullmatch(r'[A-Z0-9]{10}', team):
        raise RuntimeError('Xcode Release DEVELOPMENT_TEAM must be a 10-character Apple Team ID')
    kind = _macos_release_setting(settings, 'CODE_SIGN_IDENTITY')
    if not kind.startswith('Developer ID Application'):
        raise RuntimeError('Xcode Release must select Developer ID Application for direct distribution')
    identity = _developer_id_identity(team)
    # The keychain item name is checked in; its secret/token is not.
    return {'CINTER_MAC_SIGN_IDENTITY': identity,
            'CINTER_MAC_NOTARY_PROFILE': profile}



# This is a *reference* to credentials in the user's macOS Keychain, never
# a credential itself. Xcode's explicit Release setting takes precedence.
DEFAULT_NOTARY_PROFILE = 'CinterForgeTerminal'
NOTARY_PROFILE_RECEIPT = 'CinterForgeTerminal/Configuration/notary-profile.json'


def login_keychain_environment() -> dict[str, str]:
    """Run Apple's signing/notary tools as the login user, not Colima HOME."""
    result = dict(os.environ)
    if sys.platform == 'darwin':
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if not home.is_dir():
            raise RuntimeError('macOS login home is unavailable for notarytool Keychain access')
        result['HOME'] = str(home)
    return result


def _notary_history(profile: str) -> tuple[bool, str]:
    """Validate an actual saved notarytool profile with Apple's service."""
    try:
        result = subprocess.run(
            ['xcrun', 'notarytool', 'history', '--keychain-profile', profile,
             '--output-format', 'json'],
            capture_output=True, text=True, timeout=90, check=False,
            env=login_keychain_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'Cannot verify Apple notarization credentials: {error}') from error
    if result.returncode == 0:
        return True, ''
    return False, (result.stderr or result.stdout or 'notarytool exited nonzero').strip()[-1200:]


def _profile_receipt(builds: Path) -> Path:
    return builds.resolve() / NOTARY_PROFILE_RECEIPT


def _remember_notary_profile(builds: Path, profile: str) -> None:
    """Persist only a profile *name*, under Builds; never a password or key."""
    receipt = _profile_receipt(builds)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps({'profile': profile}, indent=2) + '\n', encoding='utf-8')
        temporary.replace(receipt)
    finally:
        temporary.unlink(missing_ok=True)


def _remembered_notary_profile(builds: Path) -> str:
    try:
        content = json.loads(_profile_receipt(builds).read_text(encoding='utf-8'))
        value = content.get('profile', '')
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return ''


def _missing_notary_item(error: str) -> bool:
    lowered = error.lower()
    return any(phrase in lowered for phrase in (
        'no keychain password item found', 'keychain item not found',
        'profile not found', 'not found in keychain',
    ))


def ensure_public_notary_profile(builds: Path) -> str:
    """Resolve Xcode's notary identity or enroll it once inside Cinter Publish.

    The Developer ID certificate used for codesign is NOT a notarization
    credential. Beta/Release must be able to authenticate to Apple's notary
    service before expensive MSI packaging or any public CDN upload.
    Existing profiles are reused. On an interactive Mac Bash session, a
    missing profile may be enrolled via Apple's own password-masked prompt.
    No credentials, passwords or private keys are written to Builds/logs.
    """
    if sys.platform != 'darwin':
        raise RuntimeError('Apple notarization profile setup requires macOS')
    settings = _project_release_settings()
    team = _macos_release_setting(settings, 'DEVELOPMENT_TEAM')
    if not re.fullmatch(r'[A-Z0-9]{10}', team):
        raise RuntimeError('Xcode Release needs a valid macOS DEVELOPMENT_TEAM for notarization')
    from_xcode = str(settings.get('CINTER_MAC_NOTARY_PROFILE', '')).strip()
    from_env = os.environ.get('CINTER_MAC_NOTARY_PROFILE', '').strip()
    chosen = from_env or from_xcode or _remembered_notary_profile(builds) or DEFAULT_NOTARY_PROFILE
    if any(char.isspace() for char in chosen) and ('\n' in chosen or '\r' in chosen):
        raise RuntimeError('Invalid multiline notarytool profile name')

    ok, issue = _notary_history(chosen)
    if ok:
        os.environ['CINTER_MAC_NOTARY_PROFILE'] = chosen
        _remember_notary_profile(builds, chosen)
        print(f'[MAC NOTARY] Existing Apple notarytool profile verified: {chosen}', flush=True)
        return chosen
    if not _missing_notary_item(issue):
        # Network/authentication failures are NOT missing credentials. Never
        # overwrite a profile just because Apple's service is unavailable.
        raise RuntimeError(f'Apple notarytool profile {chosen!r} could not be verified: {issue}')
    if not sys.stdin.isatty():
        raise RuntimeError(f'Apple notarytool profile {chosen!r} is missing from the login Keychain. '
                           'Run public Publish interactively in CinterForge Mac Bash to '
                           'enroll notarization credentials once; no build or upload occurred.')

    print('[MAC NOTARY] The Developer ID signing certificate is ready, but Apple '
          'notarization requires a separate Keychain credential.', flush=True)
    if not from_xcode and not from_env:
        existing = input('[MAC NOTARY] Existing notarytool profile name '
                         f'(Enter to create {DEFAULT_NOTARY_PROFILE}): ').strip()
        if existing:
            if '\n' in existing or '\r' in existing:
                raise RuntimeError('Invalid multiline notarytool profile name')
            ok, issue = _notary_history(existing)
            if ok:
                os.environ['CINTER_MAC_NOTARY_PROFILE'] = existing
                _remember_notary_profile(builds, existing)
                print(f'[MAC NOTARY] Existing Apple notarytool profile verified: {existing}', flush=True)
                return existing
            if not _missing_notary_item(issue):
                raise RuntimeError(f'Apple notarytool profile {existing!r} cannot authenticate: {issue}')
            chosen = existing

    apple_id = input(f'[MAC NOTARY] Apple Account email for Team {team}: ').strip()
    if not apple_id or '\n' in apple_id or '\r' in apple_id:
        raise RuntimeError('Apple Account email required to set up Keychain notarization')
    print('[MAC NOTARY] Apple notarytool will prompt securely for an app-specific '
          'password. CinterForge does not read or log that password.', flush=True)
    # Do NOT pass --password: notarytool obtains it directly from stdin/TTY,
    # avoiding argv, process listings, logs and Cinter's source tree.
    result = subprocess.run(
        ['xcrun', 'notarytool', 'store-credentials', chosen,
         '--apple-id', apple_id, '--team-id', team],
        env=login_keychain_environment(), check=False,
    )
    if result.returncode:
        raise RuntimeError('Apple notarytool did not save/validate a Keychain profile; '
                           'Publish remains blocked (no CDN upload).')
    ok, issue = _notary_history(chosen)
    if not ok:
        raise RuntimeError(f'Apple notarytool profile {chosen!r} could not be verified '
                           f'after enrollment: {issue}')
    os.environ['CINTER_MAC_NOTARY_PROFILE'] = chosen
    _remember_notary_profile(builds, chosen)
    print(f'[MAC NOTARY] Saved and validated Keychain profile: {chosen}', flush=True)
    return chosen

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--field', choices=('identity', 'profile'), required=True)
    args = parser.parse_args()
    env = release_signing_environment()
    print(env.get('CINTER_MAC_SIGN_IDENTITY' if args.field == 'identity'
                  else 'CINTER_MAC_NOTARY_PROFILE', ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
