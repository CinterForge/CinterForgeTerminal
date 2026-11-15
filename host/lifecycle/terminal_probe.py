"""Native terminal directory acceptance: real shell, real cwd, recorded stdout.

A Wine run is never counted as a native Windows terminal. Every target runner
executes this module locally after its own application build, then writes a
version-bound log and receipt to sibling Builds. Directory listings are also
shown automatically when opening an interactive CinterForgeTerminal session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

from host import HOST_RELEASE

SCHEMA = 'cinterforge.terminal.native-directory.v1'
TARGETS = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}
ARCHES = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}


def native() -> tuple[str, str]:
    try:
        return TARGETS[platform.system()], ARCHES[platform.machine().lower()]
    except KeyError as exc:
        raise RuntimeError('Unsupported native terminal platform/architecture') from exc


def msys_path(path: Path | str) -> str:
    """Convert Windows drive paths for MSYS2 bash (no global cygpath needed)."""
    value = str(path).replace('\\', '/')
    match = re.fullmatch(r'([A-Za-z]):/(.*)', value)
    if not match:
        raise ValueError(f'Expected absolute Windows drive path for MSYS2: {path}')
    return '/' + match.group(1).lower() + '/' + match.group(2)


def msys_root(builds: Path) -> Path:
    """Reuse a *complete* installed MSYS2 UCRT64 tree staged under Builds.

    The app distribution must include this closure; an ambient installation
    isn't silently considered packaged. During Setup an existing installation
    can be adopted by copying it here, without compiling MSYS2 or Bash.
    """
    root = builds.resolve() / 'Toolchains/MSYS2/win/x64'
    required = ('usr/bin/bash.exe', 'usr/bin/ls.exe', 'ucrt64/bin/g++.exe',
                'ucrt64/bin/gcc.exe', 'ucrt64/bin/cmake.exe', 'ucrt64/bin/ninja.exe')
    if root.is_symlink() or any((root / item).is_symlink() for item in required):
        raise RuntimeError(f'Unsafe symlink in MSYS2 stage: {root}')
    missing = [item for item in required if not (root / item).is_file()]
    if missing:
        raise RuntimeError(f'MSYS2 UCRT64 stage incomplete: {root} (missing {", ".join(missing)}). '
                           'Copy/adopt an existing complete MSYS2 installation during Setup; '
                           'do not assemble individual DLLs or download during offline builds.')
    return root


def shell(builds: Path, system: str, *, interactive: bool = False,
          rcfile: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """Return an actual native Bash invocation with deterministic UCRT64 PATH."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if system == 'win':
        root = msys_root(builds)
        bash = root / 'usr/bin/bash.exe'
        env.update(MSYSTEM='UCRT64', CHERE_INVOKING='1', MSYS2_PATH_TYPE='inherit',
                   PATH=os.pathsep.join([str(root / 'ucrt64/bin'), str(root / 'usr/bin'),
                                         os.environ.get('PATH', '')]))
        command = [str(bash), '--noprofile']
        if interactive:
            if rcfile is None:
                raise ValueError('Interactive shell requires an explicit rcfile')
            command += ['--rcfile', msys_path(rcfile), '-i']
        else:
            command += ['--norc', '-c']
        return command, env
    bash = Path('/bin/bash')
    if not bash.is_file():
        found = shutil.which('bash')
        if not found:
            raise RuntimeError(f'Native Bash is missing on {system}')
        bash = Path(found).resolve()
    command = [str(bash), '--noprofile']
    if interactive:
        if rcfile is None:
            raise ValueError('Interactive shell requires an explicit rcfile')
        command += ['--rcfile', str(rcfile), '-i']
    else:
        command += ['--norc', '-c']
    return command, env


def receipt_path(builds: Path, system: str, arch: str) -> Path:
    return builds / 'CinterForgeTerminal/Bootstrap/Receipts' / system / arch / HOST_RELEASE / 'terminal-directory.json'


def directory_log_path(builds: Path, system: str, arch: str) -> Path:
    return builds / 'Logs/CinterForgeTerminal' / f'terminal-directory-{system}-{arch}.log'


