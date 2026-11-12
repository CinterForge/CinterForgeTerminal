"""Focused setup regression contracts. Native target acceptance is separate."""
from tests._support import *

class BootstrapBundleTests(unittest.TestCase):
    def setUp(self):
        root = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=root, prefix='bundle-'))
        self.builds = self.tmp / 'Builds'
        self.builds.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_setup_all_requires_managed_target_runners(self):
        with self.assertRaisesRegex(RuntimeError, 'setup all is incomplete'):
            bootstrap.setup_all(self.builds)

    def test_no_bundle_from_unverified_target(self):
        with self.assertRaisesRegex(RuntimeError, 'no real bootstrap smoke receipt'):
            bootstrap.bundle_target(self.builds, 'win', 'x64')

    def test_invalid_smoke_receipt_cannot_authorize_bundle(self):
        path = bootstrap._receipt(self.builds, 'linux', 'x64')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema': bootstrap.SMOKE_SCHEMA,
                                    'platform': 'linux', 'arch': 'x64',
                                    'release': bootstrap.RELEASE, 'http': {'http_health':'passed'},
                                    'toolchain': {'status':'failed'},
                                    'bedrock': {'bedrock':'not-smoked'}}))
        with self.assertRaisesRegex(RuntimeError, 'incomplete bootstrap smoke receipt'):
            bootstrap.bundle_target(self.builds, 'linux', 'x64')

    def test_manifest_validation_detects_modification_and_untracked_files(self):
        bundle = self.builds / 'CinterForgeTerminal/Bundles/linux/x64/' / bootstrap.RELEASE
        bundle.mkdir(parents=True)
        file = bundle / 'Host/hello.txt'
        file.parent.mkdir()
        file.write_text('verified')
        (bundle / 'manifest.json').write_text(json.dumps({
            'schema': bootstrap.SCHEMA, 'files': {'Host/hello.txt': bootstrap._sha(file)},
        }))
        self.assertEqual(bootstrap.verify_bundle(bundle)['schema'], bootstrap.SCHEMA)
        (bundle / 'rogue.txt').write_text('untracked')
        with self.assertRaisesRegex(RuntimeError, 'tampered or incomplete'):
            bootstrap.verify_bundle(bundle)

    def test_runtime_symlink_rejected(self):
        root = self.builds / 'Toolchains/PowerShell/mac/arm64'
        root.mkdir(parents=True)
        (root / 'pwsh').symlink_to('/bin/sh')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            bootstrap._checked_tree(root)

    def test_app_injection_does_not_mutate_existing_files(self):
        bundle = self.builds / 'CinterForgeTerminal/Bundles/linux/x64' / bootstrap.RELEASE
        (bundle / 'Host').mkdir(parents=True)
        resource = bundle / 'Host/service.py'
        resource.write_text('print(123)')
        (bundle / 'manifest.json').write_text(json.dumps({
            'schema': bootstrap.SCHEMA, 'platform': 'linux', 'arch': 'x64',
            'release': bootstrap.RELEASE, 'files': {'Host/service.py': bootstrap._sha(resource)},
        }))
        app = self.builds / 'CreativeSuite/Staged/Example/linux/2026'
        app.mkdir(parents=True)
        (app / 'index.html').write_text('<h1>Existing app</h1>')
        destination = bootstrap.inject_bundle(self.builds, bundle, app)
        self.assertTrue((destination / 'Host/service.py').is_file())
        self.assertEqual((app / 'index.html').read_text(), '<h1>Existing app</h1>')
        self.assertEqual(bootstrap.inject_bundle(self.builds, bundle, app), destination)
        (destination / 'Host/service.py').write_text('tampered')
        with self.assertRaises(RuntimeError):
            bootstrap.inject_bundle(self.builds, bundle, app)

class MacBootstrapContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.builds = Path(self.tmp.name) / 'Builds'
        self.builds.mkdir()


    def test_sample_probe_requires_real_platform_and_listing(self):
        output = ('CINTER_SAMPLE_BEGIN platform=windows\n'
                  'CINTER_SAMPLE_CWD C:\\Builds\n'
                  'CINTER_SAMPLE_DIR file.txt\n'
                  'CINTER_SAMPLE_DIRECTORY_PASS platform=windows\n')
        three_samples._assert_probe(output, 'windows')
        with self.assertRaisesRegex(RuntimeError, 'linux sample'):
            three_samples._assert_probe(output, 'linux')
        with self.assertRaisesRegex(RuntimeError, 'windows sample'):
            three_samples._assert_probe('CINTER_SAMPLE_DIRECTORY_PASS platform=windows', 'windows')


    def test_sample_command_streams_and_records_real_output(self):
        log = self.builds / 'Logs/sample.log'
        result = three_samples._exec([sys.executable, '-c',
            "print('CINTER_SAMPLE_BEGIN platform=linux', flush=True)"],
            cwd=self.builds, logfile=log, timeout=5)
        self.assertIn('CINTER_SAMPLE_BEGIN platform=linux', result)
        self.assertIn('EXIT=0', log.read_text())

    def test_stalled_webview_is_killed_and_logged_not_accepted(self):
        log = self.builds / 'Logs/stalled.log'
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            three_samples._exec([sys.executable, '-c',
                'import time; time.sleep(60)'], cwd=self.builds,
                logfile=log, timeout=0.2)
        self.assertIn('TIMEOUT=0.2s', log.read_text())

    def test_linux_runner_is_actual_guest_bootstrap_not_fake_receipt(self):
        log = self.builds / 'Logs/linux-runner.log'
        with (mock.patch.object(linux_runner, '_docker_image', return_value='test-linux-image:123'),
              mock.patch.object(linux_runner, '_download_verified')):
            runner = linux_runner.stage(self.builds, docker=sys.executable, logfile=log)
        script = runner.read_text()
        self.assertIn('xvfb-run -a python3 -B "$GUEST_SCRIPT"', script)
        self.assertIn('--builds /builds --setup-local', script)
        self.assertIn('--network none', script)
        self.assertNotIn('CINTER_SAMPLE_WEBVIEW_JS_PASS', script)
        self.assertTrue(runner.stat().st_mode & 0o111)
        # A runner with changed inputs cannot silently replace the live stage.
        with mock.patch.object(linux_runner, '_docker_image', return_value='different-image'):
            with mock.patch.object(linux_runner, '_download_verified'):
                with self.assertRaisesRegex(RuntimeError, 'differs'):
                    linux_runner.stage(self.builds, docker=sys.executable, logfile=log)

    def test_linux_native_runner_rebuilds_only_stale_cmake_and_shows_real_errors(self):
        # A changed image invalidates only the Linux native CMake directory;
        # a failed compiler command exposes its diagnostic on the main console.
        log = self.builds / 'Logs/linux-native-build.log'
        source = self.builds / 'Toolchains/WebView/0.12.0/source'
        source.mkdir(parents=True)
        output = self.builds / 'Intermediates/CinterForgeTerminal/LinuxNative/x64/cmake'
        elf = output / 'CinterForgeTerminal'
        image = ['test-linux:1']
        def fake_run(command, **kwargs):
            if command[:2] == ['cmake', '-S']:
                # The OCI bind mount's host/container timestamp mismatch can
                # otherwise make Ninja rerun CMake 100 times without building.
                self.assertIn('-DCMAKE_SUPPRESS_REGENERATION=ON', command)
            if '--build' in command:
                elf.write_bytes(b'\x7fELF' + b'linux-native-fixture')
            return SimpleNamespace(stdout='configured\n', stderr='', returncode=0)
        with (mock.patch.object(linux_runner, '_docker_image', side_effect=lambda *a: image[0]),
              mock.patch('host.webview.provision', return_value=source),
              mock.patch.object(linux_runner.subprocess, 'run', side_effect=fake_run)):
            self.assertEqual(linux_runner.build_terminal(self.builds, docker='docker', logfile=log), elf)
            self.assertTrue((output / 'linux-native-inputs.json').is_file())
            retained = output / 'retained-marker'
            retained.write_text('same toolchain')
            linux_runner.build_terminal(self.builds, docker='docker', logfile=log)
            self.assertTrue(retained.is_file())
            image[0] = 'test-linux:2'
            linux_runner.build_terminal(self.builds, docker='docker', logfile=log)
            self.assertFalse(retained.exists())
        def failed(command, **kwargs):
            return SimpleNamespace(stdout='', stderr='fatal error: actual Linux compiler error\n', returncode=17)
        with (mock.patch.object(linux_runner, '_docker_image', return_value=image[0]),
              mock.patch('host.webview.provision', return_value=source),
              mock.patch.object(linux_runner.subprocess, 'run', side_effect=failed),
              contextlib.redirect_stdout(io.StringIO()) as printed):
            with self.assertRaisesRegex(RuntimeError, r'failed \(17\)'):
                linux_runner.build_terminal(self.builds, docker='docker', logfile=log)
            self.assertIn('actual Linux compiler error', printed.getvalue())
            self.assertIn('EXIT=17', log.read_text())
        self.assertTrue((self.builds / 'Toolchains').exists())

    def test_linux_runner_translates_mac_paths_into_guest(self):
        fake = self.builds / 'fake-docker'
        fake.write_text('#!/bin/sh\nprintf "ARG:%s\\n" "$@"\n')
        fake.chmod(0o755)
        log = self.builds / 'Logs/linux-runner.log'
        with (mock.patch.object(linux_runner, '_docker_image', return_value='test-image:fixed'),
              mock.patch.object(linux_runner, '_download_verified')):
            runner = linux_runner.stage(self.builds, docker=str(fake), logfile=log)
        bootstrap = self.builds / 'CinterForgeTerminal/Bootstrap/Runner/release/bootstrap_runner.py'
        result = subprocess.run([str(runner), str(bootstrap), '--builds', str(self.builds),
                                 '--setup-local'], capture_output=True, text=True, check=True)
        self.assertIn('ARG:/builds/CinterForgeTerminal/Bootstrap/Runner/release/bootstrap_runner.py', result.stdout)
        self.assertIn('ARG:/builds', result.stdout)
        self.assertNotIn('ARG:' + str(self.builds), result.stdout)
        self.assertIn('ARG:test-image:fixed', result.stdout)
        denied = subprocess.run([str(runner), '/etc/passwd', '--builds', str(self.builds),
                                 '--setup-local'], capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)


    def test_mac_only_all_three_samples_fails_closed_elsewhere(self):
        with mock.patch.object(three_samples.platform, 'system', return_value='Linux'):
            with self.assertRaisesRegex(RuntimeError, 'Mac-only'):
                three_samples.run(self.builds)

