"""Focused ui regression contracts. Native target acceptance is separate."""
from tests._support import *

class CommandHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_module('cinterterminal_cli_test', CINTERFORGETERMINAL_COMMANDS_REPO / 'webapp/CinterForgeTerminal.py')

    def capture(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = self.cli.execute(list(args))
        return result, output.getvalue()

    def test_help_covers_exact_supported_operations(self):
        for args, terms in [
            (('help',), ('debugger', 'bedrock', 'help')),
            (('help', 'debugger'), ('open', 'status', 'help')),
            (('help', 'bedrock'), ('status', 'eula', 'install', 'start', 'stop')),
            (('bedrock', 'help'), ('install', 'start', 'stop')),
            (('debugger', 'help'), ('open', 'status')),
        ]:
            with self.subTest(args=args):
                result, content = self.capture(*args)
                self.assertEqual(result, 0)
                for term in terms:
                    self.assertIn(term, content)

    def test_invalid_options_fail_closed(self):
        for args in [('help', 'foobar'), ('bedrock', 'delete-world'), ('debugger', '--remote')]:
            result, _ = self.capture(*args)
            self.assertEqual(result, 2)

    def test_bedrock_command_does_not_install_before_explicit_request(self):
        calls = []
        def api(path, *, body=None):
            calls.append((path, body))
            return {'installed': False, 'eula_accepted': False}
        with mock.patch.object(self.cli, '_api', side_effect=api):
            rc, output = self.capture('bedrock')
        self.assertEqual(rc, 0)
        self.assertIn('not installed', output)
        self.assertEqual(calls, [('/api/bedrock/status', None), ('/api/bedrock/eula', {})])
        self.assertNotIn('/api/bedrock/install', [c[0] for c in calls])

    def test_bedrock_install_reports_progress_until_complete_without_json_dump(self):
        def api(path, *, body=None):
            if path == '/api/bedrock/install':
                return {'installing': True, 'installed': False}
            if path == '/api/bedrock/status':
                return states.pop(0)
            if path.startswith('/api/bedrock/events?cursor='):
                return {'events': [{'id': i + 1, 'message': msg} for i, msg in enumerate(('Downloading 18%', 'Downloading 72%', 'Verified SHA-256'))], 'path': '/tmp/unused'}
            raise AssertionError(path)
        states = [
            {'eula_accepted': True, 'installing': True},
            {'installing': True, 'install_progress': 'Downloading 18%'},
            {'installing': True, 'install_progress': 'Downloading 72%'},
            {'installing': False, 'installed': True, 'install_progress': 'Verified SHA-256'},
        ]
        with mock.patch.object(self.cli, '_api', side_effect=api), \
             mock.patch.object(self.cli.time, 'sleep', return_value=None):
            rc, output = self.capture('bedrock', 'install')
        self.assertEqual(rc, 0)
        self.assertIn('Downloading 18%', output)
        self.assertIn('Downloading 72%', output)
        self.assertIn('Verified SHA-256', output)
        self.assertIn('Installed', output)
        self.assertNotIn('"installing"', output)

    def test_bedrock_install_reports_worker_failure_and_cancel_without_stopping_worker(self):
        states = iter([{'eula_accepted': True}, {'installing': True, 'install_progress': 'Starting'},
                       {'installing': False, 'install_error': 'official download unavailable'}])
        def api(path, *, body=None):
            if path == '/api/bedrock/status':
                return next(states)
            if path.startswith('/api/bedrock/events?cursor='):
                return {'events': [], 'path': '/tmp/unused'}
            return {'installing': True}
        with mock.patch.object(self.cli, '_api', side_effect=api), \
             mock.patch.object(self.cli.time, 'sleep', return_value=None):
            rc, output = self.capture('bedrock', 'install')
        self.assertEqual(rc, 1)
        self.assertIn('official download unavailable', output)
        states = iter([{'installing': True, 'install_progress': 'Downloading'}])
        with mock.patch.object(self.cli, '_api', side_effect=lambda path: next(states) if path == '/api/bedrock/status' else {'events': [], 'path': '/tmp/unused'}), \
             mock.patch.object(self.cli.time, 'sleep', side_effect=KeyboardInterrupt):
            rc, output = self.capture_dummy_monitor()
        self.assertEqual(rc, 130)
        self.assertIn('download continues', output)

    def capture_dummy_monitor(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            result=self.cli._monitor_bedrock_install()
        return result,output.getvalue()

    def test_setpass_routes_credentials_without_echoing_them(self):
        password = 'test-only-credential-please-do-not-print'
        with mock.patch.object(self.cli, '_api', return_value={'updated': True}) as api, \
             mock.patch.dict(os.environ, {'CINTER_TERMINAL_SESSION_ID': 'test-shell'}):
            result, message = self.capture('setpass', 'buildhost', password)
        self.assertEqual(result, 0)
        api.assert_called_once_with('/api/lan/setpass', body={
            'username': 'buildhost', 'password': password, 'session_id': 'test-shell'})
        self.assertNotIn(password, message)
        self.assertEqual(self.capture('setpass', 'buildhost')[0], 2)

    def test_nonlocal_endpoints_rejected(self):
        with mock.patch.dict(os.environ, {'CINTER_TERMINAL_URL': 'http://evil.example:5000',
                                                   'CINTER_TERMINAL_TOKEN': 'secrettoken'}):
            self.assertRaises(RuntimeError, self.cli._api, '/api/status')

class NativeHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = os.environ.get('CPH_HOST_LIBRARY')
        if not library or not Path(library).is_file():
            raise unittest.SkipTest('CPH_HOST_LIBRARY must point to a compiled host library')
        cls.api = ctypes.CDLL(library)
        cls.api.cph_create.restype = ctypes.c_void_p
        cls.api.cph_destroy.argtypes = [ctypes.c_void_p]
        cls.api.cph_start.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        cls.api.cph_call.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        cls.api.cph_free_string.argtypes = [ctypes.c_void_p]
        cls.api.cph_last_error.argtypes = [ctypes.c_void_p]
        cls.api.cph_last_error.restype = ctypes.c_char_p
        cls.api.cph_running.argtypes = [ctypes.c_void_p]
        cls.api.cph_stop.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cls.api.cph_api_version.restype = ctypes.c_uint32
        cls.callback_type = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
        cls.api.cph_set_event_callback.argtypes = [ctypes.c_void_p, cls.callback_type, ctypes.c_void_p]
        cls.worker = str(Path(__file__).resolve().parents[1] / 'host/worker.py').encode()
        # Generate the one RPC fixture inside Builds. Tests are defined in
        # this file; no extra authored service/test module is required.
        from pathlib import Path
        builds = Path(os.environ.get('CINTER_BUILDS_ROOT',
                                    str(Path(__file__).resolve().parents[2] / 'Builds'))).resolve()
        if builds.name != 'Builds':
            raise RuntimeError('Native API test fixtures require the shared Builds directory')
        scratch = builds / 'Intermediates/CinterForgeTerminalTests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='rpc-service-', dir=scratch)
        cls.addClassCleanup(temporary.cleanup)
        service = Path(temporary.name) / 'rpc_service.py'
        service.write_text(RPC_SERVICE_FIXTURE, encoding='utf-8')
        cls.service = str(service).encode()

    def setUp(self):
        self.handle = self.api.cph_create()
        self.assertTrue(self.handle)
        self.addCleanup(self.api.cph_destroy, self.handle)
        self.events = []
        self.callback = self.callback_type(lambda json_bytes, _: self.events.append(json.loads(json_bytes)))
        self.api.cph_set_event_callback(self.handle, self.callback, None)
        argv = (ctypes.c_char_p * 1)(self.service)
        status = self.api.cph_start(self.handle, sys.executable.encode(), self.worker,
                                    ctypes.cast(argv, ctypes.c_void_p), 1, 5000)
        self.assertEqual(status, 0, self.api.cph_last_error(self.handle))
        self.addCleanup(self.api.cph_stop, self.handle, 1500)

    def request(self, method, params=None):
        out = ctypes.c_void_p()
        status = self.api.cph_call(self.handle, method.encode(), json.dumps(params or {}).encode(),
                                   5000, ctypes.byref(out))
        self.assertEqual(status, 0, self.api.cph_last_error(self.handle))
        self.assertTrue(out.value)
        try:
            return json.loads(ctypes.string_at(out))
        finally:
            self.api.cph_free_string(out)

    def test_version_and_ping(self):
        self.assertEqual(self.api.cph_api_version(), 1)
        self.assertEqual(self.request('ping')['result'], {'pong': True})

    def test_typed_request_reply_and_multiple_events(self):
        response = self.request('progress', {'name': 'Kevin'})
        self.assertEqual(response['result']['echo']['name'], 'Kevin')
        progress = [item['done'] for item in self.events if item.get('event') == 'progress']
        self.assertEqual(progress, [1, 2])

    def test_service_error_is_correlated_rpc_reply(self):
        response = self.request('explode')
        self.assertEqual(response['error']['type'], 'ValueError')
        self.assertIn('expected', response['error']['message'])

    def test_calls_have_unique_ids(self):
        self.assertEqual(self.request('first')['id'], 1)
        self.assertEqual(self.request('second')['id'], 2)






class LiveIFCommandTransportTests(unittest.TestCase):
    def test_authenticated_start_story_read_stop_over_loopback(self):
        from webapp import CinterForgeTerminal as service
        from urllib.request import Request, urlopen
        with tempfile.TemporaryDirectory() as scratch:
            work=Path(scratch)
            builds=work/'Builds';builds.mkdir()
            steam=work/'CustomSteam/steamapps';steam.mkdir(parents=True)
            path=steam/'common/CinterForge/InteractiveFiction/Titles/zork1/zork1.z3'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'\x03'+b'\0'*127)
            with mock.patch.dict(os.environ,{'CINTER_STEAMAPPS':str(steam)}):
                service_instance=service.CinterShellServer(builds)
            worker=threading.Thread(target=service_instance.serve_forever,daemon=True)
            worker.start()
            def api(uri,body=None):
                request=Request(service_instance.origin+uri,
                    data=json.dumps(body).encode('utf-8') if body is not None else None,
                    headers={'X-CPH-Token':service_instance.token,
                             'Content-Type':'application/json'},
                    method='POST' if body is not None else 'GET')
                with urlopen(request,timeout=4) as response:
                    return response.read()
            try:
                first=json.loads(api('/api/if/start',{'command':'zork1'}))
                self.assertTrue(first['requested'])
                launch=first['launch_id']
                # Rapid repeated commands must enqueue, not overwrite, an
                # earlier request. The same title reselects its single tab.
                repeated=json.loads(api('/api/if/start',{'command':'zork1'}))
                self.assertEqual(repeated['launch_id'],launch)
                opened=json.loads(api('/api/if/next'))
                self.assertEqual((opened['start'],opened['launch_id']),('zork1',launch))
                self.assertEqual(json.loads(api('/api/if/next'))['launch_id'],launch)
                self.assertEqual(api('/api/if/story/zork1'),path.read_bytes())
                # A stale tab cannot stop a different generation of Zork.
                stale='0'*32 if launch!='0'*32 else 'f'*32
                self.assertFalse(json.loads(api('/api/if/stop',
                    {'command':'zork1','launch_id':stale}))['stopped'])
                self.assertTrue(json.loads(api('/api/if/stop',
                    {'command':'zork1','launch_id':launch}))['stopped'])
                stopped=json.loads(api('/api/if/next'))
                self.assertEqual((stopped['stop'],stopped['launch_id']),('zork1',launch))
                again=json.loads(api('/api/if/start',{'command':'zork1'}))
                self.assertNotEqual(again['launch_id'],launch)
                self.assertEqual(json.loads(api('/api/if/next'))['start'],'zork1')
                self.assertFalse(json.loads(api('/api/if/stop',
                    {'command':'zork1','launch_id':launch}))['stopped'])
                self.assertTrue(json.loads(api('/api/if/stop',
                    {'command':'zork1','launch_id':again['launch_id']}))['stopped'])
                self.assertEqual(json.loads(api('/api/if/next'))['stop'],'zork1')
            finally:
                service_instance.stop()
                worker.join(timeout=3)

class OneWebviewSourceTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / 'Builds/Intermediates/CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.builds = Path(self.temp.name) / 'Builds'
        self.builds.mkdir()

    def test_webview2_headers_are_staged_and_verified_without_a_second_download(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, mode='w') as archive:
            archive.writestr('build/native/include/WebView2.h', 'header fixture')
            archive.writestr('build/native/include/WebView2EnvironmentOptions.h', 'auxiliary header')
            archive.writestr('content/ignored.txt', 'not headers')
        content.seek(0)
        class Response(io.BytesIO):
            status = 200
        with mock.patch.object(webview.urllib.request, 'urlopen', return_value=Response(content.getvalue())) as fetch:
            sdk = webview.provision_webview2(self.builds)
            self.assertEqual(webview.provision_webview2(self.builds), sdk)
            fetch.assert_called_once()
        self.assertTrue((sdk / 'build/native/include/WebView2.h').is_file())
        self.assertFalse((sdk / 'content/ignored.txt').exists())
        manifest = json.loads((sdk / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], '1.0.1150.38')
        (sdk / 'build/native/include/WebView2.h').write_text('tampered')
        with self.assertRaisesRegex(RuntimeError, 'stage changed'):
            webview.provision_webview2(self.builds)

    def test_toolchain_is_upstream_and_mac_override_is_only_sysroot(self):
        source = self.builds / 'Toolchains/WebView/0.12.0/source'
        official = source / 'cmake/toolchains/x86_64-w64-mingw32.cmake'
        official.parent.mkdir(parents=True)
        official.write_text('set(CMAKE_SYSTEM_NAME Windows)\nset(CMAKE_SYSROOT /usr/x86_64-w64-mingw32)\n')
        with mock.patch('platform.system', return_value='Linux'):
            self.assertEqual(webview.upstream_windows_toolchain(self.builds, source), official)
        with mock.patch('platform.system', return_value='Darwin'):
            wrapper = webview.upstream_windows_toolchain(self.builds, source)
        text = wrapper.read_text()
        self.assertIn(f'include("{official.as_posix()}")', text)
        self.assertIn('set(CMAKE_SYSROOT "")', text)
        with mock.patch('platform.system', return_value='Linux'):
            self.assertEqual(webview.upstream_windows_toolchain(self.builds, source), official)



class OptionalGuestBashTests(unittest.TestCase):
    """Windows/Linux menu entrypoints never substitute Mac Bash or auto-install."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        location = str(root / 'webapp')
        if location not in sys.path:
            sys.path.insert(0, location)
        cls.adapter = load_module('cinter_devtools_shell_unit', root / 'webapp/CinterForgeTerminal.py')

    def test_mac_request_is_not_handled_by_optional_devtools(self):
        with mock.patch.object(self.adapter, 'install_devtools') as installer:
            with self.assertRaisesRegex(RuntimeError, 'only for Windows/Linux'):
                self.adapter.guest_shell_main('mac')
        installer.assert_not_called()

    def test_missing_sdk_decline_never_starts_a_guest_or_downloads_silently(self):
        with mock.patch.object(self.adapter.platform, 'system', return_value='Darwin'), \
             mock.patch.object(self.adapter.threading, 'Thread') as thread, \
             mock.patch.object(self.adapter, 'sdk_root', return_value=None), \
             mock.patch.object(self.adapter, 'install_devtools', return_value=1) as installer, \
             mock.patch.object(self.adapter, '_ensure_docker') as start:
            self.assertEqual(self.adapter.guest_shell_main('linux'), 1)
        thread.return_value.start.assert_called_once()
        installer.assert_called_once_with()
        start.assert_not_called()

    def test_installed_linux_launches_amd64_image_not_host_bash(self):
        from types import SimpleNamespace
        with mock.patch.object(self.adapter.platform, 'system', return_value='Darwin'), \
             mock.patch.object(self.adapter.threading, 'Thread'), \
             mock.patch.object(self.adapter, 'sdk_root', return_value=Path('/sdk')), \
             mock.patch.object(self.adapter, '_env', return_value={'PATH':'p'}), \
             mock.patch.object(self.adapter, '_ensure_docker', return_value='/docker'), \
             mock.patch.object(self.adapter, '_image_archive', return_value=(Path('/cached-image.tar'), 'cinterforge-terminal-linux-sample:123')) as image, \
             mock.patch.object(self.adapter, 'install_devtools') as installer, \
             mock.patch.object(self.adapter.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(self.adapter.subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value=0
            self.assertEqual(self.adapter.guest_shell_main('linux'),0)
            args=popen.call_args.args[0]
        image.assert_called_once()
        installer.assert_not_called()
        self.assertIn('--platform',args)
        self.assertIn('linux/amd64',args)
        self.assertEqual(args[-4:],['cinterforge-terminal-linux-sample:123','/bin/bash','--noprofile','-i'])
        self.assertIn('cinterforge-terminal-linux-sample:123',args)
        self.assertNotIn('/usr/bin/env',args)

    def test_installed_windows_launches_msys2_pe_under_wine_not_host_shell(self):
        from types import SimpleNamespace
        with mock.patch.object(self.adapter.platform, 'system', return_value='Darwin'), \
             mock.patch.object(self.adapter.threading, 'Thread'), \
             mock.patch.object(self.adapter, 'sdk_root', return_value=Path('/sdk')), \
             mock.patch.object(self.adapter, '_env', return_value={'PATH':'p'}), \
             mock.patch.object(self.adapter, '_ensure_docker', return_value='/docker'), \
             mock.patch.object(self.adapter, '_image_archive', return_value=(Path('/cached-image.tar'), 'cinterforge-terminal-windows-cross:123')), \
             mock.patch.object(self.adapter.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(self.adapter.subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value=0
            self.assertEqual(self.adapter.guest_shell_main('win'),0)
            args=popen.call_args.args[0]
        self.assertIn('wine',args)
        self.assertIn('Z:\\msys2\\usr\\bin\\bash.exe',args)
        self.assertIn('type=bind,src=/sdk/Windows/MSYS2,dst=/msys2,readonly',args)
        self.assertNotIn('/bin/bash',args)

