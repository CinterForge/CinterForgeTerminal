/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Contains build-specific project selection policy shared with the full build flow.
    // A Frobozz-style project alias is derived only from /Project/<alias>/ paths; the
    // standard Source/Data/Story/Assets directories are structural roots, not aliases.

    // Return the composite-project alias containing the active source, or an empty
    // string for ordinary Project/{Source,Data,Story,Assets} workspaces. Empty means
    // the host should build the workspace's normal/default story.
    function projectAliasForEditor(activeEditor) {
        if (!activeEditor) return '';
        const activePath = String(activeEditor.document?.uri?.path || '').replace(/\\/g, '/');
        const projectMatch = activePath.match(/^\/Project\/([^/]+)\//);
        if (!projectMatch) return '';
        const candidate = decodeURIComponent(projectMatch[1]);
        return ['Source', 'Data', 'Story', 'Assets'].includes(candidate) ? '' : candidate;
    }

    C.projectAliasForEditor = projectAliasForEditor;
})(globalThis.CinterForgeCode);
