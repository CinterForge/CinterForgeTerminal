"""Build and stage the pinned Code - OSS browser workbench for CinterForge.

Only the maintainer ``--update-data`` producer path may acquire/build upstream
Code - OSS. Consumer machines receive ``Builds/InteractiveFiction/CodeOSS/Web`` through the shared
third-party-web-tool package and never require Git, Node, npm, or node_modules.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import html
import json
import os
from pathlib import Path
from .adapters import codeoss_root
import platform
import re
import shutil
import tarfile
import tempfile
import time
from typing import Any
from urllib.request import urlopen
import zipfile

from .adapters import run_process

CODEOSS_REPOSITORY = "https://github.com/microsoft/vscode.git"
# VS Code / Code - OSS 1.137.0, released 2026-09-08. Pin the actual release
# commit rather than tracking main so every maintainer produces the same tree.
CODEOSS_REF = "645f29cc3176500b4b5762ba887cf2a7f0ffdf2c"
CODEOSS_VERSION = "1.137.0"
CODEOSS_SCHEMA = "cinterforge.codeoss-web.v41"
CODEOSS_BUILD_CACHE_SCHEMA = "cinterforge.codeoss-build-cache.v9"
CODEOSS_TEST_WEB_VERSION = "0.0.81"


def _log(message: str) -> None:
    print(f"[codeoss] {message}", flush=True)


def _project_root() -> Path:
    # .../CinterForge/CinterForgeServer/cinter/ifserver/maintenance/install/codeoss_install.py
    return Path(__file__).resolve().parents[2]


def codeoss_build_root(repo_root: Path | None = None) -> Path:
    """Resolve Code-OSS from the product's authoritative storage root.

    Runtime callers must not infer the checkout from this module's location:
    embedded Python lives inside the application bundle. Maintainer build
    callers can still pass an explicit checkout root.
    """
    if repo_root is not None:
        return codeoss_root(Path(repo_root).expanduser().resolve())

    # Copied producer now resolves the shared sibling Builds root directly.
    return codeoss_root(_project_root())


def codeoss_web_root(repo_root: Path | None = None) -> Path:
    return codeoss_build_root(repo_root) / "Web"


def validate_codeoss_web(web: Path) -> None:
    """Validate the staged tree exactly as a static consumer sees it."""
    import re

    web = Path(web).resolve()
    workbench = web / "out" / "vs" / "code" / "browser" / "workbench" / "workbench.html"
    required = (
        workbench,
        workbench.with_name("workbench.js"),
        web / "out" / "nls.messages.js",
        web / "resources",
        web / "LICENSE.txt",
        web / "ThirdPartyNotices.txt",
        web / "product.json",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("Code - OSS static runtime is incomplete: " + ", ".join(missing))
    text = workbench.read_text(encoding="utf-8")
    # A DOM manifest alone cannot register an extension. The production scanner
    # must actually contain the CinterForge built-in discovery patch.
    scanner_output = web / "out" / "vs" / "workbench" / "services" / "extensionManagement" / "browser" / "builtinExtensionsScannerService.js"
    scanner_candidates = [scanner_output, web / "out" / "vs" / "code" / "browser" / "workbench" / "workbench.js"]
    scanner_compiled = any(
        candidate.is_file() and "vscode-workbench-builtin-extensions" in candidate.read_text(encoding="utf-8")
        for candidate in scanner_candidates
    )
    if not scanner_compiled:
        raise RuntimeError(
            "Code-OSS compiled built-in extension scanner lacks CinterForge discovery. "
            "Rebuild the CodeOSS Web product through WebApp Setup; an HTML manifest cannot activate the extension."
        )
    if "{{" in text:
        raise RuntimeError(f"Code - OSS static workbench has unresolved template substitutions: {workbench}")
    # The web workbench revives folderUri with URI.revive(), not URI.parse().
    # A string value such as "cinterforge:/" is interpreted as a scheme and
    # crashes the whole workbench with "Scheme contains illegal characters".
    match = re.search(r'<meta\s+id="vscode-workbench-web-configuration"\s+data-settings="([^"]+)"', text)
    if not match:
        raise RuntimeError(f"Code - OSS workspace configuration meta is missing: {workbench}")
    try:
        settings = json.loads(html.unescape(match.group(1)))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Code - OSS workspace configuration JSON is invalid: {workbench}") from exc
    if settings.get("folderUri") != {"scheme": "cinterforge", "path": "/"}:
        raise RuntimeError(
            "Code - OSS staged folderUri must use URI components, not the legacy "
            "cinterforge:/ string; reconstruct the staged CodeOSS Web product"
        )
    # The finished consumer artifact must be portable. Upstream's server template
    # resolves the Web base against window.location.origin; that collapses to the
    # IF server root under /codeoss and has no usable meaning for file://. Reject
    # that stale form so a source-side fix can never be masked by an older Web tree.
    if "window.location.origin" in text:
        raise RuntimeError(
            "Code - OSS static workbench still uses server-only window.location.origin bootstrap: "
            + str(workbench)
        )
    portable_bootstrap = "new URL(document.getElementById('vscode-workbench-web-base-url').getAttribute('data-settings'), window.location.href)"
    if portable_bootstrap not in text or "toString().replace(/\\/$/, '')" not in text:
        raise RuntimeError(f"Code - OSS static workbench is missing portable Web-root bootstrap: {workbench}")
    missing_refs: list[str] = []
    for value in re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", text, flags=re.IGNORECASE):
        value = value.strip()
        if not value or value.startswith(("data:", "http:", "https:", "//", "#")):
            continue
        clean = value.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/codeoss/"):
            candidate = (web / clean[len("/codeoss/"):]).resolve()
        elif clean.startswith("/"):
            missing_refs.append(value)
            continue
        else:
            candidate = (workbench.parent / clean).resolve()
        try:
            candidate.relative_to(web)
        except ValueError:
            missing_refs.append(value)
            continue
        if not candidate.is_file():
            missing_refs.append(value)
    if missing_refs:
        raise RuntimeError("Code - OSS static workbench references missing local resources: " + ", ".join(missing_refs[:20]))


def codeoss_is_installed(repo_root: Path | None = None) -> bool:
    try:
        validate_codeoss_web(codeoss_web_root(repo_root))
        return True
    except (OSError, RuntimeError):
        return False


def _run_checked(
    args: list[str], *, cwd: Path | None = None, timeout: int = 3600, env: dict[str, str] | None = None
) -> None:
    completed = run_process(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, env=env
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Code - OSS command failed ({' '.join(args)}): {detail[-4000:]}")


def _sync_checkout(source: Path, *, refresh: bool, ref: str) -> str:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Code - OSS maintainer setup requires git")
    if not (source / ".git").is_dir():
        if source.exists():
            shutil.rmtree(source)
        source.parent.mkdir(parents=True, exist_ok=True)
        _run_checked([git, "clone", "--filter=blob:none", "--no-checkout", CODEOSS_REPOSITORY, str(source)])
    if refresh:
        _run_checked([git, "fetch", "origin", ref, "--depth", "1"], cwd=source)
    _run_checked([git, "checkout", "--detach", ref], cwd=source)
    _run_checked([git, "reset", "--hard", ref], cwd=source)
    completed = run_process([git, "rev-parse", "HEAD"], cwd=str(source), capture_output=True, text=True, timeout=15)
    if completed.returncode != 0:
        raise RuntimeError("Code - OSS checkout has no readable git revision")
    commit = completed.stdout.strip()
    if commit != ref:
        raise RuntimeError(f"Code - OSS revision mismatch: expected {ref}, got {commit}")
    return commit


CINTERFORGE_PROVIDER_SOURCE = "// CINTERFORGE_BOOTSTRAP_FILESYSTEM_PROVIDER_V1\nclass CinterForgeBootstrapFileSystemProvider implements IFileSystemProvider {\n\treadonly capabilities = FileSystemProviderCapabilities.FileReadWrite;\n\treadonly onDidChangeCapabilities = Event.None;\n\tprivate readonly _onDidChangeFile = new Emitter<readonly IFileChange[]>();\n\treadonly onDidChangeFile = this._onDidChangeFile.event;\n\tprivate revision = '';\n\tprivate watchers = 0;\n\tprivate timer: Timeout | undefined;\n\t// Coalesce Code's duplicate startup probes before they reach /code/command.\n\tprivate readonly pendingReads = new Map<string, Promise<any>>();\n\tprivate readonly readCache = new Map<string, { expires: number; value: any }>();\n\n\tprivate async rawRequest(operation: string, resource?: URI, extra: Record<string, unknown> = {}): Promise<any> {\n\t\tif (globalThis.location?.protocol === 'file:') {\n\t\t\tconst id = `cinterforge-code-${Date.now()}-${Math.random().toString(36).slice(2)}`;\n\t\t\treturn await new Promise<any>((resolve, reject) => {\n\t\t\t\tconst onMessage = (event: MessageEvent) => {\n\t\t\t\t\tif (event.source !== globalThis.parent || event.data?.type !== 'cinterforge-code-response' || event.data?.id !== id) return;\n\t\t\t\t\tclearTimeout(timer); globalThis.removeEventListener('message', onMessage);\n\t\t\t\t\tif (event.data?.ok === false) { const code=String(event.data?.error_code||''); const message=String(event.data?.error||'Local workspace request failed'); if(code==='not_found') reject(createFileSystemProviderError(message,FileSystemProviderErrorCode.FileNotFound)); else if(code==='not_directory') reject(createFileSystemProviderError(message,FileSystemProviderErrorCode.FileNotADirectory)); else if(code==='permission_denied') reject(createFileSystemProviderError(message,FileSystemProviderErrorCode.NoPermissions)); else reject(createFileSystemProviderError(message,FileSystemProviderErrorCode.Unknown)); }\n\t\t\t\t\telse resolve(event.data?.payload || {});\n\t\t\t\t};\n\t\t\t\tconst timer = setTimeout(() => { globalThis.removeEventListener('message', onMessage); reject(new Error('Local CinterForge workspace bridge timed out')); }, 5000);\n\t\t\t\tglobalThis.addEventListener('message', onMessage);\n\t\t\t\tglobalThis.parent.postMessage({ type: 'cinterforge-code-request', id, body: { operation, path: resource?.path ?? '', ...extra } }, '*');\n\t\t\t});\n\t\t}\n\t\tconst controller = new AbortController();\n\t\tconst timeout = setTimeout(() => controller.abort(), 10000);\n\t\tlet response: Response;\n\t\ttry {\n\t\t\tresponse = await fetch('/code/command', { method: 'POST', cache: 'no-store', signal: controller.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operation, path: resource?.path ?? '', ...extra }) });\n\t\t} catch (error) {\n\t\t\tconsole.error('[cinterforge-code] workspace request failed', operation, resource?.path ?? '', error);\n\t\t\tthrow createFileSystemProviderError(`CinterForge workspace ${operation} failed: ${String(error)}`, FileSystemProviderErrorCode.Unavailable);\n\t\t} finally {\n\t\t\tclearTimeout(timeout);\n\t\t}\n\t\tconst payload = await response.json().catch(() => ({}));\n\t\tif (!response.ok || payload.ok === false) {\n\t\t\tconst code = String(payload.error_code || '');\n\t\t\tif (code === 'not_found') throw createFileSystemProviderError(String(payload.error || 'Not found'), FileSystemProviderErrorCode.FileNotFound);\n\t\t\tif (code === 'not_directory') throw createFileSystemProviderError(String(payload.error || 'Not a directory'), FileSystemProviderErrorCode.FileNotADirectory);\n\t\t\tif (code === 'permission_denied') throw createFileSystemProviderError(String(payload.error || 'Permission denied'), FileSystemProviderErrorCode.NoPermissions);\n\t\t\tthrow createFileSystemProviderError(String(payload.error || `HTTP ${response.status}`), FileSystemProviderErrorCode.Unknown);\n\t\t}\n\t\treturn payload;\n\t}\n\tprivate async request(operation: string, resource?: URI, extra: Record<string, unknown> = {}): Promise<any> {\n\t\tconst readOnly = operation === 'stat' || operation === 'read-directory' || operation === 'read-file' || operation === 'workspace-state';\n\t\tif (!readOnly) { this.readCache.clear(); return this.rawRequest(operation, resource, extra); }\n\t\tconst key = `${operation}|${resource?.toString(true) ?? ''}|${JSON.stringify(extra)}`;\n\t\tconst now = Date.now();\n\t\tconst cached = this.readCache.get(key);\n\t\tif (cached && cached.expires > now) return cached.value;\n\t\tconst pending = this.pendingReads.get(key);\n\t\tif (pending) return pending;\n\t\tconst promise = this.rawRequest(operation, resource, extra).then(value => {\n\t\t\tthis.readCache.set(key, { expires: Date.now() + 250, value });\n\t\t\treturn value;\n\t\t}).finally(() => this.pendingReads.delete(key));\n\t\tthis.pendingReads.set(key, promise);\n\t\treturn promise;\n\t}\n\tprivate decode(value: string): Uint8Array { const binary = atob(value || ''); const bytes = new Uint8Array(binary.length); for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i); return bytes; }\n\tprivate encode(bytes: Uint8Array): string { let binary = ''; for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); return btoa(binary); }\n\twatch(_resource: URI, _opts: IWatchOptions): IDisposable { this.watchers++; this.startPolling(); return toDisposable(() => { this.watchers = Math.max(0, this.watchers - 1); if (!this.watchers && this.timer) { clearInterval(this.timer); this.timer = undefined; } }); }\n\tprivate startPolling(): void { if (this.timer) return; const poll = async () => { try { const state = await this.request('workspace-state'); const next = String(state.revision || ''); if (this.revision && next && next !== this.revision) this._onDidChangeFile.fire([{ type: FileChangeType.UPDATED, resource: URI.parse('cinterforge:/') }]); this.revision = next; } catch { } }; void poll(); this.timer = setInterval(poll, 5000); }\n\tasync stat(resource: URI): Promise<IStat> { const p = await this.request('stat', resource); return { type: p.type === 'directory' ? FileType.Directory : FileType.File, ctime: 0, mtime: Number(p.mtime || 0), size: Number(p.size || 0) }; }\n\tasync readdir(resource: URI): Promise<[string, FileType][]> { const p = await this.request('read-directory', resource); return (p.entries || []).map((e: any) => [String(e.name), e.type === 'directory' ? FileType.Directory : FileType.File]); }\n\tasync readFile(resource: URI): Promise<Uint8Array> { const p = await this.request('read-file', resource); return this.decode(String(p.data || '')); }\n\tasync writeFile(resource: URI, content: Uint8Array, opts: IFileWriteOptions): Promise<void> { await this.request('write-file', resource, { data: this.encode(content), create: !!opts.create, overwrite: !!opts.overwrite }); this._onDidChangeFile.fire([{ type: FileChangeType.UPDATED, resource }]); }\n\tasync mkdir(resource: URI): Promise<void> { await this.request('create-directory', resource); this._onDidChangeFile.fire([{ type: FileChangeType.ADDED, resource }]); }\n\tasync delete(resource: URI, opts: IFileDeleteOptions): Promise<void> { await this.request('delete', resource, { recursive: !!opts.recursive }); this._onDidChangeFile.fire([{ type: FileChangeType.DELETED, resource }]); }\n\tasync rename(from: URI, to: URI, opts: IFileOverwriteOptions): Promise<void> { await this.request('rename', from, { target: to.path, overwrite: !!opts.overwrite }); this._onDidChangeFile.fire([{ type: FileChangeType.DELETED, resource: from }, { type: FileChangeType.ADDED, resource: to }]); }\n}\n\n"


CINTERFORGE_STATIC_FILE_PROVIDER_SOURCE = """// CINTERFORGE_STATIC_FILE_PROVIDER_V2
class CinterForgeStaticFileSystemProvider implements IFileSystemProvider {
\treadonly capabilities = 2 /* FileReadWrite */ | 2048 /* Readonly */;
\treadonly onDidChangeCapabilities = Event.None;
\treadonly onDidChangeFile = Event.None;
\twatch(_resource: URI, _opts: IWatchOptions): IDisposable { return toDisposable(() => {}); }
\tprivate async fetchFile(resource: URI): Promise<Uint8Array> {
\t\tconst response = await fetch(resource.toString(true), { cache: 'no-store' });
\t\tif (!response.ok && response.status !== 0) throw createFileSystemProviderError(`Unable to load ${resource.toString(true)}`, FileSystemProviderErrorCode.FileNotFound);
\t\treturn new Uint8Array(await response.arrayBuffer());
\t}
\tasync stat(resource: URI): Promise<IStat> { const bytes = await this.fetchFile(resource); return { type: FileType.File, ctime: 0, mtime: 0, size: bytes.byteLength }; }
\tasync readdir(_resource: URI): Promise<[string, FileType][]> { throw createFileSystemProviderError('Directory enumeration is not supported for packaged static files', FileSystemProviderErrorCode.Unavailable); }
\tasync readFile(resource: URI): Promise<Uint8Array> { return this.fetchFile(resource); }
\tasync writeFile(): Promise<void> { throw createFileSystemProviderError('Packaged Code - OSS files are read-only', FileSystemProviderErrorCode.NoPermissions); }
\tasync mkdir(): Promise<void> { throw createFileSystemProviderError('Packaged Code - OSS files are read-only', FileSystemProviderErrorCode.NoPermissions); }
\tasync delete(): Promise<void> { throw createFileSystemProviderError('Packaged Code - OSS files are read-only', FileSystemProviderErrorCode.NoPermissions); }
\tasync rename(): Promise<void> { throw createFileSystemProviderError('Packaged Code - OSS files are read-only', FileSystemProviderErrorCode.NoPermissions); }
}

