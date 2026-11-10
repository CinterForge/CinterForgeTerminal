/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Stable acceptance-test facade over the same production request transport used by
    // the extension. Tests intentionally exercise real code/player endpoints rather
    // than a parallel mock protocol, which keeps acceptance behavior representative.

    const request = C.request;

    // -----------------------------------------------------------------------------
    // Acceptance Test Surface
    // -----------------------------------------------------------------------------

    // Execute the same /code/command surface used by production authoring operations.
    // The body is passed through unchanged except that a missing body becomes {}.
    async function acceptanceCodeCommand(body) {
        return request(
            '/code/command',
            body || {}
        );
    }

    // Submit one textual player command through the production player endpoint. This
    // deliberately bypasses terminal keystroke editing while retaining server behavior.
    async function acceptanceSessionCommand(command) {
        return request(
            '/player/command',
            {
                command: String(
                    command || ''
                )
            }
        );
    }

    // Read the current player-session snapshot and normalize an absent session to {} so
    // acceptance callers can inspect state without duplicating response-envelope logic.
    async function acceptanceSessionState() {
        const payload = await request(
            '/player/session',
            {}
        );

        return (
            payload.player_session || {}
        );
    }


    C.acceptanceCodeCommand = acceptanceCodeCommand;
    C.acceptanceSessionCommand = acceptanceSessionCommand;
    C.acceptanceSessionState = acceptanceSessionState;
})(globalThis.CinterForgeCode);
