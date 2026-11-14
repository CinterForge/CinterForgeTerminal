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
             mock.patch.object(self.adapter, 'sdk_root', return_value=None), \
             mock.patch.object(self.adapter, 'install_devtools', return_value=1) as installer, \
             mock.patch.object(self.adapter, 'boot_runtime_guests') as start:
            self.assertEqual(self.adapter.guest_shell_main('linux'), 1)
        installer.assert_called_once_with()
        start.assert_not_called()

    def test_installed_linux_reuses_app_owned_bash_not_another_docker_exec(self):
        with mock.patch.object(self.adapter.platform, 'system', return_value='Darwin'), \
             mock.patch.object(self.adapter, 'sdk_root', return_value=Path('/sdk')), \
             mock.patch.object(self.adapter, '_wait_for_app_guests') as boot, \
             mock.patch.object(self.adapter.subprocess, 'Popen') as popen:
            self.assertEqual(self.adapter.guest_shell_main('linux'), 0)
            args = self.adapter._runtime_guest_shell_command(Path('/sdk'), 'linux', Path('/Source'))
        boot.assert_called_once_with('linux')
        popen.assert_not_called()  # Only app launch owns the one interactive Bash PTY.
        self.assertEqual(args[:5], ['/sdk/mac/env/bin/docker', 'exec', '-it', '-e', 'DISPLAY=:99'])
        self.assertEqual(args[-6:-3], ['--workdir', '/host/mac/Source', self.adapter._guest_name('linux')])
        self.assertEqual(args[-3:], ['/bin/bash','--noprofile','-i'])
        self.assertNotIn('run', args)

    def test_installed_windows_reuses_app_owned_msys2_bash(self):
        with mock.patch.object(self.adapter.platform, 'system', return_value='Darwin'), \
             mock.patch.object(self.adapter, 'sdk_root', return_value=Path('/sdk')), \
             mock.patch.object(self.adapter, '_wait_for_app_guests') as boot, \
             mock.patch.object(self.adapter.subprocess, 'Popen') as popen:
            self.assertEqual(self.adapter.guest_shell_main('win'), 0)
            args = self.adapter._runtime_guest_shell_command(Path('/sdk'), 'win', Path('/Source'))
        boot.assert_called_once_with('win')
        popen.assert_not_called()
        self.assertIn('WINEPREFIX=/tmp/.wine', args)
        self.assertEqual(args[-9:-6], ['--workdir', '/host/mac/Source', self.adapter._guest_name('win')])
        self.assertEqual(args[-6:-2], ['wine', r'Z:\msys2\usr\bin\bash.exe', '--noprofile', '--norc'])
        self.assertEqual(args[-2], '-c')
        self.assertIn('exec /usr/bin/bash --noprofile --norc -i', args[-1])
        self.assertNotIn('--login', args)
        self.assertNotIn('run', args)

    def test_windows_guest_tmp_write_probe_is_at_persistent_container_boot(self):
        from types import SimpleNamespace
        sdk=Path('/sdk'); calls=[]
        def docker_call(docker,env,*args,**kwargs):
            calls.append(args)
            if args[:2] == ('image','inspect'):
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            if args[:2] == ('container','inspect'):
                return SimpleNamespace(returncode=1,stdout='',stderr='not present')
            if '/bin/bash' in args:
                return SimpleNamespace(returncode=0, stdout='GNU bash 5.2\n', stderr='')
            if 'cmd.exe' in args:
                return SimpleNamespace(returncode=0, stdout='CINTER_WINE_PE_READY\n', stderr='')
            if 'wine' in args:
                if '--version' in args:
                    return SimpleNamespace(returncode=0, stdout='wine-11.15\n', stderr='')
                return SimpleNamespace(returncode=0, stdout='MSYS_NT-10.0\n', stderr='')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with mock.patch.object(self.adapter,'_docker_call',side_effect=docker_call), \
             mock.patch.object(self.adapter,'_activate_guest_vnc') as vnc, \
             mock.patch.object(self.adapter,'_run_checked') as checked, \
             mock.patch.object(self.adapter,'_guest_host_binds',return_value=[]), \
             mock.patch.object(self.adapter,'_image_archive',return_value=(Path('/image.tar'),'image:123')):
            self.adapter._start_guest_locked(sdk,{},'/docker','win',Path('/Source'))
        self.assertEqual(checked.call_count,1)
        vnc.assert_called_once()  # VNC is prewarmed in the same container.
        run=checked.call_args_list[0].args[0]
        probe=['/docker', *calls[-1]]
        self.assertEqual(run[:3],['/docker','run','-d'])
        self.assertIn('--restart',run)
        self.assertIn('no',run)
        self.assertIn('CHERE_INVOKING=1',run)
        self.assertIn('TMPDIR=/tmp',run)
        self.assertEqual(probe[:2], ['/docker', 'exec'])
        self.assertIn('wine', probe)
        self.assertIn('bash.exe', ' '.join(probe))
        self.assertIn('/tmp/cinterforge-bash-',probe[-1])
        self.assertIn('rm "$f"',probe[-1])

    def test_windows_guest_failing_tmp_probe_prevents_ready(self):
        from types import SimpleNamespace
        def docker_call(docker,env,*args,**kwargs):
            return SimpleNamespace(returncode=0 if args[:2] == ('image','inspect') else 1,
                                   stdout='',stderr='')
        with mock.patch.object(self.adapter,'_docker_call',side_effect=docker_call), \
             mock.patch.object(self.adapter,'_guest_host_binds',return_value=[]), \
             mock.patch.object(self.adapter,'_image_archive',return_value=(Path('/image.tar'),'image:123')), \
             mock.patch.object(self.adapter,'_run_checked',return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'Windows guest framebuffer failed before Wine'):
                self.adapter._start_guest_locked(Path('/sdk'),{},'/docker','win',Path('/Source'))

    def test_unified_devtools_preserves_msys2_tmp_mount_point(self):
        with mock.patch.object(universal, '_files_under', return_value={}):
            assets = universal._devtools_assets(Path('/unused'))
        path = 'DevTools/Builds/CinterForgeTerminal/Toolchains/Windows/MSYS2/tmp/README.txt'
        self.assertIn(path, assets)
        self.assertIn(b'tmpfs', assets[path])



