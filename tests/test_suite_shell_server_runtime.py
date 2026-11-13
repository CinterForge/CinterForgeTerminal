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

            # A native WebView receives a one-use startup URL privately from
            # its Host process. A normal browser still needs setpass credentials.
            status, _, body = request('GET', '/_native_open?key=wrong')
            self.assertEqual(status, 403)
            from urllib.parse import quote
            status, headers, body = request('GET', '/_native_open?key=' +
                                            quote(server.native_grant))
            self.assertEqual(status, 303, body)
            self.assertEqual(headers['Location'], '/')
            self.assertNotIn(server.native_grant.encode('ascii'), body)
            native_cookie = headers['Set-Cookie'].split(';', 1)[0]
            self.assertIn('HttpOnly', headers['Set-Cookie'])
            status, _, body = request('GET', '/', cookie=native_cookie)
            self.assertEqual(status, 200)
            self.assertIn(server.token.encode('ascii'), body)
            status, _, body = request('GET', '/api/status', cookie=native_cookie, token=True)
            self.assertEqual(status, 200)
            status, _, body = request('GET', '/host/code-workspace')
            self.assertEqual(status, 401)
            status, _, body = request('POST', '/code/command', {
                'operation': 'workspace-state'})
            self.assertEqual(status, 403)
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
            status, _, body = request('GET', '/host/code-workspace', cookie=cookie)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)['kind'], 'terminal')

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
            # Changing the remote password must not lock out the local native app.
            status, _, body = request('GET', '/', cookie=native_cookie)
            self.assertEqual(status, 200)
        finally:
            connection.close()
            server.stop()
            worker.join(timeout=3)

    def test_native_http_parent_exit_releases_its_own_listener(self):
        """An abrupt native/Xcode exit must not orphan TCP 20300 in Python."""
        import socket
        builds = self.workspace / 'Builds'
        builds.mkdir()
        with mock.patch.object(terminal, '_load_bedrock_manager',
                               side_effect=RuntimeError('Bedrock not needed')):
            server = terminal.CinterShellServer(builds)
        port = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with mock.patch.object(terminal, '_server', server), \
                 mock.patch.object(terminal.os, 'getppid', side_effect=[900, 1]), \
                 mock.patch.object(terminal.time, 'sleep', return_value=None):
                terminal._watch_native_http_parent(server, 900)
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), 'HTTP server exits with its native owner')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        finally:
            if worker.is_alive():
                server.stop()
                worker.join(timeout=3)

    def test_native_launcher_reuses_only_verified_http_owner(self):
        """A second launch attaches to our live server but never kills port users."""
        builds = self.workspace / 'Builds'
        builds.mkdir()
        with mock.patch.object(terminal, '_load_bedrock_manager',
                               side_effect=RuntimeError('Bedrock not needed')), \
             mock.patch.object(terminal, 'CinterForgeDiscovery'), \
             mock.patch.object(terminal, 'LAN_HTTP_PORT', 0):
            server = terminal.CinterShellServer(builds, lan=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        path = terminal._lan_owner_path(builds)
        try:
            with mock.patch.object(terminal, 'LAN_HTTP_PORT', server.server_port):
                terminal._write_lan_owner(server)
                self.assertEqual(terminal._existing_lan_owner(builds)['instance'],
                                 server.lan_id)
                # The same signed Terminal may be started from an installed
                # Steam app while another native window runs from Xcode/Builds.
                # The already-running service must be recognized even when its
                # owner receipt is in the other legitimate runtime-state root.
                steam = self.workspace / 'library' / 'steamapps'
                state = steam / 'common/CinterForge/Applications/TerminalState'
                state.mkdir(parents=True)
                alternate = terminal._lan_owner_path(state)
                receipt = path.read_bytes()
                alternate.parent.mkdir(parents=True)
                alternate.write_bytes(receipt)
                alternate.chmod(0o600)
                path.unlink()
                try:
                    with mock.patch.object(terminal, '_runtime_steam_locations', return_value=[steam]):
                        self.assertEqual(terminal._existing_lan_owner(builds)['instance'],
                                         server.lan_id)
                        # A mismatched receipt must not be accepted, even from
                        # a recognized Steam library.
                        invalid = json.loads(receipt)
                        invalid['instance'] = '0' * 32
                        alternate.write_text(json.dumps(invalid))
                        self.assertIsNone(terminal._existing_lan_owner(builds))
                finally:
                    path.write_bytes(receipt)
                    path.chmod(0o600)
                    alternate.unlink(missing_ok=True)
                with mock.patch.object(terminal, 'ASSETS', builds / 'webapp'):
                    (builds / 'webapp').mkdir()
                    attached = terminal.handle('start', {'builds': str(builds)})
                    self.assertTrue(attached['attached'])
                    self.assertEqual(attached['native_grant'], server.native_grant)
                    self.assertTrue(worker.is_alive(), 'attaching must not stop the owner')
                    self.assertTrue(terminal.handle('stop', {})['stopped'])
                    self.assertTrue(worker.is_alive(), 'detaching must not stop the owner')
                # A stale/mismatched receipt cannot justify hijacking another
                # process's listening socket, even when /healthz responds.
                data = json.loads(path.read_text())
                data['instance'] = '0' * 32
                path.write_text(json.dumps(data))
                self.assertIsNone(terminal._existing_lan_owner(builds))
                terminal._write_lan_owner(server)
        finally:
            server.stop()
            worker.join(timeout=3)
        self.assertFalse(path.exists(), 'normal shutdown cleans the owner receipt')

    def test_codeoss_local_workspace_persists_and_serves_real_files(self):
        """Code uses the selected host files and same-origin HTTP, not a mock editor."""
        import base64

        builds = self.workspace / 'Builds'
        builds.mkdir()
        home = self.workspace / 'User'
        source = home / 'Source'
        documents = home / 'Documents'
        source.mkdir(parents=True)
        documents.mkdir()
        project = source / 'Project'
        project.mkdir()
        (project / 'readme.md').write_text('hello', encoding='utf-8')
        other = home / 'Another'
        other.mkdir()
        (other / 'main.py').write_text('print(1)', encoding='utf-8')
        workspace_file = home / 'both.code-workspace'
        workspace_file.write_text(json.dumps({'folders': [
            {'name': 'Project', 'path': 'Source/Project'},
            {'name': 'Another', 'path': 'Another'}
        ]}), encoding='utf-8')

        with mock.patch.dict('os.environ', {'CINTER_USER_HOME': str(home)}), \
             mock.patch.object(terminal, '_load_bedrock_manager',
                               side_effect=RuntimeError('No Bedrock needed')):
            server = terminal.CinterShellServer(builds)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertEqual(self._request(server, '/host/code-workspace')['cwd'], str(source))
            selected = self._request(server, '/code/command', {
                'operation': 'open-workspace', 'path': str(workspace_file)})
            self.assertEqual(selected['workspace_kind'], 'workspace')
            names = self._request(server, '/code/command', {
                'operation': 'read-directory', 'path': '/'})['entries']
            self.assertEqual([entry['name'] for entry in names], ['Project', 'Another'])
            content = self._request(server, '/code/command', {
                'operation': 'read-file', 'path': '/Project/readme.md'})['data']
            self.assertEqual(base64.b64decode(content), b'hello')
            for probe in ('stat', 'read-file', 'read-directory'):
                missing = self._request(server, '/code/command', {
                    'operation': probe, 'path': '/Project/not-present'})
                self.assertFalse(missing['ok'], 'missing Code-OSS paths are still missing')
                self.assertEqual(missing['error_code'], 'not_found',
                                 'ordinary VS Code optional probes must not return HTTP 400')
            self._request(server, '/code/command', {
                'operation': 'write-file', 'path': '/Another/main.py',
                'data': base64.b64encode(b'print(2)').decode(), 'overwrite': True})
            self.assertEqual((other / 'main.py').read_text(), 'print(2)')
            with self.assertRaises(__import__('urllib.error', fromlist=['HTTPError']).HTTPError):
                self._request(server, '/code/command', {
                    'operation': 'read-file', 'path': '/Project/../Another/main.py'})
            self._request(server, '/code/command', {'operation': 'set-theme', 'theme': 'light'})
            self.assertEqual(terminal.CodeWorkspace(builds).details()['theme'], 'light')
            self.assertEqual(terminal.CodeWorkspace(builds).details()['workspace_kind'], 'workspace')

            server.log_event('Code-OSS test server entry')
            opened = self._request(server, '/host/code-shell-open', {
                'id': 'terminal-code-test-1', 'backend': 'server'})
            self.assertTrue(opened['opened'])
            output = self._request(server, '/host/code-shell-output?id=terminal-code-test-1')
            self.assertTrue(any('Code-OSS test server entry' in row['text'] for row in output['rows']))
            self.assertTrue(self._request(server, '/host/code-shell-close', {
                'id': 'terminal-code-test-1'})['closed'])
            # Terminal log links must load actual bytes, even when the selected
            # editor workspace does not contain the Builds/Steam Logs tree.
            import base64
            log_path = str(server.log.path)
            stat = self._request(server, '/code/command', {
                'operation': 'host-stat', 'path': log_path})
            self.assertEqual(stat['type'], 'file')
            contents = self._request(server, '/code/command', {
                'operation': 'host-read-file', 'path': log_path})
            self.assertIn(b'Code-OSS test server entry', base64.b64decode(contents['data']))
            entries = self._request(server, '/code/command', {
                'operation': 'host-read-directory', 'path': str(server.log.path.parent)})
            self.assertIn(server.log.path.name, [entry['name'] for entry in entries['entries']])
            with mock.patch.object(terminal.subprocess, 'Popen') as launch:
                self._request(server, '/code/command', {
                    'operation': 'reveal-host', 'path': log_path})
                self._request(server, '/code/command', {
                    'operation': 'reveal-workspace', 'path': '/Project/readme.md'})
                self.assertEqual(launch.call_count, 2)
            # A displayed log path is not an unrestricted filesystem endpoint.
            outside = home / 'unshared.txt'
            outside.write_text('private', encoding='utf-8')
            with self.assertRaises(__import__('urllib.error', fromlist=['HTTPError']).HTTPError):
                self._request(server, '/code/command', {
                    'operation': 'host-read-file', 'path': str(outside)})
            class LiveShell:
                def close(self):
                    pass
            server.sessions['source-shell'] = LiveShell()
            # The shell commands reuse CodeWorkspace's persisted state.
            # A new Terminal starts dark; choosing a theme/folder persists and
            # the UI is notified to reload without inventing another settings store.
            self.assertTrue(self._request(server, '/api/code/open', {
                'session_id': 'source-shell', 'operation': 'run'})['requested'])
            self.assertEqual(self._request(server, '/api/code/next'), {'open': True, 'reload': False})
            theme = self._request(server, '/api/code/open', {
                'session_id': 'source-shell', 'operation': 'mode', 'theme': 'dark'})
            self.assertEqual(theme['theme'], 'dark')
            self.assertEqual(self._request(server, '/api/code/next'), {'open': True, 'reload': True})
            chosen = self._request(server, '/api/code/open', {
                'session_id': 'source-shell', 'operation': 'open', 'path': str(source)})
            self.assertEqual(chosen['path'], str(source))
            self.assertEqual(self._request(server, '/api/code/next'), {'open': True, 'reload': True})
            self.assertEqual(terminal.CodeWorkspace(server.mutable_builds).details()['theme'], 'dark')
            self.assertEqual(terminal.CodeWorkspace(server.mutable_builds).details()['path'], str(source))
            self.assertFalse(self._request(server, '/api/code/next')['open'])
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



class CodeTerminalLifetimeTests(unittest.TestCase):
    """Closed Code-OSS guest sessions cannot exhaust the live session bound."""

    def test_guest_bash_is_single_instance_in_code_only(self):
        class ExistingGuest(terminal.TerminalSession):
            def __init__(self, backend):
                self.code_backend = backend
                self.closed = False

            def close(self):
                self.closed = True

        root = Path(__file__).resolve().parents[2] / 'Builds' / 'Intermediates' / 'CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='code-guest-singleton-', dir=root) as temp:
            with mock.patch.object(terminal, '_load_bedrock_manager', side_effect=RuntimeError('no bedrock needed')):
                server = terminal.CinterShellServer(Path(temp))
            server.system = 'mac'  # macOS hosts the managed cross-platform development guests.
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                guest = ExistingGuest('linux')
                with server.code_sessions_lock:
                    server.code_sessions['terminal-existing-linux'] = guest
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as duplicate:
                    urlopen(Request(server.origin + '/host/code-shell-open',
                                    data=json.dumps({'id':'terminal-second-linux', 'backend':'linux'}).encode(),
                                    headers={'Content-Type':'application/json'}, method='POST'), timeout=6)
                self.assertEqual(duplicate.exception.code, 400)
                self.assertIn('already open', duplicate.exception.read().decode())
                self.assertFalse(guest.closed)
                self.assertEqual(len(server.code_sessions), 1)
                # Bypassing the UI cannot create Windows/Linux Bash in a Session.
                for backend in ('linux', 'win'):
                    with self.assertRaises(HTTPError) as rejected:
                        urlopen(Request(server.origin + '/api/terminal/open',
                                        data=json.dumps({'backend':backend}).encode(),
                                        headers={'Content-Type':'application/json',
                                                 'X-CPH-Token':server.token}, method='POST'), timeout=6)
                    self.assertEqual(rejected.exception.code, 400)
                    self.assertIn('Code-OSS', rejected.exception.read().decode())
                # Closing releases the singleton slot without terminating another guest.
                with server.code_sessions_lock:
                    server.code_sessions.pop('terminal-existing-linux')
                guest.close()
                self.assertTrue(guest.closed)
            finally:
                server.stop()
                thread.join(timeout=3)

    def test_exited_guest_session_reclaimed_without_evicting_live_shells(self):
        class MockSession(terminal.TerminalSession):
            def __init__(self, closed):
                self.closed = closed
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

            def poll(self, _wait):
                return b'final line', self.closed

        root = Path(__file__).resolve().parents[2] / 'Builds' / 'Intermediates' / 'CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='code-session-lifetime-', dir=root) as temp:
            with mock.patch.object(terminal, '_load_bedrock_manager', side_effect=RuntimeError('no bedrock needed')):
                server = terminal.CinterShellServer(Path(temp))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                dead = [MockSession(True) for _ in range(terminal.MAX_SESSIONS)]
                with server.code_sessions_lock:
                    server.code_sessions.update({f'terminal-dead-{i}': item
                                                 for i, item in enumerate(dead)})
                # Opening Server Log must reclaim exited sessions, not increase
                # the configured cap or kill any still-running PTY.
                raw = json.dumps({'id': 'terminal-code-reopen', 'backend': 'server'}).encode()
                request = Request(server.origin + '/host/code-shell-open', data=raw,
                                  headers={'Content-Type': 'application/json'}, method='POST')
                with urlopen(request, timeout=6) as response:
                    self.assertTrue(json.loads(response.read())['opened'])
                self.assertEqual(len(server.code_sessions), 1)
                self.assertTrue(all(item.close_calls == 1 for item in dead))

                live = [MockSession(False) for _ in range(terminal.MAX_SESSIONS - 1)]
                with server.code_sessions_lock:
                    server.code_sessions.update({f'terminal-live-{i}': item
                                                 for i, item in enumerate(live)})
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as captured:
                    urlopen(Request(server.origin + '/host/code-shell-open',
                                    data=json.dumps({'id':'terminal-over-cap', 'backend':'server'}).encode(),
                                    headers={'Content-Type':'application/json'}, method='POST'), timeout=6)
                self.assertEqual(captured.exception.code, 400)
                self.assertEqual(len(server.code_sessions), terminal.MAX_SESSIONS)
                self.assertTrue(all(item.close_calls == 0 for item in live))

                # A final output poll releases the exited PTY after returning its transcript.
                ended = MockSession(True)
                with server.code_sessions_lock:
                    server.code_sessions['terminal-just-exited'] = ended
                with urlopen(server.origin + '/host/code-shell-output?id=terminal-just-exited',timeout=6) as response:
                    payload=json.loads(response.read())
                self.assertEqual(payload['rows'][0]['text'], 'final line')
                self.assertTrue(payload['closed'])
                self.assertNotIn('terminal-just-exited', server.code_sessions)
                self.assertEqual(ended.close_calls, 1)
            finally:
                server.stop()
                thread.join(timeout=3)
if __name__ == '__main__':
    unittest.main()


class LauncherBackgroundAssetsTests(unittest.TestCase):
    """Three replaceable PNGs; toolbar/dock must not be baked into wallpaper."""

    def test_three_independent_platform_backgrounds_ship_as_authored_pngs(self):
        root = Path(__file__).resolve().parents[1]
        asset_dir = root / 'webapp/launcher/assets/backgrounds'
        backgrounds = [asset_dir / f'{platform}.png'
                       for platform in ('mac', 'windows', 'linux')]
        image_hashes = set()
        for image in backgrounds:
            content = image.read_bytes()
            self.assertTrue(content.startswith(b'\x89PNG\r\n\x1a\n'))
            # All start with replaceable 1920x1080 images, not giant GUI themes.
            self.assertEqual(tuple(int.from_bytes(content[i:i+4], 'big')
                                   for i in (16, 20)), (1920, 1080))
            self.assertLess(len(content), 100_000)
            image_hashes.add(__import__('hashlib').sha256(content).digest())
        self.assertEqual(len(image_hashes), 3)
        js = (root / 'webapp/launcher/app.js').read_text()
        html = (root / 'webapp/launcher/index.html').read_text()
        css = (root / 'webapp/launcher/style.css').read_text()
        self.assertIn('assets/backgrounds/${wallpaperFile}.png', js)
        self.assertIn('id="wallpaper"', html)
        self.assertIn('class="dock"', html)
        self.assertIn('.dock {', css)
        self.assertNotIn('desktop-reference.png', html)
        from host import universal_package
        staged = universal_package._files_under(root / 'webapp/launcher',
                                                 'Host/webapp/launcher')
        for image in backgrounds:
            self.assertIn('Host/webapp/launcher/assets/backgrounds/' + image.name,
                          staged)
