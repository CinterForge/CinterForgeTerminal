"""CinterForge Python Host: bootstrap, verify, and run named environments.

Every writable path belongs to the sibling Builds/Python tree. Consumers stage
manifests, requirements and service code there instead of referring to sibling
source repositories. Host-installed Python is a Setup tool, never a release
runtime closure. Never mutate an existing healthy cinterenv during adoption.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform as host_platform
import re
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from host import HOST_RELEASE

SCHEMA = 'cinterforge.python-host.projects.v1'
PYTHON_VERSION = '3.11.0'
ENV_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
PLATFORMS = {'Darwin': 'mac', 'Windows': 'win', 'Linux': 'linux'}


def current_platform() -> str:
    if host_platform.system() not in PLATFORMS:
        raise RuntimeError(f'Unsupported Python host: {host_platform.system()}')
    return PLATFORMS[host_platform.system()]


def architecture() -> str:
    machine = host_platform.machine().lower()
    return {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x64',
            'amd64': 'x64'}.get(machine, machine)


def builds_root(repo: Path, explicit: Path | None) -> Path:
    return (explicit if explicit else Path(os.environ.get('CINTER_BUILDS_ROOT', str(repo.parent / 'Builds')))).resolve()


def python_root(builds: Path) -> Path:
    return builds / 'Python'


def environment_dir(builds: Path, name: str, system: str | None = None,
                    arch: str | None = None) -> Path:
    if not ENV_PATTERN.fullmatch(name):
        raise ValueError(f'Invalid environment name: {name!r}')
    return python_root(builds) / 'Environments' / (system or current_platform()) / (arch or architecture()) / name


def python_executable(environment: Path, system: str | None = None) -> Path:
    return environment / ('Scripts/python.exe' if (system or current_platform()) == 'win' else 'bin/python')


def inside(root: Path, item: str) -> Path:
    """Reject untrusted config paths pointing into a sibling source checkout."""
    path = Path(item)
    if path.is_absolute() or '..' in path.parts or not item.strip():
        raise ValueError(f'Path must be relative to Builds/Python: {item}')
    candidate = root / path
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes Builds/Python: {item}')
    return candidate


def read_projects(builds: Path, manifest: Path | None = None) -> dict:
    source = manifest or python_root(builds) / 'Input/projects.json'
    if not source.is_file():
        raise FileNotFoundError(f'Python host project manifest missing: {source}. Stage it during the owning app Setup.')
    if not source.resolve().is_relative_to(python_root(builds).resolve()):
        raise ValueError('Project manifest must be staged within Builds/Python')
    data = json.loads(source.read_text(encoding='utf-8'))
    if data.get('schema') != SCHEMA or not isinstance(data.get('environments'), dict) or not isinstance(data.get('services'), dict):
        raise ValueError(f'Invalid Python host project manifest: {source}')
    return data


def requirements(builds: Path, profile: dict, system: str) -> list[Path]:
    specs = profile.get('requirements', {})
    if not isinstance(specs, dict):
        raise ValueError('requirements must be an object with common/mac/win/linux keys')
    root = python_root(builds)
    paths = []
    for key in ('common', system):
        value = specs.get(key)
        if value:
            selected = [value] if isinstance(value, str) else value
            if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected):
                raise ValueError(f'Invalid requirements selection for {key}')
            for item in selected:
                path = inside(root, item)
                if not path.is_file():
                    raise FileNotFoundError(f'Platform requirements missing: {path}')
                paths.append(path)
    return paths


def requirements_fingerprint(files: list[Path], version: str, system: str) -> str:
    digest = hashlib.sha256(f'python={version}\nplatform={system}\n'.encode())
    for path in files:
        digest.update(path.name.encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def _run(args: list[str], *, env: dict | None = None, cwd: Path | None = None) -> None:
    print('[CPH] ' + ' '.join(args), flush=True)
    subprocess.run(args, check=True, env=env, cwd=cwd)


def find_python(builds: Path, version: str, system: str, *, bootstrap: bool) -> str:
    """Reuse configured Python or pinned pyenv; install interpreter only if opted in.

    Unlike old Server setup, no `pyenv local` writes to any source checkout.
    Pyenv-managed versions live under Builds/Python/Toolchains when possible.
    """
    root = python_root(builds)
    override = os.environ.get('CINTERFORGE_PYTHON_BOOTSTRAP')
    if override:
        exe = Path(override).expanduser().resolve()
        if not exe.is_file():
            raise FileNotFoundError(f'CINTERFORGE_PYTHON_BOOTSTRAP missing: {exe}')
        return str(exe)
    # A local pyenv installation can provision pinned CPython under Builds.
    tools = root / 'Toolchains'
    pyenv_root = tools / ('pyenv-win' if system == 'win' else 'pyenv')
    pyenv = (pyenv_root / 'pyenv-win/bin/pyenv.bat' if system == 'win' else pyenv_root / 'bin/pyenv')
    discovered = str(pyenv) if pyenv.exists() else shutil.which('pyenv')
    if not discovered and bootstrap and system == 'mac':
        brew = shutil.which('brew')
        if not brew or not Path(brew).is_file():
            raise RuntimeError('Install Homebrew or provide CINTERFORGE_PYTHON_BOOTSTRAP')
        env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ENV_HINTS='1')
        _run([brew, 'install', '--no-ask', 'pyenv'], env=env)
        discovered = shutil.which('pyenv')
    if not discovered and bootstrap and system == 'win':
        # Existing setup used remote, unpinned PowerShell execution. Source
        # checkout under Builds is explicit and repeatable; never write to HOME.
        checkout = tools / 'pyenv-win'
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if not checkout.exists():
            _run(['git', 'clone', '--depth', '1', 'https://github.com/pyenv-win/pyenv-win.git', str(checkout)])
        pyenv = checkout / 'pyenv-win/bin/pyenv.bat'
        if not pyenv.is_file():
            raise RuntimeError(f'pyenv-win bootstrap is incomplete: {pyenv}')
        discovered = str(pyenv)
    if discovered:
        environment = dict(os.environ)
        environment['PYENV_ROOT'] = str(pyenv_root)
        if system == 'win':
            environment['PYENV'] = str(pyenv_root)
        if bootstrap:
            _run([discovered, 'install', '-s', version], env=environment)
        result = subprocess.run([discovered, 'prefix', version], env=environment,
                                capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            prefix = Path(result.stdout.strip())
            candidate = prefix / ('python.exe' if system == 'win' else 'bin/python')
            if candidate.is_file():
                return str(candidate)
    for candidate in (str(tools / system / version / ('python.exe' if system == 'win' else 'bin/python3')),
                      shutil.which('python3') or '', sys.executable):
        if not candidate or not Path(candidate).is_file():
            continue
        check = subprocess.run([candidate, '-c',
            'import sys; print(".".join(map(str, sys.version_info[:3])))'],
            capture_output=True, text=True, check=False)
        if check.returncode == 0 and check.stdout.strip() == version:
            return candidate
    raise RuntimeError(f'Python {version} unavailable for {system}. Install it with pyenv or stage a pinned interpreter; '
                       'use --bootstrap to install a missing version.')


def _verify_interpreter(python: Path, *, version: str | None = None) -> str:
    run = subprocess.run([str(python), '-c',
        'import sys;print(".".join(map(str,sys.version_info[:3])))'],
        capture_output=True, text=True, timeout=30, check=False)
    if run.returncode:
        raise RuntimeError(f'Python runtime failed: {python}: {run.stderr.strip()}')
    actual = run.stdout.strip()
    if version and actual != version:
        raise RuntimeError(f'Python at {python} is {actual}, expected {version}')
    return actual


def adopt_legacy(builds: Path, destination: Path, version: str, *, source: Path | None = None) -> bool:
    """Copy a working legacy cinterenv, verify it, retain the original.

    A venv may refer to its old absolute path. A copied candidate is accepted
    only after executing its interpreter and verifying version. No symlink hacks.
    """
    old = source or (builds / 'cinterenv')
    if destination.exists() or not old.is_dir():
        return False
    if not old.resolve().is_relative_to(builds.resolve()):
        raise ValueError('Legacy environment must reside inside Builds')
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = destination.with_name(destination.name + '.candidate')
    if candidate.exists():
        raise RuntimeError(f'Unresolved previous environment candidate: {candidate}')
    try:
        shutil.copytree(old, candidate, symlinks=False)
        _verify_interpreter(python_executable(candidate), version=version)
        candidate.rename(destination)
        return True
    except Exception:
        shutil.rmtree(candidate, ignore_errors=True)
        raise


def setup_environment(builds: Path, name: str, profile: dict, *, bootstrap: bool = False,
                      adopt: bool = False) -> dict:
    system = current_platform()
    version = profile.get('python', PYTHON_VERSION)
    dest = environment_dir(builds, name)
    reqs = requirements(builds, profile, system)
    if not reqs:
        raise ValueError(f'{name}: specify common or {system} requirements in staged manifest; '
                         'an empty environment is not a complete application.')
    stamp = requirements_fingerprint(reqs, version, system)
    adopted = False
    if not dest.exists() and adopt and name == 'cinterenv':
        adopted = adopt_legacy(builds, dest, version)
    executable = python_executable(dest)
    if not executable.is_file():
        base = find_python(builds, version, system, bootstrap=bootstrap)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise RuntimeError(f'Incomplete existing Python environment: {dest}; manual reconciliation required')
        _run([base, '-m', 'venv', str(dest)])
    actual_version = _verify_interpreter(executable, version=version)
    receipt_file = dest / 'cinterforge-requirements.json'
    before = json.loads(receipt_file.read_text()) if receipt_file.is_file() else {}
    if before.get('fingerprint') != stamp:
        _run([str(executable), '-m', 'pip', 'install', '--disable-pip-version-check',
              '--upgrade', 'pip', 'setuptools', '--prefer-binary'])
        for requirement in reqs:
            _run([str(executable), '-m', 'pip', 'install', '--disable-pip-version-check',
                  '--progress-bar', 'on', '-r', str(requirement)])
    for module in profile.get('imports', []):
        if not isinstance(module, str) or not re.fullmatch(r'[a-zA-Z_][\w.]*', module):
            raise ValueError(f'Invalid required module: {module!r}')
        _run([str(executable), '-c', f'import importlib;importlib.import_module({module!r})'])
    receipt = {'schema': 'cinterforge.python-host.environment.v1', 'name': name,
               'python': actual_version, 'platform': system, 'arch': architecture(),
               'fingerprint': stamp, 'adopted_legacy': adopted,
               'requirements': [str(p.relative_to(python_root(builds))) for p in reqs]}
    receipt_file.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'[CPH setup] {name}: ready at {dest}', flush=True)
    return receipt


def service_command(builds: Path, projects: dict, name: str) -> tuple[list[str], Path]:
    data = projects['services'].get(name)
    if not isinstance(data, dict):
        raise ValueError(f'Unknown service: {name}')
    environment_name = data.get('environment')
    if environment_name not in projects['environments']:
        raise ValueError(f'Service {name} references unknown environment')
    code = inside(python_root(builds), data['script'])
    if not code.is_file() or code.suffix.lower() != '.py':
        raise FileNotFoundError(f'Python service must be staged into Builds/Python: {code}')
    interpreter = python_executable(environment_dir(builds, environment_name))
    if not interpreter.is_file():
        raise FileNotFoundError(f'Run ./Cinter host setup first for {environment_name}: {interpreter}')
    arguments = data.get('arguments', [])
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise ValueError('Service arguments must be an array of strings')
    return [str(interpreter), '-u', str(code), *arguments], code.parent



# Application injection is an independent lifecycle operation. The consuming
# repository owns its requirements, service source and bundle layout; the Terminal Host
# consumes artifacts already staged under Builds/Python. A developer venv is
# not a distributable Python runtime unless its executable, standard library
# and native dependencies remain inside the copied bundle.
INJECTION_SCHEMA = 'cinterforge.python-host.inject.v1'


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _file_manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f'Runtime references an external symlink: {path}')
            result[path.relative_to(root).as_posix()] = 'symlink:' + os.readlink(path)
        elif path.is_file():
            result[path.relative_to(root).as_posix()] = _file_hash(path)
    return result


def _inspect_portability(environment: Path, version: str) -> dict:
    """Execute Python *inside the relocated candidate* to confirm isolation.

    Venvs whose pyvenv.cfg or symlink still points to ~/.pyenv or Homebrew are
    intentionally rejected, even if they happened to work on the build Mac.
    """
    executable = python_executable(environment)
    if not executable.is_file():
        raise RuntimeError(f'Embedded Python executable missing: {executable}')
    if not executable.resolve().is_relative_to(environment.resolve()):
        raise RuntimeError(f'Embedded Python links outside its own runtime: {executable}')
    inspection = ('import json,os,sys,sysconfig;'
                  'print(json.dumps(dict(version=".".join(map(str,sys.version_info[:3])), '
                  'executable=os.path.realpath(sys.executable), '
                  'prefix=os.path.realpath(sys.prefix), '
                  'base_prefix=os.path.realpath(sys.base_prefix), '
                  'stdlib=os.path.realpath(sysconfig.get_path("stdlib")), '
                  'purelib=os.path.realpath(sysconfig.get_path("purelib")))))')
    check = subprocess.run([str(executable), '-B', '-I', '-c', inspection], capture_output=True,
                           text=True, timeout=45, check=False,
                           env=dict(os.environ, PYTHONNOUSERSITE='1', PYTHONPATH=''))
    if check.returncode:
        raise RuntimeError(f'Embedded Python failed to start: {executable}: {check.stderr[-400:]}')
    try:
        report = json.loads(check.stdout.strip())
    except (ValueError, TypeError) as error:
        raise RuntimeError(f'Embedded Python returned invalid portability report: {executable}') from error
    if report.get('version') != version:
        raise RuntimeError(f'Embedded Python {report.get("version")}, expected {version}')
    # sys.base_prefix and stdlib must not depend on the build machine. The
    # Python installation may be directly embedded or a genuinely relocatable
    # venv rooted entirely in this environment.
    for field in ('executable', 'prefix', 'base_prefix', 'stdlib', 'purelib'):
        value = report.get(field)
        if not isinstance(value, str) or not Path(value).resolve().is_relative_to(environment.resolve()):
            raise RuntimeError(f'Python {field} escapes the embedded environment: {value}; '
                               'stage a portable runtime closure, not a developer virtualenv')
    return {key: (Path(value).resolve().relative_to(environment.resolve()).as_posix()
                  if key != 'version' else value) for key, value in report.items()}


def inject_application(builds: Path, projects: dict, application: str,
                       bundle: Path, *, replace: bool = False) -> dict:
    """Copy one verified named Python environment and its services into a stage.

    Only a staged app under Builds can be modified. The owner's Setup must
    first provide service files and requirements under Builds/Python. A release
    never refers to a sibling checkout, pyenv, Homebrew or another app bundle.
    """
    if not ENV_PATTERN.fullmatch(application):
        raise ValueError(f'Invalid application name: {application}')
    apps = projects.get('applications', {})
    profile = apps.get(application) if isinstance(apps, dict) else None
    if not isinstance(profile, dict):
        raise ValueError(f'Unknown application: {application}; declare applications in projects.json')
    environment_name = profile.get('environment')
    if environment_name not in projects['environments']:
        raise ValueError(f'Application {application} references unknown environment')
    service_names = profile.get('services', [])
    if not isinstance(service_names, list) or not all(isinstance(s, str) for s in service_names):
        raise ValueError('Application services must be a list of names')
    # Only the app owner may choose this destination; the host refuses source
    # repository paths and directories outside the shared Builds staging tree.
    root = builds.resolve()
    bundle = bundle.resolve()
    if not bundle.is_relative_to(root) or not bundle.is_dir():
        raise ValueError(f'Bundle must already be a staged application inside Builds: {bundle}')
    if current_platform() == 'mac':
        if bundle.suffix != '.app' or not (bundle / 'Contents').is_dir():
            raise ValueError('macOS injection requires an existing staged .app/Contents directory')
        resources = bundle / 'Contents/Resources'
    else:
        resources = bundle
    if not resources.resolve().is_relative_to(bundle.resolve()):
        raise ValueError('Application resource directory escapes staged bundle')
    output = resources / 'PythonRuntime'
    installed = output / environment_name
    source = environment_dir(builds, environment_name)
    source_receipt = source / 'cinterforge-requirements.json'
    if not source_receipt.is_file():
        raise RuntimeError(f'Environment missing verified requirements receipt: {source_receipt}')
    receipt = json.loads(source_receipt.read_text(encoding='utf-8'))
    if receipt.get('schema') != 'cinterforge.python-host.environment.v1' or receipt.get('name') != environment_name:
        raise RuntimeError(f'Invalid environment receipt: {source_receipt}')
    if receipt.get('platform') != current_platform() or receipt.get('arch') != architecture():
        raise RuntimeError('Python environment platform/architecture mismatch')
    env_profile = projects['environments'][environment_name]
    if receipt.get('python') != env_profile.get('python', PYTHON_VERSION):
        raise RuntimeError('Python environment version does not match project manifest')
    reqs = requirements(builds, env_profile, current_platform())
    if receipt.get('fingerprint') != requirements_fingerprint(reqs, receipt['python'], current_platform()):
        raise RuntimeError('Python environment requirements changed; run ./Cinter host setup before injection')
    if not (source.is_dir() and python_executable(source).exists()):
        raise RuntimeError(f'Python environment missing or incomplete: {source}')
    service_sources = {}
    for name in service_names:
        definition = projects['services'].get(name)
        if not isinstance(definition, dict) or definition.get('environment') != environment_name:
            raise ValueError(f'Invalid service {name} for application {application}')
        script = inside(python_root(builds), definition['script'])
        if script.suffix != '.py' or not script.is_file():
            raise RuntimeError(f'Missing staged service script: {script}')
        if not ENV_PATTERN.fullmatch(name):
            raise ValueError(f'Invalid service name: {name}')
        service_sources[name] = script
    if output.exists():
        previous_receipt = output / 'manifest.json'
        if not replace or not previous_receipt.is_file() or json.loads(previous_receipt.read_text()).get('schema') != INJECTION_SCHEMA:
            raise RuntimeError(f'Python runtime already exists: {output}; explicit --replace requires a verified prior CPH injection')
        verify_injection(bundle)  # Never overwrite a tampered previous injection.
    resources.mkdir(parents=True, exist_ok=True)
    # Candidate is on the same filesystem as the destination for atomic rename.
    with tempfile.TemporaryDirectory(prefix='cph-inject-', dir=resources) as temp:
        candidate = Path(temp) / 'PythonRuntime'
        candidate.mkdir()
        shutil.copytree(source, candidate / environment_name, symlinks=True)
        _file_manifest(candidate / environment_name)  # Reject external links before execution.
        portability = _inspect_portability(candidate / environment_name, receipt['python'])
        for name, script in service_sources.items():
            service_folder = candidate / 'Services' / name
            shutil.copytree(script.parent, service_folder, symlinks=True)
            _file_manifest(service_folder)
        # Service code is application-staged already; no repo checkout can be
        # referenced from a delivered bundle.
        files = _file_manifest(candidate)
        manifest = dict(schema=INJECTION_SCHEMA, application=application,
                        environment=environment_name, platform=current_platform(),
                        architecture=architecture(), python_version=receipt['python'],
                        requirements_fingerprint=receipt['fingerprint'],
                        services={name: f'Services/{name}/{script.name}' for name, script in service_sources.items()},
                        interpreter=f'{environment_name}/' + ('Scripts/python.exe' if current_platform() == 'win' else 'bin/python'),
                        portability=portability, files=files)
        (candidate / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        backup = None
        if output.exists():
            backup = resources / ('PythonRuntime.backup-' + application)
            if backup.exists():
                raise RuntimeError(f'Unresolved previous injection backup: {backup}')
            output.rename(backup)
        try:
            candidate.rename(output)
            # Verify the *final* location: venvs can break when moved even if
            # they worked at a temporary path. On failure restore old files.
            verify_injection(bundle)
        except Exception:
            if output.exists():
                shutil.rmtree(output)
            if backup is not None:
                backup.rename(output)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    print(f'[CPH inject] {application}: {output} ({len(manifest["files"])} verified files)', flush=True)
    return manifest


def verify_injection(bundle: Path, *, system: str | None = None) -> dict:
    resources = bundle / 'Contents/Resources' if (system or current_platform()) == 'mac' else bundle
    root = resources / 'PythonRuntime'
    manifest_path = root / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema') != INJECTION_SCHEMA:
        raise RuntimeError(f'Invalid injected Python runtime manifest: {manifest_path}')
    for rel, checksum in manifest.get('files', {}).items():
        path = inside(root, rel)
        if checksum.startswith('symlink:'):
            if not path.is_symlink() or os.readlink(path) != checksum[len('symlink:'):] or not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f'Invalid injected Python runtime symlink: {rel}')
        elif not path.is_file() or path.is_symlink() or _file_hash(path) != checksum:
            raise RuntimeError(f'Invalid injected Python runtime file: {rel}')
    actual = _file_manifest(root)
    actual.pop('manifest.json', None)  # Receipt authenticates the other files.
    if actual != manifest.get('files', {}):
        raise RuntimeError('Injected Python runtime contains added or altered files')
    checked = _inspect_portability(root / manifest['environment'], manifest['python_version'])
    if checked != manifest.get('portability'):
        raise RuntimeError('Injected Python runtime relocation report differs from receipt')
    return manifest


def _defined_c_symbol(archive: Path, name: str) -> bool | None:
    """Inspect a previously compiled POSIX archive for an actual C definition.

    `nm` also prints *undefined* references (U). Those are not sufficient:
    the macOS failure was a C++ header referencing czc_last_create_error
    while an older .a still lacked the corresponding implementation.
    Returns None when inspection is unavailable, so ordinary CMake builds
    remain possible on platforms without a suitable `nm` executable.
    """
    nm = shutil.which('nm')
    if not nm or not archive.is_file():
        return None
    inspected = subprocess.run([nm, '-g', str(archive)], capture_output=True,
                               text=True, check=False)
    if inspected.returncode != 0:
        return None
    for line in inspected.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1] in (name, '_' + name) and fields[-2] in ('T', 'W', 't', 'w'):
            return True
    return False


def _refresh_stale_zeroconf_objects(build_dir: Path, enabled: bool) -> int:
    """Invalidate only a stale Zeroconf translation unit, never all Terminal Host builds.

    CMake's timestamp-based incremental rebuild can retain objects produced
    from an earlier implementation when source archives preserve timestamps.
    Both static and shared targets compile their own copy of this source.
    """
    if not enabled or current_platform() not in ('mac', 'linux'):
        return 0
    archive = build_dir / 'libCinterForgePythonHost.a'
    if _defined_c_symbol(archive, 'czc_last_create_error') is not False:
        return 0
    removed = 0
    for target in ('CinterForgePythonHost.dir', 'CinterForgePythonHostShared.dir'):
        for suffix in ('.o', '.obj'):
            obj = build_dir / 'CMakeFiles' / target / 'src' / ('CinterZeroconf.cpp' + suffix)
            if obj.is_file():
                obj.unlink()
                removed += 1
    if removed:
        print('[Terminal Host build] An older Zeroconf library lacks czc_last_create_error; '
              f'recompiling {removed} stale mDNS object(s) from the current source', flush=True)
    return removed


def _reset_cmake_if_needed(builds: Path, directory: Path, repo: Path, *, clean: bool) -> bool:
    """Drop only this Host target's CMake intermediates when explicitly requested
    or when the cached source/build location is stale after a checkout rename.

    The reset never touches LinuxKit images, downloaded toolchains, Python
    environments, installed applications, staged artifacts, or other targets.
    """
    # Refuse symlinked CMake build trees: shutil.rmtree must never follow a
    # redirect out of the one sibling Builds root.
    if directory.is_symlink() or not directory.resolve().is_relative_to(builds.resolve()):
        raise ValueError(f'Unsafe CMake build directory: {directory}')
    if not directory.exists():
        return False
    if not directory.is_dir():
        raise ValueError(f'CMake build directory is not a directory: {directory}')
    cache = directory / 'CMakeCache.txt'
    reason = '--clean requested' if clean else None
    if not reason and cache.is_file():
        cached = {}
        for line in cache.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith(('CMAKE_HOME_DIRECTORY:INTERNAL=',
                                'CMAKE_CACHEFILE_DIR:INTERNAL=')):
                key, _, value = line.partition('=')
                cached[key] = value
        for key, expected in (('CMAKE_HOME_DIRECTORY:INTERNAL', repo),
                              ('CMAKE_CACHEFILE_DIR:INTERNAL', directory)):
            actual = cached.get(key)
            if actual and Path(actual).resolve() != expected.resolve():
                reason = f'{key} points to {actual} instead of {expected}'
                break
    if not reason:
        return False
    print(f'[Terminal Host build] Resetting only {directory} ({reason})', flush=True)
    shutil.rmtree(directory)
    return True



def _macos_sdkroot() -> Path:
    """Select Xcode's real macOS SDK for all C++ and Objective-C++ links.

    The managed Builds toolchain supplies CMake, not Apple's link SDK. With
    CMake 3.31 an unset sysroot can let C++ reuse a cached compiler while the
    first Objective-C++ compiler check fails at link time (libc++ not found).
    """
    probe = subprocess.run(['xcrun', '--sdk', 'macosx', '--show-sdk-path'],
                           capture_output=True, text=True, check=True)
    location = probe.stdout.strip()
    if not location:
        raise RuntimeError('Xcode did not report a macOS SDK path')
    sdk = Path(location).resolve()
    if not sdk.is_dir() or not (sdk / 'usr/lib/libc++.tbd').is_file():
        raise RuntimeError('Xcode macOS SDK does not contain libc++.tbd: ' + str(sdk))
    return sdk


def build_host(builds: Path, *, zeroconf: bool = True, clean: bool = False,
               application_shell: bool = False) -> Path:
    """Compile C++ process/RPC, automatically healing stale CMake source paths."""
    repo = Path(__file__).resolve().parents[1]
    directory = builds / 'Intermediates/PythonHost' / current_platform() / architecture() / 'cmake'
    _reset_cmake_if_needed(builds, directory, repo, clean=clean)
    directory.mkdir(parents=True, exist_ok=True)
    if application_shell:
        from host.webview import provision as provision_webview
        webview = provision_webview(builds)
    else:
        webview = None
    cmake = 'cmake'
    extra = []
    build_env = None
    if current_platform() == 'mac' and application_shell:
        sdk = _macos_sdkroot()
        extra.append('-DCMAKE_OSX_SYSROOT=' + str(sdk))
        print('[Terminal Host build] macOS SDK for C++/Objective-C++: ' + str(sdk), flush=True)
    if current_platform() == 'win':
        from host.toolchain import msys2_root, msys2_env
        root = msys2_root(builds)
        build_env = msys2_env(root)
        cmake = str(root / 'ucrt64/bin/cmake.exe')
        extra = ['-G', 'Ninja',
                 '-DCMAKE_MAKE_PROGRAM=' + str(root / 'ucrt64/bin/ninja.exe'),
                 '-DCMAKE_CXX_COMPILER=' + str(root / 'ucrt64/bin/g++.exe')]
    # Xcode Run requests real C++ debug symbols from this SAME maintained
    # CMake source/target; ordinary CLI Release builds retain Release flags.
    # No second native code project or compiler configuration is introduced.
    build_type = ('RelWithDebInfo' if current_platform() == 'mac' and
                  os.environ.get('CINTER_XCODE_DEBUG_NATIVE') == '1' else 'Release')
    command = [cmake, '-S', str(repo), '-B', str(directory), *extra, '-DCMAKE_BUILD_TYPE=' + build_type,
               '-DCPH_ENABLE_ZEROCONF=' + ('ON' if zeroconf else 'OFF'),
               '-DCPH_BUILDS_ROOT=' + str(builds),
               '-DCPH_BUILD_APPLICATION_SHELL=' + ('ON' if application_shell else 'OFF')]
    if webview is not None:
        command.append('-DCPH_WEBVIEW_SOURCE_DIR=' + str(webview))
        if current_platform() == 'win':
            from host.webview import provision_webview2
            sdk = provision_webview2(builds)
            command.extend(['-DMSWebView2_ROOT=' + str(sdk),
                            '-DFETCHCONTENT_FULLY_DISCONNECTED=ON'])
    _run(command, env=build_env)
    _refresh_stale_zeroconf_objects(directory, zeroconf)
    _run([cmake, '--build', str(directory), '--config', build_type], env=build_env)
    if zeroconf and current_platform() in ('mac', 'linux'):
        # Catch mixed-source artifacts before staging a broken library.
        presence = _defined_c_symbol(directory / 'libCinterForgePythonHost.a',
                                     'czc_last_create_error')
        if presence is False:
            raise RuntimeError('Compiled Zeroconf library is missing czc_last_create_error; '
                               'verify CinterZeroconf.cpp and CinterZeroconf.h are from '
                               'the same host source version')
    return directory


def host_product(builds: Path) -> Path:
    return python_root(builds) / 'Host' / current_platform() / architecture() / HOST_RELEASE


_HOST_STAGE_SCHEMA = 'cinterforge.python-host.stage.v1'
_RELEASE_FOLDER = re.compile(r'^[0-9]+(?:\.[0-9]+)+$')


def _verify_host_stage(product: Path, *, system: str, arch: str, release: str) -> dict:
    """Verify build-owned payload hashes; tolerate unrelated files and caches."""
    if product.is_symlink() or not product.is_dir():
        raise RuntimeError(f'Invalid Host stage directory: {product}')
    manifest_path = product / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError(f'Host stage manifest missing or redirected: {manifest_path}')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = manifest.get('files')
    if (manifest.get('schema') != _HOST_STAGE_SCHEMA or
            manifest.get('platform') != system or manifest.get('arch') != arch or
            manifest.get('release') != release or
            not isinstance(files, dict) or not files):
        raise RuntimeError(f'Invalid Host stage manifest: {manifest_path}')
    # Build/Stage is a mutable working lifecycle. Ignore untracked files such
    # as Python bytecode, user notes, and diagnostics. Manifest entries still
    # describe the build-owned payload and remain hash-checked below.
    for rel, digest in files.items():
        if (not isinstance(rel, str) or not rel or rel.startswith('/') or
                '..' in Path(rel).parts or '\\' in rel or
                not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise RuntimeError(f'Invalid Host stage entry: {rel!r}')
        path = product / rel
        if (path.is_symlink() or not path.is_file() or
                not path.resolve().is_relative_to(product.resolve()) or
                _file_hash(path) != digest):
            raise RuntimeError(f'Invalid Host stage payload: {path}')
    return manifest


def _retire_previous_host_stages(builds: Path, product: Path, *, system: str, arch: str) -> None:
    """Delete previous numeric releases for this platform/arch only after a new verified stage.

    Ignore non-release entries and refuse symlink redirects. Never touch other
    platform/arch stages, toolchains, environments, CMake intermediates or apps.
    """
    expected_root = python_root(builds) / 'Host' / system / arch
    if (product.parent != expected_root or expected_root.is_symlink() or
            not expected_root.resolve().is_relative_to(builds.resolve())):
        raise ValueError(f'Unsafe Host stage release root: {expected_root}')
    current = product.name
    if not _RELEASE_FOLDER.fullmatch(current):
        raise ValueError(f'Unsafe Host release version: {current}')
    current_version = tuple(int(item) for item in current.split('.'))
    # Verified promotion is a prerequisite for any destructive cleanup.
    _verify_host_stage(product, system=system, arch=arch, release=current)
    for previous in sorted(expected_root.iterdir()):
        if previous == product or not _RELEASE_FOLDER.fullmatch(previous.name):
            continue
        if tuple(int(part) for part in previous.name.split('.')) >= current_version:
            continue
        if previous.is_symlink():
            raise RuntimeError(f'Refusing symlinked older Host release: {previous}')
        if not previous.is_dir():
            continue
        manifest_path = previous / 'manifest.json'
        if manifest_path.is_symlink() or not manifest_path.is_file():
            # Unknown/partial directories are not assumed to be managed stages.
            continue
        try:
            receipt = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (receipt.get('schema') != _HOST_STAGE_SCHEMA or
                receipt.get('platform') != system or receipt.get('arch') != arch):
            continue
        print(f'[CPH stage] Retiring superseded Host stage: {previous}', flush=True)
        shutil.rmtree(previous)


def stage_host(builds: Path) -> dict:
    """Build and reconcile a version-labelled Host working stage.

    Existing staged source and binaries are replaced when Build changes them;
    unrelated files (including Python bytecode caches) are left alone. The
    manifest always describes the just-built managed payload, not every file
    that happens to be present in the working directory.
    """
    build = builds / 'Intermediates/PythonHost' / current_platform() / architecture() / 'cmake'
    repo = Path(__file__).resolve().parents[1]
    system = current_platform()
    # MSYS2 UCRT64/MinGW-w64 emits GNU .a/.dll.a names. MSVC uses .lib.
    # Accept exactly one compiler-family layout and stage it intact; do not
    # accidentally combine artifacts from two incompatible Windows ABIs.
    if system == 'win':
        mingw = (build / 'libCinterForgePythonHostStatic.a').is_file()
        msvc = (build / 'CinterForgePythonHostStatic.lib').is_file()
        if mingw == msvc:
            raise RuntimeError('Expected exactly one Windows static library toolchain layout (UCRT64 GNU or MSVC)')
        static_name = ('libCinterForgePythonHostStatic.a' if mingw else
                       'CinterForgePythonHostStatic.lib')
        import_name = ('libCinterForgePythonHost.dll.a' if mingw else 'CinterForgePythonHost.lib')
    else:
        static_name = 'libCinterForgePythonHost.a'
        import_name = None
    shared_name = {'mac': 'libCinterForgePythonHost.dylib', 'linux': 'libCinterForgePythonHost.so',
                   'win': 'CinterForgePythonHost.dll'}[system]
    executable_suffix = '.exe' if system == 'win' else ''
    names = [static_name, shared_name, 'cinter-python-host' + executable_suffix,
             'cinter-python-host-smoke' + executable_suffix,
             'cinter-host-utilities-smoke' + executable_suffix]
    cache = build / 'CMakeCache.txt'
    if not cache.is_file():
        raise RuntimeError(f'Host CMake configuration missing: {cache}; run ./Cinter host build')
    zeroconf = 'CPH_ENABLE_ZEROCONF:BOOL=ON' in cache.read_text()
    if zeroconf:
        names.extend(('cinter-zeroconf-smoke' + executable_suffix,
                      'cinter-zeroconf' + executable_suffix))
    # One product per expected name. Windows import library is versioned
    # alongside the shared DLL; static library has a distinct name.
    if system == 'win':
        names.append(import_name)
    app_shell = 'CPH_BUILD_APPLICATION_SHELL:BOOL=ON' in cache.read_text()
    if app_shell:
        names.append('CinterForgeTerminal' + executable_suffix)
    located = {}
    for name in names:
        matches = [path for path in build.rglob(name) if path.is_file()]
        if len(matches) != 1:
            raise RuntimeError(f'Expected one compiled {name}, found {len(matches)} under {build}')
        located[name] = matches[0]
    product = host_product(builds)
    product.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='python-host-stage-', dir=product.parent) as temp:
        candidate = Path(temp) / HOST_RELEASE
        for directory in ('lib', 'bin', 'include', 'python', 'webapp', 'lifecycle/host'):
            (candidate / directory).mkdir(parents=True)
        for name, source in located.items():
            destination = ('bin' if (name.startswith('cinter-') and name.endswith(executable_suffix))
                           or name == 'CinterForgeTerminal' + executable_suffix
                           or name.endswith('.dll') else 'lib')
            shutil.copy2(source, candidate / destination / name)
        headers = ['CinterPythonHost.h', 'CinterPythonHostC.h', 'CinterHostUtilities.h']
        if zeroconf:
            headers.append('CinterZeroconf.h')
        for name in headers:
            shutil.copy2(repo / 'include' / name, candidate / 'include' / name)
        shutil.copy2(repo / 'host/worker.py', candidate / 'python/worker.py')
        # Apps call this staged lifecycle during their Setup and Stage; the
        # host checkout is never used as an inter-repository dependency.
        shutil.copy2(repo / 'Cinter', candidate / 'lifecycle/Cinter')
        for name in ('__init__.py', 'manager.py', 'bootstrap_bundle.py', 'portable_python.py', 'webview.py', 'powershell.py', 'toolchain.py', 'win_cross.py', 'cross_build.py', 'environment_inputs.py'):
            shutil.copy2(repo / 'host' / name, candidate / 'lifecycle/host' / name)
        # Publisher and proven Companion R2 transport form one reusable SDK tool.
        (candidate / 'lifecycle/host/publishing').mkdir(parents=True)
        for name in ('__init__.py', 'release.py', 'cloudflare_r2_sync.py'):
            shutil.copy2(repo / 'host/publishing' / name,
                         candidate / 'lifecycle/host/publishing' / name)
        # Obsolete standalone HTTP example is not part of the Terminal stage.
        # CinterForgeTerminal is a separately deployable, authenticated HTTP/PTy
        # application service. Never omit its JS, terminal bindings or EULA
        # manager from the immutable artifact consumed by product applications.
        for name in ('CinterForgeTerminal.py', 'if_titles.json', 'mac_games.json', 'index.html',
                     'terminal_styles.json', 'terminal.js', 'terminal.css', 'startup.html', 'startup.js'):
            shutil.copy2(repo / 'webapp' / name, candidate / 'webapp' / name)
        # JOSHUA remains self-contained (including its AGPL license) and is
        # embedded in the same authenticated Terminal origin, not a repo clone.
        shutil.copytree(repo / 'webapp/joshua', candidate / 'webapp/joshua')
        # Shared Mac/Windows/Linux Launcher UI and replaceable PNGs are part
        # of the signed/staged Host closure, never read from a sibling repo.
        shutil.copytree(repo / 'webapp/launcher', candidate / 'webapp/launcher',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
        files = {item.relative_to(candidate).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
                 for item in sorted(candidate.rglob('*')) if item.is_file()}
        receipt = {'schema': _HOST_STAGE_SCHEMA, 'platform': system,
                   'arch': architecture(), 'release': HOST_RELEASE, 'api_version': 1,
                   'features': {'zeroconf': zeroconf, 'application_shell': app_shell}, 'files': files}
        (candidate / 'manifest.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
        if product.exists():
            if product.is_symlink() or not product.is_dir():
                raise RuntimeError('Unsafe Host stage destination: ' + str(product))
            # Prepare the new candidate before modifying the working stage.
            # Do not reject an existing release merely because the build changed
            # or an application wrote a cache file into the stage.
            refreshed = 0
            for rel, digest in files.items():
                source = candidate / rel
                installed = product / rel
                relative = Path(rel)
                parent = product
                for part in relative.parts[:-1]:
                    parent = parent / part
                    if parent.is_symlink():
                        raise RuntimeError('Unsafe Host stage parent symlink: ' + str(parent))
                    if parent.exists() and not parent.is_dir():
                        # Preserve the unexpected object outside the package,
                        # rather than letting it prevent a managed build.
                        quarantine = builds / 'Intermediates/PythonHost/StageConflicts'
                        quarantine.mkdir(parents=True, exist_ok=True)
                        backup = Path(tempfile.mkdtemp(prefix='previous-', dir=quarantine)) / part
                        parent.replace(backup)
                    parent.mkdir(exist_ok=True)
                if (installed.is_file() and not installed.is_symlink() and
                        _file_hash(installed) == digest):
                    continue
                if installed.is_dir() and not installed.is_symlink():
                    quarantine = builds / 'Intermediates/PythonHost/StageConflicts'
                    quarantine.mkdir(parents=True, exist_ok=True)
                    backup = Path(tempfile.mkdtemp(prefix='previous-', dir=quarantine)) / installed.name
                    installed.replace(backup)
                elif installed.is_symlink():
                    installed.unlink()
                fd, temporary_name = tempfile.mkstemp(prefix='Staging-', dir=installed.parent)
                os.close(fd)
                temporary_file = Path(temporary_name)
                try:
                    shutil.copy2(source, temporary_file)
                    temporary_file.replace(installed)
                finally:
                    temporary_file.unlink(missing_ok=True)
                refreshed += 1
            # Commit the inventory last, after the build-owned payload is in
            # place; any interrupted Stage can be repaired by running it again.
            manifest_target = product / 'manifest.json'
            if manifest_target.is_dir() and not manifest_target.is_symlink():
                quarantine = builds / 'Intermediates/PythonHost/StageConflicts'
                quarantine.mkdir(parents=True, exist_ok=True)
                backup = Path(tempfile.mkdtemp(prefix='previous-', dir=quarantine)) / 'manifest.json'
                manifest_target.replace(backup)
            fd, temporary_name = tempfile.mkstemp(prefix='Staging-', dir=product)
            os.close(fd)
            temporary_manifest = Path(temporary_name)
            try:
                temporary_manifest.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
                temporary_manifest.replace(product / 'manifest.json')
            finally:
                temporary_manifest.unlink(missing_ok=True)
            print(f'[CPH stage] Reconciled {refreshed} managed files in {product}', flush=True)
        else:
            candidate.rename(product)
    _retire_previous_host_stages(builds, product, system=system, arch=architecture())
    print(f'[CPH stage] {product}', flush=True)
    return receipt


def test_host(builds: Path) -> None:
    """Verify the entire staged artifact and exercise native RPC and queues."""
    product = host_product(builds)
    manifest = json.loads((product / 'manifest.json').read_text())
    if manifest.get('schema') != 'cinterforge.python-host.stage.v1':
        raise RuntimeError('Invalid host stage manifest')
    for rel, checksum in manifest['files'].items():
        path = inside(product, rel)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise RuntimeError(f'Invalid staged Python Host artifact: {rel}')
    suffix = '.exe' if os.name == 'nt' else ''
    _run([str(product / 'bin' / ('cinter-python-host-smoke' + suffix)),
          sys.executable, str(product / 'python/worker.py')])
    _run([str(product / 'bin' / ('cinter-host-utilities-smoke' + suffix))])
    if manifest.get('features', {}).get('zeroconf'):
        _run([str(product / 'bin' / ('cinter-zeroconf-smoke' + suffix))])
    if manifest.get('features', {}).get('application_shell'):
        _run([str(product / 'bin' / ('CinterForgeTerminal' + suffix)), '--help'])
    _run([str(product / 'bin' / ('cinter-python-host' + suffix)),
          '--python', sys.executable, '--worker', str(product / 'python/worker.py'),
          '--method', 'ping'])
    library = (product / 'bin/CinterForgePythonHost.dll' if os.name == 'nt' else
               product / 'lib' / ('libCinterForgePythonHost.dylib' if current_platform() == 'mac'
                                  else 'libCinterForgePythonHost.so'))
    env = dict(os.environ, CINTER_BUILDS_ROOT=str(builds), CPH_HOST_LIBRARY=str(library), PYTHONDONTWRITEBYTECODE='1',
               CPH_ZEROCONF_ENABLED='1' if manifest.get('features', {}).get('zeroconf') else '0')
    _run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
         cwd=Path(__file__).resolve().parents[1], env=env)
    # A successful test run is bound to the exact staged artifact; the publisher
    # will reject stale receipts. Under Wine this is NOT native Windows proof.
    execution = 'native'
    if current_platform() == 'win':
        if any(os.environ.get(key) for key in ('WINEPREFIX', 'WINELOADERNOEXEC', 'CINTERFORGE_TEST_UNDER_WINE')):
            execution = 'wine'
        else:
            try:
                import ctypes
                if getattr(ctypes.windll.ntdll, 'wine_get_version', None):
                    execution = 'wine'
            except (AttributeError, OSError):
                pass
    from host.publishing.release import TEST_SCHEMA, _digest
    receipt = {'schema': TEST_SCHEMA, 'result': 'passed',
               'platform': current_platform(), 'arch': architecture(),
               'release': HOST_RELEASE, 'execution': execution,
               'stage_manifest_sha256': _digest(product / 'manifest.json')}
    path = builds / 'Python/Host/Tests' / current_platform() / architecture() / HOST_RELEASE / 'receipt.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_suffix('.json.tmp')
    candidate.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(candidate, path)
    print(f'[CPH test] {execution} verified: {path}', flush=True)


# The developer LAN example is intentionally tiny: one server.py and one
# index.html. Process state and logs are writable runtime data under Builds.
# The old unauthenticated LAN HTTP example and its mDNS process manager were
# deliberately removed. Terminal serves only its authenticated loopback app.


def call_service(builds: Path, projects: dict, name: str, method: str,
                 params_json: str, timeout_ms: int) -> int:
    """Invoke a staged named service through the native host, not its source."""
    command, _ = service_command(builds, projects, name)
    binary = host_product(builds) / 'bin' / ('cinter-python-host.exe' if os.name == 'nt' else 'cinter-python-host')
    worker = host_product(builds) / 'python/worker.py'
    if not binary.is_file() or not worker.is_file():
        raise FileNotFoundError('Python Host artifact missing; run ./Cinter host all')
    if command[3:]:
        raise ValueError('Native RPC service must use handler module without command-line arguments')
    # command layout: [interpreter, '-u', staged_script]
    return subprocess.call([str(binary), '--python', command[0], '--worker', str(worker),
                            '--service', command[2], '--method', method,
                            '--params', params_json, '--timeout-ms', str(timeout_ms)])

def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('action', choices=('setup', 'check', 'run', 'call', 'list', 'inject', 'verify-app', 'build', 'stage', 'test', 'all', 'publish', 'inject-bundle', 'verify-bundle', 'stage-requirements'))
    cli.add_argument('name', nargs='?', help='Environment for setup/check; service for run')
    cli.add_argument('--builds', type=Path, help='Shared sibling Builds root')
    cli.add_argument('--manifest', type=Path, help='Projects manifest already staged under Builds/Python')
    cli.add_argument('--requirements-source', type=Path, help='Owning application requirements file to stage inside Builds/Python')
    cli.add_argument('--app-bundle', type=Path, help='Application already staged inside Builds (for inject/verify-app/inject-bundle)')
    cli.add_argument('--target-platform', choices=('mac', 'linux', 'win'), help='Internal platform for staged runtime injection')
    cli.add_argument('--target-arch', choices=('arm64', 'x64'), help='Internal architecture for staged runtime injection')
    cli.add_argument('--replace', action='store_true', help='Replace only a previously CPH-managed injected runtime')
    cli.add_argument('--bootstrap', action='store_true', help='Provision missing pinned Python toolchain')
    cli.add_argument('--clean', action='store_true', help='Reset only the current Host CMake target when building (not LinuxKit/Python/toolchains/staged outputs)')
    cli.add_argument('--application-shell', action='store_true',
                     help='Compile and require the real native WebView Application Shell')
    cli.add_argument('--without-zeroconf', action='store_true',
                     help='Build Python hosting without optional multicast DNS-SD')
    cli.add_argument('--adopt-legacy', action='store_true', help='Verify and copy Builds/cinterenv non-destructively')
    cli.add_argument('--method', default='ping', help='Method for native C++ RPC call')
    cli.add_argument('--params', default='{}', help='JSON params for native RPC call')
    cli.add_argument('--timeout-ms', type=int, default=30000, help='RPC timeout in milliseconds')
    cli.add_argument('--release', default=HOST_RELEASE, help='Versioned staged release to publish')
    cli.add_argument('--product', choices=('terminal', 'host-sdk'), default='terminal', help='Publish staged Application, or development-only Host SDK')
    cli.add_argument('--channel', choices=('dev', 'production'), default='dev', help='Cloudflare bucket selection')
    cli.add_argument('--config', type=Path, default=Path.home() / 'dev/config.json', help='External R2 provider configuration')
    cli.add_argument('--provider', default='auto', help='Existing Cloudflare provider name, or auto for selected bucket')
    cli.add_argument('--dry-run', action='store_true', help='Verify and package locally without network or credentials')
    cli.add_argument('--approve-production', action='store_true', help='Explicitly approve production promotion after native tests')
    args = cli.parse_args(argv)
    if args.application_shell and args.action not in ('build', 'all'):
        cli.error('--application-shell is only supported with build or all')
    if args.clean and args.action not in ('build', 'all'):
        cli.error('--clean is only supported with build or all')
    repo = Path(__file__).resolve().parents[1]
    builds = builds_root(repo, args.builds)
    try:
        if args.action == 'stage-requirements':
            if not args.name or not args.requirements_source:
                cli.error('stage-requirements requires <environment> --requirements-source <source file>')
            from host.environment_inputs import stage_requirements
            print(json.dumps(stage_requirements(builds, args.name, args.requirements_source), indent=2))
            return 0
        if args.action == 'publish':
            if args.name != 'all':
                cli.error('publish requires all: ./Cinter host publish all --channel dev|production')
            from host.publishing.release import publish
            publish(builds, args.release, args.channel, config=args.config,
                    provider=args.provider, dry_run=args.dry_run, product=args.product,
                    approve_production=args.approve_production)
            return 0
        if args.action in ('inject-bundle', 'verify-bundle'):
            from host.bootstrap_bundle import inject_bundle, verify_bundle, RELEASE, _native
            if args.target_platform or args.target_arch:
                if not args.target_platform or not args.target_arch:
                    cli.error('both --target-platform and --target-arch are required')
                selected = (args.target_platform, args.target_arch)
            else:
                selected = _native()
            runtime = builds / 'CinterForgeTerminal/Bundles' / selected[0] / selected[1] / RELEASE
            if args.action == 'verify-bundle':
                print(json.dumps(verify_bundle(runtime), indent=2, sort_keys=True))
            else:
                if args.app_bundle is None:
                    cli.error('inject-bundle requires --app-bundle')
                print(f'[CPH inject-bundle] {inject_bundle(builds, runtime, args.app_bundle)}', flush=True)
            return 0
        if args.action == 'verify-app':
            if not args.app_bundle:
                cli.error('--app-bundle is required for verify-app')
            if not args.app_bundle.resolve().is_relative_to(builds):
                raise ValueError('--app-bundle must be staged inside Builds')
            print(json.dumps(verify_injection(args.app_bundle.resolve()), indent=2))
            return 0
        if args.action in ('build', 'stage', 'test', 'all'):
            if args.action in ('build', 'all'):
                build_host(builds, zeroconf=not args.without_zeroconf, clean=args.clean,
                           application_shell=args.application_shell)
            if args.action in ('stage', 'all'):
                stage_host(builds)
            if args.action in ('test', 'all'):
                test_host(builds)
            return 0
        if args.action == 'setup' and args.name == 'all':
            # Provision the pinned *portable* Terminal Python, not a host venv.
            # This closes the missing cinterterminalenv blocker on every native
            # target without modifying Server/Converter's existing environments.
            from host.portable_python import provision as provision_python
            provision_python(builds, current_platform(), architecture())
            from host.bootstrap_bundle import setup_all
            print(json.dumps(setup_all(builds), indent=2, sort_keys=True))
            return 0
        data = read_projects(builds, args.manifest.resolve() if args.manifest else None)
        if args.action == 'inject':
            if not args.name or not args.app_bundle:
                cli.error('inject requires an application name and --app-bundle')
            inject_application(builds, data, args.name, args.app_bundle, replace=args.replace)
            return 0
        if args.action == 'list':
            print(json.dumps({'environments': sorted(data['environments']), 'services': sorted(data['services'])}, indent=2))
            return 0
        if not args.name:
            cli.error('An environment or service name is required')
        if args.action in ('setup', 'check'):
            profile = data['environments'].get(args.name)
            if not isinstance(profile, dict):
                raise ValueError(f'Unknown environment: {args.name}')
            if args.action == 'setup':
                setup_environment(builds, args.name, profile, bootstrap=args.bootstrap,
                                  adopt=args.adopt_legacy)
            else:
                executable = python_executable(environment_dir(builds, args.name))
                print(f'[CPH check] {args.name} Python {_verify_interpreter(executable, version=profile.get("python", PYTHON_VERSION))}')
        elif args.action == 'call':
            return call_service(builds, data, args.name, args.method, args.params, args.timeout_ms)
        else:
            command, folder = service_command(builds, data, args.name)
            return subprocess.call(command, cwd=folder, env=dict(os.environ, PYTHONNOUSERSITE='1', PYTHONUNBUFFERED='1'))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as failure:
        print(f'[CPH {args.action}] FAIL: {failure}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
