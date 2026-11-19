"""CinterForgeGuest — runtime guest startup and connection supervisor.

PURPOSE
    Own one HTTP server's asynchronous Linux/Windows startup orchestration,
    readiness snapshot, persistent Bash PTY maintenance and independent VNC
    health. This module coordinates already-existing runtime operations; it
    DOES NOT provision DevTools, create a VM, or implement a second Docker or
    session backend. Mac Bash and classic Macintosh emulators are independent.

CLASS / METHODS
    CinterForgeGuestSupervisor.__init__: bind exactly one HTTP server owner
        and its existing SDK, watchdog, Docker boot, PTY and RFB operations.
    snapshot: atomically report state, verified Bash guests and VNC endpoints.
    schedule: start at most one background boot worker after SDK resolution;
        return not-installed / starting / failed / ready without blocking HTTP.
    boot: wait for native lifetime ownership; boot the original t VM, verify
        guest VNC and Bash independently; publish partial results and cleanup.
    maintain: repair only lost Bash PTYs or VNC helpers inside already-running
        owned guests, never starting another VM or Docker daemon.

STARTUP / SHUTDOWN / DEPENDENCIES
    The HTTP owner creates this instance after binding its listener. All
    mutable readiness/lock/session fields remain on that same owner for API
    compatibility. Backend operations are supplied explicitly by the main
    service: this module never imports it (and never starts a second server).
    The server calls cancel_event.set() and closes owned Bash PTYs on stop;
    the native-lifetime watchdog continues to own VM crash cleanup. No
    download, setup, source-tree discovery, or new runtime state is added.

IMPORTANT: server.guest_vnc_ready and server.guest_ready are deliberately
independent. A failed Windows Wine PTY must not revoke a working Linux Bash
or previously handshaken RFB display. Preserve log messages and retry bounds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time


class CinterForgeGuestSupervisor:
    """Coordinate existing guest startup/health for one Python HTTP owner."""

    def __init__(self, server, *, sdk_root, runtime_logs_root,
                 start_runtime_watchdog, guest_boot_cancel,
                 boot_runtime_guests, stop_runtime_vm,
                 close_owned_guest_shells, new_owned_guest_shell,
                 check_owned_guest_vnc):
        self.server = server
        self._sdk_root = sdk_root
        self._runtime_logs_root = runtime_logs_root
        self._start_runtime_watchdog = start_runtime_watchdog
        self._guest_boot_cancel = guest_boot_cancel
        self._boot_runtime_guests = boot_runtime_guests
        self._stop_runtime_vm = stop_runtime_vm
        self._close_owned_guest_shells = close_owned_guest_shells
        self._new_owned_guest_shell = new_owned_guest_shell
        self._check_owned_guest_vnc = check_owned_guest_vnc

    def snapshot(self) -> dict:
        server = self.server
        with server.guest_start_lock:
            state = server.guest_start_state
            return {'state': state, 'error': server.guest_start_error,
                    'guests': list(server.guest_ready),
                    'vnc': list(server.guest_vnc_ready),
                    'errors': dict(server.guest_errors)}


    def schedule(self) -> dict:
        """Exactly one explicitly requested initialization per run, including a later SDK install.

        The HTTP service is already accepting requests when this is called. It
        publishes readiness explicitly rather than claiming the guests are ready
        just because the native WebView opened. An explicit reinstall may retry a
        failed boot; ordinary Terminal startup never calls this function.
        """
        server = self.server
        with server.guest_start_lock:
            if server.guest_start_state in ('starting', 'ready'):
                return {'state': server.guest_start_state, 'error': server.guest_start_error}
            try:
                selected_sdk = self._sdk_root()
                installed = selected_sdk is not None
            except (OSError, ValueError, RuntimeError) as error:
                server.guest_start_state = 'failed'
                server.guest_start_error = 'Cannot inspect installed DevTools: ' + str(error)
                server.log_event('Managed reusable guests FAILED: ' + server.guest_start_error)
                return {'state': 'failed', 'error': server.guest_start_error}
            if not installed:
                server.guest_start_state = 'not-installed'
                server.guest_start_error = ''
                from runtime import CinterForgeDevTools as devtools
                selection = devtools._devtools_builds_pin()
                server.log_event('Managed reusable guests: DevTools Source not selected; '
                                 'run devtools install <path/to/source>; selection=' + str(selection) +
                                 '; no VM started')
                return {'state': 'not-installed', 'error': ''}
            server.guest_start_state = 'starting'
            server.guest_start_error = ''
            server.guest_ready = []
            server.guest_vnc_ready = []
            server.guest_errors = {}
        try:
            # Startup runs off the HTTP/UI thread. The guest boot thread waits
            # until any previous native owner's crash watchdog releases the VM;
            # otherwise a fast relaunch can start new guests while old cleanup is
            # still stopping the same t profile.
            threading.Thread(target=self.boot, args=(),
                             daemon=True, name='cinter-runtime-guests').start()
        except (OSError, RuntimeError, ValueError) as error:
            with server.guest_start_lock:
                server.guest_start_state = 'failed'
                server.guest_start_error = 'Cannot start guest supervisor: ' + str(error)
            server.log_event('Managed reusable guests FAILED: ' + str(error))
            return self.snapshot()
        server.log_event('Managed reusable guests: starting Linux and Windows on explicit request; '
                         'runtime=' + os.environ.get('CINTER_TERMINAL_RUNTIME_MODE', 'unknown') +
                         '; sdk=' + str(selected_sdk))
        return self.snapshot()


    def maintain(self, sdk: Path) -> None:
        """Maintain Bash PTYs and both VNC endpoints without starting another VM.

        If a guest is unavailable, log a bounded failure and preserve Mac Bash.
        The independent native-lifetime watchdog owns crash recovery for the VM.
        """
        server = self.server
        failures = {'linux': 0, 'win': 0}
        vnc_failures = {'linux': 0, 'win': 0}
        last_vnc_check = time.monotonic()
        while not server._stopping:
            time.sleep(2)
            if server._stopping:
                return
            with server.guest_shells_lock:
                lost = [(kind, session) for kind, session in server.guest_shells.items()
                        if session.closed or session.process.poll() is not None]
                attached = set(server.guest_shells)
            with server.guest_start_lock:
                # A GUI session can be verified even when its interactive PTY
                # timed out at launch. Retry ONLY that missing PTY in the already
                # running, owned guest; do not allocate another VM or container.
                pending = [kind for kind in server.guest_vnc_ready
                           if kind not in attached and kind not in server.guest_ready
                           and failures[kind] < 3]
            for kind in pending:
                if server._stopping:
                    return
                failures[kind] += 1
                server.log_event('[DEVTOOLS] Retrying ' + kind +
                                 ' Bash PTY in existing VNC-ready guest (' +
                                 str(failures[kind]) + '/3)')
                try:
                    replacement = self._new_owned_guest_shell(server, kind, sdk)
                    with server.guest_shells_lock:
                        if server._stopping or kind in server.guest_shells:
                            replacement.close()
                            continue
                        server.guest_shells[kind] = replacement
                    with server.guest_start_lock:
                        if kind not in server.guest_ready:
                            server.guest_ready.append(kind)
                        server.guest_errors.pop(kind, None)
                        server.guest_start_state = 'ready' if not server.guest_errors else 'failed'
                        server.guest_start_error = '; '.join(k + ': ' + v
                                                            for k, v in server.guest_errors.items())
                    failures[kind] = 0
                    server.log_event('[DEVTOOLS] ' + kind + ' Bash PTY restored; Code-OSS enabled')
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    with server.guest_start_lock:
                        server.guest_errors[kind] = str(error)
                        server.guest_start_state = 'failed'
                        server.guest_start_error = '; '.join(k + ': ' + v
                                                            for k, v in server.guest_errors.items())
                    server.log_event('[DEVTOOLS] ' + kind + ' Bash PTY retry FAILED: ' + str(error))
            for kind, previous in lost:
                if server._stopping or failures[kind] >= 3:
                    continue
                failures[kind] += 1
                # Disable ONLY this shell immediately. A disconnected PTY is not
                # proof that its independent VNC endpoint has gone away.
                with server.guest_start_lock:
                    server.guest_ready = [v for v in server.guest_ready if v != kind]
                    server.guest_errors[kind] = 'Persistent Bash disconnected; reconnecting'
                    server.guest_start_state = 'failed'
                    server.guest_start_error = '; '.join(k + ': ' + v for k, v in server.guest_errors.items())
                server.log_event(kind + ' persistent Bash exited; reconnecting within its existing guest')
                try:
                    replacement = self._new_owned_guest_shell(server, kind, sdk)
                    with server.guest_shells_lock:
                        if server._stopping or server.guest_shells.get(kind) is not previous:
                            replacement.close()
                            continue
                        server.guest_shells[kind] = replacement
                    with server.code_sessions_lock:
                        for code_id, entry in list(server.code_sessions.items()):
                            if entry is previous:
                                server.code_sessions.pop(code_id, None)
                    previous.close()
                    with server.guest_start_lock:
                        if kind not in server.guest_ready:
                            server.guest_ready.append(kind)
                        server.guest_errors.pop(kind, None)
                        server.guest_start_state = 'ready' if not server.guest_errors else 'failed'
                        server.guest_start_error = '; '.join(k + ': ' + v for k, v in server.guest_errors.items())
                    failures[kind] = 0
                    server.log_event(kind + ' persistent Bash reconnected (Code-OSS can reattach)')
                except Exception as error:
                    server.log_event(kind + ' persistent Bash reconnect FAILED: ' + str(error))
                    if failures[kind] >= 3:
                        with server.guest_start_lock:
                            server.guest_errors[kind] = str(error)
                            server.guest_ready = [v for v in server.guest_ready if v != kind]
                            server.guest_start_state = 'failed'
                            server.guest_start_error = '; '.join(k + ': ' + v
                                                               for k, v in server.guest_errors.items())
            # The Session viewers use the same two VNC endpoints, but can remain
            # closed for hours. Probe them periodically and repair only missing
            # guest-local helpers; do not create an additional RFB server/VM.
            if not server._stopping and time.monotonic() - last_vnc_check >= 30:
                last_vnc_check = time.monotonic()
                for kind in ('linux', 'win'):
                    with server.guest_start_lock:
                        # VNC health is independent of the persistent Bash PTY.
                        # Continue retrying when the VNC flag was just cleared by
                        # a failed probe, until the bounded retry limit is hit.
                        known_guest = (kind in server.guest_vnc_ready or
                                       kind in server.guest_ready or
                                       vnc_failures[kind] > 0)
                    if not known_guest or server._stopping or vnc_failures[kind] >= 3:
                        continue
                    try:
                        if self._check_owned_guest_vnc(sdk, kind):
                            server.log_event(kind + ' VNC endpoint recovered in existing guest')
                        with server.guest_start_lock:
                            # Health is established by self._check_owned_guest_vnc itself;
                            # no persistent Bash PTY is needed for a live desktop.
                            if kind not in server.guest_vnc_ready:
                                server.guest_vnc_ready.append(kind)
                        vnc_failures[kind] = 0
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                        vnc_failures[kind] += 1
                        with server.guest_start_lock:
                            server.guest_vnc_ready = [v for v in server.guest_vnc_ready if v != kind]
                        server.log_event(kind + ' VNC health/recovery FAILED (' +
                                         str(vnc_failures[kind]) + '/3): ' + str(error))
                        if vnc_failures[kind] >= 3:
                            server.log_event(kind + ' VNC unavailable; Launcher can explicitly retry '
                                             'without creating a guest or VM')


    def boot(self) -> None:
        """Boot only the existing owned VM and independently verify Bash/VNC."""
        server = self.server
        watchdog_ready = False
        try:
            # A separate process watches the native PID, surviving both a Python
            # worker crash and Xcode Stop. It acknowledges exclusive lifetime
            # ownership BEFORE the boot thread is allowed to start Colima.
            self._start_runtime_watchdog(server.native_parent_pid,
                                         self._runtime_logs_root(server.mutable_builds))
            watchdog_ready = True
            if server._stopping:
                raise RuntimeError('Native Terminal shut down before guest startup completed')
            server.log_event('[DEVTOOLS] Booting shared t Colima/QEMU Linux host and verifying Docker')
            def display_connected(kind: str) -> None:
                # _start_guest_locked verifies RFB and noVNC before this callback.
                # Do not wait for the OTHER platform's Wine startup or a PTY.
                with server.guest_start_lock:
                    if kind not in server.guest_vnc_ready:
                        server.guest_vnc_ready.append(kind)
                server.log_event('[DEVTOOLS] ' + kind + ' VNC connected; desktop available')

            result = self._boot_runtime_guests(cancel_event=self._guest_boot_cancel,
                                               on_display_ready=display_connected)
            if not result.get('installed'):
                raise RuntimeError('DevTools disappeared during managed guest startup')
            ready = list(result.get('guests', []))
            errors = dict(result.get('errors', {}))
            if not ready:
                # Both real guest checks failed. Leaving their QEMU running is not
                # a usable warm state and is precisely the stranded-VM symptom.
                try:
                    self._stop_runtime_vm()
                except Exception as cleanup_error:
                    errors['cleanup'] = str(cleanup_error)
                    server.log_event('Unusable guest VM cleanup FAILED: ' + str(cleanup_error))
            sdk = self._sdk_root()
            if sdk is None:
                raise RuntimeError('Verified DevTools disappeared while connecting Bash')
            # The two already-handshaken VNC displays are usable independently of
            # long-lived interactive Bash. Do not revoke them if the Wine/MSYS2 PTY
            # is slow, fails, or is being reconnected.
            with server.guest_start_lock:
                server.guest_vnc_ready = list(ready)
                server.guest_errors = dict(errors)
            for kind in list(ready):
                server.log_event('[DEVTOOLS] Connecting persistent ' + kind + ' Bash PTY')
                if server._stopping:
                    raise RuntimeError('Native Terminal quit during guest startup')
                try:
                    connected = self._new_owned_guest_shell(server, kind, sdk)
                    with server.guest_shells_lock:
                        if server._stopping:
                            connected.close()
                            raise RuntimeError('Native Terminal quit during guest startup')
                        server.guest_shells[kind] = connected
                    # Publish this *verified* Bash as soon as its PTY answers;
                    # Windows' slower Wine startup cannot withhold Linux readiness.
                    with server.guest_start_lock:
                        if kind not in server.guest_ready:
                            server.guest_ready.append(kind)
                    server.log_event(kind + ' persistent Bash connected (Code-OSS reusable PTY)')
                except Exception as error:
                    ready.remove(kind)
                    errors[kind] = str(error)
                    server.log_event(kind + ' persistent Bash FAILED: ' + str(error))
            with server.guest_start_lock:
                server.guest_ready = ready
                # Do NOT derive VNC readiness from persistent Bash readiness.
                # Each successful _start_guest_locked independently tested RFB/HTTP.
                server.guest_errors = errors
                server.guest_start_state = 'ready' if not errors else 'failed'
                server.guest_start_error = '; '.join(k + ': ' + v for k, v in errors.items())
            if (ready or server.guest_vnc_ready) and not server._stopping:
                threading.Thread(target=self.maintain, args=(sdk,),
                                 daemon=True, name='cinter-guest-shell-supervisor').start()
            if errors:
                # A failed Wine guest must not destroy a working Linux guest.
                # Splash closes on failure; Mac Bash is always available.
                server.log_event('Managed reusable guests PARTIAL/FAILED: ' +
                                 json.dumps(result, sort_keys=True))
            else:
                server.log_event('Managed reusable guests ready: ' + json.dumps(result, sort_keys=True))
        except Exception as error:
            # Do not leave a half-booted Colima profile alive after a failed image
            # load / Bash verification. The guardian is already responsible for
            # abnormal native exits; cleanup here handles failure while it lives.
            if watchdog_ready:
                self._close_owned_guest_shells(server)
                try:
                    self._stop_runtime_vm()
                except Exception as cleanup_error:
                    server.log_event('Managed reusable guests cleanup FAILED: ' + str(cleanup_error))
            with server.guest_start_lock:
                server.guest_start_state = 'failed'
                server.guest_start_error = str(error)
                server.guest_ready = []
                server.guest_vnc_ready = []
                server.guest_errors = {'linux': str(error), 'win': str(error)}
            server.log_event('Managed reusable guests FAILED: ' + str(error))


