"""Stage a real Linux/x64 target runner from the accepted Linux sample image.

The runner invokes the *actual* target-native bootstrap code inside the same
Linux environment used to compile the WebKitGTK sample. It never creates an
acceptance receipt itself: only bootstrap_bundle.setup_local may do so after
native ELF/WebView/Python tests. A missing image or source is a hard failure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess

from host.three_samples import _docker_image, LINUX_DOCKERFILE
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
    recipe_hash = hashlib.sha256(LINUX_DOCKERFILE.read_bytes()).hexdigest()
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
    --mount "type=bind,src=$BUILDS,dst=/builds" \\
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
        'linux_recipe_sha256': hashlib.sha256(LINUX_DOCKERFILE.read_bytes()).hexdigest(),
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
    logfile.parent.mkdir(parents=True, exist_ok=True)
    for command in (args, ['cmake', '--build', '/builds/Intermediates/CinterForgeTerminal/LinuxNative/x64/cmake',
                            '--target', 'cinter-terminal', '--parallel', '4']):
        print('[LINUX NATIVE] $ ' + ' '.join(command), flush=True)
        docker_command = [docker, 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
                          '--mount', f'type=bind,src={repo},dst=/source,readonly',
                          '--mount', f'type=bind,src={builds},dst=/builds',
                          '--workdir', '/source', image, *command]
        try:
            process = subprocess.run(docker_command, capture_output=True, text=True,
                                     timeout=1800, check=False)
            transcript = process.stdout + process.stderr
            rc = process.returncode
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or b''
            stderr = error.stderr or b''
            transcript = ((stdout.decode(errors='replace') if isinstance(stdout, bytes) else stdout) +
                          (stderr.decode(errors='replace') if isinstance(stderr, bytes) else stderr))
            rc = 'TIMEOUT after 1800s'
        with logfile.open('a', encoding='utf-8') as output:
            output.write('$ ' + ' '.join(command) + '\n' + transcript + '\nEXIT=' + str(rc) + '\n')
        if rc != 0:
            # The HOPE-DEV console previously hid this error completely. Print
            # the compiler's actual message; never label the build successful.
            tail = '\n'.join(transcript.splitlines()[-80:])
            print('[LINUX NATIVE] failed (' + str(rc) + '):\n' + tail +
                  '\n[LINUX NATIVE] complete log: ' + str(logfile), flush=True)
            raise RuntimeError('Linux native Terminal build failed (' + str(rc) + '); see ' + str(logfile))
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
    from host.mac_bootstrap import ensure_docker
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    args = parser.parse_args()
    builds = args.builds.resolve()
    stage(builds, docker=ensure_docker(builds),
          logfile=builds / 'Logs/CinterForgeTerminal/linux-runner.log')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
