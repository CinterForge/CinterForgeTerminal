"""Native Windows Setup -> Build -> Test for the shared upstream WebView sample.

This runner MUST execute on Windows. Wine and a Mac-hosted cross-compile are
separate evidence, and cannot create a native Windows acceptance receipt.
Uses the verified MSYS2 UCRT64 closure, CMake/Ninja, and one authored C++ source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

SCHEMA = 'cinterforge.terminal.windows-native-webview-acceptance.v1'


def _run(command: list[str], *, cwd: Path, env: dict[str, str], log: Path,
         timeout: int = 900) -> str:
    print('[WINDOWS NATIVE] ' + ' '.join(map(str, command)), flush=True)
    try:
        proc = subprocess.run([str(s) for s in command], cwd=cwd, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors='replace', timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        with log.open('a', encoding='utf-8') as out:
            out.write(f'TIMEOUT {timeout}s: {command}\n')
        raise RuntimeError(f'Windows native command timed out: {command[0]}') from exc
    with log.open('a', encoding='utf-8') as out:
        out.write('$ ' + ' '.join(map(str, command)) + '\n' + proc.stdout +
                  f'\nEXIT={proc.returncode}\n')
    print(proc.stdout[-3000:], flush=True)
    if proc.returncode:
        raise RuntimeError(f'Windows native command failed rc={proc.returncode}: {command[0]}; see {log}')
    return proc.stdout


def run(builds: Path, *, repository: Path | None = None) -> dict:
    if platform.system() != 'Windows':
        raise RuntimeError('Windows native acceptance requires Windows, not Wine or Linux')
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('Windows acceptance requires the sibling Builds directory')
    repo = (repository or Path(__file__).resolve().parents[1]).resolve()
    from host.toolchain import stage_windows_msys2, msys2_env
    from host.webview import provision, provision_webview2
    from host.windows_webview2_runtime import ensure as ensure_webview2_runtime
    root = stage_windows_msys2(builds)
    env = msys2_env(root)
    cmake = root / 'ucrt64/bin/cmake.exe'
    ninja = root / 'ucrt64/bin/ninja.exe'
    compiler = root / 'ucrt64/bin/g++.exe'
    bash = root / 'usr/bin/bash.exe'
    for binary in (cmake, ninja, compiler, bash):
        if not binary.is_file():
            raise RuntimeError(f'Staged Windows UCRT64 dependency missing: {binary}')
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / 'windows-native-webview.log'
    log.write_text('Native Windows CinterForgeTerminal WebView acceptance\n', encoding='utf-8')
    receipt = logs / 'windows-native-webview.json'
    report = {'schema': SCHEMA, 'platform': 'win', 'execution': 'native-windows',
              'status': 'failed', 'toolchain': str(root)}
    try:
        for binary in (bash, compiler, cmake, ninja):
            _run([str(binary), '--version'], cwd=repo, env=env, log=log, timeout=30)
        runtime = ensure_webview2_runtime(builds)
        report['webview2_runtime_version'] = runtime['version']
        env['WEBVIEW2_BROWSER_EXECUTABLE_FOLDER'] = runtime['runtime_path']
        source = provision(builds)
        sdk = provision_webview2(builds)
        out = builds / 'Intermediates/CinterForgeTerminal/WindowsNative/x64/cmake'
        out.mkdir(parents=True, exist_ok=True)
        _run([str(cmake), '-S', str(repo), '-B', str(out), '-G', 'Ninja',
              '-DCMAKE_BUILD_TYPE=Release', '-DFETCHCONTENT_FULLY_DISCONNECTED=ON',
              '-DCPH_BUILD_APPLICATION_SHELL=ON', '-DCPH_BUILDS_ROOT=' + str(builds),
              '-DCPH_WEBVIEW_SOURCE_DIR=' + str(source),
              '-DMSWebView2_ROOT=' + str(sdk),
              '-DCMAKE_C_COMPILER=' + str(root / 'ucrt64/bin/gcc.exe'),
              '-DCMAKE_CXX_COMPILER=' + str(compiler),
              '-DCMAKE_MAKE_PROGRAM=' + str(ninja)],
             cwd=repo, env=env, log=log)
        _run([str(cmake), '--build', str(out), '--target', 'cinter-bootstrap-sample',
              'cinter-terminal', '--parallel', '4'], cwd=repo, env=env, log=log)
        sample = out / 'CinterBootstrapSample.exe'
        terminal = out / 'CinterForgeTerminal.exe'
        for exe in (sample, terminal):
            if not exe.is_file() or exe.read_bytes()[:2] != b'MZ':
                raise RuntimeError(f'Windows PE missing or invalid: {exe}')
        from host.windows_runtime_dlls import stage_native
        report['runtime_dlls'] = stage_native(builds, root, out)
        work = builds / 'Run/CinterForgeTerminal/win/working-directory'
        work.mkdir(parents=True, exist_ok=True)
        (work / 'windows-native-marker.txt').write_text('Native Windows test\n')
        probe = _run([str(sample), '--probe'], cwd=work, env=env, log=log, timeout=90)
        for marker in ('CINTER_SAMPLE_BEGIN platform=windows',
                       'CINTER_SAMPLE_DIRECTORY_PASS platform=windows',
                       'windows-native-marker.txt'):
            if marker not in probe:
                raise RuntimeError(f'Windows sample directory evidence missing: {marker}')
        js = _run([str(sample), '--smoke-js'], cwd=work, env=env, log=log, timeout=90)
        if 'CINTER_SAMPLE_WEBVIEW_JS_PASS ' not in js:
            raise RuntimeError('Native Windows WebView2 JS bridge never reported PASS')
        terminal_js = _run([str(terminal), '--smoke-js'], cwd=work, env=env, log=log, timeout=90)
        if 'WEBVIEW_JS_PASS result=4 callback=native' not in terminal_js:
            raise RuntimeError('Native Windows Terminal JS bridge never reported PASS')
        report.update(status='passed', sample=str(sample), terminal=str(terminal),
                      terminal_sha256=hashlib.sha256(terminal.read_bytes()).hexdigest(),
                      directory_listing='passed', webview_js='passed')
    except Exception as exc:
        report['error'] = str(exc)
        raise
    finally:
        report['completed_utc'] = time.time()
        receipt.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    run(parser.parse_args().builds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
