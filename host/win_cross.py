"""Mac->Windows GCC/Wine smoke, separate from native Windows acceptance.

Logs the actual cross-compiler and Wine commands, combined process output,
exit codes, mount evidence, and missing assertions for failed smoke tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import time


def smoke(builds: Path, *, executor=None) -> dict:
    """Compile and execute a real Windows PE using Wine and Win32 filesystem APIs."""
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise RuntimeError('Windows cross-build acceptance must run inside sibling Builds')
    from host.windows_environment import WindowsCrossEnvironment
    cross = executor or WindowsCrossEnvironment(builds).ensure()
    root = builds / 'Intermediates/CinterForgeTerminal/WindowsCrossSmoke'
    root.mkdir(parents=True, exist_ok=True)
    marker = root / 'cinter-wine-directory-marker.txt'
    marker.write_text('Real Wine Windows PE filesystem acceptance\n', encoding='utf-8')
    source = root / 'console.c'
    binary = root / 'CinterWindowsCrossSmoke.exe'
    # A Windows-native API proves that the actual PE can enumerate the
    # bind-mounted working directory. Do not rely on the C runtime's system()
    # wrapper: under Wine it may return without forwarding cmd.exe output.
    source.write_text(r'''#include <windows.h>
#include <stdio.h>
#include <string.h>
int main(void) {
    char cwd[MAX_PATH];
    WIN32_FIND_DATAA entry;
    int found = 0;
    if (!GetCurrentDirectoryA(MAX_PATH, cwd)) return 10;
    puts("CINTER_WINDOWS_WINE_PASS");
    printf("Windows current directory: %s\n", cwd);
    HANDLE search = FindFirstFileA("*", &entry);
    if (search == INVALID_HANDLE_VALUE) {
        printf("FindFirstFileA failed: %lu\n", GetLastError());
        return 11;
    }
    do {
        puts(entry.cFileName);
        if (strcmp(entry.cFileName, "cinter-wine-directory-marker.txt") == 0)
            found = 1;
    } while (FindNextFileA(search, &entry));
    FindClose(search);
    if (!found) return 12;
    puts("CINTER_WINDOWS_DIRECTORY_PASS");
    return 0;
}
''', encoding='utf-8')
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    transcript = logs / 'windows-wine-smoke.log'
    receipt = logs / 'windows-wine-smoke.json'
    cc = ['x86_64-w64-mingw32-gcc', cross.path(source), '-o', cross.path(binary)]
    wine_command = ['wine', cross.path(binary)]
    report = {
        'schema': 'cinterforge.terminal.windows-cross-smoke.v1',
        'execution': 'wine-not-native-windows', 'provider': cross.provider,
        'image': str(getattr(cross, 'image', '')), 'compiler': cc[0],
        'wine': 'wine', 'pe_executable': str(binary),
        'cwd_host': str(root), 'cwd_guest': cross.path(root),
        'source_mount_host': str(getattr(cross, 'repository', '')),
        'builds_mount_host': str(builds),
        'marker': str(marker), 'commands': [], 'checks': {},
        'status': 'failed', 'completed_utc': None,
    }
    lines = [
        'CinterForgeTerminal Windows Wine smoke (NOT native Windows acceptance)',
        'provider=' + str(cross.provider) + ' image=' + str(getattr(cross, 'image', None)),
        'cwd(host)=' + str(root), 'cwd(guest)=' + cross.path(root),
        'source(host)=' + str(getattr(cross, 'repository', '<executor not configured>')),
        'builds(host)=' + str(builds),
        'mounts: source(host) -> /source (read-only); Builds(host) -> /builds (writable)',
        'docker executable=' + str(getattr(cross, 'docker', '<native executor>')),
        'expected marker=' + marker.name,
        'process output below contains merged stdout/stderr when provided by WindowsCrossEnvironment',
        '',
    ]

    def flush():
        # Persist each command before invoking it; a timeout still leaves
        # actionable evidence rather than an empty log.
        transcript.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    flush()

    def execute(label: str, command: list[str], timeout: int):
        entry = {'stage': label, 'command': list(command), 'cwd': cross.path(root),
                 'timeout_seconds': timeout}
        report['commands'].append(entry)
        lines.append(f'[{label}] $ {shlex.join(command)}')
        flush()
        print('[WINDOWS CROSS] smoke: ' + label, flush=True)
        try:
            result = cross.execute(command, cwd=root, timeout=timeout)
        except Exception as error:
            entry['exception'] = repr(error)
            lines.append(f'[{label}] EXCEPTION: {error!r}\n')
            flush()
            raise
        output = (result.stdout or '')
        # The standard WindowsCrossEnvironment merges stderr into stdout; a
        # custom/native executor may instead return stderr separately.
        stderr = getattr(result, 'stderr', None)
        if stderr:
            output += '\n[separate stderr]\n' + stderr
        entry['returncode'] = result.returncode
        entry['output_tail'] = output[-12000:]
        lines.append(f'[{label}] exit={result.returncode} output-bytes={len(output.encode("utf-8", errors="replace"))}')
        lines.append(output if output else '<no process output>')
        lines.append('')
        flush()
        return result, output

    try:
        compile_run, compile_output = execute('compile PE', cc, 300)
        report['compiler_output'] = compile_output[-12000:]
        pe_valid = binary.is_file() and binary.read_bytes()[:2] == b'MZ'
        report['checks']['compiler_exit_zero'] = compile_run.returncode == 0
        report['checks']['binary_is_pe'] = pe_valid
        lines.append(f'[compile evidence] binary_is_pe={pe_valid} size={binary.stat().st_size if binary.is_file() else "missing"}')
        if compile_run.returncode or not pe_valid:
            raise RuntimeError(f'Windows PE compilation failed (exit={compile_run.returncode}, valid PE={pe_valid})')

        run, output = execute('run PE via Wine', wine_command, 240)
        report['output'] = output[-12000:]
        report['returncode'] = run.returncode
        report['checks']['wine_exit_zero'] = run.returncode == 0
        report['checks']['pe_console_marker'] = 'CINTER_WINDOWS_WINE_PASS' in output
        report['checks']['pe_win32_directory_marker'] = (
            marker.name in output and 'CINTER_WINDOWS_DIRECTORY_PASS' in output)
        # The Windows executable above is the execution gate. It calls real
        # Win32 filesystem APIs under Wine and must return the expected marker.
        # Do not gate actual PE/WebView testing on Wine 8 cmd.exe's `dir`,
        # which can fail on a bind-mounted Z: drive with "Invalid function".
        # cross_build.py continues to build and launch the real WebView sample
        # and Terminal PE under Wine after this smoke succeeds.
        missing = [name for name, passed in report['checks'].items() if not passed]
        lines.append('[acceptance] ' + ('all required checks satisfied' if not missing else
                                         'FAILED: ' + ', '.join(missing)))
        if missing:
            raise RuntimeError('Windows Wine smoke failed: ' + ', '.join(missing))
        report['status'] = 'passed'
    except Exception as error:
        report['error'] = str(error)
        # The diagnosis is deliberately non-acceptance evidence. It runs only
        # after failure and cannot turn a failed PE test into a success.
        diagnostics = [
            ('container filesystem', ['sh', '-c',
                'printf "guest cwd="; pwd; id; ls -la; '
                'printf "marker content="; cat ' + shlex.quote(marker.name)]),
            ('Wine cmd current directory', ['wine', 'cmd', '/c', 'cd']),
            ('Wine cmd directory listing', ['wine', 'cmd', '/c', 'dir', '/b']),
        ]
        for label, command in diagnostics:
            try:
                execute('diagnostic: ' + label, command, 45)
            except Exception as diagnostic_error:
                lines.append(f'[diagnostic: {label}] unavailable: {diagnostic_error!r}')
        lines.append('[FAILED] ' + str(error))
        print('[WINDOWS CROSS] smoke failed: ' + str(error), flush=True)
        print('[WINDOWS CROSS] detailed command/output/mount diagnostics: ' + str(transcript), flush=True)
        raise RuntimeError('Windows Wine smoke failed; inspect ' + str(transcript) + ': ' + str(error)) from error
    finally:
        report['completed_utc'] = time.time()
        receipt.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        flush()
        print('[WINDOWS CROSS] ' + report['status'] + ' (' + cross.provider + ', Wine, not native Windows)', flush=True)
    return report