class WorkspaceGuestAndHtmlMenuTests(unittest.TestCase):
    def setUp(self):
        # The production VM lock uses the app-owned COLIMA_HOME. These pure
        # argument tests use a mocked environment with no real VM state.
        patcher = mock.patch.object(self.app, '_managed_vm_lock',
                                    return_value=contextlib.nullcontext())
        patcher.start()
        self.addCleanup(patcher.stop)
        # The YAML mutation is independently exercised by test_suite_guest_vm_runtime.
        config_patcher = mock.patch.object(self.app, '_configure_colima_host_mounts')
        config_patcher.start()
        self.addCleanup(config_patcher.stop)
        # Runtime Rosetta->QEMU YAML mutation is tested independently by
        # test_suite_guest_vm_runtime. These argument tests pass env={} and
        # must not inspect a real host-managed Colima profile.
        emulation_patcher = mock.patch.object(self.app, '_configure_colima_runtime_emulation',
                                              return_value=False)
        emulation_patcher.start()
        self.addCleanup(emulation_patcher.stop)
        # Guest startup tests fake subprocess.run, so real macOS mount discovery
        # must be provided separately. Actual discovery is tested in the VM
        # runtime suite; otherwise /sbin/mount is an unexpected subprocess.
        volumes_patcher = mock.patch.object(self.app, '_mac_mounted_volumes', return_value=[])
        volumes_patcher.start()
        self.addCleanup(volumes_patcher.stop)

    @classmethod
    def setUpClass(cls):
        cls.app = load_module('cinter_workspace_guest_checks',
            Path(__file__).resolve().parents[1] / 'webapp/CinterForgeTerminal.py')

    def test_colima_mounts_workspace_on_first_boot(self):
        from types import SimpleNamespace
        calls=[]; vm={'started':False}
        def fake_run(command, **kwargs):
            if 'info' in command:
                return SimpleNamespace(returncode=0 if vm['started'] else 1, stdout='', stderr='')
            if 'status' in command: return SimpleNamespace(returncode=1,stdout='',stderr='')
            if 'ssh' in command:
                return SimpleNamespace(returncode=0 if vm['started'] else 1,stdout='',stderr='')
            raise AssertionError(command)
        def checked(cmd, env, **kwargs):
            calls.append(cmd)
            if 'start' in cmd: vm['started']=True
        with mock.patch.object(self.app.subprocess,'run',side_effect=fake_run), \
             mock.patch.object(self.app,'_run_checked',side_effect=checked):
            self.assertEqual(self.app._ensure_docker(Path('/sdk'),{},Path('/code/Source')), '/sdk/mac/env/bin/docker')
        start=next(cmd for cmd in calls if 'start' in cmd)
        self.assertIn('--mount-type',start)
        self.assertIn('9p',start)
        self.assertIn('qemu',start)
        self.assertIn('x86_64',start)
        # First boot must pass the custom mount point explicitly, because
        # Colima --save-config can replace a prewritten profile's mount list.
        self.assertEqual(start[start.index('--mount') + 1], '/:/host/mac:w')
        self.assertIn('--save-config',start)
        self.assertNotIn('stop',sum(calls,[]))

    def test_running_colima_without_socket_recovers_only_if_idle(self):
        from types import SimpleNamespace
        calls=[]; vm={'started':False}
        def fake_run(command, **kwargs):
            if 'info' in command:
                return SimpleNamespace(returncode=0 if vm['started'] else 1, stdout='',stderr='missing docker.sock')
            if 'status' in command: return SimpleNamespace(returncode=0,stdout='colima running',stderr='')
            if 'ssh' in command:
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            raise AssertionError(command)
        def checked(cmd,env,**kwargs):
            calls.append(cmd)
            if 'start' in cmd: vm['started']=True
        with mock.patch.object(self.app.subprocess,'run',side_effect=fake_run), \
             mock.patch.object(self.app,'_run_checked',side_effect=checked):
            self.app._ensure_docker(Path('/sdk'),{},Path('/code/Source'))
        self.assertIn('stop',calls[0])
        self.assertIn('start',calls[1])
        self.assertIn('--mount-type',calls[1])

    def test_running_colima_without_socket_never_stops_live_guests(self):
        from types import SimpleNamespace
        def fake_run(command, **kwargs):
            if 'info' in command: return SimpleNamespace(returncode=1,stdout='',stderr='missing docker.sock')
            if 'status' in command: return SimpleNamespace(returncode=0,stdout='running',stderr='')
            if 'ssh' in command: return SimpleNamespace(returncode=0,stdout='live-guest-id',stderr='')
            raise AssertionError(command)
        with mock.patch.object(self.app.subprocess,'run',side_effect=fake_run), \
             mock.patch.object(self.app,'_run_checked') as checked:
            with self.assertRaisesRegex(RuntimeError,'live guest containers'):
                self.app._ensure_docker(Path('/sdk'),{},Path('/code/Source'))
        checked.assert_not_called()

    def test_running_colima_without_socket_refuses_unsafe_repair(self):
        from types import SimpleNamespace
        def fake_run(command, **kwargs):
            if 'info' in command: return SimpleNamespace(returncode=1,stdout='',stderr='missing docker.sock')
            if 'status' in command: return SimpleNamespace(returncode=0,stdout='running',stderr='')
            if 'ssh' in command: return SimpleNamespace(returncode=1,stdout='',stderr='SSH unavailable')
            raise AssertionError(command)
        with mock.patch.object(self.app.subprocess,'run',side_effect=fake_run), \
             mock.patch.object(self.app,'_run_checked') as checked:
            with self.assertRaisesRegex(RuntimeError,'cannot be verified'):
                self.app._ensure_docker(Path('/sdk'),{},Path('/code/Source'))
        checked.assert_not_called()

    def test_existing_idle_colima_repairs_missing_workspace_mount(self):
        from types import SimpleNamespace
        calls=[];vm={'mounted':False,'started':True}
        def fake_run(command, **kwargs):
            if 'info' in command: return SimpleNamespace(returncode=0 if vm['started'] else 1,stdout='29.5.2',stderr='')
            if 'ssh' in command:
                return SimpleNamespace(returncode=0 if command[-1]=='/host/mac' or vm['mounted'] else 1,stdout='',stderr='')
            if 'ps' in command: return SimpleNamespace(returncode=0,stdout='',stderr='')
            raise AssertionError(command)
        def checked(cmd,env,**kwargs):
            calls.append(cmd)
            if 'stop' in cmd: vm['started']=False
            if 'start' in cmd: vm.update(started=True,mounted=True)
        with mock.patch.object(self.app.subprocess,'run',side_effect=fake_run), \
             mock.patch.object(self.app,'_run_checked',side_effect=checked):
            self.app._ensure_docker(Path('/sdk'),{},Path('/code/Source'))
        self.assertIn('stop',calls[0])
        self.assertIn('start',calls[1])
        self.assertIn('--mount-type',calls[1])

    def test_existing_busy_colima_never_kills_an_active_guest(self):
        from types import SimpleNamespace
        def fake_run(command, **kwargs):
            if 'info' in command: return SimpleNamespace(returncode=0,stdout='29.5.2',stderr='')
            if 'ssh' in command: return SimpleNamespace(returncode=1,stdout='',stderr='')
            if 'ps' in command: return SimpleNamespace(returncode=0,stdout='running-container-id',stderr='')
            raise AssertionError(command)
        with mock.patch.object(self.app.subprocess,'run',side_effect=fake_run), \
             mock.patch.object(self.app,'_run_checked') as checked:
            with self.assertRaisesRegex(RuntimeError,'unrelated containers run'):
                self.app._ensure_docker(Path('/sdk'),{},Path('/code/Source'))
        checked.assert_not_called()

    def test_code_guest_shell_reuses_linux_container_with_workspace(self):
        TOOLCHAIN_SIGN_OF_LIFE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TOOLCHAIN_SIGN_OF_LIFE_ROOT) as temp:
            source=Path(temp)
            with mock.patch.dict(os.environ,{'CINTER_CODE_WORKSPACE':str(source)}), \
                 mock.patch.object(self.app.platform,'system',return_value='Darwin'), \
                 mock.patch.object(self.app,'sdk_root',return_value=Path('/sdk')), \
                 mock.patch.object(self.app,'_wait_for_app_guests') as boot, \
                 mock.patch.object(self.app.subprocess,'Popen') as popen:
                self.assertEqual(self.app.guest_shell_main('linux'),0)
                args = self.app._runtime_guest_shell_command(Path('/sdk'), 'linux', source)
            boot.assert_called_once_with('linux')
            popen.assert_not_called()
            self.assertIn('--workdir',args)
            self.assertIn('/host/mac'+str(source),args)
            self.assertIn(self.app._guest_name('linux'),args)
            self.assertNotIn('run',args)

    def test_code_guest_shell_reuses_windows_container_with_workspace(self):
        TOOLCHAIN_SIGN_OF_LIFE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TOOLCHAIN_SIGN_OF_LIFE_ROOT) as temp:
            source=Path(temp)
            with mock.patch.dict(os.environ,{'CINTER_CODE_WORKSPACE':str(source)}), \
                 mock.patch.object(self.app.platform,'system',return_value='Darwin'), \
                 mock.patch.object(self.app,'sdk_root',return_value=Path('/sdk')), \
                 mock.patch.object(self.app,'_wait_for_app_guests') as boot, \
                 mock.patch.object(self.app.subprocess,'Popen') as popen:
                self.assertEqual(self.app.guest_shell_main('win'),0)
                args = self.app._runtime_guest_shell_command(Path('/sdk'), 'win', source)
            boot.assert_called_once_with('win')
            popen.assert_not_called()
            self.assertIn('/host/mac'+str(source),args)
            self.assertIn(self.app._guest_name('win'),args)
            self.assertEqual(args[-6:-2],['wine',r'Z:\msys2\usr\bin\bash.exe','--noprofile','--norc'])
            self.assertEqual(args[-2],'-c')
            self.assertIn('exec /usr/bin/bash --noprofile --norc -i',args[-1])
            self.assertNotIn('--login',args)
            self.assertNotIn('run',args)

    def test_htmlmenus_command_uses_authenticated_host_request(self):
        with mock.patch.object(self.app,'_api',return_value={'show':True}) as api:
            stream=io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(self.app.execute(['htmlmenus','show']),0)
        self.assertIn('HTML menus: visible',stream.getvalue())
        self.assertEqual(api.call_args.args[0],'/api/ui/html-menus')
        self.assertEqual(api.call_args.kwargs['body']['action'],'show')
        self.assertEqual(self.app.execute(['htmlmenus','unknown']),2)

    def test_codeoss_menu_actions_are_real_workbench_registry_commands(self):
        root=Path(__file__).resolve().parents[1]
        entry=json.loads((root/'webapp/codeoss/extension/package.json').read_text('utf8'))
        self.assertNotIn('menubar/terminal',entry['contributes']['menus'],
            'VS Code extensions cannot contribute to a top-level Terminal menubar')
        expected={'cinterforge.newMacTerminal','cinterforge.newLinuxTerminal',
                  'cinterforge.newWindowsTerminal','cinterforge.newServerLogTerminal'}
        extension=(root/'webapp/codeoss/extension/extension.js').read_text('utf8')
        producer=(root/'webapp/codeoss/producer.py').read_text('utf8')
        for name in expected:
            self.assertIn(name,extension)
            self.assertIn(name,producer)
        css=(root/'webapp/terminal.css').read_text('utf8')
        self.assertIn('html[data-cinter-platform="mac"]',css)
        self.assertIn('.terminal-titlebar #terminal-mode-title { display: none; }',css)

    def test_real_codeoss_menu_registry_keeps_unavailable_items_visible(self):
        # The CinterForge menu entries stay visible: only their commands have
        # preconditions, rather than a `when` clause that hides the entry.
        from webapp.codeoss.producer import _patch_codeoss_terminal_menus
        TOOLCHAIN_SIGN_OF_LIFE_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TOOLCHAIN_SIGN_OF_LIFE_ROOT) as temp:
            source=Path(temp)
            target=source/'src/vs/workbench/contrib/terminal/browser/terminalMenus.ts'
            target.parent.mkdir(parents=True)
            target.write_text("""import { MenuId, MenuRegistry } from 'actions';
const TerminalMenuBarGroup = { Create: '1_create' };
export function setupTerminalMenus(): void {
	MenuRegistry.appendMenuItems(
		[
			{ id: MenuId.MenubarTerminalMenu, item: { group: TerminalMenuBarGroup.Create } },
		]);
}
""",encoding='utf8')
            _patch_codeoss_terminal_menus(source)
            updated=target.read_text('utf8')
            for command in ('newMacTerminal','newLinuxTerminal','newWindowsTerminal',
                            'newServerLogTerminal'):
                self.assertIn('cinterforge.'+command,updated)
            for entry in updated.splitlines():
                if "id: 'cinterforge.new" in entry:
                    self.assertIn('precondition: ContextKeyExpr.', entry)
                    self.assertNotIn('when: ContextKeyExpr.', entry)
            _patch_codeoss_terminal_menus(source)
            self.assertEqual(target.read_text('utf8'),updated)

