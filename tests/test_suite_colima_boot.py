"""Mac bootstrap and c-only producer regression tests (no macOS VM required)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from host import mac_bootstrap
from linuxos import managed_backend


def _fake_env(builds):
    state = builds / 'CFTVM/U'
    return {'LIMA_HOME': str(state / 'l'), 'COLIMA_HOME': str(state / 'c'),
            'DOCKER_CONFIG': str(state / 'docker'),
            'DOCKER_HOST': f'unix://{state}/c/c/docker.sock',
            'CFOS_COLIMA_PROFILE': 'c'}


class ManagedColimaBootTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / 'Builds/Intermediates/CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='c-boot-', dir=root)
        self.addCleanup(temp.cleanup)
        self.builds = Path(temp.name) / 'Builds'
        self.builds.mkdir()

    def test_git_lfs_is_part_of_setup_managed_toolchain(self):
        self.assertIn('git-lfs=3.8.0', mac_bootstrap.PACKAGES)
        from host import devtooling
        self.assertIn(('Git LFS', 'git-lfs', ('--version',)), devtooling.MAC_TOOLS)
        with mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512), \
             mock.patch.object(managed_backend, 'backend_environment',
                               return_value=_fake_env(self.builds)):
            env = mac_bootstrap.managed_environment(self.builds)
        self.assertIn('/Toolchains/mac/env/bin', env['PATH'])

    def test_existing_mac_prefix_missing_git_lfs_is_repaired_during_setup(self):
        prefix = self.builds / 'CinterForgeTerminal/Toolchains/mac/env'
        for name in ('cmake', 'ninja', 'python3', 'node', 'go', 'pkg-config',
                     'colima', 'docker', 'docker-buildx', '7zz'):
            binary = prefix / 'bin' / name
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.touch()
        executed = []
        def fake_run(command, **kwargs):
            executed.append(command)
            if 'install' in command and 'git-lfs=3.8.0' in command:
                (prefix / 'bin/git-lfs').touch()
                (prefix / 'bin/skopeo').touch()
        explicit = subprocess.CompletedProcess([], 0, stdout=(
            '@EXPLICIT\nhttps://conda.anaconda.org/conda-forge/osx-arm64/git-lfs-3.8.0.conda\n'))
        with mock.patch.object(mac_bootstrap.platform, 'system', return_value='Darwin'), \
             mock.patch.object(mac_bootstrap.shutil, 'which', return_value='/usr/bin/xcrun'), \
             mock.patch.object(mac_bootstrap, '_mamba', return_value=Path('/fake/micromamba')), \
             mock.patch.object(mac_bootstrap, '_run', side_effect=fake_run), \
             mock.patch.object(mac_bootstrap.subprocess, 'run', return_value=explicit):
            mac_bootstrap.ensure_macos_tools(self.builds)
        self.assertTrue(any('git-lfs=3.8.0' in command for command in executed))

    def test_linuxkit_go_build_uses_existing_scratch_on_clean_setup(self):
        """Run a real offline Go build; the future c VM tmp must not be needed."""
        go_binary = shutil.which('go')
        git_binary = shutil.which('git')
        if not go_binary or not git_binary:
            self.skipTest('native Go and Git required for offline LinuxKit bootstrap smoke')
        tools = self.builds / 'CinterForgeTerminal/Toolchains/mac'
        managed_go = tools / 'env/bin/go'
        managed_go.parent.mkdir(parents=True, exist_ok=True)
        managed_go.symlink_to(go_binary)
        checkout = tools / 'linuxkit/source'
        source = checkout / 'src/cmd/linuxkit'
        source.mkdir(parents=True)
        (source / 'go.mod').write_text('module example.com/linuxkit-bootstrap-smoke\n\ngo 1.20\n')
        (source / 'main.go').write_text(
            'package main\nimport "fmt"\nfunc main() { fmt.Println("linuxkit test version") }\n')
        for args in (('init', '-q'), ('config', 'user.email', 'smoke@example.invalid'),
                     ('config', 'user.name', 'smoke'), ('add', '.'),
                     ('commit', '-qm', 'offline test fixture'), ('tag', 'v1.3.0')):
            subprocess.run([git_binary, '-C', str(checkout), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        missing_tmp = self.builds / 'CFTVM/U/tmp'
        self.assertFalse(missing_tmp.exists())
        environment = dict(os.environ, TMPDIR=str(missing_tmp), GOTMPDIR=str(missing_tmp))
        with mock.patch.object(mac_bootstrap, 'managed_environment', return_value=environment):
            executable = mac_bootstrap.ensure_linuxkit_cli(self.builds)
        self.assertTrue(executable.is_file())
        self.assertFalse(missing_tmp.exists())
        self.assertTrue((tools / 'go/tmp').is_dir())
        completed = subprocess.run([str(executable), 'version'], check=True,
                                   capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.stdout.strip(), 'linuxkit test version')

    def test_clean_bootstrap_provisions_tools_without_starting_any_vm(self):
        with mock.patch.object(mac_bootstrap, 'ensure_macos_tools', return_value={'python':'ok'}), \
             mock.patch.object(mac_bootstrap, '_stage_lima', return_value=Path('/staged/limactl')) as lima, \
             mock.patch.object(mac_bootstrap, 'ensure_qemu', return_value={'qemu-img':'/staged/qemu-img'}), \
             mock.patch.object(mac_bootstrap, '_ensure_buildx_plugin', return_value=Path('/staged/buildx')) as buildx, \
             mock.patch.object(mac_bootstrap, 'ensure_linuxkit_cli', return_value=Path('/staged/linuxkit')), \
             mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512), \
             mock.patch.object(managed_backend, 'backend_environment',
                               return_value=_fake_env(self.builds)), \
             mock.patch.object(managed_backend, 'ensure_backend') as start:
            result = mac_bootstrap.ensure(self.builds)
        self.assertEqual(result['linuxkit'], '/staged/linuxkit')
        self.assertEqual(result['lima'], '/staged/limactl')
        start.assert_not_called()
        lima.assert_called_once()
        buildx.assert_called_once()

    def test_c_is_only_backend_for_windows_and_kernel_builds(self):
        base = self.builds / 'CinterForgeTerminal/Toolchains/mac'
        for name in ('lima/bin/limactl', 'env/bin/colima', 'env/bin/docker',
                     'env/bin/docker-buildx'):
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        env = {'DOCKER_HOST': 'unix:///fake/c/docker.sock'}
        with mock.patch.object(mac_bootstrap.platform, 'system', return_value='Darwin'), \
             mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512), \
             mock.patch.object(managed_backend, 'backend_environment',
                               return_value=_fake_env(self.builds)), \
             mock.patch.object(mac_bootstrap, '_stage_lima'), \
             mock.patch.object(mac_bootstrap, '_ensure_buildx_plugin',
                               return_value=base / 'env/bin/docker-buildx'), \
             mock.patch.object(managed_backend, 'ensure_backend',
                               return_value=(str(base / 'env/bin/docker'), env)) as backend, \
             mock.patch.object(mac_bootstrap, '_probe',
                               return_value=subprocess.CompletedProcess([], 0, stdout='29.5.2')):
            result = mac_bootstrap.ensure_docker(self.builds)
        self.assertEqual(result, str(base / 'env/bin/docker'))
        backend.assert_called_once_with(managed_backend.source_directory(Path(mac_bootstrap.__file__).resolve().parents[1]))
        receipt = json.loads((self.builds / 'CFTVM/U/backend.json').read_text())
        self.assertEqual(receipt['profile'], 'c')
        self.assertEqual(receipt['state_root'], 'CFTVM/U')
        self.assertFalse((self.builds / 'CFTVM/colima/w').exists())

    def test_backend_errors_propagate_instead_of_starting_fallback_w(self):
        with mock.patch.object(mac_bootstrap.platform, 'system', return_value='Darwin'), \
             mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512), \
             mock.patch.object(managed_backend, 'backend_environment',
                               return_value=_fake_env(self.builds)), \
             mock.patch.object(mac_bootstrap, '_stage_lima'), \
             mock.patch.object(mac_bootstrap, '_ensure_buildx_plugin', return_value=Path('/bin/true')), \
             mock.patch.object(managed_backend, 'ensure_backend',
                               side_effect=RuntimeError('c boot failure')):
            with self.assertRaisesRegex(RuntimeError, 'c boot failure'):
                mac_bootstrap.ensure_docker(self.builds)
        self.assertFalse((self.builds / 'CFTVM/colima/w').exists())

    def test_cleanup_only_targets_c_even_on_failed_setup(self):
        with mock.patch.object(mac_bootstrap, 'stop_linuxkit_build_backend') as stop:
            mac_bootstrap.stop_setup_build_vms(self.builds)
        stop.assert_called_once_with(self.builds)

    def test_clean_backend_configuration_rejects_symlink(self):
        (self.builds / 'CFTVM').mkdir()
        outside = self.builds.parent / 'Other'
        outside.mkdir()
        (self.builds / 'CFTVM/U').symlink_to(outside, target_is_directory=True)
        with mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512):
            with self.assertRaisesRegex(RuntimeError, 'real directory'):
                mac_bootstrap.managed_state_root(self.builds)


    def test_foreign_backend_root_rejected_before_vm_start(self):
        wrong = dict(_fake_env(self.builds))
        wrong['DOCKER_HOST'] = 'unix:///foreign/docker.sock'
        wrong['LIMA_HOME'] = '/unrelated/lima'
        with mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512), \
             mock.patch.object(managed_backend, 'backend_environment', return_value=wrong), \
             mock.patch.object(managed_backend, 'ensure_backend') as boot:
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                mac_bootstrap.managed_environment(self.builds)
            boot.assert_not_called()

    def test_failed_c_docker_health_does_not_issue_success_receipt(self):
        root = self.builds / 'CinterForgeTerminal/Toolchains/mac'
        buildx = root / 'env/bin/docker-buildx'
        with mock.patch.object(mac_bootstrap.platform, 'system', return_value='Darwin'), \
             mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512), \
             mock.patch.object(managed_backend, 'backend_environment',
                               return_value=_fake_env(self.builds)), \
             mock.patch.object(mac_bootstrap, '_stage_lima'), \
             mock.patch.object(mac_bootstrap, '_ensure_buildx_plugin', return_value=buildx), \
             mock.patch.object(managed_backend, 'ensure_backend',
                               return_value=('/mock/docker', _fake_env(self.builds))), \
             mock.patch.object(mac_bootstrap, '_probe',
                               return_value=subprocess.CompletedProcess([], 1, stdout='Docker failed')):
            with self.assertRaisesRegex(RuntimeError, 'not operational'):
                mac_bootstrap.ensure_docker(self.builds)
        self.assertFalse((self.builds / 'CFTVM/U/backend.json').exists())


if __name__ == '__main__':
    unittest.main()
