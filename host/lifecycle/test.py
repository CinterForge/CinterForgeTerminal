#!/usr/bin/env python3
# CINTERFORGE SOURCE HEADER — host/lifecycle/test.py
# PURPOSE: Owns the test stage of the existing CinterForgeTerminal lifecycle; generated files remain in sibling Builds.
# OWNERSHIP: This source belongs only to lifecycle; guest acceptance uses lifecycle-owned VM and PTYs.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   _run: Run run for this module.
#   _sha256: Handle sha256 through this component's established interfaces.
#   _install_prepared_runtime: Populate a disposable Bedrock test fixture using Setup's verified closure.
#   test_linuxkit: Handle test linuxkit through this component's established interfaces.
#   _remove_acceptance_workspace: Remove only this test's generated workspace after the VM has exited.
#   _execute_acceptance: Execute execute acceptance for this module.
#   run_managed_guest_acceptance: Run run managed guest acceptance for this module.
#   _run_with_guest_test_environment: Run run with guest test environment for this module.
#   RepositoryContractSmoke: class responsible for repositorycontractsmoke
#   RepositoryContractSmoke.test_01_single_root_smoke_file: All repository smoke tests are physically here, not merely excluded.
#   ...and 14 additional internal entry points; see their local documentation.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
"""Single real-system smoke/acceptance suite for CinterForge Terminal.

The Test phase consumes completed Setup, Build and Stage artifacts. Bedrock
acceptance uses an isolated disposable server, never a player's world.
"""
from __future__ import annotations

# The only smoke-test source in this repository. See the policy below.

# =============================================================================
# =============================================================================
# ALL CINTERFORGE TERMINAL RELEASE/SMOKE TESTS GO IN THIS ONE FILE: host/lifecycle/test.py
#
# DO NOT create tests/, test_suite_*.py, JavaScript test harnesses, or any new
# test module. Do NOT run tests inside Setup, Build, Stage, or Publish. Setup
# provisions; Build compiles; Stage installs; Test is the ONLY phase that
# executes acceptance. Test assumes Setup, Build, Stage already completed.
#
# THIS IS A SMOKE TEST SUITE, NOT A COLLECTION OF MICRO-UNIT TESTS.
# Target 50 or FEWER meaningful tests; if the count ever reaches 75,
# CONSOLIDATE the suite back BELOW 50. In fact enforce <=50 at discovery.
# A smoke test can be dozens or hundreds of lines if it proves one whole
# end-to-end behavior. Better, fewer REAL tests are preferred. Do not add
# mocks, invented Docker replies, fake signing, fake installers, or simulated
# server startup in this release gate. Test actual staged artifacts, real
# platform binaries, real VM/Bash/VNC and real Bedrock, with hard failures.
#
# Any function required by Setup/Build/Stage belongs in production code for
# that lifecycle phase, NEVER in test.py. Keep source tests here, and run
# them only when the explicit Test phase begins. Preserve durable logs and
# fail when real dependencies are unavailable; do not silently skip work.
# =============================================================================
# =============================================================================

import errno
import hashlib
import os
import json
import platform
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[2]
LINUXOS = REPO / 'host/linux'
# Run from the repository root; imports must not depend on a caller's cwd.
for directory in (str(REPO), str(LINUXOS)):
    if directory not in sys.path:
        sys.path.insert(0, directory)
from host.linux.build_linux import verify_shared_bootstrap
from host.linux.lifecycle import verify_guest