"""

def _patch_cinterforge_bootstrap_provider(source: Path) -> None:
    """Register CinterForge's virtual filesystem before Code resolves workspace."""
    target = source / "src" / "vs" / "workbench" / "browser" / "web.main.ts"
    if not target.is_file():
        raise RuntimeError(f"Code - OSS browser bootstrap source is missing: {target}")
    text = target.read_text(encoding="utf-8")
    marker = "// CINTERFORGE_BOOTSTRAP_FILESYSTEM_PROVIDER_V1"
    if marker in text:
        return
    old_import = "import { IFileService } from '../../platform/files/common/files.js';"
    new_import = "import { createFileSystemProviderError, FileChangeType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, FileType, IFileChange, IFileDeleteOptions, IFileOverwriteOptions, IFileService, IFileSystemProvider, IFileWriteOptions, IStat, IWatchOptions } from '../../platform/files/common/files.js';"
    if old_import not in text:
        raise RuntimeError("Code - OSS FileService import changed; refusing CinterForge bootstrap patch")
    text = text.replace(old_import, new_import, 1)
    event_import = "import { Event } from '../../base/common/event.js';"
    if event_import not in text:
        raise RuntimeError("Code - OSS Event import changed; refusing CinterForge bootstrap patch")
    text = text.replace(event_import, "import { Emitter, Event } from '../../base/common/event.js';", 1)
    class_anchor = "export interface IBrowserMainWorkbench {"
    if class_anchor not in text:
        raise RuntimeError("Code - OSS BrowserMain anchor changed; refusing CinterForge bootstrap patch")
    text = text.replace(class_anchor, CINTERFORGE_PROVIDER_SOURCE + CINTERFORGE_STATIC_FILE_PROVIDER_SOURCE + class_anchor, 1)
    register_anchor = "\t\tserviceCollection.set(IFileService, fileService);"
    if register_anchor not in text:
        raise RuntimeError("Code - OSS FileService registration anchor changed; refusing CinterForge bootstrap patch")
    text = text.replace(register_anchor, register_anchor + "\n\t\t// CinterForge workspace provider must exist before workspace configuration is resolved.\n\t\tfileService.registerProvider('cinterforge', new CinterForgeBootstrapFileSystemProvider());\n\t\tif (globalThis.location?.protocol === 'file:') { fileService.registerProvider('file', new CinterForgeStaticFileSystemProvider()); }", 1)
    target.write_text(text, encoding="utf-8")
    _log("patched upstream browser bootstrap with early cinterforge: filesystem provider")


def _patch_builtin_extension_scanner(source: Path) -> None:
    """Allow packaged CinterForge built-ins to be declared by the static workbench.

    Upstream production Web builds inline their stock built-in extension manifests and
    ignore the DOM manifest used by development builds.  A file:// consumer cannot
    discover CinterForge through additionalBuiltinExtensions because that path first
    fetches package.json from a file: URI.  Append the static DOM declarations to the
    already-inlined stock list instead; HTTP and file:// then use the same system-
    extension scanner and no manifest fetch is required.
    """
    target = source / "src" / "vs" / "workbench" / "services" / "extensionManagement" / "browser" / "builtinExtensionsScannerService.ts"
    if not target.is_file():
        raise RuntimeError(f"Code - OSS built-in extension scanner source is missing: {target}")
    text = target.read_text(encoding="utf-8")
    marker = "// CINTERFORGE_STATIC_BUILTIN_EXTENSIONS_V1"
    if marker in text:
        return
    anchor = "\t\t\t\tthis.builtinExtensionsPromises = bundledExtensions.map(async e => {"
    if anchor not in text:
        raise RuntimeError(f"Code - OSS built-in extension scanner anchor changed upstream: {target}")
    patch = """\t\t\t\t// CINTERFORGE_STATIC_BUILTIN_EXTENSIONS_V1\n\t\t\t\t// Production Web normally ignores the DOM built-in list. CinterForge's\n\t\t\t\t// packaged extension is declared there so file:// never has to fetch a\n\t\t\t\t// package manifest before the extension host can start.\n\t\t\t\tconst cinterForgeBuiltinElement = mainWindow.document.getElementById('vscode-workbench-builtin-extensions');\n\t\t\t\tconst cinterForgeBuiltinSettings = cinterForgeBuiltinElement?.getAttribute('data-settings');\n\t\t\t\tif (cinterForgeBuiltinSettings) {\n\t\t\t\t\ttry { bundledExtensions.push(...JSON.parse(cinterForgeBuiltinSettings)); } catch { /* fail validation later if malformed */ }\n\t\t\t\t}\n\n"""
    target.write_text(text.replace(anchor, patch + anchor, 1), encoding="utf-8")
    _log("patched upstream built-in extension scanner for packaged static extensions")




def _patch_file_onboarding_editor_detection(source: Path) -> None:
    """Do not probe host /Applications paths from the standalone file:// workbench.

    Upstream onboarding detects other editors by asking the file service about common
    native install paths. In Safari file:// those harmless probes surface as red network
    failures. They provide no CinterForge functionality, so skip only this onboarding
    detection in standalone mode while leaving normal HTTP/Desktop Code-OSS unchanged.
    """
    target = source / "src" / "vs" / "workbench" / "contrib" / "welcomeOnboarding" / "browser" / "onboardingVariationA.ts"
    if not target.is_file():
        _log(f"onboarding editor-detection source absent; probe cleanup skipped: {target}")
        return
    text = target.read_text(encoding="utf-8")
    marker = "private async _detectInstalledEditors(): Promise<Set<string>> {\n\t\tconst detected = new Set<string>(['vscode']);"
    replacement = marker + "\n\t\tif (globalThis.location?.protocol === 'file:') return detected; // CinterForge standalone: no native-app probes"
    if replacement in text:
        return
    if marker not in text:
        raise RuntimeError("Code - OSS onboarding editor-detection patch point changed upstream")
    target.write_text(text.replace(marker, replacement, 1), encoding="utf-8")
    _log("patched upstream onboarding to skip native editor probes under file://")

