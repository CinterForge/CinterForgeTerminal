/*
 * CINTERFORGE SOURCE HEADER — webapp/codeoss/extension/editor/StoryLanguage.js
 * PURPOSE: Provides the StoryLanguage integration within the existing Code-OSS extension and authenticated Terminal host bridge.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   applyStoryLanguage: Handle apply story language through this component's established interfaces.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Enforces CinterForge language identity after Code-OSS opens virtual-workspace
    // documents. Code Web can expose a document before normal extension association
    // completes, so story and glTF files are corrected explicitly at this boundary.
    const vscode = C.vscode;

    // Assign the language implied by the portable source filename. No-op when the
    // document is already correct; failures are logged because language decoration
    // must never prevent the editor itself from opening.
    function applyStoryLanguage(document) {
        const path = String(document?.uri?.path || '').toLowerCase();
        let language = '';
        if (path.endsWith('.gltf')) language = 'json';
        else if (path.endsWith('.zil')) language = 'zil';
        else if (path.endsWith('.inf')) language = 'inform6';
        if (!language || document.languageId === language) return;
        void vscode.languages.setTextDocumentLanguage(document, language).catch((error) => {
            console.error(`[cinterforge] failed to assign ${language} language`, error);
        });
    }

    C.applyStoryLanguage = applyStoryLanguage;
})(globalThis.CinterForgeCode);
