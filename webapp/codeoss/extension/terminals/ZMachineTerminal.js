/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // PTY adapter for the interactive-fiction player session. It owns terminal input
    // editing, command submission, revision-based session polling, replay display, and
    // duplicate suppression. Browser duplicate physical key packets and server event
    // replay are separate failure modes and are intentionally de-duplicated separately.

    const vscode = C.vscode;
    const request = C.request;

    // -----------------------------------------------------------------------------
    // Z-Machine Terminal
    // -----------------------------------------------------------------------------

    class ZMachineTerminal {
        constructor() {
            this.writeEmitter =
                new vscode.EventEmitter();

            this.onDidWrite =
                this.writeEmitter.event;

            this.closeEmitter =
                new vscode.EventEmitter();

            this.onDidClose =
                this.closeEmitter.event;

            this.buffer = '';
            this.revision = 0;
            this.lastEventId = 0;
            this.sessionBaselineEstablished = false;

            this.timer = null;
            this.busy = false;

            this.pendingCR = false;
            this.escapeSequence = '';

            this.inputPacketSerial = 0;
            this.lastInputPacket = '';
            this.lastInputPacketAt = -Infinity;

            this.submittedCommands = [];
        }

        open() {
            this.writeEmitter.fire(
                'Z-Machine Interpreter\r\n> '
            );

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
                if (this.busy) {
                    await delay(25);
                    return;
                }

                this.busy = true;

                try {
                    const payload = await request(
                        `/host/player-session?after=${encodeURIComponent(
                            this.revision
                        )}&wait=25`,
                        null,
                        'GET'
                    );

                    const revision = Number(
                        payload.revision || 0
                    );

                    if (revision <= this.revision) {
                        return;
                    }

                    this.revision = revision;

                    const session =
                        payload.player_session || {};

                    const events =
                        Array.isArray(session.events)
                            ? session.events
                            : [];

                    // The host returns a complete session snapshot, not only
                    // events newer than `after`. On the first poll, establish
                    // the current event cursor instead of replaying historical
                    // commands into a newly opened terminal. In particular,
                    // an old movement command must not appear as fresh input
                    // when the user first presses Enter.
                    if (!this.sessionBaselineEstablished) {
                        this.sessionBaselineEstablished = true;
                        for (const event of events) {
                            this.lastEventId = Math.max(
                                this.lastEventId,
                                Number(event.id || 0)
                            );
                        }
                        return;
                    }

                    let wroteOutput = false;

                    for (const event of events) {
                        const eventId = Number(
                            event.id || 0
                        );

                        if (
                            eventId <= this.lastEventId
                        ) {
                            continue;
                        }

                        this.lastEventId = Math.max(
                            this.lastEventId,
                            eventId
                        );

                        const kind = String(
                            event.kind || ''
                        );

                        const text = String(
                            event.text ||
                            event.response ||
                            ''
                        );

                        if (!text) {
                            continue;
                        }

                        if (kind === 'command') {
                            const normalizedCommand =
                                text
                                    .replace(
                                        /\r?\n/g,
                                        ' '
                                    )
                                    .trim();

                            const isOwnCommand =
                                this.submittedCommands[0] ===
                                normalizedCommand;

                            if (isOwnCommand) {
                                this.submittedCommands.shift();
                            } else {
                                this.writeEmitter.fire(
                                    `> ${normalizedCommand}\r\n`
                                );
                            }
                        } else {
                            this.writeEmitter.fire(
                                `${text.replace(
                                    /\r?\n/g,
                                    '\r\n'
                                )}\r\n`
                            );
                        }

                        wroteOutput = true;
                    }

                    // Older or partial session snapshots may not carry events.
                    // Preserve a useful live terminal in that case without
                    // duplicating event-backed turns.
                    if (
                        events.length === 0 &&
                        wroteOutput === false
                    ) {
                        const command = String(
                            session.last_command || ''
                        ).trim();

                        const response = String(
                            session.last_response || ''
                        );

                        if (command) {
                            this.writeEmitter.fire(
                                `> ${command}\r\n`
                            );
                        }

                        if (response) {
                            this.writeEmitter.fire(
                                `${response.replace(
                                    /\r?\n/g,
                                    '\r\n'
                                )}\r\n`
                            );
                        }

                        wroteOutput = !!(
                            command ||
                            response
                        );
                    }

                    if (wroteOutput) {
                        this.writeEmitter.fire('> ');
                    }
                } catch (_) {
                    // Never spin if the host is unavailable or returns immediately.
                    if (this.timer !== null) {
                        await delay(250);
                    }
                } finally {
                    this.busy = false;
                }
            };

            const loop = async () => {
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
        }

        async submit() {
            const command =
                this.buffer.trim();

            this.buffer = '';

            this.writeEmitter.fire('\r\n');

            if (command) {
                this.submittedCommands.push(
                    command
                );
            }

            if (!command) {
                this.writeEmitter.fire('> ');
                return;
            }

            this.busy = true;

            try {
                await request(
                    '/host/code-terminal-command',
                    {
                        command
                    }
                );
            } catch (error) {
                this.writeEmitter.fire(
                    `ERROR: ${error.message}\r\n> `
                );
            } finally {
                this.busy = false;
            }
        }

        deletePreviousCharacter() {
            if (!this.buffer) {
                return;
            }

            // Keep the PTY's editable command buffer and the displayed line
            // in sync. The terminal does not use the shell's line editor.
            this.buffer = this.buffer.slice(0, -1);
            this.writeEmitter.fire('\b \b');
        }

        consumeInputPacket(packet) {
            for (const character of packet) {
                // macOS Forward Delete arrives from xterm as ESC [ 3 ~.
                // CSI sequences may be split across handleInput packets.
                if (this.escapeSequence) {
                    this.escapeSequence += character;

                    if (this.escapeSequence === '\x1b[') {
                        continue;
                    }

                    if (/^\x1b\[[0-9;]*$/.test(this.escapeSequence)) {
                        continue;
                    }

                    if (this.escapeSequence === '\x1b[3~') {
                        this.deletePreviousCharacter();
                    }

                    // Ignore other cursor/function-key escape sequences.
                    // Never append their bytes to the command buffer.
                    this.escapeSequence = '';
                    continue;
                }

                if (character === '\x1b') {
                    this.escapeSequence = character;
                    continue;
                }
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
                    this.deletePreviousCharacter();
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

        handleInput(data) {
            const packet = String(
                data || ''
            );

            const now =
                globalThis.performance &&
                    typeof globalThis.performance.now ===
                    'function'
                    ? globalThis.performance.now()
                    : Date.now();

            this.inputPacketSerial++;

            console.info(
                '[cinterforge-terminal-input]',
                {
                    serial:
                        this.inputPacketSerial,

                    packet,

                    codes: Array.from(
                        packet,
                        (character) => {
                            return character.codePointAt(
                                0
                            );
                        }
                    ),

                    at: now
                }
            );

            // Code-OSS Web is demonstrably delivering one physical key twice to this
            // single PTY instance: two consecutive handleInput callbacks with the
            // same one-character packet.
            //
            // Collapse only the duplicate callback pair at the PTY API boundary.
            // The 20 ms window is below normal key-repeat cadence and does not alter
            // command text after it has entered the terminal buffer.
            const duplicatePacket =
                packet === this.lastInputPacket &&
                now - this.lastInputPacketAt >= 0 &&
                now - this.lastInputPacketAt < 20;

            if (duplicatePacket) {
                this.lastInputPacket = '';
                this.lastInputPacketAt = -Infinity;
                return;
            }

            this.lastInputPacket = packet;
            this.lastInputPacketAt = now;

            this.consumeInputPacket(packet);
        }
    }

    C.ZMachineTerminal = ZMachineTerminal;
})(globalThis.CinterForgeCode);