def _patch_file_extension_module_loading(source: Path) -> None:
    """Load file:// web-extension entry modules through the main-thread FileService.

    Safari does not reliably allow the extension-host worker to fetch file: URLs,
    blob:file URLs owned by another realm, or large data: URLs.  Keep HTTP on the
    upstream path, but for a file: extension entry ask the main thread for the
    source text directly over the existing extension-host RPC channel.
    """
    main_target = source / "src" / "vs" / "workbench" / "api" / "browser" / "mainThreadExtensionService.ts"
    protocol_target = source / "src" / "vs" / "workbench" / "api" / "common" / "extHost.protocol.ts"
    worker_target = source / "src" / "vs" / "workbench" / "api" / "worker" / "extHostExtensionService.ts"
    for target in (main_target, protocol_target, worker_target):
        if not target.is_file():
            raise RuntimeError(f"Code - OSS extension loader source is missing: {target}")

    # Main thread: FileService is the browser-owned authority for file: reads.
    text = main_target.read_text(encoding="utf-8")
    marker = "// CINTERFORGE_FILE_EXTENSION_SOURCE_RPC_V2"
    if marker not in text:
        notification_import = "import { INotificationService } from '../../../platform/notification/common/notification.js';"
        if "import { IFileService } from '../../../platform/files/common/files.js';" not in text:
            if notification_import not in text:
                raise RuntimeError("Code - OSS notification import changed; refusing file extension source patch")
            text = text.replace(notification_import, "import { IFileService } from '../../../platform/files/common/files.js';\n" + notification_import, 1)
        ctor_anchor = "\t\t@INotificationService private readonly _notificationService: INotificationService,"
        if "@IFileService private readonly _fileService: IFileService," not in text:
            if ctor_anchor not in text:
                raise RuntimeError("Code - OSS MainThreadExtensionService constructor changed; refusing file extension source patch")
            text = text.replace(ctor_anchor, "\t\t@IFileService private readonly _fileService: IFileService,\n" + ctor_anchor, 1)
        method_anchor = "\tasync $asBrowserUri(uri: UriComponents): Promise<UriComponents> {"
        if method_anchor not in text:
            raise RuntimeError("Code - OSS $asBrowserUri implementation changed; refusing file extension source patch")
        read_method = """\t// CINTERFORGE_FILE_EXTENSION_SOURCE_RPC_V2\n\tasync $readExtensionFile(uri: UriComponents): Promise<string> {\n\t\tconst content = await this._fileService.readFile(URI.revive(uri));\n\t\treturn content.value.toString();\n\t}\n\n"""
        text = text.replace(method_anchor, read_method + method_anchor, 1)
        main_target.write_text(text, encoding="utf-8")

    # Protocol: expose only the narrow source-read operation needed by the worker.
    text = protocol_target.read_text(encoding="utf-8")
    if "$readExtensionFile(uri: UriComponents): Promise<string>;" not in text:
        anchor = "\t$asBrowserUri(uri: UriComponents): Promise<UriComponents>;"
        if anchor not in text:
            raise RuntimeError("Code - OSS MainThreadExtensionServiceShape changed; refusing file extension source patch")
        text = text.replace(anchor, "\t// CINTERFORGE_FILE_EXTENSION_SOURCE_RPC_V2\n\t$readExtensionFile(uri: UriComponents): Promise<string>;\n" + anchor, 1)
        protocol_target.write_text(text, encoding="utf-8")

    # Worker: bypass fetch entirely for file: extension entry modules.  HTTP and
    # every other scheme retain the exact upstream browser-URI/fetch path.
    text = worker_target.read_text(encoding="utf-8")
    if "// CINTERFORGE_FILE_EXTENSION_SOURCE_RPC_V2" not in text:
        old = """\t\t// First resolve the extension entry point URI to something we can load using `fetch`\n\t\t// This needs to be done on the main thread due to a potential `resourceUriProvider` (workbench api)\n\t\t// which is only available in the main thread\n\t\tconst browserUri = URI.revive(await this._mainThreadExtensionsProxy.$asBrowserUri(module));\n\t\tconst response = await fetch(browserUri.toString(true));\n\t\tif (extensionId) {\n\t\t\tperformance.mark(`code/extHost/didFetchExtensionCode/${extensionId}`);\n\t\t}\n\n\t\tif (response.status !== 200) {\n\t\t\tthrow new Error(response.statusText);\n\t\t}\n\n\t\t// fetch JS sources as text and create a new function around it\n\t\tconst source = await response.text();"""
        new = """\t\t// CINTERFORGE_FILE_EXTENSION_SOURCE_RPC_V2\n\t\tlet source: string;\n\t\tif (module.scheme === 'file') {\n\t\t\tsource = await this._mainThreadExtensionsProxy.$readExtensionFile(module);\n\t\t} else {\n\t\t\tconst browserUri = URI.revive(await this._mainThreadExtensionsProxy.$asBrowserUri(module));\n\t\t\tconst response = await fetch(browserUri.toString(true));\n\t\t\tif (response.status !== 200) {\n\t\t\t\tthrow new Error(response.statusText);\n\t\t\t}\n\t\t\tsource = await response.text();\n\t\t}\n\t\tif (extensionId) {\n\t\t\tperformance.mark(`code/extHost/didFetchExtensionCode/${extensionId}`);\n\t\t}"""
        if old not in text:
            raise RuntimeError("Code - OSS worker CommonJS loader changed; refusing file extension source patch")
        worker_target.write_text(text.replace(old, new, 1), encoding="utf-8")

    _log("patched file:// WebWorker extension entry loading through direct main-thread FileService RPC")


def _required_node_version(source: Path) -> str:
    nvmrc = source / ".nvmrc"
    if not nvmrc.is_file():
        raise RuntimeError(f"Code - OSS checkout is missing its pinned Node version: {nvmrc}")
    version = nvmrc.read_text(encoding="utf-8").strip().lstrip("v")
    if not version or any(part == "" for part in version.split(".")):
        raise RuntimeError(f"Code - OSS has an invalid .nvmrc: {version!r}")
    return version


def _node_dist_platform() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "darwin-arm64", "tar.gz", "bin/node"
    if system == "darwin" and machine in {"x86_64", "amd64"}:
        return "darwin-x64", "tar.gz", "bin/node"
    if system == "windows" and machine in {"amd64", "x86_64"}:
        return "win-x64", "zip", "node.exe"
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x64", "tar.gz", "bin/node"
    if system == "linux" and machine in {"arm64", "aarch64"}:
        return "linux-arm64", "tar.gz", "bin/node"
    raise RuntimeError(f"Code - OSS maintainer Node toolchain is unsupported on {system}/{machine}")


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _ensure_pinned_node(source: Path, root: Path) -> tuple[Path, Path, str]:
    """Return the exact Node/npm toolchain pinned by upstream Code - OSS.

    This is a maintainer-only build dependency stored under Builds/InteractiveFiction/CodeOSS; it is
    deliberately not part of the distributed Web runtime.
    """
    version = _required_node_version(source)
    dist_platform, archive_kind, node_rel = _node_dist_platform()
    dirname = f"node-v{version}-{dist_platform}"
    toolchains = root / "Toolchain"
    install = toolchains / dirname
    node = install / node_rel
    npm_cli = install / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if dist_platform.startswith("win-"):
        npm_cli = install / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if node.is_file() and npm_cli.is_file():
        return node, npm_cli, version

    suffix = "zip" if archive_kind == "zip" else "tar.gz"
    archive_name = f"{dirname}.{suffix}"
    base_url = f"https://nodejs.org/dist/v{version}"
    toolchains.mkdir(parents=True, exist_ok=True)
    archive = toolchains / archive_name
    sums = toolchains / f"SHASUMS256-v{version}.txt"
    _log(f"acquiring upstream-pinned Node.js v{version} for Code - OSS build ({dist_platform})")
    if not archive.is_file():
        _download(f"{base_url}/{archive_name}", archive)
    _download(f"{base_url}/SHASUMS256.txt", sums)
    expected = None
    for line in sums.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1].lstrip("*") == archive_name:
            expected = fields[0].lower()
            break
    if not expected:
        raise RuntimeError(f"Node.js checksum manifest has no entry for {archive_name}")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"Node.js archive checksum mismatch for {archive_name}")

    staging = Path(tempfile.mkdtemp(prefix=f".{dirname}-", dir=toolchains))
    try:
        if archive_kind == "zip":
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(staging)
        else:
            with tarfile.open(archive, "r:gz") as bundle:
                staging_resolved = staging.resolve()
                for member in bundle.getmembers():
                    target = (staging / member.name).resolve()
                    if target != staging_resolved and staging_resolved not in target.parents:
                        raise RuntimeError(f"Unsafe path in Node.js archive: {member.name}")
                bundle.extractall(staging)
        extracted = staging / dirname
        if not extracted.is_dir():
            raise RuntimeError(f"Node.js archive did not contain expected directory {dirname}")
        if install.exists():
            shutil.rmtree(install)
        extracted.replace(install)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if not node.is_file() or not npm_cli.is_file():
        raise RuntimeError(f"Pinned Node.js v{version} toolchain is incomplete after extraction")
    return node, npm_cli, version


def _ascii_escape_javascript_tree(root: Path) -> tuple[int, int]:
    """Make generated JavaScript source strictly ASCII without changing string data.

    Code - OSS's production minifier rejects any non-ASCII character in its
    generated JavaScript.  Escape every non-ASCII Unicode code point in the
    compiled REH-web JavaScript before minification.  JavaScript accepts Unicode
    escapes in strings, template literals, regular expressions, and identifiers;
    comments remain comments.  This is deliberately limited to generated JS and
    never rewrites the upstream checkout's authored source files.
    """
    if not root.is_dir():
        raise RuntimeError(f"Code - OSS generated web tree is missing before ASCII normalization: {root}")

    files_changed = 0
    chars_escaped = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".js", ".mjs", ".cjs"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Code - OSS generated JavaScript is not UTF-8: {path}") from exc
        if text.isascii():
            continue

        count = sum(1 for char in text if ord(char) > 0x7F)
        escaped_parts: list[str] = []
        for char in text:
            codepoint = ord(char)
            if codepoint <= 0x7F:
                escaped_parts.append(char)
            elif codepoint <= 0xFFFF:
                escaped_parts.append(f"\\u{codepoint:04x}")
            else:
                value = codepoint - 0x10000
                high = 0xD800 + (value >> 10)
                low = 0xDC00 + (value & 0x3FF)
                escaped_parts.append(f"\\u{high:04x}\\u{low:04x}")
        escaped = "".join(escaped_parts)
        if not escaped.isascii():
            raise RuntimeError(f"Code - OSS ASCII normalization failed: {path}")
        path.write_text(escaped, encoding="ascii", newline="")
        files_changed += 1
        chars_escaped += count

    # Fail loudly if a generated JavaScript file escaped the normalization pass.
    offenders: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".js", ".mjs", ".cjs"}:
            continue
        if any(byte > 0x7F for byte in path.read_bytes()):
            offenders.append(str(path))
            if len(offenders) >= 10:
                break
    if offenders:
        raise RuntimeError("Non-ASCII Code - OSS generated JavaScript remains: " + ", ".join(offenders))
    return files_changed, chars_escaped


def _force_esbuild_ascii_output(source: Path) -> None:
    """Make the pinned upstream minifier emit ASCII-only JS/CSS.

    VS Code's minify task validates its esbuild output after minification, so
    normalizing the pre-minified tree is insufficient: esbuild may re-emit
    printable Unicode characters.  Patch the pinned upstream build recipe at
    the exact minify call to request esbuild's documented ASCII charset.
    """
    optimize = source / "build" / "lib" / "optimize.ts"
    if not optimize.is_file():
        raise RuntimeError(f"Code - OSS checkout is missing minifier source: {optimize}")
    text = optimize.read_text(encoding="utf-8")
    marker = "\t\t\t\t\ttarget: [target],\n\t\t\t\t\twrite: false,"
    replacement = "\t\t\t\t\ttarget: [target],\n\t\t\t\t\tcharset: 'ascii',\n\t\t\t\t\twrite: false,"
    if replacement in text:
        _log("upstream esbuild minifier already configured for ASCII output")
        return
    matches = text.count(marker)
    if matches != 1:
        raise RuntimeError(
            f"Code - OSS minifier recipe changed; expected one ASCII patch point in {optimize}, found {matches}"
        )
    optimize.write_text(text.replace(marker, replacement, 1), encoding="utf-8", newline="")
    _log("configured upstream esbuild minifier charset=ascii")


def _audit_ascii_javascript_tree(root: Path, *, label: str) -> None:
    offenders: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".js", ".mjs", ".cjs"}:
            continue
        if any(byte > 0x7F for byte in path.read_bytes()):
            offenders.append(str(path))
            if len(offenders) >= 10:
                break
    if offenders:
        raise RuntimeError(f"Non-ASCII JavaScript remains in {label}: " + ", ".join(offenders))
    _log(f"{label} JavaScript ASCII audit passed")


