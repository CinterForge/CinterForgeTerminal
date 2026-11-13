"""Offline contracts: Setup tests real X11, RFB and noVNC on both guests."""
from tests._support import *
from host import gui_preflight
from host import three_samples
from host import windows_environment


class GuestGUIPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.builds = Path(self.temp.name) / 'Builds'
        self.builds.mkdir()

    @staticmethod
    def fake_result(command, *, stdout, **kwargs):
        guest = command[-1]
        assert guest in ('linux', 'windows')
        stdout.write(f'[GUI PREFLIGHT] PASS {guest} X11 Xvfb :99 (1280x800x24)\n')
        stdout.write(f'[GUI PREFLIGHT] PASS {guest} VNC TCP/RFB 127.0.0.1:5900\n')
        stdout.write(f'[GUI PREFLIGHT] PASS {guest} noVNC/websockify 127.0.0.1:6080\n')
        stdout.flush()
        return SimpleNamespace(returncode=0)

    def test_linux_and_windows_use_real_isolated_display_probe_and_write_receipts(self):
        with mock.patch.object(gui_preflight.subprocess, 'run', side_effect=self.fake_result) as run:
            for guest in ('linux', 'windows'):
                data = gui_preflight.verify_guest_gui(self.builds, guest=guest,
                    docker='/usr/bin/docker', image='cinter-gui-image:test')
                self.assertEqual(data['status'], 'passed')
                self.assertEqual(data['checks'], {'x11': 'passed', 'vnc': 'passed', 'novnc': 'passed'})
                receipt = self.builds / f'Logs/CinterForgeTerminal/{guest}-gui-preflight.json'
                self.assertEqual(json.loads(receipt.read_text())['status'], 'passed')
                self.assertIn('VNC TCP/RFB', receipt.with_suffix('.log').read_text())
                command = run.call_args.args[0]
                self.assertEqual(command[-5:], ['python3', '-u', gui_preflight.IMAGE_SCRIPT, '--platform', guest])
                self.assertIn('--network', command)
                self.assertEqual(command[command.index('--network') + 1], 'none')
                self.assertNotIn('wineboot', command)

    def test_partial_x11_pass_does_not_claim_vnc_pass(self):
        def incomplete(cmd, *, stdout, **kwargs):
            stdout.write('[GUI PREFLIGHT] PASS linux X11 Xvfb :99\n')
            stdout.flush()
            return SimpleNamespace(returncode=0)
        with mock.patch.object(gui_preflight.subprocess, 'run', side_effect=incomplete):
            with self.assertRaisesRegex(RuntimeError, 'readiness failed'):
                gui_preflight.verify_guest_gui(self.builds, guest='linux',
                    docker='docker', image='linux-sample:1')
        self.assertEqual(json.loads((self.builds / 'Logs/CinterForgeTerminal/linux-gui-preflight.json')
                                    .read_text())['checks']['vnc'], 'failed')

    def test_failed_probe_cannot_be_reported_as_passed(self):
        def crashed(cmd, *, stdout, **kwargs):
            stdout.write('[GUI PREFLIGHT] FAIL windows: cannot connect RFB\n')
            stdout.flush()
            return SimpleNamespace(returncode=17)
        with mock.patch.object(gui_preflight.subprocess, 'run', side_effect=crashed):
            with self.assertRaisesRegex(RuntimeError, 'cannot connect RFB'):
                gui_preflight.verify_guest_gui(self.builds, guest='windows',
                    docker='docker', image='windows-cross:1')
        receipt = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-gui-preflight.json').read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['exit_code'], 17)

    def test_timeout_writes_failure_evidence(self):
        with mock.patch.object(gui_preflight.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 3)):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                gui_preflight.verify_guest_gui(self.builds, guest='linux',
                    docker='docker', image='linux:1', timeout=3)
        receipt = json.loads((self.builds / 'Logs/CinterForgeTerminal/linux-gui-preflight.json').read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('timed out', receipt['error'])

    def test_both_images_copy_same_setup_probe_and_invalidate_on_change(self):
        script = Path(gui_preflight.SOURCE_SCRIPT)
        self.assertTrue(script.is_file())
        for dockerfile in (three_samples.LINUX_DOCKERFILE, windows_environment.DOCKERFILE):
            self.assertIn('COPY gui_preflight.py /usr/local/lib/cinterforge/gui_preflight.py',
                          dockerfile.read_text())
            self.assertIn('x11vnc', dockerfile.read_text())
            self.assertIn('novnc', dockerfile.read_text())
        self.assertEqual(len(three_samples.linux_recipe_sha256()), 64)
        self.assertEqual(len(windows_environment._image_recipe_sha256()), 64)
        self.assertNotEqual(three_samples.linux_recipe_sha256(),
                            hashlib.sha256(three_samples.LINUX_DOCKERFILE.read_bytes()).hexdigest())
        self.assertNotEqual(windows_environment._image_recipe_sha256(),
                            hashlib.sha256(windows_environment.DOCKERFILE.read_bytes()).hexdigest())

class SetupGUIOrderingTests(unittest.TestCase):
    def test_setup_verifies_both_guests_before_first_wineboot(self):
        from host import windows_dependencies
        with tempfile.TemporaryDirectory() as folder:
            builds = Path(folder) / 'Builds'
            builds.mkdir()
            cross = mock.Mock()
            cross.provider = 'docker-linux-amd64-wine'
            cross.docker = 'docker'
            cross.image = 'windows-cross:patched'
            cross.version_probes.return_value = {'wine': {'output': 'wine-11.15'}}
            cross.execute.return_value = SimpleNamespace(returncode=0, stdout='ready')
            order = []
            def gui_probe(root, *, guest, docker, image):
                order.append('gui-' + guest)
                return {'status': 'passed'}
            def wine_boot(*args, **kwargs):
                order.append('wineboot')
                raise RuntimeError('stop after preflights')
            with mock.patch.object(windows_dependencies, 'WindowsCrossEnvironment', return_value=cross), \
                 mock.patch('host.three_samples._docker_image', return_value='linux-sample:verified'), \
                 mock.patch.object(windows_dependencies, 'verify_guest_gui', side_effect=gui_probe), \
                 mock.patch.object(windows_dependencies, '_verify_wine_runtime', side_effect=wine_boot):
                with self.assertRaisesRegex(RuntimeError, 'stop after preflights'):
                    windows_dependencies.ensure(builds)
            self.assertEqual(order, ['gui-linux', 'gui-windows', 'wineboot'])
            self.assertEqual(json.loads((builds / 'Logs/CinterForgeTerminal/windows-dependencies.json')
                                        .read_text())['status'], 'failed')
