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