def _build_web(source: Path) -> None:
    if not (source / "package.json").is_file() or not (source / "package-lock.json").is_file():
        raise RuntimeError(f"Code - OSS checkout is incomplete: {source}")
    root = source.parent
    node, npm_cli, version = _ensure_pinned_node(source, root)
    env = dict(os.environ)
    path_parts = [str(node.parent)]
    # node-gyp's Unix make generator invokes `libtool -static` for static
    # archives. On macOS that is Apple's /usr/bin/libtool syntax. A Homebrew
    # GNU libtool earlier on PATH rejects `-static` and breaks @vscode/sqlite3
    # during npm ci. Make the native macOS build tools authoritative while
    # retaining the caller PATH for all other dependencies.
    if platform.system().lower() == "darwin":
        path_parts.extend(["/usr/bin", "/bin"])
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    _log(f"using upstream-pinned Node.js v{version}: {node}")
    npm = [str(node), str(npm_cli)]
    # npm ci is only reproducible when it starts without an install tree. Git
    # reset cannot remove ignored node_modules directories, including package-
    # local installs under extensions/. A previously interrupted/update-data
    # build can therefore make npm's own cleanup fail with ENOTEMPTY before it
    # installs anything. Remove install trees explicitly and fail loudly if any
    # survive; source/build products outside node_modules remain available for
    # the existing compiled-product cache.
    install_trees = sorted(
        (path for path in source.rglob("node_modules") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for install_tree in install_trees:
        _rmtree_generated_strict(install_tree)
    survivors = [path for path in source.rglob("node_modules") if path.exists()]
    if survivors:
        raise RuntimeError(
            "Code - OSS npm install cleanup failed; node_modules remain: "
            + ", ".join(str(path) for path in survivors[:20])
        )
    if install_trees:
        _log(f"removed {len(install_trees)} stale node_modules tree(s) before npm ci")
    _run_checked([*npm, "ci"], cwd=source, timeout=3600, env=env)
    # The build-next server-web bundle resolves codicon.css from the source tree,
    # but a clean checkout does not carry the generated font beside that CSS.
    # Upstream VS Code build workflows seed it from the pinned @vscode/codicons
    # package after npm install. Do the same deterministically before bundling.
    codicon_source = source / "node_modules" / "@vscode" / "codicons" / "dist" / "codicon.ttf"
    codicon_target = source / "src" / "vs" / "base" / "browser" / "ui" / "codicons" / "codicon" / "codicon.ttf"
    if not codicon_source.is_file():
        raise RuntimeError(f"Code - OSS npm install is missing @vscode/codicons font: {codicon_source}")
    codicon_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(codicon_source, codicon_target)
    _log(f"seeded upstream codicon font for server-web bundle: {codicon_target}")
    # Build the actual server-web bundle. compile-build-without-mangling only
    # leaves the REH HTML/CSS shell in recent Code - OSS and does not emit the
    # browser workbench entrypoint. Upstream's server-web target owns the full
    # browser artifact, including workbench.js, workbench.web.main.internal.js,
    # NLS, workers, and other resources needed by a static consumer.
    next_builder = source / "build" / "next" / "index.ts"
    if not next_builder.is_file():
        raise RuntimeError(f"Pinned Code - OSS checkout is missing server-web builder: {next_builder}")
    _run_checked(
        [str(node), str(next_builder), "bundle", "--nls", "--target", "server-web", "--out", "out-vscode-reh-web"],
        cwd=source, timeout=3600, env=env,
    )
    # The REH workbench build does not compile the browser entrypoints of the
    # built-in extensions. They are part of the web runtime too (Markdown
    # preview, merge-conflict, language features, etc.), so build them before
    # staging rather than serving raw extension sources.
    _run_checked([*npm, "run", "gulp", "compile-extensions-build"], cwd=source, timeout=3600, env=env)
    generated = source / "out-vscode-reh-web"
    files_changed, chars_escaped = _ascii_escape_javascript_tree(generated)
    _log(
        f"generated JavaScript ASCII audit complete files_changed={files_changed} "
        f"characters_escaped={chars_escaped}"
    )
    output = generated
    _audit_ascii_javascript_tree(output, label="compiled Code - OSS")
    workbench = output / "vs" / "code" / "browser" / "workbench" / "workbench.html"
    workbench_js = output / "vs" / "code" / "browser" / "workbench" / "workbench.js"
    nls = output / "nls.messages.js"
    missing = [str(path) for path in (workbench, workbench_js, nls) if not path.is_file()]
    if missing:
        raise RuntimeError("Code - OSS server-web build is incomplete: " + ", ".join(missing))



CODEOSS_NONPRODUCTION_EXTENSIONS = frozenset({"vscode-test-resolver"})
CODEOSS_REQUIRED_BROWSER_EXTENSIONS = ("github-authentication", "emmet", "merge-conflict")


def _missing_packaged_browser_extensions(source: Path) -> list[tuple[str, str]]:
    """Return production browser entrypoints missing from upstream's packaged .build tree."""
    authored = source / "extensions"
    packaged = source / ".build" / "extensions"
    if not authored.is_dir():
        raise RuntimeError(f"Code - OSS authored extension tree is missing: {authored}")
    missing: list[tuple[str, str]] = []
    for manifest in sorted(authored.glob("*/package.json")):
        name = manifest.parent.name
        if name in CODEOSS_NONPRODUCTION_EXTENSIONS:
            continue
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid Code - OSS extension manifest: {manifest}") from exc
        browser = package.get("browser") if isinstance(package, dict) else None
        if not isinstance(browser, str) or not browser.strip():
            continue
        rel = browser.strip()
        target = packaged / name / rel
        if target.is_file() or (not target.suffix and target.with_suffix(".js").is_file()):
            continue
        missing.append((name, rel))
    return missing


def _ensure_browser_extension_products(source: Path) -> int:
    """Ensure upstream's packaged built-in web extensions are complete.

    The authoritative production extension output is ``.build/extensions``.  VS Code's
    own ``compile-extensions-build`` task packages all local extensions and selects the
    correct build mechanism for each extension (gulp/webpack/esbuild/etc.).  Individual
    package scripts are intentionally not inferred here because the built-ins do not use
    one uniform script convention.
    """
    missing = _missing_packaged_browser_extensions(source)
    if not missing:
        _log("packaged built-in browser extensions already complete; skipping extension packaging")
        return 0

    node, npm_cli, version = _ensure_pinned_node(source, source.parent)
    env = dict(os.environ)
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    npm = [str(node), str(npm_cli)]
    _log(
        f"packaged browser extensions incomplete count={len(missing)}; "
        f"running upstream compile-web + compile-extensions-build with Node.js v{version}"
    )
    # VS Code's extension pipeline has two distinct stages. compile-web runs every
    # extensions/**/esbuild.browser.mts and materializes the declared browser
    # entrypoints (dist/browser, client/dist/browser, etc.) in the authored tree.
    # compile-extensions-build then packages those products into .build/extensions.
    # Running only the packaging task leaves browser-only esbuild products absent.
    _run_checked([*npm, "run", "gulp", "compile-web"], cwd=source, timeout=3600, env=env)
    _run_checked([*npm, "run", "gulp", "compile-extensions-build"], cwd=source, timeout=3600, env=env)

    remaining = _missing_packaged_browser_extensions(source)
    if remaining:
        # The aggregate upstream compile-web task has changed coverage across VS Code
        # releases.  Never publish a partial browser extension set: ask each still-
        # missing extension to run the compile-web script declared by its own pinned
        # package.json, then re-run the authoritative packaging task.
        repaired_individually: list[str] = []
        for name, rel in remaining:
            manifest_path = source / "extensions" / name / "package.json"
            package = json.loads(manifest_path.read_text(encoding="utf-8"))
            scripts = package.get("scripts") if isinstance(package, dict) else None
            if not isinstance(scripts, dict) or not isinstance(scripts.get("compile-web"), str):
                continue
            _log(f"building missing browser extension explicitly name={name} entrypoint={rel}")
            _run_checked([*npm, "run", "compile-web"], cwd=manifest_path.parent, timeout=3600, env=env)
            repaired_individually.append(name)
        if repaired_individually:
            _run_checked([*npm, "run", "gulp", "compile-extensions-build"], cwd=source, timeout=3600, env=env)
        remaining = _missing_packaged_browser_extensions(source)
    if remaining:
        detail = ", ".join(f"{name}:{rel}" for name, rel in remaining[:20])
        raise RuntimeError(
            "Code - OSS packaged browser extension entrypoints remain missing after "
            f"upstream and explicit compile-web + compile-extensions-build: {detail}"
        )
    _log(f"upstream packaged Code - OSS browser extensions repaired count={len(missing)}")
    return len(missing)

def _compiled_web_products_ready(source: Path) -> bool:
    """Return whether the expensive upstream compile products are reusable."""
    return (
        (source / "out-vscode-reh-web" / "vs" / "code" / "browser" / "workbench" / "workbench.html").is_file()
        and (source / "out-vscode-reh-web" / "vs" / "code" / "browser" / "workbench" / "workbench.js").is_file()
        and (source / "out-vscode-reh-web" / "nls.messages.js").is_file()
        and (source / ".build" / "extensions").is_dir()
    )


def _build_cache_path(root: Path) -> Path:
    return root / "compiled-products.json"


CODEOSS_PATCHED_COMPILE_INPUTS = (
    "src/vs/workbench/browser/web.main.ts",
    "src/vs/workbench/services/extensionManagement/browser/builtinExtensionsScannerService.ts",
    "src/vs/workbench/contrib/welcomeOnboarding/browser/onboardingVariationA.ts",
    "src/vs/workbench/api/browser/mainThreadExtensionService.ts",
    "src/vs/workbench/api/common/extHost.protocol.ts",
    "src/vs/workbench/api/worker/extHostExtensionService.ts",
)


def _patched_bootstrap_digest(source: Path) -> str:
    """Fingerprint every CinterForge-patched source that feeds the Web compile.

    The compiled-product cache must never be reusable merely because web.main.ts
    is unchanged.  In particular, builtinExtensionsScannerService.ts controls
    whether the stock bundled extensions (including github-authentication) enter
    the production extension registry.
    """
    digest = hashlib.sha256()
    for rel in CODEOSS_PATCHED_COMPILE_INPUTS:
        path = source / rel
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if not path.is_file():
            digest.update(b"<missing>")
        else:
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _build_cache_matches(root: Path, source: Path, commit: str) -> bool:
    marker = _build_cache_path(root)
    if not marker.is_file() or not _compiled_web_products_ready(source):
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema") == CODEOSS_BUILD_CACHE_SCHEMA
        and value.get("commit") == commit
        and value.get("bootstrap_sha256") == _patched_bootstrap_digest(source)
    )


def _record_build_cache(root: Path, source: Path, commit: str) -> None:
    marker = _build_cache_path(root)
    marker.write_text(
        json.dumps(
            {
                "schema": CODEOSS_BUILD_CACHE_SCHEMA,
                "commit": commit,
                "bootstrap_sha256": _patched_bootstrap_digest(source),
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def _strip_dangling_source_map_references(root: Path) -> int:
    """Remove sourceMappingURL comments only when the referenced map is absent.

    Source maps are developer metadata, not browser runtime content.  The packaged
    consumer tree must either carry a referenced map or omit the reference; leaving
    dangling references makes Safari/Chromium report false runtime failures.
    """
    import re

    root = Path(root).resolve()
    js_pattern = re.compile(r"(?m)^[ \t]*//[#@] sourceMappingURL=([^\r\n]+)[ \t]*$")
    css_pattern = re.compile(r"/\*[#@] sourceMappingURL=([^*]+?)\*/")
    changed = 0
    for script in list(root.rglob("*.js")) + list(root.rglob("*.css")):
        try:
            text = script.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        remove = False
        pattern = css_pattern if script.suffix.lower()==".css" else js_pattern
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            if value.startswith("data:"):
                continue
            clean = value.split("?", 1)[0].split("#", 1)[0]
            target = (script.parent / clean).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                remove = True
                continue
            if not target.is_file():
                remove = True
        if not remove:
            continue
        rewritten = pattern.sub(
            lambda match: match.group(0)
            if match.group(1).strip().startswith("data:")
            or (script.parent / match.group(1).strip().split("?", 1)[0].split("#", 1)[0]).is_file()
            else "",
            text,
        )
        if rewritten != text:
            script.write_text(rewritten, encoding="utf-8")
            changed += 1
    return changed


def _ensure_extension_nls_files(web: Path) -> int:
    """Materialize optional extension localization catalogs as empty JSON.

    Code - OSS probes package.nls.json for registered extensions even when an
    extension has no localized package strings. A missing catalog is harmless
    upstream, but a static web host turns every probe into a browser-visible
    404. Keep the static runtime complete by supplying an empty catalog only
    where the extension itself did not provide one.
    """
    extensions = web / "extensions"
    if not extensions.is_dir():
        return 0
    created = 0
    for manifest in sorted(extensions.glob("*/package.json")):
        catalog = manifest.parent / "package.nls.json"
        if catalog.exists():
            continue
        catalog.write_text("{}\n", encoding="utf-8")
        created += 1
    return created


def _stage_cinterforge_extension(web: Path) -> None:
    """Refresh the CinterForge-owned extension without rebuilding upstream Code - OSS."""
    # The actual authored extension and its bundler are copied with this
    # established producer, so a clean Terminal checkout is self-contained.
    extension_source = Path(__file__).resolve().parent / "extension"
    extension_target = web / "extensions" / "cinterforge-if"
    if not (extension_source / "package.json").is_file() or not (extension_source / "extension.js").is_file():
        raise RuntimeError(f"CinterForge Code - OSS extension is incomplete: {extension_source}")
    if extension_target.exists():
        shutil.rmtree(extension_target)
    extension_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(extension_source, extension_target)
    # Publish one self-contained browser-worker entry. The worker cannot use
    # vscode.workspace.fs + new Function to load its own split source at startup.
    from .extension_bundle import build_extension_bundle
    build_extension_bundle(extension_target)


def _referenced_root_node_modules(*roots: Path) -> set[str]:
    """Return root npm package names referenced by staged browser JavaScript."""
    import re
    pattern = re.compile(r"(?:^|[/\\])node_modules[/\\]((?:@[A-Za-z0-9._~-]+[/\\])?[A-Za-z0-9._~-]+)(?=[/\\])")
    packages: set[str] = set()
    text_suffixes = {".js", ".mjs", ".cjs", ".json", ".html", ".css"}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in text_suffixes:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in pattern.finditer(text):
                packages.add(match.group(1).replace("\\", "/"))
    return packages

def _runtime_copy_ignore(directory: str, names: list[str]) -> set[str]:
    """Exclude development-only material while copying browser runtime packages."""
    ignored: set[str] = set()
    for name in names:
        lower = name.casefold()
        if lower in {".bin", ".git", ".github", "test", "tests", "__tests__", "coverage", "src"}:
            ignored.add(name)
        elif lower in {".ds_store"} or lower.endswith((".map", ".tsbuildinfo")):
            ignored.add(name)
    return ignored


def _is_cinterforge_runtime_extension(name: str, manifest: dict) -> bool:
    """Return whether a stock extension belongs in CinterForge's browser editor product.

    Declarative language/theme/snippet extensions are runtime data and remain.  For
    executable stock web extensions, CinterForge ships the programming-language
    feature set rather than unrelated VS Code product surfaces (chat/auth/remote/
    notebook renderers, etc.).  CinterForge's own extension is always retained.
    The decision comes from the pinned extension manifest, not a package-name list.
    """
    if name == "cinterforge-if" or name in CODEOSS_REQUIRED_BROWSER_EXTENSIONS:
        return True
    if not _is_web_extension_manifest(manifest):
        return False
    browser = str(manifest.get("browser") or "").strip()
    main = str(manifest.get("main") or "").strip()
    if not browser and not main:
        return True
    categories = manifest.get("categories")
    if isinstance(categories, str):
        categories = [categories]
    normalized = {str(value).strip().casefold() for value in (categories or [])}
    if normalized & {"programming languages", "formatters", "linters"}:
        return True
    contributes = manifest.get("contributes")
    if isinstance(contributes, dict) and any(contributes.get(key) for key in ("languages", "grammars")):
        return True
    return False


def _rmtree_generated_strict(path: Path, *, attempts: int = 20, delay: float = 0.1) -> None:
    """Remove a generated tree, retrying only the macOS ENOTEMPTY race.

    Code - OSS staging is disposable generated output, but macOS can report
    errno 66 while directory entries are still settling.  Retry that one
    condition only; every other filesystem error remains fatal.
    """
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if getattr(exc, "errno", None) != errno.ENOTEMPTY or attempt + 1 >= attempts:
                raise
            time.sleep(delay)


def _prune_static_web_runtime(staging: Path) -> dict[str, int]:
    """Remove build/debug/desktop material from the finished static browser product.

    This runs only on the disposable Web staging tree.  The upstream checkout and
    compiled maintainer products remain untouched.
    """
    removed_maps = 0
    removed_misc = 0
    for path in sorted(staging.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_file():
            continue
        lower = path.name.casefold()
        if lower.endswith(".map"):
            path.unlink()
            removed_maps += 1
        elif lower == ".ds_store":
            path.unlink()
            removed_misc += 1

    # A root .bin directory is npm's executable shim farm.  It is build tooling,
    # never a browser resource (and can contain complete native executables).
    root_bin = staging / "node_modules" / ".bin"
    if root_bin.exists():
        _rmtree_generated_strict(root_bin)
        removed_misc += 1

    # The authored extension tree can contain its own install root. Runtime
    # dependencies belong inside packaged extension outputs, not this workspace-
    # level node_modules directory.
    extension_modules = staging / "extensions" / "node_modules"
    if extension_modules.exists():
        _rmtree_generated_strict(extension_modules)
        removed_misc += 1

    # Keep only extensions that Code - OSS can actually load in a browser host.
    removed_extensions = 0
    extensions = staging / "extensions"
    if extensions.is_dir():
        for child in list(extensions.iterdir()):
            if not child.is_dir() or child.name == "cinterforge-if":
                continue
            manifest_path = child / "package.json"
            if not manifest_path.is_file():
                _rmtree_generated_strict(child)
                removed_extensions += 1
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"Invalid Code - OSS extension manifest while pruning runtime: {manifest_path}") from exc
            if not _is_cinterforge_runtime_extension(child.name, manifest):
                _rmtree_generated_strict(child)
                removed_extensions += 1
                continue

            # Authored/build metadata is not consumed by the packaged extension.
            # Compiled browser entrypoints, contribution assets, grammars, themes,
            # snippets, WASM and localization remain untouched.
            for junk in ("src", "test", "tests", "__tests__", "preview-src", ".vscode"):
                _rmtree_generated_strict(child / junk)
            for junk_name in (
                "package-lock.json", "tsconfig.json", "tsconfig.browser.json",
                "tsconfig.tsbuildinfo", "tsconfig.browser.tsbuildinfo",
                ".vscodeignore", ".npmrc", ".gitignore",
            ):
                junk = child / junk_name
                if junk.is_file():
                    junk.unlink()
                    removed_misc += 1

    # server-web includes native/remote server entrypoints in the same output
    # tree as the browser workbench.  A static browser product cannot execute
    # Node platform modules or the server/agent launchers.
    out = staging / "out"
    if out.is_dir():
        for node_dir in list(out.rglob("node")):
            if node_dir.is_dir():
                _rmtree_generated_strict(node_dir)
                removed_misc += 1
        for name in ("server-main.js", "server-cli.js", "bootstrap-fork.js"):
            target = out / name
            if target.is_file():
                target.unlink()
                removed_misc += 1

    # Host-platform artwork/install resources are desktop packaging material.
    resources = staging / "resources"
    for platform_dir in ("win32", "darwin", "linux"):
        target = resources / platform_dir
        if target.exists():
            _rmtree_generated_strict(target)
            removed_misc += 1

    return {"maps": removed_maps, "misc": removed_misc, "extensions": removed_extensions}


def _stage_referenced_node_modules(source: Path, staging: Path) -> None:
    source_modules = source / "node_modules"
    candidates = _referenced_root_node_modules(staging / "out", staging / "extensions")

    # The web workbench resolves its TextMate tokenizer through FileAccess at
    # runtime, so these package URLs are constructed dynamically and do not
    # necessarily occur as literal /node_modules/... strings in emitted JS.
    # They are first-class browser runtime resources in upstream Code - OSS
    # (main.js plus Oniguruma's WASM), not optional extension dependencies.
    browser_runtime_packages = {"vscode-textmate", "vscode-oniguruma"}
    candidates.update(browser_runtime_packages)

    # The integrated terminal loads xterm packages through FileAccess-generated
    # URLs, so their /node_modules/... paths are not guaranteed to appear as
    # literals in the emitted bundle. Stage the exact @xterm packages installed
    # by this pinned upstream checkout; this follows upstream's lockfile rather
    # than maintaining a CinterForge addon list.
    xterm_scope = source_modules / "@xterm"
    if xterm_scope.is_dir():
        for child in sorted(xterm_scope.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir() and (child / "package.json").is_file():
                candidates.add(f"@xterm/{child.name}")

    # Generated Code - OSS output contains documentation, localization strings,
    # examples, and extension metadata alongside executable browser code.  Those
    # files can contain perfectly valid-looking /node_modules/<name>/ paths that
    # are not runtime dependencies (for example ``my-module`` and ``yo``).  The
    # checked-out npm tree is authoritative here: only stage references that
    # resolve to an installed root package.  Do not turn prose/example strings
    # into fatal packaging dependencies.
    packages = []
    ignored = []
    for package in sorted(candidates):
        src = source_modules.joinpath(*package.split("/"))
        if src.is_dir():
            packages.append(package)
        else:
            ignored.append(package)

    for package in packages:
        src = source_modules.joinpath(*package.split("/"))
        dst = (staging / "node_modules").joinpath(*package.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=_runtime_copy_ignore)

    required_runtime_files = [
        staging / "node_modules" / "vscode-textmate" / "release" / "main.js",
        staging / "node_modules" / "vscode-oniguruma" / "release" / "main.js",
        staging / "node_modules" / "vscode-oniguruma" / "release" / "onig.wasm",
    ]
    if xterm_scope.is_dir():
        required_runtime_files.append(
            staging / "node_modules" / "@xterm" / "xterm" / "lib" / "xterm.js"
        )
    missing_runtime_files = [str(path.relative_to(staging)) for path in required_runtime_files if not path.is_file()]
    if missing_runtime_files:
        raise RuntimeError(
            "Code - OSS browser tokenizer runtime is incomplete: " + ", ".join(missing_runtime_files)
        )

    _log(
        f"staged referenced Code - OSS browser node modules packages={len(packages)}"
        + (f" ignored_nonpackages={len(ignored)}" if ignored else "")
    )


def _audit_staged_web_extensions(staging: Path) -> None:
    """Fail if a staged web extension advertises a browser entrypoint we did not stage."""
    extensions = staging / "extensions"
    missing: list[str] = []
    if not extensions.is_dir():
        raise RuntimeError(f"Code - OSS staged extension runtime is missing: {extensions}")
    for manifest in sorted(extensions.glob("*/package.json")):
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid staged Code - OSS extension manifest: {manifest}") from exc
        browser = package.get("browser") if isinstance(package, dict) else None
        if not isinstance(browser, str) or not browser.strip():
            continue
        rel = browser.strip()
        target = manifest.parent / rel
        if target.is_file():
            continue
        # Node-style extension entrypoints may omit the .js suffix.
        if not target.suffix and target.with_suffix(".js").is_file():
            continue
        missing.append(f"{manifest.parent.name}:{rel}")
        if len(missing) >= 20:
            break
    if missing:
        raise RuntimeError(
            "Code - OSS staged browser extension entrypoints are missing: "
            + ", ".join(missing)
        )
    _log("staged Code - OSS browser extension entrypoint audit passed")



def _is_web_extension_manifest(manifest: dict) -> bool:
    """Return whether Code - OSS can load this staged extension in a web host."""
    browser = manifest.get("browser")
    if isinstance(browser, str) and browser.strip():
        return True
    kinds = manifest.get("extensionKind")
    if isinstance(kinds, str):
        kinds = [kinds]
    if isinstance(kinds, list) and any(str(value).strip().casefold() == "web" for value in kinds):
        return True
    # Declarative extensions (grammars/themes/snippets) have no executable
    # desktop entrypoint and are valid in the web workbench. A `main` entry
    # without a browser/web declaration is a desktop/node extension.
    return not bool(str(manifest.get("main") or "").strip())


def _patch_file_protocol_workers(web: Path) -> int:
    """Avoid Safari blob:file module workers while preserving HTTP behavior."""
    patched = 0
    workbench_js = web / "out" / "vs" / "code" / "browser" / "workbench" / "workbench.js"
    if workbench_js.is_file():
        text = workbench_js.read_text(encoding="utf-8")
        # Do not even create the bootstrap Blob under file://. Safari reports a
        # WebKitBlobResource failure for a Blob URL that is created and immediately
        # revoked even when that URL is never passed to Worker().
        old = "    const workerRunnerUrl = this.getWorkerUrl(descriptor);\n    const workerUrlWithNls = getWorkerBootstrapUrl(descriptor.label, workerRunnerUrl, this._getWorkerLoadingFailedErrorMessage(descriptor));\n    try {\n      const worker = createBlobWorker(workerUrlWithNls, { name: descriptor.label, type: \"module\" });\n      return whenESMWorkerReady(worker).finally(() => URL.revokeObjectURL(workerUrlWithNls));\n    } catch (error) {\n      URL.revokeObjectURL(workerUrlWithNls);\n      throw error;\n    }"
        new = "    const workerRunnerUrl = this.getWorkerUrl(descriptor);\n    if (globalThis.location?.protocol === \"file:\") {\n      return new Worker(ttPolicy5 ? ttPolicy5.createScriptURL(workerRunnerUrl) : workerRunnerUrl, { name: descriptor.label, type: \"module\" });\n    }\n    const workerUrlWithNls = getWorkerBootstrapUrl(descriptor.label, workerRunnerUrl, this._getWorkerLoadingFailedErrorMessage(descriptor));\n    try {\n      const worker = createBlobWorker(workerUrlWithNls, { name: descriptor.label, type: \"module\" });\n      return whenESMWorkerReady(worker).finally(() => URL.revokeObjectURL(workerUrlWithNls));\n    } catch (error) {\n      URL.revokeObjectURL(workerUrlWithNls);\n      throw error;\n    }"
        marker = 'globalThis.location?.protocol === "file:"'
        legacy = "    const workerRunnerUrl = this.getWorkerUrl(descriptor);\n    const workerUrlWithNls = getWorkerBootstrapUrl(descriptor.label, workerRunnerUrl, this._getWorkerLoadingFailedErrorMessage(descriptor));\n    if (globalThis.location?.protocol === \"file:\") {\n      URL.revokeObjectURL(workerUrlWithNls);\n      return new Worker(ttPolicy5 ? ttPolicy5.createScriptURL(workerRunnerUrl) : workerRunnerUrl, { name: descriptor.label, type: \"module\" });\n    }\n    try {\n      const worker = createBlobWorker(workerUrlWithNls, { name: descriptor.label, type: \"module\" });\n      return whenESMWorkerReady(worker).finally(() => URL.revokeObjectURL(workerUrlWithNls));\n    } catch (error) {\n      URL.revokeObjectURL(workerUrlWithNls);\n      throw error;\n    }"
        if legacy in text:
            workbench_js.write_text(text.replace(legacy, new, 1), encoding="utf-8")
            patched += 1
        elif marker not in text:
            if old not in text:
                raise RuntimeError(f"Code - OSS web-worker bootstrap changed upstream: {workbench_js}")
            workbench_js.write_text(text.replace(old, new, 1), encoding="utf-8")
            patched += 1
        elif "if (globalThis.location?.protocol" in text and "const workerUrlWithNls = getWorkerBootstrapUrl" in text.split('if (globalThis.location?.protocol',1)[0][-300:]:
            raise RuntimeError(f"Code - OSS file-worker patch is stale and still creates a Blob before the file:// branch: {workbench_js}")
    iframe = web / "out" / "vs" / "workbench" / "services" / "extensions" / "worker" / "webWorkerExtensionHostIframe.html"
    if iframe.is_file():
        text = iframe.read_text(encoding="utf-8")
        old = "\t\t\tconst worker = new Worker(URL.createObjectURL(blob), { name, type: 'module' });"
        replacement = "\t\t\tconst worker = location.protocol === 'file:'\n\t\t\t\t? new Worker(new URL('./cinterforgeFileExtensionHostWorker.js', location.href).href, { name, type: 'module' })\n\t\t\t\t: new Worker(URL.createObjectURL(blob), { name, type: 'module' });"
        marker = "cinterforgeFileExtensionHostWorker.js"
        if marker not in text:
            if old not in text:
                raise RuntimeError(f"Code - OSS extension-host worker bootstrap changed upstream: {iframe}")
            iframe.write_text(text.replace(old, replacement, 1), encoding="utf-8")
            patched += 1
        # A direct extensionHostWorkerMain.js worker is not equivalent to upstream's
        # Blob bootstrap: the Blob initializes NLS and _VSCODE_FILE_ROOT before the
        # module is evaluated. Safari rejects blob:file workers, so file:// uses a
        # static module bootstrap that performs the same initialization from the
        # packaged English NLS catalog, then imports the real extension host.
        bootstrap = iframe.with_name("cinterforgeFileExtensionHostWorker.js")
        nls_path = web / "out" / "nls.messages.js"
        nls_source = nls_path.read_text(encoding="utf-8") if nls_path.is_file() else ""
        if "_VSCODE_NLS_MESSAGES" not in nls_source:
            nls_source = "globalThis._VSCODE_NLS_MESSAGES = [];"
        bootstrap_text = (
            "// CinterForge file:// extension-host bootstrap. Generated by codeoss_install.py.\n"
            # Safari file:// workers must be self-contained. Embedding the generated
            # NLS assignment here preserves upstream's Blob bootstrap semantics without
            # depending on a second file:// module import before extensionHostWorkerMain.
            + nls_source.rstrip() + "\n"
            + "globalThis._VSCODE_NLS_LANGUAGE = 'en';\n"
            + "globalThis._VSCODE_FILE_ROOT = new URL('../../../../../', import.meta.url).href;\n"
            + "await import('../../../api/worker/extensionHostWorkerMain.js');\n"
        )
        if not bootstrap.is_file() or bootstrap.read_text(encoding="utf-8") != bootstrap_text:
            bootstrap.write_text(bootstrap_text, encoding="utf-8")
            patched += 1

        # Nested web workers are created inside extensionHostWorkerMain, but upstream
        # creates their Blob URL in the extension-host worker and asks the iframe to
        # consume that URL. WebKit does not reliably expose a worker-owned blob:file
        # URL to the parent window. Under file:// pass the bootstrap source instead and
        # let the iframe create/revoke the Blob in the same realm that constructs the
        # nested Worker. HTTP keeps the upstream URL path byte-for-byte in behavior.
        extension_host = web / "out" / "vs" / "workbench" / "api" / "worker" / "extensionHostWorkerMain.js"
        if extension_host.is_file():
            host_text = extension_host.read_text(encoding="utf-8")
            host_old = '    const blob = new Blob([bootstrap], { type: "application/javascript" });\n    const blobUrl = URL.createObjectURL(blob);\n    const channel = new MessageChannel();\n    const id2 = blobUrl;\n    const msg = {\n      type: "_newWorker",\n      id: id2,\n      port: channel.port2,\n      url: blobUrl,\n      options: options2\n    };'
            host_new = '    const blob = new Blob([bootstrap], { type: "application/javascript" });\n    const fileProtocol = globalThis.location?.protocol === "file:";\n    const blobUrl = fileProtocol ? "" : URL.createObjectURL(blob);\n    const channel = new MessageChannel();\n    const id2 = fileProtocol ? `cinterforge-nested-${Date.now()}-${Math.random()}` : blobUrl;\n    const msg = {\n      type: "_newWorker",\n      id: id2,\n      port: channel.port2,\n      url: blobUrl,\n      bootstrap: fileProtocol ? bootstrap : void 0,\n      options: options2\n    };'
            if "bootstrap: fileProtocol ? bootstrap : void 0" not in host_text:
                if host_old not in host_text:
                    raise RuntimeError(f"Code - OSS nested-worker bootstrap changed upstream: {extension_host}")
                host_text = host_text.replace(host_old, host_new, 1)
                host_text = host_text.replace("      URL.revokeObjectURL(blobUrl);", "      if (blobUrl) URL.revokeObjectURL(blobUrl);", 1)
                extension_host.write_text(host_text, encoding="utf-8")
                patched += 1

            # The source checkout is patched before compilation so file: extension modules
            # are read by MainThreadExtensionService through FileService RPC. Keep a compiled
            # output assertion/fixup here because the staged Web product is the runtime contract.
            # Never perform file:// fetch/XHR in Safari's extension-host worker.
            host_text = extension_host.read_text(encoding="utf-8")
            loader_old = '    const browserUri = URI.revive(await this._mainThreadExtensionsProxy.$asBrowserUri(module2));\n    const response = await fetch(browserUri.toString(true));\n    if (extensionId) {\n      performance.mark(`code/extHost/didFetchExtensionCode/${extensionId}`);\n    }\n    if (response.status !== 200) {\n      throw new Error(response.statusText);\n    }\n    const source = await response.text();'
            loader_rpc = '    let source;\n    if (module2.scheme === "file") {\n      source = await this._mainThreadExtensionsProxy.$readExtensionFile(module2);\n    } else {\n      const browserUri = URI.revive(await this._mainThreadExtensionsProxy.$asBrowserUri(module2));\n      const response = await fetch(browserUri.toString(true));\n      if (response.status !== 200) {\n        throw new Error(response.statusText);\n      }\n      source = await response.text();\n    }\n    if (extensionId) {\n      performance.mark(`code/extHost/didFetchExtensionCode/${extensionId}`);\n    }'
            xhr_marker = 'Unable to load local extension ${browserUri.toString(true)} via file://'
            if xhr_marker in host_text:
                xhr_start = host_text.rfind('    const browserUri = URI.revive(await this._mainThreadExtensionsProxy.$asBrowserUri(module2));', 0, host_text.index(xhr_marker))
                xhr_end_marker = '    if (extensionId) {\n      performance.mark(`code/extHost/didFetchExtensionCode/${extensionId}`);\n    }'
                xhr_end = host_text.index(xhr_end_marker, host_text.index(xhr_marker)) + len(xhr_end_marker)
                host_text = host_text[:xhr_start] + loader_rpc + host_text[xhr_end:]
                extension_host.write_text(host_text, encoding="utf-8"); patched += 1
            elif '$readExtensionFile(module2)' not in host_text:
                if loader_old not in host_text:
                    raise RuntimeError(f"Code - OSS CommonJS extension loader changed upstream: {extension_host}")
                extension_host.write_text(host_text.replace(loader_old, loader_rpc, 1), encoding="utf-8"); patched += 1

    # The staged Web bundle is the runtime contract. Some Code-OSS build/cache paths
    # can preserve a compiled workbench.js that predates the source-level RPC patch
    # while the separately emitted extensionHostWorkerMain.js already calls it. That
    # produces Safari's exact runtime error: Unknown method $readExtensionFile on actor
    # MainThreadExtensionService. Close that split-brain state in the compiled product.
    if workbench_js.is_file():
        text = workbench_js.read_text(encoding="utf-8")
        has_compiled_main_thread_extension_service = "var MainThreadExtensionService = class" in text
        if has_compiled_main_thread_extension_service and "$readExtensionFile(uri5)" not in text:
            ctor_old = "  constructor(extHostContext, _extensionService, _notificationService, _extensionsWorkbenchService, _hostService, _extensionEnablementService, _timerService, _commandService, _environmentService) {\n    this._extensionService = _extensionService;"
            ctor_new = "  constructor(extHostContext, _extensionService, _notificationService, _extensionsWorkbenchService, _hostService, _extensionEnablementService, _timerService, _commandService, _environmentService, _fileService) {\n    this._extensionService = _extensionService;\n    this._fileService = _fileService;"
            method_anchor = "  async $asBrowserUri(uri5) {\n    return FileAccess.uriToBrowserUri(URI.revive(uri5));\n  }"
            method_new = "  // CINTERFORGE_FILE_EXTENSION_SOURCE_RPC_COMPILED_V1\n  async $readExtensionFile(uri5) {\n    const content = await this._fileService.readFile(URI.revive(uri5));\n    return content.value.toString();\n  }\n" + method_anchor
            decorators_old = "  __decorateParam(8, IWorkbenchEnvironmentService)\n], MainThreadExtensionService);"
            decorators_new = "  __decorateParam(8, IWorkbenchEnvironmentService),\n  __decorateParam(9, IFileService)\n], MainThreadExtensionService);"
            if ctor_old not in text or method_anchor not in text or decorators_old not in text:
                raise RuntimeError(f"Code - OSS compiled MainThreadExtensionService changed upstream: {workbench_js}")
            text = text.replace(ctor_old, ctor_new, 1).replace(method_anchor, method_new, 1).replace(decorators_old, decorators_new, 1)
            workbench_js.write_text(text, encoding="utf-8")
            patched += 1
        # Fail at staging time rather than shipping a worker/main-thread RPC mismatch.
        text = workbench_js.read_text(encoding="utf-8")
        if has_compiled_main_thread_extension_service and ("$readExtensionFile(uri5)" not in text or "__decorateParam(9, IFileService)" not in text):
            raise RuntimeError(f"Code - OSS staged workbench is missing file-extension RPC implementation: {workbench_js}")

        iframe_text = iframe.read_text(encoding="utf-8")
        iframe_old = "\t\t\t\tif (data?.type === '_newWorker') {\n\t\t\t\t\tconst { id, port, url, options } = data;\n\t\t\t\t\tconst newWorker = new Worker(url, options);\n\t\t\t\t\tnewWorker.postMessage(port, [port]);\n\t\t\t\t\tnewWorker.onerror = console.error.bind(console);\n\t\t\t\t\tnestedWorkers.set(id, newWorker);\n\n\t\t\t\t} else if (data?.type === '_terminateWorker') {\n\t\t\t\t\tconst { id } = data;\n\t\t\t\t\tif (nestedWorkers.has(id)) {\n\t\t\t\t\t\tnestedWorkers.get(id).terminate();\n\t\t\t\t\t\tnestedWorkers.delete(id);\n\t\t\t\t\t}\n"
        iframe_new = "\t\t\t\tif (data?.type === '_newWorker') {\n\t\t\t\t\tconst { id, port, url, bootstrap, options } = data;\n\t\t\t\t\tlet ownedBlobUrl = '';\n\t\t\t\t\tconst workerUrl = location.protocol === 'file:' && bootstrap\n\t\t\t\t\t\t? (ownedBlobUrl = URL.createObjectURL(new Blob([bootstrap], { type: 'application/javascript' })))\n\t\t\t\t\t\t: url;\n\t\t\t\t\tconst newWorker = new Worker(workerUrl, options);\n\t\t\t\t\tnewWorker.postMessage(port, [port]);\n\t\t\t\t\tnewWorker.onerror = console.error.bind(console);\n\t\t\t\t\tnestedWorkers.set(id, { worker: newWorker, ownedBlobUrl });\n\n\t\t\t\t} else if (data?.type === '_terminateWorker') {\n\t\t\t\t\tconst { id } = data;\n\t\t\t\t\tif (nestedWorkers.has(id)) {\n\t\t\t\t\t\tconst nested = nestedWorkers.get(id);\n\t\t\t\t\t\tnested.worker.terminate();\n\t\t\t\t\t\tif (nested.ownedBlobUrl) URL.revokeObjectURL(nested.ownedBlobUrl);\n\t\t\t\t\t\tnestedWorkers.delete(id);\n\t\t\t\t\t}\n"
        if "ownedBlobUrl = URL.createObjectURL" not in iframe_text:
            if iframe_old in iframe_text:
                iframe.write_text(iframe_text.replace(iframe_old, iframe_new, 1), encoding="utf-8")
                patched += 1
            elif "const { id, port, url, options } = data" in iframe_text:
                raise RuntimeError(f"Code - OSS nested-worker iframe bridge changed upstream: {iframe}")
    return patched

def _refresh_extension_host_csp_hash(web: Path) -> None:
    """Keep the extension-host CSP hash aligned with our patched inline bootstrap."""
    iframe = web / "out" / "vs" / "workbench" / "services" / "extensions" / "worker" / "webWorkerExtensionHostIframe.html"
    if not iframe.is_file():
        raise RuntimeError(f"Code - OSS extension-host iframe is missing: {iframe}")
    text = iframe.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", text, flags=re.DOTALL)
    if not match:
        raise RuntimeError(f"Code - OSS extension-host inline bootstrap is missing: {iframe}")
    digest = base64.b64encode(hashlib.sha256(match.group(1).encode("utf-8")).digest()).decode("ascii")
    hashes = re.findall(r"'sha256-[A-Za-z0-9+/=]+'", text)
    if len(hashes) != 1:
        raise RuntimeError(f"Code - OSS extension-host CSP hash layout changed upstream: {iframe}")
    iframe.write_text(text.replace(hashes[0], f"'sha256-{digest}'", 1), encoding="utf-8")

def _render_static_workbench(web: Path) -> None:
    """Turn upstream's server-rendered workbench template into our static page."""
    workbench = web / "out" / "vs" / "code" / "browser" / "workbench" / "workbench.html"
    workbench_js = workbench.with_name("workbench.js")
    nls = web / "out" / "nls.messages.js"
    for required in (workbench, workbench_js, nls):
        if not required.is_file():
            raise RuntimeError(f"Code - OSS static workbench prerequisite is missing: {required}")

    builtin_extension_paths = []
    extension_root = web / "extensions"
    staged_manifests: dict[Path, dict] = {}
    if extension_root.is_dir():
        for extension_dir in sorted(extension_root.iterdir(), key=lambda item: item.name.lower()):
            manifest_path = extension_dir / "package.json"
            if not extension_dir.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Invalid staged Code - OSS extension manifest: {manifest_path}") from exc
            staged_manifests[extension_dir] = manifest
            if _is_web_extension_manifest(manifest) and extension_dir.name == "cinterforge-if":
                # Stock Code - OSS built-ins are discovered by Code itself. Only
                # CinterForge's external web extension belongs in the additional
                # built-in list; duplicating stock extensions causes noisy duplicate
                # discovery and risks duplicate activation.
                builtin_extension_paths.append("../../../../../extensions/" + extension_dir.name)

    # The static REH workbench does not receive the desktop product bootstrap
    # that grants VS Code's own bundled extensions their declared proposed APIs.
    # Preserve the grants from the exact staged/pinned extension manifests. This
    # is data-driven: never maintain a CinterForge allowlist that can drift from
    # the pinned upstream Code - OSS build.
    extension_api_proposals: dict[str, list[str]] = {}
    if extension_root.is_dir():
        for extension_dir, manifest in staged_manifests.items():
            if not _is_web_extension_manifest(manifest):
                continue
            publisher = str(manifest.get("publisher") or "vscode").strip()
            name = str(manifest.get("name") or "").strip()
            proposals = manifest.get("enabledApiProposals")
            if publisher and name and isinstance(proposals, list) and proposals:
                extension_api_proposals[f"{publisher}.{name}"] = [str(value) for value in proposals]

    config = {
        # Without folderUri, Code-OSS launches an empty-window workbench.
        # The filesystem provider can be healthy while Explorer has no root
        # and workspace-dependent terminal/editor initialization never runs.
        # Code 1.137 revives folderUri with URI.revive(), which expects
        # URI components. A string is treated as a scheme and throws UriError.
        "folderUri": {"scheme": "cinterforge", "path": "/"},
        "additionalBuiltinExtensions": [],
        "productConfiguration": {
            "enableTelemetry": False,
            "extensionEnabledApiProposals": extension_api_proposals,
        },
    }
    replacements = {
        "WORKBENCH_SCRIPT_NONCE": "cinterforge",
        "WORKBENCH_WEB_CONFIGURATION": html.escape(json.dumps(config, separators=(",", ":")), quote=True),
        "WORKBENCH_WEB_BASE_URL": "../../../../..",
        "WORKBENCH_AUTH_SESSION": "",
        "WORKBENCH_NLS_FALLBACK_URL": "../../../../../out/nls.messages.js",
        "WORKBENCH_NLS_URL": "../../../../../out/nls.messages.js",
    }
    rendered = workbench.read_text(encoding="utf-8")
    for key, value in replacements.items():
        rendered = rendered.replace("{{" + key + "}}", value)

    # Upstream resolves WORKBENCH_WEB_BASE_URL against window.location.origin and
    # then blindly appends `/out/`.  That only works when the base token is an
    # origin-absolute server path.  Our packaged workbench intentionally uses a
    # relative base so the same artifact can run from /codeoss *and* file://.
    # Resolve it against the workbench document itself and remove URL()'s trailing
    # slash before appending /out/.  Without the trim, file:// produces a path
    # containing `//out/`, which VS Code's URI validator rejects before startup.
    upstream_base_bootstrap = (
        "const baseUrl = new URL(document.getElementById('vscode-workbench-web-base-url').getAttribute('data-settings'), window.location.origin).toString();\n"
        "\t\tglobalThis._VSCODE_FILE_ROOT = baseUrl + '/out/';"
    )
    portable_base_bootstrap = (
        "const baseUrl = new URL(document.getElementById('vscode-workbench-web-base-url').getAttribute('data-settings'), window.location.href).toString().replace(/\\/$/, '');\n"
        "\t\tglobalThis._VSCODE_FILE_ROOT = baseUrl + '/out/';"
    )
    if upstream_base_bootstrap not in rendered:
        raise RuntimeError(f"Code - OSS static workbench base bootstrap changed upstream: {workbench}")
    rendered = rendered.replace(upstream_base_bootstrap, portable_base_bootstrap, 1)

    if "{{WORKBENCH_" in rendered or "{{" in rendered:
        raise RuntimeError(f"Code - OSS static workbench has unresolved template tokens: {workbench}")
    # Built-in extension URIs must follow the actual static publication origin.
    # Do not bake localhost (or any maintainer host) into the consumer artifact.
    cinterforge_manifest = staged_manifests.get(extension_root / "cinterforge-if")
    if not isinstance(cinterforge_manifest, dict):
        raise RuntimeError("CinterForge staged extension manifest is missing before static workbench rendering")
    # Match the proven maintainer Web artifact: only CinterForge's external
    # extension is supplied through the static DOM. Stock Code - OSS extensions
    # (including github-authentication) remain owned by the compiled production
    # scanner's bundled list; duplicating them here masks stale compiled products.
    builtin_dom = [{"extensionPath": "cinterforge-if", "packageJSON": cinterforge_manifest, "packageNLS": {}}]
    dynamic_extensions = (
        "<meta id=\"vscode-workbench-builtin-extensions\" data-settings=\""
        + html.escape(json.dumps(builtin_dom, separators=(",", ":")), quote=True)
        + "\">\n"
        "<script nonce=\"cinterforge\">\n"
        "{const m=document.getElementById('vscode-workbench-web-configuration');"
        "const c=JSON.parse(m.getAttribute('data-settings')||'{}');"
        "c.additionalBuiltinExtensions=[];"
        "m.setAttribute('data-settings',JSON.stringify(c));}\n"
        "</script>\n"
        "<script id=\"cinterforge-workbench-ui\" nonce=\"cinterforge\" src=\"../../../../../extensions/cinterforge-if/workbench_ui.js\"></script>\n"
    )
    marker = "</head>"
    if marker not in rendered:
        raise RuntimeError(f"Code - OSS static workbench has no </head> injection point: {workbench}")
    rendered = rendered.replace(marker, dynamic_extensions + marker, 1)
    workbench.write_text(rendered, encoding="utf-8")
    worker_patches = _patch_file_protocol_workers(web)
    _refresh_extension_host_csp_hash(web)
    _log(f"rendered static Code - OSS workbench builtins={len(builtin_extension_paths)} proposal_grants={len(extension_api_proposals)} file_worker_patches={worker_patches}")

def _refresh_static_workbench(source: Path, web: Path) -> None:
    """Refresh CinterForge-owned workbench bootstrap without rebuilding Code - OSS.

    The staged workbench is a rendered derivative of the pinned upstream template.
    Integration changes (extension filtering, proposal grants, runtime-origin wiring)
    must therefore be reapplied even when the upstream commit itself is unchanged.
    Always restore the pristine compiled template first so rendering is idempotent.
    """
    source_workbench = (
        source / "out-vscode-reh-web" / "vs" / "code" / "browser" / "workbench" / "workbench.html"
    )
    staged_workbench = (
        web / "out" / "vs" / "code" / "browser" / "workbench" / "workbench.html"
    )
    if not source_workbench.is_file():
        raise RuntimeError(f"Code - OSS compiled workbench template is missing: {source_workbench}")
    if not staged_workbench.parent.is_dir():
        raise RuntimeError(f"Code - OSS staged workbench directory is missing: {staged_workbench.parent}")
    shutil.copy2(source_workbench, staged_workbench)
    _render_static_workbench(web)


def _validate_required_browser_extensions(web: Path) -> None:
    """Fail the producer before publication if core web built-ins are incomplete."""
    extension_root = Path(web) / "extensions"
    missing: list[str] = []
    for name in CODEOSS_REQUIRED_BROWSER_EXTENSIONS:
        manifest_path = extension_root / name / "package.json"
        if not manifest_path.is_file():
            missing.append(f"{name}:package.json")
            continue
        try:
            package = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append(f"{name}:invalid-package.json")
            continue
        browser = package.get("browser") if isinstance(package, dict) else None
        if not isinstance(browser, str) or not browser.strip():
            missing.append(f"{name}:browser-entrypoint-not-declared")
            continue
        target = extension_root / name / browser.strip()
        if not target.is_file() and (target.suffix or not target.with_suffix(".js").is_file()):
            missing.append(f"{name}:{browser.strip()}")
    if missing:
        raise RuntimeError(
            "Code - OSS required browser extensions are incomplete: " + ", ".join(missing)
        )
    _log("required browser extensions validated: " + ", ".join(CODEOSS_REQUIRED_BROWSER_EXTENSIONS))


def _stage_web(source: Path, web: Path) -> None:
    output = source / "out-vscode-reh-web"
    required_files = ("LICENSE.txt", "ThirdPartyNotices.txt", "product.json")
    if not output.is_dir() or not (source / "resources").is_dir():
        raise RuntimeError("Code - OSS web build output/resources are incomplete")
    for name in required_files:
        if not (source / name).is_file():
            raise RuntimeError(f"Code - OSS required runtime metadata is missing: {source / name}")
    staging = web.with_name(web.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(output, staging / "out")
        shutil.copytree(source / "resources", staging / "resources")
        # Built-in extension packaging is split by upstream: authored/static files
        # live under source/extensions while compile-extensions-build materializes
        # build products under .build/extensions. Neither tree is a complete web
        # extension runtime by itself. Stage the authored tree first, then overlay
        # the compiled/package tree exactly as the upstream package build does.
        authored_extensions = source / "extensions"
        compiled_extensions = source / ".build" / "extensions"
        if not authored_extensions.is_dir():
            raise RuntimeError(f"Code - OSS authored extension tree is missing: {authored_extensions}")
        if not compiled_extensions.is_dir():
            raise RuntimeError(
                "Code - OSS compiled extension runtime is missing after "
                f"compile-extensions-build: {compiled_extensions}"
            )
        # The authored tree can contain npm-install artifacts (notably broken
        # extensions/node_modules/.bin symlinks on macOS). Those are build inputs,
        # not browser-extension payload. Runtime dependencies are supplied by the
        # compiled overlay and root node-module staging below.
        shutil.copytree(
            authored_extensions,
            staging / "extensions",
            ignore=shutil.ignore_patterns("node_modules", ".bin", "__pycache__", ".DS_Store"),
        )
        shutil.copytree(compiled_extensions, staging / "extensions", dirs_exist_ok=True)
        # Upstream also carries extensions used only by its browser test harness.
        # They are not part of the CinterForge production workbench and can depend
        # on test-only packages that are intentionally absent from the production
        # checkout/runtime. Never build, stage, register, or audit them.
        for name in CODEOSS_NONPRODUCTION_EXTENSIONS:
            shutil.rmtree(staging / "extensions" / name, ignore_errors=True)

        # The compiled workbench also imports a small set of browser libraries
        # from the checkout's root node_modules (for example vscode-textmate and
        # vscode-oniguruma). Stage every root package referenced by emitted web
        # code/compiled extensions. This is derived from the build output rather
        # than maintained as a language-specific allowlist.
        _stage_referenced_node_modules(source, staging)
        for name in required_files:
            shutil.copy2(source / name, staging / name)
        # CinterForge owns this web extension. It is injected into the staged
        # runtime at build time; the pinned upstream checkout remains pristine.
        _stage_cinterforge_extension(staging)
        pruned = _prune_static_web_runtime(staging)
        _log(
            "pruned static Code - OSS runtime "
            f"source_maps={pruned['maps']} nonruntime={pruned['misc']} "
            f"nonweb_extensions={pruned['extensions']}"
        )
        stripped_maps = _strip_dangling_source_map_references(staging)
        if stripped_maps:
            _log(f"stripped dangling source-map references files={stripped_maps}")
        nls_created = _ensure_extension_nls_files(staging)
        if nls_created:
            _log(f"staged empty localization catalogs for extensions count={nls_created}")
        _validate_required_browser_extensions(staging)
        _audit_staged_web_extensions(staging)
        _render_static_workbench(staging)
        validate_codeoss_web(staging)
        _log("static Code - OSS consumer validation passed")
        if web.exists():
            shutil.rmtree(web)
        staging.replace(web)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def ensure_codeoss(repo_root: Path | None = None, *, refresh: bool = False, force: bool = False) -> dict[str, Any]:
    """Ensure the staged Code - OSS web runtime exists.

    ``refresh=True``/``force=True`` is maintainer-only. Ordinary consumers never
    acquire or compile source; product sync installs the packaged Web tree.
    """
    repo = Path(repo_root).expanduser().resolve() if repo_root else _project_root()
    root = codeoss_build_root(repo)
    web = root / "Web"
    manifest_path = root / "codeoss.json"
    if codeoss_is_installed(repo) and not refresh and not force:
        return json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    if not refresh and not force:
        # Source checkouts never embed precompiled .cflib archives. A consumer
        # obtains the finished runtime through the published WebApp package;
        # WebApp Setup explicitly requests refresh=True for source compilation.
        raise RuntimeError(
            f"Packaged Code - OSS web runtime is missing: {web}. "
            "Build with WebApp Setup or install the published WebApp runtime."
        )

    ref = str(os.environ.get("CINTERFORGE_CODEOSS_REF") or CODEOSS_REF).strip() or CODEOSS_REF
    source = root / "Source"
    root.mkdir(parents=True, exist_ok=True)
    _log(f"maintainer build ref={ref} source={source} web={web}")
    commit = _sync_checkout(source, refresh=True, ref=ref)
    _patch_cinterforge_bootstrap_provider(source)
    _patch_builtin_extension_scanner(source)
    _patch_file_onboarding_editor_detection(source)
    _patch_file_extension_module_loading(source)
    existing: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                existing = value
        except Exception:
            pass
    installed_same_commit = (
        not force
        and codeoss_is_installed(repo)
        and existing.get("commit") == commit
        and existing.get("schema") == CODEOSS_SCHEMA
    )
    compiled_cache_matches = not force and _build_cache_matches(root, source, commit)
    if installed_same_commit and compiled_cache_matches:
        # The compiled upstream products are reusable, but Web is derived output.
        # Reconstruct it atomically from the proven source/build products on every
        # refresh so an incomplete or stale consumer tree can never survive merely
        # because its manifest and a few bootstrap files still validate.
        _ensure_browser_extension_products(source)
        _stage_web(source, web)
        _log(
            f"upstream + compiled bootstrap unchanged commit={commit[:12]}; "
            f"reconstructed complete staged web runtime={web}"
        )
        return existing
    if installed_same_commit and not compiled_cache_matches:
        _log(
            f"compiled bootstrap cache changed/invalid commit={commit[:12]}; "
            "rejecting staged Web reuse and rebuilding Code - OSS"
        )

    # Keep expensive upstream compilation independent from final Web staging.
    # If staging/auditing fails, the successful compile products remain reusable
    # on the next maintainer run instead of repeating npm ci + both gulp builds.
    cache_matches = compiled_cache_matches
    # Reuse compiled products only when our own build-cache marker proves they
    # were compiled by this producer schema.  The pinned upstream commit alone
    # is insufficient because CinterForge patches web.main.ts before compiling.
    # Adopting arbitrary products from the same upstream commit can therefore
    # stage an unpatched workbench that has no early cinterforge: provider.
    if cache_matches:
        _log(f"compiled Code - OSS unchanged commit={commit[:12]}; reusing build products")
    else:
        _build_web(source)
        _record_build_cache(root, source, commit)
        _log(f"compiled Code - OSS cached commit={commit[:12]}")
    # Modern built-in extensions (Markdown, Emmet, TypeScript, etc.) own
    # their browser bundles in package-level compile scripts. The root gulp
    # extension build alone does not guarantee those dist/browser products.
    # Fill only missing bundles here; successful products remain in Source and
    # subsequent update-data runs are a fast no-op.
    _ensure_browser_extension_products(source)
    _stage_web(source, web)
    manifest = {
        "schema": CODEOSS_SCHEMA,
        "repository": CODEOSS_REPOSITORY,
        "ref": ref,
        "commit": commit,
        "version": CODEOSS_VERSION if ref == CODEOSS_REF else "custom",
        "web": str(web),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _log(f"staged Code - OSS {manifest['version']} commit={commit[:12]} web={web}")
    return manifest


def ensure_codeoss_test_node(repo_root: Path | None = None) -> Path:
    """Return the Node executable used for deterministic adapter acceptance."""
    configured = str(os.environ.get("CINTERFORGE_CODEOSS_TEST_NODE") or "").strip()
    if configured:
        node = Path(configured).expanduser().resolve()
        if not node.is_file():
            raise RuntimeError(f"Configured Code - OSS acceptance Node executable is missing: {node}")
        return node
    repo = Path(repo_root).expanduser().resolve() if repo_root else _project_root()
    root = codeoss_build_root(repo)
    source = root / "Source"
    if not source.is_dir():
        raise RuntimeError(f"Code - OSS source checkout is required before acceptance: {source}")
    node, _npm_cli, _version = _ensure_pinned_node(source, root)
    return node


def ensure_codeoss_test_web(repo_root: Path | None = None) -> tuple[Path, Path]:
    """Install the pinned @vscode/test-web acceptance host for maintainers.

    The host is build/test tooling only and is never included in the distributed
    Code - OSS Web runtime.  Keep the npm install root intact so Node can resolve
    dependencies that npm hoists beside @vscode/test-web.
    """
    repo = Path(repo_root).expanduser().resolve() if repo_root else _project_root()
    root = codeoss_build_root(repo)
    source = root / "Source"
    if not source.is_dir():
        raise RuntimeError(f"Code - OSS source checkout is required before acceptance host setup: {source}")
    node, npm_cli, node_version = _ensure_pinned_node(source, root)
    install_root = root / "TestWebHost"
    package_root = install_root / "node_modules" / "@vscode" / "test-web"
    main = package_root / "out" / "server" / "main.js"
    package_json = package_root / "package.json"
    if main.is_file() and package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            package = {}
        if package.get("version") == CODEOSS_TEST_WEB_VERSION:
            _log(f"@vscode/test-web {CODEOSS_TEST_WEB_VERSION} ready: {package_root}")
            return package_root, node

    staging = root / "TestWebHost.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    npm = [str(node), str(npm_cli)]
    _log(f"installing @vscode/test-web {CODEOSS_TEST_WEB_VERSION} with pinned Node.js v{node_version}")
    _run_checked(
        [*npm, "install", "--prefix", str(staging), "--no-audit", "--no-fund",
         f"@vscode/test-web@{CODEOSS_TEST_WEB_VERSION}"],
        cwd=root, timeout=900, env=env,
    )
    staged_package = staging / "node_modules" / "@vscode" / "test-web"
    if not (staged_package / "out" / "server" / "main.js").is_file():
        raise RuntimeError(f"@vscode/test-web install is incomplete: {staged_package}")
    if install_root.exists():
        shutil.rmtree(install_root)
    staging.replace(install_root)
    _log(f"@vscode/test-web {CODEOSS_TEST_WEB_VERSION} installed: {package_root}")
    return package_root, node
