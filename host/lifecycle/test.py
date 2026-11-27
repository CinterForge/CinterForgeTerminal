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
#   test_linuxkit: Handle test linuxkit through this component's established interfaces.
#   _test_installed_testgrounds: Exercise the normal installed TestGrounds preparation.
#   run_managed_guest_acceptance: Run run managed guest acceptance for this module.
#   _run_with_guest_test_environment: Run run with guest test environment for this module.
#   RepositoryContractSmoke: class responsible for repositorycontractsmoke
#   RepositoryContractSmoke.test_01_single_root_smoke_file: All repository smoke tests are physically here, not merely excluded.
#   ...and 14 additional internal entry points; see their local documentation.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
"""Single real-system smoke/acceptance suite for CinterForge Terminal.

The Test phase consumes completed Setup, Build and Stage artifacts. Only
Mac performs native Minecraft hosting checks; foreign targets verify owner
inputs without repeating a server under Wine/QEMU. Bedrock acceptance uses the installed application's normal TestGrounds preparation
path. Other worlds and previously prepared gameplay are untouched.
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


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print('[CFOS LinuxKit Test] ' + ' '.join(map(str, command)), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_staged_asset_modules(app: Path, target: str, release: str) -> None:
    """Verify this platform's installed Assets code against current authored source.

    Called only during Test. A Mac-only Build/Stage cannot refresh Linux or
    Windows, so cross-target source freshness belongs to each target's Test.
    A full `Cinter test all` still verifies all three targets individually.
    """
    from host.lifecycle.universal_package import FOUR_ARCH

    if target not in ('mac', 'linux', 'win'):
        raise ValueError(f'Assets installed-source test has no target {target!r}')
    embedded = (app / 'Contents/Resources/Builds' if target == 'mac'
                else app / 'Builds')
    service = embedded / 'Python/Host' / target / FOUR_ARCH[target] / release / 'webapp'
    for relative in (
        'runtime/assets/CinterForgeAssetCloudflare.py',
        'runtime/assets/CinterForgeAssetViews.py',
        'runtime/CinterForgeCommands.py',
        'runtime/assets/asset_gallery.py',
        'client/minecraft/CinterForgeJavaReport.js',
    ):
        source = REPO / 'webapp' / relative
        installed = service / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError('Authored Assets runtime missing or unsafe: ' + str(source))
        if not installed.is_file() or installed.is_symlink():
            raise RuntimeError(f'{target} Assets runtime missing from installed app: {installed}; '
                               f'run ./Cinter build {target} and ./Cinter stage {target}')
        if _sha256(installed) != _sha256(source):
            raise RuntimeError(f'{target} Assets runtime in installed app is stale: {installed}; '
                               f'run ./Cinter build {target} and ./Cinter stage {target}')
    from host.lifecycle.craftmaster_dependency import craftmaster_minecraft_files
    for relative, source in craftmaster_minecraft_files(embedded / 'CinterForgeCraftMaster' / target / 'data').items():
        installed = service / relative
        if installed.is_symlink() or not installed.is_file() or _sha256(installed) != _sha256(source):
            raise RuntimeError(f'{target} installed CraftMaster owner resource mismatch: {relative}')
    # Voxel is a mandatory real browser payload, not a button-only feature.
    # Verify the actual installed web runtime, bridge and service worker at
    # the exact path served by /blockbench/ in the installed Python host.
    voxel = service / 'data/asset_engine/voxel'
    for relative in ('index.html', 'dist/bundle.js',
                     'cinterforge/cinterforge_bridge.js', 'service_worker.js'):
        packaged = voxel / relative
        if packaged.is_symlink() or not packaged.is_file() or packaged.stat().st_size == 0:
            raise RuntimeError(f'{target} CinterForge Voxel editor missing from installed app: '
                               f'{packaged}; run ./Cinter setup all, build {target}, stage {target}')
    index = (voxel / 'index.html').read_text(encoding='utf-8')
    if 'cinterforge/cinterforge_bridge.js' not in index:
        raise RuntimeError(f'{target} CinterForge Voxel has no installed Save Back bridge')
    print(f'[TEST STAGE] PASS {target} installed Assets and CinterForge Voxel are staged', flush=True)


def verify_staged_zeroconf_library(app: Path, target: str, release: str) -> None:
    """Verify the retail ctypes dependency at its exact installed lookup path.

    Build's inventory and Stage's receipt already verify its SHA-256; this
    additional Test gate catches a packaging omission before retail launch.
    """
    if target not in ('linux', 'win'):
        raise ValueError('Retail Zeroconf check requires Linux or Windows')
    arch = 'x64'
    filename = ('CinterForgePythonHost.dll' if target == 'win'
                else 'libCinterForgePythonHost.so')
    component = 'bin' if target == 'win' else 'lib'
    required = (app / 'Builds/Python/Host' / target / arch / release /
                component / filename)
    if required.is_symlink() or not required.is_file():
        raise RuntimeError(f'{target} retail Zeroconf library missing: {required}; '
                           f'run ./Cinter build {target} and ./Cinter stage {target}')
    magic = b'MZ' if target == 'win' else b'\x7fELF'
    with required.open('rb') as stream:
        if stream.read(len(magic)) != magic:
            raise RuntimeError(f'{target} retail Zeroconf library is not a native {target} binary: '
                               + str(required))
    print(f'[TEST STAGE] PASS {target} retail Zeroconf native library: {required}', flush=True)


def verify_staged_java_runtime(builds: Path, app: Path, target: str, release: str):
    """Check the installed adapter and owner-pinned Java inputs, without hosting.

    CraftMaster owns the server; Terminal must consume the exact verified owner
    payload. Never start a foreign JVM to certify its native Windows/Linux UI.
    """
    from host.lifecycle.craftmaster_dependency import verify_embedded_craftmaster
    from host.lifecycle.universal_package import FOUR_ARCH
    from webapp.runtime.CinterForgeJava import _verified_owner
    binary = verify_embedded_craftmaster(app, target)
    data = binary.parent.parent / 'data'
    owner = _verified_owner(data)
    cm = owner.provider(data)
    root = builds / 'CinterForgeCraftMaster/Server/Java'
    java, jar = owner.ensure_runtime(root, cm, target=target,
                               arch=owner.native_target()[1] if target == 'mac' else 'x64',
                               offline=True, setup_mac_builds=builds if target == 'mac' else None)
    embedded = app / 'Contents/Resources/Builds' if target == 'mac' else app / 'Builds'
    service = embedded / 'Python/Host' / target / FOUR_ARCH[target] / release / 'webapp'
    for name in ('runtime/CinterForgeJava.py', 'bedrock_status_bridge/manifest.json',
                 'bedrock_status_bridge/scripts/main.js', 'bedrock_status_bridge/items/terminal.json'):
        source = REPO/'webapp'/name if name.startswith('runtime/') else data/name
        installed = service/name
        if installed.is_symlink() or not installed.is_file() or _sha256(installed) != _sha256(source):
            raise RuntimeError('Stale staged Java/communications asset: '+name)
    print(f'[JAVA TEST] PASS {target} installed adapter, owner controller, pinned offline JDK and server JAR', flush=True)
    return binary, data, java, jar, embedded


def run_staged_java_smoke(builds: Path, app: Path, target: str, release: str) -> dict:
    """One native Mac Java readiness/console/shutdown check, not three owner tests.

    Foreign hosts receive verified Stage/owner-input checks only. Actual native
    Windows/Linux Java hosting and GUI acceptance remain target-host work.
    """
    import uuid
    from host.lifecycle.craftmaster_dependency import server_test_fixture
    if target != 'mac' or platform.system() != 'Darwin':
        raise RuntimeError('Native Java hosting smoke requires the Mac producer')
    binary, data, java, jar, embedded = verify_staged_java_runtime(builds, app, target, release)
    root = builds / 'CinterForgeCraftMaster/Server/Java'
    scratch = builds/'Acceptance/CinterForgeTerminal/Java'/target/uuid.uuid4().hex
    fixture, provenance = server_test_fixture(builds)
    scratch.mkdir(parents=True)
    world = scratch/'CinterForge TestGrounds'
    shutil.copytree(fixture/'Java', world)
    logs = builds/'Logs/CinterForgeTerminal/JavaAcceptance'/target/scratch.name
    logs.mkdir(parents=True)
    common = ['--world',str(world),'--data-dir',str(data),'--server-root',str(root),
              '--java',str(java),'--server-jar',str(jar)]
    python = embedded/'Python/Environments/mac/arm64/cinterterminalenv/bin/python'
    command = [str(python),'-B',str(data/'java_server.py'),'serve',*common,'--check']
    from host.windows.windows_environment import _run as streamed_run
    result = streamed_run(command,cwd=scratch,timeout=900,output_log=logs/'serve.log')
    result.check_returncode()
    report = {'status':'PASS','target':target,'world':str(world),
              'source':provenance['source'], 'fixture_mcworld_sha256':provenance['mcworld_sha256'],
              'binary_sha256':_sha256(binary), 'checks':['server-ready','console-list','clean-shutdown']}
    (logs/'JavaSmoke.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('[JAVA SMOKE] PASS '+target+' '+str(logs/'JavaSmoke.json'),flush=True)
    shutil.rmtree(scratch)
    return report


def test_linuxkit(repo: Path, *, offline: bool = False) -> None:
    """Exercise the installed application's TestGrounds installer on the actual save."""
    repo = repo.resolve()
    if not (repo / 'Cinter').is_file():
        raise RuntimeError(f'Expected CinterForgeTerminal checkout: {repo}')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('CFOS LinuxKit Bedrock/Rosetta Test requires Apple Silicon macOS')
    from host.linux.bedrock_consent import require_bedrock_eula
    from host.lifecycle.craftmaster_dependency import server_test_fixture
    from host.lifecycle.unpacked_release import status_four
    from host.lifecycle.universal_package import APP_ROOTS, FOUR_ARCH, HOST_RELEASE
    root = repo.parent / 'Builds'
    require_bedrock_eula(root, 'mac')
    verify_shared_bootstrap(repo, require_launcher=False)
    guest = verify_guest()
    fixture, provenance = server_test_fixture(root)
    location = status_four(root)['targets']['mac']
    if not location['steam_receipt_matches']:
        raise RuntimeError('Bedrock acceptance requires the verified installed Mac app')
    embedded = Path(location['steam_root']) / APP_ROOTS['mac'] / 'Contents/Resources/Builds'
    service = embedded / 'Python/Host' / 'mac' / FOUR_ARCH['mac'] / HOST_RELEASE / 'webapp'
    pack = service / 'bedrock_status_bridge'
    for asset in ('manifest.json', 'scripts/main.js', 'items/terminal.json'):
        if _sha256(pack / asset) != _sha256(embedded / 'CinterForgeCraftMaster/mac/data/bedrock_status_bridge' / asset):
            raise RuntimeError('Stale staged communications asset: ' + asset)
    for asset in ('CinterForgeBedrock.py', 'CinterForgeCommands.py', 'CinterForgeHTTPRoutes.py', 'CinterForgeSessions.py'):
        if _sha256(service / 'runtime' / asset) != _sha256(repo / 'webapp/runtime' / asset):
            raise RuntimeError('Stale staged TestGrounds runtime: ' + asset)
    python = embedded / 'Python/Environments/mac/arm64/cinterterminalenv/bin/python'
    receipt = root / 'Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    evidence = {'schema': 'cinterforge.cfos.linuxkit-bedrock-test.v1',
                'bootstrap_sha256': {name: _sha256(guest / name) for name in
                    ('cinter-bds-kernel', 'cinter-bds-initrd.img', 'cinter-bds-cmdline', 'CinterBDSVM')},
                'status': 'failed', 'checked_at': datetime.now(timezone.utc).isoformat(),
                'test': 'real-official-bedrock-rosetta-nethernet-console-stop',
                'world': {'name': provenance['name'], 'source': provenance['source'],
                          'mcworld_sha256': provenance['mcworld_sha256']}}
    try:
        with tempfile.TemporaryDirectory(prefix='TestGrounds-', dir=receipt.parent) as temp:
            result = Path(temp) / 'InstalledWorld.json'
            helper = ('import sys; from pathlib import Path; '
                      'sys.path.append(sys.argv[1]); '
                      'from host.lifecycle.test import _test_installed_testgrounds; '
                      '_test_installed_testgrounds(*map(Path, sys.argv[2:]))')
            _run([str(python), '-B', '-u', '-c', helper, str(repo), str(embedded),
                  str(root), str(fixture / 'fixture.json'), str(result)], cwd=service)
            evidence['installed_bedrock'] = json.loads(result.read_text('utf-8'))
        evidence['status'] = 'passed'
    except Exception as exc:
        evidence['error'] = str(exc)
        raise
    finally:
        receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'[CFOS LinuxKit Test] Evidence: {receipt}', flush=True)
    print('[BEDROCK TEST] PASS installed TestGrounds; native Rosetta, NetherNet, list, stop, exit verified', flush=True)