def run_native(builds: Path, *, cwd: Path | None = None) -> dict:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError(f'Expected sibling Builds directory: {builds}')
    system, arch = native()
    working = (cwd or builds / 'Run/CinterForgeTerminal' / system).resolve()
    if not working.is_relative_to(builds):
        raise ValueError('Native directory probe working directory must be in Builds')
    working.mkdir(parents=True, exist_ok=True)
    log = directory_log_path(builds, system, arch)
    log.parent.mkdir(parents=True, exist_ok=True)
    # Bash on all three platforms; Windows' second native CMD directory pass
    # makes its actual cmd.exe semantics visible alongside MSYS2 Bash.
    command, env = shell(builds, system)
    marker = f'CINTER_TERMINAL_NATIVE_DIRECTORY platform={system} arch={arch}'
    script = 'printf "%s\\n" "$CINTER_TERMINAL_PROBE_MARKER"; pwd; ls -la'
    env['CINTER_TERMINAL_PROBE_MARKER'] = marker
    try:
        result = subprocess.run(command + [script], cwd=working, env=env,
                                capture_output=True, text=True, timeout=45, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.write_text(f'{marker}\nERROR: {exc}\n', encoding='utf-8')
        raise RuntimeError(f'Native {system} directory command did not launch; see {log}') from exc
    output = (f'[native {system}/{arch}] executable={command[0]}\n'
              f'working_directory={working}\ncommand=pwd; ls -la\n'
              f'{result.stdout}{result.stderr}')
    if system == 'win' and result.returncode == 0:
        cmd = subprocess.run(['cmd.exe', '/d', '/c', 'cd && dir'], cwd=working,
                             capture_output=True, text=True, timeout=30, check=False)
        output += f'\n[native win/cmd.exe] command=cd && dir\n{cmd.stdout}{cmd.stderr}'
        if cmd.returncode:
            result = subprocess.CompletedProcess(result.args, cmd.returncode,
                                                 result.stdout, result.stderr + '\ncmd.exe dir failed')
    log.write_text(output, encoding='utf-8')
    if result.returncode or marker not in result.stdout or not result.stdout.strip().splitlines()[-1:]:
        raise RuntimeError(f'Native directory listing failed for {system} (rc={result.returncode}); see {log}')
    receipt = {'schema': SCHEMA, 'platform': system, 'arch': arch, 'release': HOST_RELEASE,
               'execution': 'native', 'shell': str(command[0]), 'cwd': str(working),
               'command': 'pwd; ls -la' + ('; cmd.exe /d /c "cd && dir"' if system == 'win' else ''),
               'marker': marker, 'log': str(log.relative_to(builds)),
               'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest(),
               'completed_utc': time.time()}
    location = receipt_path(builds, system, arch)
    location.parent.mkdir(parents=True, exist_ok=True)
    tmp = location.with_suffix('.tmp')
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(location)
    print(output, flush=True)
    return receipt


def verify(builds: Path, system: str, arch: str, *, since_ns: int | None = None) -> dict:
    path = receipt_path(builds, system, arch)
    if not path.is_file() or (since_ns is not None and path.stat().st_mtime_ns <= since_ns):
        raise RuntimeError(f'Missing fresh native terminal directory receipt: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if any((data.get('schema') != SCHEMA, data.get('platform') != system,
            data.get('arch') != arch, data.get('release') != HOST_RELEASE,
            data.get('execution') != 'native',
            data.get('marker') != f'CINTER_TERMINAL_NATIVE_DIRECTORY platform={system} arch={arch}')):
        raise RuntimeError(f'Invalid native terminal receipt: {path}')
    log_rel = Path(data.get('log', ''))
    if log_rel.is_absolute() or '..' in log_rel.parts or not log_rel.parts:
        raise RuntimeError('Native terminal log must live within Builds')
    log = builds / log_rel
    if not log.is_file() or hashlib.sha256(log.read_bytes()).hexdigest() != data.get('log_sha256'):
        raise RuntimeError(f'Native terminal log missing/tampered: {log}')
    if data['marker'] not in log.read_text(encoding='utf-8'):
        raise RuntimeError(f'Native terminal log lacks platform marker: {log}')
    return data


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--builds', required=True, type=Path)
    cli.add_argument('--cwd', type=Path)
    cli.add_argument('--verify-all', action='store_true')
    args = cli.parse_args(argv)
    if args.verify_all:
        # Every OS must have run the probe itself; this never synthesizes a
        # Win/Linux report on a Mac host or accepts Wine as native Windows.
        roots = args.builds / 'CinterForgeTerminal/Bootstrap/Receipts'
        results = {target: [verify(args.builds, target, p.name)
                            for p in (roots / target).glob('*') if p.is_dir()]
                   for target in ('mac', 'linux', 'win')}
        if any(not values for values in results.values()):
            raise RuntimeError('Cannot accept three-platform terminals without native mac, linux and win logs')
    else:
        results = run_native(args.builds, cwd=args.cwd)
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print('[CinterForgeTerminal directory] FAIL:', error, file=sys.stderr)
        raise SystemExit(1)
