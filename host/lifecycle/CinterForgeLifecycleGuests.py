"""CinterForgeLifecycleGuests — independent Test acceptance of Bash and VNC.

PURPOSE
    Test starts the Setup-provisioned x86-64 Debian Docker image under the
    lifecycle-owned t Colima profile. This module intentionally duplicates
    the small guest-container/PTY/VNC implementation from the application
    runtime so neither layer imports or starts the other's Python service.

MAINTAINED ENTRY POINTS
    _image_archive              Verify the sealed OCI manifest and tar hash.
    _start_guest_locked         Reuse/start one labeled Debian guest container.
    _verify_guest_bash          Check real Linux and MSYS2/Wine Bash execution.
    _activate_guest_vnc         Initialize real Xvfb/x11vnc/noVNC endpoints.
    _verify_guest_vnc           Verify RFB and HTTP handshakes.
    _lifecycle_guest_shell_command  Construct actual interactive Docker PTYs.
    _verify_persistent_guest_shell  Check a running Bash, not an echo simulation.
    CinterForgeLifecycleGuestShell  Own Test's temporary PTY, output and cleanup.

OWNERSHIP
    Only host/lifecycle calls these functions. They never import webapp,
    runtime, installed-app SDK receipts, or user Steam paths. The same pinned
    image, Docker labels, /host/mac mounts, Bash commands and VNC ports are
    retained from Phase 3. No Setup/provisioning occurs in Test.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import pty
import re
import select
import signal
import subprocess
import threading
import time
import uuid
import fcntl
import struct
import termios

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / 'webapp'
PROFILE = 't'
_LAUNCHER_PORTS = {'linux': 20601, 'win': 20602}
_launcher_lock = threading.RLock()


def _app_devtools_progress(message: str) -> None:
    print('[LIFECYCLE GUEST] ' + message, flush=True)


def _mac_host_mounts() -> list[tuple[Path, str]]:
    """Mirror the one nonoverlapping writable root share for this lifecycle."""
    return [(Path('/'), '/host/mac')]


def _run_checked(command: list[str], env: dict[str, str], *, title: str,
                 timeout: int, cancel_event=None) -> None:
    """Run the Setup-provisioned executable with live output and bounded wait."""
    start = time.monotonic()
    last = start
    _app_devtools_progress(title + ' START')
    with subprocess.Popen(command, env=env) as proc:
        try:
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError(title + ' cancelled')
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if elapsed - (last - start) >= 15:
                    _app_devtools_progress(title + ' WORKING (' + str(int(elapsed)) + 's)')
                    last = time.monotonic()
                time.sleep(.25)
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(title + ' exited with ' + str(proc.returncode))
    _app_devtools_progress(title + ' PASS (' + str(round(time.monotonic()-start,1)) + 's)')


class CinterForgeLifecycleGuestShell:
    """One actual Docker exec PTY for Test, never an HTTP TerminalSession.

    Session input/output and HUP cleanup are local to Test. We do not create
    a server, copy sessions into Steam, or modify the application's PTY list.
    """
    def __init__(self, command: list[str], *, env: dict[str, str],
                 working_directory: Path, output_log: Path):
        self.closed = False
        self.fd, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
        output_log.parent.mkdir(parents=True, exist_ok=True)
        self.log = output_log.open('ab')
        try:
            self.process = subprocess.Popen(command, cwd=working_directory,
                                            env=env, stdin=slave, stdout=slave,
                                            stderr=slave, start_new_session=True,
                                            close_fds=True)
        finally:
            os.close(slave)

    def poll(self, wait_ms: int) -> tuple[bytes, bool]:
        if self.closed:
            return b'', True
        ready, _, _ = select.select([self.fd], [], [],
                                    max(0, min(int(wait_ms), 750)) / 1000.0)
        if ready:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b''
            if data:
                self.log.write(data)
                self.log.flush()
                return data, False
        return b'', self.process.poll() is not None

    def write(self, message: str) -> None:
        if self.closed:
            raise RuntimeError('Lifecycle guest PTY closed')
        os.write(self.fd, message.encode('utf-8'))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGHUP)
                self.process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=4)
        os.close(self.fd)
        self.log.close()

def _image_archive(sdk: Path, kind: str) -> tuple[Path, str]:
    if kind not in ('linux', 'win'):
        raise ValueError('Unknown managed guest platform')
    # The same verified amd64 Debian image supplies both native Linux and
    # Wine/MSYS2 Windows tools. Two UI platforms, ONE running container.
    folder = sdk / 'WindowsCross'
    selected_file = folder / 'active-image.json'
    if selected_file.exists():
        # DevTools Build commits one exact OCI image. Steam Stage can retain
        # older unowned files, so sorting *.json by name is not deterministic.
        if selected_file.is_symlink():
            raise RuntimeError('Active DevTools guest image selection is redirected')
        try:
            active = json.loads(selected_file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise RuntimeError('Invalid active DevTools guest image selector') from exc
        if (active.get('schema') != 'cinterforge.terminal.active-guest-image.v1' or
                active.get('wine_version') != 'wine-11.15' or
                not re.fullmatch(r'[0-9a-f]{64}', str(active.get('dockerfile_sha256', '')))):
            raise RuntimeError('Unsupported active DevTools guest image selector')
        digest = active['dockerfile_sha256']
        receipt_file = folder / (digest[:16] + '.json')
        tar = folder / (digest[:16] + '.tar')
        if receipt_file.is_symlink() or tar.is_symlink() or not tar.is_file():
            raise RuntimeError('Selected DevTools guest OCI image is missing or redirected')
        try:
            receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise RuntimeError('Cannot read selected DevTools OCI manifest') from exc
        if (any(receipt.get(k) != active.get(k) for k in
                ('dockerfile_sha256', 'image', 'archive_sha256', 'wine_version')) or
                not isinstance(active.get('image'), str) or
                not active['image'].startswith('cinterforge-terminal-windows-cross:')):
            raise RuntimeError('Active OCI image does not match its sealed manifest')
        checksum = hashlib.sha256()
        with tar.open('rb') as src:
            for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
                checksum.update(block)
        if checksum.hexdigest() != active['archive_sha256']:
            raise RuntimeError('Selected DevTools OCI image differs from its sealed SHA-256')
        return tar, active['image']
    # Legacy DevTools release (no explicit selection). New releases MUST
    # include the active selector to prevent a stale-image regression.
    receipts = sorted(folder.glob('*.json'))
    for receipt in receipts:
        if receipt.is_symlink():
            continue
        try:
            record = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        # Archive selection must not silently fall back to an old stock-Wine
        # image when multiple valid hashes remain in installed DevTools.
        if record.get('wine_version') != 'wine-11.15':
            continue
        digest = record.get('dockerfile_sha256', '')
        name = record.get('image')
        tar = folder / (str(digest)[:16] + '.tar')
        if not tar.is_file() or tar.is_symlink() or not name or '/' in str(name).split(':')[0]:
            continue
        expected = record.get('archive_sha256')
        if not isinstance(expected, str) or len(expected) != 64:
            continue
        checksum = hashlib.sha256()
        with tar.open('rb') as src:
            for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
                checksum.update(block)
        if checksum.hexdigest() != expected:
            raise RuntimeError('DevTools OCI image differs from its recorded SHA-256: ' + str(tar))
        return tar, str(name)
    raise RuntimeError(f'Unified DevTools has no verified {kind} container image')

def _lifecycle_guest_shell_command(sdk: Path, kind: str, workspace: Path | None) -> list[str]:
    """One real interactive Docker/Wine Bash, shared by all Code-OSS attachments."""
    if kind not in ('linux', 'win'):
        raise ValueError('Unknown persistent guest Bash kind')
    docker = str(sdk / 'mac/env/bin/docker')
    cwd = '/host/mac' + str(workspace) if workspace else '/'
    command = [docker, 'exec', '-it', *_launcher_guest_env(kind),
               '--workdir', cwd, _guest_name(kind)]
    if kind == 'linux':
        return command + ['/bin/bash', '--noprofile', '-i']
    # Use the exact profile-free MSYS2 entry point already proven by
    # _verify_guest_bash().  --login invokes /etc/profile, whose first-start
    # pacman-key hook fails against the deliberately read-only /msys2 bind.
    # Establish coreutils/UCRT64 PATH explicitly, then replace the bootstrap
    # process with ONE long-lived interactive Bash on the existing Docker PTY.
    # No machine-wide initialization, pacman setup, or second guest is needed.
    bootstrap = ('export PATH=/usr/bin:/ucrt64/bin:$PATH; '
                 'exec /usr/bin/bash --noprofile --norc -i')
    return command + ['wine', r'Z:\msys2\usr\bin\bash.exe',
                      '--noprofile', '--norc', '-c', bootstrap]

def _verify_persistent_guest_shell(shell: CinterForgeLifecycleGuestShell, kind: str, *, timeout: float = 120) -> None:
    """Exercise the actual long-lived PTY, not a separate --version probe."""
    nonce = uuid.uuid4().hex
    # Split the marker in the transmitted command: terminal echo is NOT proof
    # of command execution. Only the command's expanded output contains it.
    marker = ('CINTER_SHELL_READY:' + nonce).encode('ascii')
    shell.write('printf "CINTER_SHELL_READY:%s%s\\n" "' + nonce[:16] +
                '" "' + nonce[16:] + '"\n')
    started = time.monotonic()
    deadline = started + timeout
    next_progress = started + 15
    label = 'Windows' if kind == 'win' else 'Linux'
    _app_devtools_progress(label + ' persistent Bash PTY START')
    output = bytearray()
    while time.monotonic() < deadline:
        data, closed = shell.poll(500)
        output.extend(data)
        if marker in output:
            # Keep one real shell prompt buffered for Code-OSS to render after
            # the initial splash; there is no new Bash when the tab attaches.
            shell.write('\n')
            _app_devtools_progress(label + ' persistent Bash PTY PASS (' +
                                   str(round(time.monotonic() - started, 1)) + 's)')
            return
        if len(output) > 4096:
            del output[:-4096]
        now = time.monotonic()
        if now >= next_progress:
            _app_devtools_progress(label + ' persistent Bash PTY WORKING (' +
                                   str(int(now - started)) + 's)')
            next_progress = now + 15
        if closed or shell.process.poll() is not None:
            break
    raise RuntimeError(kind + ' persistent Bash did not execute PTY readiness command: ' +
                       output.decode('utf-8', 'replace')[-700:])

def _guest_name(kind: str) -> str:
    if kind not in ('linux', 'win'):
        raise ValueError('Invalid Terminal guest')
    return 'cinterforge-terminal-t-debian'

def _guest_host_binds(sdk: Path, kind: str, workspace: Path | None) -> list[str]:
    """All accessible Mac drives, not a sandbox/allowlist; stable startup cwd."""
    binds = ['--mount', 'type=bind,src=/host/mac,dst=/host/mac']
    mounts = _mac_host_mounts()
    for _, guest in mounts[1:]:
        binds += ['--mount', 'type=bind,src=' + guest + ',dst=' + guest]
    if workspace is not None:
        binds += ['--mount', 'type=bind,src=/host/mac' + str(workspace) + ',dst=/workspace']
    if kind in ('linux', 'win'):
        msys = sdk / 'Windows/MSYS2'
        binds += ['--mount', 'type=bind,src=/host/mac' + str(msys) + ',dst=/msys2,readonly',
                  '--tmpfs', '/msys2/tmp:rw,nosuid,nodev,size=64m']
    return binds

def _docker_call(docker: str, env: dict[str, str], *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run([docker, *args], env=env, check=False, text=True,
                          capture_output=True, timeout=timeout)

def _guest_container_failure_evidence(docker: str, env: dict[str, str], name: str) -> str:
    """Collect exit cause before cleanup destroys the only useful evidence.

    App-installed failures may only be reported through the Steam Server log:
    unlike developer Setup, a customer has no access to the builder or a
    persistent Docker container.  Probe only this known, owned container and
    bound each read; never retry/reinstall anything during diagnosis.
    """
    result = _docker_call(
        docker, env, 'container', 'inspect', '--format',
        'status={{.State.Status}} exit={{.State.ExitCode}} '
        'oom={{.State.OOMKilled}} error={{.State.Error}} '
        'finished={{.State.FinishedAt}}', name, timeout=15)
    if result.returncode:
        state = ('inspect unavailable: ' +
                 (result.stderr or result.stdout or 'no Docker response').strip()[-350:])
    else:
        state = (result.stdout or 'no Docker state returned').strip()[-500:]
    logs = _docker_call(docker, env, 'logs', '--tail', '30', name, timeout=15)
    output = ((logs.stdout or '') + '\n' + (logs.stderr or '')).strip()[-1400:]
    if logs.returncode and not output:
        output = 'Docker logs unavailable (exit ' + str(logs.returncode) + ')'
    return 'container ' + name + ' ' + state + '; last Docker output: ' + (output or '(empty)')

def _start_guest_locked(sdk: Path, env: dict[str, str], docker: str,
                        kind: str, workspace: Path | None) -> None:
    name = _guest_name(kind)
    tar, image = _image_archive(sdk, kind)
    if _docker_call(docker, env, 'image', 'inspect', image).returncode:
        _run_checked([docker, 'load', '--input', str(tar)], env,
                     title='Loading verified ' + kind + ' OCI image', timeout=1200)
    existing = _docker_call(docker, env, 'container', 'inspect', '--format',
                            '{{index .Config.Labels "io.cinterforge.terminal"}}|{{.State.Running}}', name)
    if existing.returncode == 0:
        owner, _, running = existing.stdout.strip().partition('|')
        if owner != PROFILE:
            raise RuntimeError('Existing Docker container is not Terminal-owned: ' + name)
        if running == 'true':
            # A named running container is not proof Bash actually works.
            # Wine needs the guest display before it initializes its prefix.
            try:
                if kind == 'win':
                    _ensure_wine_display(docker, env, name)
                _verify_guest_bash(docker, env, kind, name)
                _activate_guest_vnc(docker, env, kind, name)
            except Exception as error:
                # The one shared container can exit between inspect and exec.
                # Keep its exit evidence even on the reuse path.
                try:
                    evidence = _guest_container_failure_evidence(docker, env, name)
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as diagnostic_error:
                    evidence = 'Docker exit evidence unavailable: ' + str(diagnostic_error)
                raise RuntimeError(str(error) + '; ' + evidence) from error
            return
        _stop_owned_guests_locked_one(docker, env, name)
    environment = ['-e', 'MSYSTEM=UCRT64', '-e', 'CHERE_INVOKING=1',
                   '-e', 'HOME=/tmp', '-e', 'TMPDIR=/tmp', '-e', 'TMP=/tmp', '-e', 'TEMP=/tmp']
    # Both loopback VNC endpoints belong to ONE persistent guest container.
    # Setup includes GUI dependencies; the two displays remain independent.
    args = [docker, 'run', '-d', '--restart', 'no', '--name', name,
            '--label', 'io.cinterforge.terminal=' + PROFILE,
            '--platform', 'linux/amd64',
            '--publish', f'127.0.0.1:{_LAUNCHER_PORTS["linux"]}:6080',
            '--publish', f'127.0.0.1:{_LAUNCHER_PORTS["win"]}:6081',
            *_guest_host_binds(sdk, kind, workspace),
            *environment, '--workdir', '/workspace' if workspace else '/',
            # Do not use a shell-owned background sleep/wait loop as PID 1.
            # An interactive shell is created only by an explicit docker exec;
            # this foreground keepalive has no inherited stdin or shell traps.
            '--entrypoint', '/bin/sleep', image, 'infinity']
    _run_checked(args, env, title='Starting one reusable ' + kind + ' guest', timeout=120)
    try:
        if kind == 'win':
            _ensure_wine_display(docker, env, name)
        _verify_guest_bash(docker, env, kind, name)
        _activate_guest_vnc(docker, env, kind, name)
    except Exception as error:
        # Docker exec only says "container ... is not running". Capture the
        # real exit status / OOM flag / entrypoint stderr BEFORE removing it.
        # There is exactly one named Debian container for Linux and Windows.
        try:
            evidence = _guest_container_failure_evidence(docker, env, name)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as diagnostic_error:
            evidence = 'Docker exit evidence unavailable: ' + str(diagnostic_error)
        # Linux and Windows are two capabilities of THIS SAME container.
        # A broken Windows Wine/VNC probe must not delete the functioning
        # Linux Bash container (and vice versa).  Only remove it when Docker
        # confirms that the shared container has actually stopped.
        try:
            running = _docker_call(docker, env, 'container', 'inspect', '--format',
                                   '{{.State.Running}}', name, timeout=15)
            if running.returncode or running.stdout.strip() != 'true':
                _stop_owned_guests_locked_one(docker, env, name)
            else:
                evidence += '; keeping running shared guest for the other platform'
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as cleanup_error:
            evidence += '; cleanup inspection failed: ' + str(cleanup_error)
        raise RuntimeError(str(error) + '; ' + evidence) from error
    except BaseException:
        _stop_owned_guests_locked_one(docker, env, name)
        raise

def _ensure_wine_display(docker: str, env: dict[str, str], name: str) -> None:
    """Start the owned headless framebuffer *before* Wine first-run.

    It stays in the same Windows guest container and is reused by Launcher.
    No GUI is opened in the app until the Launcher tab is explicitly chosen.
    """
    script = r'''set -eu
command -v Xvfb >/dev/null
command -v xdpyinfo >/dev/null
if ! DISPLAY=:100 xdpyinfo >/dev/null 2>&1; then
  nohup Xvfb :100 -screen 0 1600x900x24 -nolisten tcp >/tmp/cinter-launcher-win-xvfb.log 2>&1 </dev/null &
fi
for n in 1 2 3 4 5 6 7 8 9 10; do
  if DISPLAY=:100 xdpyinfo >/dev/null 2>&1; then exit 0; fi
  sleep 1
done
cat /tmp/cinter-launcher-win-xvfb.log >&2 || true
exit 18
'''
    result = _docker_call(docker, env, 'exec', name, '/bin/sh', '-c', script, timeout=35)
    if result.returncode:
        raise RuntimeError('Windows guest framebuffer failed before Wine: ' +
                           (result.stderr or result.stdout or '').strip()[-700:])

def _guest_verification_probe(docker: str, env: dict[str, str], name: str,
                              stage: str, command: list[str], *, timeout: int,
                              expected: str = '') -> subprocess.CompletedProcess:
    """Verify a real owned guest with bounded time and visible idle progress.

    A Docker exec may provide no stdout while Wine initializes. Never mistake
    that silence for a successful guest; report the exact layer and exit code.
    The lifecycle console already persists these messages under Builds/Logs.
    """
    started = time.monotonic()
    completed = threading.Event()
    _app_devtools_progress('Windows Bash probe START ' + stage)

    def heartbeat() -> None:
        while not completed.wait(15):
            _app_devtools_progress('Windows Bash probe WORKING ' + stage +
                                   ' (' + str(int(time.monotonic() - started)) + 's)')

    ticker = threading.Thread(target=heartbeat, daemon=True)
    ticker.start()
    try:
        try:
            result = _docker_call(docker, env, *command, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('win guest Bash verification timed out during ' + stage +
                               ' after ' + str(timeout) + 's') from error
    finally:
        completed.set()
        ticker.join(timeout=1)

    output = ((result.stdout or '') + '\n' + (result.stderr or '')).strip()
    if result.returncode != 0:
        signal = (' (SIGSEGV / signal 11)' if result.returncode in (139, -11) else '')
        diagnostic = output[-1200:] if output else 'no guest output'
        raise RuntimeError('win guest Bash verification exited ' + str(result.returncode) +
                           signal + ' during ' + stage + ': ' + diagnostic)
    if expected and expected.lower() not in output.lower():
        raise RuntimeError('win guest Bash verification did not confirm ' + stage +
                           ': ' + (output[-700:] if output else 'no guest output'))
    _app_devtools_progress('Windows Bash probe PASS ' + stage +
                           ' (' + str(round(time.monotonic() - started, 1)) + 's)')
    return result

def _verify_guest_bash(docker: str, env: dict[str, str], kind: str, name: str) -> None:
    if kind == 'linux':
        _app_devtools_progress('Verifying reusable Linux guest Bash')
        result = _docker_call(docker, env, 'exec', name, '/bin/bash', '--version', timeout=150)
        if result.returncode:
            diagnostic = (result.stderr or result.stdout or '').strip()[-1200:]
            raise RuntimeError('linux guest Bash verification exited ' + str(result.returncode) +
                               (': ' + diagnostic if diagnostic else '; no guest output'))
        return

    # Check each layer before launching the real Windows MSYS2 shell. An exit
    # 139 from a qemu-backed guest previously carried no stdout or stderr and
    # could not distinguish a broken Linux binary translator, Wine startup,
    # or the Windows PE/Cygwin runtime. These are read-only smoke commands;
    # they neither provision a dependency nor substitute a simulated shell.
    common = ['exec', *_launcher_guest_env('win'), name]
    _guest_verification_probe(docker, env, name, 'amd64 Linux userspace',
                              ['exec', name, '/bin/bash', '--version'],
                              timeout=30, expected='GNU bash')
    _guest_verification_probe(docker, env, name, 'Wine loader',
                              [*common, 'wine', '--version'], timeout=45,
                              expected='wine-')
    _guest_verification_probe(docker, env, name, 'Wine Windows PE execution',
                              [*common, 'wine', 'cmd.exe', '/c', 'echo',
                               'CINTER_WINE_PE_READY'], timeout=90,
                              expected='CINTER_WINE_PE_READY')
    # The base MSYS2 Bash starts without its PATH when --noprofile is
    # specified. Test actual shell plus coreutils, not merely wine --version.
    _guest_verification_probe(
        docker, env, name, 'MSYS2 UCRT64 Bash + coreutils',
        [*common, 'wine', r'Z:\msys2\usr\bin\bash.exe', '--noprofile', '--norc',
         '-c', 'export PATH=/usr/bin:/ucrt64/bin:$PATH; '
               'test -x /usr/bin/rm.exe || exit 73; '
               'f=/tmp/cinterforge-bash-$$; : > "$f" && rm "$f" && '
               '/usr/bin/uname -s'],
        timeout=150, expected='msys')

def _stop_owned_guests_locked_one(docker: str, env: dict[str, str], name: str) -> None:
    _docker_call(docker, env, 'rm', '-f', name)

def _launcher_guest_env(kind: str) -> list[str]:
    result = ['-e', 'DISPLAY=' + (':100' if kind == 'win' else ':99'),
              '-e', r'PS1=\u@\h \w \$ ']
    if kind == 'win':
        # Match the guest's existing MSYS2 Bash environment and prefix, not
        # the separate builder-only Wine PREFIX or a second Windows image.
        result += ['-e', 'HOME=/tmp', '-e', 'WINEPREFIX=/tmp/.wine',
                   '-e', 'CINTER_WINE_GUEST=1']
    return result

def _verify_guest_vnc(docker: str, env: dict[str, str], kind: str, name: str) -> None:
    """Verify actual RFB handshake and noVNC HTTP on the retained guest."""
    rfb_port = 5901 if kind == 'win' else 5900
    web_port = 6081 if kind == 'win' else 6080
    script = r'''import socket, time, urllib.request, sys
rfb_port, web_port = map(int, sys.argv[1:])
error = 'not ready'
for i in range(25):
    try:
        with socket.create_connection(('127.0.0.1', rfb_port), timeout=2) as conn:
            banner = conn.recv(12)
            if len(banner) != 12 or not banner.startswith(b'RFB '):
                raise RuntimeError('invalid RFB handshake: ' + repr(banner))
    except (OSError, RuntimeError, ValueError) as exc:
        error = 'RFB port %d: %s' % (rfb_port, exc)
        time.sleep(0.3)
        continue
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/vnc.html' % web_port, timeout=2) as response:
            if response.status != 200 or b'<html' not in response.read(2048).lower():
                raise RuntimeError('noVNC HTTP endpoint incomplete')
    except (OSError, RuntimeError, ValueError) as exc:
        error = 'noVNC port %d: %s' % (web_port, exc)
        time.sleep(0.3)
        continue
    print('CINTER_VNC_READY')
    break
else:
    raise RuntimeError('VNC unavailable: ' + error)
'''
    result = _docker_call(docker, env, 'exec', name, 'python3', '-c', script,
                          str(rfb_port), str(web_port), timeout=90)
    if result.returncode or 'CINTER_VNC_READY' not in (result.stdout or ''):
        raise RuntimeError(kind + ' guest VNC handshake failed: ' +
                           (result.stderr or result.stdout or '').strip()[-650:])

def _activate_guest_vnc(docker: str, env: dict[str, str], kind: str, name: str) -> dict:
    with _launcher_lock:
        # Do not install/repair packages at runtime. All dependencies belong
        # to Setup's existing pinned guest images and the unified SDK.
        probe_script = ("for tool in Xvfb xdpyinfo openbox x11vnc websockify feh pgrep; do "
                        'command -v "$tool" >/dev/null 2>&1 || echo missing:$tool; done; '
                        "test -f /usr/share/novnc/vnc.html || echo missing:novnc; true")
        probe = _docker_call(docker, env, 'exec', name, '/bin/sh', '-c', probe_script,
                             timeout=30)
        if probe.returncode or (probe.stdout or '').strip():
            detail = (probe.stdout or probe.stderr or 'probe failed').strip()[-400:]
            raise RuntimeError('Guest GUI tools missing from staged ' + kind + ' image (' + detail +
                               '); rebuild DevTools in Setup, then Build/Stage')
        # The guest and host display the SAME replaceable platform PNG. Copy
        # the staged resource into the owned runtime container; never install
        # packages or write to Builds from a downloaded application.
        wallpaper_name = 'windows' if kind == 'win' else 'linux'
        wallpaper = ASSETS / 'launcher/assets/backgrounds' / (wallpaper_name + '.png')
        if not wallpaper.is_file() or wallpaper.is_symlink():
            raise RuntimeError('Missing staged ' + kind + ' Launcher wallpaper')
        copied = _docker_call(docker, env, 'cp', str(wallpaper),
                              name + ':/tmp/cinterforge-launcher-' + wallpaper_name + '-wallpaper.png', timeout=30)
        if copied.returncode:
            raise RuntimeError('Unable to load ' + kind + ' Launcher wallpaper: ' + copied.stderr.strip()[:400])
        # Exact one-off helper script is idempotent within this owned container.
        # No root-level host agent, no additional VM; Docker container removal
        # terminates every helper when the application exits.
        # Independent desktops inside ONE Debian guest. Do not use global
        # `pgrep -x openbox/x11vnc`: another display can already own that PID.
        display = ':100' if kind == 'win' else ':99'
        rfb_port = 5901 if kind == 'win' else 5900
        web_port = 6081 if kind == 'win' else 6080
        script = r'''set -eu
DISPLAY="$1"
RFB="$2"
WEB="$3"
WALLPAPER="$4"
PREFIX="$5"
if ! DISPLAY="$DISPLAY" xdpyinfo >/dev/null 2>&1; then
  nohup Xvfb "$DISPLAY" -screen 0 1600x900x24 -nolisten tcp >"/tmp/cinter-${PREFIX}-xvfb.log" 2>&1 </dev/null &
fi
for n in 1 2 3 4 5 6 7 8 9 10; do
  DISPLAY="$DISPLAY" xdpyinfo >/dev/null 2>&1 && break
  sleep 1
done
DISPLAY="$DISPLAY" xdpyinfo >/dev/null 2>&1 || exit 18
# Per-desktop helpers are keyed to their display and network port.
if ! test -s "/tmp/cinter-${PREFIX}-openbox.pid" ||
   ! kill -0 "$(cat "/tmp/cinter-${PREFIX}-openbox.pid")" 2>/dev/null; then
  DISPLAY="$DISPLAY" nohup openbox --sm-disable >"/tmp/cinter-${PREFIX}-openbox.log" 2>&1 </dev/null &
  echo $! >"/tmp/cinter-${PREFIX}-openbox.pid"
fi
DISPLAY="$DISPLAY" feh --bg-fill "$WALLPAPER"
if ! python3 -c 'import socket,sys;s=socket.socket();s.settimeout(0.5);r=s.connect_ex(("127.0.0.1",int(sys.argv[1])));s.close();sys.exit(0 if r==0 else 1)' "$RFB"; then
  DISPLAY="$DISPLAY" nohup x11vnc -display "$DISPLAY" -rfbport "$RFB" -localhost -forever -shared -nopw >"/tmp/cinter-${PREFIX}-vnc.log" 2>&1 </dev/null &
fi
if ! python3 -c 'import socket,sys;s=socket.socket();s.settimeout(0.5);r=s.connect_ex(("127.0.0.1",int(sys.argv[1])));s.close();sys.exit(0 if r==0 else 1)' "$WEB"; then
  nohup websockify --web /usr/share/novnc "$WEB" "127.0.0.1:$RFB" >"/tmp/cinter-${PREFIX}-web.log" 2>&1 </dev/null &
fi
'''
        result = _docker_call(docker, env, 'exec', *_launcher_guest_env(kind),
                              name, '/bin/bash', '-c', script, 'gui', display,
                              str(rfb_port), str(web_port),
                              '/tmp/cinterforge-launcher-' + wallpaper_name + '-wallpaper.png',
                              kind, timeout=45)
        if result.returncode:
            raise RuntimeError('Guest graphical display startup failed: ' +
                               (result.stderr or result.stdout).strip()[-600:])
        try:
            _verify_guest_vnc(docker, env, kind, name)
        except RuntimeError as error:
            # The image's transient preflight starts its own display, so its
            # earlier PASS cannot diagnose a failed *retained* runtime helper.
            # Capture the guest-local startup logs before the owning container
            # is torn down by Setup's failure cleanup.
            details = ''
            try:
                diagnostic = _docker_call(
                    docker, env, 'exec', name, '/bin/sh', '-c',
                    'for service in xvfb openbox vnc web; do '
                    'file=/tmp/cinter-' + kind + '-$service.log; '
                    'if test -f "$file"; then printf "\n[%s]\n" "$service"; '
                    'tail -n 18 "$file"; fi; done', timeout=20)
                details = (diagnostic.stdout or diagnostic.stderr or '').strip()[-1800:]
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise RuntimeError(str(error) + ('\nGuest display logs:\n' + details
                                             if details else '')) from error
        port = _LAUNCHER_PORTS[kind]
        # A forwarded Colima localhost port is not an Internet service.
        # Its browser frame appears only after an explicit Launcher action.
        return {'platform': kind, 'viewer_url':
                f'http://127.0.0.1:{port}/vnc.html?autoconnect=true&resize=scale&show_dot=true'}
