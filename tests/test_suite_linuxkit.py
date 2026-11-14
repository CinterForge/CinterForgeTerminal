"""Focused linuxkit regression contracts. Native target acceptance is separate."""
from tests._support import *

class ManagedLimaEntitlements(unittest.TestCase):
    def setUp(self):
        root = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        root.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root, prefix='lima-sign-')
        self.addCleanup(self.directory.cleanup)
        self.binary = Path(self.directory.name) / 'lima/bin/limactl'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b'mocked staged official limactl binary')

    def test_missing_vz_entitlement_signs_managed_binary_and_rechecks(self):
        current = {'signed': False}
        changes = []

        def codesign(command, **kwargs):
            command = list(command)
            if '--verify' in command:
                return subprocess.CompletedProcess(command, 0 if current['signed'] else 1,
                                                   stdout=b'', stderr=b'')
            if '--display' in command:
                values = {'com.apple.security.network.client': True}
                if current['signed']:
                    values['com.apple.security.virtualization'] = True
                return subprocess.CompletedProcess(command, 0, stdout=plistlib.dumps(values), stderr=b'')
            self.assertIn('--force', command)
            self.assertEqual(command[-1], str(self.binary))
            plist = Path(command[command.index('--entitlements') + 1])
            rights = plistlib.loads(plist.read_bytes())
            self.assertTrue(rights['com.apple.security.virtualization'])
            self.assertTrue(rights['com.apple.security.network.client'])
            self.assertTrue(rights['com.apple.security.network.server'])
            changes.append(command)
            current['signed'] = True
            return subprocess.CompletedProcess(command, 0, stdout=b'', stderr=b'')

        with mock.patch.object(mac.subprocess, 'run', side_effect=codesign):
            mac._ensure_lima_vz_entitlement(self.binary)
        self.assertEqual(len(changes), 1)

    def test_already_signed_managed_binary_is_not_modified(self):
        calls = []

        def codesign(command, **kwargs):
            calls.append(command)
            if '--display' in command:
                return subprocess.CompletedProcess(command, 0,
                    stdout=plistlib.dumps({'com.apple.security.virtualization': True}), stderr=b'')
            return subprocess.CompletedProcess(command, 0, stdout=b'', stderr=b'')

        with mock.patch.object(mac.subprocess, 'run', side_effect=codesign):
            mac._ensure_lima_vz_entitlement(self.binary)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.binary.parent.parent / 'vz-entitlements.plist').exists())

    def test_signing_without_effective_entitlement_fails_setup(self):
        commands = []

        def codesign(command, **kwargs):
            commands.append(command)
            if '--display' in command:
                return subprocess.CompletedProcess(command, 0,
                    stdout=plistlib.dumps({'com.apple.security.network.client': True}), stderr=b'')
            return subprocess.CompletedProcess(command, 0, stdout=b'', stderr=b'')

        with mock.patch.object(mac.subprocess, 'run', side_effect=codesign):
            with self.assertRaisesRegex(RuntimeError, 'not signed with'):
                mac._ensure_lima_vz_entitlement(self.binary)
        self.assertTrue(any('--force' in cmd for cmd in commands))

    def test_cached_lima_archive_still_checks_entitlement(self):
        tool = Path(self.directory.name) / 'CinterForgeTerminal/Toolchains/mac'
        binary = tool / 'lima/bin/limactl'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'cached Lima')
        for label, sha in (('lima', mac.LIMA_SHA256), ('guestagents', mac.LIMA_AGENTS_SHA256)):
            (tool / 'lima' / (label + '.sha256')).write_text(sha + '\n')
            archive = tool / 'downloads' / (f'{label}-{mac.LIMA_VERSION}-arm64.tar.gz')
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b'mocked archive')
        with mock.patch.object(mac, '_file_sha256', side_effect=lambda p:
                               mac.LIMA_SHA256 if Path(p).name.startswith('lima-') else mac.LIMA_AGENTS_SHA256), \
             mock.patch.object(mac, '_ensure_lima_vz_entitlement') as signer:
            self.assertEqual(mac._stage_lima(Path(self.directory.name)), binary)
        signer.assert_called_once_with(binary)

