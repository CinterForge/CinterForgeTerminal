/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Implements the CinterForge filesystem protocol used by Code-OSS when this
    // provider is explicitly registered. The browser bootstrap currently owns the
    // live cinterforge: workspace provider, so this class remains a reusable provider
    // implementation rather than being silently registered here. Host errors are
    // translated into VS Code filesystem errors at this boundary.

    const vscode = C.vscode;
    const request = C.request;
    const encodeBase64 = C.encodeBase64;
    const decodeBase64 = C.decodeBase64;

    // Code-OSS probes these conventional files/directories even when a project does
    // not provide them. Treating them as optional prevents harmless editor feature
    // discovery from becoming a filesystem error while keeping all other missing
    // paths loud and authoritative.
    const optionalJsonProbes = new Set([
        '/.vscode/settings.json',
        '/.vscode/tasks.json',
        '/.vscode/launch.json',
        '/.vscode/mcp.json'
    ]);

    const optionalDirectoryProbes = new Set([
        '/.github/agents',
        '/.copilot/agents',
        '/.claude/agents'
    ]);

    // -----------------------------------------------------------------------------
    // Optional Workspace Probes
    // -----------------------------------------------------------------------------

    function optionalProbe(path) {
        if (optionalJsonProbes.has(path)) {
            return 'file';
        }

        if (optionalDirectoryProbes.has(path)) {
            return 'directory';
        }

        return '';
    }

    // -----------------------------------------------------------------------------
    // CinterForge Filesystem Provider
    // -----------------------------------------------------------------------------

    class CinterForgeFileSystemProvider {
        constructor() {
            this.emitter = new vscode.EventEmitter();
            this.onDidChangeFile = this.emitter.event;

            this.watchers = 0;
            this.revision = '';
            this.timer = null;
        }

        watch() {
            this.watchers++;
            this.startPolling();

            return new vscode.Disposable(
                () => {
                    this.watchers = Math.max(
                        0,
                        this.watchers - 1
                    );

                    if (
                        this.watchers === 0 &&
                        this.timer
                    ) {
                        clearInterval(this.timer);
                        this.timer = null;
                    }
                }
            );
        }

        startPolling() {
            if (this.timer) {
                return;
            }

            const poll = async () => {
                try {
                    const state = await request(
                        '/code/command',
                        {
                            operation: 'workspace-state'
                        }
                    );

                    const nextRevision = String(
                        state.revision || ''
                    );

                    if (
                        this.revision &&
                        nextRevision &&
                        nextRevision !== this.revision
                    ) {
                        this.emitter.fire([
                            {
                                type: vscode.FileChangeType.Changed,
                                uri: vscode.Uri.parse('cinterforge:/')
                            }
                        ]);
                    }

                    this.revision = nextRevision;
                } catch (_) {
                    // Workspace polling is best-effort.
                }
            };

            void poll();

            this.timer = setInterval(
                poll,
                5000
            );
        }

        async fsRequest(
            uri,
            operation
        ) {
            try {
                return await request(
                    '/code/command',
                    {
                        operation,
                        path: uri.path
                    }
                );
            } catch (error) {
                if (
                    error &&
                    error.code === 'not_found'
                ) {
                    throw vscode.FileSystemError.FileNotFound(
                        uri
                    );
                }

                if (
                    error &&
                    error.code === 'not_directory'
                ) {
                    throw vscode.FileSystemError.FileNotADirectory(
                        uri
                    );
                }

                throw error;
            }
        }

        async stat(uri) {
            try {
                const payload = await this.fsRequest(
                    uri,
                    'stat'
                );

                return {
                    type:
                        payload.type === 'directory'
                            ? vscode.FileType.Directory
                            : vscode.FileType.File,

                    ctime: 0,
                    mtime: payload.mtime || 0,
                    size: payload.size || 0
                };
            } catch (error) {
                const probeType = optionalProbe(
                    uri.path
                );

                if (
                    error &&
                    error.code === 'FileNotFound' &&
                    probeType
                ) {
                    return {
                        type:
                            probeType === 'directory'
                                ? vscode.FileType.Directory
                                : vscode.FileType.File,

                        ctime: 0,
                        mtime: 0,
                        size:
                            probeType === 'file'
                                ? 3
                                : 0
                    };
                }

                throw error;
            }
        }

        async readDirectory(uri) {
            try {
                const payload = await this.fsRequest(
                    uri,
                    'read-directory'
                );

                return (payload.entries || []).map(
                    (entry) => {
                        return [
                            entry.name,
                            entry.type === 'directory'
                                ? vscode.FileType.Directory
                                : vscode.FileType.File
                        ];
                    }
                );
            } catch (error) {
                if (
                    error &&
                    error.code === 'FileNotFound' &&
                    optionalProbe(uri.path) === 'directory'
                ) {
                    return [];
                }

                throw error;
            }
        }

        async readFile(uri) {
            try {
                const payload = await this.fsRequest(
                    uri,
                    'read-file'
                );

                return decodeBase64(
                    payload.data
                );
            } catch (error) {
                if (
                    error &&
                    error.code === 'FileNotFound' &&
                    optionalProbe(uri.path) === 'file'
                ) {
                    return encoder.encode('{}\n');
                }

                throw error;
            }
        }

        async writeFile(
            uri,
            content,
            options
        ) {
            await request(
                '/code/command',
                {
                    operation: 'write-file',
                    path: uri.path,
                    data: encodeBase64(content),
                    create: !!options.create,
                    overwrite: !!options.overwrite
                }
            );

            this.emitter.fire([
                {
                    type: vscode.FileChangeType.Changed,
                    uri
                }
            ]);
        }

        async createDirectory(uri) {
            await request(
                '/code/command',
                {
                    operation: 'create-directory',
                    path: uri.path
                }
            );

            this.emitter.fire([
                {
                    type: vscode.FileChangeType.Created,
                    uri
                }
            ]);
        }

        async delete(
            uri,
            options
        ) {
            await request(
                '/code/command',
                {
                    operation: 'delete',
                    path: uri.path,
                    recursive: !!options.recursive
                }
            );

            this.emitter.fire([
                {
                    type: vscode.FileChangeType.Deleted,
                    uri
                }
            ]);
        }

        async rename(
            oldUri,
            newUri,
            options
        ) {
            await request(
                '/code/command',
                {
                    operation: 'rename',
                    path: oldUri.path,
                    target: newUri.path,
                    overwrite: !!options.overwrite
                }
            );

            this.emitter.fire([
                {
                    type: vscode.FileChangeType.Deleted,
                    uri: oldUri
                },
                {
                    type: vscode.FileChangeType.Created,
                    uri: newUri
                }
            ]);
        }
    }

    C.optionalProbe = optionalProbe;
    C.CinterForgeFileSystemProvider = CinterForgeFileSystemProvider;
})(globalThis.CinterForgeCode);
