/*
 * CINTERFORGE SOURCE HEADER — webapp/codeoss/extension/ui/CodeUIBridge.js
 * PURPOSE: Provides the CodeUIBridge integration within the existing Code-OSS extension and authenticated Terminal host bridge.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   terminalTabState: Handle terminal tab state through this component's established interfaces.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
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