def _fake_c_environment(builds):
    state = builds / 'CFTVM/U'
    env = {k: v for k, v in os.environ.items()
           if k not in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
    return dict(env, LIMA_HOME=str(state / 'l'),
                COLIMA_HOME=str(state / 'c'), DOCKER_CONFIG=str(state / 'docker'),
                DOCKER_HOST=f'unix://{state}/c/c/docker.sock',
                CFOS_COLIMA_PROFILE='c')


class ManagedMacSocketPaths(unittest.TestCase):
    def test_managed_socket_is_short_without_external_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            builds = Path(directory) / 'Builds'
            builds.mkdir()
            with mock.patch.object(mac, 'SOCKET_PATH_LIMIT', 512), \
                 mock.patch('linuxos.managed_backend.backend_environment',
                            return_value=_fake_c_environment(builds)):
                env = mac.managed_environment(builds)
            state = builds / mac.VM_STATE_FOLDER
            self.assertEqual(Path(env['LIMA_HOME']), state / 'l')
            self.assertEqual(Path(env['COLIMA_HOME']), state / 'c')
            self.assertEqual(Path(env['DOCKER_CONFIG']), state / 'docker')
            self.assertEqual(env['CFOS_COLIMA_PROFILE'], 'c')
            self.assertNotIn('DOCKER_CONTEXT', env)
            self.assertIn('CFTVM/U/c/c/docker.sock', env['DOCKER_HOST'])
            self.assertIn('docker-buildx=0.35.0', mac.PACKAGES)
            socket = (Path(env['LIMA_HOME']) / ('colima-' + mac.COLIMA_PROFILE)
                      / 'ssh.sock.1234567890123456')
            retired = (builds / 'CinterForgeTerminal/Toolchains/mac/container/lima'
                       / 'colima-w/ssh.sock.1234567890123456')
            self.assertLess(len(os.fsencode(socket)), len(os.fsencode(retired)))
            self.assertFalse(state.is_symlink())

    def test_foreign_docker_host_cannot_override_managed_context(self):
        with tempfile.TemporaryDirectory() as directory:
            builds = Path(directory) / 'Builds'
            builds.mkdir()
            with mock.patch.dict(os.environ, {
                'DOCKER_HOST': 'tcp://some-other-engine:2375',
                'DOCKER_CONTEXT': 'foreign',
                'DOCKER_TLS_VERIFY': '1',
                'DOCKER_CERT_PATH': str(Path(directory) / 'Other'),
            }):
                with mock.patch.object(mac, 'SOCKET_PATH_LIMIT', 512), \
                     mock.patch('linuxos.managed_backend.backend_environment',
                                return_value=_fake_c_environment(builds)):
                    env = mac.managed_environment(builds)
            self.assertEqual(env['DOCKER_HOST'],
                             f'unix://{builds}/CFTVM/U/c/c/docker.sock')
            for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
                self.assertNotIn(key, env)

    def test_long_physical_root_rejected_without_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            builds = Path(directory) / ('x' * 75) / 'Builds'
            builds.mkdir(parents=True)
            state = builds / mac.VM_STATE_FOLDER
            with self.assertRaisesRegex(RuntimeError, 'Lima socket would be'):
                mac.managed_state_root(builds)
            self.assertFalse(state.exists())
    def test_symlinked_vm_root_is_rejected_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            builds = Path(directory) / 'Builds'
            builds.mkdir()
            outside = Path(directory) / 'Other'
            outside.mkdir()
            (builds / "CFTVM").mkdir()
            (builds / mac.VM_STATE_FOLDER).symlink_to(outside, target_is_directory=True)
            with mock.patch.object(mac, 'SOCKET_PATH_LIMIT', 255):
                with self.assertRaisesRegex(RuntimeError, 'real directory'):
                    mac.managed_state_root(builds)
            self.assertTrue(outside.is_dir())
    def test_unhealthy_running_colima_is_stopped_before_restart(self):
        """Only canonical c backend owns recovery, never a second w driver."""
        from linuxos import managed_backend
        with tempfile.TemporaryDirectory() as directory:
            builds = Path(directory) / 'Builds'
            builds.mkdir()
            tools = builds / 'CinterForgeTerminal/Toolchains/mac'
            for rel in ('env/bin/docker', 'env/bin/colima', 'env/bin/docker-buildx',
                        'lima/bin/limactl'):
                binary = tools / rel
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b'fixture')
            buildx = tools / 'env/bin/docker-buildx'
            with mock.patch.object(mac.platform, 'system', return_value='Darwin'), \
                 mock.patch.object(mac, 'SOCKET_PATH_LIMIT', 512), \
                 mock.patch.object(managed_backend, 'backend_environment',
                                   return_value=_fake_c_environment(builds)), \
                 mock.patch.object(mac, '_stage_lima'), \
                 mock.patch.object(mac, '_ensure_buildx_plugin', return_value=buildx), \
                 mock.patch.object(managed_backend, 'ensure_backend',
                                   side_effect=RuntimeError('c readiness failed')) as backend:
                with self.assertRaisesRegex(RuntimeError, 'c readiness failed'):
                    mac.ensure_docker(builds)
            backend.assert_called_once()
            self.assertFalse((builds / 'CFTVM/colima/w').exists())

    def test_running_vm_missing_mounts_is_reconfigured_without_deleting_images(self):
        """The c backend is the sole owner of mount checks and re-use."""
        from linuxos import managed_backend
        with tempfile.TemporaryDirectory() as directory:
            builds = Path(directory) / 'Builds'
            builds.mkdir()
            tools = builds / 'CinterForgeTerminal/Toolchains/mac'
            for rel in ('env/bin/docker', 'env/bin/colima', 'env/bin/docker-buildx',
                        'lima/bin/limactl'):
                binary = tools / rel
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b'fixture')
            buildx = tools / 'env/bin/docker-buildx'
            env = {'DOCKER_HOST': 'unix:///owned/c/docker.sock'}
            with mock.patch.object(mac.platform, 'system', return_value='Darwin'), \
                 mock.patch.object(mac, 'SOCKET_PATH_LIMIT', 512), \
                 mock.patch.object(managed_backend, 'backend_environment',
                                   return_value=_fake_c_environment(builds)), \
                 mock.patch.object(mac, '_stage_lima'), \
                 mock.patch.object(mac, '_ensure_buildx_plugin', return_value=buildx), \
                 mock.patch.object(managed_backend, 'ensure_backend',
                                   return_value=(str(tools / 'env/bin/docker'), env)) as backend, \
                 mock.patch.object(mac, '_probe', return_value=subprocess.CompletedProcess([], 0, '29.5.2')):
                mac.ensure_docker(builds)
                mac.ensure_docker(builds)
            self.assertEqual(backend.call_count, 2)
            receipt = builds / 'CFTVM/U/backend.json'
            self.assertTrue(receipt.is_file())
            self.assertEqual(json.loads(receipt.read_text())['profile'], 'c')
            self.assertFalse((builds / 'CFTVM/colima/w').exists())

