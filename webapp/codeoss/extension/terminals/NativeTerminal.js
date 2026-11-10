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
        constructor(id) {
            this.id = String(
                id || ''
            );

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
        }

        open() {
            // Opening a Code terminal is the explicit creation boundary. Merely
            // launching IFPlayer never starts a native shell.
            const start = async () => {
                try {
                    await request(
                        '/host/code-shell-open',
                        {
                            id: this.id
                        }
                    );
                } catch (error) {
                    this.writeEmitter.fire(
                        `ERROR: ${error.message}\r\n`
                    );
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
                await start();

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
            this.timer = null;

            void request(
                '/host/code-shell-close',
                {
                    id: this.id
                }
            ).catch(() => {
                // The host may already be gone while the terminal is closing.
            });
        }

        handleInput(data) {
            const text = String(
                data || ''
            );

            if (!text) {
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
