"""Stage a real Linux/x64 target runner from the accepted Linux sample image.

The runner invokes the *actual* target-native bootstrap code inside the same
Linux environment used to compile the WebKitGTK sample. It never creates an
acceptance receipt itself: only bootstrap_bundle.setup_local may do so after
native ELF/WebView/Python tests. A missing image or source is a hard failure.
"""
from __future__ import annotations

import hashlib
from collections import deque
import os
import signal
import threading
import json
from pathlib import Path
import shlex
import shutil
import subprocess

from host.three_samples import _docker_image, LINUX_DOCKERFILE, linux_recipe_sha256
from host.portable_python import PACKAGES, VERSION, UPSTREAM, _download_verified


def stage(builds: Path, *, docker: str, logfile: Path) -> Path:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('Linux runner staging requires sibling Builds')
    if not Path(docker).is_file() and not shutil.which(docker):
        raise RuntimeError(f'Linux runner Docker executable missing: {docker}')
    # The target guest is intentionally network-isolated; provision its
    # pinned interpreter archive from Mac before entering Linux.
    triplet, expected = PACKAGES[('linux', 'x64')]
    filename = f'cpython-{VERSION}+{UPSTREAM}-{triplet}-install_only.tar.gz'
    cache = builds / 'Intermediates/CinterForgeTerminal/PythonPackages' / filename
    _download_verified(cache, filename, expected)
    image = _docker_image(builds, docker, logfile)
    root = builds / 'CinterForgeTerminal/Toolchains/TargetRunners/linux'
    root.mkdir(parents=True, exist_ok=True)
    executable = root / 'run'
    recipe_hash = linux_recipe_sha256()
    from host.amd64_backend import docker_bind_source
    guest_builds = docker_bind_source(builds)
    script = f'''#!/bin/sh
set -eu
# Actual Linux/x64 process, not a host-generated PASS receipt.
# The source runner and all artifacts reside in the mounted Builds tree.
BUILDS={shlex.quote(str(builds))}
DOCKER={shlex.quote(str(docker))}
IMAGE={shlex.quote(image)}
BOOTSTRAP=${{1:?Bootstrap source runner required}}
shift
if [ "$#" -ne 3 ] || [ "$1" != "--builds" ] || [ "$2" != "$BUILDS" ] || [ "$3" != "--setup-local" ]; then
  echo "Linux runner requires the exact staged bootstrap lifecycle arguments" >&2
  exit 2
fi
case "$BOOTSTRAP" in
  "$BUILDS"/*) ;;
  *) echo "Refusing bootstrap outside Builds: $BOOTSTRAP" >&2; exit 2 ;;
esac
GUEST_SCRIPT=/builds/${{BOOTSTRAP#"$BUILDS"/}}
exec "$DOCKER" run --rm --platform linux/amd64 --network none \\
    --mount "type=bind,src={guest_builds},dst=/builds" \\
    --workdir /builds --env HOME=/builds/Run/CinterForgeTerminal/LinuxRunner/Home \\
    --env WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1 \\
    --env LIBGL_ALWAYS_SOFTWARE=1 \\
    "$IMAGE" xvfb-run -a python3 -B "$GUEST_SCRIPT" --builds /builds --setup-local
'''
    receipt = {'schema': 'cinterforge.terminal.linux-runner.v1',
               'image': image, 'dockerfile_sha256': recipe_hash,
               'script_sha256': hashlib.sha256(script.encode()).hexdigest()}
    if executable.exists() and executable.read_text() != script:
        raise RuntimeError(f'Existing Linux runner differs: {executable}')
    executable.write_text(script)
    executable.chmod(0o755)
    (root / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(f'[LINUX RUNNER] staged native Linux/x64 target: {executable}', flush=True)
    return executable



def build_terminal(builds: Path, *, docker: str, logfile: Path) -> Path:
    """Cross-build the real Linux/x64 ELF and expose the failing compiler output.

    The Linux OCI recipe and pinned WebView source define the toolchain. Never
    reuse a CMake cache produced by a different image or source closure; only
    the *affected* LinuxNative intermediate build tree may be discarded.
    """
    from host.webview import provision
    repo = Path(__file__).resolve().parents[1]
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('Linux build requires the existing sibling Builds directory')
    webview = provision(builds)
    image = _docker_image(builds, docker, logfile)
    out = builds / 'Intermediates/CinterForgeTerminal/LinuxNative/x64/cmake'
    signature = {
        'schema': 'cinterforge.terminal.linux-native-build-inputs.v1',
        'image': image,
        'linux_recipe_sha256': linux_recipe_sha256(),
        'cmake_source_sha256': hashlib.sha256((repo / 'CMakeLists.txt').read_bytes()).hexdigest(),
        'webview': webview.relative_to(builds).as_posix(),
    }
    stamp = out / 'linux-native-inputs.json'
    if out.is_symlink():
        raise RuntimeError('Refusing symlinked Linux native CMake output: ' + str(out))
    if out.exists():
        try:
            same = json.loads(stamp.read_text()) == signature
        except (FileNotFoundError, ValueError):
            same = False
        if not same:
            print('[LINUX NATIVE] Toolchain or CMake inputs changed; resetting only ' + str(out), flush=True)
            shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    args = ['cmake', '-S', '/source', '-B', '/builds/Intermediates/CinterForgeTerminal/LinuxNative/x64/cmake',
            '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_SUPPRESS_REGENERATION=ON',
            '-DCPH_BUILD_APPLICATION_SHELL=ON',
            '-DCPH_ENABLE_ZEROCONF=ON', '-DCPH_BUILDS_ROOT=/builds',
            '-DFETCHCONTENT_FULLY_DISCONNECTED=ON',
            '-DCPH_WEBVIEW_SOURCE_DIR=/builds/' + webview.relative_to(builds).as_posix()]
    # macOS bind mounts can expose source timestamps newer than Docker-generated
    # build.ninja, repeatedly triggering CMake until Ninja aborts (100 tries).
    # We configure explicitly above and invalidate this CMake tree when pinned
    # build inputs change, so auto-regeneration is unnecessary for this runner.
    from host.amd64_backend import docker_bind_source
    logfile.parent.mkdir(parents=True, exist_ok=True)
    for command in (args, ['cmake', '--build', '/builds/Intermediates/CinterForgeTerminal/LinuxNative/x64/cmake',
                            '--target', 'cinter-terminal', '--parallel', '4']):
        print('[LINUX NATIVE] $ ' + ' '.join(command), flush=True)
        docker_command = [docker, 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
                          '--mount', f'type=bind,src={docker_bind_source(repo)},dst=/source,readonly',
                          '--mount', f'type=bind,src={docker_bind_source(builds)},dst=/builds',
                          '--workdir', '/source', image, *command]
        from host.setup_progress import progress_step
        tail_lines = deque(maxlen=80)
        with logfile.open('a', encoding='utf-8') as output:
            output.write('$ ' + ' '.join(docker_command) + '\n')
            output.flush()
            with progress_step('real Linux AMD64 CMake/ELF build',
                               prefix='[LINUX NATIVE]', interval_seconds=10):
                process = subprocess.Popen(docker_command, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True,
                                           bufsize=1, start_new_session=(os.name == 'posix'))
                assert process.stdout is not None
                def relay():
                    for line in process.stdout:
                        print(line, end='', flush=True)
                        output.write(line)
                        output.flush()
                        tail_lines.append(line)
                reader = threading.Thread(target=relay, daemon=True)
                reader.start()
                try:
                    rc = process.wait(timeout=1800)
                except subprocess.TimeoutExpired:
                    if os.name == 'posix':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait(timeout=10)
                    rc = -1
                finally:
                    reader.join(timeout=5)
                    process.stdout.close()
                output.write('EXIT=' + str(rc) + '\n')
                output.flush()
        if rc != 0:
            tail = ''.join(tail_lines)
            print('[LINUX NATIVE] failed (' + str(rc) + '):\n' + tail +
                  '\n[LINUX NATIVE] complete log: ' + str(logfile), flush=True)
            raise RuntimeError('Linux native Terminal build failed (' + str(rc) +
                               '); see ' + str(logfile))
        print('[LINUX NATIVE] command completed', flush=True)
    executable = out / 'CinterForgeTerminal'
    if not executable.is_file():
        raise RuntimeError('Linux builder did not emit real ELF: ' + str(executable))
    with executable.open('rb') as native:
        if native.read(4) != b'\x7fELF':
            raise RuntimeError('Linux builder did not emit real ELF: ' + str(executable))
    stamp.write_text(json.dumps(signature, indent=2, sort_keys=True) + '\n')
    print('[LINUX NATIVE] built real ELF: ' + str(executable), flush=True)
    return executable

def main() -> int:
    import argparse
    from host.windows_environment import WindowsCrossEnvironment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    args = parser.parse_args()
    builds = args.builds.resolve()
    stage(builds, docker=WindowsCrossEnvironment(builds)._docker_available(),
          logfile=builds / 'Logs/CinterForgeTerminal/linux-runner.log')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
