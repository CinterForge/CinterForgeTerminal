"""No-VM regression checks for precise Windows Bash crash-stage reporting."""
from tests._support import *
from webapp import CinterForgeTerminal as terminal


class WindowsGuestProbeTests(unittest.TestCase):
    def test_windows_success_requires_real_wine_and_msys2(self):
        seen = []

        def docker_call(docker, env, *args, **kwargs):
            seen.append(args)
            command = ' '.join(args)
            if '/bin/bash --version' in command:
                output = 'GNU bash, version 5.2'
            elif 'wine --version' in command:
                output = 'wine-11.15'
            elif 'cmd.exe /c echo' in command:
                output = 'CINTER_WINE_PE_READY'
            else:
                self.assertIn(r'Z:\msys2\usr\bin\bash.exe', command)
                output = 'MSYS_NT-10.0'
            return subprocess.CompletedProcess(args, 0, output, '')

        output = io.StringIO()
        with (mock.patch.object(terminal, '_docker_call', side_effect=docker_call),
              contextlib.redirect_stdout(output)):
            terminal._verify_guest_bash('docker', {'PATH': '/fake'}, 'win', 'owned')
        self.assertEqual(len(seen), 4)
        self.assertIn('PASS Wine Windows PE execution', output.getvalue())
        self.assertIn('PASS MSYS2 UCRT64 Bash + coreutils', output.getvalue())

    def test_segmentation_fault_is_attributed_to_exact_layer(self):
        calls = []

        def docker_call(docker, env, *args, **kwargs):
            calls.append(args)
            line = ' '.join(args)
            if '/bin/bash --version' in line:
                return subprocess.CompletedProcess(args, 0, 'GNU bash, version 5.2', '')
            if 'wine --version' in line:
                return subprocess.CompletedProcess(args, 0, 'wine-11.15', '')
            return subprocess.CompletedProcess(args, 139, '', '')

        with mock.patch.object(terminal, '_docker_call', side_effect=docker_call):
            with self.assertRaisesRegex(RuntimeError,
                                        r'139.*SIGSEGV.*during Wine Windows PE execution.*no guest output'):
                terminal._verify_guest_bash('docker', {}, 'win', 'owned')
        self.assertEqual(len(calls), 3)

    def test_timeout_is_reported_with_stage_not_silently_retried(self):
        def docker_call(docker, env, *args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])

        with mock.patch.object(terminal, '_docker_call', side_effect=docker_call) as call:
            with self.assertRaisesRegex(RuntimeError, r'timed out during amd64 Linux userspace'):
                terminal._verify_guest_bash('docker', {}, 'win', 'owned')
        self.assertEqual(call.call_count, 1)

    def test_successful_wine_but_bad_msys2_still_fails(self):
        def docker_call(docker, env, *args, **kwargs):
            line = ' '.join(args)
            if '/bin/bash --version' in line:
                output = 'GNU bash'
            elif 'wine --version' in line:
                output = 'wine-11.15'
            elif 'cmd.exe /c echo' in line:
                output = 'CINTER_WINE_PE_READY'
            else:
                return subprocess.CompletedProcess(args, 139, '', 'qemu: guest SIGSEGV')
            return subprocess.CompletedProcess(args, 0, output, '')

        with mock.patch.object(terminal, '_docker_call', side_effect=docker_call):
            with self.assertRaisesRegex(RuntimeError,
                                        r'139.*during MSYS2 UCRT64 Bash \+ coreutils.*guest SIGSEGV'):
                terminal._verify_guest_bash('docker', {}, 'win', 'owned')
