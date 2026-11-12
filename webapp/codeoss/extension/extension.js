/* CinterForge Code - OSS web extension coordinator. ASCII source only. */

const vscode = require('vscode');

// Shared namespace for the split Code-OSS extension. The web extension host only
// provides require('vscode'); relative CommonJS require() is not available. Load
// the maintained classic scripts from the extension filesystem during activation
// and execute them in this worker global before the coordinator starts.
const CinterForgeCode = globalThis.CinterForgeCode || {};
globalThis.CinterForgeCode = CinterForgeCode;
CinterForgeCode.vscode = vscode;

const classicScriptPaths = [
    'runtime/ControlTransport.js',
    'runtime/BinaryEncoding.js',
    'filesystem/CinterForgeFileSystemProvider.js',
    'filesystem/WorkspaceSearch.js',
    'terminals/ZMachineTerminal.js',
    'terminals/JavaScriptConsoleTerminal.js',
    'terminals/NativeTerminal.js',
    'terminals/TerminalManager.js',
    'editor/StoryLanguage.js',
    'editor/StoryEditor.js',
    'editor/ThemeManager.js',
    'build/InformCompiler.js',
    'build/StoryBuilder.js',
    'ui/CodeUIBridge.js',
    'acceptance/AcceptanceAPI.js'
];

let classicScriptsLoaded = false;

async function loadClassicScripts(context) {
    if (classicScriptsLoaded) {
        return;
    }

    const decoder = new TextDecoder();

    for (const relativePath of classicScriptPaths) {
        const segments = relativePath.split('/');
        const scriptUri = vscode.Uri.joinPath(
            context.extensionUri,
            ...segments
        );

        const bytes = await vscode.workspace.fs.readFile(scriptUri);
        const source = decoder.decode(bytes);

        // Code-OSS already evaluates the browser entry as classic CommonJS code.
        // Execute each maintained split source in that same worker global. This is
        // deliberately not importScripts, an ES module import, or a bundler.
        const executeClassicScript = new Function(
            source + '\n//# sourceURL=' + scriptUri.toString()
        );

        executeClassicScript.call(globalThis);
    }

    classicScriptsLoaded = true;
}