class SetupPreflightOrderingTests(unittest.TestCase):
    """Users run only ./Cinter setup all; diagnosis must be its first action."""

    def setUp(self):
        # Setup download/extraction is separately tested with fixture manifests;
        # these ordering tests must not attempt public network access.
        from host import unpacked_release
        patcher = mock.patch.object(unpacked_release, 'setup_restore_if_needed',
                                    return_value={'source':'fixture-provisioned','downloaded':False})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def cli_module():
        import importlib.machinery
        import importlib.util
        path = Path(__file__).resolve().parents[1] / 'Cinter'
        loader = importlib.machinery.SourceFileLoader('cinter_preflight_test', str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_setup_all_prepares_dependencies_before_guest_and_acceptance(self):
        cli = self.cli_module()
        from host import portable_python, universal_package, bootstrap_bundle
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(cli, 'BUILDS', Path(directory) / 'Builds'), \
             mock.patch.object(cli.platform, 'system', return_value='Linux'), \
             mock.patch.object(cli, 'invoke') as call, \
             mock.patch.object(portable_python, 'provision'), \
             mock.patch.object(universal_package, 'provision_release_runtimes'), \
             mock.patch.object(bootstrap_bundle, 'provision_zmachine'):
            self.assertEqual(cli.main(['setup', 'all']), 0)
        invoked = [' '.join(map(str, c.args)) for c in call.call_args_list]
        self.assertIn('host.windows_dependencies', invoked[0])
        self.assertIn('host.devtooling', invoked[1])
        guest = next(i for i,v in enumerate(invoked) if 'linuxos/lifecycle.py setup' in v)
        accept = next(i for i,v in enumerate(invoked) if 'linuxos/lifecycle.py test' in v)
        self.assertLess(guest, accept)
        self.assertFalse(any('build mac' in x or 'build win' in x for x in invoked))

    def test_mac_setup_prepares_everything_before_release_build(self):
        cli = self.cli_module()
        from host import portable_python, universal_package, bootstrap_bundle, mac_bootstrap
        sequence = []
        def invoke(*args):
            sequence.append(' '.join(map(str, args)))
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(cli, 'BUILDS', Path(directory) / 'Builds'), \
             mock.patch.object(cli, 'invoke', side_effect=invoke), \
             mock.patch.object(cli.platform, 'system', return_value='Darwin'), \
             mock.patch.object(mac_bootstrap, 'managed_environment', return_value={}), \
             mock.patch.object(mac_bootstrap, 'stop_linuxkit_build_backend',
                               side_effect=lambda builds: sequence.append('stop-owned-c')), \
             mock.patch.object(portable_python, 'provision'), \
             mock.patch.object(universal_package, 'provision_release_runtimes'), \
             mock.patch.object(bootstrap_bundle, 'provision_zmachine'):
            self.assertEqual(cli.main(['setup', 'all']), 0)
        invoked = [x for x in sequence if x not in ('stop-owned-w', 'stop-owned-c')]
        self.assertIn('host.mac_bootstrap', invoked[0])
        self.assertIn('host.windows_dependencies', invoked[1])
        self.assertIn('host.devtooling', invoked[2])
        guest = next(x for x in sequence if 'linuxos/lifecycle.py setup' in x)
        accept = next(x for x in sequence if 'linuxos/lifecycle.py test' in x)
        # w is released within LinuxKit Setup, *after* its custom-kernel
        # producer used Docker, not before the kernel has been constructed.
        self.assertLess(sequence.index(guest), sequence.index('stop-owned-c'))
        self.assertLess(sequence.index('stop-owned-c'), sequence.index(accept))
        self.assertFalse(any('host.universal_package build' in x or 'build win' in x for x in invoked))

    def test_all_all_setup_pulls_lagomorph_internal_source_before_staging(self):
        cli = self.cli_module()
        from host import portable_python, universal_package, bootstrap_bundle
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(cli, 'BUILDS', Path(directory) / 'Builds'), \
             mock.patch.object(cli.platform, 'system', return_value='Linux'), \
             mock.patch.object(cli, 'invoke') as invoke, \
             mock.patch.object(portable_python, 'provision'), \
             mock.patch.object(universal_package, 'provision_release_runtimes'), \
             mock.patch.object(bootstrap_bundle, 'provision_zmachine'):
            self.assertEqual(cli.main(['setup', 'all']), 0)
            calls = [' '.join(map(str, c.args)) for c in invoke.call_args_list]
        pull = next(i for i, command in enumerate(calls) if 'pull lagomorph source' in command)
        stage = next(i for i, command in enumerate(calls) if 'host.lagomorph_setup' in command)
        self.assertLess(pull, stage)
        self.assertEqual(sum('pull lagomorph source' in call for call in calls), 1)
        self.assertFalse(any(' publish ' in (' ' + call + ' ') for call in calls))

        # 'all all' is the standard orchestrator; it MUST transit Setup,
        # not independently acquire/source artwork during Build or Stage.
        with mock.patch.object(cli, 'invoke') as invoke:
            self.assertEqual(cli.main(['all', 'all']), 0)
        self.assertEqual([tuple(x.args[-2:]) for x in invoke.call_args_list],
                         [('setup', 'all'), ('build', 'all'),
                          ('stage', 'all'), ('test', 'all')])

    def test_test_all_runs_available_automated_checks_without_launch(self):
        cli = self.cli_module()
        from host import universal_package
        report = {'complete_build_receipt': True, 'complete_stage_receipt': True,
                  'steam_destination': '/SteamLibrary/steamapps/common/CinterForge/Applications',
                  'targets': {target: {'build_present': True, 'steam_app_root_present': True}
                              for target in universal_package.FOUR_TARGETS}}
        with mock.patch.object(cli.platform, 'system', return_value='Darwin'), \
             mock.patch.object(cli, 'invoke') as invoke, \
             mock.patch.object(universal_package, 'status_four', return_value=report):
            self.assertEqual(cli.main(['test', 'all']), 0)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(tuple(invoke.call_args.args[-2:]), ('test', 'mac'))
        self.assertNotIn('launch', invoke.call_args.args)

    def test_test_all_refuses_missing_stage_receipts(self):
        cli = self.cli_module()
        from host import universal_package
        report = {'complete_build_receipt': True, 'complete_stage_receipt': False,
                  'steam_destination': '/some/library', 'targets': {}}
        with mock.patch.object(cli.platform, 'system', return_value='Darwin'), \
             mock.patch.object(cli, 'invoke'), \
             mock.patch.object(universal_package, 'status_four', return_value=report):
            with self.assertRaisesRegex(RuntimeError, 'four built and staged'):
                cli.main(['test', 'all'])

    def test_missing_tool_fails_setup_before_expensive_build(self):
        cli = self.cli_module()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(cli, 'BUILDS', Path(directory) / 'Builds'), \
             mock.patch.object(cli.platform, 'system', return_value='Linux'), \
             mock.patch.object(cli, 'invoke', side_effect=RuntimeError('cmake: missing')) as call:
            with self.assertRaisesRegex(RuntimeError, 'cmake: missing'):
                cli.main(['setup', 'all'])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[3], 'host.windows_dependencies')

