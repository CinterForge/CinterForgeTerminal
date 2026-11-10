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

        const response = await fetch(
            `${runtimeBase()}${path}`,
            requestOptions
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
    C.setRuntimeBaseForTests = setRuntimeBaseForTests;
})(globalThis.CinterForgeCode);
