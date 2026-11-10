/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Defines the terminal-tab state sent to CinterForge's workbench UI. Only CinterForge
    // PTYs are exposed as peer tabs; unrelated Code terminals remain Code-owned. The
    // active terminal name is published separately so the host can mirror selection.
    const vscode = C.vscode;

    // Snapshot the CinterForge terminal tabs for the outer workbench UI. Filtering here
    // is intentional: Code may own other terminals that must not appear in IFPlayer's
    // peer-tab strip.
    function terminalTabState() {
        const names = (vscode.window.terminals || [])
            .map((terminal) => String(terminal.name || ''))
            .filter((name) => name === 'Z-Machine' || /^Terminal \d+$/.test(name));
        const active = String(vscode.window.activeTerminal?.name || '');
        return {type: 'terminal-tabs', names, active};
    }

    C.terminalTabState = terminalTabState;
})(globalThis.CinterForgeCode);
