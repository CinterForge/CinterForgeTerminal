/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // PTY adapter for a native shell owned by the authoring host. Opening allocates a
    // host shell id, polling streams output into Code-OSS, writes forward user input,
    // and close releases the host resource. Retry delays keep empty/error polls from
    // turning into a busy loop.

    const vscode = C.vscode;
    const request = C.request;

    // -----------------------------------------------------------------------------
    // Native Terminal
    // -----------------------------------------------------------------------------

    class NativeTerminal {
        constructor(id, backend = 'native') {
            this.id = String(id || '');
            this.backend = backend;

            this.writeEmitter =
                new vscode.EventEmitter();

            this.onDidWrite =
                this.writeEmitter.event;

            this.closeEmitter =
                new vscode.EventEmitter();

            this.onDidClose =
                this.closeEmitter.event;

            this.timer = null;
            this.busy = false;
            this.opened = false;
            this.closed = false;
            this.pendingDimensions = null;
        }

        open() {
            if (this.closed || this.timer !== null) return;
            // Opening a Code terminal is the explicit creation boundary. Merely
            // launching IFPlayer never starts a native shell.
            const start = async () => {
                try {
                    await request(
                        '/host/code-shell-open',
                        {
                            id: this.id,
                            backend: this.backend
                        }
                    );
                    // close() may have run while the HTTP open was pending.
                    // Never leave that late-created PTY in the Host registry.
                    if (this.closed) {
                        await request('/host/code-shell-close', {id: this.id})
                            .catch(() => {});
                        return false;
                    }
                    this.opened = true;
                    // Code-OSS can resize an xterm before its asynchronous
                    // guest PTY exists. Send only the latest size after open.
                    if (this.pendingDimensions) {
                        const dimensions = this.pendingDimensions;
                        this.pendingDimensions = null;
                        await request('/host/code-shell-resize', {
                            id: this.id,
                            cols: dimensions.columns,
                            rows: dimensions.rows
                        }).catch(() => {});
                    }
                    return true;
                } catch (error) {
                    if (!this.closed) {
                        this.writeEmitter.fire(`ERROR: ${error.message}\r\n`);
                    }
                    return false;
                }
            };

            const delay = (milliseconds) => {
                return new Promise(
                    (resolve) => {
                        setTimeout(
                            resolve,
                            milliseconds
                        );
                    }
                );
            };

            const poll = async () => {
                if (
                    this.busy ||
                    this.timer === null
                ) {
                    return;
                }

                this.busy = true;

                try {
                    const payload = await request(
                        `/host/code-shell-output?id=${encodeURIComponent(
                            this.id
                        )}&wait=25`,
                        null,
                        'GET'
                    );

                    const rows =
                        payload.rows || [];

                    for (const row of rows) {
                        this.writeEmitter.fire(
                            String(
                                row.text || ''
                            )
                        );
                    }
                    if (payload.closed) {
                        // Host has released an exited process. Preserve the
                        // final transcript without polling or reserving a slot.
                        this.opened = false;
                        this.timer = null;
                        return;
                    }

                    // The host normally long-polls. Keep a small client-side floor
                    // as well so an empty immediate response can never create a
                    // request storm.
                    if (
                        rows.length === 0 &&
                        this.timer !== null
                    ) {
                        await delay(25);
                    }
                } catch (_) {
                    // A failed or closing host must never turn the terminal into a
                    // tight request loop. Normal production polling is a 25-second
                    // long poll; failures retry slowly enough to keep Code and
                    // IFPlayer responsive.
                    if (this.timer !== null) {
                        await delay(250);
                    }
                } finally {
                    this.busy = false;
                }
            };

            const loop = async () => {
                if (!await start()) {
                    // An unopened backend has no output endpoint; polling it
                    // forever created the repeated HTTP 400 Web Inspector flood.
                    this.timer = null;
                    return;
                }

                while (this.timer !== null) {
                    await poll();
                }
            };

            this.timer = {
                active: true
            };

            void loop();
        }

        close() {
            if (this.closed) return;
            this.closed = true;
            this.timer = null;
            // The pending open owns its own delayed close. Sending a close
            // before open completes is a no-op on the server and leaks the
            // session the open subsequently creates.
            if (!this.opened) return;
            this.opened = false;
            void request('/host/code-shell-close', {id: this.id}).catch(() => {
                // The host may already be gone while the terminal is closing.
            });
        }

        setDimensions(dimensions) {
            if (this.closed || this.backend === 'server') return;
            if (!this.opened) {
                this.pendingDimensions = dimensions;
                return;
            }
            void request('/host/code-shell-resize', {
                id: this.id,
                cols: dimensions.columns,
                rows: dimensions.rows
            }).catch(() => {});
        }

        handleInput(data) {
            const text = String(
                data || ''
            );

            if (!text || this.backend === 'server') {
                return;
            }

            void request(
                '/host/code-shell-write',
                {
                    id: this.id,
                    data: text
                }
            ).catch(
                (error) => {
                    this.writeEmitter.fire(
                        `ERROR: ${error.message}\r\n`
                    );
                }
            );
        }
    }

    C.NativeTerminal = NativeTerminal;
})(globalThis.CinterForgeCode);