class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.builds = Path(self.temp.name) / 'Builds'
        self.root = self.builds / 'Python'
        self.root.mkdir(parents=True)

    def test_environment_name_rejects_traversal(self):
        with self.assertRaises(ValueError):
            manager.environment_dir(self.builds, '../CinterForgeServer')

    def test_manifest_must_be_staged_inside_builds(self):
        outside = Path(self.temp.name) / 'projects.json'
        outside.write_text(json.dumps({'schema':manager.SCHEMA,'environments':{},'services':{}}))
        with self.assertRaises(ValueError):
            manager.read_projects(self.builds, outside)

    def test_platform_specific_requirements_selection(self):
        for name in ('common.txt', 'mac.txt', 'win.txt'):
            (self.root / name).write_text(name)
        profile = {'requirements':{'common':'common.txt','mac':'mac.txt','win':'win.txt'}}
        self.assertEqual([p.name for p in manager.requirements(self.builds, profile, 'mac')],
                         ['common.txt', 'mac.txt'])
        self.assertEqual([p.name for p in manager.requirements(self.builds, profile, 'win')],
                         ['common.txt', 'win.txt'])

    def test_requirements_must_not_use_sibling_source(self):
        with self.assertRaises(ValueError):
            manager.requirements(self.builds, {'requirements':{'common':'../CinterForgeServer/requirements.txt'}}, 'mac')

    def test_receipt_fingerprint_tracks_platform_and_content(self):
        f = self.root / 'requirements.txt'
        f.write_text('one==1.0\n')
        before = manager.requirements_fingerprint([f], '3.10.0', 'mac')
        self.assertNotEqual(before, manager.requirements_fingerprint([f], '3.10.0', 'win'))
        f.write_text('one==2.0\n')
        self.assertNotEqual(before, manager.requirements_fingerprint([f], '3.10.0', 'mac'))

    def test_adopt_preserves_existing_environment_if_destination_exists(self):
        old = self.builds / 'cinterenv'
        old.mkdir(parents=True)
        dest = manager.environment_dir(self.builds, 'cinterenv')
        dest.mkdir(parents=True)
        self.assertFalse(manager.adopt_legacy(self.builds, dest, '3.10.0'))
        self.assertTrue(old.is_dir())

    def test_does_not_install_unlisted_empty_environment(self):
        with self.assertRaises(ValueError):
            manager.setup_environment(self.builds, 'converter', {'python':'3.10.0'})

    def test_service_uses_only_staged_code(self):
        service = self.root / 'Services/server/server.py'
        service.parent.mkdir(parents=True)
        service.write_text('print("ok")\n')
        env = manager.environment_dir(self.builds, 'cinterenv')
        exe = manager.python_executable(env)
        exe.parent.mkdir(parents=True)
        exe.write_text('executable')
        info = {'environments':{'cinterenv':{}},
                'services':{'server':{'environment':'cinterenv', 'script':'Services/server/server.py'}}}
        command, cwd = manager.service_command(self.builds, info, 'server')
        self.assertEqual(command[2], str(service))
        self.assertEqual(cwd, service.parent)

    def test_zeroconf_symbol_check_rejects_undefined_reference(self):
        nm_result = mock.Mock(returncode=0, stdout=(
            '                 U _czc_last_create_error\n'
            '0000000000000030 T _czc_create\n'), stderr='')
        with mock.patch.object(manager.shutil, 'which', return_value='/usr/bin/nm'), \
             mock.patch.object(manager.subprocess, 'run', return_value=nm_result):
            archive = self.root / 'libCinterForgePythonHost.a'
            archive.write_bytes(b'archive fixture')
            self.assertFalse(manager._defined_c_symbol(archive, 'czc_last_create_error'))
            nm_result.stdout += '0000000000000120 T _czc_last_create_error\n'
            self.assertTrue(manager._defined_c_symbol(archive, 'czc_last_create_error'))

    def test_zeroconf_rebuild_invalidates_only_stale_objects(self):
        build = self.builds / 'Intermediates/PythonHost/mac/arm64/cmake'
        target = build / 'CMakeFiles/CinterForgePythonHost.dir/src'
        shared = build / 'CMakeFiles/CinterForgePythonHostShared.dir/src'
        target.mkdir(parents=True)
        shared.mkdir(parents=True)
        old = target / 'CinterZeroconf.cpp.o'
        other = target / 'CinterPythonHost.cpp.o'
        second = shared / 'CinterZeroconf.cpp.o'
        for item in (old, other, second):
            item.write_text('compiled object')
        archive = build / 'libCinterForgePythonHost.a'
        archive.write_bytes(b'old archive')
        with mock.patch.object(manager, 'current_platform', return_value='mac'), \
             mock.patch.object(manager, '_defined_c_symbol', return_value=False):
            self.assertEqual(manager._refresh_stale_zeroconf_objects(build, True), 2)
        self.assertFalse(old.exists())
        self.assertFalse(second.exists())
        self.assertTrue(other.exists())

    def test_zeroconf_current_archive_never_invalidated(self):
        build = self.builds / 'Intermediates/PythonHost/mac/arm64/cmake'
        compiled = build / 'CMakeFiles/CinterForgePythonHost.dir/src/CinterZeroconf.cpp.o'
        compiled.parent.mkdir(parents=True)
        compiled.write_text('current')
        with mock.patch.object(manager, 'current_platform', return_value='mac'), \
             mock.patch.object(manager, '_defined_c_symbol', return_value=True):
            self.assertEqual(manager._refresh_stale_zeroconf_objects(build, True), 0)
        self.assertTrue(compiled.exists())

