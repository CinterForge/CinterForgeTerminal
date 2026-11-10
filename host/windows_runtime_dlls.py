"""Resolve and stage the actual recursive Windows PE DLL closure.

Never assume that libstdc++, libgcc and libwinpthread are the *only* DLLs.
Inspect imported names of both applications AND each copied dependency. Windows
OS DLLs remain OS-provided; every other DLL must resolve to the exact toolchain
used for the build and be copied beside the EXE, or acceptance fails.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import subprocess

SYSTEM_DLLS = frozenset('''
advapi32 bcrypt cabinet cfgmgr32 clbcatq comctl32 comdlg32 crypt32 cryptnet
cryptui d2d1 d3d10 d3d11 d3d12 d3d9 dbghelp dcomp ddraw dhcpcsvc dnsapi
dwmapi dxgi gdi32 hid imm32 iphlpapi kernel32 kernelbase mpr msimg32
msvcrt ncrypt netapi32 normaliz ntdll ole32 oleaut32 opengl32 propsys
psapi rpcrt4 secur32 setupapi shell32 shlwapi user32 userenv ucrtbase
version winhttp wininet winmm winspool wintrust ws2_32 wtsapi32
windowscodecs uxtheme usp10 dwmapi win32u dwrite mfplat mfreadwrite
mf mfcore combase cryptbase sechost shcore wldp ntasn1 rpcrt4
'''.split())
IMPORT_PATTERN = re.compile(r'^\s*DLL Name:\s*(\S+)', re.MULTILINE | re.IGNORECASE)
DLL_SAFE = re.compile(r'^[A-Za-z0-9_.+-]+\.dll$', re.I)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _names(text: str) -> set[str]:
    names = {m.lower() for m in IMPORT_PATTERN.findall(text)}
    if any(not DLL_SAFE.fullmatch(name) for name in names):
        raise RuntimeError('PE binary has an invalid imported DLL name')
    return names


def _os_provided(name: str) -> bool:
    stem = name.removesuffix('.dll')
    return (stem in SYSTEM_DLLS or stem.startswith(('api-ms-win-', 'ext-ms-')))


def _verify_pe(path: Path) -> None:
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise RuntimeError(f'Windows imported dependency is not a PE file: {path}')


def stage_cross(builds: Path, cross, output: Path, binaries: tuple[Path, ...]) -> dict[str, str]:
    if not output.resolve().is_relative_to(builds.resolve()):
        raise RuntimeError(f'Windows DLL closure destination escapes Builds: {output}')
    queue = [cross.path(path) for path in binaries]
    copied: dict[str, str] = {}
    visited: set[str] = set()
    while queue:
        binary = queue.pop(0)
        if binary in visited:
            continue
        visited.add(binary)
        proc = cross.execute(['x86_64-w64-mingw32-objdump', '-p', binary],
                             cwd=output, timeout=120)
        if proc.returncode:
            raise RuntimeError(f'Cannot inspect PE imports for {binary}: {proc.stdout[-500:]}')
        for name in sorted(_names(proc.stdout)):
            if _os_provided(name) or name in copied:
                continue
            # Toolchain compiler owns provenance. If it cannot resolve a DLL,
            # look in the toolchain's standard redistributable directories.
            lookup = cross.execute(['x86_64-w64-mingw32-g++', '-print-file-name=' + name],
                                   cwd=output, timeout=60)
            source = lookup.stdout.strip().splitlines()[-1] if lookup.stdout.strip() else ''
            if lookup.returncode or not source.startswith('/'):
                find = cross.execute(['find', '/usr/lib/gcc', '/usr/x86_64-w64-mingw32',
                                      '-iname', name, '-type', 'f', '-print', '-quit'],
                                     cwd=output, timeout=120)
                source = find.stdout.strip().splitlines()[0] if find.stdout.strip() else ''
            if not source.startswith('/'):
                raise RuntimeError(f'Unresolved non-system Windows DLL: {name} imported by {binary}')
            target = output / name
            if not target.is_file():
                command = cross.execute(['cp', source, cross.path(target)],
                                        cwd=output, timeout=120)
                if command.returncode:
                    raise RuntimeError(f'Failed to copy Windows DLL {name}: {command.stdout[-350:]}')
            _verify_pe(target)
            copied[name] = _sha(target)
            queue.append(cross.path(target))
    print(f'[WINDOWS CROSS] recursive PE closure resolved {len(copied)} non-system DLLs', flush=True)
    return copied


def stage_native(builds: Path, msys2: Path, output: Path) -> dict[str, str]:
    if not output.resolve().is_relative_to(builds.resolve()):
        raise RuntimeError(f'Windows DLL closure destination escapes Builds: {output}')
    objdump = msys2 / 'ucrt64/bin/objdump.exe'
    if not objdump.is_file():
        objdump = msys2 / 'usr/bin/objdump.exe'
    if not objdump.is_file():
        raise RuntimeError('Complete Windows UCRT64 Binutils objdump.exe is missing')
    queue = list(output.glob('*.exe'))
    if not queue:
        raise RuntimeError('No Windows PE executables to analyze for runtime closure')
    visited = set()
    copied: dict[str, str] = {}
    while queue:
        binary = queue.pop(0)
        if binary.resolve() in visited:
            continue
        visited.add(binary.resolve())
        process = subprocess.run([str(objdump), '-p', str(binary)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, timeout=120)
        if process.returncode:
            raise RuntimeError(f'Cannot inspect Windows native imports of {binary}: '
                               + process.stdout[-400:])
        for name in sorted(_names(process.stdout)):
            if _os_provided(name) or name in copied:
                continue
            candidates = [p for base in ('ucrt64/bin', 'usr/bin')
                          for p in (msys2 / base).glob('*')
                          if p.name.lower() == name]
            if not candidates:
                raise RuntimeError(f'Unresolved Windows UCRT64 DLL {name} imported by {binary}')
            source = candidates[0]
            _verify_pe(source)
            target = output / name
            if not target.is_file() or _sha(target) != _sha(source):
                shutil.copy2(source, target)
            copied[name] = _sha(target)
            queue.append(target)
    print(f'[WINDOWS NATIVE] recursive PE closure resolved {len(copied)} non-system DLLs', flush=True)
    return copied
