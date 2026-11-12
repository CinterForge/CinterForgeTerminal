/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Owns the single control-request abstraction used by every extension subsystem.
    // Callers send endpoint, payload, and method without knowing whether the request
    // travels directly over HTTP or through the file:// workbench BroadcastChannel.
    // Keeping that decision here prevents terminals, build code, and acceptance tests
    // from acquiring environment-specific transport branches.

    const vscode = C.vscode;

    let runtimeBaseOverride = '';
    let terminalViaParent = false;
    let bridgeSerial = 0;

    // The outer Terminal page already has the authenticated HTTP origin. A
    // Code-OSS web extension worker may have a file:/blob: URL and cannot
    // resolve /host/... against itself. Reuse the existing same-origin channel
    // rather than assuming the worker's URL is the application's URL.
    function terminalParentRequest(type, path = '', body = {}, method = 'POST', timeout = 2500) {
        if (typeof BroadcastChannel !== 'function') {
            return Promise.reject(new Error('Code-OSS host channel unavailable'));
        }
        const channel = new BroadcastChannel('cinterforge-terminal-code-commands');
        const requestId = 'host-' + Date.now().toString(36) + '-' +
            (++bridgeSerial).toString(36) + '-' + Math.random().toString(36).slice(2);
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                channel.close();
                reject(new Error('Code-OSS host channel timed out'));
            }, timeout);
            channel.onmessage = event => {
                const reply = event?.data;
                if (reply?.type !== type + '-response' || reply.requestId !== requestId) return;
                clearTimeout(timer);
                channel.close();
                if (reply.ok === false) reject(new Error(String(reply.error || 'Host request failed')));
                else resolve(reply.payload || {});
            };
            channel.postMessage({type, requestId, path, body, method});
        });
    }


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
    // Runtime Control Transport
    // -----------------------------------------------------------------------------

    function runtimeBase() {
        if (runtimeBaseOverride) {
            return runtimeBaseOverride;
        }

        const configuredBase = String(
            vscode.workspace
                .getConfiguration('cinterforge')
                .get('controlBase') || ''
        ).replace(/\/$/, '');

        if (configuredBase) {
            return configuredBase;
        }

        if (
            globalThis.location &&
            globalThis.location.origin
        ) {
            return globalThis.location.origin;
        }

        throw new Error(
            'CinterForge IF control runtime is unavailable'
        );
    }

    async function request(
        path,
        body,
        method = 'POST'
    ) {
        // A file:// extension host has no meaningful location.origin. WebKit reports
        // "null". Route control requests through the workbench document, which is
        // same-package and can reach IFPlayer's owned authoring host.
        //
        // This is control transport only. Workspace files continue through the
        // bootstrap filesystem provider.
        const terminalEndpoint = path === '/code/command' ||
            path.startsWith('/host/code-shell-');
        // Prefer the verified Terminal host before the file: IFPlayer bridge.
        // WebKit may execute this worker at file: even in the HTTP Terminal.
        if (terminalEndpoint && terminalViaParent) {
            return terminalParentRequest('host-request', path, body || {}, method, 12000);
        }
        const shouldUseLocalBridge =
            !runtimeBaseOverride &&
            globalThis.location?.protocol === 'file:' &&
            typeof BroadcastChannel === 'function';

        if (shouldUseLocalBridge) {
            const channel = new BroadcastChannel(
                'cinterforge-ifplayer-control'
            );

            const requestId =
                `control-${Date.now()}-${Math.random().toString(36).slice(2)}`;

            try {
                return await new Promise(
                    (resolve, reject) => {
                        const timer = setTimeout(
                            () => {
                                channel.close();

                                reject(
                                    new Error(
                                        'Local CinterForge control bridge timed out'
                                    )
                                );
                            },
                            5000
                        );

                        channel.onmessage = (event) => {
                            const response = event?.data;

                            if (
                                response?.type !== 'response' ||
                                response.id !== requestId
                            ) {
                                return;
                            }

                            clearTimeout(timer);
                            channel.close();

                            if (response.ok === false) {
                                const error = new Error(
                                    response.error ||
                                    'Local CinterForge control request failed'
                                );

                                error.code = String(
                                    response.error_code || ''
                                );

                                reject(error);
                                return;
                            }

                            resolve(
                                response.payload || {}
                            );
                        };

                        channel.postMessage({
                            type: 'request',
                            id: requestId,
                            path: String(path || ''),
                            method,
                            body: body || {}
                        });
                    }
                );
            } catch (error) {
                try {
                    channel.close();
                } catch (_) {
                    // Channel may already be closed.
                }

                throw error;
            }
        }

        const requestOptions = {
            method,
            cache: 'no-store',
            headers: {
                'Content-Type': 'application/json'
            }
        };

        if (method !== 'GET') {
            requestOptions.body = JSON.stringify(
                body || {}
            );
        }

        // Terminal's workspace, shell and file-manager endpoints always belong
        // to the SAME origin that serves this Code-OSS workbench. A persisted
        // IFPlayer controlBase must not redirect native development commands.
        const base = terminalEndpoint && globalThis.location?.origin &&
            globalThis.location.origin !== 'null'
            ? globalThis.location.origin : runtimeBase();
        const response = await fetch(
            `${base}${path}`,
            {...requestOptions, credentials: 'same-origin'}
        );

        const payload = await response
            .json()
            .catch(() => {
                return {};
            });

        if (
            !response.ok ||
            payload.ok === false
        ) {
            const error = new Error(
                payload.error ||
                `HTTP ${response.status}`
            );

            error.code = String(
                payload.error_code || ''
            );

            throw error;
        }

        return payload;
    }

    // Identify the native Terminal host without relying on the IFPlayer control
    // transport. Code-OSS's extension worker may retain an IF controlBase from an
    // earlier workspace; that value must not redirect this same-origin probe.
    // A 404 is an ordinary IFPlayer host (which has no Terminal workspace API).
    // A failing Terminal endpoint is not evidence that this is an IF story: do
    // not silently boot the Z-Machine and issue a storm of unrelated requests.
    async function detectTerminalHost() {
        // Prefer direct same-origin HTTP when the worker has a real HTTP URL.
        // Do not interpret an invalid file:/blob: worker URL as IF mode; the
        // outer Terminal page can answer with its actual host capabilities.
        let directError = null;
        const location = globalThis.location;
        if (location?.protocol === 'http:' || location?.protocol === 'https:') {
            try {
                // A root-relative fetch from WebKit's extension-host Worker
                // throws "URL is not valid or contains user credentials" even
                // when the worker script was loaded over localhost HTTP. An
                // absolute same-origin URL is accepted by that Worker and does
                // not depend on an IFPlayer controlBase or a parent bridge.
                const endpoint = new URL('/host/code-workspace', location.href);
                const response = await fetch(endpoint.href, {
                    method: 'GET', credentials: 'same-origin', cache: 'no-store',
                    headers: {'Accept': 'application/json'}
                });
                if (response.ok) {
                    const payload = await response.json();
                    if (payload?.kind === 'terminal') {
                        C.terminalHostEnabled = true;
                        return payload;
                    }
                } else if (response.status !== 404) {
                    directError = new Error('Terminal workspace discovery failed (HTTP ' + response.status + ')');
                }
            } catch (error) {
                directError = error;
            }
        }
        try {
            const payload = await terminalParentRequest('host-probe', '', {}, 'GET', 1500);
            if (payload?.kind === 'terminal') {
                C.terminalHostEnabled = true;
                terminalViaParent = true;
                return payload;
            }
        } catch (error) {
            // A standalone IFPlayer has no parent Terminal answering this
            // channel. An actual HTTP service failure is still reported.
            if (directError) throw directError;
            if (location?.protocol === 'http:' || location?.protocol === 'https:') {
                // An HTTP Code-OSS page may be standalone IFPlayer, which
                // correctly has no Terminal workspace endpoint.
                return null;
            }
        }
        return null;
    }

    // Test-only transport override. It lives in this closure so tests change the
    // same runtimeBaseOverride read by runtimeBase() and request(). Production
    // callers never need to know or mutate the underlying transport state.
    function setRuntimeBaseForTests(value) {
        runtimeBaseOverride = String(
            value || ''
        ).replace(
            /\/$/,
            ''
        );
    }

    C.runtimeBase = runtimeBase;
    C.request = request;
    C.detectTerminalHost = detectTerminalHost;
    C.setRuntimeBaseForTests = setRuntimeBaseForTests;
})(globalThis.CinterForgeCode);
