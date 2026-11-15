"""CinterForgeCode — one authoritative Code-OSS workspace/file-access owner.

PURPOSE: Keep Code-OSS workspace selection, theme, persistent workspace state
and read/write/reveal operations separate from HTTP request dispatch.

CLASS: CinterForgeCodeWorkspace is instantiated by the one Python HTTP server.
METHODS: __init__ selects user Source/Documents and reloads saved state;
_select validates local folder or .code-workspace roots; persist saves selected
workspace and theme; details returns UI state; _path confines virtual paths
within selected roots; _host_path restricts host file access to workspace and
authorized Logs; _reveal_host_file delegates to the platform UI; operation
applies existing stat/read/write/directory/rename/delete/theming actions.

STARTUP: The HTTP owner explicitly provides platform name and writable root.
SHUTDOWN: No subprocess, service, or VM ownership; changes are persisted only
when requested. The existing authenticated /host/code-workspace route remains
in the main service. No Code-OSS editor fork or new path authorization exists.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid

class CinterForgeCodeWorkspace:
    """The selected host folder or .code-workspace, mounted at cinterforge:/.

    A single-folder workspace maps the virtual root directly to that directory.
    Multi-root .code-workspace files expose each named folder below the virtual
    root, using the existing Code-OSS filesystem provider. Only explicit
    workspace selections can change the roots; file operations stay inside them.
    State belongs to the developer Builds or installed TerminalState tree.
    """

    MAX_FILE_BYTES = 8 * 1024 * 1024

    def __init__(self, writable_root: Path, *, system: str):
        self.system = system
        self.lock = threading.RLock()
        self.state_file = writable_root / 'CinterForgeTerminal/CodeOSS/workspace.json'
        self.home = self._user_home()
        default = self.home / 'Source'
        if not default.is_dir():
            default = self.home / 'Documents'
        if not default.is_dir():
            default = self.home
        self.selection = default
        self.kind = 'folder'
        self.theme = 'dark'
        self.roots = [(default.name or 'Home', default)]
        # The Host attaches its existing Builds/Steam Logs directory after startup.
        # Logs are readable/revealable without adding them to the editor workspace.
        self.host_log_roots = []
        self.revision = 0
        if self.state_file.is_file() and not self.state_file.is_symlink():
            try:
                state = json.loads(self.state_file.read_text('utf-8'))
                self.theme = state.get('theme') if state.get('theme') in ('dark', 'light') else 'dark'
                self._select(str(state['path']), str(state.get('kind', 'folder')))
            except (ValueError, OSError, KeyError, TypeError):
                # A stale/moved workspace must not keep the editor from starting.
                self.selection = default
                self.kind = 'folder'
                self.roots = [(default.name or 'Home', default)]

    @staticmethod
    def _user_home() -> Path:
        provided = os.environ.get('CINTER_USER_HOME')
        if provided:
            return Path(provided).expanduser().resolve()
        if os.name == 'nt':
            return Path(os.environ.get('USERPROFILE') or Path.home()).resolve()
        try:
            import pwd
            return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        except (ImportError, KeyError, OSError):
            return Path.home().resolve()

    def _select(self, raw: str, kind: str) -> None:
        if kind not in ('folder', 'workspace') or not raw or '\x00' in raw:
            raise ValueError('Open an existing folder or .code-workspace file')
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError('Provide an absolute host filesystem path')
        candidate = candidate.resolve(strict=True)
        if kind == 'folder':
            if not candidate.is_dir():
                raise ValueError('Selected folder does not exist')
            roots = [(candidate.name or 'Home', candidate)]
        else:
            if not candidate.is_file() or candidate.suffix != '.code-workspace':
                raise ValueError('Select an existing .code-workspace file')
            if candidate.stat().st_size > 256 * 1024:
                raise ValueError('Workspace definition is too large')
            workspace = json.loads(candidate.read_text('utf-8'))
            folders = workspace.get('folders')
            if not isinstance(folders, list) or not 1 <= len(folders) <= 32:
                raise ValueError('Workspace must contain 1–32 local folders')
            roots = []
            used = set()
            for folder in folders:
                if not isinstance(folder, dict) or not isinstance(folder.get('path'), str):
                    raise ValueError('Workspace must contain local path folders')
                target = Path(folder['path']).expanduser()
                if not target.is_absolute():
                    target = candidate.parent / target
                target = target.resolve(strict=True)
                if not target.is_dir():
                    raise ValueError('Workspace folder does not exist: ' + str(target))
                label = folder.get('name') or target.name
                if not isinstance(label, str) or not label or '/' in label or '\\' in label or label in ('.', '..') or label in used:
                    raise ValueError('Workspace folder names must be unique and path-safe')
                used.add(label)
                roots.append((label, target))
        self.selection, self.kind, self.roots = candidate, kind, roots
        self.revision += 1

    def persist(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if self.state_file.is_symlink():
            raise ValueError('Unsafe Code-OSS state file')
        pending = self.state_file.with_name('workspace-' + uuid.uuid4().hex + '.tmp')
        try:
            pending.write_text(json.dumps({'path': str(self.selection), 'kind': self.kind,
                                           'theme': self.theme}, indent=2) + '\n', 'utf-8')
            os.replace(pending, self.state_file)
        finally:
            pending.unlink(missing_ok=True)

    def details(self) -> dict:
        with self.lock:
            return {'kind': 'terminal', 'platform': self.system,
                    'workspace_kind': self.kind, 'path': str(self.selection),
                    'folders': [{'name': name, 'path': str(path)} for name, path in self.roots],
                    'cwd': str(self.roots[0][1]), 'theme': self.theme}

    def _path(self, uri_path: str) -> Path | None:
        if not isinstance(uri_path, str) or not uri_path.startswith('/') or '\x00' in uri_path:
            raise ValueError('Invalid Code-OSS workspace path')
        segments = uri_path.split('/')[1:]
        if any(part in ('.', '..') or '\\' in part for part in segments):
            raise ValueError('Unsafe workspace traversal')
        if self.kind == 'folder':
            root, rest = self.roots[0][1], segments
        else:
            if not segments or not segments[0]:
                return None  # The virtual multi-root directory itself.
            matched = next((root for name, root in self.roots if name == segments[0]), None)
            if matched is None:
                raise FileNotFoundError('No such workspace root')
            root, rest = matched, segments[1:]
        target = root.joinpath(*rest).resolve()
        if not target.is_relative_to(root):
            raise PermissionError('File is outside the selected workspace')
        return target

    def _host_path(self, raw: str) -> Path:
        """Resolve host files only inside the selected workspace or existing Logs roots.

        The browser's cinterforge-host: URI names an actual host path. Do not
        treat that virtual URL as permission to read files outside these roots.
        """
        if not isinstance(raw, str) or not raw or '\x00' in raw:
            raise ValueError('Invalid host file path')
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', raw):
            raw = raw[1:]
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ValueError('Host file path must be absolute')
        target = candidate.resolve(strict=True)
        roots = [root for _, root in self.roots] + self.host_log_roots
        if not any(target.is_relative_to(root.resolve()) for root in roots):
            raise PermissionError('Host file is outside the workspace and Logs')
        return target

    @staticmethod
    def _reveal_host_file(path: Path) -> None:
        """Show the actual host folder, selecting files when the OS supports it."""
        if sys.platform == 'darwin':
            command = ['open', str(path)] if path.is_dir() else ['open', '-R', str(path)]
        elif os.name == 'nt':
            command = (['explorer.exe', str(path)] if path.is_dir()
                       else ['explorer.exe', '/select,', str(path)])
        else:
            command = ['xdg-open', str(path if path.is_dir() else path.parent)]
        subprocess.Popen(command, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def operation(self, body: dict) -> dict:
        operation = body.get('operation')
        with self.lock:
            if operation in ('host-stat', 'host-read-file', 'host-read-directory',
                             'reveal-host', 'reveal-workspace'):
                raw = body.get('path', '')
                target = (self._path(str(raw)) if operation == 'reveal-workspace'
                          else self._host_path(raw))
                if target is None:
                    target = self.selection if self.kind == 'workspace' else self.roots[0][1]
                if operation.startswith('reveal-'):
                    self._reveal_host_file(target)
                    return {'revealed': str(target)}
                if operation == 'host-stat':
                    info = target.stat()
                    return {'type': 'directory' if target.is_dir() else 'file',
                            'size': info.st_size, 'mtime': int(info.st_mtime * 1000)}
                if operation == 'host-read-directory':
                    if not target.is_dir():
                        raise NotADirectoryError('Not a host directory')
                    entries = []
                    for item in sorted(target.iterdir(), key=lambda entry: entry.name.casefold()):
                        if len(entries) >= 10000:
                            break
                        try:
                            checked = self._host_path(str(item))
                        except (ValueError, PermissionError, OSError):
                            continue
                        entries.append({'name': item.name,
                                        'type': 'directory' if checked.is_dir() else 'file'})
                    return {'entries': entries}
                if not target.is_file() or target.stat().st_size > self.MAX_FILE_BYTES:
                    raise ValueError('Host log/file is not readable or exceeds 8 MiB')
                return {'data': base64.b64encode(target.read_bytes()).decode('ascii')}
            if operation == 'workspace-state':
                return {'revision': str(self.revision), **self.details()}
            if operation in ('open-folder', 'open-workspace'):
                self._select(str(body.get('path', '')), 'folder' if operation == 'open-folder' else 'workspace')
                self.persist()
                return self.details()
            if operation == 'set-theme':
                theme = body.get('theme')
                if theme not in ('dark', 'light'):
                    raise ValueError('Theme must be dark or light')
                self.theme = theme
                self.persist()
                return {'theme': self.theme}
            raw = str(body.get('path', '/'))
            target = self._path(raw)
            if operation == 'stat':
                if target is None:
                    return {'type': 'directory', 'size': 0, 'mtime': 0}
                info = target.stat()
                return {'type': 'directory' if target.is_dir() else 'file',
                        'size': info.st_size, 'mtime': int(info.st_mtime * 1000)}
            if operation == 'read-directory':
                if target is None:
                    return {'entries': [{'name': label, 'type': 'directory'} for label, _ in self.roots]}
                if not target.exists():
                    raise FileNotFoundError('Workspace directory does not exist')
                if not target.is_dir():
                    raise NotADirectoryError('Not a workspace directory')
                entries = []
                for item in sorted(target.iterdir(), key=lambda p: p.name.casefold()):
                    if len(entries) >= 10000:
                        break
                    # A symlink escaping this workspace cannot be followed.
                    try:
                        self._path(raw.rstrip('/') + '/' + item.name)
                    except (ValueError, PermissionError, OSError):
                        continue
                    entries.append({'name': item.name, 'type': 'directory' if item.is_dir() else 'file'})
                return {'entries': entries}
            if target is None or any(target == root for _, root in self.roots):
                raise ValueError('Cannot modify the workspace root')
            if operation == 'read-file':
                if not target.exists():
                    raise FileNotFoundError('Workspace file does not exist')
                if not target.is_file() or target.stat().st_size > self.MAX_FILE_BYTES:
                    raise ValueError('File is not readable or is over 8 MiB')
                return {'data': base64.b64encode(target.read_bytes()).decode('ascii')}
            if operation == 'write-file':
                raw_data = body.get('data', '')
                if not isinstance(raw_data, str) or len(raw_data) > self.MAX_FILE_BYTES * 2:
                    raise ValueError('File is too large')
                data = base64.b64decode(raw_data, validate=True)
                if len(data) > self.MAX_FILE_BYTES:
                    raise ValueError('File is over 8 MiB')
                if target.exists() and (target.is_dir() or body.get('overwrite') is False):
                    raise FileExistsError('File already exists')
                if not target.exists() and not body.get('create', False):
                    raise FileNotFoundError('File does not exist')
                target.write_bytes(data)
            elif operation == 'create-directory':
                target.mkdir()
            elif operation == 'delete':
                if target.is_dir():
                    if body.get('recursive'):
                        shutil.rmtree(target)
                    else:
                        target.rmdir()
                else:
                    target.unlink()
            elif operation == 'rename':
                destination = self._path(str(body.get('target', '')))
                if destination is None or any(destination == root for _, root in self.roots):
                    raise ValueError('Cannot replace the workspace root')
                if destination.exists() and not body.get('overwrite'):
                    raise FileExistsError('Destination already exists')
                if destination.exists() and destination.is_dir():
                    raise ValueError('Cannot overwrite a directory')
                target.replace(destination)
            else:
                raise ValueError('Unknown Code-OSS workspace operation')
            self.revision += 1
            return {'ok': True}

