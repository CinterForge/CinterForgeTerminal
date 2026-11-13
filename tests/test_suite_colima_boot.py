"""Setup-only Lima/Colima boot regression tests (no macOS VM required)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from host import mac_bootstrap


class ManagedColimaBootTests(unittest.TestCase):
    def setUp(self):
        # The actual macOS Lima socket limit applies to test fixtures too.
        # Put the fake Builds next to the repository's real sibling Builds,
        # rather than nesting it under Builds/CFTVM/tmp (too deep on macOS).
        sibling_builds = Path(os.environ.get(
            'CINTER_BUILDS_ROOT', str(Path(__file__).resolve().parents[2] / 'Builds'))).resolve()
        sibling_builds.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='b-', dir=sibling_builds)
        self.addCleanup(temp.cleanup)
        self.builds = Path(temp.name) / 'Builds'
        self.builds.mkdir()
        self.state = mac_bootstrap.managed_state_root(self.builds)
        self.sshdir = self.state / 'lima/colima-w'
        self.sshdir.mkdir(parents=True)

    def test_guest_ssh_uses_managed_config_and_verifies_host_key(self):
        (self.sshdir / 'ssh.config').write_text('Host lima-colima-w\n')
        ok = subprocess.CompletedProcess([], 0, 'READY\n')
        with mock.patch.object(mac_bootstrap, '_probe', return_value=ok) as probe:
            self.assertIn('READY', mac_bootstrap._guest_ssh(self.state, 'echo READY'))
        cmd = probe.call_args.args[0]
        self.assertEqual(cmd[0], '/usr/bin/ssh')
        self.assertIn(str(self.sshdir / 'ssh.config'), cmd)
        self.assertIn('BatchMode=yes', cmd)
        self.assertNotIn('StrictHostKeyChecking=no', cmd)
        self.assertTrue(cmd[-1].startswith('sudo -n /bin/sh -c '))

    def test_boot_diagnostic_writes_cloudinit_and_host_log(self):
        (self.sshdir / 'ha.stderr.log').write_text('lima-boot-done marker absent\n')
        with mock.patch.object(mac_bootstrap, '_guest_ssh', return_value='cloud-final.service failed\n'):
            path, content = mac_bootstrap._colima_boot_diagnostics(self.builds, 'boot stuck')
        self.assertIn('cloud-final', content)
        self.assertIn('boot stuck', path.read_text())
        self.assertIn('lima-boot-done marker absent', path.read_text())
        self.assertTrue(str(path).startswith(str(self.builds / 'Logs/CinterForgeTerminal')))

    def test_only_failed_cloud_final_is_restarted(self):
        failed = ('/run/lima-reboot-required: ABSENT\n'
                  'cloud-final.service - Cloud-init final\nActive: failed')
        with mock.patch.object(mac_bootstrap, '_guest_ssh', return_value='rc=0') as guest:
            self.assertTrue(mac_bootstrap._retry_failed_cloud_final(self.state, failed))
            guest.assert_called_once()
            self.assertIn('systemctl restart cloud-final.service', guest.call_args.args[1])
        with mock.patch.object(mac_bootstrap, '_guest_ssh') as guest:
            self.assertFalse(mac_bootstrap._retry_failed_cloud_final(
                self.state, failed.replace('Active: failed', 'Active: activating')))
            self.assertFalse(mac_bootstrap._retry_failed_cloud_final(
                self.state, failed.replace('ABSENT', 'PRESENT')))
            guest.assert_not_called()

    def test_docker_restart_is_guarded_by_both_current_boot_markers(self):
        with mock.patch.object(mac_bootstrap, '_guest_ssh', return_value='rc=0\nCINTER_DOCKER_STARTED') as guest:
            self.assertTrue(mac_bootstrap._start_docker_after_lima_boot(self.state))
            script = guest.call_args.args[1]
            self.assertIn('cmp -s /run/lima-boot-done /run/lima-ssh-ready', script)
            self.assertIn('! test -e /run/lima-reboot-required', script)
            self.assertIn('! systemctl is-active --quiet docker.service', script)
        with mock.patch.object(mac_bootstrap, '_guest_ssh', return_value='rc=0'):
            self.assertFalse(mac_bootstrap._start_docker_after_lima_boot(self.state))

    def test_colima_exits_success_without_recovery(self):
        process = mock.Mock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with mock.patch.object(mac_bootstrap.subprocess, 'Popen', return_value=process), \
             mock.patch.object(mac_bootstrap, '_colima_boot_diagnostics') as diagnostics:
            mac_bootstrap._start_colima_checked(['colima', 'start', 'w'],
                builds=self.builds, env={}, total_timeout=1, poll_interval=0.01)
            diagnostics.assert_not_called()
            process.terminate.assert_not_called()

    def test_colima_stall_logs_evidence_and_terminates_child(self):
        process = mock.Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired(['colima', 'start'], 0.01), 0]
        process.poll.return_value = None
        with mock.patch.object(mac_bootstrap.subprocess, 'Popen', return_value=process), \
             mock.patch.object(mac_bootstrap, '_colima_boot_diagnostics',
                               return_value=(self.builds / 'Logs/colima.log', 'no marker')) as diagnose, \
             mock.patch.object(mac_bootstrap.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, 'did not complete Lima boot scripts'):
                mac_bootstrap._start_colima_checked(['colima', 'start', 'w'],
                    builds=self.builds, env={}, total_timeout=1, poll_interval=0.01)
        diagnose.assert_called_once()
        process.terminate.assert_called_once()

    def test_failed_colima_launch_stops_only_own_profile(self):
        toolbase = self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin'
        toolbase.mkdir(parents=True)
        (toolbase / 'docker').touch()
        (toolbase / 'colima').touch()
        with mock.patch.object(mac_bootstrap.platform, 'system', return_value='Darwin'), \
             mock.patch.object(mac_bootstrap, '_stage_lima'), \
             mock.patch.object(mac_bootstrap, 'managed_environment', return_value={}), \
             mock.patch.object(mac_bootstrap, '_ensure_buildx_plugin'), \
             mock.patch.object(mac_bootstrap, '_run'), \
             mock.patch.object(mac_bootstrap, '_probe',
                               return_value=subprocess.CompletedProcess([], 1, 'unavailable')), \
             mock.patch.object(mac_bootstrap, '_start_colima_checked',
                               side_effect=RuntimeError('boot stage failed')), \
             mock.patch.object(mac_bootstrap, '_stop_colima_profile') as stop:
            with self.assertRaisesRegex(RuntimeError, 'boot stage failed'):
                mac_bootstrap.ensure_docker(self.builds)
        self.assertTrue(stop.called)
        self.assertTrue(all(call.args[1] == 'w' for call in stop.call_args_list))


if __name__ == '__main__':
    unittest.main()
