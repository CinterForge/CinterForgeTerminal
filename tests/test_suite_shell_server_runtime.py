"""Regression for the reported nonfunctional native shell and Server tab logging.

Only directly relevant checks: actual authenticated shell-open PTY, persistence,
Stream API, installed Steam logs, and developer Builds separation.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

from webapp import CinterForgeTerminal as terminal


class ShellServerRuntimeTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / 'Builds' / 'Intermediates' / 'CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='server-runtime-', dir=root)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def _request(self, server, path, value=None):
        raw = json.dumps(value).encode() if value is not None else None
        request = Request(server.origin + path, data=raw, headers={
            'X-CPH-Token': server.token,
            **({'Content-Type': 'application/json'} if value is not None else {})},
            method='POST' if value is not None else 'GET')
        with urlopen(request, timeout=6) as response:
            return json.loads(response.read())

    def test_packaged_steam_shell_initializes_and_persists_error_log_in_steam(self):
        steam = self.workspace / 'external-library' / 'steamapps'
        embedded = (steam / 'common/CinterForge/Applications/macOS/CinterForge Terminal/fixture/'
                    'CinterForge Terminal.app/Contents/Resources/Builds')
        embedded.mkdir(parents=True)
        # Mirror the *actual* packaged host layout: release is ASSETS.parent,
        # not ASSETS.parents[1] (which would incorrectly return arm64).
        packaged = embedded / 'Python/Host/mac/arm64/2026.11.23/webapp'
        packaged.mkdir(parents=True)
        source = Path(terminal.__file__).parent
        for name in ('CinterForgeTerminal.py', 'if_titles.json', 'mac_games.json'):
            (packaged/name).write_bytes((source/name).read_bytes())
        before = sorted(embedded.rglob('*'))
        assets_patch = mock.patch.object(terminal, 'ASSETS', packaged)
        assets_patch.start()
        self.addCleanup(assets_patch.stop)
        with mock.patch.object(terminal, '_load_bedrock_manager', side_effect=RuntimeError('no bedrock needed')):
            server = terminal.CinterShellServer(embedded)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            created = self._request(server, '/api/terminal/open', {
                'backend':'native', 'profile':'apple-ii', 'cols':80, 'rows':25})
            self.assertTrue(created['id'])
            self.assertEqual(terminal._session_environment(server.mutable_builds, server.system)
                             ['CINTER_TERMINAL_RELEASE'], '2026.11.23')
            chunks = []
            for _ in range(20):
                polled = self._request(server, '/api/terminal/poll', {'id': created['id'], 'wait_ms': 150})
                chunks.append(__import__('base64').b64decode(polled['output']))
                if b'Welcome to CinterForge.' in b''.join(chunks):
                    break
            self.assertIn(b'Welcome to CinterForge.', b''.join(chunks))
            self.assertTrue(server.log.path.is_file())
            self.assertTrue(server.log.path.is_relative_to(steam / 'common/CinterForge/Logs'))
            self.assertEqual(sorted(embedded.rglob('*')), before)
            stream = self._request(server, '/api/server/log?cursor=0')
            self.assertTrue(stream['read_only'])
            self.assertTrue(any('terminal opened:' in line for line in stream['lines']))
            self.assertEqual(stream['log_path'], str(server.log.path))
            # Reproduce the old packaged-import failure and assert the HTTP
            # response is usable JSON, rather than WebKit's opaque Load failed.
            from urllib.error import HTTPError
            with mock.patch.object(terminal, '_install_shell_commands',
                                   side_effect=ModuleNotFoundError("No module named 'host'")):
                with self.assertRaises(HTTPError) as captured:
                    self._request(server, '/api/terminal/open',
                                  {'backend':'native', 'profile':'modern'})
            self.assertEqual(captured.exception.code, 500)
            self.assertIn('Server log', json.loads(captured.exception.read())['error'])
            self.assertIn('ModuleNotFoundError', server.log.path.read_text())
        finally:
            server.stop()
            worker.join(timeout=3)

    def test_downloaded_app_outside_steam_discovers_real_external_library(self):
        steam_default = self.workspace / 'Home/Library/Application Support/Steam/steamapps'
        external = self.workspace / 'External Steam/steamapps'
        source = self.workspace / 'Downloads/CinterForge Terminal.app/Contents/Resources/Builds'
        source.mkdir(parents=True)
        steam_default.mkdir(parents=True)
        external.mkdir(parents=True)
        (external / 'appmanifest_7.acf').write_text('"AppState" { "name" "CinterForge Terminal" }')
        with mock.patch.object(terminal, '_runtime_steam_locations', return_value=[steam_default, external]):
            chosen = terminal._terminal_mutable_root(source)
            self.assertEqual(chosen, external / 'common/CinterForge/Applications/TerminalState')
            self.assertTrue(chosen.is_dir())
            self.assertEqual(terminal._user_steamapps(source), external)
        self.assertEqual([p for p in source.rglob('*')], [])
        self.assertFalse((steam_default / 'common/CinterForge').exists())

    def test_ambiguous_downloaded_app_does_not_guess_default_steam_library(self):
        source = self.workspace / 'Downloads/CinterForge Terminal.app/Contents/Resources/Builds'
        source.mkdir(parents=True)
        a = self.workspace / 'SteamA/steamapps'
        b = self.workspace / 'SteamB/steamapps'
        a.mkdir(parents=True);b.mkdir(parents=True)
        with mock.patch.object(terminal, '_runtime_steam_locations', return_value=[a,b]):
            with self.assertRaisesRegex(RuntimeError, 'Multiple Steam libraries'):
                terminal._terminal_mutable_root(source)
        self.assertEqual([p for p in source.rglob('*')], [])

    def test_developer_logs_stay_under_builds(self):
        builds = self.workspace / 'Builds'
        builds.mkdir()
        self.assertEqual(terminal._terminal_mutable_root(builds), builds)
        log = terminal.ServerRuntimeLog(terminal._runtime_logs_root(builds))
        try:
            self.assertTrue(log.path.is_relative_to(builds / 'Logs'))
            log.append('native shell failed: diagnostic example')
            self.assertIn('native shell failed', log.path.read_text())
        finally:
            log.close()

    def test_installed_devtools_never_create_home_builds(self):
        parent = Path('/mnt/data/Builds') / ('s-' + __import__('uuid').uuid4().hex[:8])
        steam = parent / 'steamapps'
        steam.mkdir(parents=True)
        self.addCleanup(lambda: __import__('shutil').rmtree(parent, ignore_errors=True))
        with mock.patch.dict(os.environ, {'CINTER_TERMINAL_RUNTIME_MODE': 'installed',
                                          'CINTER_STEAMAPPS': str(steam),
                                          'CINTER_BUILDS_ROOT': str(self.workspace/'wrong/Builds')}):
            result = terminal._state_root()
        self.assertTrue(result.is_relative_to(steam))
        self.assertFalse((self.workspace / 'wrong' / 'Builds').exists())


if __name__ == '__main__':
    unittest.main()
