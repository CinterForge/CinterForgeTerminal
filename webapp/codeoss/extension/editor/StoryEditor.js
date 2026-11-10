/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Provides deterministic story-source discovery for startup restoration. The walk
    // uses the active cinterforge: filesystem, checks that the requested root exists,
    // then performs breadth-first traversal so nearby canonical story files win before
    // deeply nested fallbacks. Missing roots are normal and return null.
    const vscode = C.vscode;

    // Search one known project root for the first supported story source. Breadth-first
    // traversal makes the result deterministic by directory depth and avoids coupling
    // this helper to any one game's nested source layout.
    async function findStorySource(fileSystem, rootPath) {
        const root = vscode.Uri.parse(`cinterforge:${rootPath}`);
        try { await fileSystem.stat(root); } catch (_) { return null; }
        const queue = [root];
        while (queue.length > 0) {
            const directory = queue.shift();
            const entries = await fileSystem.readDirectory(directory);
            for (const [name, type] of entries) {
                const child = vscode.Uri.joinPath(directory, name);
                if (type === vscode.FileType.Directory) {
                    queue.push(child);
                    continue;
                }
                if (/\.(?:ni|zil|inf)$/i.test(name)) return child;
            }
        }
        return null;
    }

    C.findStorySource = findStorySource;
})(globalThis.CinterForgeCode);
