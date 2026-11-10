"""Real toolchain and terminal sign-of-life evidence for each target.

Never infer a target from a host-built exe or a marker file. This module runs
commands on the current OS; Linux and Windows execution uses the actual
registered target runner. All evidence is written under sibling Builds.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

SCHEMA = 'cinterforge.terminal.toolchain-smoke.v1'
TOOL_NAMES = ('bash', 'cc', 'cxx', 'cmake', 'ninja', 'python', 'node')


def target() -> tuple[str, str]:
    system = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}.get(platform.system())
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'amd64': 'x64'}.get(platform.machine().lower())
    if not system or not arch:
        raise RuntimeError(f'Unsupported native platform: {platform.system()} {platform.machine()}')
    return system, arch


def msys2_root(builds: Path) -> Path:
    """Find a complete UCRT64 installation, preferring an already staged copy.

    A system installation is reused for initial provisioning, but does not
    silently satisfy the offline-bundle contract. Stage it into Builds before
    producing a distributable bundle.
    """
    candidates = [builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2',
                  builds / 'Toolchains/MSYS2',
                  Path(os.environ.get('CINTER_MSYS2_ROOT', 'C:/msys64'))]
    if os.name == 'nt':
        candidates.extend([Path('C:/msys64'), Path('C:/tools/msys64')])
    for root in candidates:
        if all((root / p).is_file() for p in ('usr/bin/bash.exe', 'ucrt64/bin/gcc.exe',
                                              'ucrt64/bin/g++.exe', 'ucrt64/bin/cmake.exe',
                                              'ucrt64/bin/ninja.exe')):
            return root.resolve()
    raise RuntimeError('MSYS2 UCRT64 toolchain missing: expected Bash + GCC/G++ + CMake + Ninja; '
                       'install/reuse MSYS2 and stage it under Builds/CinterForgeTerminal/Toolchains/Windows/MSYS2')


def stage_windows_msys2(builds: Path) -> Path:
    """Reuse a complete preinstalled MSYS2; copy it once into shared Builds.

    A full, conventional installation is easier and safer than manually
    picking DLLs out of the UCRT64 toolchain. Never download at build time.
    """
    output = builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
    try:
        existing = msys2_root(builds)
    except RuntimeError:
        if os.name != 'nt':
            raise
        from host.windows_install import ensure
        existing = ensure(builds)
    if existing.is_relative_to(builds.resolve()):
        return existing
    if os.name != 'nt':
        raise RuntimeError('Windows MSYS2 can only be staged on native Windows')
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError(f'Incomplete Windows MSYS2 stage cannot be overwritten: {output}')
    import tempfile
    with tempfile.TemporaryDirectory(prefix='MSYS2-', dir=output.parent) as td:
        candidate = Path(td) / 'MSYS2'
        print(f'[MSYS2] staging existing full installation {existing} -> {output}', flush=True)
        shutil.copytree(existing, candidate, symlinks=False)
        candidate.rename(output)
    if not all((output / name).is_file() for name in (
        'usr/bin/bash.exe', 'usr/bin/cygpath.exe', 'ucrt64/bin/gcc.exe',
        'ucrt64/bin/g++.exe', 'ucrt64/bin/cmake.exe', 'ucrt64/bin/ninja.exe')):
        raise RuntimeError(f'MSYS2 copy is not a self-contained UCRT64 toolchain: {output}')
    return output


def msys2_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env['MSYSTEM'] = 'UCRT64'
    env['CHERE_INVOKING'] = '1'
    env['MSYS2_PATH_TYPE'] = 'inherit'
    env['PATH'] = os.pathsep.join([str(root / 'ucrt64/bin'), str(root / 'usr/bin'),
                                   env.get('PATH', '')])
    return env


def _probe(name: str, command: list[str], *, cwd: Path, env: dict[str, str]) -> dict:
    executable = command[0]
    resolved = shutil.which(executable, path=env.get('PATH')) if not Path(executable).is_file() else executable
    if not resolved:
        raise RuntimeError(f'{name}: missing executable {executable}')
    proc = subprocess.run(command, cwd=cwd, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=30, check=False)
    output = proc.stdout.strip()
    if proc.returncode or not output:
        raise RuntimeError(f'{name}: sign-of-life failed rc={proc.returncode}: {output[-500:]}')
    print(f'[TOOLCHAIN {name}] executable={resolved} rc={proc.returncode} version={output.splitlines()[0]}', flush=True)
    return {'executable': str(Path(resolved).resolve()), 'command': command,
            'exit_code': proc.returncode, 'output': output[:3000]}


def probe_local(builds: Path, *, require_cross: bool = False) -> dict:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError(f'Toolchain evidence requires sibling Builds: {builds}')
    system, arch = target()
    work = builds / 'Run/CinterForgeTerminal' / system / 'working-directory'
    work.mkdir(parents=True, exist_ok=True)
    marker = work / 'acceptance-marker.txt'
    marker.write_text('CinterForgeTerminal native terminal directory listing\n', encoding='utf-8')
    logdir = builds / 'Logs/CinterForgeTerminal'
    logdir.mkdir(parents=True, exist_ok=True)
    report = {'schema': SCHEMA, 'platform': system, 'arch': arch, 'status': 'failed',
              'working_directory': str(work), 'tools': {}, 'missing': [],
              'completed_utc': None}
    if system == 'win':
        try:
            root = stage_windows_msys2(builds)
            env = msys2_env(root)
            bash = root / 'usr/bin/bash.exe'
            # Login shell is essential: UCRT64 sets the target environment here.
            command = [str(bash), '--login', '-c']
            tools = {'bash': [str(bash), '--version'],
                     'cc': [str(root / 'ucrt64/bin/gcc.exe'), '--version'],
                     'cxx': [str(root / 'ucrt64/bin/g++.exe'), '--version'],
                     'cmake': [str(root / 'ucrt64/bin/cmake.exe'), '--version'],
                     'ninja': [str(root / 'ucrt64/bin/ninja.exe'), '--version'],
                     'python': [str(root / 'ucrt64/bin/python.exe'), '--version'],
                     'node': [str(root / 'ucrt64/bin/node.exe'), '--version']}
            report['msys2_root'] = str(root)
            report['offline_toolchain_staged'] = root.is_relative_to(builds)
            # No success receipt for an external machine-wide MSYS2 installation.
            if not report['offline_toolchain_staged']:
                report['missing'].append('Offline Windows MSYS2 closure is not staged inside Builds')
        except RuntimeError as exc:
            report['missing'].append(str(exc))
            env = dict(os.environ)
            tools = {}
            command = None
    else:
        env = dict(os.environ)
        bash = shutil.which('bash') or '/bin/bash'
        cc = shutil.which('clang' if system == 'mac' else 'gcc') or ('clang' if system == 'mac' else 'gcc')
        cxx = shutil.which('clang++' if system == 'mac' else 'g++') or ('clang++' if system == 'mac' else 'g++')
        tools = {'bash': [bash, '--version'], 'cc': [cc, '--version'],
                 'cxx': [cxx, '--version'], 'cmake': ['cmake', '--version'],
                 'ninja': ['ninja', '--version'],
                 'python': [str(builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/python3')
                            if system == 'mac' else sys.executable, '--version'],
                 'node': ['node', '--version']}
        command = [bash, '--noprofile', '--norc', '-c']
        if system == 'linux':
            tools['pkg-config'] = ['pkg-config', '--version']
            # The real native WebView link, not pkg-config alone, is the final gate.
            tools['webkitgtk'] = ['pkg-config', '--modversion', 'webkit2gtk-4.1']
        # Cross-tools may live in the managed Docker/MinGW/Wine image. They are
        # provisioned and run by Setup, never assumed to exist on the Mac PATH.
    try:
        for name, args in tools.items():
            try:
                report['tools'][name] = _probe(name, args, cwd=work, env=env)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                report['missing'].append(f'{name}: {exc}')
                print(f'[TOOLCHAIN {name}] FAIL {exc}', flush=True)
        if require_cross and system == 'mac':
            try:
                from host.windows_environment import WindowsCrossEnvironment
                cross = WindowsCrossEnvironment(builds).ensure()
                report['cross_provider'] = cross.provider
                report['cross_image'] = cross.image
                report['tools'].update(cross.version_probes(work))
                if cross.provider != 'host':
                    report['tools']['docker'] = _probe('docker', [cross.docker, '--version'],
                                                      cwd=work, env=env)
            except (OSError, RuntimeError, subprocess.TimeoutExpired,
                    subprocess.CalledProcessError) as exc:
                report['missing'].append(f'windows-cross-provisioning: {exc}')
                print(f'[TOOLCHAIN windows-cross-provisioning] FAIL {exc}', flush=True)
        if command:
            try:
                listing = _probe('native-shell-directory', command +
                    ['printf "CINTER_TERMINAL_DIRECTORY_BEGIN\\n"; pwd; ls -la; '
                     'printf "CINTER_TERMINAL_DIRECTORY_END\\n"'],
                    cwd=work, env=env)
                if ('acceptance-marker.txt' not in listing['output'] or
                        'CINTER_TERMINAL_DIRECTORY_BEGIN' not in listing['output'] or
                        'CINTER_TERMINAL_DIRECTORY_END' not in listing['output']):
                    raise RuntimeError('Actual shell did not list its working directory and marker')
                report['directory_listing'] = listing
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                report['missing'].append(f'native-shell-directory: {exc}')
        report['status'] = 'passed' if not report['missing'] else 'failed'
        report['completed_utc'] = time.time()
        stem = f'toolchain-{system}-{arch}'
        (logdir / (stem + '.json')).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        lines = [f'CinterForgeTerminal toolchain sign of life: {system}/{arch}',
                 f'Status: {report["status"]}']
        for name, item in report['tools'].items():
            lines.extend([f'== {name}: {item["executable"]}', item['output']])
        if 'directory_listing' in report:
            lines.extend(['== Native terminal directory listing', report['directory_listing']['output']])
        lines.extend('FAIL: ' + message for message in report['missing'])
        (logdir / (stem + '.log')).write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'[TOOLCHAIN] {system}/{arch}: {report["status"]}; evidence: {logdir / (stem + ".log")}', flush=True)
        if report['missing']:
            raise RuntimeError(f'{system}/{arch} toolchain acceptance incomplete:\n  ' + '\n  '.join(report['missing']))
        return report
    except Exception:
        raise


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--builds', required=True, type=Path)
    cli.add_argument('--require-cross', action='store_true')
    args = cli.parse_args()
    probe_local(args.builds, require_cross=args.require_cross)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
