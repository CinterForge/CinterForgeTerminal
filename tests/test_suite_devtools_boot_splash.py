"""Startup UI fallback and per-guest Bash readiness; no Colima or network needed."""
from tests._support import *
from webapp import CinterForgeTerminal as terminal
from types import SimpleNamespace

class DevToolsSplashTests(unittest.TestCase):
    def server(self, state='starting'):
        events = []
        server = SimpleNamespace(system='mac', guest_start_lock=threading.RLock(),
            guest_start_state=state, guest_start_error='', guest_ready=[], guest_errors={},
            native_parent_pid=1001, mutable_builds=Path('/test/Builds'),
            guest_shells={}, guest_shells_lock=threading.RLock(),
            _stopping=False, log_event=events.append)
        return server, events

    def test_splash_gates_only_mac_booting_with_devtools(self):
        server, _ = self.server()
        self.assertTrue(terminal._boot_splash_required(server, '/', ''))
        self.assertTrue(terminal._boot_splash_required(server, '/index.html', ''))
        self.assertFalse(terminal._boot_splash_required(server, '/', 'devtools-boot-complete=1'))
        self.assertFalse(terminal._boot_splash_required(server, '/', 'accept-ui=1'))
        self.assertFalse(terminal._boot_splash_required(server, '/terminal.js', ''))
        server.system = 'win'
        self.assertFalse(terminal._boot_splash_required(server, '/', ''))
        server.system = 'mac'
        for state in ('ready', 'failed', 'not-installed'):
            server.guest_start_state = state
            self.assertFalse(terminal._boot_splash_required(server, '/', ''))

    def test_failed_wine_preserves_linux_guest_ready(self):
        server, events = self.server()
        result = {'installed': True, 'guests': ['linux'],
                  'errors': {'win': 'wine bash exited 127'}, 'docker': 'one-managed-engine'}
        with mock.patch.object(terminal, '_start_runtime_watchdog'), \
             mock.patch.object(terminal, '_runtime_logs_root', return_value=Path('/test/Logs')), \
             mock.patch.object(terminal, 'boot_runtime_guests', return_value=result), \
             mock.patch.object(terminal, 'sdk_root', return_value=Path('/sdk')), \
             mock.patch.object(terminal, '_new_owned_guest_shell', return_value=mock.Mock()) as bash, \
             mock.patch.object(terminal.threading, 'Thread') as monitor, \
             mock.patch.object(terminal, 'stop_runtime_vm') as stop:
            terminal._boot_guests_logged(server)
        bash.assert_called_once_with(server, 'linux', Path('/sdk'))
        self.assertIn('linux', server.guest_shells)
        monitor.assert_called_once()
        self.assertEqual(server.guest_start_state, 'failed')
        self.assertEqual(terminal._guest_boot_snapshot(server)['guests'], ['linux'])
        self.assertIn('wine bash exited 127', terminal._guest_boot_snapshot(server)['error'])
        self.assertFalse(terminal._boot_splash_required(server, '/', ''))
        self.assertTrue(any('PARTIAL/FAILED' in item for item in events))
        stop.assert_not_called()  # Leave the still-running Linux guest alive.
        with mock.patch.object(terminal, '_api', return_value=terminal._guest_boot_snapshot(server)):
            terminal._wait_for_app_guests('linux', timeout_seconds=1)
            with self.assertRaisesRegex(RuntimeError, 'wine bash exited 127'):
                terminal._wait_for_app_guests('win', timeout_seconds=1)

    def test_two_guests_boot_independently_under_one_vm(self):
        calls = []
        def start(sdk, env, docker, kind, workspace):
            calls.append(kind)
            if kind == 'win': raise RuntimeError('MSYS2 Bash exited 127')
        with mock.patch.object(terminal.platform, 'system', return_value='Darwin'), \
             mock.patch.object(terminal, 'sdk_root', return_value=Path('/sdk')), \
             mock.patch.object(terminal, '_env', return_value={'COLIMA_HOME':'/tmp'}), \
             mock.patch.object(terminal, '_guest_workspace', return_value=None), \
             mock.patch.object(terminal, '_managed_vm_lock'), \
             mock.patch.object(terminal, '_ensure_docker_locked', return_value='docker'), \
             mock.patch.object(terminal, '_start_guest_locked', side_effect=start):
            result = terminal.boot_runtime_guests()
        self.assertEqual(calls, ['linux', 'win'])
        self.assertEqual(result['guests'], ['linux'])
        self.assertIn('127', result['errors']['win'])

    def test_browser_splash_has_bounded_mac_bash_fallback_and_auth(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / 'webapp/startup.js').read_text()
        html = (root / 'webapp/startup.html').read_text()
        server = (root / 'webapp/CinterForgeTerminal.py').read_text()
        self.assertIn('Initializing DevTools...', html)
        self.assertIn('__CPH_TOKEN__', html)
        self.assertIn("'/api/lifecycle/guest-status'", js)
        self.assertIn("'X-CPH-Token': token", js)
        self.assertIn("data.state === 'failed'", js)
        self.assertIn("window.location.replace('/?devtools-boot-complete=1')", js)
        self.assertIn('5 * 60 * 1000', js)
        self.assertIn("'startup.html'", server)
        self.assertIn("'/startup.js'", server)

    def test_all_stage_paths_include_splash(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('host/universal_package.py', 'host/mac_app.py',
                     'host/manager.py', 'host/bootstrap_bundle.py'):
            text = (root / name).read_text()
            self.assertIn("'startup.html'", text, name)
            self.assertIn("'startup.js'", text, name)

    def test_new_install_does_not_attach_guest_before_app_relaunch(self):
        with mock.patch.object(terminal.platform, 'system', return_value='Darwin'), \
             mock.patch.object(terminal, 'sdk_root', side_effect=[None, Path('/sdk')]), \
             mock.patch.object(terminal, 'install_devtools', return_value=0), \
             mock.patch.object(terminal, '_wait_for_app_guests') as attach, \
             mock.patch.object(terminal.subprocess, 'Popen') as child:
            self.assertEqual(terminal.guest_shell_main('linux'), 0)
        attach.assert_not_called()
        child.assert_not_called()

    def test_installer_requests_app_relaunch_not_vm_boot(self):
        code = (Path(__file__).resolve().parents[1] / 'webapp/CinterForgeTerminal.py').read_text()
        section = code.split('def install_devtools()')[1].split('\ndef _api(')[0]
        self.assertIn("_api('/api/lifecycle/reboot', body={'component': 'app'})", section)
        self.assertNotIn("_api('/api/lifecycle/guest-boot'", section)

if __name__ == '__main__':
    unittest.main()
