"""CinterForgeTerminal application bootstrap: execute real smoke tests then seal a runtime bundle.

This is intentionally a gate, not an alternative Python/Bedrock installer. The
existing named Python environment, Bedrock manager, and versioned Host are used
as-is. All inputs and outputs live in sibling Builds; no automatic EULA consent
or redistribution of the official Bedrock server is performed.

On the macOS orchestrator, `setup all` invokes its native target and registered
Builds-resident Linux/Windows runners. Each runner executes this same module on
the target runtime. A missing runner, runtime, or failed smoke aborts the stage.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from host import HOST_RELEASE
from host.portable_python import _binary as _python_binary

TARGETS = ('mac', 'linux', 'win')
SCHEMA = 'cinterforge.terminal.bootstrap-bundle.v1'
SMOKE_SCHEMA = 'cinterforge.terminal.bootstrap-smoke.v1'
RUNNERS_SCHEMA = 'cinterforge.terminal.setup-runners.v1'
RELEASE = HOST_RELEASE
PYTHON_ENV = 'cinterterminalenv'
SERVER_MARKERS = ('Server started', 'CINTER_BDS_NETHERNET_PASS')


def _native() -> tuple[str, str]:
    system = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}.get(platform.system())
    if system is None:
        raise RuntimeError(f'Unsupported platform: {platform.system()}')
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if arch not in ('arm64', 'x64'):
        raise RuntimeError(f'Unsupported architecture: {platform.machine()}')
    return system, arch


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _require_builds(builds: Path) -> Path:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError(f'Expected the existing sibling Builds directory, got: {builds}')
    return builds


def _checked_tree(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise FileNotFoundError(f'Runtime component missing: {root}')
    files = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            # A copied application must be independent of its build machine.
            raise RuntimeError(f'Non-portable runtime symlink: {p}')
        if p.is_file():
            files[p.relative_to(root).as_posix()] = _sha(p)
    if not files:
        raise RuntimeError(f'Empty runtime component: {root}')
    return files


def _host_stage(builds: Path, system: str, arch: str) -> Path:
    root = builds / 'Python/Host' / system / arch / RELEASE
    receipt = root / 'manifest.json'
    if not receipt.is_file():
        raise RuntimeError(f'Versioned Python Host stage missing: {receipt}')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if (data.get('schema') != 'cinterforge.python-host.stage.v1' or
            data.get('platform') != system or data.get('arch') != arch):
        raise RuntimeError(f'Invalid Python Host stage: {receipt}')
    actual = _checked_tree(root)
    for rel, digest in data.get('files', {}).items():
        if actual.get(rel) != digest:
            raise RuntimeError(f'Host staging checksum mismatch: {rel}')
    if set(actual) != set(data['files']) | {'manifest.json'}:
        raise RuntimeError('Host staged file set differs from manifest')
    return root


def _powershell(builds: Path, system: str, arch: str) -> tuple[Path, Path]:
    root = builds / 'Toolchains/PowerShell' / system / arch
    exe = root / ('pwsh.exe' if system == 'win' else 'pwsh')
    if not exe.is_file():
        raise RuntimeError(f'Portable PowerShell missing: {exe}. Stage the complete pinned PowerShell archive under Builds.')
    _checked_tree(root)  # Require the complete local runtime closure, not an external symlink.
    return root, exe


def _portable_python(builds: Path, system: str, arch: str) -> Path:
    root = builds / 'Python/Environments' / system / arch / PYTHON_ENV
    if not (root / 'cinterforge-requirements.json').is_file():
        raise RuntimeError(f'Verified named Python environment missing: {root}')
    receipt = json.loads((root / 'cinterforge-requirements.json').read_text(encoding='utf-8'))
    if (receipt.get('schema') != 'cinterforge.python-host.environment.v1' or
            receipt.get('platform') != system or receipt.get('arch') != arch or
            receipt.get('name') != PYTHON_ENV):
        raise RuntimeError('Python environment receipt does not match the selected platform')
    if system == 'win':
        from host.windows_winpty import verify as verify_winpty
        verify_winpty(root)
    executable = _python_binary(root, system)
    if not executable.is_file() or not executable.resolve().is_relative_to(root.resolve()):
        raise RuntimeError(f'Embedded Python executable missing or nonlocal: {root}')
    _checked_tree(root)
    probe = ('import sys,sysconfig,json,os;'
             'print(json.dumps(dict(version=".".join(map(str,sys.version_info[:3])), '
             'executable=os.path.realpath(sys.executable),'
             'prefix=os.path.realpath(sys.prefix),'
             'base_prefix=os.path.realpath(sys.base_prefix),'
             'stdlib=os.path.realpath(sysconfig.get_path("stdlib")))))')
    result = subprocess.run([str(executable), '-B', '-I', '-c', probe],
                            capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError('Embedded Python failed portability smoke: ' + result.stderr[-500:])
    report = json.loads(result.stdout.strip())
    if report.get('version') != receipt['python'] or any(
        not Path(report[key]).resolve().is_relative_to(root.resolve())
        for key in ('executable', 'prefix', 'base_prefix', 'stdlib')):
        raise RuntimeError('Python runtime still depends on external build-machine paths')
    return root


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load staged service: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smoke_http(builds: Path, stage: Path, executable: Path) -> dict:
    """Exercise the actual staged server in a child of its *portable* Python.

    Merely importing it inside Setup's own Python would accept a broken or
    missing packaged runtime and produce an invalid positive receipt.
    """
    server_py = stage / 'webapp/CinterForgeTerminal.py'
    assets = stage / 'webapp'
    for name in ('CinterForgeTerminal.py', 'if_titles.json', 'mac_games.json', 'index.html',
                 'terminal.js', 'terminal.css', 'terminal_styles.json'):
        if not (assets / name).is_file():
            raise RuntimeError(f'HTTP service asset missing: {name}')
    if not executable.is_file():
        raise RuntimeError(f'Packaged Python executable missing: {executable}')
    run_dir = builds / 'Run/CinterForgeTerminal/BootstrapHttp'
    run_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='http-', dir=run_dir) as scratch:
        scratch = Path(scratch)
        ready = scratch / 'ready.json'
        log_path = builds / 'Logs/CinterForgeTerminal/setup-http.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                   HOME=str(scratch), TMPDIR=str(scratch), TMP=str(scratch),
                   TEMP=str(scratch), XDG_CACHE_HOME=str(scratch),
                   XDG_CONFIG_HOME=str(scratch), XDG_DATA_HOME=str(scratch))
        # Do not use the Setup interpreter or fallback to a system executable.
        command = [str(executable), '-B', '-I', str(server_py), '--builds', str(builds),
                   '--ready', str(ready)]
        with log_path.open('ab') as log:
            proc = subprocess.Popen(command, cwd=assets, env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while not ready.is_file():
                    if proc.poll() is not None:
                        raise RuntimeError(f'Staged Python HTTP service exited (rc={proc.returncode}); see {log_path}')
                    if time.monotonic() > deadline:
                        raise RuntimeError(f'Staged Python HTTP service did not become ready; see {log_path}')
                    time.sleep(.05)
                state = json.loads(ready.read_text(encoding='utf-8'))
                if state.get('schema') != 'cinterforge.terminal.http-ready.v1' or state.get('pid') != proc.pid:
                    raise RuntimeError('Staged Python HTTP service emitted invalid readiness')
                origin = state['origin']
                token = state['token']
                # Bound the request to the child process's loopback listener.
                from urllib.parse import urlparse
                parsed = urlparse(origin)
                if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
                        not parsed.port or parsed.path not in ('', '/')):
                    raise RuntimeError('Staged Python HTTP service is not loopback-only')
                with urlopen(origin + '/healthz', timeout=5) as reply:
                    if reply.status != 200 or json.loads(reply.read()) != {'ready': True}:
                        raise RuntimeError('CinterForgeTerminal Python HTTP health check failed')
                headers = {'Host': f'127.0.0.1:{parsed.port}', 'X-CPH-Token': token}
                with urlopen(Request(origin + '/api/status', headers=headers), timeout=5) as reply:
                    payload = json.loads(reply.read())
                    if reply.status != 200 or payload.get('platform') != _native()[0]:
                        raise RuntimeError('CinterForgeTerminal authenticated HTTP API failed')
                from urllib.error import HTTPError
                try:
                    urlopen(origin + '/api/status', timeout=5)
                except HTTPError as exc:
                    if exc.code != 403:
                        raise RuntimeError('Unauthenticated HTTP API did not reject access') from exc
                else:
                    raise RuntimeError('Unauthenticated HTTP API unexpectedly accepted access')
                with urlopen(origin + '/', timeout=5) as reply:
                    if reply.status != 200 or b'CinterForgeTerminal' not in reply.read():
                        raise RuntimeError('CinterForgeTerminal HTML not served by staged Python HTTP service')
                response = {'http_health': 'passed', 'authenticated_api': 'passed',
                            'unauthenticated_api_rejected': 'passed', 'html': 'passed',
                            'python_sha256': _sha(executable), 'server_sha256': _sha(server_py)}
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                ready.unlink(missing_ok=True)
            if proc.returncode != 0:
                raise RuntimeError(f'Staged Python HTTP service did not shut down cleanly (rc={proc.returncode})')
            return response


def _smoke_powershell(executable: Path, root: Path, builds: Path) -> dict:
    # The executable MUST be in the staged runtime; never fall back to system PATH.
    marker = 'CINTER_POWERSHELL_SETUP_PASS'
    code = (f'Write-Output "{marker}"; '
            '$PSVersionTable.PSVersion.ToString(); '
            'if ($PSVersionTable.PSVersion.Major -lt 7) { exit 23 }; exit 0')
    runtime = builds / 'Run/CinterForgeTerminal/PowerShellSmoke'
    runtime.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(runtime), TMPDIR=str(runtime), TEMP=str(runtime),
               XDG_CACHE_HOME=str(runtime), DOTNET_CLI_HOME=str(runtime),
               POWERSHELL_TELEMETRY_OPTOUT='1', PYTHONDONTWRITEBYTECODE='1')
    run = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', code],
                         cwd=root, env=env, capture_output=True, text=True, timeout=45, check=False)
    if run.returncode or marker not in run.stdout:
        raise RuntimeError(f'Portable PowerShell smoke failed (rc={run.returncode}): {run.stderr[-500:]}')
    return {'powershell': 'passed', 'version': run.stdout.replace(marker, '').strip(),
            'runtime_sha256': _sha(executable)}


def _smoke_linuxkit_bedrock(builds: Path, arch: str) -> dict:
    """Consume the existing LinuxKit/CraftMaster smoke; do NOT install Bedrock twice.

    `./Cinter setup all` already boots LinuxKit, runs the official server,
    exercises its console/networking, and writes this receipt. The native Host
    bundle uses the same executed evidence rather than a second incompatible
    `BedrockManager` installation with different paths/consent storage.
    """
    if arch != 'arm64':
        raise RuntimeError('Apple LinuxKit/Rosetta Bedrock appliance requires macOS arm64')
    path = builds / 'Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json'
    if not path.is_file():
        raise RuntimeError(f'LinuxKit Bedrock execution receipt missing: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('schema') != 'cinterforge.cfos.linuxkit-bedrock-test.v1' or
            data.get('status') != 'passed' or
            data.get('test') != 'real-official-bedrock-rosetta-nethernet-console-stop'):
        raise RuntimeError(f'LinuxKit Bedrock execution receipt is not accepted: {path}')
    guest = builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac' / arch
    components = {'cinter-bds-kernel', 'cinter-bds-initrd.img',
                  'cinter-bds-cmdline', 'CinterBDSVM'}
    hashes = data.get('bootstrap_sha256')
    if not isinstance(hashes, dict) or not components.issubset(hashes):
        raise RuntimeError('LinuxKit Bedrock evidence is missing bootstrap checksums')
    for name in components:
        file = guest / name
        if not file.is_file() or file.is_symlink() or _sha(file) != hashes[name]:
            raise RuntimeError(f'LinuxKit Bedrock evidence differs from staged appliance: {file}')
    return {'bedrock': 'passed', 'execution': 'linuxkit-rosetta',
            'evidence_sha256': _sha(path), 'guest_component_sha256':
            {k: hashes[k] for k in sorted(components)}}


def _smoke_bedrock(builds: Path, stage: Path, system: str) -> dict:
    if system == 'mac':
        return _smoke_linuxkit_bedrock(builds, _native()[1])
    manager = _load('cinter_bootstrap_bedrock', stage / 'webapp/CinterForgeTerminal.py').BedrockManager(builds, system)
    if not manager.accepted():
        raise RuntimeError('Bedrock EULA not accepted in WebView; cannot download or smoke server during Setup')
    if not manager.installed():
        raise RuntimeError('Bedrock is not installed; use WebView EULA → official download → verified install first')
    log_path = builds / 'Logs/Bedrock' / (manager.app + '.log')
    previous = log_path.stat().st_size if log_path.exists() else 0
    try:
        state = manager.start()
        if not state.get('running'):
            raise RuntimeError('Bedrock server process did not remain running')
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if log_path.is_file():
                with log_path.open('rb') as source:
                    source.seek(previous)
                    output = source.read(1024 * 1024).decode('utf-8', errors='replace')
                if any(marker in output for marker in SERVER_MARKERS):
                    return {'bedrock': 'passed', 'readiness': 'console-marker',
                            'platform': manager.target}
            if not manager.status().get('running'):
                raise RuntimeError('Bedrock process exited before declaring server readiness')
            time.sleep(.3)
        raise RuntimeError('Bedrock never logged a server-ready marker during Setup smoke')
    finally:
        manager.stop()


def _smoke_webview(builds: Path, host: Path, system: str) -> dict:
    """Launch the actual native WebView and require a JS->C++ callback."""
    executable = host / 'bin' / ('CinterForgeTerminal.exe' if system == 'win' else 'CinterForgeTerminal')
    if not executable.is_file():
        raise RuntimeError(f'Native WebView Application Shell not staged: {executable}')
    run_directory = builds / 'Run/CinterForgeTerminal'
    run_directory.mkdir(parents=True, exist_ok=True)
    run = subprocess.run([str(executable), '--smoke-js'],
                         cwd=run_directory,
                         capture_output=True, text=True, timeout=25, check=False)
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    output = run.stdout + run.stderr
    (logs / f'webview-js-{system}.log').write_text(output, encoding='utf-8')
    if run.returncode or 'WEBVIEW_JS_PASS result=4 callback=native' not in output:
        raise RuntimeError(f'Native {system} WebView JavaScript callback failed rc={run.returncode}; '
                           f'see {logs / f"webview-js-{system}.log"}')
    return {'status': 'passed', 'execution': 'native-webview-js-callback',
            'executable_sha256': _sha(executable), 'output': output[-3000:]}


def _receipt(builds: Path, system: str, arch: str) -> Path:
    return builds / 'CinterForgeTerminal/Bootstrap/Receipts' / system / arch / RELEASE / 'receipt.json'


def smoke_local(builds: Path) -> dict:
    builds = _require_builds(builds)
    system, arch = _native()
    host = _host_stage(builds, system, arch)
    python_root = _portable_python(builds, system, arch)
    python_exe = _python_binary(python_root, system)
    # A real Bash/compiler and WebView/JS execution gate on every native OS.
    from host.toolchain import probe_local
    tools = probe_local(builds, require_cross=(system == 'mac'))
    webview = _smoke_webview(builds, host, system)
    if system == 'mac':
        from host.win_cross import smoke as cross_smoke
        windows_cross = cross_smoke(builds)
    else:
        windows_cross = None
    # Fail closed. Do not write a positive receipt until every actual smoke passed.
    report = {'schema': SMOKE_SCHEMA, 'platform': system, 'arch': arch, 'release': RELEASE,
              'host_manifest_sha256': _sha(host / 'manifest.json'),
              'toolchain': tools, 'webview_js': webview, 'windows_cross_wine': windows_cross,
              'http': _smoke_http(builds, host, python_exe),
              'bedrock': _smoke_bedrock(builds, host, system),
              'completed_utc': time.time()}
    receipt = _receipt(builds, system, arch)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temp = receipt.with_suffix('.json.tmp')
    temp.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(receipt)
    return report


def _stage_copy(source: Path, target: Path) -> None:
    _checked_tree(source)
    shutil.copytree(source, target, symlinks=False)
    if _checked_tree(source) != _checked_tree(target):
        raise RuntimeError(f'Runtime copy does not match verified input: {source}')


def _stable_smoke_receipt(receipt: dict) -> dict:
    # Setup executes smoke tests on every invocation. Execution timestamps are
    # volatile evidence, not a reason to invalidate an identical runtime bundle.
    # The original dated receipt remains under Builds/Bootstrap/Receipts.
    data = {key: value for key, value in receipt.items() if key != 'completed_utc'}
    if isinstance(data.get('toolchain'), dict):
        data['toolchain'] = {k: v for k, v in data['toolchain'].items() if k != 'completed_utc'}
    if isinstance(data.get('windows_cross_wine'), dict):
        data['windows_cross_wine'] = {k: v for k, v in data['windows_cross_wine'].items()
                                      if k != 'completed_utc'}
    return data


def _smoke_copied_runtime(builds: Path, candidate: Path, system: str, arch: str,
                          receipt: dict) -> dict:
    """Execute the *relocated* Python and native WebView binary, not the originals.

    This can only run on the target OS/architecture. The managed target runner
    builds its own bundle, then the orchestrator can inspect its manifest.
    """
    if _native() != (system, arch):
        raise RuntimeError('Relocated runtime smoke requires the actual target runner')
    python_exe = _python_binary(candidate / 'PythonRuntime', system)
    http = _smoke_http(builds, candidate / 'Host', python_exe)
    webview = _smoke_webview(builds, candidate / 'Host', system)
    for key in ('http_health', 'authenticated_api', 'unauthenticated_api_rejected',
                'html', 'python_sha256', 'server_sha256'):
        if http.get(key) != receipt.get('http', {}).get(key):
            raise RuntimeError(f'Relocated HTTP smoke differs from staged runtime: {key}')
    if webview.get('status') != receipt.get('webview_js', {}).get('status') or \
            webview.get('executable_sha256') != receipt.get('webview_js', {}).get('executable_sha256'):
        raise RuntimeError('Relocated WebView JS smoke differs from native staged runtime')
    return {'http': http, 'webview_js': webview, 'execution': 'native-relocated'}


def _find_arches(builds: Path, system: str) -> list[str]:
    root = builds / 'CinterForgeTerminal/Bootstrap/Receipts' / system
    return sorted({p.parent.parent.name for p in root.glob(f'*/{RELEASE}/receipt.json')})


def bundle_target(builds: Path, system: str, arch: str) -> Path:
    builds = _require_builds(builds)
    receipt = _receipt(builds, system, arch)
    if not receipt.is_file():
        raise RuntimeError(f'Target has no real bootstrap smoke receipt: {receipt}')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if (data.get('schema') != SMOKE_SCHEMA or data.get('platform') != system or
            data.get('arch') != arch or data.get('release') != RELEASE or
            data.get('http', {}).get('http_health') != 'passed' or
            data.get('http', {}).get('authenticated_api') != 'passed' or
            data.get('http', {}).get('unauthenticated_api_rejected') != 'passed' or
            data.get('http', {}).get('html') != 'passed' or
            data.get('toolchain', {}).get('status') != 'passed' or
            data.get('toolchain', {}).get('platform') != system or
            data.get('toolchain', {}).get('arch') != arch or
            data.get('webview_js', {}).get('status') != 'passed' or
            data.get('bedrock', {}).get('bedrock') != 'passed' or
            (system == 'mac' and data.get('windows_cross_wine', {}).get('status') != 'passed')):
        raise RuntimeError(f'Target has incomplete bootstrap smoke receipt: {receipt}')
    host = _host_stage(builds, system, arch)
    if data['host_manifest_sha256'] != _sha(host / 'manifest.json') or \
            data.get('http', {}).get('server_sha256') != _sha(host / 'webapp/CinterForgeTerminal.py'):
        raise RuntimeError(f'Smoke receipt belongs to an older staged Host: {receipt}')
    # PowerShell is available as a future optional terminal but not a bootstrap prerequisite.
    environment = builds / 'Python/Environments' / system / arch / PYTHON_ENV
    # For foreign targets this must already have been validated by their own
    # smoke process. No build-machine execution of foreign binaries is faked.
    if not environment.is_dir():
        raise RuntimeError(f'Portable Python dependency missing: {environment}')
    python_exec = _python_binary(environment, system)
    if data.get('http', {}).get('python_sha256') != _sha(python_exec):
        raise RuntimeError('Python executable differs from the smoke-tested runtime')
    output = builds / 'CinterForgeTerminal/Bundles' / system / arch / RELEASE
    output.parent.mkdir(parents=True, exist_ok=True)
    if _native() != (system, arch):
        # The target runner must assemble and smoke the relocated closure.
        if not (output / 'manifest.json').is_file():
            raise RuntimeError(f'Foreign-target bundle must be produced by its target runner: {output}')
        foreign = verify_bundle(output)
        if (foreign.get('platform') != system or foreign.get('arch') != arch or
                foreign.get('release') != RELEASE or
                foreign.get('smoke_receipt') != _stable_smoke_receipt(data) or
                foreign.get('relocated_runtime_smoke', {}).get('execution') != 'native-relocated'):
            raise RuntimeError(f'Foreign-target bundle does not match its current receipt: {output}')
        return output
    with tempfile.TemporaryDirectory(prefix='CinterBundle-', dir=output.parent) as td:
        candidate = Path(td) / RELEASE
        candidate.mkdir()
        _stage_copy(host, candidate / 'Host')
        _stage_copy(environment, candidate / 'PythonRuntime')
        # Self-hosting source + pinned WebView source are part of every developer
        # bundle. No sibling checkout or network access is required to rebuild.
        source = _bootstrap_runner(builds).parent
        _stage_copy(source, candidate / 'Source')
        from host.webview import TAG as webview_tag
        _stage_copy(builds / 'Toolchains/WebView' / webview_tag / 'source', candidate / 'WebViewSource')
        # Windows native and Mac cross-builds require the same pinned upstream
        # WebView2 SDK headers, never an implicit NuGet request at rebuild time.
        if system in ('mac', 'win'):
            from host.webview import WEBVIEW2_VERSION
            sdk = builds / 'Toolchains/WebView2' / WEBVIEW2_VERSION / 'source'
            _stage_copy(sdk, candidate / 'WebView2Headers')
        # The redistributable browser itself is mandatory: an offline
        # Evergreen installer still changes system-global state. Package the
        # verified Microsoft's Fixed Version browser inside the app instead.
        if system == 'win':
            from host.windows_fixed_runtime import verify as verify_fixed
            browser_folder = verify_fixed(builds)
            _stage_copy(browser_folder, candidate / 'WebView2FixedRuntime')
        # Windows cross tooling on macOS is the verified offline container
        # image below, NOT the nonexistent Build/win directory. The native
        # Windows bundle independently includes its complete MSYS2 closure.
        toolchain_roots = {'mac': ('mac', 'linux'),
                           'linux': ('linux',), 'win': ('win',)}[system]
        for target in toolchain_roots:
            folder = (builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2' if target == 'win' and system == 'win'
                      else builds / 'CinterForgeTerminal/Toolchains/Build' / target)
            _stage_copy(folder, candidate / 'BuildTools' / target)
        if system == 'mac' and data.get('toolchain', {}).get('cross_provider') == 'docker-linux-amd64-wine':
            # The actual cross-compiler/Wine image must travel with the Mac
            # developer bundle, so later Windows rebuilds need no downloads.
            from host.windows_environment import WindowsCrossEnvironment
            offline_image, _ = WindowsCrossEnvironment.validate_staged(builds)
            _stage_copy(offline_image, candidate / 'WindowsCrossImage')
            from host.windows_fixed_runtime import verify as verify_fixed
            _stage_copy(verify_fixed(builds), candidate / 'WindowsCrossWebView2FixedRuntime')
        if system == 'mac':
            # CinterForgeTerminal's macOS Bedrock guest is deployed independently
            # from the licensed Bedrock server. Include the complete staged
            # LinuxKit guest closure, not a path back into CFOS source.
            guest = builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac' / arch
            _stage_copy(guest, candidate / 'LinuxKit')
        relocated = _smoke_copied_runtime(builds, candidate, system, arch, data)
        manifest = {'schema': SCHEMA, 'platform': system, 'arch': arch, 'release': RELEASE,
                    'bedrock_distribution': 'EULA-gated installer only; official binary not included',
                    'smoke_receipt': _stable_smoke_receipt(data),
                    'relocated_runtime_smoke': relocated,
                    'files': _checked_tree(candidate)}
        (candidate / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        if output.exists():
            verify_bundle(output)
            if json.loads((output / 'manifest.json').read_text()) != manifest:
                raise RuntimeError(f'Existing immutable runtime bundle differs: {output}')
            return output
        candidate.rename(output)
    verify_bundle(output)
    return output


def verify_bundle(bundle: Path) -> dict:
    data = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if data.get('schema') != SCHEMA:
        raise RuntimeError(f'Not a CinterForgeTerminal runtime bundle: {bundle}')
    actual = _checked_tree(bundle)
    actual.pop('manifest.json', None)
    if actual != data.get('files'):
        raise RuntimeError(f'Runtime bundle tampered or incomplete: {bundle}')
    return data


def inject_bundle(builds: Path, bundle: Path, application: Path) -> Path:
    """Inject into an existing staged application without touching its source repo."""
    builds = _require_builds(builds)
    bundle, application = bundle.resolve(), application.resolve()
    if not bundle.is_relative_to(builds) or not application.is_relative_to(builds):
        raise ValueError('Both bundle and staged application must live under Builds')
    data = verify_bundle(bundle)
    if not application.is_dir():
        raise RuntimeError(f'Application stage not found: {application}')
    location = (application / 'Contents/Resources' if data['platform'] == 'mac' else application)
    if data['platform'] == 'mac' and (application.suffix != '.app' or not (application / 'Contents').is_dir()):
        raise RuntimeError('A macOS target must be a staged .app with Contents')
    location.mkdir(parents=True, exist_ok=True)
    destination = location / 'CinterForgeTerminalRuntime'
    if destination.exists():
        if verify_bundle(destination) != data:
            raise RuntimeError('Application has a different existing CinterForgeTerminal runtime; refusing overwrite')
        return destination
    _stage_copy(bundle, destination)
    verify_bundle(destination)
    return destination


def _bootstrap_runner(builds: Path) -> Path:
    """Stage a self-contained and versioned *source* runner under Builds.

    A lone copied bootstrap_bundle.py cannot import `host`, compile the Host,
    or test a target. The runner carries the actual current CMake/C++/Python
    sources and target tests. It does not read any sibling repository checkout
    on Linux or Windows. Host release immutability applies to this closure too.
    """
    repo = Path(__file__).resolve().parents[1]
    output = builds / 'CinterForgeTerminal/Bootstrap/Runner' / RELEASE
    names = ('CMakeLists.txt', 'Cinter', 'build/powershell.packages.example.json')
    subtrees = ('host', 'include', 'src', 'build', 'webapp', 'tests', 'linuxos')
    sources = {name: repo / name for name in names}
    for directory in subtrees:
        for path in (repo / directory).rglob('*'):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
                sources[path.relative_to(repo).as_posix()] = path
    bootstrap = ("#!/usr/bin/env python3\n"
                 "from pathlib import Path\nimport os, sys\n"
                 "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
                 "from host.bootstrap_bundle import main\n"
                 "if __name__ == '__main__':\n    raise SystemExit(main())\n")
    manifest_data = {'schema': 'cinterforge.terminal.bootstrap-runner.v1',
                     'release': RELEASE,
                     'files': {rel: _sha(src) for rel, src in sorted(sources.items())}}
    manifest_data['files']['bootstrap_runner.py'] = hashlib.sha256(bootstrap.encode()).hexdigest()
    if output.exists():
        if not (output / 'manifest.json').is_file():
            raise RuntimeError(f'Incomplete immutable bootstrap source runner: {output}')
        if (json.loads((output / 'manifest.json').read_text(encoding='utf-8')) != manifest_data or
                {k: v for k, v in _checked_tree(output).items() if k != 'manifest.json'} != manifest_data['files']):
            raise RuntimeError(f'Existing bootstrap source runner differs; bump Host release: {output}')
        return output / 'bootstrap_runner.py'
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='CinterRunner-', dir=output.parent) as scratch:
        candidate = Path(scratch) / RELEASE
        candidate.mkdir()
        for rel, src in sources.items():
            destination = candidate / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, destination)
        launcher = candidate / 'bootstrap_runner.py'
        launcher.write_text(bootstrap, encoding='utf-8')
        launcher.chmod(launcher.stat().st_mode | 0o111)
        (candidate / 'manifest.json').write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        if {k: v for k, v in _checked_tree(candidate).items() if k != 'manifest.json'} != manifest_data['files']:
            raise RuntimeError('Staged bootstrap runner did not match its sources')
        candidate.rename(output)
    return output / 'bootstrap_runner.py'


def setup_local(builds: Path) -> dict:
    """Run the *real* native Setup/Build/Stage/Test on a target runner.

    No foreign OS is inferred from Wine, a LinuxKit Bedrock appliance, or a
    marker file. Stage the standard-library-only portable Python first, then
    build and smoke the actual native Host and its relocated service closure.
    """
    builds = _require_builds(builds)
    system, arch = _native()
    from host.portable_python import provision as provision_python
    from host.manager import build_host, stage_host, test_host
    from host.toolchain import probe_local
    probe_local(builds, require_cross=(system == 'mac'))
    provision_python(builds, system, arch)
    build_host(builds, application_shell=True)
    stage_host(builds)
    test_host(builds)
    report = smoke_local(builds)
    bundle = bundle_target(builds, system, arch)
    return {'platform': system, 'arch': arch, 'receipt': report, 'bundle': str(bundle)}


def _registered_runners(builds: Path, native: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Resolve managed, Builds-resident target runners without requiring hand setup.

    `setup-runners.json` remains an optional override for already staged tools;
    it is NOT a prerequisite for Setup. A Bedrock-only LinuxKit appliance is
    not misrepresented as a general Linux Python/Windows execution environment.
    Missing transports are explicit target errors, not silently skipped.
    """
    config_path = builds / 'CinterForgeTerminal/Input/setup-runners.json'
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))
        if config.get('schema') != RUNNERS_SCHEMA or not isinstance(config.get('runners'), dict):
            raise RuntimeError(f'Invalid optional target-runner manifest: {config_path}')
        supplied = config['runners']
    else:
        supplied = {}
    commands = {}
    missing = {}
    for target in TARGETS:
        if target == native:
            continue
        command = supplied.get(target)
        if command is None:
            # Target tool installers may stage the runner here as part of Setup.
            # This convention does not assume a LinuxKit Bedrock appliance can
            # execute arbitrary Python/PowerShell or Windows programs.
            executable = (builds / 'CinterForgeTerminal/Toolchains/TargetRunners' /
                          target / 'run')
            if executable.is_file():
                command = [str(executable), '{bootstrap}', '--builds', '{builds}', '--setup-local']
        if (not isinstance(command, list) or not command or
                not all(isinstance(item, str) and item for item in command)):
            expected = (builds / 'CinterForgeTerminal/Toolchains/TargetRunners' /
                        target / 'run')
            missing[target] = (f'No managed {target} target runner at {expected}. '
                'A general-purpose Linux or Windows Bash/Python/WebView target runner '
                'must be provisioned; the existing LinuxKit Bedrock guest is not one.')
            continue
        executable = Path(command[0]).resolve()
        if not executable.is_file() or not executable.is_relative_to(builds.resolve()):
            missing[target] = f'{target} target runner is not a staged executable under Builds: {executable}'
            continue
        commands[target] = [str(executable), *command[1:]]
    return commands, missing