# Migrated from tests/test_linuxkit_shared_docker.py.
"""LinuxKit Setup/Build/Stage/Test separation (mocked; no VMs/network)."""

import inspect
import json
import pathlib
import runpy
import subprocess
import zipfile
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'linuxos'))
import lifecycle
from tests import acceptance_linuxkit as test_linuxkit
from host import mac_bootstrap
from linuxos import managed_backend
import bedrock_consent


class LinuxKitPhaseOwnershipTests(unittest.TestCase):
    def test_guest_uses_separate_managed_c_backend(self):
        env = {'CFOS_COLIMA_PROFILE': 'c', 'DOCKER_HOST': 'unix:///Builds/CFTVM/U/c/c/docker.sock'}
        with mock.patch.object(lifecycle.platform, 'system', return_value='Darwin'), \
             mock.patch.object(lifecycle.platform, 'machine', return_value='arm64'), \
             mock.patch.object(managed_backend, 'ensure_backend', return_value=('/managed/docker', env)) as ensure:
            docker, actual = lifecycle.managed_setup_docker()
        self.assertEqual(docker, '/managed/docker')
        self.assertIs(actual, env)
        ensure.assert_called_once_with(managed_backend.source_directory(lifecycle.REPO))

    def test_c_backend_does_not_fall_back_to_w_backend(self):
        with mock.patch.object(lifecycle.platform, 'system', return_value='Linux'), \
             mock.patch.object(mac_bootstrap, 'ensure_docker') as ensure:
            with self.assertRaisesRegex(RuntimeError, 'Apple Silicon macOS'):
                lifecycle.managed_setup_docker()
        ensure.assert_not_called()

    def test_script_import_resolves_linuxos_managed_backend(self):
        # Actual shell command invokes linuxos/lifecycle.py by filename, not -m.
        # This caught the user's ModuleNotFoundError after launcher signing.
        namespace = runpy.run_path(str(ROOT / 'linuxos/lifecycle.py'), run_name='regression')
        with mock.patch.object(namespace['platform'], 'system', return_value='Darwin'), \
             mock.patch.object(namespace['platform'], 'machine', return_value='arm64'), \
             mock.patch.object(managed_backend, 'ensure_backend', return_value=('/managed/docker', {})) as ensure:
            self.assertEqual(namespace['managed_setup_docker']()[0], '/managed/docker')
        ensure.assert_called_once()

    def test_setup_builds_guest_stage_only_verifies(self):
        body = inspect.getsource(lifecycle.prepare_guest)
        self.assertIn('docker, env = managed_setup_docker()', body)
        self.assertIn("provision_guest_runtime(output / 'GuestRuntime', docker, env)", body)
        self.assertEqual(inspect.getsource(lifecycle.stage_guest).count('return verify_guest()'), 1)
        self.assertNotIn('managed_setup_docker', inspect.getsource(lifecycle.stage_guest))
        with mock.patch.object(lifecycle, 'verify_guest', return_value=pathlib.Path('/prepared')) as verify:
            self.assertEqual(lifecycle.stage_guest(), pathlib.Path('/prepared'))
        verify.assert_called_once_with()

    def test_lifecycle_uses_migrated_acceptance_entrypoint(self):
        # Only tests/ owns acceptance code. The Setup/Test dispatch must not
        # reference a deleted linuxos/test_linuxkit.py file after migration.
        with mock.patch.object(lifecycle, '_licensed') as licensed, \
             mock.patch.object(lifecycle, '_call') as dispatch:
            lifecycle.test(offline=True)
        licensed.assert_called_once()
        command = dispatch.call_args.args
        self.assertEqual(pathlib.Path(command[2]).resolve(),
                         (ROOT / 'tests/acceptance_linuxkit.py').resolve())
        self.assertIn('--offline', command)

    def test_official_bedrock_setup_imports_packaged_craftmaster(self):
        # Reproduces the real Setup path: test_linuxkit imports
        # craftmaster.build_craftmaster_server, NOT a standalone script.
        # An already-verified miniature fixture bypasses external downloads.
        scratch = ROOT.parent / 'Builds/Intermediates/CinterForgeTerminalTests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='craftmaster-import-', dir=scratch) as temporary:
            repo = pathlib.Path(temporary) / 'CinterForgeTerminal'
            repo.mkdir()
            cache = (repo.parent / 'Builds/Intermediates/CinterForgeTerminal/'
                     'LinuxKit/Downloads')
            cache.mkdir(parents=True)
            archive = cache / 'bedrock-server-linux.zip'
            with zipfile.ZipFile(archive, 'w') as package:
                package.writestr('bedrock_server', b'fixture: not for execution')
            self.assertEqual(test_linuxkit.prepare_official_bedrock_archive(repo), archive)

    def test_craftmaster_package_imports_resolve_owned_helpers(self):
        from craftmaster import build_craftmaster_server, validate_minecraft_server
        helper = pathlib.Path(build_craftmaster_server.cinterforgeBuildsRootPath.__code__.co_filename)
        self.assertEqual(helper.resolve(), (ROOT / 'linuxos/craftmaster/workspace_paths.py').resolve())
        self.assertEqual(validate_minecraft_server.cinterforgeBuildsRootPath,
                         build_craftmaster_server.cinterforgeBuildsRootPath)

    def test_craftmaster_direct_entrypoints_remain_executable(self):
        # The acceptance CLI still invokes these scripts by their filenames.
        for filename in ('build_craftmaster_server.py', 'validate_minecraft_server.py'):
            with self.subTest(filename=filename):
                result = subprocess.run(
                    [sys.executable, '-B', str(ROOT / 'linuxos/craftmaster' / filename), '--help'],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--repo-root', result.stdout)

    def test_setup_runs_kernel_guest_official_archive_and_consent(self):
        with mock.patch.object(lifecycle, 'setup_kernel') as kernel, \
             mock.patch.object(lifecycle, 'prepare_guest') as guest, \
             mock.patch.object(lifecycle, 'verify_guest') as verify, \
             mock.patch.object(lifecycle, '_licensed') as licensed, \
             mock.patch.object(test_linuxkit, 'prepare_official_bedrock_archive') as archive:
            lifecycle.setup()
        for fn in (kernel, guest, verify, licensed, archive):
            fn.assert_called_once()

    def test_clean_mac_setup_starts_c_before_kernel_and_reuses_it_for_guest(self):
        phases = []
        env = {'DOCKER_HOST': 'unix:///owned/c/docker.sock', 'PATH': '/managed/bin'}
        with mock.patch.object(lifecycle.platform, 'system', return_value='Darwin'), \
             mock.patch.object(lifecycle, 'managed_setup_docker',
                               side_effect=lambda: (phases.append('c-ready') or ('/managed/docker', env))), \
             mock.patch.object(lifecycle, 'setup_kernel', side_effect=lambda: phases.append('kernel')), \
             mock.patch.object(lifecycle, 'prepare_guest', side_effect=lambda: phases.append('guest-c')), \
             mock.patch.object(lifecycle, 'verify_guest', side_effect=lambda: phases.append('verify')), \
             mock.patch.object(lifecycle, '_licensed', side_effect=lambda: phases.append('consent')), \
             mock.patch.object(test_linuxkit, 'prepare_official_bedrock_archive',
                               side_effect=lambda repo: phases.append('bedrock-archive')), \
             mock.patch.dict(os.environ, {}, clear=False):
            lifecycle.setup()
        self.assertEqual(phases, ['c-ready', 'kernel', 'guest-c', 'verify', 'consent', 'bedrock-archive'])

    def test_acceptance_uses_setup_guest_and_does_not_start_builder(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = pathlib.Path(temp) / 'CinterForgeTerminal'
            repo.mkdir()
            (repo / 'Cinter').touch()
            (repo / 'linuxos/craftmaster').mkdir(parents=True)
            (repo / 'linuxos/craftmaster/build_craftmaster_server.py').touch()
            (repo / 'linuxos/craftmaster/validate_minecraft_server.py').touch()
            builds = pathlib.Path(temp) / 'Builds'
            builds.mkdir()
            prepared = pathlib.Path(temp) / 'prepared'
            prepared.mkdir()
            for name in ('cinter-bds-kernel', 'cinter-bds-initrd.img',
                         'cinter-bds-cmdline', 'CinterBDSVM'):
                (prepared / name).write_bytes(name.encode())
            with mock.patch.object(test_linuxkit.platform, 'system', return_value='Darwin'), \
                 mock.patch.object(test_linuxkit.platform, 'machine', return_value='arm64'), \
                 mock.patch.object(test_linuxkit, 'verify_shared_bootstrap'), \
                 mock.patch.object(bedrock_consent, 'require_bedrock_eula'), \
                 mock.patch.object(test_linuxkit, 'HERE', repo / 'linuxos'), \
                 mock.patch.object(test_linuxkit, 'verify_guest', return_value=prepared) as verify, \
                 mock.patch.object(lifecycle, 'managed_setup_docker') as backend, \
                 mock.patch.object(test_linuxkit, '_execute_acceptance') as acceptance:
                test_linuxkit.test_linuxkit(repo)
            verify.assert_called_once()
            backend.assert_not_called()
            self.assertEqual(acceptance.call_args.args[-1], prepared)
            receipt = builds / 'Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json'
            self.assertEqual(json.loads(receipt.read_text())['status'], 'passed')


    def test_bedrock_acceptance_cleanup_retries_macos_enotempty(self):
        # A generated Bedrock world may gain filesystem-observer entries while
        # Python recursively removes it on macOS (Errno 66).
        with tempfile.TemporaryDirectory() as temp:
            workspace = pathlib.Path(temp) / 'Acceptance-race'
            data = workspace / 'Server/worlds/Bedrock level/db'
            data.mkdir(parents=True)
            (data / 'CURRENT').write_text('generated Bedrock world')
            real_rmtree = shutil.rmtree
            calls = []

            def rmtree_with_observer_race(path):
                calls.append(pathlib.Path(path))
                if len(calls) == 1:
                    real_rmtree(path)
                    (workspace / 'Server').mkdir(parents=True)
                    (workspace / 'Server/.DS_Store').write_text('observer entry')
                    raise OSError(errno.ENOTEMPTY, 'Directory not empty', str(workspace / 'Server'))
                return real_rmtree(path)

            with mock.patch.object(test_linuxkit.shutil, 'rmtree', side_effect=rmtree_with_observer_race), \
                 mock.patch.object(test_linuxkit.time, 'sleep') as sleeper:
                test_linuxkit._remove_acceptance_workspace(workspace)
            self.assertEqual(len(calls), 2)
            sleeper.assert_called_once()
            self.assertFalse(workspace.exists())

    def test_bedrock_acceptance_cleanup_fails_if_generated_workspace_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = pathlib.Path(temp) / 'Acceptance-stuck'
            (workspace / 'Server').mkdir(parents=True)
            with mock.patch.object(test_linuxkit.shutil, 'rmtree',
                                   side_effect=OSError(errno.ENOTEMPTY, 'Directory not empty')), \
                 mock.patch.object(test_linuxkit.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed after 6 attempts'):
                    test_linuxkit._remove_acceptance_workspace(workspace)
            self.assertTrue(workspace.is_dir())

    def test_bedrock_acceptance_cleanup_runs_when_validator_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            invoked = []

            def run(command):
                invoked.append(command)
                if '--linuxkit-test-install' in command:
                    server = pathlib.Path(command[command.index('--linuxkit-test-install') + 1])
                    (server / 'worlds/Bedrock level/db').mkdir(parents=True)
                    (server / 'worlds/Bedrock level/db/CURRENT').write_text('generated')
                else:
                    raise RuntimeError('validator rejection')

            with mock.patch.object(test_linuxkit, '_run', side_effect=run), \
                 mock.patch.object(test_linuxkit, '_install_prepared_runtime', return_value={}):
                with self.assertRaisesRegex(RuntimeError, 'validator rejection'):
                    test_linuxkit._execute_acceptance(ROOT, ROOT, parent / 'install.py',
                                                       parent / 'validate.py', parent, {}, False, parent)
            self.assertEqual(len(invoked), 2)
            self.assertEqual(list(parent.glob('Acceptance-*')), [])

    def test_bedrock_acceptance_real_subprocess_fixture_and_disposal(self):
        # Exercise real installer/validator child processes and a generated
        # multi-directory Bedrock world, not just a mocked command dispatch.
        with tempfile.TemporaryDirectory() as temp:
            scratch = pathlib.Path(temp)
            installer = scratch / 'install.py'
            validator = scratch / 'validate.py'
            installer.write_text(
                'import pathlib, sys\n'
                'server = pathlib.Path(sys.argv[sys.argv.index("--linuxkit-test-install") + 1])\n'
                'for i in range(24):\n'
                '    db = server / "worlds" / "Bedrock level" / "db" / str(i)\n'
                '    db.mkdir(parents=True)\n'
                '    for j in range(8): (db / f"{j}.ldb").write_bytes(b"generated")\n'
                'print("fixture server installed", flush=True)\n'
            )
            validator.write_text(
                'import pathlib, sys\n'
                'server = pathlib.Path(sys.argv[sys.argv.index("--cfos-linuxkit-test-server") + 1])\n'
                'assert len(list(server.glob("worlds/Bedrock level/db/*/*.ldb"))) == 192\n'
                'print("fixture server smoke passed", flush=True)\n'
            )
            with mock.patch.object(test_linuxkit, '_install_prepared_runtime', return_value={}):
                test_linuxkit._execute_acceptance(ROOT, ROOT, installer, validator,
                                                   scratch, {}, True, scratch)
            self.assertEqual(list(scratch.glob('Acceptance-*')), [])

    def test_bedrock_acceptance_never_downloads_missing_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with mock.patch.object(test_linuxkit, '_run') as runner, \
                 mock.patch.object(test_linuxkit, '_install_prepared_runtime', return_value={}):
                test_linuxkit._execute_acceptance(ROOT, ROOT, root / 'install.py',
                                                   root / 'validate.py', root, {}, False, root)
            self.assertIn('--offline', runner.call_args_list[0].args[0])
        with mock.patch.object(lifecycle, '_licensed'), \
             mock.patch.object(lifecycle, '_call') as shell:
            lifecycle.test()
        self.assertIn('--offline', shell.call_args.args)





# Migrated from tests/test_linuxkit_kernel_provision.py.
"""No-network checks for restoring the original custom LinuxKit kernel producer.

These tests verify command wiring and reuse, not a real LinuxKit build or boot.
"""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'linuxos'))
spec = importlib.util.spec_from_file_location('terminal_linuxkit_kernel_under_test', ROOT / 'linuxos/build_linux.py')
assert spec and spec.loader
kernel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kernel)


class KernelSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.destination = self.workspace / 'Builds/CreativeSuite/CFOS/LinuxKit'
        self.destination.mkdir(parents=True)
        self.checkout = self.workspace / 'Builds/CinterForgeTerminal/Toolchains/mac/linuxkit/kernel-source'
        (self.checkout / 'kernel/6.6.x').mkdir(parents=True)
        (self.checkout / 'kernel/6.6.x/build-args').write_text('KERNEL_VERSION=6.6.71\n')
        (self.checkout / 'kernel/build-kernel.yml').write_text('image: linuxkit/kernel\n')
        self.tag = 'linuxkit/kernel:6.6.71-cinter-virtiofs-hash-dirty'
        self.commands = []
        self.has_image = False

    def fake_run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[:3] == ['linuxkit', 'pkg', 'show-tag']:
            return subprocess.CompletedProcess(command, 0, stdout=self.tag + '\n')
        if command[:3] == ['/managed/docker', 'image', 'inspect']:
            return subprocess.CompletedProcess(command, 0 if self.has_image else 1, stdout='')
        if command[:3] == ['/managed/docker', 'context', 'show']:
            return subprocess.CompletedProcess(command, 0, stdout='managed-context\n')
        if command[:3] == ['linuxkit', 'pkg', 'build']:
            self.assertEqual(kwargs['env']['LINUXKIT_BUILDERS'], 'linux/arm64=managed-context')
            self.assertIn('--build-arg-file', command)
            self.assertIn('--docker', command)
            self.assertTrue(kwargs['env']['LINUXKIT_CACHE'].startswith(str(self.workspace / 'Builds')))
            self.assertEqual(command[command.index('--tag') + 1], kernel.KERNEL_IMAGE_PATTERN)
            self.has_image = True
            return subprocess.CompletedProcess(command, 0)
        self.fail(f'Unexpected command {command!r}')

    def setup_images(self):
        with mock.patch.object(kernel, '_clone_kernel_source', return_value=self.checkout), \
             mock.patch.object(kernel, 'cfos_builds_root', return_value=self.workspace / 'Builds'), \
             mock.patch.object(kernel.shutil, 'which', return_value='/managed/docker'), \
             mock.patch.object(kernel.subprocess, 'run', side_effect=self.fake_run):
            return kernel.ensure_custom_kernel_image(self.workspace, self.destination)

    def test_fresh_docker_creates_original_kernel_fragment_and_image(self):
        self.assertEqual(self.setup_images(), self.tag)
        self.assertEqual((self.checkout / 'kernel/6.6.x--cinter-virtiofs/config-aarch64').read_text(),
                         'CONFIG_VIRTIO_FS=y\n')
        self.assertEqual((self.destination / 'cinter-kernel-build-args').read_text(),
                         'KERNEL_VERSION=6.6.71\nEXTRA=-cinter-virtiofs\n')
        self.assertEqual((self.destination / 'custom-kernel-image.txt').read_text(), self.tag + '\n')
        self.assertEqual(len([c for c, _ in self.commands if c[:3] == ['linuxkit', 'pkg', 'build']]), 1)

    def test_current_image_reused_and_derived_yaml_kept(self):
        self.setup_images()
        self.commands.clear()
        self.assertEqual(self.setup_images(), self.tag)
        self.assertFalse(any(c[:3] == ['linuxkit', 'pkg', 'build'] for c, _ in self.commands))
        authored = kernel.HERE / 'linuxkit.yml'
        self.assertTrue(authored.is_file())
        rendered = kernel._render_kernel_image(authored.read_text(), self.tag)
        (self.destination / 'linuxkit.yml').write_text(rendered)
        with mock.patch.object(kernel, 'linuxkit_bootstrap_root', return_value=self.destination):
            kernel.stage_sources(self.workspace)
        self.assertEqual((self.destination / 'linuxkit.yml').read_text(), rendered)
        self.assertEqual(authored.read_text().splitlines()[2].strip().split()[1],
                         'linuxkit/kernel:6.6.71-cinter-virtiofs-819af9d59279506dd2994e7aea1cbbaaebfdb0a2-dirty-2d674b9')

    def test_dirty_producer_inputs_rebuild_image_even_when_cached(self):
        self.setup_images()
        self.commands.clear()
        (self.destination / 'custom-kernel-inputs.sha256').write_text('different-inputs\n')
        self.setup_images()
        self.assertEqual(len([c for c, _ in self.commands
                              if c[:3] == ['linuxkit', 'pkg', 'build']]), 1)

    def test_existing_built_artifacts_do_not_call_kernel_producer(self):
        for name in ('cinter-bds-kernel', 'cinter-bds-initrd.img', 'cinter-bds-cmdline'):
            (self.destination / name).write_bytes(b'valid-output')
        (self.destination / 'built-source-fingerprint.txt').write_text('known\n')
        with mock.patch.object(kernel, 'stage_sources', return_value=self.destination), \
             mock.patch.object(kernel, 'shared_source_fingerprint', return_value='known\n'), \
             mock.patch.object(kernel, 'ensure_arm64_boot_kernel'), \
             mock.patch.object(kernel, 'verify_shared_bootstrap'), \
             mock.patch.object(kernel, 'ensure_custom_kernel_image') as producer, \
             mock.patch.object(kernel.platform, 'system', return_value='Linux'), \
             mock.patch.object(sys, 'argv', ['build_linux.py', '--repo-root', str(self.workspace)]):
            self.assertEqual(kernel.main(), 0)
        producer.assert_not_called()

    def test_absent_kernel_triggers_producer_before_linuxkit_build(self):
        (self.destination / 'linuxkit.yml').write_text((kernel.HERE / 'linuxkit.yml').read_text())
        with mock.patch.object(kernel, 'stage_sources', return_value=self.destination), \
             mock.patch.object(kernel, 'shared_source_fingerprint', return_value='known\n'), \
             mock.patch.object(kernel, 'ensure_arm64_boot_kernel'), \
             mock.patch.object(kernel, 'verify_shared_bootstrap'), \
             mock.patch.object(kernel.shutil, 'which', return_value='/managed/linuxkit'), \
             mock.patch.object(kernel, 'ensure_custom_kernel_image', return_value=self.tag) as producer, \
             mock.patch.object(kernel, '_linuxkit_build_environment', return_value={'LINUXKIT_CACHE': '/managed/cache'}), \
             mock.patch.object(kernel.platform, 'system', return_value='Linux'), \
             mock.patch.object(sys, 'argv', ['build_linux.py', '--repo-root', str(self.workspace)]):
            def run(cmd, **kwargs):
                producer.assert_called_once()
                self.assertEqual(cmd[:2], ['linuxkit', 'build'])
                for name in ('cinter-bds-kernel', 'cinter-bds-initrd.img', 'cinter-bds-cmdline'):
                    (self.destination / name).write_bytes(b'mock')
                return subprocess.CompletedProcess(cmd, 0)
            with mock.patch.object(kernel.subprocess, 'run', side_effect=run):
                self.assertEqual(kernel.main(), 0)
        self.assertEqual((self.destination / 'linuxkit.yml').read_text().splitlines()[2],
                         '  image: ' + self.tag)