class ToolchainProbeTests(unittest.TestCase):
    def setUp(self):
        TOOLCHAIN_SIGN_OF_LIFE_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=TOOLCHAIN_SIGN_OF_LIFE_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.builds = Path(self.tmp.name) / 'Builds'
        self.builds.mkdir()

    def test_native_bash_directory_and_version_evidence(self):
        if os.name == 'nt':
            self.skipTest('POSIX Bash test only')
        actual = toolchain._probe
        def fake_webkit(name, command, *, cwd, env):
            if name == 'python' and toolchain.target()[0] == 'mac':
                return actual(name, [sys.executable, '--version'], cwd=cwd, env=env)
            if name == 'webkitgtk':
                return {'executable': '/usr/bin/pkg-config', 'command': command,
                        'exit_code': 0, 'output': 'test-only WebKitGTK fixture'}
            return actual(name, command, cwd=cwd, env=env)
        with mock.patch.object(toolchain, '_probe', side_effect=fake_webkit):
            report = toolchain.probe_local(self.builds)
        self.assertEqual(report['status'], 'passed')
        for name in ('bash', 'cc', 'cxx', 'cmake', 'ninja', 'python', 'node'):
            self.assertEqual(report['tools'][name]['exit_code'], 0)
            self.assertTrue(report['tools'][name]['output'])
        listing = report['directory_listing']['output']
        self.assertIn('acceptance-marker.txt', listing)
        self.assertIn('CINTER_TERMINAL_DIRECTORY_BEGIN', listing)
        self.assertTrue((self.builds / 'Logs/CinterForgeTerminal/toolchain-linux-x64.log').is_file()
                        or (self.builds / 'Logs/CinterForgeTerminal/toolchain-mac-arm64.log').is_file())

    def test_missing_required_tool_logs_failure_not_success(self):
        actual = toolchain._probe
        def unavailable(name, command, *, cwd, env):
            if name == 'cmake':
                raise RuntimeError('forced missing CMake')
            if name == 'webkitgtk':
                return {'executable': 'pkg-config', 'command': command,
                        'exit_code': 0, 'output': 'fixture'}
            return actual(name, command, cwd=cwd, env=env)
        with mock.patch.object(toolchain, '_probe', side_effect=unavailable):
            with self.assertRaisesRegex(RuntimeError, 'cmake'):
                toolchain.probe_local(self.builds)
        system, arch = toolchain.target()
        data = json.loads((self.builds / 'Logs/CinterForgeTerminal' /
                           f'toolchain-{system}-{arch}.json').read_text())
        self.assertEqual(data['status'], 'failed')
        self.assertTrue(any('cmake' in error for error in data['missing']))

    def test_msys2_absence_fails_instead_of_system_bash_fallback(self):
        with mock.patch.object(toolchain, 'msys2_root', side_effect=RuntimeError('MSYS2 missing')):
            with self.assertRaisesRegex(RuntimeError, 'MSYS2 missing'):
                toolchain.stage_windows_msys2(self.builds)

    def test_wine_smoke_requires_a_real_provisioned_provider(self):
        # Host MinGW/Wine absence must first trigger managed provisioning. An
        # unavailable engine is still a hard failure, never a fake Wine pass.
        with mock.patch('host.windows_environment.WindowsCrossEnvironment.ensure',
                        side_effect=RuntimeError('Docker engine unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Docker engine unavailable'):
                win_cross.smoke(self.builds)

    def test_wine_receipt_requires_console_marker_and_real_windows_directory(self):
        from types import SimpleNamespace
        root = self.builds / 'Intermediates/CinterForgeTerminal/WindowsCrossSmoke'
        def compile_or_run(command, *, cwd, timeout):
            if command[0] == 'x86_64-w64-mingw32-gcc':
                (root / 'CinterWindowsCrossSmoke.exe').write_bytes(b'MZ\0\0')
                return SimpleNamespace(returncode=0, stdout='')
            return SimpleNamespace(returncode=0, stdout='CINTER_WINDOWS_WINE_PASS\n')
        cross = mock.Mock()
        cross.provider = 'docker-linux-amd64-wine'
        cross.path.side_effect = lambda path: '/builds/' + Path(path).name
        cross.execute.side_effect = compile_or_run
        with self.assertRaisesRegex(RuntimeError, 'Windows Wine smoke failed'):
            win_cross.smoke(self.builds, executor=cross)
        receipt = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-wine-smoke.json').read_text())
        self.assertEqual(receipt['status'], 'failed')

    def test_wine_smoke_runs_real_pe_and_requires_win32_directory_evidence(self):
        """The Wine acceptance gate is PE execution, not cmd.exe formatting."""
        from types import SimpleNamespace
        root = self.builds / 'Intermediates/CinterForgeTerminal/WindowsCrossSmoke'
        commands = []
        def emulate(command, *, cwd, timeout):
            commands.append(list(command))
            if command[0] == 'x86_64-w64-mingw32-gcc':
                (root / 'CinterWindowsCrossSmoke.exe').write_bytes(b'MZ' + b'fixture')
                return SimpleNamespace(returncode=0, stdout='')
            self.assertEqual(command[0], 'wine')
            self.assertTrue(command[1].endswith('CinterWindowsCrossSmoke.exe'))
            return SimpleNamespace(returncode=0, stdout=(
                'CINTER_WINDOWS_WINE_PASS\n'
                'cinter-wine-directory-marker.txt\n'
                'CINTER_WINDOWS_DIRECTORY_PASS\n'))
        cross = mock.Mock()
        cross.provider = 'docker-linux-amd64-wine'
        cross.path.side_effect = lambda path: '/builds/' + Path(path).name
        cross.execute.side_effect = emulate
        report = win_cross.smoke(self.builds, executor=cross)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['checks']['pe_win32_directory_marker'])
        self.assertEqual(len(commands), 2)
        self.assertFalse(any('cmd' in command for command in commands))

    def test_wine_smoke_rejects_pe_without_win32_marker(self):
        """Running a PE without its real Windows API evidence is a failure."""
        from types import SimpleNamespace
        root = self.builds / 'Intermediates/CinterForgeTerminal/WindowsCrossSmoke'
        def emulate(command, *, cwd, timeout):
            if command[0] == 'x86_64-w64-mingw32-gcc':
                (root / 'CinterWindowsCrossSmoke.exe').write_bytes(b'MZ' + b'fixture')
                return SimpleNamespace(returncode=0, stdout='')
            if command[0] == 'wine' and command[1].endswith('.exe'):
                return SimpleNamespace(returncode=0, stdout='CINTER_WINDOWS_WINE_PASS\n')
            return SimpleNamespace(returncode=0, stdout='diagnostic only')
        cross = mock.Mock()
        cross.provider = 'docker-linux-amd64-wine'
        cross.path.side_effect = lambda path: '/builds/' + Path(path).name
        cross.execute.side_effect = emulate
        with self.assertRaisesRegex(RuntimeError, 'pe_win32_directory_marker'):
            win_cross.smoke(self.builds, executor=cross)
        receipt = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-wine-smoke.json').read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertTrue(receipt['checks']['wine_exit_zero'])
        self.assertFalse(receipt['checks']['pe_win32_directory_marker'])

    def test_mac_setup_records_managed_cross_provider_not_missing_host_wine(self):
        from types import SimpleNamespace
        cross = mock.Mock()
        cross.provider = 'docker-linux-amd64-wine'
        cross.image = 'cinterforge-terminal-windows-cross:test'
        cross.docker = str(self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker')
        cross.version_probes.return_value = {
            'mingw-w64': {'executable': 'docker://test/g++', 'exit_code': 0, 'output': 'G++ test'} ,
            'wine': {'executable': 'docker://test/wine', 'exit_code': 0, 'output': 'Wine test'},
        }
        def fake_probe(name, command, *, cwd, env):
            if name == 'native-shell-directory':
                output = 'CINTER_TERMINAL_DIRECTORY_BEGIN\nacceptance-marker.txt\nCINTER_TERMINAL_DIRECTORY_END'
            else:
                output = f'{name}: fixture'
            return {'executable': command[0], 'exit_code': 0, 'output': output}
        with (mock.patch.object(toolchain, 'target', return_value=('mac', 'arm64')),
              mock.patch.object(toolchain, '_probe', side_effect=fake_probe),
              mock.patch('host.windows_environment.WindowsCrossEnvironment') as cls):
            cls.return_value.ensure.return_value = cross
            report = toolchain.probe_local(self.builds, require_cross=True)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['cross_provider'], 'docker-linux-amd64-wine')
        self.assertIn('wine', report['tools'])
        self.assertEqual(report['tools']['wine']['exit_code'], 0)