def setup_all(builds: Path) -> dict:
    """One public operation for all targets; report every incomplete target."""
    builds = _require_builds(builds)
    runner = _bootstrap_runner(builds)
    system, arch = _native()
    runners, problems = _registered_runners(builds, system)
    freshly_smoked = {}
    if system in TARGETS:
        try:
            smoke_local(builds)
            bundle_target(builds, system, arch)
            freshly_smoked[system] = [arch]
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            problems[system] = f'Native {system} runtime cannot be bundled: {exc}'
    for target, command in runners.items():
        try:
            run = [command[0], *(arg.replace('{bootstrap}', str(runner))
                                  .replace('{builds}', str(builds)) for arg in command[1:])]
            before = {a: _receipt(builds, target, a).stat().st_mtime_ns
                      for a in _find_arches(builds, target)}
            subprocess.run(run, check=True, timeout=300)
            verified = [a for a in _find_arches(builds, target)
                        if _receipt(builds, target, a).stat().st_mtime_ns > before.get(a, 0)]
            if not verified:
                raise RuntimeError('runner exited without a fresh target smoke receipt')
            freshly_smoked[target] = verified
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            problems[target] = str(exc)
    if problems:
        detail = '\n'.join(f'  {target}: {problem}' for target, problem in sorted(problems.items()))
        raise RuntimeError('CinterForgeTerminal setup all is incomplete on these targets:\n' + detail)
    outputs = {}
    for target in TARGETS:
        if target not in freshly_smoked:
            raise RuntimeError(f'No real target execution for {target}')
        outputs[target] = [str(bundle_target(builds, target, arch))
                           for arch in freshly_smoked[target]]
    return outputs


