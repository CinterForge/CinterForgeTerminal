"""Mac orchestrator: build, run and log all THREE real native WebView samples.

One build/terminal_bootstrap/main.cpp is compiled as a Mac executable,
Linux ELF and Windows PE. Mac and Linux require a live WebView JS callback;
Windows must execute its directory probe under real Wine. Native Windows
WebView2 acceptance remains distinct from Wine compatibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import threading
import signal

SCHEMA = 'cinterforge.terminal.three-samples.v1'
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
LINUX_DOCKERFILE = HERE / 'docker/linux-sample.Dockerfile'
LINUX_NAME = 'cinterforge-terminal-linux-sample'


def _exec(command: list[str], *, cwd: Path, logfile: Path, env=None,
          timeout: int = 1800) -> str:
    """Stream evidence as it arrives, but never hang indefinitely on a WebView.

    A blocking readline() in the main thread defeats subprocess timeouts when
    the child creates a window but never emits a newline. A reader thread keeps
    progress visible while the controlling thread enforces the deadline.
    """
    logfile.parent.mkdir(parents=True, exist_ok=True)
    print('[THREE SAMPLES] $ ' + ' '.join(map(str, command)), flush=True)
    output: list[str] = []
    with logfile.open('a', encoding='utf-8') as transcript:
        transcript.write('$ ' + ' '.join(map(str, command)) + '\n')
        transcript.flush()
        process = subprocess.Popen([str(c) for c in command], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            start_new_session=(os.name == 'posix'))
        assert process.stdout

        def read_output() -> None:
            for line in process.stdout:
                print(line, end='', flush=True)
                transcript.write(line)
                transcript.flush()
                output.append(line)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            rc = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # A native WebView may hang with no console output; terminate its
            # complete process group rather than leaving a stray GUI behind.
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
            reader.join(timeout=3)
            transcript.write(f'TIMEOUT={timeout}s\n')
            transcript.flush()
            raise RuntimeError(f'Three-platform command timed out after {timeout}s: '
                               f'{command[0]}; see {logfile}') from exc
        reader.join(timeout=3)
        transcript.write(f'EXIT={rc}\n')
        transcript.flush()
    if rc:
        raise RuntimeError(f'Three-platform command failed (rc={rc}); see {logfile}')
    return ''.join(output)


def _assert_probe(output: str, target: str) -> None:
    begin = f'CINTER_SAMPLE_BEGIN platform={target}'
    end = f'CINTER_SAMPLE_DIRECTORY_PASS platform={target}'
    if not all(token in output for token in (begin, 'CINTER_SAMPLE_CWD ', 'CINTER_SAMPLE_DIR ', end)):
        raise RuntimeError(f'{target} sample did not execute its own directory-listing contract')


def _sha_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open('rb') as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b''):
            sha.update(block)
    return sha.hexdigest()


def _docker_image(builds: Path, docker: str, logfile: Path) -> str:
    digest = hashlib.sha256(LINUX_DOCKERFILE.read_bytes()).hexdigest()[:16]
    image = f'{LINUX_NAME}:{digest}'
    staged = builds / 'CinterForgeTerminal/Toolchains/LinuxSample'
    tar = staged / f'{digest}.tar'
    receipt = staged / f'{digest}.json'
    if tar.exists():
        if not receipt.is_file():
            raise RuntimeError('Linux sample image is missing its integrity receipt')
        cached = json.loads(receipt.read_text())
        if cached.get('dockerfile_sha256') != digest or cached.get('archive_sha256') != _sha_file(tar):
            raise RuntimeError('Linux sample image archive/recipe integrity mismatch')
    exists = subprocess.run([docker, 'image', 'inspect', image], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        if tar.is_file() and receipt.is_file():
            data = json.loads(receipt.read_text())
            if data.get('archive_sha256') != _sha_file(tar):
                raise RuntimeError('Cached Linux sample image was modified')
            _exec([docker, 'load', '--input', str(tar)], cwd=REPO, logfile=logfile)
        else:
            _exec([docker, 'build', '--platform', 'linux/amd64', '--progress', 'plain',
                   '-t', image, '-f', str(LINUX_DOCKERFILE), str(LINUX_DOCKERFILE.parent)],
                  cwd=REPO, logfile=logfile)
    if subprocess.run([docker, 'image', 'inspect', image], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError('Linux sample Docker image unavailable')
    if not tar.is_file():
        staged.mkdir(parents=True, exist_ok=True)
        partial = tar.with_suffix('.tar.part')
        try:
            _exec([docker, 'save', '-o', str(partial), image], cwd=REPO, logfile=logfile)
            partial.replace(tar)
            receipt.write_text(json.dumps({'dockerfile_sha256': digest,
                'archive_sha256': _sha_file(tar),
                'image': image}, indent=2) + '\n')
        finally:
            partial.unlink(missing_ok=True)
    return image


def _container_exec(builds: Path, docker: str, image: str, command: list[str],
                    logfile: Path, *, working: str = '/source') -> str:
    return _exec([docker, 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
                  '--mount', f'type=bind,src={REPO},dst=/source,readonly',
                  '--mount', f'type=bind,src={builds},dst=/builds',
                  '--workdir', working, image, *command], cwd=REPO, logfile=logfile)


def run(builds: Path, *, docker: str | None = None) -> dict:
    if platform.system() != 'Darwin':
        raise RuntimeError('Three-platform bootstrap is Mac-only; native Windows/Linux setup is separate')
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('Three-platform samples must be staged in sibling Builds')
    from host.webview import provision, provision_webview2
    from host.windows_environment import WindowsCrossEnvironment
    from host.win_cross import smoke as wine_console_smoke
    webview = provision(builds)
    webview2 = provision_webview2(builds)
    logs = builds / 'Logs/CinterForgeTerminal'
    log = logs / 'three-platform-samples.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('CinterForgeTerminal three-platform sample acceptance\n')
    report = {'schema': SCHEMA, 'status': 'failed', 'targets': {},
              'source': str(REPO / 'build/terminal_bootstrap/main.cpp')}
    receipt = logs / 'three-platform-samples.json'
    try:
        # Native Mac: Xcode + staged CMake/Ninja, WebKit via upstream webview.
        mac_build = builds / 'Intermediates/CinterForgeTerminal/Samples/mac'
        _exec(['cmake', '-S', str(REPO), '-B', str(mac_build), '-G', 'Ninja',
               '-DCMAKE_BUILD_TYPE=Release', '-DCPH_BUILD_APPLICATION_SHELL=ON',
               '-DCPH_WEBVIEW_SOURCE_DIR=' + str(webview),
               '-DCPH_BUILDS_ROOT=' + str(builds)], cwd=REPO, logfile=log)
        _exec(['cmake', '--build', str(mac_build), '--target', 'cinter-bootstrap-sample'],
              cwd=REPO, logfile=log)
        mac_exe = mac_build / 'CinterBootstrapSample'
        output = _exec([str(mac_exe), '--probe'], cwd=mac_build, logfile=log)
        _assert_probe(output, 'macos')
        js = _exec([str(mac_exe), '--smoke-js'], cwd=mac_build, logfile=log, timeout=90)
        if 'CINTER_SAMPLE_WEBVIEW_JS_PASS ' not in js:
            raise RuntimeError('macOS WebView JavaScript callback missing')
        report['targets']['mac'] = {'status': 'passed', 'execution': 'native-macos',
                                    'executable': str(mac_exe), 'webview_js': 'passed'}
        receipt.write_text(json.dumps(report, indent=2) + '\n')

        # Linux x64: the identical C++/WebView source, compiled *inside* Linux.
        if not docker:
            from host.mac_bootstrap import ensure_docker
            docker = ensure_docker(builds)
        linux_image = _docker_image(builds, docker, log)
        linux_build = builds / 'Intermediates/CinterForgeTerminal/Samples/linux-x64'
        linux_build.mkdir(parents=True, exist_ok=True)
        _container_exec(builds, docker, linux_image, [
            'cmake', '-S', '/source', '-B', '/builds/Intermediates/CinterForgeTerminal/Samples/linux-x64',
            '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCPH_BUILD_APPLICATION_SHELL=ON',
            '-DCPH_WEBVIEW_SOURCE_DIR=/builds/' + webview.relative_to(builds).as_posix(),
            '-DCPH_BUILDS_ROOT=/builds'], log)
        _container_exec(builds, docker, linux_image, ['cmake', '--build',
            '/builds/Intermediates/CinterForgeTerminal/Samples/linux-x64',
            '--target', 'cinter-bootstrap-sample'], log)
        elf = linux_build / 'CinterBootstrapSample'
        if not elf.is_file() or elf.read_bytes()[:4] != b'\x7fELF':
            raise RuntimeError('Linux sample did not produce an ELF binary')
        linux_bin = '/builds/Intermediates/CinterForgeTerminal/Samples/linux-x64/CinterBootstrapSample'
        linux_probe = _container_exec(builds, docker, linux_image,
            [linux_bin, '--probe'], log, working='/builds/Intermediates/CinterForgeTerminal/Samples/linux-x64')
        _assert_probe(linux_probe, 'linux')
        linux_js = _container_exec(builds, docker, linux_image,
            ['xvfb-run', '-a', linux_bin, '--smoke-js'], log)
        if 'CINTER_SAMPLE_WEBVIEW_JS_PASS ' not in linux_js:
            raise RuntimeError('Linux WebKitGTK JavaScript callback missing')
        report['targets']['linux'] = {'status': 'passed', 'execution': 'linux-amd64-docker-xvfb',
                                      'executable': str(elf), 'webview_js': 'passed',
                                      'image': linux_image}
        receipt.write_text(json.dumps(report, indent=2) + '\n')

        # Windows x64: same upstream WebView CMake and authored C++ source.
        # Wine is a compatibility smoke; it is *not* native Windows WebView2.
        cross = WindowsCrossEnvironment(builds).ensure()
        wine_console_smoke(builds, executor=cross)
        from host.webview import upstream_windows_toolchain
        from host.cross_build import _prepare_cross_cache
        win_build = builds / 'Intermediates/CinterForgeTerminal/Samples/win-x64'
        win_toolchain = webview / 'cmake/toolchains/x86_64-w64-mingw32.cmake'
        if cross.provider == 'host':
            win_toolchain = upstream_windows_toolchain(builds, webview)
        _prepare_cross_cache(builds, win_build, Path(cross.path(win_toolchain)),
                             source_root=cross.path(REPO))
        win_args = ['cmake', '-S', cross.path(REPO), '-B', cross.path(win_build),
            '-G', 'Ninja', '-DCMAKE_TOOLCHAIN_FILE=' + cross.path(win_toolchain),
            '-DWEBVIEW_TOOLCHAIN_EXECUTABLE_SUFFIX=-posix',
            '-DCMAKE_BUILD_TYPE=Release', '-DFETCHCONTENT_FULLY_DISCONNECTED=ON',
            '-DMSWebView2_ROOT=' + cross.path(webview2),
            '-DCPH_BUILD_APPLICATION_SHELL=ON',
            '-DCPH_WEBVIEW_SOURCE_DIR=' + cross.path(webview),
            '-DCPH_BUILDS_ROOT=' + cross.path(builds)]
        for command in (win_args, ['cmake', '--build', cross.path(win_build),
                                   '--target', 'cinter-bootstrap-sample']):
            print('[THREE SAMPLES] Windows cross: ' + ' '.join(command), flush=True)
            execution = cross.execute(command, cwd=REPO, timeout=1800)
            with log.open('a') as transcript:
                transcript.write('$ ' + ' '.join(command) + '\n' + execution.stdout + '\n')
            if execution.returncode:
                raise RuntimeError(f'Windows sample build failed, rc={execution.returncode}; see {log}')
        pe = win_build / 'CinterBootstrapSample.exe'
        if not pe.is_file() or pe.read_bytes()[:2] != b'MZ':
            raise RuntimeError('Windows sample did not produce a PE executable')
        from host.windows_runtime_dlls import stage_cross
        report['windows_runtime_dlls'] = stage_cross(builds, cross, win_build, (pe,))
        execution = cross.execute(['wine', cross.path(pe), '--probe'], cwd=win_build, timeout=240)
        with log.open('a') as transcript:
            transcript.write('$ wine ' + str(pe) + ' --probe\n' + execution.stdout + '\n')
        if execution.returncode:
            raise RuntimeError('Windows sample failed under Wine: ' + execution.stdout[-500:])
        _assert_probe(execution.stdout, 'windows')
        # Wine verifies PE execution and runtime dependencies only. Microsoft's
        # WebView2 browser is not a reliable Wine/Xvfb acceptance target and can
        # hang before the JS callback. Native Windows owns the graphical gate.
        # Do not record this compatibility probe as a WebView2 JS pass.
        report['targets']['win'] = {'status': 'passed', 'execution': 'wine-windows-pe-not-native-windows',
                                    'executable': str(pe), 'webview_js': 'not-run-native-windows-required'}
        report['status'] = 'passed-with-native-windows-webview-pending'
        report['completed_utc'] = time.time()
        receipt.write_text(json.dumps(report, indent=2) + '\n')
        return report
    except Exception as exc:
        report['error'] = str(exc)
        report['completed_utc'] = time.time()
        receipt.write_text(json.dumps(report, indent=2) + '\n')
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    args = parser.parse_args()
    run(args.builds.resolve())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