HERE = LINUXOS
from host.linux.guest_inputs import _require_elf


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print('[CFOS LinuxKit Test] ' + ' '.join(map(str, command)), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _install_prepared_runtime(server: Path, guest: Path) -> dict:
    """Populate a disposable Bedrock test fixture using Setup's verified closure.

    No Docker, network, glibc extraction or C compilation occurs here.
    """
    _require_elf(server / 'bedrock_server', 62)
    cinter = server / '.cinter'
    if cinter.exists():
        raise RuntimeError('Isolated acceptance server unexpectedly contains a runtime')
    cinter.mkdir()
    loader_source = guest / 'GuestRuntime/glibc-x86_64/ld-linux-x86-64.so.2'
    probe_source = guest / 'GuestRuntime/cinter-nethernet-probe'
    _require_elf(loader_source, 62)
    _require_elf(probe_source, 183)
    shutil.copytree(loader_source.parent, cinter / 'glibc-x86_64', symlinks=False)
    shutil.copy2(probe_source, cinter / 'cinter-nethernet-probe')
    _require_elf(cinter / 'glibc-x86_64/ld-linux-x86-64.so.2', 62)
    _require_elf(cinter / 'cinter-nethernet-probe', 183)
    manifest = {
        'bedrock_sha256': _sha256(server / 'bedrock_server'),
        'glibc_loader_sha256': _sha256(cinter / 'glibc-x86_64/ld-linux-x86-64.so.2'),
        'probe_sha256': _sha256(cinter / 'cinter-nethernet-probe'),
        'source': 'verified-setup-guest-runtime',
    }
    (cinter / 'acceptance-inputs.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return manifest


def test_linuxkit(repo: Path, *, offline: bool = False) -> None:
    repo = repo.resolve()
    if not (repo / 'Cinter').is_file():
        raise RuntimeError(f'Expected CinterForgeTerminal checkout: {repo}')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('CFOS LinuxKit Bedrock/Rosetta Test requires Apple Silicon macOS')
    # Direct developer test invocation uses the same explicit CLI consent gate.
    # Packaged applications collect consent in the WebView and share the receipt.
    from host.linux.bedrock_consent import require_bedrock_eula
    require_bedrock_eula(repo.parent / 'Builds', 'mac')
    server_repo = repo
    verify_shared_bootstrap(repo, require_launcher=False)
    root = repo.parent / 'Builds'
    if not root.is_dir():
        raise RuntimeError(f'Shared Builds directory missing: {root}')
    tmp_parent = root / 'Intermediates/CinterForgeTerminal/LinuxKit'
    tmp_parent.mkdir(parents=True, exist_ok=True)
    build = HERE
    installer = build / 'craftmaster/build_craftmaster_server.py'
    validator = build / 'craftmaster/validate_minecraft_server.py'
    if not installer.is_file() or not validator.is_file():
        raise RuntimeError(f'CinterForge Bedrock installer/validator missing under {build}')
    # Setup has already built and verified the dedicated `c` guest closure.
    # Acceptance uses that closure directly; Test must not start either VM
    # builder or acquire toolchains and images.
    # Keep a durable Test receipt outside the disposable test installation.
    receipt = root / 'Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    guest = verify_guest()
    evidence = {'schema': 'cinterforge.cfos.linuxkit-bedrock-test.v1',
                'bootstrap_sha256': {name: _sha256(guest / name) for name in
                   ('cinter-bds-kernel', 'cinter-bds-initrd.img', 'cinter-bds-cmdline', 'CinterBDSVM')},
                'status': 'failed', 'checked_at': datetime.now(timezone.utc).isoformat(),
                'test': 'real-official-bedrock-rosetta-nethernet-console-stop'}
    try:
        _execute_acceptance(repo, server_repo, installer, validator, tmp_parent, evidence, offline,
                            guest)
    except Exception as exc:
        evidence['error'] = str(exc)
        receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
        raise
    evidence['status'] = 'passed'
    receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    print(f'[CFOS LinuxKit Test] Evidence: {receipt}', flush=True)
    print('[CFOS LinuxKit Test] PASS: official Bedrock installed and executed via ARM64 LinuxKit/Rosetta; NetherNet, list, stop, exit verified', flush=True)


def _remove_acceptance_workspace(workspace: Path) -> None:
    """Remove only this test's generated workspace after the VM has exited.

    macOS filesystem observers can create entries while rmtree traverses the
    large Bedrock world directory, causing a transient ENOTEMPTY. Retry the
    *whole* disposal, never suppress an incomplete cleanup or alter user data.
    """
    for attempt in range(1, 7):
        if workspace.is_symlink():
            raise RuntimeError(f'Unsafe Bedrock acceptance cleanup symlink: {workspace}')
        if not workspace.exists():
            print('[CFOS LinuxKit Test] Generated Bedrock acceptance cleanup PASS', flush=True)
            return
        try:
            shutil.rmtree(workspace)
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY:
                raise
            if attempt == 6:
                remaining = ', '.join(p.name for p in list(workspace.iterdir())[:10]) if workspace.is_dir() else '(removed)'
                raise RuntimeError(
                    f'Bedrock acceptance passed but generated workspace cleanup failed '
                    f'after {attempt} attempts: {workspace}; remaining: {remaining}'
                ) from exc
            print(f'[CFOS LinuxKit Test] Cleanup retry {attempt}/5: '
                  f'generated Bedrock workspace changed during removal ({workspace})', flush=True)
            time.sleep(0.2 * attempt)
    if workspace.exists():
        raise RuntimeError(f'Bedrock acceptance cleanup left generated files: {workspace}')
    print('[CFOS LinuxKit Test] Generated Bedrock acceptance cleanup PASS', flush=True)


def _execute_acceptance(repo: Path, server_repo: Path, installer: Path, validator: Path,
                        tmp_parent: Path, evidence: dict, offline: bool,
                        guest: Path) -> None:
    workspace = Path(tempfile.mkdtemp(prefix='Acceptance-', dir=tmp_parent))
    try:
        server = workspace / 'Server'
        command = [sys.executable, '-u', str(installer), '--repo-root', str(server_repo),
                   '--linuxkit-test-install', str(server)]
        # Setup has already acquired and validated this official archive.
        # Acceptance must never repair missing inputs by downloading them.
        command.append('--offline')
        _run(command)
        evidence['runtime'] = _install_prepared_runtime(server, guest)
        _run([sys.executable, '-u', str(validator), '--repo-root', str(server_repo),
              '--cfos-linuxkit-test-server', str(server), '--cfos-root', str(repo)])
    finally:
        _remove_acceptance_workspace(workspace)




GUEST_SCHEMA = 'cinterforge.terminal.guest-test-acceptance.v1'
GUEST_CHECKS = ('linux_vm', 'linux_guest', 'linux_bash', 'linux_vnc',
          'windows_guest', 'windows_bash', 'windows_vnc', 'shutdown')


def run_managed_guest_acceptance(builds: Path) -> dict:
    # The Test stage runs actual VM/Bash/VNC acceptance after Stage; however
    # Lifecycle never imports the application HTTP server; leave environment
    # overrides only for the duration of actual guest acceptance.
    keys = ('CINTER_BUILDS_ROOT', 'CINTER_TERMINAL_RUNTIME_MODE')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        return _run_with_guest_test_environment(builds)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_with_guest_test_environment(builds: Path) -> dict:
    from host.lifecycle.CinterForgeLifecycleVM import CinterForgeLifecycleVM
    from host.lifecycle.CinterForgeLifecycleGuests import (
        CinterForgeLifecycleGuestShell, _image_archive, _docker_call,
        _start_guest_locked, _lifecycle_guest_shell_command,
        _verify_persistent_guest_shell)
    vm = CinterForgeLifecycleVM(builds)
    builds = builds.resolve(strict=True)
    if builds.name != 'Builds':
        raise RuntimeError('Guest runtime acceptance must use the existing sibling Builds')
    if platform.system() != 'Darwin':
        raise RuntimeError('Managed macOS Linux/Windows guest acceptance requires a Mac producer')
    sdk = builds / 'CinterForgeTerminal/Toolchains'
    # Setup has already provisioned these inputs. Test must not install them.
    required = ('mac/env/bin/colima', 'mac/env/bin/docker', 'mac/lima/bin/limactl',
                'Windows/MSYS2/usr/bin/bash.exe')
    missing = [part for part in required if not (sdk / part).is_file()]
    if missing:
        raise RuntimeError('Test missing Setup-provisioned guest runtime inputs: ' + ', '.join(missing))
    os.environ['CINTER_BUILDS_ROOT'] = str(builds)
    os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'developer'
    workspace = builds.parent
    env = vm.environment(sdk)
    # Use the same isolated Docker socket in the lifecycle's real
    # persistent Bash environment; no runtime service import is needed. The same selector must serve
    # Docker management and both long-lived PTYs; no mocked Docker context.
    shell_env = {key: env[key] for key in ('HOME', 'COLIMA_HOME', 'LIMA_HOME',
                                           'COLIMA_CACHE_HOME', 'DOCKER_CONFIG',
                                           'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                                           'DOCKER_HOST', 'TMPDIR', 'PATH')}
    if (shell_env.get('DOCKER_HOST') != env.get('DOCKER_HOST') or
            'DOCKER_CONTEXT' in shell_env):
        raise RuntimeError('Managed guest Bash environment must inherit the '
                           'actual t Docker socket, never a Docker context')
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    receipt = logs / 'guest-connections-test.json'
    transcript = logs / 'guest-connections-test.log'
    state = {'schema': GUEST_SCHEMA, 'status': 'failed', 'checks': {key: 'not-run' for key in GUEST_CHECKS},
             'timestamp': time.time(), 'sdk': str(sdk), 'logs': str(transcript)}
    shells: list[CinterForgeLifecycleGuestShell] = []
    owns_vm = False

    def passed(check: str) -> None:
        state['checks'][check] = 'passed'
        with transcript.open('a', encoding='utf-8') as fp:
            fp.write('[TEST GUEST] PASS ' + check + '\n')
        print('[TEST GUEST] PASS ' + check, flush=True)

    transcript.write_text('Test real guest / Bash / VNC acceptance\n', encoding='utf-8')
    try:
        with vm.managed_vm_lock(env):
            if vm.native_owner_active(env):
                raise RuntimeError('An active native Terminal owns this VM; '
                                   'Test must not adopt or stop its running connections')
            # No live app owns this profile; Test accepts cleanup
            # responsibility even if VM startup fails halfway.
            owns_vm = True
            # Lifecycle owns a separate copy of the existing behavior.
            docker = vm.ensure_docker_locked(sdk, env, workspace)
            passed('linux_vm')
            for kind, label in (('linux', 'linux'), ('win', 'windows')):
                _image_tar, image = _image_archive(sdk, kind)
                present = _docker_call(docker, env, 'image', 'inspect', image)
                if present.returncode:
                    raise RuntimeError('Test cannot load/provision a missing guest OCI image: ' +
                                       image + '; rerun Setup')
                _start_guest_locked(sdk, env, docker, kind, workspace)
                # Existing pinned OCI image exercises the real guest's Bash and
                # activated and checked its actual VNC and noVNC endpoints.
                passed(label + '_guest')
                passed(label + '_vnc')
                session = CinterForgeLifecycleGuestShell(
                    _lifecycle_guest_shell_command(sdk, kind, workspace),
                    env=shell_env, working_directory=workspace,
                    output_log=logs / ('guest-test-' + kind + '-bash.log'))
                shells.append(session)
                _verify_persistent_guest_shell(session, kind)
                passed(label + '_bash')
    except BaseException as error:
        state['error'] = str(error)
        with transcript.open('a', encoding='utf-8') as fp:
            fp.write('[TEST GUEST] FAIL ' + repr(error) + '\n')
        raise
    finally:
        for session in reversed(shells):
            try:
                session.close()
            except Exception as error:
                state['cleanup_error'] = str(error)
        # Even if VM startup fails half-way, this is a dedicated Test-owned
        # t profile. Stop only its verified labeled containers, then the VM.
        if owns_vm:
            try:
                vm.stop(sdk)
                status = subprocess.run([str(sdk / 'mac/env/bin/colima'), 'status', vm.profile],
                                        capture_output=True, text=True, env=env, timeout=25)
                if status.returncode == 0:
                    raise RuntimeError('Test-owned runtime VM remains running after cleanup')
                passed('shutdown')
            except Exception as error:
                state['cleanup_error'] = str(error)
                with transcript.open('a', encoding='utf-8') as fp:
                    fp.write('[TEST GUEST] FAIL shutdown: ' + str(error) + '\n')
        if state['checks']['shutdown'] == 'passed' and all(v == 'passed' for v in state['checks'].values()):
            state['status'] = 'passed'
        receipt.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    if state['status'] != 'passed':
        raise RuntimeError('Managed guest Test acceptance failed; see ' + str(receipt))
    return state





# =============================================================================
# RELEASE SMOKE CHECKS (READ THE ACTUAL BUILT/STAGED ARTIFACTS; NO MOCKING)
# =============================================================================

import unittest


class RepositoryContractSmoke(unittest.TestCase):
    """Release source checks with no monkeypatching or synthetic Docker state."""

    def test_01_single_root_smoke_file(self):
        """All repository smoke tests are physically here, not merely excluded."""
        self.assertTrue((REPO / 'host/lifecycle/test.py').is_file())
        self.assertFalse((REPO / 'tests').exists(), 'Legacy tests directory must be deleted')
        other = [str(x.relative_to(REPO)) for x in REPO.rglob('test_suite_*.py')]
        self.assertFalse(other, 'Obsolete regression modules remain: ' + repr(other[:5]))

    def test_02_actual_runtime_sources_parse(self):
        """Check every maintained production Python source without executing Setup."""
        import ast
        for root in (REPO / 'host', REPO / 'webapp'):
            for path in root.rglob('*.py'):
                if '__pycache__' in path.parts:
                    continue
                with self.subTest(source=str(path.relative_to(REPO))):
                    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        ast.parse((REPO / 'Cinter').read_text(encoding='utf-8'), filename='Cinter')

    def test_03_authored_json_manifests(self):
        """Parse every operational Terminal manifest from the delivered repo."""
        for path in (REPO / 'webapp').rglob('*.json'):
            with self.subTest(manifest=path.name):
                data = json.loads(path.read_text(encoding='utf-8'))
                self.assertIsNotNone(data)
        for name in ('webapp/data/projects.example.json', 'webapp/data/powershell.packages.example.json'):
            with self.subTest(manifest=name):
                self.assertIsNotNone(json.loads((REPO / name).read_text()))

    def test_04_game_catalogs_and_hidden_easter_egg(self):
        """Preserve authored game/IF/Classic Mac catalogs and hidden Lagomorph."""
        import re
        games = json.loads((REPO / 'webapp/data/mac_games.json').read_text())
        titles = json.loads((REPO / 'webapp/data/if_titles.json').read_text())
        self.assertEqual(len(games['games']), 67)
        self.assertEqual(len(titles['titles']), 16)
        self.assertEqual(len({item['command'] for item in games['games']}), 67)
        terminal = (REPO / 'webapp/runtime/CinterForgeCommands.py').read_text() + (REPO / 'webapp/runtime/CinterForgeIF.py').read_text()
        self.assertIn('lagomorph', terminal.lower())
        self.assertTrue((REPO / 'webapp/index.html').is_file())
        self.assertIn('codeoss', (REPO / 'webapp/data/terminal_styles.json').read_text().lower())

    def test_05_test_phase_is_not_invoked_by_setup(self):
        """Assert the actual CLI's Setup branch doesn't call Test by mistake."""
        source = (REPO / 'Cinter').read_text()
        setup_start = source.index("if argv in (['setup', 'all']")
        setup_end = source.index('    # Four unpacked Build outputs', setup_start)
        body = source[setup_start:setup_end]
        self.assertNotIn("'host/linux/lifecycle.py'), 'test'", body)
        self.assertNotIn("'host.guest_runtime_acceptance'", body)
        self.assertNotIn("'host/lifecycle/test.py'", body)
        self.assertNotIn("'test', selected", body)
        self.assertIn('managed_amd64_backend', body)

    def test_06_build_and_stage_do_not_call_smoke_suite(self):
        """The only root-suite launcher is the explicit Test dispatcher."""
        source = (REPO / 'Cinter').read_text()
        self.assertEqual(source.count('_run_automated_regression_tests()'), 2)
        self.assertIn("'stage', selected", source)
        self.assertIn("'test', selected", source)
        self.assertIn("'-B', '-m', 'host.lifecycle.test', '-q'", source)


class RealStagedSystemSmoke(unittest.TestCase):
    """High-value Mac release checks after completed Setup + Build + Stage."""

    @classmethod
    def setUpClass(cls):
        if platform.system() != 'Darwin':
            raise RuntimeError('Real staged Mac release smoke requires macOS (no mocks)')
        cls.builds = REPO.parent / 'Builds'
        if not cls.builds.is_dir() or cls.builds.is_symlink():
            raise RuntimeError('Real smoke requires sibling Builds from Setup')
        from host.lifecycle.universal_package import HOST_RELEASE
        from host.lifecycle.unpacked_release import _load
        cls.release = HOST_RELEASE
        cls.receipt = _load(cls.builds, cls.release)
        if 'mac' not in cls.receipt['targets']:
            raise RuntimeError('Mac smoke requires Mac Build receipt; run Build + Stage')

    def test_07_real_build_inputs_and_all_staged_artifacts(self):
        """Check source hashes, Build sizes and matching Steam Stage receipts."""
        from host.lifecycle.unpacked_release import status_four
        from host.lifecycle.universal_package import FOUR_TARGETS
        self.assertLessEqual(set(self.receipt['targets']), set(FOUR_TARGETS))
        status = status_four(self.builds)
        for target, report in self.receipt['targets'].items():
            with self.subTest(target=target):
                self.assertEqual(report['file_count'], len(report['files']))
                self.assertGreater(report['bytes'], 0)
                stage = status['targets'][target]
                self.assertTrue(stage['build_present'], f'{target} Build receipt missing')
                self.assertTrue(stage['steam_receipt_matches'],
                                f'{target} Stage receipt/files mismatch at {stage["steam_root"]}: '
                                f'{stage.get("steam_receipt_error", "no diagnostic")}')
                self.assertTrue(stage['steam_app_root_present'],
                                f'{target} staged app directory missing at '
                                f'{stage["steam_root"]}: '
                                f'{stage.get("steam_receipt_error", "no diagnostic")}')
                # Stage has already verified all Build bytes; sample concrete
                # published inputs instead of re-reading a 7 GiB SDK twice.
                names = sorted(report['files'])
                for name in {names[0], names[len(names)//2], names[-1]}:
                    item = report['files'][name]
                    source = Path(item['source'])
                    self.assertTrue(source.is_file() and not source.is_symlink())
                    self.assertTrue(source.resolve().is_relative_to(self.builds.resolve()))
                    self.assertEqual(_sha256(source), item['sha256'])

    def test_08_real_signed_mac_app_and_codeoss_lagomorph(self):
        """Verify the exact signed staged native app and real bundled assets."""
        from host.mac.mac_app import test as inspect_staged_app
        inspect_staged_app(self.builds)

    def test_09_real_bedrock_rosetta_network_console_and_shutdown(self):
        """Execute the actual official Bedrock server; never a player's world."""
        test_linuxkit(REPO, offline=True)
        receipt = self.builds / 'Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json'
        self.assertEqual(json.loads(receipt.read_text())['status'], 'passed')

    def test_10_real_wixl_msi_compilation_and_registration(self):
        """Build/inspect a true x64 MSI with Setup-provisioned tools only."""
        if 'win' not in self.receipt['targets']:
            self.skipTest('Targeted Mac-only Build has no Windows MSI')
        from host.lifecycle.amd64_backend import managed_amd64_backend
        from host.windows.msi_installer import test as smoke_msi
        with managed_amd64_backend(self.builds, setup=False):
            report = smoke_msi(self.builds)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['template_checks']['registration']['uninstall_supported'])

    def test_11_real_guest_bash_vnc_and_clean_qemu_shutdown(self):
        """Exercise real Linux/Windows PTYs, both viewers and owned VM exit."""
        result = run_managed_guest_acceptance(self.builds)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(all(value == 'passed' for value in result['checks'].values()))

    def test_12_native_elf_and_pe_staged_executables(self):
        """Read actual Linux/Windows binary headers from the Steam Stage."""
        from host.lifecycle.unpacked_release import status_four
        from host.lifecycle.universal_package import APP_ROOTS
        stages = status_four(self.builds)['targets']
        for target, magic, exe in (
            ('linux', b'\x7fELF', 'CinterForge Terminal'),
            ('win', b'MZ', 'CinterForge Terminal.exe'),
        ):
            if target not in self.receipt['targets']:
                continue
            with self.subTest(target=target):
                root = Path(stages[target]['steam_root']) / APP_ROOTS[target] / exe
                self.assertTrue(root.is_file(), str(root))
                with root.open('rb') as stream:
                    self.assertEqual(stream.read(len(magic)), magic)


def load_tests(loader, tests, pattern):
    """Hard cap for root smoke tests: never hide a second discovery suite."""
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(RepositoryContractSmoke))
    suite.addTests(loader.loadTestsFromTestCase(RealStagedSystemSmoke))
    if suite.countTestCases() > 50:
        raise RuntimeError('Smoke suite exceeds 50; consolidate substantial checks into fewer tests')
    return suite


if __name__ == '__main__':
    unittest.main()
