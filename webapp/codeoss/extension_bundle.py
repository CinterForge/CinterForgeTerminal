"""Build a single browser-worker entry point for the CinterForge Code extension.

VS Code web extension workers cannot fetch and evaluate separate source modules
through vscode.workspace.fs during activation. Keep the readable source split,
but publish one deterministic classic CommonJS entry point instead.
"""

import json
from pathlib import Path


SCRIPT_PATHS = (
    'runtime/ControlTransport.js',
    'runtime/BinaryEncoding.js',
    'filesystem/CinterForgeFileSystemProvider.js',
    'filesystem/WorkspaceSearch.js',
    'terminals/ZMachineTerminal.js',
    'terminals/JavaScriptConsoleTerminal.js',
    'terminals/NativeTerminal.js',
    'terminals/TerminalManager.js',
    'editor/StoryLanguage.js',
    'editor/StoryEditor.js',
    'editor/ThemeManager.js',
    'build/InformCompiler.js',
    'build/StoryBuilder.js',
    'ui/CodeUIBridge.js',
    'acceptance/AcceptanceAPI.js',
)


def build_extension_bundle(extension_directory: Path) -> Path:
    extension_directory = Path(extension_directory)
    entry = (extension_directory / 'extension.js').read_text(encoding='utf-8')
    begin = entry.index('const classicScriptPaths = [')
    end = entry.index('function activateCoordinator(context) {', begin)
    if entry.count('await loadClassicScripts(context);') != 1:
        raise RuntimeError('Code-OSS extension activation bootstrap changed')

    sections = []
    for relative_path in SCRIPT_PATHS:
        source_path = extension_directory / relative_path
        source = source_path.read_text(encoding='utf-8')
        sections.append(f'\n// --- {relative_path} ---\n{source}\n')

    bundle = (
        entry[:begin]
        + '\n'.join(sections)
        + '\n'
        + entry[end:]
    ).replace('await loadClassicScripts(context);', '// Subsystems are included in this worker entry point.')

    target = extension_directory / 'extension.bundle.js'
    target.write_text(bundle, encoding='utf-8')

    manifest_path = extension_directory / 'package.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['browser'] = './extension.bundle.js'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return target


