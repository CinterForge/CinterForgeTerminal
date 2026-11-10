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
