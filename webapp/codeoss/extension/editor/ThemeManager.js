/*
 * CINTERFORGE SOURCE HEADER — webapp/codeoss/extension/editor/ThemeManager.js
 * PURPOSE: Provides the ThemeManager integration within the existing Code-OSS extension and authenticated Terminal host bridge.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   applyIFPlayerTheme: Handle apply ifplayer theme through this component's established interfaces.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
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