async function activateCoordinator(context) {
    const {
        request,
        detectTerminalHost,
        registerSearchProviders,
        ZMachineTerminal,
        HostFileSystemProvider,
        compileInform,
        applyStoryLanguage,
        applyIFPlayerTheme,
        findStorySource,
        createNativeTerminalPty,
        projectAliasForEditor,
        terminalTabState,
        acceptanceCodeCommand,
        acceptanceSessionCommand,
        acceptanceSessionState,
        setRuntimeBaseForTests
    } = CinterForgeCode;

    // The browser bootstrap owns cinterforge:. Use VS Code's registered
    // workspace filesystem here too so local file:// Project reads traverse the
    // same provider instead of opening a second server-only /code/command
    // transport.
    const fileSystem =
        vscode.workspace.fs;

    const diagnostics =
        vscode.languages.createDiagnosticCollection(
            'cinterforge-inform'
        );

    context.subscriptions.push(
        diagnostics
    );

    const buildOutput =
        vscode.window.createOutputChannel(
            'CinterForge Build'
        );

    context.subscriptions.push(
        buildOutput
    );

    // The same extension is used by IFPlayer and Terminal. Only Terminal has
    // a host filesystem and native/guest development sessions. Probe the
    // existing Terminal HTTP service; leave IFPlayer's game/editor path alone.
    // Only a missing Terminal workspace API means this is IFPlayer. A failed
    // Terminal lookup must be reported, not silently turned into IF mode.
    let terminalHost = null;
    let terminalHostError = null;
    try {
        terminalHost = await detectTerminalHost();
    } catch (error) {
        terminalHostError = error;
        console.error('[cinterforge] Terminal host discovery failed', error);
        vscode.window.showErrorMessage(
            'CinterForge Terminal workspace unavailable: ' + error.message
        );
    }

    // -------------------------------------------------------------------------
    // Host log links and native file-manager integration (Terminal only)
    // -------------------------------------------------------------------------
    if (terminalHost) {
        const hostFileScheme = 'cinterforge-host';
        context.subscriptions.push(vscode.workspace.registerFileSystemProvider(
            hostFileScheme, new HostFileSystemProvider(), {isReadonly: true}
        ));

        const hostUri = path => vscode.Uri.from({
            scheme: hostFileScheme,
            path: String(path).replace(/\\/g, '/').replace(/^([A-Za-z]:)/, '/$1')
        });

        const openHostPath = async (path, lineNumber = 0) => {
            try {
                const uri = hostUri(path);
                const result = await request('/code/command', {
                    operation: 'host-stat', path: uri.path
                });
                if (result.type === 'directory') {
                    await request('/code/command', {
                        operation: 'reveal-host', path: uri.path
                    });
                } else {
                    const document = await vscode.workspace.openTextDocument(uri);
                    const options = {preview: false};
                    if (lineNumber > 0) {
                        const position = new vscode.Position(lineNumber - 1, 0);
                        options.selection = new vscode.Range(position, position);
                    }
                    await vscode.window.showTextDocument(document, options);
                }
            } catch (error) {
                vscode.window.showErrorMessage('Cannot open host file: ' + error.message);
            }
        };

        context.subscriptions.push(vscode.commands.registerCommand(
            'cinterforge.openHostLog', async () => {
                const path = await vscode.window.showInputBox({
                    prompt: 'Absolute path to a host log or workspace file'
                });
                if (path) await openHostPath(path);
            }
        ));

        context.subscriptions.push(vscode.commands.registerCommand(
            'cinterforge.revealInHostOS', async (resource) => {
                const uri = resource || vscode.window.activeTextEditor?.document.uri ||
                    vscode.workspace.workspaceFolders?.[0]?.uri;
                if (!uri || (uri.scheme !== 'cinterforge' && uri.scheme !== hostFileScheme)) {
                    vscode.window.showWarningMessage('Select a CinterForge workspace or log file first');
                    return;
                }
                try {
                    await request('/code/command', {
                        operation: uri.scheme === hostFileScheme ? 'reveal-host' : 'reveal-workspace',
                        path: uri.path
                    });
                } catch (error) {
                    vscode.window.showErrorMessage('Cannot reveal host file: ' + error.message);
                }
            }
        ));

        // Unlike ordinary VS Code Web file links, these open against the real
        // host filesystem, not the browser's virtual (cinterforge:/) workspace.
        // Match explicit absolute paths to files, including log lines with :line.
        if (typeof vscode.window.registerTerminalLinkProvider === 'function') {
            const fileEnding = '(?:log|txt|json|jsonl|md|py|js|ts|cpp|c|h|html|yaml|yml|out|err)';
            // Labeled paths may contain spaces (for example "CinterForge Terminal").
            // Bare terminal links must not swallow surrounding prose or URLs.
            const labeledPaths = new RegExp('\\bpath=((?:[A-Za-z]:[\\\\/]|/)[^\\r\\n<>"\\x27]*?\\.' +
                fileEnding + ')(?::(\\d+))?(?=$|[\\s\\]),;:])', 'gi');
            const barePaths = new RegExp('(?:[A-Za-z]:[\\\\/]|/)[^\\s\\r\\n<>"\\x27()]+?\\.' +
                fileEnding + '(?::(\\d+))?(?=$|[\\s\\]),;:])', 'gi');
            context.subscriptions.push(vscode.window.registerTerminalLinkProvider({
                provideTerminalLinks(context) {
                    const links = [];
                    const labeledRanges = [];
                    for (const match of context.line.matchAll(labeledPaths)) {
                        const path = match[1];
                        const offset = match[0].indexOf(path);
                        const start = match.index + offset;
                        const length = path.length + (match[2] ? match[2].length + 1 : 0);
                        labeledRanges.push([start, start + length]);
                        links.push({startIndex: start, length, path,
                            line: match[2] ? Number(match[2]) : 0,
                            tooltip: 'Open on CinterForge host'});
                    }
                    for (const match of context.line.matchAll(barePaths)) {
                        if (labeledRanges.some(([first, last]) => match.index >= first && match.index < last)) continue;
                        // The trailing 's:/' in https:// is not a Windows disk.
                        const prefix = context.line.slice(Math.max(0, match.index - 6), match.index);
                        if (/https?$/i.test(prefix)) continue;
                        links.push({startIndex: match.index, length: match[0].length,
                            path: match[0].replace(/:\d+$/, ''),
                            line: match[1] ? Number(match[1]) : 0,
                            tooltip: 'Open on CinterForge host'});
                    }
                    return links;
                },
                handleTerminalLink(link) {
                    return openHostPath(link.path, link.line);
                }
            }));
        }
    }

    // -------------------------------------------------------------------------
    // Editor Configuration
    // -------------------------------------------------------------------------

    // CinterForge authoring uses an explicit Edit -> Save -> Build -> Test
    // contract. Auto Save hides the dirty indicator and makes it impossible for
    // the author to tell whether the bytes they are about to Build were
    // explicitly saved.
    const filesConfig =
        vscode.workspace.getConfiguration(
            'files'
        );

    if (
        String(
            filesConfig.get('autoSave') ||
            'off'
        ) !== 'off'
    ) {
        Promise.resolve(
            filesConfig.update(
                'autoSave',
                'off',
                vscode.ConfigurationTarget.Global
            )
        ).catch(
            (error) => {
                console.error(
                    '[cinterforge] failed to disable Auto Save',
                    error
                );
            }
        );
    }

    // Code Web can open virtual-workspace files before its extension-based file
    // association pass has assigned the contributed language. Make the story
    // associations authoritative at the CinterForge extension boundary as well.

    for (
        const document of
        vscode.workspace.textDocuments || []
    ) {
        applyStoryLanguage(document);
    }

    context.subscriptions.push(
        vscode.workspace.onDidOpenTextDocument(
            applyStoryLanguage
        )
    );

    // cinterforge: is registered by the Code browser bootstrap before workspace
    // initialization.
    registerSearchProviders(
        context,
        fileSystem
    );

    // Code is embedded inside IFPlayer, not a standalone browser application.
    // The host owns navigation and lifetime, so Code must not install a browser
    // beforeunload confirmation that traps normal CinterForge exit/reload.
    //
    // Dirty editors remain dirty inside Code. This only disables the
    // browser-level leave-page prompt.
    const windowConfig =
        vscode.workspace.getConfiguration(
            'window'
        );

    if (
        String(
            windowConfig.get(
                'confirmBeforeClose'
            ) || ''
        ) !== 'never'
    ) {
        Promise.resolve(
            windowConfig.update(
                'confirmBeforeClose',
                'never',
                vscode.ConfigurationTarget.Global
            )
        ).catch(() => {
            // This setting is helpful but not required for activation.
        });
    }

    // -------------------------------------------------------------------------
    // Theme Synchronization
    // -------------------------------------------------------------------------

    // The workbench is launched with cinterforge:/ as its real virtual
    // workspace. `onFileSystem:cinterforge` activates this extension so the
    // provider is registered for that workspace.
    //
    // Do not call updateWorkspaceFolders here. Code Web canonicalizes that API
    // call into a navigation/reload, which races provider startup and loses the
    // workspace.

    // IFPlayer's browser L-key state is the sole theme authority. The extension
    // host asks for the current value once and then consumes direct browser
    // broadcasts.
    let themeChannel = null;

    if (terminalHost) {
        // One shared preference in TerminalState survives WebView restarts.
        await applyIFPlayerTheme(terminalHost.theme !== 'light');
    }

    if (!terminalHost && typeof BroadcastChannel === 'function') {
        themeChannel =
            new BroadcastChannel(
                'cinterforge-ifplayer-theme'
            );

        let initialThemeReceived = false;
        let initialThemeRequestTimer = null;

        const requestInitialTheme = () => {
            if (
                initialThemeReceived ||
                !themeChannel
            ) {
                return;
            }

            themeChannel.postMessage({
                type: 'request-theme'
            });

            initialThemeRequestTimer =
                setTimeout(
                    requestInitialTheme,
                    100
                );
        };

        themeChannel.onmessage = (
            event
        ) => {
            if (
                event?.data?.type !== 'theme'
            ) {
                return;
            }

            initialThemeReceived = true;

            if (
                initialThemeRequestTimer !==
                null
            ) {
                clearTimeout(
                    initialThemeRequestTimer
                );

                initialThemeRequestTimer =
                    null;
            }

            void applyIFPlayerTheme(
                !!event.data.dark
            );
        };

        // BroadcastChannel messages are not queued for listeners that do not yet
        // exist. Code can activate before IFPlayer has installed its responder,
        // so retry only the startup handshake until the first authoritative
        // theme arrives. Live theme changes remain event-driven.
        requestInitialTheme();

        // Code's web extension host may run in a worker or iframe realm whose
        // BroadcastChannel delivery differs from the IFPlayer page. Keep the
        // host's tiny theme state as the cross-realm source of truth as well.
        //
        // This transports presentation state only. Gameplay remains
        // browser-owned.
        let themeSyncBusy = false;

        const syncHostTheme = async () => {
            if (themeSyncBusy) {
                return;
            }

            themeSyncBusy = true;

            try {
                const payload = await request(
                    '/host/theme',
                    null,
                    'GET'
                );

                if (
                    typeof payload.dark ===
                    'boolean'
                ) {
                    await applyIFPlayerTheme(
                        payload.dark
                    );
                }
            } catch (_) {
                // Theme startup synchronization is best-effort.
            } finally {
                themeSyncBusy = false;
            }
        };

        // One startup synchronization is enough. Subsequent theme changes are
        // delivered by the BroadcastChannel above. Do not poll /host/theme.
        void syncHostTheme();

        context.subscriptions.push({
            dispose() {
                if (
                    initialThemeRequestTimer !==
                    null
                ) {
                    clearTimeout(
                        initialThemeRequestTimer
                    );
                }

                themeChannel?.close();
            }
        });
    }

    // Expose an obvious dark/light switch inside the real Code-OSS status bar.
    const themeStatus = terminalHost
        ? vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100)
        : null;
    if (themeStatus) {
        themeStatus.text = terminalHost.theme === 'light' ? '$(color-mode) Light' : '$(color-mode) Dark';
        themeStatus.tooltip = 'Switch Code-OSS between dark and light themes';
        themeStatus.command = 'cinterforge.toggleColorTheme';
        themeStatus.show();
        context.subscriptions.push(themeStatus);
    }

    if (terminalHost) {
        // Both commands accept any existing local path on the host. Code's
        // browser does not have access to the operating system's file picker,
        // so requesting the absolute path is more reliable than pretending the
        // virtual cinterforge:/ tree is the user's entire local filesystem.
        const changeWorkspace = async (kind) => {
            const isFolder = kind === 'folder';
            const selected = await vscode.window.showInputBox({
                title: isFolder ? 'Open Local Folder' : 'Open Local Workspace',
                prompt: isFolder
                    ? 'Full path to a folder on this Terminal host'
                    : 'Full path to an existing .code-workspace file on this Terminal host',
                value: terminalHost.path,
                ignoreFocusOut: true
            });
            if (selected === undefined) return;
            try {
                await request('/code/command', {
                    operation: isFolder ? 'open-folder' : 'open-workspace',
                    path: selected.trim()
                });
                // The browser's bootstrap provider mounts cinterforge:/ before
                // Code initializes. Reload the workbench after changing roots;
                // this also recreates integrated shells in the new directory.
                await vscode.commands.executeCommand('workbench.action.reloadWindow');
            } catch (error) {
                vscode.window.showErrorMessage('Cannot open ' + kind + ': ' + error.message);
            }
        };
        context.subscriptions.push(
            vscode.commands.registerCommand('cinterforge.openFolder', () => changeWorkspace('folder')),
            vscode.commands.registerCommand('cinterforge.openWorkspace', () => changeWorkspace('workspace'))
        );
    }

    // -------------------------------------------------------------------------
    // Terminal Configuration
    // -------------------------------------------------------------------------

    // Code's built-in terminal + button should create another CinterForge PTY,
    // not an unavailable browser/default shell profile.
    const terminalConfig =
        vscode.workspace.getConfiguration(
            'terminal.integrated'
        );

    const terminalProfileKeys = [
        'defaultProfile.osx',
        'defaultProfile.windows',
        'defaultProfile.linux'
    ];

    for (
        const key of terminalProfileKeys
    ) {
        Promise.resolve(
            terminalConfig.update(
                key,
                'CinterForge Terminal',
                vscode.ConfigurationTarget.Global
            )
        ).catch(() => {
            // Profile configuration should not block activation.
        });
    }

    // Keep Code's instance model enabled internally. CinterForge promotes those
    // instances to peer panel tabs and hides Code's duplicate instance list.
    //
    // CinterForge terminals are real Code PTYs, not pseudo-tabs.
    //
    // In the local editor keep Code's terminal instance-tab strip hidden because
    // WebKit renders PTY names as pill-like buttons. Server authoring needs both
    // real terminal instances visible at once: the browser Z-Machine PTY and the
    // native OS shell.
    const serverAuthoring =
        globalThis.location?.protocol !==
        'file:';

    Promise.resolve(
        terminalConfig.update(
            'tabs.enabled',
            serverAuthoring,
            vscode.ConfigurationTarget.Global
        )
    ).catch(() => {
        // Terminal tab presentation is not required for activation.
    });

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.toggleColorTheme',
            async () => {
                if (terminalHost) {
                    const next = terminalHost.theme === 'light' ? 'dark' : 'light';
                    await request('/code/command', {operation: 'set-theme', theme: next});
                    terminalHost.theme = next;
                    await applyIFPlayerTheme(next === 'dark');
                    themeStatus.text = next === 'dark' ? '$(color-mode) Dark' : '$(color-mode) Light';
                } else {
                    themeChannel?.postMessage({type: 'request-theme'});
                }
            }
        )
    );

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.compileInform',
            () => {
                return compileInform(
                    diagnostics
                );
            }
        )
    );

    let nativeSerial = 0;
    const codeWindowId = Math.random().toString(36).slice(2);

    const nativeTerminals =
        new Map();

    let zMachinePty = null;

    const openZMachineInterpreter = () => {
        const existingTerminal =
            (vscode.window.terminals || [])
                .find(
                    (terminal) => {
                        return (
                            terminal.name ===
                            'Z-Machine'
                        );
                    }
                );

        if (existingTerminal) {
            existingTerminal.show();
            return existingTerminal;
        }

        zMachinePty =
            new ZMachineTerminal();

        const terminal =
            vscode.window.createTerminal({
                name: 'Z-Machine',
                pty: zMachinePty
            });

        terminal.show();

        return terminal;
    };

    const showVerifiedReplayLine = (
        line
    ) => {
        const terminal =
            openZMachineInterpreter();

        terminal.show();

        if (zMachinePty) {
            const text = String(
                line || ''
            ).replace(
                /\r?\n/g,
                '\r\n'
            );

            zMachinePty.writeEmitter.fire(
                `${text}\r\n`
            );
        }
    };

    class AssetServerTerminal {
        constructor() {
            this.writeEmitter = new vscode.EventEmitter();
            this.onDidWrite = this.writeEmitter.event;
            this.closeEmitter = new vscode.EventEmitter();
            this.onDidClose = this.closeEmitter.event;
            this.active = false;
            this.cursor = 0;
        }

        open() {
            this.active = true;
            this.writeEmitter.fire('CinterForge Minecraft Asset Test Server\r\n');
            this.writeEmitter.fire('Server console input is sent directly to Bedrock.\r\n\r\n');
            void this.poll();
        }

        close() {
            this.active = false;
        }

        write(line) {
            const text = String(line || '').replace(/\r?\n/g, '\r\n');
            this.writeEmitter.fire(`${text}\r\n`);
        }

        handleInput(data) {
            if (!data) {
                return;
            }

            const input = String(data);
            for (const character of input) {
                if (character === '\r' || character === '\n') {
                    this.writeEmitter.fire('\r\n');
                    continue;
                }
                if (character === '\x7f' || character === '\b') {
                    this.writeEmitter.fire('\b \b');
                    continue;
                }
                if (character >= ' ') {
                    this.writeEmitter.fire(character);
                }
            }

            void request('/assets/command', {
                operation: 'asset-test-server-input',
                data: input
            }).catch((error) => {
                this.writeEmitter.fire(`ERROR: ${error.message || error}\r\n`);
            });
        }

        async poll() {
            while (this.active) {
                try {
                    const payload = await request('/assets/command', {
                        operation: 'asset-test-server-output',
                        cursor: this.cursor
                    });
                    const rows = Array.isArray(payload.rows) ? payload.rows : [];
                    for (const row of rows) {
                        this.cursor = Math.max(this.cursor, Number(row.sequence || 0));
                        this.writeEmitter.fire(`${String(row.text || '')}\r\n`);
                    }
                    await new Promise((resolve) => setTimeout(resolve, rows.length ? 25 : 250));
                } catch (error) {
                    this.writeEmitter.fire(`ERROR: ${error.message || error}\r\n`);
                    await new Promise((resolve) => setTimeout(resolve, 500));
                }
            }
        }
    }

    class AssetTaskTerminal {
        constructor(title) {
            this.title = String(title || 'Asset Task');
            this.writeEmitter = new vscode.EventEmitter();
            this.onDidWrite = this.writeEmitter.event;
        }

        open() {
            this.writeEmitter.fire(`CinterForge ${this.title}\r\n\r\n`);
        }

        close() {}
        handleInput() {}
        write(line) {
            this.writeEmitter.fire(`${String(line || '')}\r\n`);
        }
    }

    let assetServerTerminal = null;
    let assetServerPty = null;
    let assetTaskTerminal = null;
    let assetTaskPty = null;

    const openAssetServerTerminal = () => {
        if (assetServerTerminal) {
            assetServerTerminal.show();
            return assetServerTerminal;
        }
        assetServerPty = new AssetServerTerminal();
        assetServerTerminal = vscode.window.createTerminal({
            name: 'Minecraft Server',
            pty: assetServerPty
        });
        assetServerTerminal.show();
        return assetServerTerminal;
    };

    const openAssetTaskTerminal = (title) => {
        if (assetTaskTerminal) {
            assetTaskTerminal.dispose();
        }
        assetTaskPty = new AssetTaskTerminal(title);
        assetTaskTerminal = vscode.window.createTerminal({
            name: String(title || 'Blockify All'),
            pty: assetTaskPty
        });
        assetTaskTerminal.show();
        return assetTaskTerminal;
    };

    const hostPlatformName = terminalHost?.platform === 'mac' ? 'Mac' :
        terminalHost?.platform === 'win' ? 'Windows' : 'Linux';

    const openNativeTerminal = (backend = 'native', label = null) => {
        nativeSerial++;
        const id = `terminal-${codeWindowId}-${nativeSerial}`;
        const pty = createNativeTerminalPty(id, backend);
        const terminal = vscode.window.createTerminal({
            name: label || (terminalHost ? (
                backend === 'server' ? 'Server Log' :
                backend === 'linux' ? 'Linux' :
                backend === 'win' ? 'Windows' : hostPlatformName
            ) : `Terminal ${nativeSerial}`),
            pty
        });

        nativeTerminals.set(
            terminal,
            id
        );

        terminal.show();

        return terminal;
    };

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.openIFTerminal',
            openZMachineInterpreter
        )
    );

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.newTerminal',
            openNativeTerminal
        )
    );

    // The pinned Code-OSS producer registers these command IDs into the real
    // MenubarTerminalMenu. The extension supplies the existing PTY operations.
    if (terminalHost) {
        for (const system of ['mac', 'linux', 'win']) {
            await vscode.commands.executeCommand('setContext',
                'cinterforge.host' + (system === 'win' ? 'Windows' :
                    system === 'mac' ? 'Mac' : 'Linux'),
                terminalHost.platform === system);
        }
        await vscode.commands.executeCommand('setContext', 'cinterforge.hostTerminal', true);
    }
    for (const [id, platform, label] of [
        ['cinterforge.newMacTerminal', 'mac', 'Mac'],
        ['cinterforge.newLinuxTerminal', 'linux', 'Linux'],
        ['cinterforge.newWindowsTerminal', 'win', 'Windows'],
        ['cinterforge.newServerLogTerminal', 'server', 'Server Log']
    ]) {
        context.subscriptions.push(vscode.commands.registerCommand(id, () => {
            if (!terminalHost) return;
            const host = terminalHost.platform;
            if (platform !== 'server' && platform !== host && host !== 'mac') {
                return vscode.window.showErrorMessage(label + ' guest is unavailable on this host');
            }
            const backend = platform === host ? 'native' :
                platform;
            return openNativeTerminal(backend, label);
        }));
    }

    // Native File/Terminal menus and the outer HTML toolbar invoke this same
    // backend selector. These are integrated Code-OSS terminals: never create
    // a second CinterForge Session or an unlabeled browser-only shell.
    const codeCommandChannel = terminalHost && typeof BroadcastChannel === 'function'
        ? new BroadcastChannel('cinterforge-terminal-code-commands') : null;
    if (codeCommandChannel) {
        codeCommandChannel.onmessage = (event) => {
            const request = event.data || {};
            if (request.type === 'new-terminal') {
                const backend = String(request.backend || 'native');
                const supported = ['mac', 'native', 'linux', 'win', 'server', 'minecraft', 'if'];
                if (!supported.includes(backend)) return;
                if (backend === 'if') {
                    openZMachineInterpreter();
                } else if (backend === 'minecraft') {
                    openNativeTerminal('minecraft', 'Minecraft');
                } else {
                    const target = backend === 'mac' ? 'native' : backend;
                    const label = target === 'server' ? 'Server Log' :
                        target === 'linux' ? 'Linux' :
                        target === 'win' ? 'Windows' : hostPlatformName;
                    openNativeTerminal(target, label);
                }
            } else if (request.type === 'vscode-command' &&
                       typeof request.command === 'string' &&
                       /^(workbench\.|editor\.|references\.|cinterforge\.|undo$|redo$)/.test(request.command)) {
                void vscode.commands.executeCommand(request.command).catch(error =>
                    vscode.window.showErrorMessage('Code menu: ' + error.message));
            }
        };
        context.subscriptions.push({dispose: () => codeCommandChannel.close()});
    }


    if (
        typeof vscode.window.registerTerminalProfileProvider ===
        'function'
    ) {
        const terminalProfileProvider =
            vscode.window.registerTerminalProfileProvider(
                'cinterforge.native',
                {
                    provideTerminalProfile() {
                        nativeSerial++;

                        const id =
                            `terminal-${codeWindowId}-${nativeSerial}`;

                        const pty =
                            createNativeTerminalPty(id);

                        return new vscode.TerminalProfile({
                            name: terminalHost ? hostPlatformName :
                                `Terminal ${nativeSerial}`,
                            pty
                        });
                    }
                }
            );

        context.subscriptions.push(
            terminalProfileProvider
        );
    }

    if (
        typeof vscode.window.onDidCloseTerminal ===
        'function'
    ) {
        context.subscriptions.push(
            vscode.window.onDidCloseTerminal(
                (terminal) => {
                    // A web terminal can be closed by Code itself when its PTY
                    // cannot be restored. Never recreate it from the close
                    // callback: doing so creates a zero-delay close/reopen loop
                    // that can peg the extension host and make the entire
                    // embedded workbench appear frozen.
                    //
                    // The initial tabs are still created below, and the
                    // toolbar/commands can reopen either surface.
                    nativeTerminals.delete(
                        terminal
                    );

                    if (
                        terminal ===
                        vscode.window.activeTerminal
                    ) {
                        setTimeout(
                            publishTerminalTabs,
                            0
                        );
                    }
                }
            )
        );
    }

    // -------------------------------------------------------------------------
    // Build Story
    // -------------------------------------------------------------------------

    const buildStory = async () => {
        buildOutput.clear();
        diagnostics.clear();

        buildOutput.appendLine(
            '[Build] Building current Inform story...'
        );

        buildOutput.show(true);

        const activeEditor =
            vscode.window.activeTextEditor;

        if (
            activeEditor &&
            activeEditor.document.isDirty
        ) {
            await activeEditor.document.save();
        }

        // Delegate composite-project selection to the build policy helper so
        // the same path rules are not duplicated inside activation.
        const projectAlias =
            projectAliasForEditor(activeEditor);

        const buildStart = await request(
            '/host/build-story-start',
            projectAlias
                ? {
                    story: projectAlias
                }
                : {}
        );

        if (
            buildStart.requires_story_selection
        ) {
            throw new Error(
                'Frobozz Build requires an active source file under ' +
                'Project/<story>. Open a source file for CCA, Zork I, ' +
                'Zork II, or Zork III and Build again.'
            );
        }

        let cursor = 0;
        let state = 'running';
        let error = '';

        let reloadRequired = true;
        let builtStory = '';
        let buildUrl = '';

        const groupedDiagnostics =
            new Map();

        const addProblem = (line) => {
            const text = String(
                line || ''
            );

            const match = text.match(
                /([^\s:]+\.ni):(\d+)(?::(\d+))?[: ]+(.+)/i
            );

            if (!match) {
                return;
            }

            const filename =
                match[1]
                    .split(/[\\/]/)
                    .pop();

            const uri = vscode.Uri.parse(
                `cinterforge:/Project/${filename}`
            );

            const key =
                uri.toString();

            const row =
                groupedDiagnostics.get(key) || {
                    uri,
                    items: []
                };

            const lineNumber = Math.max(
                0,
                Number(match[2]) - 1
            );

            const column = Math.max(
                0,
                Number(match[3] || 1) - 1
            );

            row.items.push(
                new vscode.Diagnostic(
                    new vscode.Range(
                        lineNumber,
                        column,
                        lineNumber,
                        column + 1
                    ),
                    match[4],
                    vscode.DiagnosticSeverity.Error
                )
            );

            groupedDiagnostics.set(
                key,
                row
            );

            diagnostics.set(
                row.uri,
                row.items
            );
        };

        while (state === 'running') {
            await new Promise(
                (resolve) => {
                    setTimeout(
                        resolve,
                        100
                    );
                }
            );

            const poll = await request(
                '/host/build-story-output',
                {
                    cursor
                }
            );

            cursor = Number(
                poll.cursor || cursor
            );

            state = String(
                poll.state || 'failed'
            );

            error = String(
                poll.error || ''
            );

            reloadRequired =
                poll.reload_required !==
                false;

            builtStory = String(
                poll.story || builtStory
            );

            buildUrl = String(
                poll.url || buildUrl
            ).trim();

            for (
                const line of poll.lines || []
            ) {
                const text =
                    String(line);

                buildOutput.appendLine(
                    text
                );

                addProblem(text);

                if (
                    text.startsWith(
                        '[story-build-replay] '
                    )
                ) {
                    const replayLine =
                        text.slice(
                            '[story-build-replay] '.length
                        );

                    showVerifiedReplayLine(
                        replayLine
                    );
                }
            }

            buildOutput.show(true);
        }

        if (state !== 'complete') {
            if (error) {
                buildOutput.appendLine(
                    `[Build] FAILED: ${error}`
                );
            }

            buildOutput.show(true);

            throw new Error(
                error ||
                'Story build failed'
            );
        }

        if (buildUrl) {
            buildOutput.appendLine(
                `[Build] Cloudflare URL: ${buildUrl}`
            );
        }

        if (!reloadRequired) {
            buildOutput.appendLine(
                `[Build] SUCCESS - ${builtStory || 'Frobozz story'} ` +
                'built, published, and staged. Frobozz keeps its ' +
                'multi-story editor session running; relaunch Frobozz ' +
                'to load the rebuilt runtime.'
            );

            buildOutput.show(true);
            return;
        }

        buildOutput.appendLine(
            '[Build] SUCCESS - published to Cloudflare; reloading ' +
            'Z-Machine and Session from the new runtime...'
        );

        buildOutput.show(true);

        const reloadRequest =
            await request(
                '/host/runtime-reload-request',
                {}
            );

        const reloadRevision = Number(
            reloadRequest.revision || 0
        );

        const reloadStoryUrl = String(
            reloadRequest.story_url || ''
        ).trim();

        if (!reloadStoryUrl) {
            throw new Error(
                'Build reload response did not provide the staged story URL'
            );
        }

        if (
            typeof BroadcastChannel ===
            'function'
        ) {
            const reloadChannel =
                new BroadcastChannel(
                    'cinterforge-ifplayer-code-ui'
                );

            reloadChannel.postMessage({
                type: 'runtime-reload-requested',
                revision: reloadRevision,
                story_url: reloadStoryUrl
            });

            reloadChannel.close();
        }

        let reloadResult = {
            state: 'timeout'
        };

        const deadline =
            Date.now() + 10000;

        while (
            Date.now() < deadline
        ) {
            await new Promise(
                (resolve) => {
                    setTimeout(
                        resolve,
                        100
                    );
                }
            );

            const status = await request(
                '/host/runtime-reload-state',
                null,
                'GET'
            );

            if (
                Number(
                    status.acknowledged || 0
                ) !== reloadRevision
            ) {
                continue;
            }

            reloadResult = {
                state: String(
                    status.state ||
                    'failed'
                ),

                error: String(
                    status.error || ''
                )
            };

            break;
        }

        if (
            reloadResult.state ===
            'ready'
        ) {
            buildOutput.appendLine(
                '[Build] RELOAD COMPLETE - Z-Machine and Session are ' +
                'running the new runtime.'
            );

            console.info(
                '[Build] reload complete',
                {
                    revision:
                        reloadRevision
                }
            );
        } else if (
            reloadResult.state ===
            'failed'
        ) {
            const reloadError = String(
                reloadResult.error ||
                'runtime reload failed'
            );

            buildOutput.appendLine(
                `[Build] RELOAD FAILED: ${reloadError}`
            );

            console.error(
                '[Build] reload failed',
                reloadResult
            );

            buildOutput.show(true);

            throw new Error(
                reloadError
            );
        } else {
            buildOutput.appendLine(
                '[Build] RELOAD UNCONFIRMED - timed out waiting for ' +
                'the runtime-ready acknowledgement.'
            );

            console.warn(
                '[Build] reload acknowledgement timed out',
                {
                    revision:
                        reloadRevision
                }
            );
        }

        buildOutput.show(true);

        return {
            ok: true
        };
    };

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.buildStory',
            buildStory
        )
    );

    // -------------------------------------------------------------------------
    // Story Commands
    // -------------------------------------------------------------------------

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.loadStory',
            async () => {
                // Load Story is deliberately browser-local. The outer IFPlayer
                // document owns the directory picker and can load a built
                // Story/Assets package even when the Python authoring server is
                // absent.
                if (
                    typeof BroadcastChannel !==
                    'function'
                ) {
                    vscode.window.showErrorMessage(
                        'Load Story requires browser directory access.'
                    );

                    return;
                }

                const channel =
                    new BroadcastChannel(
                        'cinterforge-ifplayer-code-ui'
                    );

                channel.postMessage({
                    type: 'open-local-world'
                });

                channel.close();
            }
        )
    );

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.newStory',
            async () => {
                const name =
                    await vscode.window.showInputBox({
                        title: 'New Story',
                        prompt: 'Story name',
                        placeHolder: 'My Story',
                        ignoreFocusOut: true
                    });

                if (name === undefined) {
                    return;
                }

                const trimmedName =
                    String(name || '').trim();

                if (!trimmedName) {
                    vscode.window.showErrorMessage(
                        'Story name is required.'
                    );

                    return;
                }

                try {
                    const created =
                        await request(
                            '/host/new-story',
                            {
                                name:
                                    trimmedName
                            }
                        );

                    vscode.window.showInformationMessage(
                        `Created ${created.story_id}`
                    );
                } catch (error) {
                    vscode.window.showErrorMessage(
                        `New Story failed: ${error.message || error}`
                    );
                }
            }
        )
    );

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'cinterforge.importStory',
            () => {
                vscode.window.showInformationMessage(
                    'Import Story is not implemented yet.'
                );
            }
        )
    );

    // -------------------------------------------------------------------------
    // Code UI Bridge
    // -------------------------------------------------------------------------

    let codeUiChannel = null;

    const publishTerminalTabs = () => {
        if (!codeUiChannel) {
            return;
        }

        // The UI helper owns filtering and active-tab serialization. This bridge
        // only decides when the current terminal state must be published.
        codeUiChannel.postMessage(
            terminalTabState()
        );
    };

    if (
        typeof BroadcastChannel ===
        'function'
    ) {
        codeUiChannel =
            new BroadcastChannel(
                'cinterforge-ifplayer-code-ui'
            );

        codeUiChannel.onmessage = (
            event
        ) => {
            const type =
                event?.data?.type;

            if (type === 'build-story') {
                codeUiChannel.postMessage({
                    type: 'build-story-state',
                    building: true
                });
                void buildStory()
                    .catch(
                        (error) => {
                            vscode.window.showErrorMessage(
                                `Build failed to start: ${error.message || error}`
                            );
                        }
                    )
                    .finally(
                        () => {
                            codeUiChannel.postMessage({
                                type: 'build-story-state',
                                building: false
                            });
                        }
                    );
            }

            if (type === 'new-story') {
                void vscode.commands.executeCommand(
                    'cinterforge.newStory'
                );
            }

            if (type === 'import-story') {
                void vscode.commands.executeCommand(
                    'cinterforge.importStory'
                );
            }

            if (type === 'load-story') {
                void vscode.commands.executeCommand(
                    'cinterforge.loadStory'
                );
            }

            if (type === 'pretty-print') {
                void vscode.commands
                    .executeCommand(
                        'editor.action.formatDocument'
                    )
                    .then(
                        undefined,
                        (error) => {
                            vscode.window.showErrorMessage(
                                `Pretty Print failed: ${error.message || error}`
                            );
                        }
                    );
            }

            if (type === 'asset-server-terminal') {
                openAssetServerTerminal();
                setTimeout(publishTerminalTabs, 0);
            }

            if (type === 'asset-server-terminal-write') {
                if (!assetServerPty) {
                    openAssetServerTerminal();
                }
                assetServerPty.write(String(event.data.text || ''));
            }

            if (type === 'asset-task-terminal-open') {
                openAssetTaskTerminal(String(event.data.title || 'Blockify All'));
                setTimeout(publishTerminalTabs, 0);
            }

            if (type === 'asset-task-terminal-write') {
                if (!assetTaskPty) {
                    openAssetTaskTerminal(String(event.data.title || 'Blockify All'));
                }
                assetTaskPty.write(String(event.data.text || ''));
            }

            if (type === 'asset-native-terminal-command') {
                const terminal = openNativeTerminal();
                const command = String(event.data.command || '').trim();
                if (command) {
                    terminal.sendText(command, true);
                }
                setTimeout(publishTerminalTabs, 0);
            }

            if (type === 'new-terminal') {
                openNativeTerminal();

                setTimeout(
                    publishTerminalTabs,
                    0
                );
            }

            if (
                type === 'activate-terminal'
            ) {
                const name = String(
                    event.data.name || ''
                );

                let terminal =
                    (vscode.window.terminals || [])
                        .find(
                            (item) => {
                                return (
                                    item.name ===
                                    name
                                );
                            }
                        );

                // Keep Project startup lightweight. The visible peer tabs are
                // the affordance; instantiate their PTYs only when the user
                // selects one. This prevents Code activation from opening two
                // polling PTYs while the workbench itself is still restoring.
                if (
                    !terminal &&
                    name === 'Z-Machine'
                ) {
                    terminal =
                        openZMachineInterpreter();
                } else if (
                    !terminal &&
                    /^Terminal \d+$/.test(
                        name
                    )
                ) {
                    terminal =
                        openNativeTerminal();
                }

                if (terminal) {
                    terminal.show();
                }

                setTimeout(
                    publishTerminalTabs,
                    0
                );
            }
        };

        context.subscriptions.push({
            dispose() {
                codeUiChannel?.close();
            }
        });

        if (
            typeof vscode.window.onDidOpenTerminal ===
            'function'
        ) {
            context.subscriptions.push(
                vscode.window.onDidOpenTerminal(
                    () => {
                        setTimeout(
                            publishTerminalTabs,
                            0
                        );
                    }
                )
            );
        }

        if (
            typeof vscode.window.onDidChangeActiveTerminal ===
            'function'
        ) {
            context.subscriptions.push(
                vscode.window.onDidChangeActiveTerminal(
                    () => {
                        setTimeout(
                            publishTerminalTabs,
                            0
                        );
                    }
                )
            );
        }

        if (
            typeof vscode.window.onDidCloseTerminal ===
            'function'
        ) {
            context.subscriptions.push(
                vscode.window.onDidCloseTerminal(
                    () => {
                        setTimeout(
                            publishTerminalTabs,
                            0
                        );
                    }
                )
            );
        }
    }

    // -------------------------------------------------------------------------
    // Active Story Restoration
    // -------------------------------------------------------------------------

    // The active story source is the editor's permanent document. Browser-local
    // Code state may remember a path from an older launch. Always open the
    // current workspace contract instead.
    //
    // If the user closes the last editor, reopen story.ni rather than leaving
    // Code as an empty generic workbench.
    let reopeningStory = false;

    const ensureStoryEditor = async () => {
        if (reopeningStory) {
            return;
        }

        reopeningStory = true;

        try {
            // A project switch keeps the Code workbench process/browser state.
            // If its restored active tab names a file that does not exist in the
            // new Project facade, close only that stale editor before selecting
            // the new source.
            const activeEditor =
                vscode.window.activeTextEditor;

            if (
                activeEditor?.document?.uri
                    ?.scheme ===
                'cinterforge'
            ) {
                try {
                    await fileSystem.stat(
                        activeEditor.document.uri
                    );
                } catch (_) {
                    await vscode.commands.executeCommand(
                        'workbench.action.closeActiveEditor'
                    );
                }
            }

            const candidates = [
                '/Project/Source/story.ni',
                '/Project/Source/Source/story.ni'
            ];

            let storyUri = null;

            for (
                const candidate of candidates
            ) {
                try {
                    const probe =
                        vscode.Uri.parse(
                            `cinterforge:${candidate}`
                        );

                    await fileSystem.stat(
                        probe
                    );

                    storyUri = probe;
                    break;
                } catch (_) {
                    // Continue to the next canonical source location.
                }
            }

            const findStorySourceInWorkspace =
                (rootPath) => {
                    return findStorySource(
                        fileSystem,
                        rootPath
                    );
                };

            if (!storyUri) {
                storyUri =
                    await findStorySourceInWorkspace(
                        '/Project/Source'
                    );
            }

            // Frobozz is a composite workspace. Its Project root intentionally
            // has no Source child; instead it owns
            // Project/{cca,zork1,zork2,zork3}.
            //
            // Always restore the CCA Lead on entry so stale browser editor state
            // cannot become the active build source.
            if (!storyUri) {
                storyUri =
                    await findStorySourceInWorkspace(
                        '/Project/cca'
                    );
            }

            if (!storyUri) {
                throw new Error(
                    'Project contains no active Inform or ZIL source file'
                );
            }

            const document =
                await vscode.workspace.openTextDocument(
                    storyUri
                );

            await vscode.window.showTextDocument(
                document,
                {
                    preview: false,
                    preserveFocus: false
                }
            );
        } catch (error) {
            console.error(
                '[cinterforge] active story source unavailable',
                error
            );
        } finally {
            reopeningStory = false;
        }
    };

    // Do not force story.ni back open when the active editor changes. Code
    // briefly reports no active text editor while opening files such as .gltf;
    // the old listener raced that transition and immediately replaced the
    // user's selected TestingGrounds/testing_grounds.gltf with story.ni.
    //
    // Startup restoration below is sufficient. After that, the user's selected
    // Project file owns the editor.

    // -------------------------------------------------------------------------
    // Startup
    // -------------------------------------------------------------------------

    // Restore the story editor and the real integrated terminals. Terminal
    // creation must be checked explicitly: an exception here previously left
    // Project/F1 without a terminal and suppressed the reason entirely.
    const initializeProjectWorkspace = async () => {
        if (terminalHostError) {
            // A failed authenticated Terminal probe is not an IF session.
            // Do not load story source or start a Z-Machine fallback.
            return;
        }
        if (terminalHost) {
            // Exactly two integrated surfaces on Linux/Windows, four on Mac.
            // Each real shell runs in the directory selected in CodeWorkspace;
            // Server Log streams the existing read-only HTTP/Python logfile.
            const current = terminalHost.platform === 'mac' ? 'Mac' :
                terminalHost.platform === 'win' ? 'Windows' : 'Linux';
            const startup = [['native', current], ['server', 'Server Log']];
            if (terminalHost.platform === 'mac') {
                startup.push(['linux', 'Linux'], ['win', 'Windows']);
            }
            try {
                for (const [backend, label] of startup) {
                    if (!(vscode.window.terminals || []).some(t => t.name === label)) {
                        openNativeTerminal(backend, label);
                    }
                }
                const primary = (vscode.window.terminals || []).find(t => t.name === current);
                primary?.show();
                await vscode.commands.executeCommand('workbench.action.terminal.focus');
                publishTerminalTabs();
            } catch (error) {
                console.error('[cinterforge] Code-OSS shell startup failed', error);
                vscode.window.showErrorMessage('Code-OSS terminals: ' + error.message);
            }
            return;
        }
        try {
            await ensureStoryEditor();
        } catch (error) {
            console.error(
                '[cinterforge] story editor initialization failed',
                error
            );
        }

        try {
            // A PTY alone is not enough: Code must open its panel before the
            // terminal can be displayed. Await the panel command rather than
            // racing it with a fixed 500 ms timeout.
            await vscode.commands.executeCommand(
                'workbench.action.terminal.focus'
            );

            const terminals = vscode.window.terminals || [];
            const hasZMachine = terminals.some(
                (terminal) => terminal.name === 'Z-Machine'
            );

            if (!hasZMachine) {
                openZMachineInterpreter();
            }

            if (globalThis.location?.protocol !== 'file:') {
                const hasNativeTerminal =
                    (vscode.window.terminals || []).some(
                        (terminal) => /^Terminal \d+$/.test(terminal.name)
                    );

                if (!hasNativeTerminal) {
                    openNativeTerminal();
                }
            }

            // Leave the interactive fiction terminal visible on Project/F1.
            const zMachine = (vscode.window.terminals || []).find(
                (terminal) => terminal.name === 'Z-Machine'
            );

            if (!zMachine) {
                throw new Error('Z-Machine terminal was not registered');
            }

            zMachine.show();
            publishTerminalTabs();

            console.info(
                '[cinterforge] Project terminals ready',
                (vscode.window.terminals || []).map(
                    (terminal) => terminal.name
                )
            );
        } catch (error) {
            console.error(
                '[cinterforge] Project terminal initialization failed',
                error
            );
        }
    };

    void initializeProjectWorkspace();

    return {
        setRuntimeBaseForTests,
        acceptanceCodeCommand,
        acceptanceSessionCommand,
        acceptanceSessionState,
        acceptanceFileSystemProvider:
            fileSystem
    };
}

async function activate(context) {
    console.info('[cinterforge-if] extension activation started');
    try {
        await loadClassicScripts(context);
        const coordinator = await activateCoordinator(context);
        console.info('[cinterforge-if] extension activation completed');
        return coordinator;
    } catch (error) {
        console.error('[cinterforge-if] extension activation failed', error);
        throw error;
    }
}

// -----------------------------------------------------------------------------
// Extension Deactivation
// -----------------------------------------------------------------------------

function deactivate() {
    // Disposable extension resources are owned by context.subscriptions.
}

exports.activate = activate;
exports.deactivate = deactivate;