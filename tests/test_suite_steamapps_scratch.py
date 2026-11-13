"""Regression checks: Steam holds apps and player data, never VM/download scratch."""
import shutil
from contextlib import contextmanager
from tests._support import *


@contextmanager
def _short_builds_home():
    """A unique, Builds-owned mock home short enough for macOS Lima sockets.

    Regular tempfile names under Builds/CFTVM/tmp exceed the real UNIX-socket
    limit when the mocked home adds Library/Caches/CFTVM/T. Avoid bypassing
    production's safety check or writing a fixture outside sibling Builds.
    """
    base = BOOTSTRAP_BUNDLE_BUILDS.resolve()
    base.mkdir(parents=True, exist_ok=True)
    for attempt in range(256):
        home = base / ('h' + format(attempt, '02x'))
        try:
            home.mkdir()
        except FileExistsError:
            continue
        try:
            yield home
        finally:
            shutil.rmtree(home)
        return
    raise RuntimeError('No short temporary home available under Builds')
from webapp import CinterForgeTerminal as terminal


class SteamScratchIsolationTests(unittest.TestCase):
    def setUp(self):
        # Keep test-generated files inside sibling Builds, as in the lifecycle.
        base = BOOTSTRAP_BUNDLE_BUILDS
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='s-', dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.steam = self.root / 'steamapps'
        self.steam.mkdir()
        self.state = self.steam / 'common/CinterForge/Applications/TerminalState'
        self.state.mkdir(parents=True)

    def test_installed_shell_tmp_scripts_cache_outside_steam(self):
        with (mock.patch.object(terminal, '_real_login_home', return_value=self.root),
              mock.patch.object(terminal.platform, 'system', return_value='Darwin')):
            env = terminal._session_environment(self.state, 'mac')
        steam = self.steam.resolve()
        for key in ('TMPDIR', 'TMP', 'TEMP', 'XDG_CACHE_HOME', 'CINTER_TERMINAL_SCRATCH_ROOT'):
            self.assertFalse(Path(env[key]).is_relative_to(steam), key)
            self.assertTrue(Path(env[key]).is_relative_to(self.root / 'Library/Caches'))
        # Mac Bash is the real host user's shell: Keychain and ./Cinter Publish
        # must see the account HOME, not a managed scratch HOME.
        self.assertEqual(Path(env['HOME']), self.root)
        # Persistent user shell data and logs are still Steam-backed.
        self.assertTrue(Path(env['HISTFILE']).is_relative_to(steam))
        self.assertTrue(Path(env['CINTER_TERMINAL_DIRECTORY_LOG']).is_relative_to(steam))
        self.assertEqual(env['CINTER_TERMINAL_RUNTIME_MODE'], 'installed')

    def test_devtools_multi_gb_partial_download_is_not_in_steam(self):
        with (mock.patch.object(terminal, '_real_login_home', return_value=self.root),
              mock.patch.object(terminal.platform, 'system', return_value='Darwin')):
            partial = terminal._devtools_download_partial('2026.11.23', 'CinterForgeTerminal-devtools.zip')
        self.assertFalse(partial.is_relative_to(self.steam.resolve()))
        self.assertIn('Downloads', partial.parts)
        self.assertTrue(partial.is_relative_to(self.root / 'Library/Caches'))

    def test_vm_state_stays_outside_steam(self):
        # Verify the *real* socket-path guard using a short, Builds-owned home.
        with _short_builds_home() as home:
            with (mock.patch.dict(os.environ, {
                      'CINTER_TERMINAL_RUNTIME_MODE': 'installed',
                      'CINTER_STEAMAPPS': str(self.steam)}, clear=False),
                  mock.patch.object(terminal, '_real_login_home', return_value=home),
                  mock.patch.object(terminal.platform, 'system', return_value='Darwin')):
                state = terminal._state_root()
                self.assertTrue(state.is_relative_to(home / 'Library/Caches'))
                self.assertFalse(state.is_relative_to(self.steam.resolve()))

    def test_cache_cannot_redirect_through_steam_symlink(self):
        with tempfile.TemporaryDirectory(prefix='H', dir=BOOTSTRAP_BUNDLE_BUILDS) as home_dir:
            home = Path(home_dir)
            (home / 'Library').mkdir()
            (home / 'Library/Caches').symlink_to(self.steam, target_is_directory=True)
            with mock.patch.object(terminal.platform, 'system', return_value='Darwin'):
                with self.assertRaisesRegex(RuntimeError, 'steamapps'):
                    terminal._installed_cache_root(home)

    def test_developer_scratch_remains_in_builds(self):
        with (mock.patch.object(terminal, '_real_login_home', return_value=self.root),
              mock.patch.object(terminal.platform, 'system', return_value='Darwin')):
            env = terminal._session_environment(BOOTSTRAP_BUNDLE_BUILDS, 'mac')
        self.assertTrue(Path(env['TMPDIR']).is_relative_to(BOOTSTRAP_BUNDLE_BUILDS))
        self.assertTrue(Path(env['XDG_CACHE_HOME']).is_relative_to(BOOTSTRAP_BUNDLE_BUILDS))
        self.assertEqual(Path(env['HOME']), self.root)
