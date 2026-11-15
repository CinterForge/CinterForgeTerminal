/*
 * CINTERFORGE SOURCE HEADER — webapp/codeoss/extension/terminals/TerminalManager.js
 * PURPOSE: Provides the TerminalManager integration within the existing Code-OSS extension and authenticated Terminal host bridge.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   createNativeTerminalPty: Create create native terminal pty for this module.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Contains terminal-selection policy shared by activation and the Code UI bridge.
    // Local file:// authoring uses the IFPlayer JavaScript console; server authoring
    // uses a real host shell. Protocol-specific behavior remains inside each PTY class.

    // Select the PTY implementation from the authoring environment. file:// runs
    // inside the local IFPlayer page and therefore uses its JavaScript console; HTTP
    // authoring has a host capable of owning a native shell identified by `id`.
    function createNativeTerminalPty(id, backend = 'native') {
        // A Terminal-hosted Code worker may itself have a file: URL. The
        // verified host handshake, not worker URL spelling, chooses real PTYs.
        return C.terminalHostEnabled || globalThis.location?.protocol !== 'file:'
            ? new C.NativeTerminal(id, backend)
            : new C.JavaScriptConsoleTerminal();
    }

    C.createNativeTerminalPty = createNativeTerminalPty;
})(globalThis.CinterForgeCode);
