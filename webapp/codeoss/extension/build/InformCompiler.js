/*
 * CINTERFORGE SOURCE HEADER — webapp/codeoss/extension/build/InformCompiler.js
 * PURPOSE: Provides the InformCompiler integration within the existing Code-OSS extension and authenticated Terminal host bridge.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   compileInform: Handle compile inform through this component's established interfaces.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Owns the direct Inform compile command. Dirty source is saved first so compiler
    // diagnostics describe the bytes visible to the author. Host diagnostics are then
    // converted into VS Code ranges/severities and published through the collection
    // supplied by activation.

    const vscode = C.vscode;
    const request = C.request;

    // -----------------------------------------------------------------------------
    // Inform Compilation
    // -----------------------------------------------------------------------------

    async function compileInform(diagnostics) {
        diagnostics.clear();

        const activeEditor =
            vscode.window.activeTextEditor;

        if (
            activeEditor &&
            activeEditor.document.isDirty
        ) {
            await activeEditor.document.save();
        }

        const activePath =
            activeEditor &&
                activeEditor.document.uri.scheme ===
                'cinterforge'
                ? activeEditor.document.uri.path
                : '';

        const result = await request(
            '/code/command',
            {
                operation: 'compile',
                path: activePath
            }
        );

        const groupedDiagnostics =
            new Map();

        for (
            const item of result.diagnostics || []
        ) {
            const uri = vscode.Uri.parse(
                `cinterforge:${item.path || activePath}`
            );

            const line = Math.max(
                0,
                Number(item.line || 1) - 1
            );

            const column = Math.max(
                0,
                Number(item.column || 1) - 1
            );

            const key = uri.toString();

            const row =
                groupedDiagnostics.get(key) || {
                    uri,
                    items: []
                };

            const diagnostic =
                new vscode.Diagnostic(
                    new vscode.Range(
                        line,
                        column,
                        line,
                        column + 1
                    ),
                    String(
                        item.message ||
                        'Inform compiler error'
                    ),
                    vscode.DiagnosticSeverity.Error
                );

            row.items.push(diagnostic);

            groupedDiagnostics.set(
                key,
                row
            );
        }

        for (
            const row of groupedDiagnostics.values()
        ) {
            diagnostics.set(
                row.uri,
                row.items
            );
        }

        return result;
    }

    C.compileInform = compileInform;
})(globalThis.CinterForgeCode);
