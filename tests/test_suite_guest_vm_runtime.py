"""No-VM regressions for the shared macOS Windows/Linux guest backend."""
from tests._support import *
from webapp import CinterForgeTerminal as terminal
from concurrent.futures import ThreadPoolExecutor


class SharedGuestVMTests(unittest.TestCase):
    def setUp(self):
        area = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        area.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=area, prefix='vm-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.colima = self.root / 'colima'
        self.colima.mkdir()
        self.env = {'COLIMA_HOME': str(self.colima)}
        self.sdk = self.root / 'Library/Application Support/Steam/steamapps/DevTools'
        self.workspace = self.root / 'Source'
        self.workspace.mkdir()

    def test_windows_guest_ignores_old_stock_wine_archive(self):
        import hashlib
        folder = self.sdk / 'WindowsCross'
        folder.mkdir(parents=True)
        for suffix, version in (('0' * 16, 'wine-11.0'), ('f' * 16, 'wine-11.15')):
            archive = folder / (suffix + '.tar')
            archive.write_bytes(('image-' + version).encode())
            (folder / (suffix + '.json')).write_text(json.dumps({
                'dockerfile_sha256': suffix * 4,
                'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'image': 'cinterforge-terminal-windows-cross:' + suffix,
                'wine_version': version,
            }))
        archive, image = terminal._image_archive(self.sdk, 'win')
        self.assertEqual(archive.name, 'f' * 16 + '.tar')
        self.assertTrue(image.endswith('f' * 16))

    def test_linux_and_windows_requests_cannot_start_colima_concurrently(self):
        max_active = [0]
        active = [0]
        guard = threading.Lock()

        def work(sdk, env, workspace):
            with guard:
                active[0] += 1
                max_active[0] = max(active[0], max_active[0])
            time.sleep(.03)
            with guard:
                active[0] -= 1
            return 'docker'

        with mock.patch.object(terminal, '_ensure_docker_locked', side_effect=work):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(terminal._ensure_docker, self.sdk,
                                           self.env, self.workspace) for _ in range(2)]
                self.assertEqual([f.result() for f in futures], ['docker', 'docker'])
        self.assertEqual(max_active[0], 1)

    def test_host_mounts_cover_all_drives_without_overlapping_lima_locations(self):
        # The host root includes /Volumes, including names with spaces.
        # Lima rejects nested shares even if their guest destinations differ.
        with (mock.patch.object(terminal.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, '_mac_mounted_volumes', return_value=[
                  Path('/Volumes/Work'), Path('/Volumes/External Drive'),
                  Path('/System/Volumes/Data')])):
            mounts = terminal._mac_host_mounts()
        self.assertEqual(mounts, [(Path('/'), '/host/mac')])
        self.assertFalse(any(p != Path('/') for p, _ in mounts))

    def test_saved_colima_mounts_detect_legacy_overlapping_configuration(self):
        config = self.colima / terminal.PROFILE / 'colima.yaml'
        config.parent.mkdir()
        config.write_text('cpu: 4\nmounts:\n  - location: "/"\n'
                          '    mountPoint: "/host/mac"\n    writable: true\n'
                          '  - location: "/Volumes"\n'
                          '    mountPoint: "/host/volumes"\n    writable: true\n')
        desired = [(Path('/'), '/host/mac')]
        self.assertTrue(terminal._configure_colima_host_mounts(self.env, desired, dry_run=True))
        self.assertIn('/host/volumes', config.read_text())  # dry-run never edits
        self.assertTrue(terminal._configure_colima_host_mounts(self.env, desired))
        self.assertNotIn('/host/volumes', config.read_text())
        self.assertFalse(terminal._configure_colima_host_mounts(self.env, desired, dry_run=True))

    def test_colima_equivalent_yaml_format_does_not_restart_vm(self):
        config = self.colima / terminal.PROFILE / 'colima.yaml'
        config.parent.mkdir()
        config.write_text('cpu: 4\nmounts:\n  - location: /\n'
                          '    mountPoint: /host/mac\n    writable: true\n')
        desired = [(Path('/'), '/host/mac')]
        self.assertFalse(terminal._configure_colima_host_mounts(self.env, desired, dry_run=True))
        self.assertEqual(config.read_text().count('mountPoint'), 1)

    def test_colima_yaml_preserves_unrelated_settings_and_is_idempotent(self):
        config = self.colima / terminal.PROFILE / 'colima.yaml'
        config.parent.mkdir()
        config.write_text('cpu: 5\nmounts:\n  - location: ~/Source\n    writable: false\ndocker: {}\n')
        mounts = [(Path('/'), '/host/mac')]
        terminal._configure_colima_host_mounts(self.env, mounts)
        actual = config.read_text()
        self.assertIn('cpu: 5', actual)
        self.assertIn('docker: {}', actual)
        self.assertIn('location: "/"', actual)
        self.assertIn('mountPoint: "/host/mac"', actual)
        self.assertNotIn('mountPoint: "/host/volumes"', actual)
        self.assertNotIn('~/Source', actual)
        terminal._configure_colima_host_mounts(self.env, mounts)
        self.assertEqual(config.read_text(), actual)

    def test_runtime_emulation_converts_existing_rosetta_config_in_place(self):
        config = self.colima / terminal.PROFILE / 'colima.yaml'
        config.parent.mkdir()
        config.write_text('cpus: 4\nrosetta: true\nbinfmt: false\n'
                          'mounts:\n  - location: "/"\n'
                          '    mountPoint: "/host/mac"\n    writable: true\n')
        self.assertTrue(terminal._configure_colima_runtime_emulation(self.env, dry_run=True))
        self.assertIn('rosetta: true', config.read_text())
        self.assertTrue(terminal._configure_colima_runtime_emulation(self.env))
        actual = config.read_text()
        self.assertIn('rosetta: false', actual)
        self.assertIn('binfmt: true', actual)
        self.assertIn('mountPoint: "/host/mac"', actual)
        self.assertFalse(terminal._configure_colima_runtime_emulation(self.env, dry_run=True))
        self.assertFalse(terminal._configure_colima_runtime_emulation(self.env))

    def test_old_runtime_rosetta_profile_is_restarted_for_qemu(self):
        config = self.colima / terminal.PROFILE / 'colima.yaml'
        config.parent.mkdir()
        config.write_text('rosetta: true\nbinfmt: false\n'
                          'mounts:\n  - location: "/"\n'
                          '    mountPoint: "/host/mac"\n    writable: true\n')
        seen = []
        def run(argv, **kwargs):
            if argv[1] == 'info':
                return subprocess.CompletedProcess(argv, 0, '29.5.2', '')
            if argv[1] == 'ssh':
                return subprocess.CompletedProcess(argv, 0, '', '')
            if argv[1] == 'ps':
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError(argv)
        def checked(argv, env, **kwargs):
            seen.append(argv)
        with (mock.patch.object(terminal, '_mac_host_mounts', return_value=[(Path('/'), '/host/mac')]),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked', side_effect=checked)):
            terminal._ensure_docker(self.sdk, self.env, self.workspace)
        self.assertEqual([cmd[1] for cmd in seen], ['stop', 'start'])
        self.assertIn('--vz-rosetta=false', seen[1])
        self.assertIn('--binfmt=true', seen[1])
        self.assertNotIn('--vz-rosetta', seen[1])
        self.assertIn('rosetta: false', config.read_text())
        self.assertIn('binfmt: true', config.read_text())

    def test_runtime_emulation_refuses_symlinked_profile(self):
        other = self.root / 'other'
        other.mkdir()
        (other / 'colima.yaml').write_text('rosetta: true\n')
        (self.colima / terminal.PROFILE).symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            terminal._configure_colima_runtime_emulation(self.env)

    def test_cold_boot_mounts_mac_tree_without_overwriting_guest_root(self):
        state = {'started': False, 'start_args': None}

        def run(argv, **kwargs):
            if 'info' in argv:
                return subprocess.CompletedProcess(argv, 0 if state['started'] else 1, '', '')
            if 'status' in argv:
                return subprocess.CompletedProcess(argv, 1, '', 'not running')
            if 'ssh' in argv:
                return subprocess.CompletedProcess(argv, 0 if state['started'] else 1, '', '')
            raise AssertionError(argv)

        def checked(argv, env, **kwargs):
            self.assertEqual(argv[1:3], ['start', terminal.PROFILE])
            state['started'] = True
            state['start_args'] = argv

        with (mock.patch.object(terminal, '_mac_host_mounts', return_value=[
                  (Path('/'), '/host/mac')]),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked', side_effect=checked)):
            result = terminal._ensure_docker(self.sdk, self.env, self.workspace)
        self.assertTrue(result.endswith('/mac/env/bin/docker'))
        args = state['start_args']
        self.assertIn('--mount-type', args)
        self.assertIn('virtiofs', args)
        self.assertIn('--vz-rosetta=false', args)
        self.assertIn('--binfmt=true', args)
        self.assertNotIn('--mount', args)  # custom mountPoint comes from profile YAML
        config = (self.colima / terminal.PROFILE / 'colima.yaml').read_text()
        self.assertIn('mountPoint: "/host/mac"', config)
        self.assertNotIn('mountPoint: "/host/volumes"', config)

    def test_active_guest_is_never_stopped_for_missing_workspace_mount(self):
        def run(argv, **kwargs):
            if 'info' in argv:
                return subprocess.CompletedProcess(argv, 0, '29.5.2', '')
            if 'ssh' in argv:
                return subprocess.CompletedProcess(argv, 0 if argv[-1] == '/host/mac' else 1, '', '')
            if 'ps' in argv:
                return subprocess.CompletedProcess(argv, 0, 'active-container\n', '')
            raise AssertionError(argv)
        with (mock.patch.object(terminal, '_mac_host_mounts', return_value=[(Path('/'), '/host/mac')]),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked') as checked):
            with self.assertRaisesRegex(RuntimeError, 'unrelated containers run'):
                terminal._ensure_docker(self.sdk, self.env, self.workspace)
        checked.assert_not_called()

    def test_idle_guest_is_restarted_once_with_root_share(self):
        state = {'started': True, 'mount': False, 'ops': []}

        def run(argv, **kwargs):
            if 'info' in argv:
                return subprocess.CompletedProcess(argv, 0 if state['started'] else 1, '', '')
            if 'ssh' in argv:
                visible = argv[-1] == '/host/mac' or state['mount']
                return subprocess.CompletedProcess(argv, 0 if visible else 1, '', '')
            if 'ps' in argv:
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError(argv)

        def checked(argv, env, **kwargs):
            state['ops'].append(argv[1])
            if argv[1] == 'stop':
                state['started'] = False
            elif argv[1] == 'start':
                state['started'] = True
                state['mount'] = True

        with (mock.patch.object(terminal, '_mac_host_mounts', return_value=[(Path('/'), '/host/mac')]),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked', side_effect=checked)):
            terminal._ensure_docker(self.sdk, self.env, self.workspace)
        self.assertEqual(state['ops'], ['stop', 'start'])

    def test_unavailable_socket_never_stops_running_guest_without_inspection(self):
        def run(argv, **kwargs):
            if 'info' in argv:
                return subprocess.CompletedProcess(argv, 1, '', 'missing socket')
            if 'status' in argv:
                return subprocess.CompletedProcess(argv, 0, 'running', '')
            if 'ssh' in argv:
                return subprocess.CompletedProcess(argv, 1, '', 'cannot inspect daemon')
            raise AssertionError(argv)
        with (mock.patch.object(terminal, '_mac_mounted_volumes', return_value=[]),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked') as checked):
            with self.assertRaisesRegex(RuntimeError, 'cannot be verified'):
                terminal._ensure_docker(self.sdk, self.env, self.workspace)
        checked.assert_not_called()

    def test_running_vm_mount_verification_uses_real_colima_ssh_profile(self):
        """An already healthy guest must be reused, not falsely reconfigured.

        Colima's `ssh` subcommand does *not* take the profile as a positional
        argument. That old syntax attempted to run `t` in the guest, causing
        every mount probe to return nonzero and every shell startup to fail.
        """
        ssh_calls = []
        expected_mounts = [(Path('/'), '/host/mac')]
        terminal._configure_colima_host_mounts(self.env, expected_mounts)
        def run(argv, **kwargs):
            if argv[1] == 'info':
                return subprocess.CompletedProcess(argv, 0, '29.5.2', '')
            if argv[1] == 'ssh':
                self.assertEqual(argv[2:5], ['--profile', terminal.PROFILE, '--'])
                self.assertEqual(argv[5:7], ['test', '-d'])
                self.assertTrue(argv[-1].startswith('/host/'))
                ssh_calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError('Unexpected restart or subprocess: ' + str(argv))
        with (mock.patch.object(terminal, '_mac_host_mounts', return_value=expected_mounts),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked') as checked):
            docker = terminal._ensure_docker(self.sdk, self.env, self.workspace)
        checked.assert_not_called()
        self.assertEqual(docker, str(self.sdk / 'mac/env/bin/docker'))
        self.assertGreaterEqual(len(ssh_calls), 3)

    def test_unavailable_socket_checks_guest_containers_with_correct_profile(self):
        """Recover an idle guest only after inspecting its actual Docker service."""
        ops = []
        state = {'socket': False, 'guest_ps': 0}
        def run(argv, **kwargs):
            if argv[1] == 'info':
                return subprocess.CompletedProcess(argv, 0 if state['socket'] else 1, '', '')
            if argv[1] == 'status':
                return subprocess.CompletedProcess(argv, 0, 'running', '')
            if argv[1] == 'ssh':
                self.assertEqual(argv[2:5], ['--profile', terminal.PROFILE, '--'])
                if argv[5:7] == ['docker', 'ps']:
                    self.assertEqual(argv[-1], '--quiet')
                    state['guest_ps'] += 1
                    return subprocess.CompletedProcess(argv, 0, '', '')
                if argv[5:7] == ['test', '-d']:
                    return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError(argv)
        def checked(argv, env, **kwargs):
            ops.append(argv[1])
            if argv[1] == 'start':
                state['socket'] = True
        with (mock.patch.object(terminal, '_mac_host_mounts', return_value=[(Path('/'), '/host/mac')]),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked', side_effect=checked)):
            terminal._ensure_docker(self.sdk, self.env, self.workspace)
        self.assertEqual(ops, ['stop', 'start'])
        self.assertEqual(state['guest_ps'], 1)

    def test_reboot_docker_uses_named_colima_ssh_profile(self):
        commands = []
        def run(argv, **kwargs):
            self.assertEqual(argv[1:5], ['ssh', '--profile', terminal.PROFILE, '--'])
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with (mock.patch.object(terminal, 'sdk_root', return_value=self.sdk),
              mock.patch.object(terminal, '_env', return_value=self.env),
              mock.patch.object(terminal, '_guest_workspace', return_value=self.workspace),
              mock.patch.object(terminal, '_stop_owned_guests_locked'),
              mock.patch.object(terminal, '_start_guest_locked'),
              mock.patch.object(terminal, '_docker_call', return_value=subprocess.CompletedProcess([], 0, '29.5.2', '')),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run)):
            result = terminal.reboot_runtime_guests('docker')
        self.assertEqual(result['ready'], True)
        self.assertEqual(len(commands), 1)

    def test_stop_uses_only_t_profile_after_normal_docker_cleanup(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == 'status':
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError(argv)
        def docker(docker, env, *args, **kwargs):
            if args[0] == 'info':
                return subprocess.CompletedProcess([], 0, '29.5.2', '')
            if args[0] == 'ps':
                return subprocess.CompletedProcess([], 0, '', '')
            raise AssertionError(args)
        with (mock.patch.object(terminal.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, 'sdk_root', return_value=self.sdk),
              mock.patch.object(terminal, '_env', return_value=self.env),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_docker_call', side_effect=docker),
              mock.patch.object(terminal, '_stop_owned_guests_locked') as stop_guests,
              mock.patch.object(terminal, '_run_checked') as checked):
            terminal.stop_runtime_vm()
        stop_guests.assert_called_once()
        self.assertEqual(checked.call_args.args[0][1:], ['stop', terminal.PROFILE])
        self.assertEqual(calls[0][1:], ['status', terminal.PROFILE])

    def test_stale_host_docker_socket_shuts_down_through_colima_ssh(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            if argv[1] == 'status':
                return subprocess.CompletedProcess(argv, 0, '', '')
            if argv[1] == 'ssh' and 'ps' in argv:
                return subprocess.CompletedProcess(argv, 0,
                    'cinterforge-terminal-t-linux|t\n'
                    'cinterforge-terminal-t-win|t\n', '')
            if argv[1] == 'ssh' and 'rm' in argv:
                return subprocess.CompletedProcess(argv, 0, 'removed', '')
            raise AssertionError(argv)
        with (mock.patch.object(terminal.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, 'sdk_root', return_value=self.sdk),
              mock.patch.object(terminal, '_env', return_value=self.env),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_docker_call', return_value=subprocess.CompletedProcess([], 1, '', 'missing')),
              mock.patch.object(terminal, '_run_checked') as checked):
            terminal.stop_runtime_vm()
        self.assertEqual(len([a for a in commands if 'rm' in a]), 2)
        self.assertEqual(checked.call_args.args[0][1:], ['stop', terminal.PROFILE])

    def test_stale_host_docker_socket_preserves_foreign_container(self):
        def run(argv, **kwargs):
            if argv[1] == 'status':
                return subprocess.CompletedProcess(argv, 0, '', '')
            if argv[1] == 'ssh':
                return subprocess.CompletedProcess(argv, 0, 'other-app|other\n', '')
            raise AssertionError(argv)
        with (mock.patch.object(terminal.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, 'sdk_root', return_value=self.sdk),
              mock.patch.object(terminal, '_env', return_value=self.env),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_docker_call', return_value=subprocess.CompletedProcess([], 1, '', 'missing')),
              mock.patch.object(terminal, '_run_checked') as checked):
            with self.assertRaisesRegex(RuntimeError, 'unowned Docker container'):
                terminal.stop_runtime_vm()
        checked.assert_not_called()

    def test_server_health_reflects_real_listener_lifetime(self):
        prior_server = terminal._server
        prior_thread = terminal._server_thread
        self.addCleanup(setattr, terminal, '_server', prior_server)
        self.addCleanup(setattr, terminal, '_server_thread', prior_thread)
        terminal._server = None
        terminal._server_thread = None
        self.assertEqual(terminal.handle('service_health', {})['alive'], False)
        fake = SimpleNamespace(_http_serving=threading.Event(), _stopping=False)
        fake._http_serving.set()
        terminal._server = fake
        terminal._server_thread = SimpleNamespace(is_alive=lambda: True)
        self.assertTrue(terminal.handle('service_health', {})['alive'])
        fake._stopping = True
        self.assertFalse(terminal.handle('service_health', {})['alive'])

    def test_watchdog_ack_waits_for_previous_owner_to_release_colima(self):
        import fcntl
        lock_path = self.colima / 'CinterForgeTerminalLifetime.lock'
        prior = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(prior, fcntl.LOCK_EX)
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(read_fd) if read_fd >= 0 else None)
        complete = threading.Event()
        failures = []
        def new_owner():
            try:
                terminal._runtime_watchdog_main(['987654', str(write_fd)])
            except BaseException as error:
                failures.append(error)
            finally:
                complete.set()
        with (mock.patch.object(terminal.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, 'sdk_root', return_value=self.sdk),
              mock.patch.object(terminal, '_env', return_value=self.env),
              mock.patch.object(terminal.os, 'kill', side_effect=ProcessLookupError),
              mock.patch.object(terminal, 'stop_runtime_vm') as stop):
            thread = threading.Thread(target=new_owner)
            thread.start()
            self.assertFalse(complete.wait(0.05), 'Old owner must not be bypassed')
            fcntl.flock(prior, fcntl.LOCK_UN)
            os.close(prior)
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(os.read(read_fd, 1), b'1')
        stop.assert_called_once_with()

    def test_server_reboot_reuses_existing_native_lifetime_watchdog(self):
        native_pid = 987654
        owner = terminal._runtime_watchdog_owner(self.env)
        owner.write_text(json.dumps({'native_pid': native_pid,
                                     'watchdog_pid': os.getpid()}))
        with (mock.patch.object(terminal, 'sdk_root', return_value=self.sdk),
              mock.patch.object(terminal, '_env', return_value=self.env),
              mock.patch.object(terminal.subprocess, 'Popen') as popen):
            terminal._start_runtime_watchdog(native_pid, self.root / 'Logs')
        popen.assert_not_called()  # No second guardian and no second VM owner.

    def test_native_crash_watchdog_stops_owned_vm_without_server(self):
        with (mock.patch.object(terminal.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, 'sdk_root', return_value=self.sdk),
              mock.patch.object(terminal, '_env', return_value=self.env),
              mock.patch.object(terminal.os, 'kill', side_effect=ProcessLookupError),
              mock.patch.object(terminal, 'stop_runtime_vm') as stop):
            self.assertEqual(terminal._runtime_watchdog_main(['987654']), 0)
        stop.assert_called_once_with()

    def test_windows_shell_keeps_real_wine_msys2_and_writable_tmp(self):
        code = (ROOT / 'webapp/CinterForgeTerminal.py').read_text()
        self.assertIn("'--tmpfs', '/msys2/tmp:rw,nosuid,nodev,size=64m'", code)
        self.assertIn(r'Z:\msys2\usr\bin\bash.exe', code)
        self.assertIn("docker, 'exec'", code)  # tabs attach, not start containers
        self.assertIn("docker, 'run', '-d'", code)  # one persistent guest only
        self.assertIn("'--workdir', '/workspace' if workspace else '/'", code)
        self.assertIn('type=bind,src=/host/mac', code)


if __name__ == '__main__':
    unittest.main()
