"""Provision a complete MSYS2 UCRT64 bootstrap inside sibling Builds on Windows.

MSYS2's official versioned installer and pacman are the authoritative
implementation; do not reconstruct a hand-picked collection of DLLs. Initial
acquisition may use network; finished application rebuilds use staged files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import time
import urllib.request

MSYS2_VERSION = '20260927'
MSYS2_INSTALLER_URL = ('https://repo.msys2.org/distrib/x86_64/'
                       f'msys2-x86_64-{MSYS2_VERSION}.exe')
PACKAGES = (
    'mingw-w64-ucrt-x86_64-gcc',
    'mingw-w64-ucrt-x86_64-cmake',
    'mingw-w64-ucrt-x86_64-ninja',
    'mingw-w64-ucrt-x86_64-python',
    'mingw-w64-ucrt-x86_64-nodejs',
    'mingw-w64-ucrt-x86_64-pkgconf',
    'mingw-w64-ucrt-x86_64-binutils',
)
REQUIRED = ('usr/bin/bash.exe', 'usr/bin/pacman.exe',
            'usr/bin/ls.exe', 'usr/bin/rm.exe', 'usr/bin/tar.exe',
            'ucrt64/bin/objdump.exe', 'ucrt64/bin/gcc.exe', 'ucrt64/bin/g++.exe',
            'ucrt64/bin/cmake.exe', 'ucrt64/bin/ninja.exe',
            'ucrt64/bin/python.exe', 'ucrt64/bin/node.exe')


def _execute(command: list[str], *, cwd: Path, log: Path, timeout: int = 1200,
             env: dict[str, str] | None = None) -> None:
    print('[WINDOWS MSYS2] ' + ' '.join(map(str, command)), flush=True)
    with log.open('a', encoding='utf-8') as stream:
        stream.write('$ ' + ' '.join(map(str, command)) + '\n')
        stream.flush()
        try:
            process = subprocess.run([str(s) for s in command], cwd=cwd,
                                     stdout=stream, stderr=subprocess.STDOUT, env=env,
                                     timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            stream.write(f'TIMEOUT={timeout}s\n')
            raise RuntimeError('MSYS2 provisioning timed out; inspect ' + str(log)) from exc
        stream.write(f'EXIT={process.returncode}\n')
    if process.returncode:
        raise RuntimeError(f'MSYS2 provisioning rc={process.returncode}; inspect {log}')


def ensure(builds: Path) -> Path:
    if platform.system() != 'Windows':
        raise RuntimeError('MSYS2 provisioning must run on native Windows')
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('MSYS2 installation requires sibling Builds directory')
    root = builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
    if all((root / name).is_file() for name in REQUIRED):
        print('[WINDOWS MSYS2] Reusing complete staged UCRT64 installation', flush=True)
        return root
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / 'windows-msys2-setup.log'
    downloads = builds / 'CinterForgeTerminal/Toolchains/Windows/Downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    installer = downloads / f'msys2-x86_64-{MSYS2_VERSION}.exe'
    if not (root / 'usr/bin/pacman.exe').is_file():
        if root.exists() and any(root.iterdir()):
            raise RuntimeError(f'Existing incomplete MSYS2 tree requires recovery: {root}')
        if not installer.is_file():
            print('[WINDOWS MSYS2] Fetching official versioned MSYS2 installer', flush=True)
            partial = installer.with_suffix('.exe.part')
            try:
                with urllib.request.urlopen(MSYS2_INSTALLER_URL, timeout=90) as response, partial.open('wb') as out:
                    import shutil
                    shutil.copyfileobj(response, out)
                partial.replace(installer)
            finally:
                partial.unlink(missing_ok=True)
        # The official MSYS2 CLI installer uses `in --confirm-command`.
        # Install directly into Builds, not C:/msys64 or any source checkout.
        _execute([str(installer), 'in', '--confirm-command', '--accept-messages',
                  '--root', root.as_posix()], cwd=downloads, log=log)
    bash = root / 'usr/bin/bash.exe'
    if not bash.is_file():
        raise RuntimeError('MSYS2 installer did not produce native Bash')
    env = dict(os.environ, MSYSTEM='UCRT64', CHERE_INVOKING='1', MSYS2_PATH_TYPE='inherit')
    env['PATH'] = os.pathsep.join((str(root / 'ucrt64/bin'), str(root / 'usr/bin'),
                                    env.get('PATH', '')))
    # Use the actual installed MSYS2 pacman and UCRT64 packages.
    # The shell login sets all MSYS2 environment and path translations.
    # Direct pacman invocation avoids a guessed Python->MSYS argv conversion.
    pacman = root / 'usr/bin/pacman.exe'
    # A partial -Sy upgrade can mix incompatible MSYS2 core/CRT DLLs.
    # Update the entire installed environment before adding UCRT packages.
    _execute([str(pacman), '-Syu', '--noconfirm'], cwd=root, log=log, env=env)
    _execute([str(pacman), '-S', '--needed', '--noconfirm', *PACKAGES],
             cwd=root, log=log, env=env)
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise RuntimeError('MSYS2 provisioned but missing required binaries: ' + ', '.join(missing))
    receipt = {'schema': 'cinterforge.terminal.msys2-setup.v1',
               'installer': MSYS2_INSTALLER_URL, 'version': MSYS2_VERSION,
               'packages': PACKAGES, 'root': str(root), 'completed_utc': time.time(),
               'status': 'passed'}
    (logs / 'windows-msys2-setup.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return root
