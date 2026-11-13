"""Xcode Release signing is canonical for CLI and integrated Mac Bash Publish."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from host import xcode_signing as signing
from host import mac_app
from linuxos import launcher_signing


TEAM = 'TAN2T5524R'
NAME = f'Developer ID Application: CinterForge Inc. ({TEAM})'
SHA = '044C6D268490638165FCF56C2CA84888F5A8ADEA'


class XcodeSigningTests(unittest.TestCase):
    def test_xcode_project_selects_macos_specific_release_signer(self):
        # Assert against the real checked-in Xcode settings, not a second
        # configuration invented by this test.
        project = signing.PROJECT.read_text(encoding='utf-8')
        self.assertIn('"CODE_SIGN_IDENTITY[sdk=macosx*]" = "Developer ID Application";', project)
        self.assertIn(f'"DEVELOPMENT_TEAM[sdk=macosx*]" = {TEAM};', project)
        generic_and_qualified = {
            'PRODUCT_BUNDLE_IDENTIFIER': signing.BUNDLE_ID,
            'CODE_SIGN_IDENTITY': 'Apple Development',
            'CODE_SIGN_IDENTITY[sdk=macosx*]': 'Developer ID Application',
            'DEVELOPMENT_TEAM': '',
            'DEVELOPMENT_TEAM[sdk=macosx*]': TEAM,
            'CINTER_MAC_NOTARY_PROFILE': '',
        }
        output = '\n'.join((f'  6) {SHA} "{NAME}"', f'  7) {SHA} "{NAME}"', '     7 valid identities found'))
        with (mock.patch.object(signing.sys, 'platform', 'darwin'),
              mock.patch.object(signing, '_project_release_settings', return_value=generic_and_qualified),
              mock.patch.object(signing.subprocess, 'run', return_value=subprocess.CompletedProcess(
                  args=[], returncode=0, stdout=output, stderr='')) as run):
            values = signing.release_signing_environment()
        self.assertEqual(values['CINTER_MAC_SIGN_IDENTITY'], SHA)
        self.assertEqual(values['CINTER_MAC_NOTARY_PROFILE'], '')
        self.assertEqual(run.call_args.args[0][:2], ['/usr/bin/security', 'find-identity'])

    def test_sdk_specific_empty_value_does_not_fall_back_to_generic(self):
        settings = {'DEVELOPMENT_TEAM': 'WRONGTEAM0', 'DEVELOPMENT_TEAM[sdk=macosx*]': ''}
        self.assertEqual(signing._macos_release_setting(settings, 'DEVELOPMENT_TEAM'), '')

    def test_duplicate_keychain_rows_with_same_hash_are_one_identity(self):
        listing = f'  6) {SHA} "{NAME}"\n  7) {SHA} "{NAME}"\n'
        with mock.patch.object(signing.subprocess, 'run', return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=listing, stderr='')):
            self.assertEqual(signing._developer_id_identity(TEAM), SHA)

    def test_mac_app_seals_vm_with_exact_xcode_release_sha(self):
        # The real Mac failure was `codesign ... --sign "Developer ID ..."`
        # returning "no identity found" despite the valid SHA in Keychain.
        # Assert that both nested VM and outer app receive that exact SHA.
        with tempfile.TemporaryDirectory() as scratch:
            app = Path(scratch) / 'CinterForge Terminal.app'
            vm = (app / 'Contents/Resources/Builds/CinterForgeTerminal/Dependencies/'
                  'LinuxKit/mac/arm64/CinterBDSVM')
            vm.parent.mkdir(parents=True)
            vm.write_bytes(b'fixture')
            calls = []
            def fake_sign(binary, *, identity='-'):
                calls.append(('vm', identity))
            def fake_run(command, **kwargs):
                calls.append(('codesign', list(command)))
                return subprocess.CompletedProcess(command, 0, '', '')
            with (mock.patch.dict(mac_app.os.environ, {'CINTER_MAC_SIGN_IDENTITY': SHA}),
                  mock.patch.object(mac_app.os, 'uname', return_value=SimpleNamespace(sysname='Darwin')),
                  mock.patch.object(launcher_signing, 'sign_launcher', side_effect=fake_sign),
                  mock.patch.object(launcher_signing, 'has_virtualization_entitlement', return_value=True),
                  mock.patch.object(mac_app, '_verify_signature'),
                  mock.patch.object(mac_app.subprocess, 'run', side_effect=fake_run)):
                mac_app._sign_and_verify(app)
            self.assertIn(('vm', SHA), calls)
            signing_calls = [command for kind, command in calls if kind == 'codesign']
            self.assertEqual(len(signing_calls), 1)
            self.assertEqual(signing_calls[0][signing_calls[0].index('--sign') + 1], SHA)

    def test_codesign_uses_real_login_home_not_colima_managed_home(self):
        # `Cinter build all` replaces HOME for the Colima/Lima process family.
        # The Xcode identity is resolved before that replacement, while the VM
        # launcher's codesign subprocess runs after it. Both signers must get
        # the original login user's Keychain context, without moving VM data.
        import os
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            login_home = root / 'login'
            managed_home = root / 'managed-colima-home'
            login_home.mkdir()
            managed_home.mkdir()
            app = root / 'CinterForge Terminal.app'
            vm = (app / 'Contents/Resources/Builds/CinterForgeTerminal/Dependencies/'
                  'LinuxKit/mac/arm64/CinterBDSVM')
            vm.parent.mkdir(parents=True)
            vm.write_bytes(b'fixture')
            seen = []

            def fake_sign(binary, *, identity='-'):
                seen.append(('vm', os.environ['HOME'], identity))

            def fake_codesign(command, **kwargs):
                seen.append(('app', os.environ['HOME'], command[command.index('--sign') + 1]))
                return subprocess.CompletedProcess(command, 0, '', '')

            with (mock.patch.dict(os.environ, {
                    'HOME': str(managed_home), 'CINTER_MAC_SIGN_IDENTITY': SHA}),
                  mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(login_home))),
                  mock.patch.object(mac_app.os, 'uname', return_value=SimpleNamespace(sysname='Darwin')),
                  mock.patch.object(launcher_signing, 'sign_launcher', side_effect=fake_sign),
                  mock.patch.object(launcher_signing, 'has_virtualization_entitlement', return_value=True),
                  mock.patch.object(mac_app, '_verify_signature'),
                  mock.patch.object(mac_app.subprocess, 'run', side_effect=fake_codesign)):
                mac_app._sign_and_verify(app)
                self.assertEqual(os.environ['HOME'], str(managed_home))
            self.assertEqual(seen, [('vm', str(login_home), SHA),
                                    ('app', str(login_home), SHA)])

    def test_codesign_failure_restores_colima_home(self):
        import os
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            login_home = root / 'login'
            login_home.mkdir()
            original_home = str(root / 'managed-colima-home')
            with (mock.patch.dict(os.environ, {'HOME': original_home}),
                  mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(login_home))),
                  mock.patch.object(mac_app.os, 'uname', return_value=SimpleNamespace(sysname='Darwin')),
                  mock.patch.object(mac_app, '_sign_and_verify_with_login_keychain',
                                    side_effect=RuntimeError('codesign failed')) as sign):
                with self.assertRaisesRegex(RuntimeError, 'codesign failed'):
                    mac_app._sign_and_verify(root / 'CinterForge Terminal.app')
                self.assertEqual(os.environ['HOME'], original_home)
                sign.assert_called_once()

    def test_distinct_certificates_for_same_team_are_not_silently_chosen(self):
        listing = (f'  1) {SHA} "{NAME}"\n' +
                   f'  2) {"A"*40} "{NAME}"\n')
        with mock.patch.object(signing.subprocess, 'run', return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=listing, stderr='')):
            with self.assertRaisesRegex(RuntimeError, 'Multiple distinct'):
                signing._developer_id_identity(TEAM)

    def test_plutil_project_reader_merges_release_target_over_project(self):
        project_id, target_id, project_list, target_list = 'PROJECT', 'TARGET', 'PROJECTCONFIGS', 'TARGETCONFIGS'
        doc = {'rootObject': project_id, 'objects': {
            project_id: {'targets': [target_id], 'buildConfigurationList': project_list},
            target_id: {'name': signing.TARGET_NAME, 'buildConfigurationList': target_list},
            project_list: {'buildConfigurations': ['PROJRELEASE']},
            target_list: {'buildConfigurations': ['TARGETRELEASE']},
            'PROJRELEASE': {'name': 'Release', 'buildSettings': {'DEVELOPMENT_TEAM': 'WRONGTEAM0'}},
            'TARGETRELEASE': {'name': 'Release', 'buildSettings': {
                'DEVELOPMENT_TEAM': '', 'DEVELOPMENT_TEAM[sdk=macosx*]': TEAM}}}}
        with mock.patch.object(signing.subprocess, 'run', return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(doc), stderr='')):
            settings = signing._project_release_settings()
        self.assertEqual(settings['DEVELOPMENT_TEAM'], '')
        self.assertEqual(signing._macos_release_setting(settings, 'DEVELOPMENT_TEAM'), TEAM)


class NotaryProfileTests(unittest.TestCase):
    def settings(self, profile=''):
        return {'DEVELOPMENT_TEAM[sdk=macosx*]': TEAM,
                'CINTER_MAC_NOTARY_PROFILE': profile}

    def test_public_notary_reuses_xcode_profile_and_only_stores_its_name(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            builds = Path(folder) / 'Builds'
            with (mock.patch.object(signing.sys, 'platform', 'darwin'),
                  mock.patch.object(signing, '_project_release_settings', return_value=self.settings('xcode-profile')),
                  mock.patch.object(signing, '_notary_history', return_value=(True, '')) as history,
                  mock.patch.dict(os.environ, {'CINTER_MAC_NOTARY_PROFILE': ''}),
                  mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=folder)),
                  mock.patch('builtins.input', side_effect=AssertionError('no interactive setup expected'))):
                self.assertEqual(signing.ensure_public_notary_profile(builds), 'xcode-profile')
                self.assertEqual(os.environ['CINTER_MAC_NOTARY_PROFILE'], 'xcode-profile')
            history.assert_called_once_with('xcode-profile')
            saved = json.loads((builds / signing.NOTARY_PROFILE_RECEIPT).read_text())
            self.assertEqual(saved, {'profile': 'xcode-profile'})
            self.assertNotIn('password', str(saved).lower())

    def test_public_notary_enrollment_uses_masked_apple_prompt_not_argv_password(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            builds = Path(folder) / 'Builds'
            home = Path(folder) / 'login'
            home.mkdir()
            managed = Path(folder) / 'colima-home'
            seen = []

            def store(argv, **kwargs):
                seen.append((argv, kwargs['env']['HOME']))
                return subprocess.CompletedProcess(argv, 0, '', '')

            with (mock.patch.object(signing.sys, 'platform', 'darwin'),
                  mock.patch.object(signing.sys.stdin, 'isatty', return_value=True),
                  mock.patch.object(signing, '_project_release_settings', return_value=self.settings()),
                  mock.patch.object(signing, '_notary_history', side_effect=[
                      (False, 'No Keychain password item found for profile'), (True, '')]),
                  mock.patch.dict(os.environ, {'HOME': str(managed), 'CINTER_MAC_NOTARY_PROFILE': ''}),
                  mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(home))),
                  mock.patch('builtins.input', side_effect=['', 'developer@example.com']),
                  mock.patch.object(signing.subprocess, 'run', side_effect=store)):
                self.assertEqual(signing.ensure_public_notary_profile(builds), 'CinterForgeTerminal')
                self.assertEqual(os.environ['HOME'], str(managed))
            self.assertEqual(len(seen), 1)
            argv, environment_home = seen[0]
            self.assertEqual(argv[:4], ['xcrun', 'notarytool', 'store-credentials', 'CinterForgeTerminal'])
            self.assertEqual(argv[-2:], ['--team-id', TEAM])
            self.assertEqual(environment_home, str(home))
            self.assertNotIn('--password', argv)
            self.assertEqual(json.loads((builds / signing.NOTARY_PROFILE_RECEIPT).read_text()),
                             {'profile': 'CinterForgeTerminal'})

    def test_noninteractive_missing_notary_refuses_before_any_enrollment(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            builds = Path(folder) / 'Builds'
            with (mock.patch.object(signing.sys, 'platform', 'darwin'),
                  mock.patch.object(signing.sys.stdin, 'isatty', return_value=False),
                  mock.patch.object(signing, '_project_release_settings', return_value=self.settings()),
                  mock.patch.object(signing, '_notary_history', return_value=(False, 'No Keychain password item found')),
                  mock.patch.dict(os.environ, {'CINTER_MAC_NOTARY_PROFILE': ''}),
                  mock.patch.object(signing.subprocess, 'run') as subprocess_run):
                with self.assertRaisesRegex(RuntimeError, 'interactively'):
                    signing.ensure_public_notary_profile(builds)
            subprocess_run.assert_not_called()
            self.assertFalse((builds / signing.NOTARY_PROFILE_RECEIPT).exists())

    def test_auth_or_network_failure_never_overwrites_existing_notary_profile(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            builds = Path(folder) / 'Builds'
            with (mock.patch.object(signing.sys, 'platform', 'darwin'),
                  mock.patch.object(signing, '_project_release_settings', return_value=self.settings('xcode-profile')),
                  mock.patch.object(signing, '_notary_history', return_value=(False, 'HTTP 503 unavailable')),
                  mock.patch.dict(os.environ, {'CINTER_MAC_NOTARY_PROFILE': ''}),
                  mock.patch('builtins.input', side_effect=AssertionError('never prompt on network failure')),
                  mock.patch.object(signing.subprocess, 'run') as subprocess_run):
                with self.assertRaisesRegex(RuntimeError, 'HTTP 503'):
                    signing.ensure_public_notary_profile(builds)
            subprocess_run.assert_not_called()

    def test_notarytool_uses_login_home_after_colima_changed_home(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            login = Path(folder) / 'login'
            login.mkdir()
            with (mock.patch.object(signing.sys, 'platform', 'darwin'),
                  mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(login))),
                  mock.patch.dict(os.environ, {'HOME': str(Path(folder) / 'vm-home')})):
                result = signing.login_keychain_environment()
                self.assertEqual(result['HOME'], str(login))
                self.assertNotEqual(result['HOME'], os.environ['HOME'])


if __name__ == '__main__':
    unittest.main()
