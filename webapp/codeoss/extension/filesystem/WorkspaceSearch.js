/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Adds file and text search over the already-registered cinterforge: workspace
    // filesystem. Search deliberately walks through vscode.workspace.fs so it uses
    // the same authoritative provider as editors instead of opening a parallel host
    // transport. Traversal observes cancellation, excludes generated/noisy trees, and
    // avoids treating large or binary files as searchable text.

    const vscode = C.vscode;

    // -----------------------------------------------------------------------------
    // Workspace Search
    // -----------------------------------------------------------------------------

    function searchPatternMatch(
        value,
        pattern
    ) {
        const needle = String(
            pattern || ''
        )
            .trim()
            .toLowerCase();

        if (!needle) {
            return true;
        }

        return String(
            value || ''
        )
            .toLowerCase()
            .includes(needle);
    }

    async function walkWorkspace(
        fileSystem,
        uri,
        visit,
        token
    ) {
        if (token?.isCancellationRequested) {
            return;
        }

        let entries = [];

        try {
            entries = await fileSystem.readDirectory(
                uri
            );
        } catch (_) {
            return;
        }

        for (const [name, type] of entries) {
            if (token?.isCancellationRequested) {
                return;
            }

            if (
                name === 'node_modules' ||
                name === '.git' ||
                name === '.DS_Store'
            ) {
                continue;
            }

            const child = vscode.Uri.joinPath(
                uri,
                name
            );

            if (type === vscode.FileType.Directory) {
                await walkWorkspace(
                    fileSystem,
                    child,
                    visit,
                    token
                );

                continue;
            }

            if (type === vscode.FileType.File) {
                await visit(child);
            }
        }
    }

    function registerSearchProviders(
        context,
        fileSystem
    ) {
        if (
            typeof vscode.workspace.registerFileSearchProvider ===
            'function'
        ) {
            const fileSearchProvider =
                vscode.workspace.registerFileSearchProvider(
                    'cinterforge',
                    {
                        async provideFileSearchResults(
                            query,
                            options,
                            token
                        ) {
                            const results = [];
                            const root = vscode.Uri.parse(
                                'cinterforge:/'
                            );

                            const maxResults = Number(
                                options?.maxResults || 10000
                            );

                            await walkWorkspace(
                                fileSystem,
                                root,
                                async (uri) => {
                                    if (
                                        results.length >= maxResults
                                    ) {
                                        return;
                                    }

                                    if (
                                        searchPatternMatch(
                                            uri.path,
                                            query?.pattern
                                        )
                                    ) {
                                        results.push(uri);
                                    }
                                },
                                token
                            );

                            return results;
                        }
                    }
                );

            context.subscriptions.push(
                fileSearchProvider
            );
        }

        if (
            typeof vscode.workspace.registerTextSearchProvider !==
            'function'
        ) {
            return;
        }

        const textSearchProvider =
            vscode.workspace.registerTextSearchProvider(
                'cinterforge',
                {
                    async provideTextSearchResults(
                        query,
                        options,
                        progress,
                        token
                    ) {
                        let resultCount = 0;
                        let limitHit = false;

                        const maxResults = Math.max(
                            1,
                            Number(
                                options?.maxResults || 1000
                            )
                        );

                        const root = vscode.Uri.parse(
                            'cinterforge:/'
                        );

                        const source = String(
                            query?.pattern || ''
                        );

                        let regex = null;

                        if (query?.isRegExp) {
                            try {
                                regex = new RegExp(
                                    source,
                                    query?.isCaseSensitive
                                        ? 'g'
                                        : 'gi'
                                );
                            } catch (_) {
                                return {
                                    limitHit: false
                                };
                            }
                        }

                        await walkWorkspace(
                            fileSystem,
                            root,
                            async (uri) => {
                                if (
                                    resultCount >= maxResults
                                ) {
                                    limitHit = true;
                                    return;
                                }

                                let bytes;

                                try {
                                    bytes =
                                        await fileSystem.readFile(
                                            uri
                                        );
                                } catch (_) {
                                    return;
                                }

                                if (
                                    bytes.length >
                                    2 * 1024 * 1024 ||
                                    bytes.includes(0)
                                ) {
                                    return;
                                }

                                const text =
                                    decoder.decode(bytes);

                                const lines =
                                    text.split(/\r?\n/);

                                for (
                                    let lineIndex = 0;
                                    lineIndex < lines.length &&
                                    resultCount < maxResults;
                                    lineIndex++
                                ) {
                                    const lineText =
                                        lines[lineIndex];

                                    let start = -1;
                                    let length = 0;

                                    if (regex) {
                                        regex.lastIndex = 0;

                                        const match =
                                            regex.exec(lineText);

                                        if (match) {
                                            start =
                                                match.index;

                                            length = Math.max(
                                                1,
                                                match[0].length
                                            );
                                        }
                                    } else {
                                        const haystack =
                                            query?.isCaseSensitive
                                                ? lineText
                                                : lineText.toLowerCase();

                                        const needle =
                                            query?.isCaseSensitive
                                                ? source
                                                : source.toLowerCase();

                                        start =
                                            haystack.indexOf(
                                                needle
                                            );

                                        length =
                                            needle.length;
                                    }

                                    if (start < 0) {
                                        continue;
                                    }

                                    const matchLength =
                                        Math.max(
                                            1,
                                            length
                                        );

                                    progress.report({
                                        uri,

                                        ranges:
                                            new vscode.Range(
                                                lineIndex,
                                                start,
                                                lineIndex,
                                                start +
                                                matchLength
                                            ),

                                        preview: {
                                            text: lineText,

                                            matches:
                                                new vscode.Range(
                                                    0,
                                                    start,
                                                    0,
                                                    start +
                                                    matchLength
                                                )
                                        }
                                    });

                                    resultCount++;
                                }

                                if (
                                    resultCount >= maxResults
                                ) {
                                    limitHit = true;
                                }
                            },
                            token
                        );

                        return {
                            limitHit
                        };
                    }
                }
            );

        context.subscriptions.push(
            textSearchProvider
        );
    }

    C.searchPatternMatch = searchPatternMatch;
    C.walkWorkspace = walkWorkspace;
    C.registerSearchProviders = registerSearchProviders;
})(globalThis.CinterForgeCode);