def provision_zmachine(builds: Path) -> Path:
    """Acquire Daniel Lockard's upstream Git source; build JS in sibling Builds.

    The authored version 0.3.2 is checked against upstream package.json. A
    concrete Git commit and SHA-256 inventory are recorded with the output.
    Never read the WebApp checkout or acquire any game files during Setup.
    """
    builds = _require_builds(builds)
    version = '0.3.2'
    dest = builds / 'CinterForgeTerminal/Dependencies/ZMachine' / version
    receipt = dest / 'source-receipt.json'
    if receipt.is_file() and (dest / 'dist/index.js').is_file():
        info = json.loads(receipt.read_text())
        if info.get('version') != version or info.get('repository') != 'https://github.com/daniellockard/zmachine.git':
            raise RuntimeError('ZMachine upstream receipt changed')
        for name, expected in info['files'].items():
            file = dest / name
            if not file.is_file() or _sha(file) != expected:
                raise RuntimeError('Damaged staged ZMachine file: ' + name)
        return dest
    source = 'https://github.com/daniellockard/zmachine.git'
    tools = builds / 'CinterForgeTerminal/Toolchains/mac/env/bin'
    git = shutil.which('git')
    npm = str(tools / 'npm') if (tools / 'npm').is_file() else shutil.which('npm')
    node = str(tools / 'node') if (tools / 'node').is_file() else shutil.which('node')
    if not all((git, npm, node)):
        raise RuntimeError('ZMachine Setup requires Git plus staged Node/npm')
    parent = builds / 'Intermediates/CinterForgeTerminal/ZMachine'
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='upstream-', dir=parent) as scratch:
        clone = Path(scratch) / 'source'
        subprocess.run([git,'clone','--depth','1',source,str(clone)],check=True)
        commit = subprocess.run([git,'-C',str(clone),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
        package = json.loads((clone/'package.json').read_text())
        if package.get('version') != version or package.get('name') != 'zmachine':
            raise RuntimeError(f'Upstream ZMachine version differs from vetted {version}: {package.get("version")}')
        env = dict(os.environ, npm_config_cache=str(builds/'CinterForgeTerminal/Toolchains/NpmCache'),
                   npm_config_update_notifier='false', HUSKY='0')
        subprocess.run([npm,'ci','--ignore-scripts','--no-audit','--no-fund'],cwd=clone,env=env,check=True)
        subprocess.run([npm,'run','build'],cwd=clone,env=env,check=True)
        if not (clone/'dist/index.js').is_file():
            raise RuntimeError('Upstream TypeScript build did not produce dist/index.js')
        candidate = Path(scratch) / 'artifact';candidate.mkdir()
        shutil.copytree(clone/'dist', candidate/'dist')
        shutil.copy2(clone/'LICENSE', candidate/'LICENSE')
        shutil.copy2(clone/'package.json', candidate/'package.json')
        hashes = {p.relative_to(candidate).as_posix():_sha(p)
                  for p in sorted(candidate.rglob('*')) if p.is_file()}
        (candidate/'source-receipt.json').write_text(json.dumps({
            'schema':'cinterforge.terminal.zmachine-git.v1','repository':source,
            'version':version,'commit':commit,'files':hashes},indent=2,sort_keys=True)+'\n')
        dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists():
            raise RuntimeError('Existing unverified ZMachine stage: '+str(dest))
        candidate.rename(dest)
    print('[ZMachine] Git source '+commit+' compiled under '+str(dest),flush=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    import argparse
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--builds', type=Path, required=True)
    cli.add_argument('--smoke-local', action='store_true', help='Internal smoke-only command')
    cli.add_argument('--setup-local', action='store_true', help='Perform full native build/stage/test/smoke on the target runner')
    args = cli.parse_args(argv)
    if args.setup_local:
        print(json.dumps(setup_local(args.builds), indent=2))
    elif args.smoke_local:
        receipt = smoke_local(args.builds)
        system, arch = _native()
        bundle = bundle_target(args.builds, system, arch)
        print(json.dumps({'receipt': receipt, 'bundle': str(bundle)}, indent=2))
    else:
        print(json.dumps(setup_all(args.builds), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