def _test_installed_testgrounds(embedded: Path, builds: Path, fixture_receipt: Path, result: Path) -> None:
    """Offline acceptance of the staged runtime, using the Setup-verified publication.

    No new world/server copy is created for acceptance. Explicit Setup consent
    and its cached official ZIP permit the same normal package installation on
    a clean developer machine without downloading during Test.
    """
    import zipfile
    from runtime.CinterForgeBedrock import CinterForgeBedrockManager
    from host.lifecycle.craftmaster_dependency import (craftmaster_bedrock_module,
        read_native_test_fixture_receipt, _verify_installed_testgrounds, _world_artifacts)
    receipt = read_native_test_fixture_receipt(fixture_receipt)
    entry = receipt['catalog_entry']
    manager = CinterForgeBedrockManager(embedded, 'mac', user_install=True)
    if manager.app != 'CinterForgeTerminal':
        raise RuntimeError('TestGrounds acceptance must use the normal Terminal server root')
    if manager._live_session_locked()[0] is not None:
        raise RuntimeError('Stop the installed Bedrock server before running TestGrounds acceptance')
    source = manager._java_saves_destination() / entry['folder']
    if source.resolve() != Path(receipt['installed_java']).resolve():
        raise RuntimeError('TestGrounds Setup and installed runtime disagree on the Java saves root')
    _verify_installed_testgrounds(_world_artifacts(builds), source, entry, receipt)
    publication = source / 'CinterForge/InstalledPublication.json'
    if (not publication.is_file() or publication.is_symlink() or
            json.loads(publication.read_text('utf-8')).get('sha256') != entry['sha256']):
        raise RuntimeError('Installed TestGrounds publication does not match Setup; run ./Cinter setup testgrounds')
    java = next((item for item in manager.list_java_worlds()
                 if Path(item['path']) == source.resolve()), None)
    if java is None:
        raise RuntimeError('Installed TestGrounds Java save is not discoverable: ' + str(source))
    export = manager.java_world_export_name(java)
    archive = manager.craftmaster_output_root(java) / 'Worlds' / export / (export + '.mcworld')
    if not archive.is_file() or archive.is_symlink() or _sha256(archive) != receipt['mcworld_sha256']:
        raise RuntimeError('Installed TestGrounds MCWorld does not match verified Setup input')
    if not manager.accepted():
        if not craftmaster_bedrock_module(builds, 'CinterForgeLifecycleBedrock').CinterForgeLifecycleBedrock(builds, 'mac').accepted():
            raise PermissionError('Bedrock EULA consent missing; rerun interactive Setup')
        manager.accept(True)
    cached = builds / 'CinterForgeCraftMaster/Server/Bedrock/Downloads/bedrock-server-linux.zip'
    state = manager.prepare_testgrounds(java, verify_server=True, server_archive=cached,
                                        progress=lambda line: print(line, flush=True))
    proof = state['testgrounds']
    if (proof['archive_sha256'] != receipt['mcworld_sha256'] or not proof['graceful_stop'] or
            not proof['native_server_verified'] or state['running']):
        raise RuntimeError('Installed TestGrounds native acceptance proof incomplete')
    result.write_text(json.dumps({'source_world': state['source_world'], 'active_world': state['active_world'],
                                  'mcworld_sha256': proof['archive_sha256'], 'fixture': proof['fixture'],
                                  'fixture_previously_installed': state['fixture_previously_installed']},
                                 indent=2) + '\n', encoding='utf-8')


