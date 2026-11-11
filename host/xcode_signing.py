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


def _developer_id_identity(team: str) -> str:
    result = subprocess.run(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning'],
                            capture_output=True, text=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError('Cannot inspect installed macOS signing certificates: ' +
                           (result.stderr or result.stdout).strip())
    matches = []
    for line in result.stdout.splitlines():
        parsed = re.search(r'\"(Developer ID Application: [^\"]+)\"', line)
        if parsed and parsed.group(1).endswith(f'({team})'):
            matches.append(parsed.group(1))
    if not matches:
        raise RuntimeError('Xcode Release Team ' + team +
                           ' has no usable Developer ID Application certificate in Keychain')
    if len(matches) != 1:
        raise RuntimeError('Multiple Developer ID Application certificates for Team ' + team +
                           '; choose a unique signing identity before publishing')
    return matches[0]


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
    team = str(settings.get('DEVELOPMENT_TEAM', '')).strip()
    profile = str(settings.get('CINTER_MAC_NOTARY_PROFILE', '')).strip()
    if not team:
        if profile:
            raise RuntimeError('Xcode has a notary profile but no Development Team')
        return {}
    if not re.fullmatch(r'[A-Z0-9]{10}', team):
        raise RuntimeError('Xcode Release DEVELOPMENT_TEAM must be a 10-character Apple Team ID')
    kind = str(settings.get('CODE_SIGN_IDENTITY', '')).strip()
    if not kind.startswith('Developer ID Application'):
        raise RuntimeError('Xcode Release must select Developer ID Application for direct distribution')
    identity = _developer_id_identity(team)
    # The keychain item name is checked in; its secret/token is not.
    return {'CINTER_MAC_SIGN_IDENTITY': identity,
            'CINTER_MAC_NOTARY_PROFILE': profile}


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
