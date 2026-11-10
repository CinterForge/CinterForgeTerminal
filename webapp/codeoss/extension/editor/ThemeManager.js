/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Applies the host-owned IFPlayer light/dark state to Code-OSS. This helper only
    // changes Code's configured color theme; synchronization, startup handshakes, and
    // BroadcastChannel lifetime remain owned by activation so there is one authority.
    const vscode = C.vscode;

    // Translate IFPlayer's boolean theme state into Code-OSS's concrete built-in theme.
    // Avoid writing configuration when it already matches to prevent unnecessary
    // workbench theme churn and configuration events.
    async function applyIFPlayerTheme(dark) {
        const desiredTheme = dark ? 'Default Dark Modern' : 'Default Light Modern';
        const workbenchConfig = vscode.workspace.getConfiguration('workbench');
        if (String(workbenchConfig.get('colorTheme') || '') !== desiredTheme) {
            await workbenchConfig.update('colorTheme', desiredTheme, vscode.ConfigurationTarget.Global);
        }
    }

    C.applyIFPlayerTheme = applyIFPlayerTheme;
})(globalThis.CinterForgeCode);