class DevToolingReportTests(unittest.TestCase):
    """A missing version or size is an error; only Xcode's size is exempt."""

    def test_xcode_exempt_from_size_but_not_from_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'xcodebuild'
            binary.write_bytes(b'executable')
            with mock.patch.object(devtooling.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, 'Xcode 16.0\n', '')):
                found = devtooling._local('Xcode', binary, ('-version',), xcode=True)
            self.assertEqual(found['version'], 'Xcode 16.0')
            self.assertIsNone(found['size'])

    def test_actual_version_and_size_required_for_other_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'cmake'
            binary.write_bytes(b'12345678')
            with mock.patch.object(devtooling.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, 'cmake version 3.31\n', '')):
                found = devtooling._local('CMake', binary, ('--version',))
            self.assertEqual(found['size'], 8)
            self.assertEqual(found['version'], 'cmake version 3.31')

    def test_empty_version_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'ninja'
            binary.write_bytes(b'contents')
            with mock.patch.object(devtooling.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, '', '')):
                with self.assertRaises(RuntimeError):
                    devtooling._local('Ninja', binary, ('--version',))

    def test_markdown_preserves_failed_rows_and_xcode_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'Builds/Logs/CinterForgeTerminal/DevTooling.md'
            devtooling._write_report(report, [
                {'platform': 'macOS', 'name': 'Xcode', 'version': 'Xcode 16',
                 'size': None, 'path': str(Path(temporary) / 'Xcode'), 'xcode': True, 'status': 'PASS'},
                {'platform': 'Windows', 'name': 'Python', 'version': 'FAILED',
                 'path': 'python.exe', 'status': 'FAIL'},
            ])
            content = report.read_text()
            self.assertIn('Exempt (Xcode)', content)
            self.assertIn('**Result: FAIL**', content)
            self.assertIn('| Windows | Python | FAILED | FAILED |', content)

    def test_cygwin_warning_is_not_a_successful_version_probe(self):
        """Stock Wine must not turn an MSYS runtime diagnostic into PASS."""
        output = subprocess.CompletedProcess([], 0, 'Cygwin WARNING:\n', '')
        with self.assertRaisesRegex(RuntimeError, 'compatibility warning'):
            devtooling._version(output)

    def test_installed_msys2_package_versions_without_wine(self):
        """Mac-hosted Setup inventories Windows tools from verified package receipts."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'MSYS2'
            db = root / 'var/lib/pacman/local'
            for name in ('bash', 'pacman', 'coreutils', 'tar'):
                folder = db / (name + '-1.2-3')
                folder.mkdir(parents=True)
                (folder / 'desc').write_text(f'%NAME%\n{name}\n\n%VERSION%\n1.2-3\n')
            from host.windows_msys2_cross import SCHEMA
            (root / 'base-staged.json').write_text(json.dumps({'sha256': 'a' * 64}))
            resolved = {name: {'version': '2.3-4', 'repository': 'MSYS' if name == 'winpty' else 'UCRT64'}
                        for name in set(devtooling.WINDOWS_PACKAGE_OWNERS.values())
                        if name not in ('bash', 'pacman', 'coreutils', 'tar')}
            (root / 'closure.json').write_text(json.dumps({'schema': SCHEMA,
                'base_sha256': 'a' * 64, 'resolved': resolved}))
            tool = root / 'ucrt64/bin/node.exe'
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b'MZ' + b'1' * 32)
            with mock.patch.object(devtooling.platform, 'system', return_value='Darwin'):
                versions = devtooling._staged_windows_versions(root)
                found = devtooling._windows(None, root, 'ucrt64/bin/node.exe',
                                            ('--version',), package_versions=versions)
            self.assertEqual(found['version'], '2.3-4')
            self.assertEqual(found['size'], 34)
            self.assertIn('native-execution-not-tested', found['check'])
            self.assertEqual(versions['bash'], '1.2-3')

    def test_missing_msys2_package_version_fails_closed(self):
        """An executable alone is not enough; its installed version must be known."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'MSYS2'
            root.mkdir()
            (root / 'base-staged.json').write_text(json.dumps({'sha256': 'a' * 64}))
            from host.windows_msys2_cross import SCHEMA
            (root / 'closure.json').write_text(json.dumps({'schema': SCHEMA,
                'base_sha256': 'a' * 64, 'resolved': {}}))
            (root / 'var/lib/pacman/local').mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, 'versions missing'):
                devtooling._staged_windows_versions(root)




