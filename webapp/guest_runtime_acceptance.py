"""Setup-only acceptance of the installed app's *actual* managed guest paths.

Uses the producer's already provisioned Toolchains, not a Steam receipt, and
starts the one dedicated Colima t profile and one shared Debian AMD64 container.
Tests interactive PTYs and retained RFB/noVNC endpoints. Always closes its own
PTYs and VM; never publishes, downloads, stages, or leaves live processes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import time

from webapp import CinterForgeTerminal as runtime

SCHEMA = 'cinterforge.terminal.guest-setup-acceptance.v1'
CHECKS = ('linux_vm', 'linux_guest', 'linux_bash', 'linux_vnc',
          'windows_guest', 'windows_bash', 'windows_vnc', 'shutdown')


def run(builds: Path) -> dict:
    # The canonical CLI invokes Setup acceptance in its own process, but
    # in-process regression callers must not leak developer path overrides
    # into later installed-app tests or service lifetimes.
    keys = ('CINTER_BUILDS_ROOT', 'CINTER_TERMINAL_RUNTIME_MODE')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        return _run_with_setup_environment(builds)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_with_setup_environment(builds: Path) -> dict:
    builds = builds.resolve(strict=True)
    if builds.name != 'Builds':
        raise RuntimeError('Guest runtime acceptance must use the existing sibling Builds')
    if platform.system() != 'Darwin':
        raise RuntimeError('Managed macOS Linux/Windows guest acceptance requires a Mac producer')
    sdk = builds / 'CinterForgeTerminal/Toolchains'
    # Setup alone creates all prerequisites and images in this canonical tree.
    required = ('mac/env/bin/colima', 'mac/env/bin/docker', 'mac/lima/bin/limactl',
                'Windows/MSYS2/usr/bin/bash.exe')
    missing = [part for part in required if not (sdk / part).is_file()]
    if missing:
        raise RuntimeError('Setup missing guest runtime inputs: ' + ', '.join(missing))
    os.environ['CINTER_BUILDS_ROOT'] = str(builds)
    os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'developer'
    workspace = builds.parent
    env = runtime._env(sdk)
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    receipt = logs / 'guest-connections-setup.json'
    transcript = logs / 'guest-connections-setup.log'
    state = {'schema': SCHEMA, 'status': 'failed', 'checks': {key: 'not-run' for key in CHECKS},
             'timestamp': time.time(), 'sdk': str(sdk), 'logs': str(transcript)}
    shells: list[runtime.TerminalSession] = []
    owns_vm = False

    def passed(check: str) -> None:
        state['checks'][check] = 'passed'
        with transcript.open('a', encoding='utf-8') as fp:
            fp.write('[SETUP GUEST] PASS ' + check + '\n')
        print('[SETUP GUEST] PASS ' + check, flush=True)

    transcript.write_text('Setup retained guest / Bash / VNC acceptance\n', encoding='utf-8')
    try:
        with runtime._managed_vm_lock(env):
            owner = runtime._runtime_watchdog_owner(env)
            if owner.is_file():
                record = json.loads(owner.read_text(encoding='utf-8'))
                native_pid = record.get('native_pid')
                if isinstance(native_pid, int) and runtime._live_runtime_watchdog(native_pid, env):
                    raise RuntimeError('An active native Terminal owns this VM; '
                                       'Setup must not adopt or stop its running connections')
            # No live app owns this profile; Setup now accepts cleanup
            # responsibility even if VM startup fails halfway.
            owns_vm = True
            # These functions are the same ones used at native app launch.
            docker = runtime._ensure_docker_locked(sdk, env, workspace)
            passed('linux_vm')
            for kind, label in (('linux', 'linux'), ('win', 'windows')):
                runtime._start_guest_locked(sdk, env, docker, kind, workspace)
                # _start_guest_locked has exercised the real guest's Bash and
                # activated and checked its actual VNC and noVNC endpoints.
                passed(label + '_guest')
                passed(label + '_vnc')
                session = runtime.TerminalSession(
                    builds, 'mac', 100, 30,
                    url='http://127.0.0.1:20300', token='setup-local-only',
                    command=runtime._runtime_guest_shell_command(sdk, kind, workspace),
                    child_env=runtime._guest_docker_context(sdk),
                    working_directory=workspace,
                    output_log=logs / ('guest-setup-' + kind + '-bash.log'))
                shells.append(session)
                runtime._verify_persistent_guest_shell(session, kind)
                passed(label + '_bash')
    except BaseException as error:
        state['error'] = str(error)
        with transcript.open('a', encoding='utf-8') as fp:
            fp.write('[SETUP GUEST] FAIL ' + repr(error) + '\n')
        raise
    finally:
        for session in reversed(shells):
            try:
                session.close()
            except Exception as error:
                state['cleanup_error'] = str(error)
        # Even if VM startup fails half-way, this is a dedicated Setup-owned
        # t profile. Stop only its verified labeled containers, then the VM.
        if owns_vm:
            try:
                runtime.stop_runtime_vm(setup_sdk=sdk)
                status = subprocess.run([str(sdk / 'mac/env/bin/colima'), 'status', runtime.PROFILE],
                                        capture_output=True, text=True, env=env, timeout=25)
                if status.returncode == 0:
                    raise RuntimeError('Setup-owned runtime VM remains running after cleanup')
                passed('shutdown')
            except Exception as error:
                state['cleanup_error'] = str(error)
                with transcript.open('a', encoding='utf-8') as fp:
                    fp.write('[SETUP GUEST] FAIL shutdown: ' + str(error) + '\n')
        if state['checks']['shutdown'] == 'passed' and all(v == 'passed' for v in state['checks'].values()):
            state['status'] = 'passed'
        receipt.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    if state['status'] != 'passed':
        raise RuntimeError('Managed guest Setup acceptance failed; see ' + str(receipt))
    return state


def verify_setup_receipt(builds: Path) -> dict:
    """Test-only read: validate durable Setup evidence without starting a VM."""
    file = builds / 'Logs/CinterForgeTerminal/guest-connections-setup.json'
    if not file.is_file() or file.is_symlink():
        raise RuntimeError('Managed runtime guest Setup acceptance receipt missing: ' + str(file))
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RuntimeError('Cannot read managed guest acceptance receipt: ' + str(error)) from error
    if (data.get('schema') != SCHEMA or data.get('status') != 'passed' or
            data.get('sdk') != str(builds.resolve() / 'CinterForgeTerminal/Toolchains') or
            not isinstance(data.get('checks'), dict) or
            any(data['checks'].get(check) != 'passed' for check in CHECKS)):
        raise RuntimeError('Managed runtime acceptance is incomplete; run ./Cinter setup all: ' + str(file))
    return data



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', required=True, type=Path)
    result = run(parser.parse_args().builds)
    print('[SETUP GUEST] ' + result['status'].upper(), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
