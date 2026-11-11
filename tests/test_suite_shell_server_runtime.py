"""Regression for the reported nonfunctional native shell and Server tab logging.

Only directly relevant checks: actual authenticated shell-open PTY, persistence,
Stream API, installed Steam logs, and developer Builds separation.
"""
from __future__ import annotations

import json
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

    def test_setpass_enforces_browser_login_including_localhost(self):
        """Once setpass succeeds, even localhost needs a login cookie.

        Exercise HTTP requests rather than inspecting the login source text:
        a user can access the open-by-default UI, configure credentials, log
        in, and then lose access with a stale cookie after a password change.
        """
        from http.client import HTTPConnection
        from urllib.parse import urlencode

        builds = self.workspace / 'Builds'
        builds.mkdir()
        with mock.patch.object(terminal, '_load_bedrock_manager',
                               side_effect=RuntimeError('Bedrock not needed')), \
             mock.patch.object(terminal, 'CinterForgeDiscovery'), \
             mock.patch.object(terminal, 'LAN_HTTP_PORT', 0):
            server = terminal.CinterShellServer(builds, lan=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=6)

        def request(method, path, body=None, *, cookie=None, token=False):
            headers = {'Host': 'localhost:' + str(server.server_port)}
            if cookie:
                headers['Cookie'] = cookie
            if token:
                headers['X-CPH-Token'] = server.token
            if isinstance(body, dict):
                headers['Content-Type'] = 'application/json'
                body = json.dumps(body).encode('utf-8')
            elif isinstance(body, str):
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
                body = body.encode('utf-8')
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()

        class FakeShell:
            def close(self):
                pass

        try:
            server.sessions['live-shell'] = FakeShell()
            status, _, body = request('GET', '/')
            self.assertEqual(status, 200)
            self.assertIn(server.token.encode('ascii'), body)
            status, _, _ = request('GET', '/api/status', token=True)
            self.assertEqual(status, 200)

            password = 'fixture-password-not-for-logs'
            status, _, body = request('POST', '/api/lan/setpass', {
                'session_id': 'live-shell', 'username': 'developer', 'password': password
            }, token=True)
            self.assertEqual(status, 200, body)
            self.assertNotIn(password.encode(), body)
            self.assertTrue((builds / terminal.LAN_LOGIN_FILENAME).is_file())

            status, _, body = request('GET', '/')
            self.assertEqual(status, 200)
            self.assertIn(b'name="username"', body)
            self.assertNotIn(server.token.encode('ascii'), body)
            status, _, body = request('GET', '/terminal.js')
            self.assertEqual(status, 401)
            self.assertNotIn(server.token.encode('ascii'), body)
            status, _, body = request('GET', '/api/status', token=True)
            self.assertEqual(status, 401)
            self.assertIn(b'LAN login required', body)
            status, _, body = request('GET', '/healthz')
            self.assertEqual(status, 200)
            self.assertIn(b'"ready":true', body)

            status, _, _ = request('POST', '/login', urlencode({
                'username': 'developer', 'password': 'incorrect'}))
            self.assertEqual(status, 401)
            status, headers, body = request('POST', '/login', urlencode({
                'username': 'developer', 'password': password}))
            self.assertEqual(status, 303, body)
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            status, _, body = request('GET', '/', cookie=cookie)
            self.assertEqual(status, 200)
            self.assertIn(server.token.encode('ascii'), body)
            status, _, body = request('GET', '/api/status', cookie=cookie, token=True)
            self.assertEqual(status, 200, body)

            # Shell commands are native child processes, not browser clients.
            # Their private credential must keep CLI commands functional without
            # granting cookie-free access to pages on the same machine.
            with mock.patch.dict('os.environ', {
                    'CINTER_TERMINAL_URL': server.origin,
                    'CINTER_TERMINAL_TOKEN': server.token,
                    'CINTER_TERMINAL_CLI_TOKEN': server.cli_token}):
                self.assertEqual(terminal._api('/api/status')['platform'], server.system)
                self.assertTrue(terminal._api('/api/lan/setpass', body={
                    'session_id': 'live-shell', 'username': 'developer',
                    'password': 'replacement'})['updated'])
            status, _, _ = request('GET', '/api/status', cookie=cookie, token=True)
            self.assertEqual(status, 401, 'old login cookies must be invalidated')
        finally:
            connection.close()
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

if __name__ == '__main__':
    unittest.main()