class MacEditShortcutSourceTests(unittest.TestCase):
    """Keep native copy/paste shortcuts wired through both menu lifecycles."""

    @classmethod
    def setUpClass(cls):
        cls.source = (CINTERFORGETERMINAL_COMMANDS_REPO / 'src/CinterApplicationShell.cpp').read_text()

    def test_terminal_edit_menu_keeps_standard_command_shortcuts(self):
        self.assertIn('addAction(@"Edit", @"Copy", @"edit:copy")', self.source)
        self.assertIn('addAction(@"Edit", @"Paste", @"edit:paste")', self.source)
        self.assertIn('[copyItem setKeyEquivalent:@"c"]', self.source)
        self.assertIn('[pasteItem setKeyEquivalent:@"v"]', self.source)

    def test_edit_menu_executes_native_clipboard_actions(self):
        self.assertIn('[copyItem setAction:@selector(copyTerminal:)]', self.source)
        self.assertIn('[pasteItem setAction:@selector(pasteTerminal:)]', self.source)
        self.assertIn('if ([action isEqualToString:@"edit:copy"]) { [self copyTerminal:sender]; return; }', self.source)
        self.assertIn('if ([action isEqualToString:@"edit:paste"]) { [self pasteTerminal:sender]; return; }', self.source)
        self.assertIn('window.bind("cinterClipboard",', self.source)
        self.assertIn('NSPasteboard *board = [NSPasteboard generalPasteboard]', self.source)

    def test_live_code_oss_edit_menu_preserves_shortcuts_after_refresh(self):
        self.assertIn('[name isEqualToString:@"Edit"] && depth == 0', self.source)
        self.assertIn('[action isEqualToString:@"edit:copy"]', self.source)
        self.assertIn('[action isEqualToString:@"edit:paste"]', self.source)
        self.assertIn('[title isEqualToString:@"Copy"]', self.source)
        self.assertIn('[title isEqualToString:@"Paste"]', self.source)
        self.assertIn('[entry setKeyEquivalent:@"c"]', self.source)
        self.assertIn('[entry setKeyEquivalent:@"v"]', self.source)