GUEST_SCHEMA = 'cinterforge.terminal.guest-test-acceptance.v1'
GUEST_CHECKS = ('linux_vm', 'linux_guest', 'linux_bash', 'linux_vnc',
          'windows_guest', 'windows_bash', 'windows_vnc', 'shutdown')


def run_managed_guest_acceptance(builds: Path, *, require_msi: bool = False) -> dict:
    # The Test stage runs actual VM/Bash/VNC acceptance after Stage; however
    # Lifecycle never imports the application HTTP server; leave environment
    # overrides only for the duration of actual guest acceptance.
    keys = ('CINTER_BUILDS_ROOT', 'CINTER_TERMINAL_RUNTIME_MODE')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        return _run_with_guest_test_environment(builds, require_msi=require_msi)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_with_guest_test_environment(builds: Path, *, require_msi: bool = False) -> dict:
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
    sdk = builds / 'Toolchains'
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
            if require_msi:
                from host.windows.msi_installer import _check_tools, test as test_msi
                try:
                    _check_tools(builds)
                    print('[WINDOWS MSI TEST] Reusing verified compiler/registration receipt', flush=True)
                except RuntimeError:
                    # Reuse the currently owned x64 daemon, before starting
                    # the interactive guests. No nested backend/VM lifetime.
                    selectors = ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')
                    saved = {key: os.environ.get(key) for key in (*shell_env, *selectors)}
                    try:
                        for key in selectors:
                            os.environ.pop(key, None)
                        os.environ.update(shell_env)
                        test_msi(builds)
                        _check_tools(builds)
                    finally:
                        for key, value in saved.items():
                            if value is None:
                                os.environ.pop(key, None)
                            else:
                                os.environ[key] = value
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
        self.assertEqual(len(games['games']), 61)
        self.assertEqual(len(titles['titles']), 16)
        self.assertEqual(len({item['command'] for item in games['games']}), 61)
        terminal = (REPO / 'webapp/runtime/CinterForgeCommands.py').read_text() + (REPO / 'webapp/runtime/CinterForgeState.py').read_text()
        self.assertIn('lagomorph', terminal.lower())
        self.assertTrue((REPO / 'webapp/index.html').is_file())
        self.assertIn('codeoss', (REPO / 'webapp/data/terminal_styles.json').read_text().lower())

    def test_05_test_phase_is_not_invoked_by_setup(self):
        """Assert the actual CLI's Setup branch doesn't call Test by mistake."""
        source = (REPO / 'Cinter').read_text()
        setup_start = source.index("if argv in (['setup', 'all']")
        setup_end = source.index('    # Three unpacked native Build outputs', setup_start)
        body = source[setup_start:setup_end]
        self.assertNotIn("'host/linux/lifecycle.py'), 'test'", body)
        self.assertNotIn("'host.guest_runtime_acceptance'", body)
        self.assertNotIn("'host/lifecycle/test.py'", body)
        self.assertNotIn("'test', selected", body)
        self.assertIn('managed_amd64_backend', body)
        self.assertIn('prepare_server_test_fixture(BUILDS)', body)
        self.assertNotIn("craftmaster_world_command(BUILDS, ['setup', 'worlds'", body)

    def test_06_build_and_stage_do_not_call_smoke_suite(self):
        """The only root-suite launcher is the explicit Test dispatcher."""
        source = (REPO / 'Cinter').read_text()
        self.assertEqual(source.count('_run_automated_regression_tests()'), 2)
        self.assertIn("'stage', selected", source)
        self.assertIn("'test', selected", source)
        self.assertIn("'-B', '-m', 'host.lifecycle.test', '-q'", source)
        import inspect
        smoke = inspect.getsource(run_staged_java_smoke)
        self.assertIn("'serve',*common,'--check'", smoke)
        self.assertNotIn("'generate'", smoke)
        self.assertNotIn("'convert'", smoke)
        self.assertNotIn('--verify-generated-spawn', smoke)
        self.assertNotIn('validated_worlds(', smoke)


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
            if target == 'devtools':
                continue  # shared Builds/Toolchains is never Steam-staged
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
                if target in ('mac', 'linux', 'win'):
                    # Real platform-matched CraftMaster bytes are mandatory in
                    # every installed Terminal, independent of its DevTools.
                    from host.lifecycle.universal_package import APP_ROOTS
                    from host.lifecycle.craftmaster_dependency import verify_embedded_craftmaster
                    app = Path(stage['steam_root']) / APP_ROOTS[target]
                    if target in ('win', 'linux'):
                        verify_staged_zeroconf_library(app, target, self.release)
                    binary = verify_embedded_craftmaster(app, target)
                    self.assertGreater(binary.stat().st_size, 1024)
                    # `Cinter test mac` must not reject a previous Linux/
                    # Windows Stage merely because only Mac was rebuilt.
                    # Their source-integrity checks run in `Cinter test linux`
                    # and `Cinter test win`, including during `test all`.
                    if target == 'mac':
                        verify_staged_asset_modules(app, target, self.release)
                        run_staged_java_smoke(self.builds, app, target, self.release)

    def test_08_real_signed_mac_app_and_codeoss_lagomorph(self):
        """Verify the exact signed staged native app and real bundled assets."""
        from host.mac.mac_app import test as inspect_staged_app, accept_native_ui
        from host.lifecycle.unpacked_release import status_four
        from host.lifecycle.universal_package import APP_ROOTS
        inspect_staged_app(self.builds)
        stage = status_four(self.builds)
        location = stage['targets']['mac']
        self.assertTrue(location['steam_receipt_matches'],
                        'Cannot launch an unverified Steam Stage Mac application')
        steam = Path(stage['steam_destination']).parents[2]
        release_dir = Path(location['steam_root'])
        app = release_dir / APP_ROOTS['mac']
        embedded = app / 'Contents/Resources/Builds'
        # These assets are needed by the actual installed HTTP/Code-OSS server,
        # not merely by a producer workbench in sibling Builds.
        code = embedded / 'CodeOSS/extensions'
        for rel in ('markdown-language-features/package.json',
                    'html-language-features/package.json',
                    'mermaid-markdown-features/dist/browser/extension.js'):
            with self.subTest(staged_preview=rel):
                self.assertTrue((code / rel).is_file() and not (code / rel).is_symlink(),
                                f'Installed Code-OSS preview missing: {code / rel}')
        for rel in ('vscode-textmate/release/main.js',
                    'vscode-oniguruma/release/onig.wasm',
                    '@vscode/vscode-languagedetection/dist/lib/index.js'):
            path = embedded / 'CodeOSS/node_modules' / rel
            with self.subTest(staged_code_runtime=rel):
                self.assertTrue(path.is_file() and not path.is_symlink(),
                                f'Installed Code-OSS editor dependency missing: {path}')
        # The staged native app is lightweight. Its optional SDK remains
        # under the developer workspace, not a second Steam copy. Verify the
        # exact shared path via the staged runtime's developer lookup.
        python = embedded / 'Python/Environments/mac/arm64/cinterterminalenv/bin/python'
        webapp = embedded / f'Python/Host/mac/arm64/{self.release}/webapp'
        expected = (self.builds / 'Toolchains').resolve()
        env = dict(os.environ, CINTER_BUILDS_ROOT=str(self.builds),
                   CINTER_TERMINAL_RUNTIME_MODE='developer')
        code_check = ('from runtime.CinterForgeRuntimeBootstrap import sdk_root; '
                      'p=sdk_root(); print(str(p) if p else "MISSING")')
        probe = subprocess.run([str(python), '-B', '-c', code_check], cwd=webapp,
                               env=env, capture_output=True, text=True,
                               timeout=40, check=False)
        self.assertEqual(probe.returncode, 0,
                         'Shared DevTools resolver failed: ' + probe.stderr[-1000:])
        self.assertEqual(probe.stdout.strip(), str(expected),
                         'Runtime did not resolve Builds/Toolchains: ' + probe.stdout.strip())
        print('[TEST STAGE] PASS shared developer toolchains: ' + str(expected), flush=True)
        # A passing receipt or developer VM is not enough: exercise the real
        # signed binary installed in Steam and its own Python HTTP service.
        accept_native_ui(self.builds, launcher=app / 'Contents/MacOS/CinterForgeTerminal',
                         steamapps=steam)

    def test_09_real_bedrock_rosetta_network_console_and_shutdown(self):
        """Exercise the installed TestGrounds server and preserve existing gameplay."""
        test_linuxkit(REPO, offline=True)
        receipt = self.builds / 'Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json'
        self.assertEqual(json.loads(receipt.read_text())['status'], 'passed')

    def test_10_real_guest_bash_vnc_and_clean_qemu_shutdown(self):
        """Exercise actual PTYs/viewers and verify MSI evidence in the same VM lifetime."""
        result = run_managed_guest_acceptance(self.builds, require_msi='win' in self.receipt['targets'])
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(all(value == 'passed' for value in result['checks'].values()))

    def test_11_native_elf_and_pe_staged_executables(self):
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
    """Release gate contains only staged product checks; source checks remain directly callable."""
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(RealStagedSystemSmoke))
    if suite.countTestCases() > 50:
        raise RuntimeError('Smoke suite exceeds 50; consolidate substantial checks into fewer tests')
    return suite


if __name__ == '__main__':
    unittest.main()
