/*
 * CINTERFORGE SOURCE HEADER — webapp/codeoss/extension/acceptance/AcceptanceAPI.js
 * PURPOSE: Provides the AcceptanceAPI integration within the existing Code-OSS extension and authenticated Terminal host bridge.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   acceptanceCodeCommand: Handle acceptance code command through this component's established interfaces.
 *   acceptanceSessionCommand: Handle acceptance session command through this component's established interfaces.
 *   acceptanceSessionState: Handle acceptance session state through this component's established interfaces.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
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
