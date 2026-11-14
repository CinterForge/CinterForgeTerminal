"""No-VM checks for Setup/launch/Code-OSS/quit connection ownership."""
from tests._support import *
from host import guest_runtime_acceptance as acceptance
from webapp import CinterForgeTerminal as terminal
from contextlib import nullcontext


class PersistentConnectionContractTests(unittest.TestCase):
    def setUp(self):
        parent = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent, prefix='guest-connections-')
        self.addCleanup(self.temp.cleanup)
        self.builds = Path(self.temp.name) / 'Builds'
        self.builds.mkdir()
        self.sdk = self.builds / 'CinterForgeTerminal/Toolchains'
        for part in ('mac/env/bin/colima', 'mac/env/bin/docker',
                     'mac/lima/bin/limactl', 'Windows/MSYS2/usr/bin/bash.exe'):
            path = self.sdk / part
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('test fixture')

    def test_bash_command_is_one_real_guest_exec_without_additional_vm(self):
        linux = terminal._runtime_guest_shell_command(self.sdk, 'linux', self.builds.parent)
        windows = terminal._runtime_guest_shell_command(self.sdk, 'win', self.builds.parent)
        self.assertEqual(linux[:3], [str(self.sdk / 'mac/env/bin/docker'), 'exec', '-it'])
        self.assertEqual(windows[:3], linux[:3])
        self.assertEqual(linux[-3:], ['/bin/bash', '--noprofile', '-i'])
        self.assertIn(r'Z:\msys2\usr\bin\bash.exe', windows)
        self.assertEqual(windows[-4:-1], ['--noprofile', '--norc', '-c'])
        self.assertIn('export PATH=/usr/bin:/ucrt64/bin:$PATH', windows[-1])
        self.assertIn('exec /usr/bin/bash --noprofile --norc -i', windows[-1])
        self.assertNotIn('--login', windows)
        self.assertNotIn('run', linux)
        self.assertNotIn('run', windows)

    def test_profile_free_bootstrap_runs_persistent_interactive_bash(self):
        # A read-only MSYS2 runtime cannot run /etc/profile's pacman-key
        # first-start hook. Check that the launch contract bypasses login
        # profiles and retains an interactive Bash with real coreutils PATH.
        args = terminal._runtime_guest_shell_command(self.sdk, 'win', self.builds.parent)
        self.assertNotIn('--login', args)
        self.assertEqual(args[-3:-1], ['--norc', '-c'])
        script = args[-1]
        with tempfile.TemporaryDirectory(dir=self.builds.parent) as home:
            Path(home, '.bash_profile').write_text('exit 97\n')
            Path(home, '.bashrc').write_text('exit 96\n')
            result = subprocess.run(
                ['/bin/bash', '--noprofile', '--norc', '-c', script],
                input='command -v rm; printf "CINTER_INTERACTIVE_PTY_READY\\n"; exit\n',
                text=True, capture_output=True, timeout=15,
                env=dict(os.environ, HOME=home, PATH='/bin'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/usr/bin/rm', result.stdout)
        self.assertIn('CINTER_INTERACTIVE_PTY_READY', result.stdout)

    def test_persistent_pty_readiness_checks_execution_not_echo(self):
        nonce = 'a' * 32
        class Shell:
            def __init__(self):
                self.writes = []
                self.calls = 0
                self.process = SimpleNamespace(poll=lambda: None)
            def write(self, value):
                self.writes.append(value)
            def poll(self, wait_ms):
                self.calls += 1
                if self.calls == 1:
                    return b'printf "CINTER_SHELL_READY:%s%s" "aaa" "bbb"', False
                return ('CINTER_SHELL_READY:' + nonce).encode('ascii'), False
        shell = Shell()
        with mock.patch.object(terminal.uuid, 'uuid4', return_value=SimpleNamespace(hex=nonce)):
            terminal._verify_persistent_guest_shell(shell, 'win', timeout=1)
        self.assertEqual(shell.calls, 2)
        self.assertEqual(shell.writes[-1], '\n')

    def test_receipt_is_test_only_evidence_not_an_implicit_setup(self):
        file = self.builds / 'Logs/CinterForgeTerminal/guest-connections-setup.json'
        with self.assertRaisesRegex(RuntimeError, 'receipt missing'):
            acceptance.verify_setup_receipt(self.builds)
        file.parent.mkdir(parents=True)
        fixture = {'schema': acceptance.SCHEMA, 'status': 'passed',
                   'sdk': str(self.sdk), 'checks': {check: 'passed' for check in acceptance.CHECKS}}
        file.write_text(json.dumps(fixture))
        self.assertEqual(acceptance.verify_setup_receipt(self.builds)['status'], 'passed')
        fixture['checks']['windows_bash'] = 'not-run'
        file.write_text(json.dumps(fixture))
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            acceptance.verify_setup_receipt(self.builds)

    def test_setup_uses_runtime_paths_and_closes_everything(self):
        shells = []
        def make_session(*args, **kwargs):
            shell = mock.Mock()
            shells.append(shell)
            return shell
        with (mock.patch.object(acceptance.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, '_env', return_value={'COLIMA_HOME': str(self.builds / 'vm')}),
              mock.patch.object(terminal, '_managed_vm_lock', return_value=nullcontext()),
              mock.patch.object(terminal, '_ensure_docker_locked', return_value='docker') as vm,
              mock.patch.object(terminal, '_start_guest_locked') as guest,
              mock.patch.object(terminal, 'TerminalSession', side_effect=make_session),
              mock.patch.object(terminal, '_runtime_guest_shell_command', return_value=['docker', 'exec']),
              mock.patch.object(terminal, '_guest_docker_context', return_value={'PATH': '/tools'}),
              mock.patch.object(terminal, '_verify_persistent_guest_shell') as shell_probe,
              mock.patch.object(terminal, 'stop_runtime_vm') as stop,
              mock.patch.object(acceptance.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1))):
            result = acceptance.run(self.builds)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(all(value == 'passed' for value in result['checks'].values()))
        vm.assert_called_once()
        self.assertEqual([call.args[3] for call in guest.call_args_list], ['linux', 'win'])
        self.assertEqual(shell_probe.call_count, 2)
        self.assertEqual(len(shells), 2)
        for shell in shells:
            shell.close.assert_called_once()
        stop.assert_called_once_with(setup_sdk=self.sdk)
        self.assertEqual(json.loads((self.builds / 'Logs/CinterForgeTerminal/guest-connections-setup.json')
                                    .read_text())['status'], 'passed')

    def test_setup_cleans_vm_and_connected_shell_after_failed_windows_probe(self):
        shells = []
        def make_session(*args, **kwargs):
            shell = mock.Mock()
            shells.append(shell)
            return shell
        with (mock.patch.object(acceptance.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, '_env', return_value={'COLIMA_HOME': str(self.builds / 'vm')}),
              mock.patch.object(terminal, '_managed_vm_lock', return_value=nullcontext()),
              mock.patch.object(terminal, '_ensure_docker_locked', return_value='docker'),
              mock.patch.object(terminal, '_start_guest_locked'),
              mock.patch.object(terminal, 'TerminalSession', side_effect=make_session),
              mock.patch.object(terminal, '_runtime_guest_shell_command', return_value=['docker', 'exec']),
              mock.patch.object(terminal, '_guest_docker_context', return_value={}),
              mock.patch.object(terminal, '_verify_persistent_guest_shell',
                                side_effect=[None, RuntimeError('Win Bash did not start')]),
              mock.patch.object(terminal, 'stop_runtime_vm') as stop,
              mock.patch.object(acceptance.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1))):
            with self.assertRaisesRegex(RuntimeError, 'Win Bash did not start'):
                acceptance.run(self.builds)
        stop.assert_called_once_with(setup_sdk=self.sdk)
        self.assertEqual(len(shells), 2)
        self.assertTrue(all(shell.close.called for shell in shells))
        self.assertEqual(json.loads((self.builds / 'Logs/CinterForgeTerminal/guest-connections-setup.json')
                                    .read_text())['status'], 'failed')

    def test_setup_does_not_stop_active_app_owner(self):
        receipt = self.builds / 'vm/CinterForgeTerminalLifetime.owner.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'native_pid': 1188, 'watchdog_pid': 2211}))
        with (mock.patch.object(acceptance.platform, 'system', return_value='Darwin'),
              mock.patch.object(terminal, '_env', return_value={'COLIMA_HOME': str(receipt.parent)}),
              mock.patch.object(terminal, '_managed_vm_lock', return_value=nullcontext()),
              mock.patch.object(terminal, '_live_runtime_watchdog', return_value=True),
              mock.patch.object(terminal, 'stop_runtime_vm') as stop):
            with self.assertRaisesRegex(RuntimeError, 'active native Terminal owns'):
                acceptance.run(self.builds)
        stop.assert_not_called()

    def test_code_oss_attach_and_tab_close_never_close_owned_shell(self):
        class Shell(terminal.TerminalSession):
            def __init__(self):
                self.closed = False
                self.process = SimpleNamespace(poll=lambda: None)
                self.closes = 0
            def close(self):
                self.closes += 1
        guest = Shell()
        server = SimpleNamespace(
            system='mac', code_closed_owners=set(), code_sessions={},
            code_sessions_lock=threading.Lock(), guest_shells={'linux': guest},
            guest_shells_lock=threading.RLock(),
            code_workspace=SimpleNamespace(details=lambda: {'cwd': str(self.builds.parent)}),
            devtools_requested=False)
        handler = terminal.Handler.__new__(terminal.Handler)
        handler.server = server
        id1 = 'terminal-abcde-1'
        result = handler._code_shell_action('/host/code-shell-open', {'id': id1, 'backend': 'linux'})
        self.assertTrue(result['reused'])
        self.assertIs(server.code_sessions[id1], guest)
        result = handler._code_shell_action('/host/code-shell-close', {'id': id1})
        self.assertEqual(result['closed'], True)
        self.assertEqual(guest.closes, 0)
        id2 = 'terminal-abcde-2'
        handler._code_shell_action('/host/code-shell-open', {'id': id2, 'backend': 'linux'})
        self.assertIs(server.code_sessions[id2], guest)
        self.assertEqual(guest.closes, 0)
        handler._code_shell_action('/host/code-shell-close-owner', {'owner': 'abcde'})
        self.assertEqual(guest.closes, 0)
        server.log_event = lambda message: None
        terminal._close_owned_guest_shells(server)
        self.assertEqual(guest.closes, 1)

    def test_vnc_health_monitor_reuses_existing_guest_and_repairs_in_place(self):
        with (mock.patch.object(terminal, '_env', return_value={'DOCKER_CONTEXT': 'colima-t'}),
              mock.patch.object(terminal, '_verify_guest_vnc') as probe,
              mock.patch.object(terminal, '_activate_guest_vnc') as recover):
            self.assertFalse(terminal._check_owned_guest_vnc(self.sdk, 'linux'))
            recover.assert_not_called()
            probe.side_effect = RuntimeError('VNC port closed')
            self.assertTrue(terminal._check_owned_guest_vnc(self.sdk, 'win'))
            recover.assert_called_once_with(str(self.sdk / 'mac/env/bin/docker'),
                                            {'DOCKER_CONTEXT': 'colima-t'}, 'win',
                                            terminal._guest_name('win'))
        # Recovery calls an idempotent display helper, never Docker run,
        # docker exec Bash, a second Colima VM, or Code-OSS PTY allocation.

    def test_novnc_startup_checks_real_listener_not_parent_bash_command(self):
        # A pgrep -f 'websockify.*6080 ' inside `bash -c <script>` matches
        # the bash process itself and silently skips startup. Exercise the
        # actual extracted shell predicate, not just its source spelling.
        import socket
        if shutil.which('bash') is None:
            self.skipTest('Bash required for guest startup shell regression')
        commands = []
        def call(docker, env, *argv, **kwargs):
            commands.append(argv)
            ready = 'CINTER_VNC_READY\n' if 'python3' in argv else ''
            return subprocess.CompletedProcess(argv, 0, ready, '')
        with (mock.patch.object(terminal, '_docker_call', side_effect=call),
              mock.patch.object(terminal, 'ASSETS', ROOT / 'webapp')):
            terminal._activate_guest_vnc('docker', {}, 'linux', terminal._guest_name('linux'))
        scripts = [args[args.index('-c') + 1] for args in commands
                   if '-c' in args and any('websockify --web' in part for part in args)]
        self.assertEqual(len(scripts), 1)
        script = scripts[0]
        self.assertNotIn("pgrep -f 'websockify", script)
        self.assertIn('socket.socket()', script)
        first = script.index('if ! python3 -c')
        start = script.index('if ! python3 -c', first + 1)  # noVNC port, not RFB
        snippet = script[start:script.index('\nfi', start) + len('\nfi')]
        with socket.socket() as listening:
            listening.bind(('127.0.0.1', 0))
            port = listening.getsockname()[1]
        directory = Path(self.temp.name)
        fakebin = directory / 'bin'
        fakebin.mkdir(exist_ok=True)
        marker = directory / 'websockify-was-invoked'
        executable = fakebin / 'websockify'
        executable.write_text('#!/bin/sh\nprintf "started\n" >> "$CINTER_WEB_MARKER"\n')
        executable.chmod(0o755)
        snippet = snippet.replace('\"/tmp/cinter-${PREFIX}-web.log\"', '\"' + str(directory / 'web.log') + '\"')
        # The helper takes distinct Linux/Windows port parameters; the test
        # supplies the same positional arguments as the real Docker exec.
        shell_args = ['bash', '-c', snippet, 'gui', ':99', '5900', str(port), '/dev/null', 'linux']
        env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ.get('PATH', ''),
                   CINTER_WEB_MARKER=str(marker), WEB=str(port), RFB='5900', PREFIX='linux')
        completed = subprocess.run(shell_args, env=env, capture_output=True,
                                   text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for _ in range(20):
            if marker.exists():
                break
            time.sleep(.05)
        self.assertTrue(marker.exists(), 'closed noVNC port must start websockify')
        self.assertEqual(marker.read_text().splitlines(), ['started'])
        # A healthy listener causes zero new launches, even though Bash's
        # command line still contains the literal `websockify` launch command.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', port))
            listener.listen(1)
            completed = subprocess.run(shell_args, env=env,
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(marker.read_text().splitlines(), ['started'])

    def test_gui_activation_verifies_rfb_and_novnc_no_second_display(self):
        commands = []
        def call(docker, env, *argv, **kwargs):
            commands.append(argv)
            if argv[:2] == ('exec', terminal._guest_name('linux')) and 'python3' in argv:
                return subprocess.CompletedProcess(argv, 0, 'CINTER_VNC_READY\n', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with (mock.patch.object(terminal, '_docker_call', side_effect=call),
              mock.patch.object(terminal, 'ASSETS', ROOT / 'webapp')):
            output = terminal._activate_guest_vnc('docker', {}, 'linux',
                                                  terminal._guest_name('linux'))
        self.assertIn('20601', output['viewer_url'])
        self.assertTrue(any('python3' in command for command in commands))
        self.assertFalse(any('run' in command for command in commands))


if __name__ == '__main__':
    unittest.main()