class CodeOSSBrowserExtensionBundleTests(unittest.TestCase):
    """The staged extension must parse as the browser worker actually loads it."""

    def test_real_codeoss_menu_bridge_fails_closed_on_missing_upstream_anchors(self):
        """Producer must patch actual IAction menus, never fabricate fallback UI."""
        from webapp.codeoss import producer
        build_root = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        build_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_root, prefix='codeoss-menus-') as tmp:
            target = (Path(tmp) / 'src/vs/workbench/browser/parts/titlebar/menubarControl.ts')
            target.parent.mkdir(parents=True)
            # These are the exact integration anchors in the pinned Code-OSS
            # MenubarControl: compiled actions, live menu update, method boundary.
            source = ("		for (const title of Object.keys(this.topLevelTitles)) {\n"
                      "			if (this.menubar) {\n				if (!firstTime) {\n"
                      + 2 * ("						this.menubar?.updateMenu({ actions, label: "
                             "mnemonicMenuLabel(this.topLevelTitles[title]) });\n")
                      + "\n	}\n\n	private getWebNavigationActions(): IAction[] {\n")
            target.write_text(source, encoding='utf-8')
            producer._patch_codeoss_menubar_bridge(Path(tmp))
            patched = target.read_text(encoding='utf-8')
            self.assertIn('CINTERFORGE_CODEOSS_MENU_SERVICE_BRIDGE_V1', patched)
            self.assertIn('cinterforge-codeoss-menu-model', patched)
            self.assertIn('cinterforge-codeoss-menu-run', patched)
            self.assertIn('action.run()', patched)
            producer._patch_codeoss_menubar_bridge(Path(tmp))
            self.assertEqual(target.read_text(encoding='utf-8'), patched)
            target.write_text('upstream changed', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'MenubarControl loop changed'):
                producer._patch_codeoss_menubar_bridge(Path(tmp))

    def test_codeoss_worker_uses_only_exported_zmachine_constructor(self):
        """Regression: the rejected HostedZMachineTerminal did not exist."""
        root = Path(__file__).resolve().parents[1] / 'webapp/codeoss/extension'
        coordinator = (root / 'extension.js').read_text(encoding='utf-8')
        pty = (root / 'terminals/ZMachineTerminal.js').read_text(encoding='utf-8')
        self.assertIn('C.ZMachineTerminal = ZMachineTerminal;', pty)
        self.assertIn('new ZMachineTerminal()', coordinator)
        self.assertNotIn('HostedZMachineTerminal', coordinator)

    def test_codeoss_real_menu_request_replays_source_model(self):
        """The actual Code-OSS action snapshot can be requested after initial publish."""
        from webapp.codeoss import producer
        build_root = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        build_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_root, prefix='codeoss-menu-') as temp:
            path = Path(temp) / 'src/vs/workbench/browser/parts/titlebar'
            path.mkdir(parents=True)
            target = path / 'menubarControl.ts'
            target.write_text(
                "\t\tfor (const title of Object.keys(this.topLevelTitles)) {\n"
                "\t\t\tif (this.menubar) {\n\t\t\t\tif (!firstTime) {\n"
                + 2 * ("\t\t\t\t\t\tthis.menubar?.updateMenu({ actions, label: "
                       "mnemonicMenuLabel(this.topLevelTitles[title]) });\n")
                + "\n\t}\n\n\tprivate getWebNavigationActions(): IAction[] {\n",
                encoding='utf-8')
            producer._patch_codeoss_menubar_bridge(Path(temp))
            patched = target.read_text(encoding='utf-8')
            self.assertIn("event.data?.type === 'cinterforge-codeoss-menu-request'", patched)
            self.assertIn('runtime.__cinterForgeMenuSnapshot || []', patched)
            self.assertIn('runtime.__cinterForgeMenuActions?.get(', patched)
        outer = (Path(__file__).resolve().parents[1] / 'webapp/terminal.js').read_text(encoding='utf-8')
        self.assertIn("type: 'cinterforge-codeoss-menu-request'", outer)
        self.assertNotIn('code:menubar:', outer)

    def test_codeoss_host_logs_provider_refuses_all_mutations(self):
        """Provider must implement every method while retaining read-only logs."""
        source = (Path(__file__).resolve().parents[1] /
                  'webapp/codeoss/extension/filesystem/CinterForgeFileSystemProvider.js')
        body = source.read_text(encoding='utf-8').split('class HostFileSystemProvider {', 1)[1]
        for method in ('writeFile(uri)', 'createDirectory(uri)', 'delete(uri)', 'rename(oldUri)'):
            self.assertIn(method + ' { throw vscode.FileSystemError.NoPermissions(', body)


    def test_setup_reuses_only_extension_matching_authored_source(self):
        """A valid but stale Code-OSS Web product must be refreshed by Setup."""
        from webapp.codeoss import producer, setup as code_setup
        from webapp.codeoss.extension_bundle import build_extension_bundle

        source = Path(__file__).resolve().parents[1] / 'webapp/codeoss/extension'
        build_root = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        build_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_root, prefix='codeoss-freshness-') as tmp:
            workspace = Path(tmp)
            staged = workspace / 'Builds/InteractiveFiction/CodeOSS/Web/extensions/cinterforge-if'
            staged.parent.mkdir(parents=True)
            shutil.copytree(source, staged)
            build_extension_bundle(staged)
            web = staged.parents[1]
            self.assertTrue(code_setup._extension_matches_source(web))

            with mock.patch.object(producer, 'validate_codeoss_web'), \
                 mock.patch.object(producer, 'ensure_codeoss', return_value={'refreshed': True}) as refresh:
                self.assertTrue(code_setup.setup(workspace)['reused'])
                refresh.assert_not_called()

                # Compiled worker may be stale even when every authored source
                # file was copied: normal Setup must not accept that package.
                (staged / 'extension.bundle.js').write_text('// old bundle\n', encoding='utf-8')
                self.assertFalse(code_setup._extension_matches_source(web))
                self.assertFalse(code_setup.setup(workspace)['reused'])
                refresh.assert_called_once_with(workspace, refresh=True)

            # Source-only fixes must also invalidate an otherwise complete stage.
            build_extension_bundle(staged)
            (staged / 'runtime/ControlTransport.js').write_text('// stale worker\n', encoding='utf-8')
            self.assertFalse(code_setup._extension_matches_source(web))

    def test_bundled_extension_parses_in_javascript_runtime(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is supplied by Setup; not installed in this test environment')
        from webapp.codeoss.extension_bundle import build_extension_bundle
        source = Path(__file__).resolve().parents[1] / 'webapp/codeoss/extension'
        build_root = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        build_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_root, prefix='codeoss-worker-') as tmp:
            staged = Path(tmp) / 'extension'
            shutil.copytree(source, staged)
            bundle = build_extension_bundle(staged)
            checked = subprocess.run([node, '--check', str(bundle)], capture_output=True,
                                     text=True, timeout=30, check=False)
            self.assertEqual(checked.returncode, 0,
                             'The browser-worker extension must parse: ' + checked.stderr)
            self.assertEqual(json.loads((staged / 'package.json').read_text())['browser'],
                             './extension.bundle.js')

    def test_terminal_host_probe_distinguishes_if_from_native_host(self):
        """Execute the actual browser transport: Terminal 200, IF 404, denied 401."""
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is supplied by Setup')
        transport = Path(__file__).resolve().parents[1] /             'webapp/codeoss/extension/runtime/ControlTransport.js'
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const calls = [];
let current = {ok:true,status:200,kind:'terminal'};
const context = {
  URL,
  location: {protocol:'http:', origin:'http://127.0.0.1:20300',
    href:'http://127.0.0.1:20300/codeoss/out/vs/workbench/api/worker/extensionHostWorkerMain.js'},
  console,
  fetch: async (path, options) => {
    if (!String(path).startsWith('http://127.0.0.1:20300/'))
      throw new TypeError('URL is not valid or contains user credentials');
    calls.push({path, options});
    return {ok:current.ok,status:current.status,
      json:async () => ({kind:current.kind,theme:'dark',platform:'mac'})};
  },
  CinterForgeCode: {vscode:{workspace:{getConfiguration:()=>({get:()=> 'http://other-if-host/'})}}}
};
context.globalThis = context;
vm.runInNewContext(source, context, {filename:'ControlTransport.js'});
(async () => {
  const probe = context.CinterForgeCode.detectTerminalHost;
  assert.equal((await probe()).platform, 'mac');
  assert.equal(calls[0].path, 'http://127.0.0.1:20300/host/code-workspace');
  assert.equal(calls[0].options.credentials, 'same-origin');
  current = {ok:false,status:404};
  assert.equal(await probe(), null);
  current = {ok:false,status:401};
  await assert.rejects(probe(), /HTTP 401/);
  current = {ok:true,status:200,kind:'ifplayer'};
  assert.equal(await probe(), null);
  context.location.protocol = 'file:';
  const before = calls.length;
  assert.equal(await probe(), null);
  assert.equal(calls.length, before);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        checked = subprocess.run([node, '-e', script, str(transport)], capture_output=True,
                                 text=True, timeout=30, check=False)
        self.assertEqual(checked.returncode, 0, checked.stderr)


class CodeOSSHostOptionalDependencyTests(unittest.TestCase):
    """Upstream Code-OSS nested npm installs must match the native build host."""

    def setUp(self):
        build_root = BOOTSTRAP_BUNDLE_BUILDS / "Intermediates/CinterForgeTerminalTests"
        build_root.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=build_root, prefix="codeoss-npm-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.source = self.tmp / "Source"
        self.source.mkdir()
        (self.source / "package.json").write_text('{}\n', encoding="utf-8")
        (self.source / "package-lock.json").write_text('{}\n', encoding="utf-8")
        copilot = self.source / "extensions" / "copilot"
        copilot.mkdir(parents=True)
        self.lock = copilot / "package-lock.json"
        self.lock.write_text(json.dumps({"packages": {
            "node_modules/@github/copilot": {"version": "1.0.73"},
            "node_modules/@github/copilot-darwin-arm64": {
                "version": "1.0.73", "optional": True,
                "integrity": "sha512-" + "A" * 88,
            },
        }}), encoding="utf-8")

    def test_environment_preserves_optional_sdk_and_clears_cross_target_flags(self):
        inherited = {
            "NPM_CONFIG_ARCH": "x64", "npm_config_platform": "win32",
            "npm_config_omit": "optional", "NPM_CONFIG_OPTIONAL": "false",
            "npm_config_include": "dev", "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "HTTPS_PROXY": "http://127.0.0.1:1234", "PATH": "/usr/bin",
        }
        actual = producer._codeoss_npm_environment(inherited)
        self.assertEqual(actual["npm_config_include"], "optional")
        for key in inherited:
            if key.lower() in {
                "npm_config_arch", "npm_config_platform", "npm_config_omit",
                "npm_config_optional", "npm_config_ignore_scripts", "npm_config_include",
            } and key != "npm_config_include":
                self.assertNotIn(key, actual)
        self.assertEqual(actual["HTTPS_PROXY"], inherited["HTTPS_PROXY"])
        self.assertEqual(actual["PATH"], inherited["PATH"])

    def test_locked_darwin_arm64_copilot_sdk_is_accepted(self):
        with mock.patch.object(producer.platform, "system", return_value="Darwin"), \
             mock.patch.object(producer.platform, "machine", return_value="arm64"):
            producer._verify_locked_copilot_sdk_platform(self.source)

    def test_missing_host_sdk_fails_before_npm_install(self):
        self.lock.write_text(json.dumps({"packages": {
            "node_modules/@github/copilot": {"version": "1.0.73"},
        }}), encoding="utf-8")
        with mock.patch.object(producer.platform, "system", return_value="Darwin"), \
             mock.patch.object(producer.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(RuntimeError, "absent from lockfile"):
                producer._verify_locked_copilot_sdk_platform(self.source)

    def test_unpinned_platform_dependency_is_rejected(self):
        data = json.loads(self.lock.read_text(encoding="utf-8"))
        data["packages"]["node_modules/@github/copilot-darwin-arm64"]["version"] = "1.0.108"
        self.lock.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(producer.platform, "system", return_value="Darwin"), \
             mock.patch.object(producer.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(RuntimeError, "lock mismatch"):
                producer._verify_locked_copilot_sdk_platform(self.source)

    def test_build_uses_optional_deps_in_root_and_nested_npm(self):
        seen = []
        def verify_npm_command(argv, **kwargs):
            seen.append((argv, kwargs))
            raise RuntimeError("controlled stop before any real npm installation")
        with mock.patch.object(producer, "_ensure_pinned_node", return_value=(
            self.tmp / "node" / "bin" / "node", self.tmp / "node" / "npm-cli.js", "24.18.0",
        )), mock.patch.object(producer.platform, "system", return_value="Darwin"), \
             mock.patch.object(producer.platform, "machine", return_value="arm64"), \
             mock.patch.dict(producer.os.environ, {"npm_config_platform": "win32", "npm_config_omit": "optional"}), \
             mock.patch.object(producer, "_run_checked", side_effect=verify_npm_command):
            with self.assertRaisesRegex(RuntimeError, "controlled stop"):
                producer._build_web(self.source)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0][-2:], ["ci", "--include=optional"])
        self.assertEqual(seen[0][1]["env"]["npm_config_include"], "optional")
        self.assertNotIn("npm_config_platform", seen[0][1]["env"])
        self.assertNotIn("npm_config_omit", seen[0][1]["env"])