class AppOwnedGuestWarmupTests(unittest.TestCase):
    """App boots one pair; opening a tab only observes readiness and attaches."""

    @classmethod
    def setUpClass(cls):
        cls.app = load_module('cinter_app_guest_warmup',
                              CINTERFORGETERMINAL_COMMANDS_REPO / 'webapp/CinterForgeTerminal.py')

    def server(self):
        from types import SimpleNamespace
        logs=[]
        server=SimpleNamespace(guest_start_lock=threading.Lock(),
                               guest_start_state='not-installed', guest_start_error='',
                               guest_shells_lock=threading.RLock(), guest_shells={},
                               guest_ready=[], guest_errors={},
                               native_parent_pid=1234, mutable_builds=Path('/test/Builds'),
                               _stopping=False, log_event=logs.append)
        return server, logs

    def test_launch_boot_scheduled_once_and_both_guests_mark_ready(self):
        server, logs=self.server()
        with mock.patch.object(self.app,'sdk_root',return_value=Path('/sdk')), \
             mock.patch.object(self.app,'_start_runtime_watchdog'), \
             mock.patch.object(self.app,'_runtime_logs_root',return_value=Path('/logs')), \
             mock.patch.object(self.app.threading,'Thread') as thread:
            self.assertEqual(self.app._schedule_guest_boot(server)['state'],'starting')
            self.assertEqual(self.app._schedule_guest_boot(server)['state'],'starting')
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
        with mock.patch.object(self.app,'_start_runtime_watchdog'), \
             mock.patch.object(self.app,'sdk_root',return_value=Path('/sdk')), \
             mock.patch.object(self.app,'_new_owned_guest_shell',side_effect=lambda *args: mock.Mock()), \
             mock.patch.object(self.app.threading,'Thread') as thread, \
             mock.patch.object(self.app,'_runtime_logs_root',return_value=Path('/logs')), \
             mock.patch.object(self.app,'boot_runtime_guests',return_value={
                'installed':True,'guests':['linux','win']}) as boot:
            self.app._boot_guests_logged(server)
        self.assertEqual(len(server.guest_shells), 2)
        boot.assert_called_once()
        self.assertEqual(self.app._guest_boot_snapshot(server)['state'],'ready')
        self.assertIn('ready',logs[-1])

    def test_launch_without_devtools_never_starts_vm_or_watchdog(self):
        server,_=self.server()
        with mock.patch.object(self.app,'sdk_root',return_value=None), \
             mock.patch.object(self.app,'_start_runtime_watchdog') as watchdog, \
             mock.patch.object(self.app.threading,'Thread') as thread:
            self.assertEqual(self.app._schedule_guest_boot(server)['state'],'not-installed')
        watchdog.assert_not_called()
        thread.assert_not_called()

    def test_failed_warmup_is_reported_not_retried_by_opening_tab(self):
        server,_=self.server()
        server.guest_start_state='starting'
        with mock.patch.object(self.app,'_start_runtime_watchdog'), \
             mock.patch.object(self.app,'_runtime_logs_root',return_value=Path('/logs')), \
             mock.patch.object(self.app,'stop_runtime_vm') as cleanup, \
             mock.patch.object(self.app,'boot_runtime_guests',side_effect=RuntimeError('Colima mount failed')) as boot:
            self.app._boot_guests_logged(server)
        cleanup.assert_called_once_with()
        boot.assert_called_once()
        self.assertEqual(self.app._guest_boot_snapshot(server)['state'],'failed')
        with mock.patch.object(self.app,'_api',return_value=self.app._guest_boot_snapshot(server)) as api, \
             mock.patch.object(self.app,'boot_runtime_guests') as forbidden:
            with self.assertRaisesRegex(RuntimeError,'Colima mount failed'):
                self.app._wait_for_app_guests('linux',timeout_seconds=5)
        forbidden.assert_not_called()
        api.assert_called_once_with('/api/lifecycle/guest-status')

    def test_tab_waits_for_app_boot_then_execs_without_new_boot(self):
        with mock.patch.object(self.app,'_api',side_effect=[{'state':'starting'},{'state':'ready'}]) as api, \
             mock.patch.object(self.app.time,'sleep') as sleep, \
             mock.patch.object(self.app,'boot_runtime_guests') as forbidden:
            self.app._wait_for_app_guests('win',timeout_seconds=5)
        self.assertEqual(api.call_count,2)
        sleep.assert_called_once_with(1)
        forbidden.assert_not_called()

    def test_native_server_launch_schedules_guests_before_any_session_tab(self):
        from types import SimpleNamespace
        TOOLCHAIN_SIGN_OF_LIFE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TOOLCHAIN_SIGN_OF_LIFE_ROOT) as temp:
            builds=Path(temp)/'Builds'
            (builds/'webapp').mkdir(parents=True)
            server=SimpleNamespace(mutable_builds=builds,
                code_workspace=SimpleNamespace(details=lambda: {'cwd':str(builds.parent)}),
                log=SimpleNamespace(path=builds/'Logs/server.log'),
                origin='http://127.0.0.1:20300',server_port=20300,system='mac',
                serve_forever=mock.Mock(),stop=mock.Mock(), native_grant='test-native')
            with mock.patch.dict(os.environ,{}), \
                 mock.patch.object(self.app,'ASSETS',builds/'webapp'), \
                 mock.patch.object(self.app,'_server',None), \
                 mock.patch.object(self.app,'_server_thread',None), \
                 mock.patch.object(self.app.platform,'system',return_value='Darwin'), \
                 mock.patch.object(self.app,'_existing_lan_owner',return_value=None), \
                 mock.patch.object(self.app,'CinterShellServer',return_value=server), \
                 mock.patch.object(self.app,'_steam_library_for_installed_root',
                                   return_value=Path('/real/steamapps')) as steam, \
                 mock.patch.object(self.app,'_write_lan_owner'), \
                 mock.patch.object(self.app,'_schedule_guest_boot',return_value={'state':'starting'}) as warmup, \
                 mock.patch.object(self.app.threading,'Thread') as threads:
                result=self.app.handle('start',{'builds':str(builds)})
                self.assertEqual(result['port'],20300)
                warmup.assert_called_once_with(server)
                steam.assert_called_once_with(builds)
                self.assertEqual(os.environ['CINTER_STEAMAPPS'],'/real/steamapps')
                self.assertEqual(threads.call_count,2)  # HTTP + native-parent watcher; no PTY