# Migrated from tests/test_managed_process_lifetime.py.
"""Focused lifecycle ownership regression checks; no macOS VM is started."""
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from host import mac_bootstrap


class ProcessLifetimeTests(unittest.TestCase):
    def test_cleanup_stops_only_named_c_producer(self):
        with mock.patch.object(mac_bootstrap, 'stop_linuxkit_build_backend') as stop:
            mac_bootstrap.stop_setup_build_vms(pathlib.Path('/tmp/Builds'))
        stop.assert_called_once_with(pathlib.Path('/tmp/Builds'))

    def test_docker_failure_skips_graceful_stop(self):
        with mock.patch.object(mac_bootstrap, '_run') as execute:
            mac_bootstrap._stop_colima_profile(pathlib.Path('/bin/colima'),
                                                mac_bootstrap.COLIMA_PROFILE, {}, unhealthy=True)
        execute.assert_called_once_with(['/bin/colima', 'stop', mac_bootstrap.COLIMA_PROFILE, '--force'],
                                        env={}, timeout=45)

    def test_graceful_stop_timeout_forces_exact_profile(self):
        with mock.patch.object(mac_bootstrap, '_run', side_effect=[
                subprocess.TimeoutExpired('colima stop', 8), None]) as execute:
            mac_bootstrap._stop_colima_profile(pathlib.Path('/bin/colima'), 'c', {})
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(execute.call_args.args[0], ['/bin/colima', 'stop', 'c', '--force'])
        self.assertEqual(execute.call_args.kwargs['timeout'], 45)

    def test_crash_watchdog_triggers_cleanup(self):
        with mock.patch.object(mac_bootstrap.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO()})()), \
             mock.patch.object(mac_bootstrap, 'stop_setup_build_vms') as stop:
            self.assertEqual(mac_bootstrap.cleanup_watchdog(pathlib.Path('/tmp/Builds')), 0)
            stop.assert_called_once()

    def test_clean_watchdog_does_not_stop_twice(self):
        with mock.patch.object(mac_bootstrap.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(b'finished\n')})()), \
             mock.patch.object(mac_bootstrap, 'stop_setup_build_vms') as stop:
            self.assertEqual(mac_bootstrap.cleanup_watchdog(pathlib.Path('/tmp/Builds')), 0)
            stop.assert_not_called()

    def test_linuxkit_builds_nested_module_at_pinned_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            builds = pathlib.Path(temp) / 'Builds'
            root = builds / 'CinterForgeTerminal/Toolchains/mac'
            (root / 'env/bin').mkdir(parents=True)
            (root / 'env/bin/go').touch()
            def execute(command, **kw):
                if command[:2] == ['git', 'clone']:
                    (root / 'linuxkit/source/src/cmd/linuxkit').mkdir(parents=True)
                    (root / 'linuxkit/source/src/cmd/linuxkit/go.mod').touch()
                elif len(command) > 2 and command[1] == 'build':
                    pathlib.Path(command[3]).touch()
            with mock.patch.object(mac_bootstrap, 'managed_environment', return_value={}), \
                 mock.patch.object(mac_bootstrap, '_run', side_effect=execute) as runner, \
                 mock.patch.object(mac_bootstrap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='v1.3.0\n')):
                exe = mac_bootstrap.ensure_linuxkit_cli(builds)
            self.assertTrue(exe.is_file())
            builds_args = [call for call in runner.call_args_list if len(call.args[0]) > 1 and call.args[0][1] == 'build']
            self.assertEqual(len(builds_args), 1)
            self.assertEqual(builds_args[0].kwargs['cwd'], str(root / 'linuxkit/source/src/cmd/linuxkit'))
            self.assertNotIn('go install', str(runner.call_args_list))

    def test_outer_setup_cleans_up_when_body_raises(self):
        import runpy
        program = runpy.run_path(str(ROOT / 'Cinter'), run_name='cinter_test')
        scope = program['_temporary_build_services']
        class Watcher:
            def __init__(self):
                self.stdin = io.BytesIO()
            def wait(self, timeout):
                return 0
        watcher = Watcher()
        with mock.patch.object(program['platform'], 'system', return_value='Darwin'), \
             mock.patch.object(program['subprocess'], 'Popen', return_value=watcher), \
             mock.patch.object(mac_bootstrap, 'stop_setup_build_vms') as stop:
            with self.assertRaisesRegex(RuntimeError, 'broken setup'):
                with scope(['setup', 'all']):
                    raise RuntimeError('broken setup')
        stop.assert_called_once()
        self.assertTrue(watcher.stdin.closed)

    def test_nested_build_does_not_stop_outer_setup_vm(self):
        import runpy
        program = runpy.run_path(str(ROOT / 'Cinter'), run_name='cinter_test')
        with mock.patch.dict(program['os'].environ, {'CINTER_LIFECYCLE_PARENT':'1'}), \
             mock.patch.object(mac_bootstrap, 'stop_setup_build_vms') as stop:
            with program['_temporary_build_services'](['build', 'win']):
                pass
        stop.assert_not_called()

    def test_linuxkit_cleanup_targets_exact_separate_profile(self):
        from linuxos import managed_backend
        with tempfile.TemporaryDirectory() as temp:
            builds = pathlib.Path(temp) / 'Builds'
            root = builds / 'CinterForgeTerminal/Toolchains/mac'
            (root / 'env/bin').mkdir(parents=True)
            (root / 'env/bin/colima').touch()
            vmhome = builds / 'CFTVM/U/c'
            (vmhome / 'c').mkdir(parents=True)
            env = {'CFOS_COLIMA_PROFILE':'c', 'COLIMA_HOME':str(vmhome),
                   'LIMA_HOME':str(builds / 'CFTVM/U/l'), 'PATH':'/usr/bin'}
            with mock.patch.object(mac_bootstrap.platform, 'system', return_value='Darwin'), \
                 mock.patch.object(managed_backend, 'backend_environment', return_value=env), \
                 mock.patch.object(mac_bootstrap, '_run') as run:
                mac_bootstrap.stop_linuxkit_build_backend(builds)
            self.assertEqual(run.call_args.args[0],
                             [str(root / 'env/bin/colima'), 'stop', 'c'])

    def test_cleanup_propagates_failure_of_single_owned_producer(self):
        with mock.patch.object(mac_bootstrap, 'stop_linuxkit_build_backend',
                               side_effect=RuntimeError('c cleanup failed')) as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'c cleanup failed'):
                mac_bootstrap.stop_setup_build_vms(pathlib.Path('/tmp/Builds'))
            cleanup.assert_called_once()

    def test_bedrock_watchdog_checks_exact_vm_before_stopping(self):
        spec = importlib.util.spec_from_file_location('bedrock_watchdog_test', ROOT / 'webapp/CinterForgeTerminal.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manager = mock.Mock()
        manager._launcher_pid_matches.side_effect = [True, True]
        with mock.patch.object(module, 'BedrockManager', return_value=manager), \
             mock.patch.object(module.select, 'select', return_value=([sys.stdin], [], [])), \
             mock.patch.object(module.os, 'read', return_value=b''):
            result = module._vm_watchdog_main(['/tmp', 'mac', '0', '4321', '9876'])
        self.assertEqual(result, 0)
        manager.stop.assert_called_once()




class LinuxKitColimaBootReportingTests(unittest.TestCase):
    """The c guest must never silently wait through a full VM boot timeout."""

    def test_boot_streams_output_and_saves_builds_log(self):
        from linuxos.managed_backend import _run_colima_start_live
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'Builds/Logs/CinterForgeTerminal/linuxkit-colima-c-start.log'
            cmd = [sys.executable, '-u', '-c',
                   "import time; print('COLIMA_BOOT_START'); time.sleep(0.35); print('COLIMA_BOOT_READY')"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = _run_colima_start_live(cmd, os.environ.copy(), log,
                                                timeout=3, heartbeat=0.1)
            self.assertEqual(result.returncode, 0)
            self.assertIn('COLIMA_BOOT_START', output.getvalue())
            self.assertIn('COLIMA_BOOT_READY', output.getvalue())
            self.assertIn('Waiting for Colima c boot', output.getvalue())
            self.assertIn('COLIMA_BOOT_READY', log.read_text())
            self.assertIn('COLIMA_BOOT_START', result.stdout)

    def test_boot_timeout_stops_owned_start_process_and_preserves_log(self):
        from linuxos.managed_backend import _run_colima_start_live
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'Builds/Logs/CinterForgeTerminal/linuxkit-colima-c-start.log'
            cmd = [sys.executable, '-u', '-c',
                   "import time; print('COLIMA_BOOT_BLOCKED'); time.sleep(30)"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaises(subprocess.TimeoutExpired):
                    _run_colima_start_live(cmd, os.environ.copy(), log,
                                           timeout=4, heartbeat=0.25)
            self.assertIn('COLIMA_BOOT_BLOCKED', log.read_text())
            self.assertIn('Waiting for Colima c boot', output.getvalue())
