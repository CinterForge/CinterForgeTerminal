"""Linux- or Mac-hosted Windows cross-build using upstream webview/webview CMake.

Preinstalled MinGW-w64, CMake and Ninja are reused. The pinned upstream
WebView MinGW toolchain and WebView2 SDK headers are consumed directly from
Builds, so the native and cross-target applications share the same C++ shell
and JS binding code. Wine CLI testing is not native WebView2 acceptance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import argparse
import platform
import shutil
import subprocess
import time
import traceback


def _prepare_cross_cache(builds: Path, output: Path, toolchain: Path,
                         *, source_root: str | None = None) -> None:
    """Invalidate only an old Windows CMake tree when its compiler toolchain changed."""
    if not output.resolve().is_relative_to(builds.resolve()) or output.is_symlink():
        raise ValueError(f"Unsafe Windows CMake build directory: {output}")
    cache = output / "CMakeCache.txt"
    if cache.is_file():
        entries = {}
        for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("CMAKE_TOOLCHAIN_FILE:", "CMAKE_HOME_DIRECTORY:")):
                name, _, value = line.partition("=")
                entries[name.partition(":")[0]] = value
        repo = Path(__file__).resolve().parents[1]
        if (entries.get("CMAKE_TOOLCHAIN_FILE") != str(toolchain) or
                entries.get("CMAKE_HOME_DIRECTORY") not in (None, source_root or str(repo))):
            print("[WINDOWS CROSS] upstream toolchain changed; resetting Windows CMake tree only", flush=True)
            shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)


def _run(command: list[str], cwd: Path, log, executor, *, graphical: bool = False) -> str:
    log.write('$ ' + ' '.join(command) + '\n')
    log.flush()
    print('[WINDOWS CROSS] ' + ' '.join(command), flush=True)
    finished = executor.execute(command, cwd=cwd, timeout=180 if graphical else 900, graphical=graphical)
    log.write(finished.stdout + f'\n(exit={finished.returncode})\n')
    log.flush()
    if finished.returncode:
        raise RuntimeError(f'Windows cross-build step exited {finished.returncode}; see {log.name}')
    return finished.stdout


def build(builds: Path) -> dict:
    if platform.system() not in ('Darwin', 'Linux'):
        raise RuntimeError('Windows cross-build requires a Linux or macOS host')
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError('Expected sibling Builds')
    from host.windows_environment import WindowsCrossEnvironment
    from host.win_cross import smoke
    from host.webview import (provision as provision_webview,
                              provision_webview2, upstream_windows_toolchain)
    repo = Path(__file__).resolve().parents[1]
    log = builds / 'Logs/CinterForgeTerminal/windows-cross-build.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    receipt = log.with_suffix('.json')
    report = {'schema': 'cinterforge.terminal.windows-cross-build.v1',
              'execution': 'cross-compile-wine-not-native-windows',
              'status': 'failed', 'wine_webview_js': 'not-run-native-windows-required', 'platform': 'win',
              'host': platform.system(), 'completed_utc': None}
    with log.open('w', encoding='utf-8') as transcript:
        try:
            cross = WindowsCrossEnvironment(builds).ensure()
            report.update(provider=cross.provider, cross_image=cross.image)
            # A successful image install is not compiler acceptance: run the tools.
            versions = cross.version_probes(repo)
            report['toolchain_versions'] = versions
            smoke(builds, executor=cross)
            report['wine_console_smoke'] = 'passed'
            webview = provision_webview(builds)
            sdk = provision_webview2(builds)
            official = webview / 'cmake/toolchains/x86_64-w64-mingw32.cmake'
            if cross.provider == 'host':
                toolchain = upstream_windows_toolchain(builds, webview)
            else:
                if not official.is_file():
                    raise RuntimeError(f'Official WebView MinGW toolchain missing: {official}')
                toolchain = official
            out = builds / 'Intermediates/CinterForgeTerminal/WindowsCross/x64/cmake'
            _prepare_cross_cache(builds, out, Path(cross.path(toolchain)),
                                 source_root=cross.path(repo))
            report.update(webview='upstream-webview/webview-0.12.0',
                          cmake_toolchain=str(toolchain), webview2_sdk=str(sdk))
            _run(['cmake', '-S', cross.path(repo), '-B', cross.path(out), '-G', 'Ninja',
                  '-DCMAKE_TOOLCHAIN_FILE=' + cross.path(toolchain),
                  '-DWEBVIEW_TOOLCHAIN_EXECUTABLE_SUFFIX=-posix',
                  # Each Setup explicitly reconfigures the cross-build. Disable Ninja's
                  # automatic CMake regeneration: host/VM mount timestamps can
                  # otherwise keep build.ninja dirty indefinitely.
                  '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_SUPPRESS_REGENERATION=ON',
                  '-DFETCHCONTENT_FULLY_DISCONNECTED=ON',
                  '-DMSWebView2_ROOT=' + cross.path(sdk),
                  '-DCPH_ENABLE_ZEROCONF=ON', '-DCPH_BUILD_APPLICATION_SHELL=ON',
                  '-DCPH_WEBVIEW_SOURCE_DIR=' + cross.path(webview),
                  '-DCPH_BUILDS_ROOT=' + cross.path(builds)], repo, transcript, cross)
            _run(['cmake', '--build', cross.path(out), '--target',
                  'cinter-bootstrap-sample', 'cinter-terminal', '--parallel', '4'],
                 repo, transcript, cross)
            sample = out / 'CinterBootstrapSample.exe'
            exe = out / 'CinterForgeTerminal.exe'
            for binary in (sample, exe):
                if not binary.is_file() or binary.read_bytes()[:2] != b'MZ':
                    raise RuntimeError(f'Windows CMake build did not produce the PE target: {binary}')
            report.update(sample=str(sample), output=str(exe))
            # The PE import table—not compiler success—determines which
            # MinGW runtime DLLs Windows and Wine need alongside the binaries.
            from host.windows_runtime_dlls import stage_cross
            report['runtime_dlls'] = stage_cross(builds, cross, out, (sample, exe))
            work = builds / 'Run/CinterForgeTerminal/WindowsCross/Acceptance'
            work.mkdir(parents=True, exist_ok=True)
            marker = work / 'windows-wine-acceptance-marker.txt'
            marker.write_text('Actual Windows sample launch under Wine\n', encoding='utf-8')
            # Both PE binaries have to be run, not merely --help or a C-only probe.
            listing = _run(['wine', cross.path(sample), '--probe'], work, transcript, cross)
            for required in ('CINTER_SAMPLE_BEGIN platform=windows',
                             'CINTER_SAMPLE_DIRECTORY_PASS platform=windows', marker.name):
                if required not in listing:
                    raise RuntimeError(f'Wine Windows sample directory evidence missing: {required}')
            report['wine_sample_directory'] = 'passed'
            # Wine is a Windows PE execution smoke, not a reliable host for
            # Microsoft's WebView2 browser. Validate the compiled Terminal CLI
            # here; the native Windows machine owns graphical callback testing.
            terminal_cli = _run(['wine', cross.path(exe), '--help'], work,
                                transcript, cross)
            if 'CinterForgeTerminal --python ' not in terminal_cli:
                raise RuntimeError('Windows Terminal CLI failed under Wine')
            report.update(wine_terminal_cli='passed',
                          wine_sample_webview_js='not-run-native-windows-required',
                          wine_terminal_webview_js='not-run-native-windows-required',
                          status='passed')
            return report
        except Exception as exc:
            report['error'] = str(exc)
            raise
        finally:
            report['completed_utc'] = time.time()
            receipt.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                               encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.builds)
    except Exception as exc:
        # Failed provisioning is a real cross-build result, not an absent receipt.
        # Keep the failure under the same Builds/Logs root as successful runs.
        logs = args.builds.resolve() / 'Logs/CinterForgeTerminal'
        logs.mkdir(parents=True, exist_ok=True)
        trace = traceback.format_exc()
        with (logs / 'windows-cross-build.log').open('a', encoding='utf-8') as transcript:
            transcript.write('WINDOWS CROSS FAILURE\n' + trace + '\n')
        # build() already writes the complete stage-by-stage failure receipt.
        # If build() itself was mocked or failed before creating it, write a fallback.
        receipt = logs / 'windows-cross-build.json'
        if not receipt.is_file():
            receipt.write_text(json.dumps({
                'schema': 'cinterforge.terminal.windows-cross-build.v1',
                'status': 'failed', 'host': platform.system(),
                'error': str(exc), 'completed_utc': time.time(),
                'wine_webview_js': 'not-passed',
            }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        raise
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
