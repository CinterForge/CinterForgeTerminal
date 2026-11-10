"""Focused windows regression contracts. Native target acceptance is separate."""
from tests._support import *

WINDOWS_SETUP_RETRIES_PAYLOAD = b'MZ' + b'official-installer-test' * 55000

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
            result = executor.execute(['wine', executor.path(pe), '--smoke-js'], cwd=cwd, graphical=True)
        args = run.call_args.args[0]
        self.assertIn('none', args)
        self.assertEqual(args[-6:], ['xvfb-run', '-a', '--server-args=-screen 0 1280x800x24',
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

    def test_webview_watchdog_terminates_on_gui_thread(self):
        source = (self.repo / 'src/CinterApplicationShell.cpp').read_text()
        self.assertIn('window.dispatch([&window] { window.terminate(); });', source)
        self.assertNotIn('if (!completed.load()) window.terminate();', source)

    def test_wine_cross_build_does_not_require_graphical_webview(self):
        source = (self.repo / 'host/cross_build.py').read_text()
        samples = (self.repo / 'host/three_samples.py').read_text()
        dockerfile = (self.repo / 'host/docker/windows-cross.Dockerfile').read_text()
        self.assertIn("cross.path(exe), '--help'", source)
        self.assertNotIn('graphical=True', source)
        self.assertNotIn("cross.path(pe), '--smoke-js'", samples)
        self.assertIn('not-run-native-windows-required', samples)
        self.assertIn('xvfb xauth', dockerfile)


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

    def test_build_verifies_winpty_and_uses_gui_subsystem(self):
        from host import windows_winpty
        source = (Path(__file__).resolve().parents[1]/'CMakeLists.txt').read_text()
        self.assertIn('WIN32_EXECUTABLE TRUE', source)
        winmain = (Path(__file__).resolve().parents[1]/'src/cinterforge_terminal_main.cpp').read_text()
        self.assertIn('int WINAPI WinMain(', winmain)
        winpy = (Path(__file__).resolve().parents[1]/'host/universal_package.py').read_text()
        self.assertIn('verify_winpty(runtime)', winpy)
        self.assertEqual(windows_winpty.VERSION, '2.0.15')


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
