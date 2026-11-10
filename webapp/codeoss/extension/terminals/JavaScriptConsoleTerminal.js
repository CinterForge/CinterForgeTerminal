/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Local-authoring PTY that evaluates JavaScript in the IFPlayer page through the
    // host console endpoint. It owns only terminal presentation and line editing; it
    // does not create a native shell resource and therefore has nothing remote to close.

    const vscode = C.vscode;
    const request = C.request;

    // -----------------------------------------------------------------------------
    // Local JavaScript Console Terminal
    // -----------------------------------------------------------------------------

    class JavaScriptConsoleTerminal {
        constructor() {
            this.writeEmitter =
                new vscode.EventEmitter();

            this.onDidWrite =
                this.writeEmitter.event;

            this.buffer = '';
            this.pendingCR = false;
        }

        open() {
            this.writeEmitter.fire(
                'CinterForge JavaScript Console\r\n' +
                'Evaluates in the local IFPlayer window.\r\n' +
                '> '
            );
        }

        close() {
            // No host resource is owned by the local JavaScript console.
        }

        async submit() {
            const command =
                this.buffer.trim();

            this.buffer = '';

            this.writeEmitter.fire('\r\n');

            if (!command) {
                this.writeEmitter.fire('> ');
                return;
            }

            try {
                const result = await request(
                    '/host/js-console-command',
                    {
                        command
                    }
                );

                const resultText = String(
                    result.text ?? 'undefined'
                ).replace(
                    /\r?\n/g,
                    '\r\n'
                );

                this.writeEmitter.fire(
                    `${resultText}\r\n> `
                );
            } catch (error) {
                this.writeEmitter.fire(
                    `ERROR: ${error.message}\r\n> `
                );
            }
        }

        handleInput(data) {
            const input = String(
                data || ''
            );

            for (const character of input) {
                if (character === '\r') {
                    this.pendingCR = true;
                    void this.submit();
                    continue;
                }

                if (character === '\n') {
                    if (this.pendingCR) {
                        this.pendingCR = false;
                        continue;
                    }

                    void this.submit();
                    continue;
                }

                this.pendingCR = false;

                if (
                    character === '\x7f' ||
                    character === '\b'
                ) {
                    if (this.buffer) {
                        this.buffer =
                            this.buffer.slice(
                                0,
                                -1
                            );

                        this.writeEmitter.fire(
                            '\b \b'
                        );
                    }

                    continue;
                }

                if (character >= ' ') {
                    this.buffer += character;

                    this.writeEmitter.fire(
                        character
                    );
                }
            }
        }
    }

    C.JavaScriptConsoleTerminal = JavaScriptConsoleTerminal;
})(globalThis.CinterForgeCode);
