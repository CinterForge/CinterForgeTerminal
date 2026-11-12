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

    def test_host_mounts_cover_root_and_enumerated_external_drives(self):
        with (mock.patch.object(terminal.platform, 'system', return_value='Darwin'),
              mock.patch.object(Path, 'is_dir', return_value=True),
              mock.patch.object(terminal, '_mac_mounted_volumes', return_value=[
                  Path('/Volumes/Work'), Path('/Volumes/External Drive'),
                  Path('/System/Volumes/Data')])):
            mounts = terminal._mac_host_mounts()
        self.assertIn((Path('/'), '/host/mac'), mounts)
        self.assertIn((Path('/Volumes'), '/host/volumes'), mounts)
        self.assertTrue(any(p == Path('/Volumes/Work') and g.startswith('/host/drives/')
                            for p, g in mounts))
        # Spaces are handled via /Volumes, not by a broken direct Colima path.
        self.assertFalse(any(p == Path('/Volumes/External Drive') for p, _ in mounts))
        self.assertNotIn((Path('/System/Volumes/Data'), '/System/Volumes/Data'), mounts)

    def test_colima_yaml_preserves_unrelated_settings_and_is_idempotent(self):
        config = self.colima / terminal.PROFILE / 'colima.yaml'
        config.parent.mkdir()
        config.write_text('cpu: 5\nmounts:\n  - location: ~/Source\n    writable: false\ndocker: {}\n')
        mounts = [(Path('/'), '/host/mac'), (Path('/Volumes'), '/host/volumes')]
        terminal._configure_colima_host_mounts(self.env, mounts)
        actual = config.read_text()
        self.assertIn('cpu: 5', actual)
        self.assertIn('docker: {}', actual)
        self.assertIn('location: "/"', actual)
        self.assertIn('mountPoint: "/host/mac"', actual)
        self.assertIn('mountPoint: "/host/volumes"', actual)
        self.assertNotIn('~/Source', actual)
        terminal._configure_colima_host_mounts(self.env, mounts)
        self.assertEqual(config.read_text(), actual)

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
                  (Path('/'), '/host/mac'), (Path('/Volumes'), '/host/volumes')]),
              mock.patch.object(terminal.subprocess, 'run', side_effect=run),
              mock.patch.object(terminal, '_run_checked', side_effect=checked)):
            result = terminal._ensure_docker(self.sdk, self.env, self.workspace)
        self.assertTrue(result.endswith('/mac/env/bin/docker'))
        args = state['start_args']
        self.assertIn('--mount-type', args)
        self.assertIn('virtiofs', args)
        self.assertNotIn('--mount', args)  # custom mountPoint comes from profile YAML
        config = (self.colima / terminal.PROFILE / 'colima.yaml').read_text()
        self.assertIn('mountPoint: "/host/mac"', config)
        self.assertIn('mountPoint: "/host/volumes"', config)

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
