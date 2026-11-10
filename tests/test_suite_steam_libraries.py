"""Steam library discovery regressions: select installed app, not default drive."""
import os
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from host import universal_package as release
from host.universal_package import steamapps_root

SCRATCH = Path(__file__).resolve().parents[2] / 'Builds/Intermediates/CinterForgeTerminalTests'


class SteamLibraryDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='steam-libraries-', dir=SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.login = self.root / 'login'
        self.client = self.login / 'Library/Application Support/Steam'
        self.default = self.client / 'steamapps'
        self.secondary_library = self.root / 'External Drive/SteamLibrary'
        self.external = self.secondary_library / 'steamapps'
        self.default.mkdir(parents=True)
        self.external.mkdir(parents=True)
        self.mock_home = mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(self.login)))
        self.mock_platform = mock.patch('platform.system', return_value='Darwin')
        self.mock_environment = mock.patch.dict(os.environ, {'HOME': str(self.root / 'Builds/CFTVM/home')}, clear=True)
        self.mock_home.start()
        self.addCleanup(self.mock_home.stop)
        self.mock_platform.start()
        self.addCleanup(self.mock_platform.stop)
        self.mock_environment.start()
        self.addCleanup(self.mock_environment.stop)

    def register_secondary(self, config_at='config'):
        config = self.client / config_at / 'libraryfolders.vdf'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('"libraryfolders"\n{\n "0"\n { "path" "not-this-one" }\n "1"\n {\n "path" "' + str(self.secondary_library) + '"\n "apps" { "1234" "123" }\n }\n}\n')
        return config

    def register_install(self, location):
        (location / 'common/CinterForge').mkdir(parents=True, exist_ok=True)
        (location / 'appmanifest_1234.acf').write_text('"AppState"\n{\n"appid" "1234"\n"installdir" "CinterForge"\n}')

    def test_external_library_appmanifest_beats_default(self):
        self.register_secondary()
        self.register_install(self.external)
        self.assertEqual(steamapps_root(), self.external.resolve())

    def test_existing_cinterforge_directory_beats_empty_default(self):
        self.register_secondary(config_at='steamapps')
        (self.external / 'common/CinterForge').mkdir(parents=True)
        self.assertEqual(steamapps_root(), self.external.resolve())

    def test_appmanifest_beats_stale_default_stage(self):
        self.register_secondary()
        # An earlier defective Stage left a CinterForge folder in the wrong
        # library; Steam's actual appmanifest is authoritative.
        (self.default / 'common/CinterForge/Applications').mkdir(parents=True)
        self.register_install(self.external)
        self.assertEqual(steamapps_root(), self.external.resolve())

    def test_actual_installed_steam_games_beat_old_default_stage(self):
        self.register_secondary()
        (self.default / 'common/CinterForge/Applications').mkdir(parents=True)
        (self.external / 'appmanifest_5678.acf').write_text(
            '"AppState"\n{\n"appid" "5678"\n"name" "Other Game"\n"installdir" "Other Game"\n}')
        self.assertEqual(steamapps_root(), self.external.resolve())

    def test_cinterforge_steam_name_matches_different_installdir(self):
        self.register_secondary()
        (self.external / 'appmanifest_5678.acf').write_text(
            '"AppState"\n{\n"appid" "5678"\n"name" "CinterForge Terminal"\n"installdir" "Creative Suite"\n}')
        self.assertEqual(steamapps_root(), self.external.resolve())

    def test_ambiguous_installations_refuse_to_guess(self):
        self.register_secondary()
        self.register_install(self.default)
        self.register_install(self.external)
        with self.assertRaisesRegex(RuntimeError, 'Multiple Steam libraries'):
            steamapps_root()

    def test_no_target_in_multiple_libraries_refuses_default(self):
        self.register_secondary()
        with self.assertRaisesRegex(RuntimeError, 'Cannot uniquely locate'):
            steamapps_root()

    def test_stage_linux_uses_discovered_external_steam_library(self):
        self.register_secondary()
        self.register_install(self.external)
        builds = self.root / 'Builds'
        output = builds / 'CinterForgeTerminal/Distribution'
        output.mkdir(parents=True)
        fixture_release = '2026.11.fixture'
        zip_path = output / 'CinterForgeTerminal-linux.zip'
        entry = release._write_release_zip(zip_path,
                 {'CinterForge Terminal/readme-user.txt': b'Linux release fixture'},
                 target='linux', release=fixture_release, root='CinterForge Terminal')
        artifacts = {name: dict(entry, target=name) for name in release.FOUR_TARGETS}
        artifacts['linux'] = entry
        (output / 'four-artifacts.json').write_text(json.dumps({
            'schema': release.FOUR_SCHEMA, 'release': fixture_release,
            'artifacts': artifacts}))
        release.stage_four(builds, release=fixture_release, targets=('linux',))
        installed = release._steam_release_path(self.external, 'linux', fixture_release)
        self.assertTrue((installed / 'CinterForge Terminal/readme-user.txt').is_file())
        self.assertFalse((self.default / 'common/CinterForge').exists())

    def test_unique_default_fallback_preserves_first_install(self):
        self.assertEqual(steamapps_root(), self.default.resolve())

    def test_explicit_override_preserved(self):
        self.register_secondary()
        self.register_install(self.external)
        self.assertEqual(steamapps_root(explicit=self.default), self.default.resolve())

    def test_readonly_discovery_does_not_create_install_folder(self):
        self.register_secondary()
        before = {str(p) for p in self.root.rglob('*')}
        with self.assertRaisesRegex(RuntimeError, 'Cannot uniquely locate'):
            steamapps_root()
        self.assertEqual(before, {str(p) for p in self.root.rglob('*')})


if __name__ == '__main__':
    unittest.main()
