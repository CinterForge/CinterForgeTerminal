"""Focused windows regression contracts. Native target acceptance is separate."""
from tests._support import *

WINDOWS_SETUP_RETRIES_PAYLOAD = b'MZ' + b'official-installer-test' * 55000

class WindowsSetupProgressTests(unittest.TestCase):
    def test_reports_live_heartbeat_and_completion_without_changing_result(self):
        from host.setup_progress import progress_step
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with progress_step('initialize Wine prefix', interval_seconds=0.01):
                time.sleep(0.045)
        value = output.getvalue()
        self.assertIn('START initialize Wine prefix', value)
        self.assertIn('WORKING initialize Wine prefix', value)
        self.assertIn('PASS initialize Wine prefix', value)
        self.assertNotIn('FAIL initialize Wine prefix', value)

    def test_reports_failure_and_propagates_original_exception(self):
        from host.setup_progress import progress_step
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'original error'):
                with progress_step('verify Wine loader', interval_seconds=0.01):
                    raise RuntimeError('original error')
        self.assertIn('FAIL verify Wine loader', output.getvalue())
        self.assertNotIn('PASS verify Wine loader', output.getvalue())


class WindowsCrossEnvironmentTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / 'Builds/Intermediates/CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.builds = Path(self.temp.name) / 'Builds'
        self.builds.mkdir()
        self.repo = Path(__file__).resolve().parents[1]

    def test_reuses_preinstalled_complete_host_toolchain(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        with mock.patch.object(provider, '_native_ready', return_value=True), \
             mock.patch.object(provider, '_docker_available', side_effect=AssertionError('should not install Docker')):
            self.assertIs(provider.ensure(), provider)
        self.assertEqual(provider.provider, 'host')

    def test_docker_image_is_exported_verified_and_restored_offline(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        provider.docker = str(self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker')
        identity, digest = provider._image_identity()
        state = {'exists': False, 'builds': 0, 'loads': 0, 'saves': 0, 'commands': []}
        def fake_logged(command, **kw):
            state['commands'].append(command)
            if 'build' in command:
                state['builds'] += 1
                state['exists'] = True
            elif 'save' in command:
                state['saves'] += 1
                Path(command[command.index('--output') + 1]).write_bytes(b'real-image-tar-test-fixture')
            elif 'load' in command:
                state['loads'] += 1
                state['exists'] = True
        with mock.patch.object(provider, '_has_image', side_effect=lambda name: state['exists']), \
             mock.patch.object(win, '_logged', side_effect=fake_logged), \
             mock.patch.object(win, '_run', return_value=subprocess.CompletedProcess(
                 [], 0, 'github.com/docker/buildx v0.35.0')):
            provider._ensure_image()
            self.assertEqual(state['builds'], 1)
            self.assertEqual(state['commands'][0][1:4], ['buildx', 'build', '--load'])
            self.assertIn('linux/amd64', state['commands'][0])
            self.assertEqual(state['saves'], 1)
            self.assertEqual(provider.image, identity)
            manifest = json.loads((provider.stage / f'{digest[:16]}.json').read_text())
            self.assertEqual(manifest['dockerfile_sha256'], digest)
            state['exists'] = False
            provider._ensure_image()
            self.assertEqual(state['loads'], 1)
            self.assertEqual(state['builds'], 1)
            archive = provider.stage / f'{digest[:16]}.tar'
            archive.write_bytes(b'corrupted')
            with self.assertRaisesRegex(RuntimeError, 'corrupt'):
                provider._ensure_image()

    def test_missing_buildx_fails_before_legacy_image_build(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        provider.docker = str(self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker')
        with mock.patch.object(provider, '_has_image', return_value=False), \
             mock.patch.object(win, '_run', return_value=subprocess.CompletedProcess(
                 [], 125, 'docker: unknown buildx command')), \
             mock.patch.object(win, '_logged') as logged:
            with self.assertRaisesRegex(RuntimeError, 'requires managed Docker Buildx'):
                provider._ensure_image()
        logged.assert_not_called()
        self.assertEqual(list(provider.stage.glob('*.tar')), [])

    def test_container_mounts_only_source_and_builds_without_network(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        provider.provider = 'docker-linux-amd64-wine'
        provider.image = 'cinterforge-terminal-windows-cross:test'
        provider.docker = str(self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker')
        cwd = self.builds / 'Intermediates/CinterForgeTerminal/WindowsCrossSmoke'
        cwd.mkdir(parents=True)
        result = SimpleNamespace(returncode=0, stdout='wine-9.0')
        with mock.patch.object(win, '_run', return_value=result) as run:
            provider.execute(['wine', '--version'], cwd=cwd)
        cmd = run.call_args.args[0]
        self.assertIn('--network', cmd)
        self.assertIn('none', cmd)
        self.assertIn('linux/amd64', cmd)
        self.assertIn(f'type=bind,src={self.repo},dst=/source,readonly', cmd)
        self.assertIn(f'type=bind,src={self.builds},dst=/builds', cmd)
        self.assertEqual(cmd[-2:], ['wine', '--version'])
        self.assertEqual(provider.path(cwd), '/builds/Intermediates/CinterForgeTerminal/WindowsCrossSmoke')
        with self.assertRaisesRegex(RuntimeError, 'outside source/Builds'):
            provider.path(self.builds.parent / 'outside/anything')

    def test_patched_wine_pin_rejects_stock_cross_image(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        provider.provider = 'docker-linux-amd64-wine'
        provider.image = 'cinterforge-terminal-windows-cross:test'
        provider.docker = 'docker'
        for actual in ('wine-8.0', 'wine-11.1', 'wine-11.0-rc5'):
            def probe(command, **kwargs):
                output = actual if command[0] == 'wine' else 'ok'
                return subprocess.CompletedProcess(command, 0, output)
            with mock.patch.object(provider, 'execute', side_effect=probe):
                with self.assertRaisesRegex(RuntimeError, 'requires MSYS2-patched wine-11.15'):
                    provider.version_probes(self.builds)

    def test_patched_wine_pin_accepts_exact_cross_version(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        provider.provider = 'docker-linux-amd64-wine'
        provider.image = 'cinterforge-terminal-windows-cross:test'
        provider.docker = 'docker'
        def probe(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, 'wine-11.15' if command[0] == 'wine' else 'ok')
        with mock.patch.object(provider, 'execute', side_effect=probe):
            receipt = provider.version_probes(self.builds)
        self.assertEqual(receipt['wine']['output'], 'wine-11.15')

    def test_old_host_wine_not_selected_as_pinned_toolchain(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        with mock.patch.object(win.shutil, 'which', return_value='/tmp/fake/wine'), \
             mock.patch.object(win, '_run', return_value=subprocess.CompletedProcess(
                 [], 0, 'wine-8.0')):
            self.assertFalse(provider._native_ready())
        with mock.patch.object(win.shutil, 'which', return_value='/tmp/fake/wine'), \
             mock.patch.object(win, '_run', return_value=subprocess.CompletedProcess(
                 [], 0, 'wine-11.15')):
            self.assertTrue(provider._native_ready())

    def test_subprocess_writes_wine_diagnostics_during_execution(self):
        log = self.builds / 'Logs/CinterForgeTerminal/wine-test.log'
        # The operating system writes directly to the file, rather than a
        # Python PIPE whose contents are lost if wineboot gets interrupted.
        result = win._run([sys.executable, '-c',
                           'import sys; print("WINE_DIAGNOSTIC_MARKER", flush=True)'],
                          quiet=True, output_log=log, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('WINE_DIAGNOSTIC_MARKER', result.stdout)
        self.assertIn('WINE_DIAGNOSTIC_MARKER', log.read_text())

    def test_guest_native_wine_volumes_isolated_and_setup_owned(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        provider.provider = 'docker-linux-amd64-wine'
        provider.docker = 'docker'
        cwd = self.builds / 'Intermediates/CinterForgeTerminal/WindowsCrossSmoke'
        cwd.mkdir(parents=True)
        provider.image = 'cinterforge-terminal-windows-cross:1111111111111111'
        volume1 = provider._wine_volume_name()
        # Build and Publish may not silently provision a missing Wine volume.
        with self.assertRaisesRegex(RuntimeError, 'rerun ./Cinter setup win'):
            provider.execute(['winepath', '-w', '/builds'], cwd=cwd)
        responses = [SimpleNamespace(returncode=1, stdout='not found'),
                     SimpleNamespace(returncode=0, stdout=volume1),
                     SimpleNamespace(returncode=0, stdout='')]
        with mock.patch.object(win, '_run', side_effect=responses) as runner:
            provider.prepare_wine_runtime()
        argv = [call.args[0] for call in runner.call_args_list]
        self.assertIn(['docker', 'volume', 'inspect', volume1], argv)
        self.assertIn('volume', argv[1])
        self.assertIn('create', argv[1])
        self.assertIn('0:0', argv[2])
        self.assertIn(f'type=volume,src={volume1},dst=/cinterwine', argv[2])
        self.assertFalse(provider.wine_prefix_initialized())
        with mock.patch.object(win, '_run', return_value=SimpleNamespace(returncode=0, stdout=r'Z:\builds')) as run:
            provider.execute(['winepath', '-w', '/builds'], cwd=cwd)
        cmd = run.call_args.args[0]
        self.assertIn(f'type=volume,src={volume1},dst=/cinterwine', cmd)
        self.assertIn('WINEPREFIX=/cinterwine/prefix', cmd)
        self.assertIn('HOME=/cinterwine/home', cmd)
        self.assertNotIn('/builds/Run/CinterForgeTerminal/WindowsCross/WinePrefixes/1111111111111111', cmd)
        generation = provider.wine_volume_generation()
        self.assertEqual(len(generation), 32)
        provider.mark_wine_prefix_initialized()
        self.assertTrue(provider.wine_prefix_initialized())
        # A different toolchain image must not inherit the verified prefix.
        provider.image = 'cinterforge-terminal-windows-cross:2222222222222222'
        self.assertNotEqual(volume1, provider._wine_volume_name())
        with self.assertRaisesRegex(RuntimeError, 'rerun ./Cinter setup win'):
            provider.execute(['winepath', '-w', '/builds'], cwd=cwd)
        with mock.patch.object(win, '_run', return_value=SimpleNamespace(returncode=0, stdout='ok')) as run:
            provider.execute(['wine', '--version'], cwd=cwd)
        self.assertNotIn('WINEPREFIX=/cinterwine/prefix', run.call_args.args[0])

    def test_prepared_wine_volume_must_still_exist_in_colima(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        provider.provider = 'docker-linux-amd64-wine'
        provider.image = 'cinterforge-terminal-windows-cross:1111111111111111'
        provider.docker = 'docker'
        with mock.patch.object(win, '_run', return_value=SimpleNamespace(returncode=0, stdout='ok')):
            provider.prepare_wine_runtime()
        new_process = win.WindowsCrossEnvironment(self.builds)
        new_process.provider = provider.provider
        new_process.image = provider.image
        new_process.docker = provider.docker
        with mock.patch.object(win, '_run', return_value=SimpleNamespace(returncode=1, stdout='not found')):
            with self.assertRaisesRegex(RuntimeError, 'unavailable or stale'):
                new_process.execute(['winepath', '-w', '/builds'], cwd=self.builds)

    def test_wine_runtime_requires_boot_before_functional_winepath(self):
        from host import windows_dependencies as dependencies
        cross = mock.Mock()
        cross.wine_prefix_initialized.return_value = False
        cross.execute.side_effect = [
            SimpleNamespace(returncode=0, stdout='initialized'),
            SimpleNamespace(returncode=0, stdout=r'Z:\builds'),
        ]
        result = dependencies._verify_wine_runtime(cross, cwd=self.builds)
        self.assertEqual(result['winepath'], 'executable-verified')
        cross.prepare_wine_runtime.assert_called_once_with()
        cross.mark_wine_prefix_initialized.assert_called_once_with()
        self.assertEqual(cross.execute.call_args_list[0].args[0][-2:], ['wineboot', '--init'])
        self.assertEqual(cross.execute.call_args_list[0].kwargs['timeout'], 180)
        self.assertTrue(str(cross.execute.call_args_list[0].kwargs['diagnostic_log']).endswith('windows-wineboot.log'))
        self.assertEqual(cross.execute.call_args_list[1].args[0], ['winepath', '-w', '/builds'])
        cross.execute.reset_mock()
        cross.execute.side_effect = [SimpleNamespace(returncode=1, stdout='c0000135')]
        with self.assertRaisesRegex(RuntimeError, 'prefix initialization failed'):
            dependencies._verify_wine_runtime(cross, cwd=self.builds)
        self.assertEqual(cross.execute.call_count, 1)
        cross.execute.reset_mock()
        cross.execute.side_effect = [SimpleNamespace(returncode=0, stdout='booted'),
                                     SimpleNamespace(returncode=1, stdout='c0000135')]
        with self.assertRaisesRegex(RuntimeError, 'winepath'):
            dependencies._verify_wine_runtime(cross, cwd=self.builds)
        cross.execute.reset_mock()
        cross.wine_prefix_initialized.return_value = True
        cross.execute.side_effect = [SimpleNamespace(returncode=0, stdout=r'Z:\builds')]
        dependencies._verify_wine_runtime(cross, cwd=self.builds)
        self.assertEqual(cross.execute.call_count, 1)
        self.assertEqual(cross.execute.call_args.args[0][0], 'winepath')

    def test_missing_engine_rejects_cross_environment(self):
        provider = win.WindowsCrossEnvironment(self.builds)
        with mock.patch.object(provider, '_native_ready', return_value=False), \
             mock.patch.object(provider, '_docker_available', side_effect=RuntimeError('engine unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'engine unavailable'):
                provider.ensure()

class WindowsSetupRetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.builds = Path(tmp.name) / 'Builds'
        self.builds.mkdir()
        self.root = self.builds / 'CinterForgeTerminal/Toolchains/Windows/Downloads'
        self.root.mkdir(parents=True)
        self.exe = self.root / f'msys2-x86_64-{dep.MSYS2_VERSION}.exe'
        self.receipt = self.exe.with_suffix('.json')

    def test_valid_existing_installer_reused_without_network(self):
        self.exe.write_bytes(WINDOWS_SETUP_RETRIES_PAYLOAD)
        self.receipt.write_text(json.dumps({'url': dep.MSYS2_INSTALLER_URL,
                                            'sha256': hashlib.sha256(WINDOWS_SETUP_RETRIES_PAYLOAD).hexdigest(),
                                            'size': len(WINDOWS_SETUP_RETRIES_PAYLOAD)}))
        with mock.patch.object(dep.urllib.request, 'urlopen', side_effect=AssertionError('download forbidden')):
            self.assertEqual(dep._download_msys2(self.builds), self.exe)

    def test_orphaned_installer_requires_official_redownload_and_matching_hash(self):
        self.exe.write_bytes(WINDOWS_SETUP_RETRIES_PAYLOAD)
        with mock.patch.object(dep.urllib.request, 'urlopen', return_value=io.BytesIO(WINDOWS_SETUP_RETRIES_PAYLOAD)) as fetch:
            self.assertEqual(dep._download_msys2(self.builds), self.exe)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.exe.read_bytes(), WINDOWS_SETUP_RETRIES_PAYLOAD)
        self.assertEqual(json.loads(self.receipt.read_text())['sha256'], hashlib.sha256(WINDOWS_SETUP_RETRIES_PAYLOAD).hexdigest())
        self.assertFalse(self.exe.with_name(self.exe.name + '.part').exists())

    def test_missing_installer_with_receipt_must_match_original(self):
        expected = hashlib.sha256(WINDOWS_SETUP_RETRIES_PAYLOAD).hexdigest()
        self.receipt.write_text(json.dumps({'url': dep.MSYS2_INSTALLER_URL, 'sha256': expected}))
        with mock.patch.object(dep.urllib.request, 'urlopen', return_value=io.BytesIO(WINDOWS_SETUP_RETRIES_PAYLOAD)):
            self.assertEqual(dep._download_msys2(self.builds), self.exe)
        self.assertEqual(hashlib.sha256(self.exe.read_bytes()).hexdigest(), expected)

    def test_unmatched_orphan_preserved_without_receipt(self):
        self.exe.write_bytes(WINDOWS_SETUP_RETRIES_PAYLOAD)
        with mock.patch.object(dep.urllib.request, 'urlopen', return_value=io.BytesIO(b'MZ' + b'x' * 1100000)):
            with self.assertRaisesRegex(RuntimeError, 'differs from official redownload'):
                dep._download_msys2(self.builds)
        self.assertEqual(self.exe.read_bytes(), WINDOWS_SETUP_RETRIES_PAYLOAD)
        self.assertFalse(self.receipt.exists())

    def test_wrong_source_receipt_is_rejected_before_network(self):
        self.exe.write_bytes(WINDOWS_SETUP_RETRIES_PAYLOAD)
        self.receipt.write_text(json.dumps({'url': 'https://other.example/path',
                                            'sha256': hashlib.sha256(WINDOWS_SETUP_RETRIES_PAYLOAD).hexdigest()}))
        with mock.patch.object(dep.urllib.request, 'urlopen', side_effect=AssertionError('wrong origin')):
            with self.assertRaisesRegex(RuntimeError, 'receipt integrity'):
                dep._download_msys2(self.builds)

    def test_uses_official_base_tarball_instead_of_wine_installer(self):
        """A failed Qt installer must not cause another Wine installer attempt."""
        import tarfile
        base = self.root / 'msys2-base-x86_64-test.tar.xz'
        with tarfile.open(base, 'w:xz') as archive:
            for name in ('pacman.exe', 'bash.exe', 'ls.exe', 'tar.exe'):
                data = b'MZ' + b'pretend binary'
                member = tarfile.TarInfo('msys64/usr/bin/' + name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        root = self.builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
        (root / 'uninstall').mkdir(parents=True)
        (root / 'uninstall/partial.log').write_text('interrupted Qt install')
        msys2._extract_base(base, root)
        self.assertTrue((root / 'usr/bin/pacman.exe').is_file())
        self.assertTrue((root.parent / 'MSYS2-incomplete/uninstall/partial.log').is_file())
        self.assertEqual(json.loads((root / 'base-staged.json').read_text())['sha256'],
                         hashlib.sha256(base.read_bytes()).hexdigest())
        # An interrupted UCRT64 download must not replace a valid base.
        msys2._extract_base(base, root)
        self.assertTrue((root / 'usr/bin/pacman.exe').is_file())

    def test_package_index_resolves_transitive_and_virtual_dependencies(self):
        from host.windows_install import PACKAGES
        dependency_root = PACKAGES[0]
        packages = {
            name: {'depends': ['virtual-msys-library'] if name == dependency_root else [],
                   'provides': []} for name in PACKAGES
        }
        packages['provider'] = {'depends': [], 'provides': ['virtual-msys-library=1']}
        self.assertEqual(msys2._resolve(packages), sorted([*PACKAGES, 'provider']))
        del packages['provider']
        with self.assertRaisesRegex(RuntimeError, 'dependency unavailable'):
            msys2._resolve(packages)

    def test_native_msys_dependency_resolved_and_not_confused_with_ucrt_winpty(self):
        """UCRT64 Node.js requires MSYS winpty, not the UCRT64 winpty variant."""
        from host.windows_install import PACKAGES
        ucrt = {name: {'depends': ['winpty>=0.4.3'] if name.endswith('-nodejs') else [],
                       'provides': []} for name in PACKAGES}
        # This is a different package; it does not provide MSYS `winpty`.
        ucrt['mingw-w64-ucrt-x86_64-winpty'] = {'depends': [], 'provides': []}
        msys = {'winpty': {'depends': [], 'provides': []}}
        self.assertEqual(msys2._resolve(ucrt, msys), sorted([*PACKAGES, 'winpty']))
        with self.assertRaisesRegex(RuntimeError, 'dependency unavailable'):
            msys2._resolve(ucrt)

    def test_repository_database_parsing_checks_sha_and_dependencies(self):
        import tarfile
        database = self.root / 'ucrt64.db'
        package = 'mingw-w64-ucrt-x86_64-gcc'
        desc = ('%NAME%\n' + package + '\n\n%FILENAME%\n' +
                package + '-1.0-1-any.pkg.tar.zst\n\n%SHA256SUM%\n' +
                'a' * 64 + '\n\n%DEPENDS%\nprovider>=1\n')
        with tarfile.open(database, 'w:gz') as archive:
            payload = desc.encode()
            member = tarfile.TarInfo(package + '-1.0-1/desc')
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        parsed = msys2._repo_packages(database)
        self.assertEqual(parsed[package]['depends'], ['provider>=1'])
        self.assertEqual(parsed[package]['sha256'], 'a' * 64)

    def test_ucrt_package_unpack_does_not_run_wine_or_pacman(self):
        from subprocess import CompletedProcess
        packages = [self.root / 'abc.pkg.tar.zst']
        packages[0].write_bytes(b'placeholder')
        called = []
        class FakeCross:
            def path(self, path):
                return '/builds/' + Path(path).relative_to(self_builds).as_posix()
            def execute(self, command, *, cwd, timeout, network=False, graphical=False):
                called.append(command)
                return CompletedProcess(command, 0, '')
        self_builds = self.builds
        msys2._unpack_ucrt(FakeCross(), self.builds, self.builds, packages)
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][0], 'bash')
        self.assertIn('7z x -so', called[0][4])
        self.assertNotIn('wine', ' '.join(called[0]))

class WindowsNativeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cinter-win-native-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builds = self.root / 'Builds'
        self.builds.mkdir()
        self.repo = self.root / 'CinterForgeTerminal'
        self.repo.mkdir()
        self.msys = self.root / 'MSYS2'
        for name in ('ucrt64/bin/cmake.exe', 'ucrt64/bin/ninja.exe',
                     'ucrt64/bin/g++.exe', 'ucrt64/bin/gcc.exe',
                     'usr/bin/bash.exe'):
            p = self.msys / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'mock binary (not acceptance evidence)')
        self.webview = self.root / 'webview'
        self.webview.mkdir()
        self.sdk = self.root / 'sdk'
        self.sdk.mkdir()

    def test_refuses_wine_linux_and_macos_as_windows_acceptance(self):
        for system in ('Darwin', 'Linux'):
            with self.subTest(system=system), mock.patch.object(windows_native.platform, 'system', return_value=system):
                with self.assertRaisesRegex(RuntimeError, 'requires Windows'):
                    windows_native.run(self.builds, repository=self.repo)

    def test_builds_both_pe_targets_and_checks_two_js_callbacks(self):
        called = []
        def execute(cmd, **kw):
            called.append(cmd)
            if '--build' in cmd:
                out = self.builds / 'Intermediates/CinterForgeTerminal/WindowsNative/x64/cmake'
                (out / 'CinterBootstrapSample.exe').write_bytes(b'MZsample')
                (out / 'CinterForgeTerminal.exe').write_bytes(b'MZterminal')
                return 'built'
            if '--probe' in cmd:
                return ('CINTER_SAMPLE_BEGIN platform=windows\n'
                        'windows-native-marker.txt\n'
                        'CINTER_SAMPLE_DIRECTORY_PASS platform=windows\n')
            if '--smoke-js' in cmd:
                return ('WEBVIEW_JS_PASS result=4 callback=native\n' if 'CinterForgeTerminal.exe' in str(cmd[0])
                        else 'CINTER_SAMPLE_WEBVIEW_JS_PASS result=4\n')
            return 'tool version'
        with mock.patch.object(windows_native.platform, 'system', return_value='Windows'), \
             mock.patch('host.toolchain.stage_windows_msys2', return_value=self.msys), \
             mock.patch('host.toolchain.msys2_env', return_value={}), \
             mock.patch('host.windows_webview2_runtime.ensure', return_value={'version':'fixture', 'runtime_path':str(self.builds / 'Toolchains/WebView2/FixedRuntime/x64/fixture')}), \
             mock.patch('host.webview.provision', return_value=self.webview), \
             mock.patch('host.webview.provision_webview2', return_value=self.sdk), \
             mock.patch('host.windows_runtime_dlls.stage_native', return_value={}), \
             mock.patch.object(windows_native, '_run', side_effect=execute):
            result = windows_native.run(self.builds, repository=self.repo)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['execution'], 'native-windows')
        self.assertEqual(result['webview_js'], 'passed')
        self.assertTrue(any('--target' in cmd and 'cinter-terminal' in cmd for cmd in called))
        self.assertEqual(len([cmd for cmd in called if '--smoke-js' in cmd]), 2)
        receipt = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-native-webview.json').read_text())
        self.assertEqual(receipt['status'], 'passed')

    def test_missing_native_js_callback_is_hard_failure_with_receipt(self):
        def execute(cmd, **kw):
            if '--build' in cmd:
                out = self.builds / 'Intermediates/CinterForgeTerminal/WindowsNative/x64/cmake'
                (out / 'CinterBootstrapSample.exe').write_bytes(b'MZsample')
                (out / 'CinterForgeTerminal.exe').write_bytes(b'MZterminal')
            if '--probe' in cmd:
                return ('CINTER_SAMPLE_BEGIN platform=windows\nwindows-native-marker.txt\n'
                        'CINTER_SAMPLE_DIRECTORY_PASS platform=windows\n')
            return 'not a real callback'
        with mock.patch.object(windows_native.platform, 'system', return_value='Windows'), \
             mock.patch('host.toolchain.stage_windows_msys2', return_value=self.msys), \
             mock.patch('host.toolchain.msys2_env', return_value={}), \
             mock.patch('host.windows_webview2_runtime.ensure', return_value={'version':'fixture', 'runtime_path':str(self.builds / 'Toolchains/WebView2/FixedRuntime/x64/fixture')}), \
             mock.patch('host.webview.provision', return_value=self.webview), \
             mock.patch('host.webview.provision_webview2', return_value=self.sdk), \
             mock.patch('host.windows_runtime_dlls.stage_native', return_value={}), \
             mock.patch.object(windows_native, '_run', side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, 'never reported PASS'):
                windows_native.run(self.builds, repository=self.repo)
        receipt = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-native-webview.json').read_text())
        self.assertEqual(receipt['status'], 'failed')

class MSYS2ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cinter-msys2-')
        self.addCleanup(self.tmp.cleanup)
        self.builds = Path(self.tmp.name) / 'Builds'
        self.builds.mkdir()

    def test_refuses_non_windows(self):
        with mock.patch.object(windows_install.platform, 'system', return_value='Linux'):
            with self.assertRaisesRegex(RuntimeError, 'native Windows'):
                windows_install.ensure(self.builds)

    def test_reuses_full_installed_closure_without_download(self):
        root = self.builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
        for name in windows_install.REQUIRED:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        with mock.patch.object(windows_install.platform, 'system', return_value='Windows'), \
             mock.patch.object(windows_install.urllib.request, 'urlopen', side_effect=AssertionError('no download')):
            self.assertEqual(windows_install.ensure(self.builds), root)

    def test_stages_official_installer_and_ucrt64_packages(self):
        downloaded = []
        environments = []
        root = self.builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
        installer = self.builds / ('CinterForgeTerminal/Toolchains/Windows/Downloads/' +
                                   f'msys2-x86_64-{windows_install.MSYS2_VERSION}.exe')
        installer.parent.mkdir(parents=True)
        installer.write_bytes(b'fixture downloaded vendor installer')
        def simulated_execute(command, **kwargs):
            downloaded.append(command)
            environments.append(kwargs.get('env'))
            if 'in' in command:
                p = root / 'usr/bin/pacman.exe'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'fixture')
                (root / 'usr/bin/bash.exe').write_bytes(b'fixture')
            if '-S' in command:
                for name in windows_install.REQUIRED:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'fixture')
        with mock.patch.object(windows_install.platform, 'system', return_value='Windows'), \
             mock.patch.object(windows_install, '_execute', side_effect=simulated_execute):
            self.assertEqual(windows_install.ensure(self.builds), root)
        self.assertIn('--root', downloaded[0])
        self.assertIn('-Syu', downloaded[-2])
        self.assertIn('--needed', downloaded[-1])
        self.assertIn('mingw-w64-ucrt-x86_64-gcc', downloaded[-1])
        self.assertIn(str(root / 'ucrt64/bin'), environments[-1]['PATH'])
        self.assertEqual(environments[-1]['MSYSTEM'], 'UCRT64')
        receipt = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-msys2-setup.json').read_text())
        self.assertEqual(receipt['status'], 'passed')

    def test_corrupt_partial_install_fails_without_erasing(self):
        root = self.builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
        root.mkdir(parents=True)
        (root / 'other').write_text('keep me')
        with mock.patch.object(windows_install.platform, 'system', return_value='Windows'):
            with self.assertRaisesRegex(RuntimeError, 'incomplete MSYS2'):
                windows_install.ensure(self.builds)
        self.assertEqual((root / 'other').read_text(), 'keep me')

class WindowsWebViewRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.builds = Path(self.tmp.name) / 'Builds'
        self.builds.mkdir()
        self.browser_folder = self.builds / 'Toolchains/WebView2/FixedRuntime/x64/test'
        self.browser_folder.mkdir(parents=True)

    def test_native_windows_reuses_staged_fixed_browser_without_registry_or_installer(self):
        (self.browser_folder / 'msedgewebview2.exe').write_bytes(b'MZbrowser-fixture')
        with mock.patch.object(runtime.platform, 'system', return_value='Windows'), \
             mock.patch('host.windows_fixed_runtime.ensure', return_value=self.browser_folder), \
             mock.patch.object(runtime, 'installed_version', side_effect=AssertionError('registry forbidden')), \
             mock.patch.object(runtime.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')), \
             mock.patch.dict(os.environ, {}, clear=True):
            receipt = runtime.ensure(self.builds)
            self.assertEqual(os.environ['WEBVIEW2_BROWSER_EXECUTABLE_FOLDER'], str(self.browser_folder))
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['execution'], 'installed-files-verified-not-GUI-acceptance')
        written = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-webview2-runtime.json').read_text())
        self.assertEqual(written['status'], 'passed')

    def test_fixed_browser_missing_executable_fails_closed(self):
        with mock.patch.object(runtime.platform, 'system', return_value='Windows'), \
             mock.patch('host.windows_fixed_runtime.ensure', return_value=self.browser_folder):
            with self.assertRaisesRegex(RuntimeError, 'not actually staged'):
                runtime.ensure(self.builds)
        self.assertFalse((self.builds / 'Logs/CinterForgeTerminal/windows-webview2-runtime.json').exists())

    def test_fixed_browser_provision_failure_cannot_claim_success(self):
        with mock.patch.object(runtime.platform, 'system', return_value='Windows'), \
             mock.patch('host.windows_fixed_runtime.ensure', side_effect=RuntimeError('invalid CAB hash')):
            with self.assertRaisesRegex(RuntimeError, 'invalid CAB hash'):
                runtime.ensure(self.builds)
        self.assertFalse((self.builds / 'Logs/CinterForgeTerminal/windows-webview2-runtime.json').exists())

    def test_rejects_non_windows(self):
        with mock.patch.object(runtime.platform, 'system', return_value='Linux'):
            with self.assertRaisesRegex(RuntimeError, 'Windows runtime acceptance'):
                runtime.ensure(self.builds)

class WindowsWineWebViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.builds = self.workspace / 'Builds'
        self.builds.mkdir()
        self.repo = Path(__file__).resolve().parents[1]

    def test_graphical_wine_uses_xvfb_inside_offline_container(self):
        executor = WindowsCrossEnvironment(self.builds, repository=self.repo)
        executor.provider = 'docker-linux-amd64-wine'
        executor.image = 'cinterforge-terminal-windows-cross:test'
        executor.docker = '/usr/bin/docker'
        cwd = self.builds / 'Intermediates/Windows'
        cwd.mkdir(parents=True)
        pe = cwd / 'sample.exe'
        pe.write_bytes(b'MZ')
        with mock.patch('host.windows_environment._run', return_value=SimpleNamespace(returncode=0, stdout='PASS')) as run:
            executor.prepare_wine_runtime()
            result = executor.execute(['wine', executor.path(pe), '--smoke-js'], cwd=cwd, graphical=True)
        args = run.call_args.args[0]
        self.assertIn('none', args)
        from host.gui_preflight import IMAGE_SCRIPT
        self.assertEqual(args[-9:], ['python3', '-u', IMAGE_SCRIPT, '--platform', 'windows', '--',
                                     'wine', executor.path(pe), '--smoke-js'])
        self.assertEqual(result.returncode, 0)

    def test_cross_build_refuses_webview_missing_js_callback(self):
        stdout = 'WebView initialized but JavaScript did not call native host'
        with self.assertRaisesRegex(RuntimeError, 'JS callback did not reach'):
            with tempfile.TemporaryFile(mode='w+') as log:
                with mock.patch.object(cross_build, '_run', return_value=stdout) as run:
                    js = cross_build._run(['wine', 'terminal.exe', '--smoke-js'],
                                          self.repo, log, object(), graphical=True)
                    if 'WEBVIEW_JS_PASS result=4 callback=native' not in js:
                        raise RuntimeError('Windows WebView JS callback did not reach native C++ under Wine')
                self.assertTrue(run.call_args.kwargs['graphical'])




class PackagedWindowsBashTests(unittest.TestCase):
    """Windows app must ship its own native pywinpty binding and GUI launcher."""

    def test_installs_verified_winpty_wheel_in_standalone_python(self):
        import zipfile
        from host import windows_winpty
        TOOLCHAIN_SIGN_OF_LIFE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='cinter-winpty-', dir=TOOLCHAIN_SIGN_OF_LIFE_ROOT) as folder:
            base = Path(folder)
            builds = base / 'Builds'
            builds.mkdir()
            root = builds / 'Intermediates/CinterForgeTerminal/ForeignPython/win/x64/3.11.18'
            root.mkdir(parents=True)
            (root/'python.exe').write_bytes(b'MZ-Windows-CPython')
            wheel = builds / 'Intermediates/CinterForgeTerminal/PythonPackages' / windows_winpty.WHEEL
            wheel.parent.mkdir(parents=True)
            with zipfile.ZipFile(wheel, 'w') as z:
                z.writestr('winpty/__init__.py', 'class PtyProcess: pass\n')
                z.writestr('winpty/pty.cp311-win_amd64.pyd', b'MZ-sample')
                z.writestr('pywinpty-2.0.15.dist-info/WHEEL', b'Wheel-Version: 1.0\n')
            with mock.patch.object(windows_winpty, '_fetch', return_value=wheel):
                windows_winpty.ensure(builds, root)
            windows_winpty.verify(root)
            self.assertTrue((root/'Lib/site-packages/winpty/__init__.py').is_file())
            with mock.patch.object(windows_winpty, '_fetch', side_effect=AssertionError('no redownload')):
                windows_winpty.ensure(builds, root)
            (root/'Lib/site-packages/winpty/__init__.py').write_text('tampered')
            with self.assertRaisesRegex(RuntimeError, 'modified or incomplete'):
                windows_winpty.verify(root)



class ManagedGuestBashSocketTests(unittest.TestCase):
    def test_nested_mac_app_builds_uses_shortest_workspace_builds(self):
        from webapp import CinterForgeTerminal as devtools_shell
        TOOLCHAIN_SIGN_OF_LIFE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='cft-vm-', dir=TOOLCHAIN_SIGN_OF_LIFE_ROOT) as tmp:
            outer = Path(tmp) / 'Builds'
            nested = outer / 'CinterForgeTerminal/Applications/mac/Release/CinterForge Terminal.app/Contents/Resources/Builds'
            nested.mkdir(parents=True)
            # Model a short installed workspace without creating VM state outside Builds.
            # The real runtime still checks full physical socket lengths.
            actual_encode = os.fsencode
            def encoded_short_workspace(path):
                return actual_encode(str(path).replace(str(Path(tmp)), '/test'))
            with mock.patch.dict(os.environ, {'CINTER_BUILDS_ROOT': str(nested),
                                              'CINTER_USER_HOME': tmp}), \
                 mock.patch.object(devtools_shell.os, 'fsencode', side_effect=encoded_short_workspace):
                state = devtools_shell._state_root()
            self.assertEqual(state, TOOLCHAIN_SIGN_OF_LIFE_ROOT.parents[1] / 'CFTVM/T')
            self.assertLess(len(encoded_short_workspace(state/'l/colima-t/ssh.sock.1234567890123456')), 100)
