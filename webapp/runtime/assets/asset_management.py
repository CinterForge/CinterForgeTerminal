#========= Copyright 2025+, CinterForge, Inc. All rights reserved. ============
"""CinterForge asset management.

Normal editor/runtime asset authority is Cloudflare.  The local
``Builds/Assets`` tree is a Build-time authoring input only
(Art Department and TestingGrounds included); live Asset UI paths must not use it
as an asset catalog or byte source.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from runtime.assets.project_paths import interactive_fiction_root, repo_root

SCHEMA = "cinterforge.asset-folder.v3"
ASSET_REFERENCE_VERSION = 2
LIBRARY_SCHEMA = "cinterforge.assets-library.v3"

# Every file needed to reopen, preview, animate, regenerate, and export one
# saved version is owned here.  Representation-only entries may point inside
# another owned file (for example an entity identifier inside an mcaddon).
ARTIFACT_ROLES = (
    "source", "thumbnail", "preview", "runtime", "blockify", "addon",
    "picture", "statue", "egg", "hand", "world", "chest", "blockbench",
)
PORTABLE_FILE_ROLES = {"source_dependency", "runtime_dependency", "viewer_dependency"}
_ASSETS_ROOT_CONTEXT: ContextVar[Path | None] = ContextVar("cinterforge_assets_root_context", default=None)
_ASSET_PROGRESS_CONTEXT: ContextVar[Callable[..., None] | None] = ContextVar("cinterforge_asset_progress_context", default=None)

BLOCKIFY_ADDON_SCHEMA = "cinterforge.blockify-addon.single-placeable-v22"
BLOCKIFY_ADDON_PACK_VERSION = [1, 16, 0]

def _normalize_version_artifacts(version: dict[str, Any]) -> dict[str, Any]:
    """Keep one authoritative ownership record for all files/representations derived from a source version.

    Legacy fields remain mirrored for compatibility with the UI/runtime, but Delete/Setup/Sync
    consume ``artifacts`` rather than discovering loose files on disk.
    """
    artifacts = version.get("artifacts") if isinstance(version.get("artifacts"), dict) else {}
    normalized: dict[str, Any] = {role: artifacts.get(role) for role in ARTIFACT_ROLES}
    source_file = str(version.get("file") or "").strip()
    normalized["source"] = {"file": source_file} if source_file else None
    thumbnail_file = str(version.get("thumbnail_file") or "").strip()
    normalized["thumbnail"] = {"file": thumbnail_file} if thumbnail_file else None
    blockify = version.get("blockify") if isinstance(version.get("blockify"), dict) else {}
    blockify_file = str(blockify.get("file") or "").strip()
    addon_file = str(blockify.get("mcaddon_file") or "").strip()
    normalized["blockify"] = {"file": blockify_file} if blockify_file else None
    normalized["addon"] = {"file": addon_file} if addon_file else None
    blockbench_file = str(blockify.get("blockbench_file") or "").strip()
    normalized["blockbench"] = {"file": blockbench_file} if blockbench_file else None
    if addon_file:
        entity_identifier = str(blockify.get("minecraft_entity_identifier") or "").strip()
        egg_identifier = str(blockify.get("minecraft_egg_item_identifier") or "").strip()
        statue_identifier = str(blockify.get("minecraft_statue_entity_identifier") or "").strip()
        picture_identifier = str(blockify.get("minecraft_picture_item_identifier") or "").strip()
        picture_file = str(blockify.get("picture_file") or "").strip()
        if statue_identifier:
            normalized["statue"] = {"contained_in": addon_file, "identifier": statue_identifier}
        if picture_identifier:
            normalized["picture"] = {"contained_in": addon_file, "identifier": picture_identifier}
            if picture_file:
                normalized["picture"]["file"] = picture_file
        if egg_identifier:
            normalized["egg"] = {"contained_in": addon_file, "identifier": egg_identifier}
        elif entity_identifier and entity_identifier != statue_identifier:
            normalized["egg"] = {"contained_in": addon_file, "identifier": entity_identifier}
        elif isinstance(normalized.get("egg"), dict) and normalized["egg"].get("contained_in"):
            normalized["egg"] = None
    else:
        if isinstance(normalized.get("statue"), dict) and normalized["statue"].get("contained_in"): normalized["statue"] = None
        if isinstance(normalized.get("picture"), dict) and normalized["picture"].get("contained_in"): normalized["picture"] = None
        if isinstance(normalized.get("egg"), dict) and normalized["egg"].get("contained_in"): normalized["egg"] = None
    version["artifacts"] = normalized
    return normalized

def _normalize_portable_files(version: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize additional locally-owned files needed to reopen this version.

    Fixed one-per-role artifacts live in ``artifacts``.  This list covers the
    unbounded case: external glTF buffers/textures, viewer conversion files, or
    other runtime intermediates that must travel with the asset.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(version.get("portable_files") or []):
        if not isinstance(raw, dict):
            continue
        filename = str(raw.get("file") or "").strip()
        role = str(raw.get("role") or "runtime_dependency").strip()
        if not filename or filename in seen or role not in PORTABLE_FILE_ROLES:
            continue
        row = dict(raw); row["file"] = filename; row["role"] = role
        rows.append(row); seen.add(filename)
    version["portable_files"] = rows
    return rows

def _owned_artifact_files(version: dict[str, Any]) -> dict[str, str]:
    artifacts = _normalize_version_artifacts(version)
    result: dict[str, str] = {}
    for role, meta in artifacts.items():
        if isinstance(meta, dict):
            filename = str(meta.get("file") or "").strip()
            if filename:
                result[role] = filename
    for index, meta in enumerate(_normalize_portable_files(version)):
        filename = str(meta.get("file") or "").strip()
        if filename:
            result[f"portable:{index}:{meta.get('role')}"] = filename
    return result

def _managed_folder_files(folder: Path) -> list[Path]:
    """Return only files owned by the manifest; loose/orphan files are intentionally excluded."""
    manifest_path = _manifest_path(folder)
    if not manifest_path.is_file():
        return []
    manifest = _load_manifest(folder)
    before = json.dumps(manifest.get("versions") or [], sort_keys=True, separators=(",",":"), default=str)
    names = {"manifest.json"}
    for version in manifest.get("versions") or []:
        if isinstance(version, dict):
            names.update(_owned_artifact_files(version).values())
    after = json.dumps(manifest.get("versions") or [], sort_keys=True, separators=(",",":"), default=str)
    if before != after:
        _write_manifest(folder, manifest)
    files=[]
    for name in sorted(names):
        candidate=(folder/name).resolve()
        if folder.resolve() in candidate.parents and candidate.is_file():
            files.append(candidate)
    return files

def _managed_tree_snapshot(root: Path) -> dict[str, str]:
    root=Path(root).resolve(); result: dict[str,str]={}
    if not root.is_dir(): return result
    for manifest_path in sorted(root.rglob("manifest.json")):
        folder=manifest_path.parent
        for path in _managed_folder_files(folder):
            result[path.relative_to(root).as_posix()] = _sha256(path)
    return result

def _authoritative_tree_snapshot(root: Path) -> dict[str, str]:
    """Snapshot the complete shared Builds/Assets tree.

    Cloudflare transport treats Builds/Assets as one authoritative byte library.
    Asset manifests remain application metadata; they do not decide which files
    are allowed to travel between machines.
    """
    return _tree_snapshot(Path(root).resolve())


def _overlay_authoritative_tree(source: Path, destination: Path) -> dict[str, int]:
    """Overlay one published Builds/Assets snapshot onto a local tree.

    Every published path is authoritative and is copied/updated in place. Files
    that exist only on the local machine are preserved so ordinary setup does not
    destroy local work. There are no namespace-specific rules: RPG, Objects,
    Animated, Images, and future asset roots all use this same transport behavior.
    """
    source=Path(source).resolve(); destination=Path(destination).resolve()
    result={"copied":0,"updated":0,"unchanged":0}
    if not source.is_dir():
        return result
    for src in sorted((p for p in source.rglob("*") if p.is_file()), key=lambda p:p.relative_to(source).as_posix().casefold()):
        rel=src.relative_to(source); dst=destination/rel
        if dst.is_file():
            if _sha256(dst)==_sha256(src):
                result["unchanged"]+=1
                continue
            dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst); result["updated"]+=1
        else:
            dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst); result["copied"]+=1
    if any(result.values()):
        log(f"authoritative-assets-overlay source={source} destination={destination} result={result}")
    return result


def project_root() -> Path:
    """Compatibility accessor for repository source paths; shared path authority."""
    return repo_root()


def assets_root() -> Path:
    """Return the shared Builds/Assets library without materializing it.

    Runtime reads must never recreate Builds/Assets. Writers own
    creation of the specific destination directories they need.
    """
    contextual = _ASSETS_ROOT_CONTEXT.get()
    if contextual is not None:
        return contextual
    override = os.environ.get("CINTERFORGE_ASSETS_ROOT", "").strip()
    return Path(override).expanduser().resolve() if override else interactive_fiction_root().parent / "Assets"


def _log_path() -> Path:
    storage = os.environ.get("CINTERFORGE_STORAGE_ROOT", "").strip()
    base = Path(storage).expanduser() if storage else assets_root().parent
    path = base / "Progress" / "ifplayer.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def log(message: str) -> None:
    line = f"[asset_management] {datetime.now(timezone.utc).isoformat()} {message}"
    print(line, flush=True)
    progress = _ASSET_PROGRESS_CONTEXT.get()
    if progress is not None:
        progress(state="running", output=line)
    # IFPlayer already tees stdout/stderr into this exact log. Writing directly
    # as well duplicates every asset-management line. Keep direct append only
    # for callers running outside the IFPlayer process tee (setup/tools/tests).
    if os.environ.get("CINTERFORGE_IF_LOG_ACTIVE") == "1":
        return
    try:
        with _log_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        print(f"[asset_management] log-write-failed path={_log_path()} error={exc!r}", flush=True)


def _safe_token(text: str, fallback: str = "asset") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "").strip()).strip("._-")
    return text[:80] or fallback


def search_name(query: str, fallback: str = "asset") -> str:
    """Return the logical saved-asset name derived from the user's search.

    Parenthesized qualifiers are deliberately excluded so searches such as
    ``brass lantern ( lit )`` and ``brass lantern ( unlit )`` share one managed
    asset folder.  The remaining full phrase is retained (rather than only the
    first word), whitespace is normalized, and the result is filesystem-safe.
    """
    text = str(query or "").strip()
    # Remove parenthesized qualifiers, including nested groups, without
    # accidentally concatenating the text on either side of the qualifier.
    previous = None
    while text != previous:
        previous = text
        text = re.sub(r"\([^()]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    logical = text or str(fallback or "asset").strip()
    name = _safe_token(logical, fallback=_safe_token(fallback))
    log(f"search-name-derived query={query!r} logical={logical!r} saved_name={name!r}")
    return name


def _manifest_path(folder: Path) -> Path:
    return folder / "manifest.json"


def _load_manifest(folder: Path) -> dict[str, Any]:
    path = _manifest_path(folder)
    if not path.is_file():
        return {
            "schema": SCHEMA,
            "name": folder.name,
            "version_count": 0,
            "versions": [],
            "last_viewed": "",
            "default_version": "",
            "default_animation": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("manifest root is not an object")
        ref_version = value.get("asset_reference_version", 1)
        if not isinstance(ref_version, int) or isinstance(ref_version, bool):
            raise ValueError(f"invalid asset_reference_version: {ref_version!r}")
        if ref_version < 1 or ref_version > ASSET_REFERENCE_VERSION:
            raise ValueError(f"unsupported asset_reference_version: {ref_version!r}")
        # v1/unversioned is read-only migration input. Do not mutate it here.
        # v2 is canonical and must already be portable.
        if ref_version == ASSET_REFERENCE_VERSION:
            _validate_manifest_relative_paths(value)
        return value
    except Exception as exc:
        log(f"manifest-load-failed folder={folder} error={exc!r}")
        raise


def _is_absolute_filesystem_string(value: str) -> bool:
    """Return True for machine-specific filesystem locations, never for URLs."""
    text = str(value or "").strip()
    if not text or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        return False
    return text.startswith(("/", "\\\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", text))


def _portable_asset_path_from_legacy_absolute(value: str) -> str | None:
    """Convert an old absolute Assets pathname to its portable Assets-relative key.

    Legacy manifests are accepted on input so they can be repaired.  We only
    migrate paths whose identity is unambiguous: the suffix following an
    ``Assets`` path component.  Nothing machine-specific is retained.
    """
    text = str(value or "").strip().replace("\\", "/")
    parts = [part for part in text.split("/") if part]
    asset_indexes = [index for index, part in enumerate(parts) if part.casefold() == "assets"]
    if not asset_indexes:
        return None
    index = asset_indexes[-1]
    relative = "/".join(parts[index + 1:]).strip("/")
    if not relative or relative.startswith("../") or "/../" in f"/{relative}/":
        return None
    return relative


def _manifest_string_is_filesystem_path(value: str, location: str) -> bool:
    """True only when an absolute-looking manifest string is actually a filesystem path.

    Minecraft slash commands (for example ``/give``) are data, not paths.  They
    live under the explicit ``minecraft_commands`` mapping and must survive the
    portable-path guard unchanged.
    """
    if ".minecraft_commands." in location:
        return False
    return _is_absolute_filesystem_string(value)


def _normalize_legacy_manifest_paths(value: Any, *, location: str = "manifest") -> Any:
    """Repair legacy absolute asset paths at the serialization boundary.

    Loading remains permissive.  Before a manifest is written, recognizable old
    ``.../Assets/<portable-key>`` values become ``<portable-key>``.  An absolute
    path that cannot be mapped safely fails visibly instead of being persisted or
    silently discarded.
    """
    if isinstance(value, dict):
        for key in list(value):
            value[key] = _normalize_legacy_manifest_paths(value[key], location=f"{location}.{key}")
        return value
    if isinstance(value, list):
        for index in range(len(value)):
            value[index] = _normalize_legacy_manifest_paths(value[index], location=f"{location}[{index}]")
        return value
    if isinstance(value, str) and _manifest_string_is_filesystem_path(value, location):
        portable = _portable_asset_path_from_legacy_absolute(value)
        if portable is None:
            raise ValueError(f"asset manifest contains unmappable absolute filesystem path at {location}: {value!r}")
        log(f"manifest-path-migrated location={location} portable={portable}")
        return portable
    return value


def _validate_manifest_relative_paths(value: Any, *, location: str = "manifest") -> None:
    """Fail before persisting any machine-specific filesystem path in an asset manifest."""
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_manifest_relative_paths(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_manifest_relative_paths(child, location=f"{location}[{index}]")
    elif isinstance(value, str) and _manifest_string_is_filesystem_path(value, location):
        raise ValueError(f"asset manifest contains absolute filesystem path at {location}: {value!r}")


def _write_manifest(folder: Path, manifest: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    # Accept legacy manifests on read, but canonicalize machine-specific asset
    # paths before *any* descriptor or manifest serialization occurs.
    _normalize_legacy_manifest_paths(manifest)
    versions = [v for v in list(manifest.get("versions") or []) if isinstance(v, dict)]
    # Normalize stable animation dependency references before writing either the
    # folder manifest or per-version runtime descriptors.
    assigned, default = _portable_animation_references(manifest)
    if assigned:
        manifest["assigned_animations"] = assigned
    elif "assigned_animations" in manifest:
        manifest["assigned_animations"] = []
    if default:
        manifest["default_animation"] = default
    for version in versions:
        _normalize_version_artifacts(version)
        _normalize_portable_files(version)
        _write_version_runtime_descriptor(folder, manifest, version)
        _normalize_version_artifacts(version)
        _normalize_portable_files(version)
    # Every manifest emitted by current code is canonical v2. v1/unversioned
    # exists only as migration input and is never written back.
    manifest["asset_reference_version"] = ASSET_REFERENCE_VERSION
    manifest["schema"] = SCHEMA
    manifest["name"] = str(manifest.get("name") or folder.name)
    manifest["version_count"] = len(versions)
    manifest["versions"] = versions
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    _validate_manifest_relative_paths(manifest)
    temp = _manifest_path(folder).with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp.replace(_manifest_path(folder))
    try:
        display_folder = folder.resolve().relative_to(assets_root().resolve()).as_posix() or "."
    except ValueError:
        display_folder = str(folder.resolve())
    log(
        f"manifest-written folder={display_folder} versions={len(versions)} "
        f"last_viewed={manifest.get('last_viewed', '')} "
        f"default={manifest.get('default_version', '')}"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_descriptor_filename(version: dict[str, Any]) -> str:
    source = str(version.get("file") or "asset")
    return f"{Path(source).stem}.runtime.json"


def _animation_reference(folder_rel: str, filename: str, *, name: str = "", pose_frame: int = 0) -> dict[str, Any]:
    """Return a portable animation dependency reference with stable identity.

    Paths remain for fast local resolution, while provider/asset-id/hash allow Setup
    to repair references if a filename had to be renamed because of a collision.
    """
    ref: dict[str, Any] = {"folder": str(folder_rel or ""), "file": str(filename or ""), "pose_frame": int(pose_frame)}
    if name:
        ref["name"] = str(name)
    root = assets_root().resolve()
    try:
        folder = (root / ref["folder"]).resolve()
        if root not in folder.parents or not folder.is_dir():
            return ref
        manifest = _load_manifest(folder)
        version = next((v for v in manifest.get("versions") or [] if isinstance(v, dict) and str(v.get("file") or "") == ref["file"]), None)
        if not isinstance(version, dict):
            return ref
        source = folder / ref["file"]
        digest = str(version.get("sha256") or "").strip().lower() or (_sha256(source) if source.is_file() else "")
        if digest:
            ref["sha256"] = digest
        for key in ("provider", "kind", "asset_id", "search_name"):
            value = version.get(key)
            if value not in (None, ""):
                ref[key] = value
        if not ref.get("name"):
            metadata = version.get("metadata") if isinstance(version.get("metadata"), dict) else {}
            ref["name"] = str(metadata.get("title") or manifest.get("name") or Path(ref["file"]).stem)
    except Exception as exc:
        log(f"animation-reference-enrich-failed folder={folder_rel} file={filename} error={exc!r}")
    return ref


def _portable_animation_references(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    assigned: list[dict[str, Any]] = []
    for raw in list(manifest.get("assigned_animations") or []):
        if not isinstance(raw, dict):
            continue
        folder = str(raw.get("folder") or "").strip(); filename = str(raw.get("file") or "").strip()
        if not folder or not filename:
            continue
        enriched = _animation_reference(folder, filename, name=str(raw.get("name") or ""), pose_frame=int(raw.get("pose_frame") or 0))
        # Existing stable identity from a transported manifest wins over any
        # coincidental local file currently occupying the same path.
        enriched = {**enriched, **raw}
        assigned.append(enriched)
    default_raw = manifest.get("default_animation") if isinstance(manifest.get("default_animation"), dict) else {}
    default: dict[str, Any] = {}
    if default_raw:
        folder = str(default_raw.get("folder") or "").strip(); filename = str(default_raw.get("file") or "").strip()
        if folder and filename:
            default = {**_animation_reference(folder, filename, name=str(default_raw.get("name") or ""), pose_frame=int(default_raw.get("pose_frame") or 0)), **default_raw}
    return assigned, default



def _version_dependencies(manifest: dict[str, Any], version: dict[str, Any]) -> list[dict[str, Any]]:
    """Return explicit cross-asset dependencies required for identical UI/runtime behavior."""
    dependencies: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    assigned, default = _portable_animation_references(manifest)
    for raw in ([default] if default else []) + assigned:
        if not isinstance(raw, dict):
            continue
        folder = str(raw.get("folder") or "").strip(); filename = str(raw.get("file") or "").strip()
        key=(folder,filename)
        if not folder or not filename or key in seen:
            continue
        row=dict(raw); row["relationship"] = "default_animation" if default and key==(str(default.get("folder") or ""),str(default.get("file") or "")) else "animation"
        row["required_for_ui"] = True
        dependencies.append(row); seen.add(key)
    return dependencies

def _validate_version_portability(folder: Path, manifest: dict[str, Any], version: dict[str, Any], *, root: Path | None = None) -> list[str]:
    """Return missing/corrupt required files for one portable asset version."""
    issues: list[str] = []
    folder = Path(folder).resolve(); library_root = Path(root).resolve() if root else assets_root().resolve()
    artifacts=_normalize_version_artifacts(version)
    runtime_meta=artifacts.get("runtime") if isinstance(artifacts.get("runtime"),dict) else {}
    if not str(runtime_meta.get("file") or "").strip():
        issues.append("owned:runtime:missing-manifest-entry")
    for role, filename in _owned_artifact_files(version).items():
        candidate=(folder/filename).resolve()
        if folder not in candidate.parents or not candidate.is_file() or candidate.stat().st_size <= 0:
            issues.append(f"owned:{role}:{filename}")
    for dependency in _version_dependencies(manifest, version):
        dep_folder=str(dependency.get("folder") or ""); dep_file=str(dependency.get("file") or "")
        candidate=(library_root/dep_folder/dep_file).resolve()
        if library_root not in candidate.parents or not candidate.is_file() or candidate.stat().st_size <= 0:
            issues.append(f"dependency:{dep_folder}/{dep_file}")
            continue
        expected=str(dependency.get("sha256") or "").strip().lower()
        if expected and _sha256(candidate).lower()!=expected:
            issues.append(f"dependency-sha:{dep_folder}/{dep_file}")
    return issues

def _materialize_portable_contract(root: Path | None = None) -> dict[str, int]:
    """Upgrade valid legacy manifests in-place before validation/publishing.

    This does not invent missing source/dependency data. It only writes the
    deterministic runtime descriptor/ownership metadata that can be rebuilt
    from files already present locally.
    """
    library_root=(Path(root).resolve() if root else assets_root().resolve())
    folders=versions=0
    if not library_root.is_dir(): return {"folders":0,"versions":0}
    for manifest_path in sorted(library_root.rglob("manifest.json")):
        folder=manifest_path.parent
        try: manifest=_load_manifest(folder)
        except Exception: continue
        valid=[]
        for version in manifest.get("versions") or []:
            if not isinstance(version,dict): continue
            filename=str(version.get("file") or "").strip()
            if filename and (folder/filename).is_file():
                valid.append(version)
        if not valid: continue
        manifest["versions"]=valid
        _write_manifest(folder,manifest); folders += 1; versions += len(valid)
    log(f"asset-portable-contract-materialized root={library_root} folders={folders} versions={versions}")
    return {"folders":folders,"versions":versions}


def validate_library_portability(root: Path | None = None, *, fail: bool = False) -> dict[str, Any]:
    """Validate that every manifest describes a self-contained, reproducible UI/runtime asset."""
    library_root=(Path(root).resolve() if root else assets_root().resolve())
    problems: list[dict[str, Any]]=[]; versions=0
    if library_root.is_dir():
        for manifest_path in sorted(library_root.rglob("manifest.json")):
            folder=manifest_path.parent
            try: manifest=_load_manifest(folder)
            except Exception as exc:
                problems.append({"folder":folder.relative_to(library_root).as_posix(),"issues":[f"manifest:{exc}"]}); continue
            for version in manifest.get("versions") or []:
                if not isinstance(version,dict): continue
                versions += 1
                issues=_validate_version_portability(folder,manifest,version,root=library_root)
                if issues:
                    problems.append({"folder":folder.relative_to(library_root).as_posix(),"file":str(version.get("file") or ""),"issues":issues})
    result={"ok":not problems,"versions":versions,"problems":problems}
    if problems:
        log(f"asset-portability-validation-failed root={library_root} problems={json.dumps(problems[:20],sort_keys=True)}")
        if fail:
            raise RuntimeError(f"Asset library is not portable: {len(problems)} version(s) have missing/corrupt required files")
    else:
        log(f"asset-portability-validation-ok root={library_root} versions={versions}")
    return result

def _write_version_runtime_descriptor(folder: Path, manifest: dict[str, Any], version: dict[str, Any]) -> Path | None:
    """Write the portable UI/runtime description owned by one saved source version.

    This sidecar is deliberately data-only.  It records everything another CinterForge
    installation needs to reconstruct the saved asset UI and choose the same preview /
    animation / Minecraft behavior without contacting the original provider.
    """
    source_name = str(version.get("file") or "").strip()
    source = folder / source_name if source_name else None
    if source is None or not source.is_file():
        return None
    assigned, default = _portable_animation_references(manifest)
    artifacts = _normalize_version_artifacts(version)
    runtime_name = _runtime_descriptor_filename(version)
    artifacts["runtime"] = {"file": runtime_name, "kind": "cinterforge-runtime-descriptor", "required_for_ui": True}
    blockify = version.get("blockify") if isinstance(version.get("blockify"), dict) else {}
    payload = {
        "schema": "cinterforge.asset-runtime.v1",
        "source_file": source_name,
        "source_sha256": str(version.get("sha256") or "").strip().lower() or _sha256(source),
        "logical_name": str(manifest.get("name") or folder.name),
        "category": str(manifest.get("category") or ""),
        "subgroup": str(manifest.get("subgroup") or ""),
        "provider": str(version.get("provider") or ""),
        "kind": str(version.get("kind") or ""),
        "asset_id": str(version.get("asset_id") or ""),
        "search_query": str(version.get("search_query") or ""),
        "search_name": str(version.get("search_name") or ""),
        "metadata": version.get("metadata") if isinstance(version.get("metadata"), dict) else {},
        "inspection": version.get("inspection") if isinstance(version.get("inspection"), dict) else {},
        "animation": {
            "assigned": assigned,
            "default": default,
            "embedded": list((version.get("inspection") or {}).get("animation_names") or []) if isinstance(version.get("inspection"), dict) else [],
            "automatic_cycle": True if (assigned or ((version.get("inspection") or {}).get("animation_names") if isinstance(version.get("inspection"), dict) else [])) else False,
        },
        "blockify": blockify,
        "behavior_profile": list(blockify.get("behavior_profile") or []),
        "behavior_signature": str(blockify.get("behavior_signature") or ""),
        "artifacts": artifacts,
        "portable_files": _normalize_portable_files(version),
        "dependencies": _version_dependencies(manifest, version),
    }
    destination = folder / runtime_name
    temp = destination.with_suffix(destination.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp.replace(destination)
    artifacts["runtime"].update({"sha256": _sha256(destination), "size_bytes": destination.stat().st_size})
    version["artifacts"] = artifacts
    return destination


def register_version_artifact(relative_source_path: str, role: str, artifact_path: Path, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach an existing generated viewer/runtime file to one version's ownership."""
    role = str(role or "").strip()
    if role not in ARTIFACT_ROLES or role in {"source", "runtime"}:
        raise ValueError(f"Unsupported registerable asset artifact role: {role}")
    source = resolve_saved(relative_source_path)
    artifact = Path(artifact_path).resolve(); folder = source.parent.resolve()
    if folder not in artifact.parents or not artifact.is_file():
        raise ValueError("Runtime artifact must live beside its owned source version")
    manifest = _load_manifest(folder)
    version = next((v for v in manifest.get("versions") or [] if isinstance(v, dict) and str(v.get("file") or "") == source.name), None)
    if not isinstance(version, dict):
        raise ValueError(f"Saved asset version is not present in manifest: {relative_source_path}")
    artifacts = _normalize_version_artifacts(version)
    entry = {"file": artifact.name, "sha256": _sha256(artifact), "size_bytes": artifact.stat().st_size}
    if metadata:
        entry.update(dict(metadata))
    artifacts[role] = entry
    version["artifacts"] = artifacts
    _write_manifest(folder, manifest)
    log(f"asset-runtime-artifact-registered source={relative_source_path} role={role} artifact={artifact.name}")
    return dict(entry)


def _glb_json(path: Path) -> dict[str, Any]:
    if path.suffix.casefold() not in {".glb", ".gltf"}:
        return {}
    if path.suffix.casefold() == ".gltf":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    with path.open("rb") as fh:
        header = fh.read(12)
        if len(header) != 12 or header[:4] != b"glTF":
            return {}
        while True:
            chunk_header = fh.read(8)
            if len(chunk_header) < 8:
                break
            length = int.from_bytes(chunk_header[:4], "little")
            ctype = chunk_header[4:8]
            data = fh.read(length)
            if ctype == b"JSON":
                try:
                    return json.loads(data.rstrip(b"\x00 \t\r\n").decode("utf-8"))
                except Exception:
                    return {}
    return {}


def inspect_model(path: Path) -> dict[str, Any]:
    """Inspect glTF/GLB animation and humanoid skin structure conservatively."""
    doc = _glb_json(path)
    nodes = doc.get("nodes") if isinstance(doc.get("nodes"), list) else []
    skins = doc.get("skins") if isinstance(doc.get("skins"), list) else []
    authored = doc.get("animations") if isinstance(doc.get("animations"), list) else []
    # An empty glTF animation record is not a playable clip. Embedded motion
    # does not require a Mixamo or even a skinned rig: node animation counts.
    animations = [animation for animation in authored if isinstance(animation, dict)
                  and any(isinstance(channel, dict) and isinstance(channel.get("target"), dict)
                          and channel.get("target") for channel in animation.get("channels") or [])]
    raw_names = [str(n.get("name") or "") for n in nodes if isinstance(n, dict)]
    # Mirror CIFAssetBrowser.NormalizeRigName: FBX2glTF can retain hierarchy
    # and namespace prefixes (e.g. Armature|mixamorig:Hips). The Babylon
    # retargeter recognizes the terminal bone name, so inspection must too.
    def normalized_bone_name(raw: str) -> str:
        terminal = re.split(r"[|/\\]", raw.strip())[-1]
        terminal = terminal.split(":")[-1]
        terminal = re.sub(r"^mixamorig[_-]?", "", terminal, flags=re.IGNORECASE)
        return re.sub(r"[^a-z0-9]", "", terminal.casefold())

    names = {normalized_bone_name(raw) for raw in raw_names}
    markers = {
        "hips", "spine", "spine1", "spine2", "neck", "head",
        "leftshoulder", "rightshoulder", "leftarm", "rightarm",
        "leftforearm", "rightforearm", "lefthand", "righthand",
        "leftupleg", "rightupleg", "leftleg", "rightleg", "leftfoot", "rightfoot",
    }
    hits = sorted(markers & names)
    required = {"hips", "head", "leftarm", "rightarm", "leftupleg", "rightupleg"}
    skinned_joint_count = 0
    for skin in skins:
        if isinstance(skin, dict) and isinstance(skin.get("joints"), list):
            skinned_joint_count = max(skinned_joint_count, len(skin["joints"]))
    mixamo_compatible = bool(skins) and required.issubset(names) and len(hits) >= 10 and skinned_joint_count >= 12
    result = {
        "format": path.suffix.casefold().lstrip("."),
        "has_embedded_animations": bool(animations),
        "animation_count": len(animations),
        "animation_names": [str(animation.get("name") or f"Animation {index + 1}") for index, animation in enumerate(animations) if isinstance(animation, dict)],
        "has_skin": bool(skins),
        "skin_count": len(skins),
        "max_skin_joint_count": skinned_joint_count,
        "mixamo_compatible_rig": mixamo_compatible,
        "mixamo_bone_markers": hits,
    }
    log(f"model-inspection file={path.name} result={json.dumps(result, sort_keys=True)}")
    return result


def category_for(provider: str, kind: str, metadata: dict[str, Any], source_path: Path | None = None) -> tuple[str, str]:
    provider_key = str(provider or "").casefold()
    kind_key = str(kind or "").casefold()
    if kind_key == "image":
        decision = ("Images", "")
    elif kind_key == "animation":
        decision = ("Animations", "")
    elif provider_key == "mixamo" and kind_key == "3d":
        decision = ("Animated", "Mixamo")
    elif provider_key == "objaverse":
        detected = inspect_model(source_path) if source_path and source_path.is_file() else {}
        # A Mixamo-compatible rig is eligible for animation assignment during
        # import even if its source GLB has no embedded clips. Keep it in the
        # Animated library while the migration attaches a catalog animation.
        # Embedded clips qualify regardless of rig; an otherwise unanimated,
        # non-Mixamo model belongs in Objects.
        if detected.get("has_embedded_animations") or detected.get("mixamo_compatible_rig"):
            decision = ("Animated", "Mixamo" if detected.get("mixamo_compatible_rig") else "Fixed")
        else:
            decision = ("Objects", "")
    else:
        decision = ("Objects", "")
    log(f"classification provider={provider_key} kind={kind_key} source={source_path} category={decision[0]} subgroup={decision[1] or '-'}")
    return decision


def _folder_for(category: str, subgroup: str, name: str) -> Path:
    base = assets_root() / category
    if subgroup:
        base = base / subgroup
    folder = base / (name if re.fullmatch(r"CINTERASST[A-Za-z0-9#_-]{5,90}", name) else _safe_token(name))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _unique_version_path(folder: Path, base: str, suffix: str) -> Path:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    candidate = folder / f"{base}{suffix}"
    index = 2
    existing_names = {p.name.casefold() for p in folder.iterdir() if p.is_file()}
    while candidate.name.casefold() in existing_names:
        candidate = folder / f"{base}{index}{suffix}"
        index += 1
    return candidate


def _first_saved_animation() -> dict[str, str]:
    base = assets_root() / "Animations"
    if not base.is_dir(): return {}
    candidates: list[tuple[int, dict[str, str]]] = []
    for folder in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        try: manifest = _load_manifest(folder)
        except Exception: continue
        for version in manifest.get("versions") or []:
            filename = str(version.get("file") or "")
            if not filename.casefold().endswith(".fbx") or not (folder / filename).is_file():
                continue
            words = " ".join([folder.name, filename, str(version.get("search_query") or ""), str(version.get("metadata",{}).get("title") if isinstance(version.get("metadata"),dict) else "")]).casefold()
            priority = 0 if re.search(r"(^|[^a-z])idle([^a-z]|$)", words) else 1
            candidates.append((priority, {"folder": folder.relative_to(assets_root()).as_posix(), "file": filename, "name":"Idle" if priority == 0 else Path(filename).stem, "pose_frame": 0}))
    candidates.sort(key=lambda row:(row[0],row[1]["folder"].casefold(),row[1]["file"].casefold()))
    return candidates[0][1] if candidates else {}


def _assign_animation_to_unconfigured_mixamo_characters(animation: dict[str, str]) -> int:
    if not animation: return 0
    base = assets_root() / "Animated" / "Mixamo"; changed = 0
    if not base.is_dir(): return 0
    for folder in (p for p in base.iterdir() if p.is_dir()):
        try: manifest = _load_manifest(folder)
        except Exception: continue
        assigned=[dict(row) for row in list(manifest.get("assigned_animations") or []) if isinstance(row,dict)]
        if not assigned:
            assigned.append(dict(animation))
            manifest["assigned_animations"] = assigned
        if not manifest.get("default_animation"):
            manifest["default_animation"] = dict(animation)
        else:
            continue
        _write_manifest(folder, manifest); changed += 1
        log(f"default-animation-auto-assigned character={folder.relative_to(assets_root())} animation={animation['folder']}/{animation['file']} pose_frame=0")
    return changed


def find_saved_version(*, provider: str, kind: str, asset_id: str) -> dict[str, Any] | None:
    """Return an existing managed version for the exact provider/kind/id, if any."""
    provider_key=str(provider or "").casefold(); kind_key=str(kind or "").casefold(); asset_key=str(asset_id or "")
    root=assets_root()
    for manifest_path in root.glob("**/manifest.json"):
        folder=manifest_path.parent
        try: manifest=_load_manifest(folder)
        except Exception: continue
        for version in manifest.get("versions") or []:
            if str(version.get("provider") or "").casefold()!=provider_key: continue
            if str(version.get("kind") or "").casefold()!=kind_key: continue
            if str(version.get("asset_id") or "")!=asset_key: continue
            filename=str(version.get("file") or ""); path=folder/filename
            if not filename or not path.is_file(): continue
            rel=path.relative_to(root).as_posix()
            log(f"asset-already-saved provider={provider_key} kind={kind_key} id={asset_key} relative={rel}")
            return {"path":str(path),"relative_path":rel,"manifest":manifest,"version":version}
    return None


def _copy_source_dependencies(source_path: Path, destination_folder: Path, destination_stem: str) -> list[dict[str, Any]]:
    """Copy external files referenced by a .gltf so the saved source is portable.

    GLB/FBX/images are self-contained for our current import paths.  A textual
    glTF can reference arbitrary local buffers/textures, which must travel with
    the saved version or another machine cannot render it.
    """
    source_path=Path(source_path)
    if source_path.suffix.casefold() != ".gltf":
        return []
    try:
        doc=json.loads(source_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"gltf-dependency-parse-failed source={source_path} error={exc!r}")
        raise ValueError(f"Cannot save non-portable glTF; failed to parse {source_path.name}: {exc}") from exc
    uris: list[str]=[]
    for section in ("buffers","images"):
        for row in doc.get(section) or []:
            if not isinstance(row,dict): continue
            uri=str(row.get("uri") or "").strip()
            if not uri or uri.startswith("data:") or "://" in uri:
                continue
            uris.append(uri)
    result=[]; used=set()
    for index,uri in enumerate(dict.fromkeys(uris),1):
        src=(source_path.parent/uri).resolve()
        if source_path.parent.resolve() not in src.parents or not src.is_file():
            raise FileNotFoundError(f"glTF dependency is missing or outside its source folder: {uri}")
        suffix="".join(src.suffixes) or src.suffix or ".bin"
        candidate=destination_folder/f"{destination_stem}.dep{index}{suffix}"
        n=2
        while candidate.name.casefold() in used or candidate.exists():
            candidate=destination_folder/f"{destination_stem}.dep{index}_{n}{suffix}"; n+=1
        shutil.copy2(src,candidate); used.add(candidate.name.casefold())
        result.append({"role":"source_dependency","file":candidate.name,"uri":candidate.name,"original_uri":uri,"sha256":_sha256(candidate),"size_bytes":candidate.stat().st_size,"required_for_ui":True})
        log(f"asset-source-dependency-saved source={src} destination={candidate}")
    return result

def _rewrite_saved_gltf_dependencies(gltf_path: Path, portable_files: list[dict[str, Any]], *, previous: bool = False) -> None:
    if Path(gltf_path).suffix.casefold() != ".gltf" or not portable_files:
        return
    doc=json.loads(Path(gltf_path).read_text(encoding="utf-8"))
    mapping={}
    for row in portable_files:
        old=str(row.get("original_uri") if previous else row.get("original_uri") or row.get("uri") or "")
        new=str(row.get("file") or "")
        if old and new: mapping[old]=new
        uri=str(row.get("uri") or "")
        if uri and new: mapping[uri]=new
    changed=False
    for section in ("buffers","images"):
        for item in doc.get(section) or []:
            if not isinstance(item,dict): continue
            uri=str(item.get("uri") or "")
            if uri in mapping and mapping[uri] != uri:
                item["uri"]=mapping[uri]; changed=True
    if changed:
        Path(gltf_path).write_text(json.dumps(doc,ensure_ascii=False,separators=(",",":")),encoding="utf-8")


def save_version(*, source_path: Path, query: str, provider: str, kind: str, asset_id: str, metadata: dict[str, Any], thumbnail_path: Path | None = None, publish: bool = False, uci: str = "") -> dict[str, Any]:
    source_path = Path(source_path)
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise FileNotFoundError(f"Downloaded asset file is missing/empty: {source_path}")
    fallback = str(metadata.get("title") or metadata.get("name") or asset_id or "asset")
    name = search_name(query, fallback=fallback.split()[0] if fallback else "asset")
    if uci and not re.fullmatch(r"CINTERASST[A-Za-z0-9#_-]{5,90}", uci):
        raise ValueError("Invalid original CinterForge Asset UCI")
    category, subgroup = category_for("objaverse" if uci and provider.casefold() == "local" else provider, kind, metadata, source_path)
    # Local file imports keep a direct UCI-owned folder, not a provider
    # subgroup. A clipless Mixamo-compatible rig remains animatable while the
    # conversion job attaches its first clip; non-Mixamo rigs with no clips
    # remain Objects. Provider acquisitions keep their established layout.
    if uci and provider.casefold() == "local" and category == "Animated":
        subgroup = ""
    folder = _folder_for(category, subgroup, uci or name)
    suffix = source_path.suffix or (".fbx" if kind == "animation" else ".bin")
    if str(kind).casefold() == "animation" and str(provider).casefold() == "mixamo" and suffix.casefold() != ".fbx":
        raise ValueError(f"Mixamo animation persistence requires the original FBX; got {source_path.name}")
    dest = _unique_version_path(folder, source_path.stem if uci else name, suffix)
    shutil.copy2(source_path, dest)
    thumbnail_dest: Path | None = None
    if thumbnail_path is not None:
        thumbnail_path = Path(thumbnail_path)
        if thumbnail_path.is_file() and thumbnail_path.stat().st_size > 0:
            thumb_suffix = thumbnail_path.suffix.casefold() or ".jpg"
            if thumb_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                thumb_suffix = ".jpg"
            thumbnail_dest = folder / f"{dest.stem}.thumb{thumb_suffix}"
            shutil.copy2(thumbnail_path, thumbnail_dest)
            log(f"asset-thumbnail-saved provider={provider} id={asset_id} source={thumbnail_path} destination={thumbnail_dest}")
        else:
            log(f"asset-thumbnail-missing provider={provider} id={asset_id} requested={thumbnail_path}")
    digest = _sha256(dest)
    detected = inspect_model(dest) if str(kind).casefold() == "3d" else {}
    manifest = _load_manifest(folder)
    manifest.update({"name": name, "category": category, "subgroup": subgroup, "last_viewed": dest.name})
    if uci:
        old = str(manifest.get("uci") or "").strip()
        if old and old != uci:
            raise ValueError("Existing asset UCI mismatch")
        manifest["uci"] = uci
    version = {
        "file": dest.name,
        "sha256": digest,
        "size_bytes": dest.stat().st_size,
        "provider": provider,
        "kind": kind,
        "asset_id": asset_id,
        "search_query": query,
        "search_name": name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
    }
    if thumbnail_dest is not None:
        version["thumbnail_file"] = thumbnail_dest.name
    if str(provider).casefold() == "mixamo":
        version["mixamo_asset_type"] = "animation" if str(kind).casefold() == "animation" else "character"
        version["mixamo_compatible"] = True
    if detected:
        version["inspection"] = detected
    version["portable_files"] = _copy_source_dependencies(source_path, folder, dest.stem)
    if version["portable_files"] and dest.suffix.casefold()==".gltf":
        _rewrite_saved_gltf_dependencies(dest, version["portable_files"], previous=True)
        digest = _sha256(dest)
        version["sha256"] = digest
        version["size_bytes"] = dest.stat().st_size
    _normalize_version_artifacts(version)
    _normalize_portable_files(version)
    manifest.setdefault("versions", []).append(version)
    if uci:
        representations = manifest.setdefault("representations", {})
        if not representations.get("solid"):
            representations["solid"] = {"model": dest.name, "files": [row["file"] for row in version["portable_files"]]}
    if not manifest.get("default_version"):
        manifest["default_version"] = dest.name
    _write_manifest(folder, manifest)
    if str(provider).casefold() == "mixamo" and str(kind).casefold() == "animation":
        _assign_animation_to_unconfigured_mixamo_characters(
            {
                "folder": folder.relative_to(assets_root()).as_posix(),
                "file": dest.name,
                "name": str(metadata.get("title") or dest.stem),
                "pose_frame": 0,
            }
        )
    elif str(provider).casefold() == "mixamo" and str(kind).casefold() == "3d":
        default = _first_saved_animation()
        if default:
            manifest["assigned_animations"] = [dict(default)]
            manifest["default_animation"] = dict(default)
            _write_manifest(folder, manifest)
    # User acquisition is a Cloudflare commit; internal migration/tests may use
    # this persistence primitive without network publication.
    if publish:
        if str(provider).casefold() == "mixamo" and str(kind).casefold() == "animation":
            # Babylon cannot consume Mixamo FBX directly. Build the owned GLB
            # derivative as part of the acquisition commit so Cloudflare receives
            # source + runtime representation together.
            from .asset_library import saved_animation_preview_path
            saved_animation_preview_path(dest.relative_to(assets_root()).as_posix())
            manifest = _load_manifest(folder)
        _publish_managed_asset_folder(folder)
    log(f"asset-saved provider={provider} kind={kind} id={asset_id} query={query!r} source={source_path} destination={dest} sha256={digest}")
    return {"path": str(dest), "relative_path": dest.relative_to(assets_root()).as_posix(), "manifest": manifest}


def _uci_asset_folders(root: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    """Return canonical local UCI assets; folder location is representation storage, never identity."""
    library_root = (Path(root).resolve() if root else assets_root().resolve())
    rows: list[tuple[Path, dict[str, Any]]] = []
    if not library_root.is_dir():
        return rows
    for manifest_path in sorted(library_root.glob("**/CINTERASST*/manifest.json"), key=lambda path: path.as_posix().casefold()):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"uci-manifest-invalid path={manifest_path} error={exc!r}")
            continue
        uci = str(manifest.get("uci") or "").strip()
        if not uci or uci != manifest_path.parent.name:
            log(f"uci-manifest-identity-mismatch path={manifest_path} uci={uci!r}")
            continue
        rows.append((manifest_path.parent, manifest))
    return rows


def resolve_uci_asset(uci: str) -> dict[str, Any]:
    """Resolve one UCI to its current local representations without accepting a path as identity."""
    identity = str(uci or "").strip()
    if not identity:
        raise ValueError("asset UCI is required")
    matches = [(folder, manifest) for folder, manifest in _uci_asset_folders() if str(manifest.get("uci") or "") == identity]
    if not matches:
        raise FileNotFoundError(f"local UCI asset is missing: {identity} (searched {assets_root().resolve()})")
    if len(matches) != 1:
        raise RuntimeError(f"local UCI asset is ambiguous: {identity} ({len(matches)} copies)")
    folder, manifest = matches[0]
    representations = manifest.get("representations") if isinstance(manifest.get("representations"), dict) else {}
    solid = representations.get("solid") if isinstance(representations.get("solid"), dict) else {}
    block_rows: list[tuple[int, dict[str, Any]]] = []
    for key, value in representations.items():
        match = re.fullmatch(r"block_(\d+)", str(key))
        if match and isinstance(value, dict):
            block_rows.append((int(match.group(1)), value))
    block_rows.sort(key=lambda row: row[0], reverse=True)
    block = block_rows[0][1] if block_rows else {}
    root = assets_root().resolve()
    relative_folder = folder.relative_to(root).as_posix()
    def representation_path(meta: dict[str, Any], key: str) -> str:
        value = str(meta.get(key) or "").strip().replace("\\", "/")
        return f"{relative_folder}/{value}" if value else ""
    return {
        "uci": identity,
        "name": str(manifest.get("name") or identity),
        "category": str(manifest.get("category") or folder.parent.name),
        "folder": relative_folder,
        "manifest": manifest,
        "solid_model": representation_path(solid, "model"),
        "solid_thumbnail": representation_path(solid, "thumbnail"),
        "block_model": representation_path(block, "model"),
        "block_thumbnail": representation_path(block, "thumbnail"),
        "block_resolution": block_rows[0][0] if block_rows else 0,
    }


def list_local_uci_assets() -> dict[str, Any]:
    """Present the canonical local UCI library in the compatibility UI shape."""
    groups: dict[str, Any] = {"Images": [], "Objects": [], "Animated": {"Mixamo": [], "Fixed": []}, "Animations": []}
    root = assets_root().resolve()
    seen_ucis: set[str] = set()
    for folder, manifest in _uci_asset_folders(root):
        uci = str(manifest.get("uci") or "")
        if uci in seen_ucis:
            raise RuntimeError(f"Duplicate canonical asset UCI in local library: {uci}")
        seen_ucis.add(uci)
        # Use the already validated manifest and folder instead of rescanning
        # the entire library once for every asset (quadratic startup cost).
        representations = manifest.get("representations") or {}
        solid = representations.get("solid") or {}
        block_rows = sorted(
            (
                (int(match.group(1)), value)
                for key, value in representations.items()
                if (match := re.fullmatch(r"block_(\d+)", str(key)))
                and isinstance(value, dict)
            ),
            reverse=True,
        )
        block = block_rows[0][1] if block_rows else {}
        relative_folder = folder.relative_to(root).as_posix()

        def asset_path(representation: dict[str, Any], field: str) -> str:
            filename = str(representation.get(field) or "").strip().replace("\\", "/")
            return f"{relative_folder}/{filename}" if filename else ""

        resolved = {
            "uci": uci,
            "name": str(manifest.get("name") or uci),
            "folder": relative_folder,
            "solid_model": asset_path(solid, "model"),
            "solid_thumbnail": asset_path(solid, "thumbnail"),
            "block_model": asset_path(block, "model"),
            "block_resolution": block_rows[0][0] if block_rows else 0,
        }
        category = str(manifest.get("category") or folder.parent.name)
        model = resolved["solid_model"]
        thumbnail = resolved["solid_thumbnail"]
        relative_folder = resolved["folder"]
        filename = model[len(relative_folder) + 1:] if model.startswith(relative_folder + "/") else ""
        thumbnail_file = thumbnail[len(relative_folder) + 1:] if thumbnail.startswith(relative_folder + "/") else ""
        version = {
            "file": filename,
            "provider": "Local",
            "kind": "image" if category.casefold() == "images" else "3d",
            "uci": resolved["uci"],
            "metadata": {},
            "thumbnail_file": thumbnail_file,
            "blockify": {"model": resolved["block_model"], "resolution": resolved["block_resolution"]} if resolved["block_model"] else {},
        }
        raw_versions = manifest.get("versions")
        versions = [dict(v) for v in raw_versions if isinstance(v, dict)] if isinstance(raw_versions, list) else []
        if isinstance(raw_versions, list) and len(versions) != len(raw_versions):
            log(f"uci-inventory-skipped-invalid-version uci={uci} count={len(raw_versions) - len(versions)}")
        if not versions:
            versions = [version]
        # A migrated model and the original Terrain voxel JSON are both
        # canonical UCI-owned artifacts; neither should be fabricated from the
        # other. The existing browser accepts an owned file as its Blockify marker.
        block_model = str(block.get("model") or "")
        terrain_data = "block_16/blockify.json"
        has_terrain_data = (folder / terrain_data).is_file()
        for saved_version in versions:
            saved_block = dict(saved_version.get("blockify") or {})
            if block.get("blockbench_edited"):
                saved_block.update({"file": block.get("data"), "resolution": 16,
                                    "blockbench_edited": True, "source_up_normalized": True,
                                    "thumbnail": block.get("thumbnail")})
                saved_version["blockify"] = saved_block
            elif not saved_block.get("file"):
                if has_terrain_data:
                    saved_block.update({"file": terrain_data, "resolution": 16})
                elif block_model and (folder / block_model).is_file():
                    saved_block.update({"file": block_model,
                                        "model": resolved["block_model"],
                                        "resolution": resolved["block_resolution"]})
                if saved_block.get("file"):
                    saved_version["blockify"] = saved_block
        entry = {
            "folder": resolved["folder"], "name": resolved["name"], "uci": resolved["uci"],
            "category": category, "subgroup": str(manifest.get("subgroup") or ""),
            "versions": versions,
            "last_viewed": str(manifest.get("last_viewed") or filename),
            "default_version": manifest.get("default_version"),
            "default_animation": manifest.get("default_animation"),
            "assigned_animations": list(manifest.get("assigned_animations") or []),
            # A locally imported rig can be animated in its generated Solid
            # representation while preserving the user's original GLB bytes.
            "import_animation": manifest.get("import_animation"),
            "solid_model": resolved["solid_model"],
        }
        key = category.casefold()
        if key == "images": groups["Images"].append(entry)
        elif key in {"animated", "characters"}:
            groups["Animated"]["Mixamo" if entry["subgroup"].casefold()=="mixamo" else "Fixed"].append(entry)
        elif key in {"animations", "animation"}:
            groups["Animations"].append(entry)
        else:
            groups["Objects"].append(entry)
    return {"groups": groups, "authority": "local-uci", "read_only": False}

def list_saved() -> dict[str, Any]:
    """Return the canonical local UCI Asset Library.

    Browsing local authoring assets must not depend on Cloudflare. Push/Pull/Prune
    are the only synchronization boundaries; this inventory is strictly local.
    """
    return list_local_uci_assets()

def resolve_saved(relative_path: str) -> Path:
    """Resolve a portable asset path against the canonical local authoring Assets tree."""
    relative = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    if not relative or ".." in Path(relative).parts:
        raise ValueError(f"Invalid saved asset path: {relative_path}")
    root = assets_root().expanduser().resolve()
    candidate = (root / Path(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Saved asset path escapes Assets root: {relative_path}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate

def ensure_saved_thumbnail(relative_model_path: str, *, required_recipe: str = '') -> Path | None:
    """Return/create the persisted thumbnail for a saved asset version.

    Provider thumbnails are authoritative when available. This is intentionally
    lazy: it runs only when the UI asks for a particular saved asset preview,
    never while listing the whole library.
    """
    model = resolve_saved(relative_model_path)
    root = assets_root().resolve()
    folder = model.parent
    manifest = _load_manifest(folder)
    version = next((v for v in manifest.get("versions") or [] if str(v.get("file") or "") == model.name), None)
    if required_recipe and (not isinstance(version, dict) or str(version.get('thumbnail_recipe') or '') != required_recipe):
        # Old provider/T-pose thumbnails are not valid frame-zero captures.
        # Let the browser generate and persist the correct first animation frame.
        return None
    # Keep the established thumbnail cache for unanimated source models.
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = folder / f"{model.stem}.thumb{suffix}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    if not isinstance(version, dict):
        return None

    existing = str(version.get("thumbnail_file") or "").strip()
    if existing:
        candidate = folder / existing
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
        # The filesystem is authoritative. A manifest may outlive any file it
        # references, so clear stale thumbnail metadata before recovery.
        version.pop("thumbnail_file", None)
        _write_manifest(folder, manifest)
        log(
            f"manifest-stale-thumbnail folder={folder.relative_to(root).as_posix()} "
            f"file={existing!r} action=clear-before-recovery"
        )

    provider = str(version.get("provider") or "").casefold()
    asset_id = str(version.get("asset_id") or "").strip()
    if provider != "objaverse" or not asset_id:
        return None

    from . import objaverse
    provider_thumb = objaverse.thumbnail_path(asset_id)
    if provider_thumb is None or not provider_thumb.is_file() or provider_thumb.stat().st_size <= 0:
        return None

    suffix = provider_thumb.suffix.casefold() or ".jpg"
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    destination = folder / f"{model.stem}.thumb{suffix}"
    shutil.copy2(provider_thumb, destination)
    version["thumbnail_file"] = destination.name
    _write_manifest(folder, manifest)
    log(
        f"asset-thumbnail-recovered provider={provider} id={asset_id} "
        f"model={model.relative_to(root).as_posix()} thumbnail={destination.relative_to(root).as_posix()}"
    )
    return destination


def save_generated_thumbnail(relative_model_path: str, data_url: str, *, recipe: str = '') -> Path:
    """Persist a generated thumbnail for one saved asset version.

    This is the final fallback after a saved thumbnail is missing and the
    original provider cannot supply one. The local source file is authoritative;
    manifest metadata is updated only when a matching version already exists.
    """
    if recipe and recipe != 'animation-frame-zero-v1':
        raise ValueError('Unsupported saved thumbnail recipe')
    model = resolve_saved(relative_model_path)
    root = assets_root().resolve()
    folder = model.parent
    # A generated thumbnail belongs to the local source file, not to the
    # manifest. Newly acquired assets are allowed to receive thumbnails before
    # any catalog/manifest reconciliation occurs.
    manifest = _load_manifest(folder)
    version = next((v for v in manifest.get("versions") or [] if str(v.get("file") or "") == model.name), None)

    match = re.fullmatch(r"data:image/(jpeg|jpg|png|webp);base64,([A-Za-z0-9+/=\r\n]+)", str(data_url or "").strip(), re.IGNORECASE)
    if not match:
        raise ValueError("Generated thumbnail must be a base64 image data URL")
    raw = base64.b64decode(match.group(2), validate=False)
    if not raw:
        raise ValueError("Generated thumbnail is empty")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Generated thumbnail is too large")

    ext = match.group(1).casefold()
    suffix = ".jpg" if ext in {"jpeg", "jpg"} else f".{ext}"
    destination = folder / f"{model.stem}.thumb{suffix}"
    temp = destination.with_suffix(destination.suffix + ".tmp")
    temp.write_bytes(raw)
    temp.replace(destination)
    if isinstance(version, dict):
        version["thumbnail_file"] = destination.name
        if recipe:
            version['thumbnail_recipe'] = recipe
        else:
            version.pop('thumbnail_recipe', None)
        _write_manifest(folder, manifest)
    # Save owns local authored state. Generated thumbnails are local derivatives
    # until an explicit Push or Build publishes the Assets tree.
    log(
        f"asset-thumbnail-generated model={model.relative_to(root).as_posix()} "
        f"thumbnail={destination.relative_to(root).as_posix()} bytes={len(raw)} published=false"
    )
    return destination

def _resolve_folder(folder_rel: str) -> Path:
    root = assets_root().resolve()
    folder = (root / str(folder_rel or "")).resolve()
    if folder == root or root not in folder.parents:
        raise ValueError("Invalid asset folder")
    if not folder.is_dir():
        raise FileNotFoundError(folder_rel)
    return folder



# Blockify/Blockbench implementation lives in asset_blockify.  Re-export the
# established names here so existing callers keep the asset_management API.
from .asset_blockify import (
    _blockify_slug,
    _uuid_from_seed,
    _png_rgba,
    _blockify_front_icon_png,
    _blockify_fill_polygon,
    _blockify_hero_icon_png,
    _blockify_egg_icon_png,
    _animation_motion_kind,
    _clean_behavior_profile,
    _write_blockify_mcaddon,
    _png_dimensions,
    _write_image_mcaddon,
    save_image_minecraft,
    save_blockify,
    load_blockify,
    blockify_addon_is_current,
    regenerate_blockify_addon,
    _blockbench_resource_session,
    _blockbench_static_geometry_to_voxels,
    save_blockbench_override,
)

def mark_viewed(folder_rel: str, filename: str) -> dict[str, Any]:
    folder = _resolve_folder(folder_rel)
    if not (folder / filename).is_file():
        raise FileNotFoundError(filename)
    m = _load_manifest(folder)
    m["last_viewed"] = filename
    _write_manifest(folder, m)
    log(f"asset-viewed folder={folder_rel} file={filename}")
    return m


def delete_version(folder_rel: str, filename: str) -> dict[str, Any]:
    folder = _resolve_folder(folder_rel)
    target = (folder / filename).resolve()
    if folder not in target.parents:
        raise ValueError("Invalid asset filename")
    if not target.is_file():
        raise FileNotFoundError(filename)
    m = _load_manifest(folder)
    deleted_version = next((v for v in m.get("versions", []) if str(v.get("file") or "") == filename), {})
    owned = _owned_artifact_files(deleted_version) if isinstance(deleted_version, dict) else {"source": filename}
    for role, owned_name in owned.items():
        owned_path = (folder / owned_name).resolve()
        if folder in owned_path.parents and owned_path.is_file():
            owned_path.unlink()
            log(f"asset-owned-artifact-deleted folder={folder_rel} role={role} file={owned_name}")
    versions = [v for v in m.get("versions", []) if str(v.get("file") or "") != filename]
    m["versions"] = versions
    if m.get("last_viewed") == filename:
        m["last_viewed"] = str((versions[-1].get("file") if versions else "") or "")
    if m.get("default_version") == filename:
        m["default_version"] = str((versions[0].get("file") if versions else "") or "")
    if versions:
        _write_manifest(folder, m)
    else:
        shutil.rmtree(folder)
    log(f"asset-deleted folder={folder_rel} file={filename} remaining={len(versions)}")
    return {"deleted": filename, "remaining": len(versions)}



def delete_uci_asset(uci: str) -> dict[str, Any]:
    """Remove one canonical UCI and all its representations via Server ownership.

    A migration-only UCI has no saved-version entries, so delete_version() is
    insufficient. Resolve the UCI through the original library, refuse any
    ambiguous or symlinked folder, and preserve all other asset records.
    """
    resolved = resolve_uci_asset(uci)
    folder_rel = str(resolved['folder'])
    raw_folder = assets_root().resolve() / folder_rel
    if raw_folder.is_symlink() or any(p.is_symlink() for p in raw_folder.parents if p != raw_folder and assets_root().resolve() in p.parents):
        raise ValueError('Cannot delete a symlinked canonical asset')
    folder = _resolve_folder(folder_rel)
    manifest = _load_manifest(folder)
    if manifest.get('uci') != uci or folder.name != uci or folder.is_symlink():
        raise ValueError('Canonical asset identity does not match deletion target')
    versions = list(manifest.get('versions') or [])
    # Delete the complete verified canonical UCI folder as one operation. Migrated
    # representations can outlive saved version files: delete_version() refuses a
    # missing source file and previously left such assets impossible to remove.
    # The folder is already validated against its exact UCI above; never unlink
    # an arbitrary supplied path or traverse to a neighboring asset.
    shutil.rmtree(folder)
    if folder.exists():
        raise RuntimeError('Original Server did not remove UCI: ' + uci)
    log(f'asset-deleted-uci folder={folder_rel} versions={len(versions)}')
    return {'deleted': uci, 'versions': len(versions)}


def set_default_animation(folder_rel: str, animation_folder: str, animation_file: str) -> dict[str, Any]:
    folder = _resolve_folder(folder_rel)
    rel = folder.relative_to(assets_root().resolve()).as_posix()
    if not rel.startswith("Animated/Mixamo/"):
        raise ValueError("Default animations apply only to Mixamo-compatible characters")
    animation = resolve_saved(f"{animation_folder}/{animation_file}")
    if animation.suffix.casefold() != ".fbx":
        raise ValueError("Default animation must be an FBX file")
    m = _load_manifest(folder)
    assignment = _animation_reference(animation_folder, animation_file, pose_frame=0)
    assigned = [dict(row) for row in list(m.get("assigned_animations") or []) if isinstance(row, dict)]
    if not any(str(row.get("folder") or "") == animation_folder and str(row.get("file") or "") == animation_file for row in assigned):
        assigned.append(dict(assignment))
    m["assigned_animations"] = assigned
    m["default_animation"] = dict(assignment)
    _write_manifest(folder, m)
    log(f"default-animation-set character={folder_rel} animation={animation_folder}/{animation_file} pose_frame=0")
    return m


def add_animation_assignment(folder_rel: str, animation_folder: str, animation_file: str, *, name: str = "") -> dict[str, Any]:
    folder = _resolve_folder(folder_rel)
    rel = folder.relative_to(assets_root().resolve()).as_posix()
    if not rel.startswith("Animated/Mixamo/"):
        raise ValueError("Animation assignments apply only to Mixamo-compatible characters")
    animation = resolve_saved(f"{animation_folder}/{animation_file}")
    if animation.suffix.casefold() != ".fbx":
        raise ValueError("Assigned character animation must be an FBX file")
    m = _load_manifest(folder)
    assigned = [dict(row) for row in list(m.get("assigned_animations") or []) if isinstance(row, dict)]
    if not any(str(row.get("folder") or "") == animation_folder and str(row.get("file") or "") == animation_file for row in assigned):
        assigned.append(_animation_reference(animation_folder, animation_file, name=str(name or animation.stem), pose_frame=0))
    m["assigned_animations"] = assigned
    _write_manifest(folder, m)
    log(f"animation-assigned character={folder_rel} animation={animation_folder}/{animation_file} name={name!r}")
    return m


def remove_animation_assignment(folder_rel: str, animation_folder: str, animation_file: str) -> dict[str, Any]:
    folder = _resolve_folder(folder_rel)
    rel = folder.relative_to(assets_root().resolve()).as_posix()
    if not rel.startswith("Animated/Mixamo/"):
        raise ValueError("Animation assignments apply only to Mixamo-compatible characters")
    m = _load_manifest(folder)
    assigned = [dict(row) for row in list(m.get("assigned_animations") or []) if isinstance(row, dict)]
    default_animation = m.get("default_animation") if isinstance(m.get("default_animation"), dict) else {}
    is_default = str(default_animation.get("folder") or "") == animation_folder and str(default_animation.get("file") or "") == animation_file
    if is_default:
        raise ValueError("The default Idle animation cannot be removed")
    if len(assigned) <= 1:
        raise ValueError("A character must always keep at least one animation")
    remaining = [row for row in assigned if not (str(row.get("folder") or "") == animation_folder and str(row.get("file") or "") == animation_file)]
    if not remaining:
        raise ValueError("A character must always keep at least one animation")
    m["assigned_animations"] = remaining
    _write_manifest(folder, m)
    log(f"animation-unassigned character={folder_rel} animation={animation_folder}/{animation_file} remaining={len(remaining)}")
    return m


def set_embedded_animation_enabled(folder_rel: str, animation_name: str, enabled: bool) -> dict[str, Any]:
    folder = _resolve_folder(folder_rel)
    rel = folder.relative_to(assets_root().resolve()).as_posix()
    if not rel.startswith("Animated/Fixed/"):
        raise ValueError("Embedded animation selection applies only to animated Objaverse assets")
    if not enabled:
        raise ValueError("Embedded Objaverse animations are intrinsic to the asset and cannot be removed")
    m = _load_manifest(folder)
    m.pop("disabled_embedded_animations", None)
    _write_manifest(folder, m)
    return m


def _version_hash(version: dict[str, Any], folder: Path) -> str:
    value = str(version.get("sha256") or "").strip().lower()
    path = folder / str(version.get("file") or "")
    return value if value else (_sha256(path) if path.is_file() else "")


def _merge_folder(source: Path, destination: Path) -> dict[str, int]:
    destination.mkdir(parents=True, exist_ok=True)
    srcm = _load_manifest(source)
    dstm = _load_manifest(destination)
    existing_hashes = {_version_hash(v, destination) for v in dstm.get("versions", [])}
    existing_hashes.discard("")
    existing_names = {p.name.casefold() for p in destination.iterdir() if p.is_file()}
    merged = list(dstm.get("versions") or [])
    copied = skipped = renamed = 0
    for version in srcm.get("versions") or []:
        src = source / str(version.get("file") or "")
        if not src.is_file():
            log(f"merge-source-missing folder={source} file={version.get('file')}")
            continue
        digest = _version_hash(version, source)
        if digest and digest in existing_hashes:
            skipped += 1
            log(f"merge-deduplicated source={src} destination={destination} sha256={digest}")
            continue
        base, suffix = src.stem, src.suffix
        candidate = destination / src.name
        n = 2
        original_name = candidate.name
        while candidate.name.casefold() in existing_names:
            candidate = destination / f"{base}{n}{suffix}"
            n += 1
        if candidate.name != original_name:
            renamed += 1
            log(f"merge-collision-renamed source={src.name} destination={candidate.name} folder={destination}")
        shutil.copy2(src, candidate)
        existing_names.add(candidate.name.casefold())
        # Do not add this incoming hash to ``existing_hashes`` during the same
        # merge pass. Two separately saved versions can intentionally have
        # identical bytes while carrying different provider/search metadata;
        # a fresh setup must preserve both. ``existing_hashes`` represents only
        # versions that predated this merge, so rerunning setup still deduplicates.
        nv = dict(version)
        nv["file"] = candidate.name
        nv["sha256"] = digest or _sha256(candidate)
        nv["size_bytes"] = candidate.stat().st_size
        # Copy every generated/derived file owned by this exact source version.
        # Names are remapped on collision and both canonical ``artifacts`` and
        # compatibility fields are updated together.
        source_owned = _owned_artifact_files(version)
        copied_roles: dict[str, str] = {"source": candidate.name}
        for role, source_name in source_owned.items():
            if role == "source":
                continue
            source_artifact = (source / source_name).resolve()
            if source.resolve() not in source_artifact.parents or not source_artifact.is_file():
                log(f"merge-owned-artifact-missing source={source_artifact} role={role} asset={src}")
                continue
            suffixes = "".join(source_artifact.suffixes) or source_artifact.suffix
            if role == "thumbnail":
                ext = source_artifact.suffix.casefold() or ".jpg"
                preferred = f"{candidate.stem}.thumb{ext}"
            elif role == "blockify":
                preferred = f"{candidate.stem}.blockify.json"
            elif role == "addon":
                preferred = f"{candidate.stem}.mcaddon"
            elif role == "picture":
                preferred = f"{candidate.stem}.picture{source_artifact.suffix}"
            elif role.startswith("portable:"):
                preferred = f"{candidate.stem}.dep{len([k for k in copied_roles if k.startswith('portable:')])+1}{source_artifact.suffix}"
            else:
                preferred = f"{candidate.stem}.{role}{source_artifact.suffix}"
            artifact_candidate = destination / preferred
            n=2
            stem=artifact_candidate.stem; ext=artifact_candidate.suffix
            while artifact_candidate.name.casefold() in existing_names:
                if preferred.endswith('.blockify.json'):
                    artifact_candidate = destination / f"{candidate.stem}.blockify{n}.json"
                else:
                    artifact_candidate = destination / f"{stem}{n}{ext}"
                n += 1
            shutil.copy2(source_artifact, artifact_candidate)
            existing_names.add(artifact_candidate.name.casefold())
            copied_roles[role] = artifact_candidate.name
            log(f"merge-owned-artifact-copied role={role} source={source_artifact} destination={artifact_candidate}")
        if "thumbnail" in copied_roles:
            nv["thumbnail_file"] = copied_roles["thumbnail"]
        else:
            nv.pop("thumbnail_file", None)
        blockify = dict(nv.get("blockify") or {}) if isinstance(nv.get("blockify"),dict) else {}
        if "blockify" in copied_roles and "addon" in copied_roles:
            blockify["file"] = copied_roles["blockify"]; blockify["mcaddon_file"] = copied_roles["addon"]
            if "picture" in copied_roles:
                blockify["picture_file"] = copied_roles["picture"]
            nv["blockify"] = blockify
        else:
            nv.pop("blockify", None)
        artifacts = _normalize_version_artifacts(nv)
        # Preserve all portable runtime/representation metadata while remapping
        # any files copied under collision-safe names.  Runtime descriptors are
        # regenerated by _write_manifest so their contents always describe the
        # destination library rather than stale incoming paths.
        original_artifacts = version.get("artifacts") if isinstance(version.get("artifacts"),dict) else {}
        for role in ARTIFACT_ROLES:
            meta = original_artifacts.get(role)
            if not isinstance(meta, dict):
                continue
            updated = dict(meta)
            if role in copied_roles:
                updated["file"] = copied_roles[role]
            contained = str(updated.get("contained_in") or "")
            if contained and contained == source_owned.get("addon"):
                updated["contained_in"] = copied_roles.get("addon", contained)
            artifacts[role] = updated
        nv["artifacts"] = artifacts
        portable=[]
        for index, meta in enumerate(_normalize_portable_files(version)):
            row=dict(meta); key=f"portable:{index}:{row.get('role')}"
            if key in copied_roles:
                old_uri=str(row.get("uri") or row.get("file") or "")
                row["file"]=copied_roles[key]; row["uri"]=row["file"]; row["previous_uri"]=old_uri
                copied_path=destination/row["file"]
                row["sha256"]=_sha256(copied_path); row["size_bytes"]=copied_path.stat().st_size
                portable.append(row)
        nv["portable_files"] = portable
        if portable and candidate.suffix.casefold()==".gltf":
            doc=json.loads(candidate.read_text(encoding="utf-8")); changed_doc=False
            mapping={str(row.get("previous_uri") or ""):str(row.get("file") or "") for row in portable if row.get("previous_uri")}
            for section in ("buffers","images"):
                for item in doc.get(section) or []:
                    if isinstance(item,dict) and str(item.get("uri") or "") in mapping:
                        item["uri"]=mapping[str(item.get("uri") or "")]; changed_doc=True
            if changed_doc: candidate.write_text(json.dumps(doc,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
            for row in portable: row.pop("previous_uri",None)
            nv["sha256"]=_sha256(candidate); nv["size_bytes"]=candidate.stat().st_size
        merged.append(nv)
        copied += 1
    # Preserve local defaults/last-viewed if they exist. Official metadata fills blanks.
    for key, value in srcm.items():
        if key not in {"versions", "version_count", "last_viewed", "default_version", "default_animation", "updated_at"} and key not in dstm:
            dstm[key] = value
    dstm.setdefault("name", srcm.get("name") or destination.name)
    dstm.setdefault("category", srcm.get("category") or "")
    dstm.setdefault("subgroup", srcm.get("subgroup") or "")
    dstm["versions"] = merged
    if not dstm.get("last_viewed") and merged:
        dstm["last_viewed"] = str(merged[-1].get("file") or "")
    if not dstm.get("default_version") and merged:
        dstm["default_version"] = str(merged[0].get("file") or "")
    if not dstm.get("default_animation") and srcm.get("default_animation"):
        dstm["default_animation"] = srcm.get("default_animation")
    _write_manifest(destination, dstm)
    return {"copied": copied, "skipped": skipped, "renamed": renamed}


def _animation_dependency_index(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = Path(root).resolve() / "Animations"
    if not base.is_dir():
        return rows
    for manifest_path in sorted(base.rglob("manifest.json")):
        folder = manifest_path.parent
        try:
            manifest = _load_manifest(folder)
        except Exception:
            continue
        for version in manifest.get("versions") or []:
            if not isinstance(version, dict):
                continue
            filename = str(version.get("file") or "").strip()
            source = folder / filename
            if not filename or not source.is_file():
                continue
            rows.append({
                "folder": folder.relative_to(Path(root).resolve()).as_posix(),
                "file": filename,
                "sha256": str(version.get("sha256") or "").strip().lower() or _sha256(source),
                "provider": str(version.get("provider") or ""),
                "kind": str(version.get("kind") or ""),
                "asset_id": str(version.get("asset_id") or ""),
                "search_name": str(version.get("search_name") or ""),
                "name": str((version.get("metadata") or {}).get("title") if isinstance(version.get("metadata"), dict) else "") or str(manifest.get("name") or source.stem),
            })
    return rows



def local_animation_candidates() -> list[dict[str, str]]:
    """List playable Mixamo-compatible motion already saved in Builds/Assets.

    This is an authoring inventory, not the external Mixamo search index. A
    candidate must have a real local Babylon-loadable representation with
    animation tracks. No missing files are downloaded or provisioned.
    """
    root = assets_root().resolve()
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(folder: Path, filename: str, name: str, identity: str,
            *, expected_mixamo: bool = False) -> None:
        if not filename or not identity:
            return
        candidate = folder / filename
        if candidate.is_symlink():
            return
        source = candidate.resolve()
        try:
            relative = source.relative_to(root)
        except ValueError:
            return
        if source.is_symlink() or not source.is_file() or source.suffix.casefold() not in {".glb", ".gltf"}:
            return
        portable_path = relative.as_posix()
        if portable_path in seen:
            return
        inspection = inspect_model(source)
        if not inspection.get("has_embedded_animations"):
            return
        # Animation-only Mixamo GLBs may have no skin: they carry keyframes on
        # named humanoid transforms for retargeting to a separate character.
        # Match the existing Mixamo rig-name inspector rather than excluding
        # valid saved animation resources solely because they have no skin.
        markers = set(inspection.get("mixamo_bone_markers") or [])
        humanoid_motion_source = {"hips", "head", "leftarm", "rightarm", "leftupleg", "rightupleg"}.issubset(markers)
        if not expected_mixamo and not (inspection.get("mixamo_compatible_rig") or humanoid_motion_source):
            return
        seen.add(portable_path)
        candidates.append({
            "folder": relative.parent.as_posix(),
            "file": relative.name,
            "name": name or source.stem,
            "id": identity,
            "source": "local-assets",
        })

    # Saved animation versions can already be playable GLB/glTF, not only
    # Mixamo FBX with a .preview.glb derivative. Never require the retired
    # search database, and never run a converter inside the import request.
    for row in _animation_dependency_index(root):
        filename = str(row.get("file") or "")
        folder = root / str(row.get("folder") or "")
        extension = Path(filename).suffix.casefold()
        identity = str(row.get("asset_id") or row.get("sha256") or "")
        mixamo_source = str(row.get("provider") or "").casefold() == "mixamo"
        if extension in {".glb", ".gltf"}:
            add(folder, filename, str(row.get("name") or ""), identity,
                expected_mixamo=mixamo_source)
        elif extension == ".fbx" and mixamo_source:
            add(folder, Path(filename).with_suffix(".preview.glb").name,
                str(row.get("name") or ""), identity, expected_mixamo=True)

    # Legacy downloaded animation assets can have their portable .preview.glb
    # on disk without an accompanying manifest. Use the existing real files;
    # source FBX alone cannot be loaded by Babylon without conversion.
    animations_dir = root / "Animations"
    if animations_dir.is_dir():
        for source in sorted(animations_dir.rglob("*")):
            if source.suffix.casefold() not in {".glb", ".gltf"} or not source.is_file():
                continue
            add(source.parent, source.name, source.stem,
                source.relative_to(root).as_posix())

    # A saved character with embedded Mixamo-compatible animation is also a
    # valid motion source. Preserve its UCI and use its existing GLB/glTF;
    # never materialize an alternate copy of the asset.
    for folder, manifest in _uci_asset_folders(root):
        if str(manifest.get("category") or "").casefold() != "animated":
            continue
        identity = str(manifest.get("uci") or "")
        name = str(manifest.get("name") or folder.name)
        versions = manifest.get("versions")
        for version in versions if isinstance(versions, list) else []:
            if not isinstance(version, dict):
                continue
            filename = str(version.get("file") or "")
            if Path(filename).suffix.casefold() not in {".glb", ".gltf"}:
                continue
            add(folder, filename, name, identity)

        representations = manifest.get("representations")
        solid = representations.get("solid") if isinstance(representations, dict) else None
        if isinstance(solid, dict):
            filename = str(solid.get("model") or "")
            if Path(filename).suffix.casefold() in {".glb", ".gltf"}:
                add(folder, filename, name, identity)

    log(f"local-animation-candidates root={root} count={len(candidates)}")
    return candidates


def _repair_animation_dependencies(root: Path) -> int:
    """Repair character animation paths after Setup collision renames.

    Stable SHA/provider/asset-id metadata travels in the manifest, so path names
    are an optimization rather than the identity of the dependency.
    """
    root = Path(root).resolve()
    index = _animation_dependency_index(root)
    changed_count = 0
    if not index:
        return 0
    def resolve(raw: dict[str, Any]) -> dict[str, Any]:
        folder = str(raw.get("folder") or "")
        filename = str(raw.get("file") or "")
        candidate = root / folder / filename
        sha = str(raw.get("sha256") or "").strip().lower()
        provider = str(raw.get("provider") or "").casefold()
        asset_id = str(raw.get("asset_id") or "")
        if folder and filename and candidate.is_file():
            local = _animation_reference(folder, filename, name=str(raw.get("name") or ""), pose_frame=int(raw.get("pose_frame") or 0))
            local_sha = str(local.get("sha256") or "").strip().lower()
            local_provider = str(local.get("provider") or "").casefold()
            local_asset_id = str(local.get("asset_id") or "")
            identity_matches = (
                (not sha or local_sha == sha)
                and (
                    not provider
                    or not asset_id
                    or (local_provider == provider and local_asset_id == asset_id)
                )
            )
            if identity_matches:
                return {**raw, **local}
        matches = [row for row in index if sha and row["sha256"] == sha]
        if not matches and provider and asset_id:
            matches = [row for row in index if row["provider"].casefold() == provider and row["asset_id"] == asset_id]
        if not matches:
            return raw
        row = matches[0]
        return {
            **raw,
            **row,
            "pose_frame": int(raw.get("pose_frame") or 0),
            "name": str(
                raw.get("name")
                or row.get("name")
                or Path(row["file"]).stem
            ),
        }
    chars = root / "Animated" / "Mixamo"
    if not chars.is_dir():
        return 0
    for manifest_path in sorted(chars.rglob("manifest.json")):
        folder = manifest_path.parent
        try:
            manifest = _load_manifest(folder)
        except Exception:
            continue
        before = json.dumps(
            {
                "assigned": manifest.get("assigned_animations") or [],
                "default": manifest.get("default_animation") or {},
            },
            sort_keys=True,
            default=str,
        )
        manifest["assigned_animations"] = [
            resolve(dict(row))
            for row in manifest.get("assigned_animations") or []
            if isinstance(row, dict)
        ]
        if (
            isinstance(manifest.get("default_animation"), dict)
            and manifest.get("default_animation")
        ):
            manifest["default_animation"] = resolve(dict(manifest["default_animation"]))
        after = json.dumps(
            {
                "assigned": manifest.get("assigned_animations") or [],
                "default": manifest.get("default_animation") or {},
            },
            sort_keys=True,
            default=str,
        )
        if before != after:
            _write_manifest(folder, manifest)
            changed_count += 1
            log(f"animation-dependencies-repaired folder={folder.relative_to(root).as_posix()}")
    return changed_count


def merge_library(source_root: Path, destination_root: Path | None = None) -> dict[str, int]:
    source_root = Path(source_root).resolve()
    destination_root = (Path(destination_root).resolve() if destination_root else assets_root().resolve())
    totals = {"folders": 0, "copied": 0, "skipped": 0, "renamed": 0}
    if not source_root.is_dir():
        log(f"merge-library-no-source source={source_root}")
        return totals
    for manifest_path in sorted(source_root.rglob("manifest.json")):
        source = manifest_path.parent
        rel = source.relative_to(source_root)
        if not rel.parts or rel.parts[0] not in {"Images", "Objects", "Animated", "Animations"}:
            continue
        result = _merge_folder(source, destination_root / rel)
        totals["folders"] += 1
        for key in ("copied", "skipped", "renamed"):
            totals[key] += result[key]
    repaired = _repair_animation_dependencies(destination_root)
    if repaired:
        totals["animation_refs_repaired"] = repaired
    portability = validate_library_portability(destination_root, fail=False)
    totals["portable_versions"] = int(portability.get("versions") or 0)
    totals["portability_problems"] = len(portability.get("problems") or [])
    log(f"merge-library source={source_root} destination={destination_root} totals={totals}")
    return totals


def record_mixamo_test(folder_rel: str, *, success: bool, details: dict[str, Any]) -> dict[str, Any]:
    """Persist a real browser/Babylon retarget test result and promote only on success."""
    source = _resolve_folder(folder_rel)
    root = assets_root().resolve()
    if source.parent != root / "Animated" / "Fixed":
        raise ValueError("Only Fixed animated assets can be tested/promoted")
    payload = {"tested_at": datetime.now(timezone.utc).isoformat(), "success": bool(success), "details": details}
    m = _load_manifest(source)
    m["mixamo_test"] = payload
    _write_manifest(source, m)
    log(f"mixamo-test-result folder={folder_rel} success={success} details={json.dumps(details, sort_keys=True)}")
    if not success:
        return {"promoted": False, "message": "Mixamo retarget test failed; asset remains Fixed.", "test": payload}
    destination = root / "Animated" / "Mixamo" / source.name
    merge_result = _merge_folder(source, destination)
    shutil.rmtree(source)
    dm = _load_manifest(destination)
    dm["subgroup"] = "Mixamo"
    dm["mixamo_test"] = payload
    if not dm.get("default_animation"):
        animation = _first_saved_animation()
        if animation: dm["default_animation"] = animation
    _write_manifest(destination, dm)
    log(f"mixamo-promoted source={folder_rel} destination={destination.relative_to(root)} merge={merge_result}")
    return {"promoted": True, "folder": destination.relative_to(root).as_posix(), "test": payload, "merge": merge_result}


def promote_fixed(folder_rel: str) -> dict[str, Any]:
    """Compatibility shim: never promotes without an actual browser retarget test."""
    source = _resolve_folder(folder_rel)
    checks = []
    for version in _load_manifest(source).get("versions") or []:
        path = source / str(version.get("file") or "")
        checks.append({"file": path.name, "inspection": inspect_model(path) if path.is_file() else {}})
    log(f"mixamo-test-required folder={folder_rel} checks={json.dumps(checks, sort_keys=True)}")
    return {"promoted": False, "requires_runtime_test": True, "message": "Run the Babylon Mixamo animation test before promotion.", "checks": checks}





# Shared Assets sync runs in a background thread so the IFPlayer UI can poll
# status while Cloudflare transfers are active.
# Cloudflare transport, Push/Pull, and publication live in asset_transport.
# Re-export the established surface so existing callers remain unchanged.
from .asset_transport import (
    _set_sync_progress,
    sync_status,
    _sync_worker,
    start_sync_official,
    _folder_bytes,
    _build_transport_bundles,
    _safe_relative,
    AssetTransportContentMismatch,
    _extract_transport_bundles,
    _sync_state_path,
    _tree_snapshot,
    _load_sync_state,
    _official_transport_cache_path,
    _write_sync_state,
    _copy_tree_file,
    _remove_empty_parents,
    _reconcile_official,
    _config_path,
    _cloudflare_provider,
    _asset_provider_config,
    _cloudflare_prefix,
    public_asset_base_url,
    request_asset_url,
    _cloudflare_script,
    _run_cloudflare,
    asset_progress_output,
    _temporary_assets_root,
    _cloudflare_download_to,
    cloudflare_blockify_load,
    cloudflare_blockify_save,
    cloudflare_blockbench_session,
    cloudflare_blockbench_save,
    publish_cloudflare_asset_bundle,
    publish_cloudflare_file,
    publish_managed_asset_file,
    _publish_managed_asset_folder,
    _linux_simulated_cloudflare_root,
    _using_linux_cloudflare_simulation,
    _replace_simulated_cloudflare_from_local,
    download_official,
    pull_official,
    sync_official,
    _cloudflare_visual_catalog,
    cloudflare_visual_catalog_url,
    _authenticated_cloudflare_asset_keys,
    _visual_catalog_from_cloudflare_keys,
    load_cloudflare_visual_catalog,
    sync_cloudflare_visual_assets,
    publish_official,
)

from .asset_transport import (
    _BUNDLE_SCHEMA,
    _LEGACY_BUNDLE_SCHEMA,
    _BUNDLE_TARGET_BYTES,
)
from . import asset_transport as _asset_transport

# Keep monkeypatching/caller overrides on the historical asset_management facade
# effective while transport internals live in asset_transport.  This is also
# useful to embedders that replace roots/providers for isolated builds/tests.
def _transport_call(function_name: str, *args, **kwargs):
    compatibility_names = (
        "assets_root", "project_root", "log", "merge_library",
        "_authoritative_tree_snapshot", "_overlay_authoritative_tree", "_sha256",
        "_load_manifest", "_sync_state_path", "_run_cloudflare",
        "_using_linux_cloudflare_simulation", "_linux_simulated_cloudflare_root",
        "_write_sync_state", "_cloudflare_prefix", "_cloudflare_provider",
    )
    previous = {}
    module_globals = globals()
    for name in compatibility_names:
        if name in module_globals and hasattr(_asset_transport, name):
            previous[name] = getattr(_asset_transport, name)
            setattr(_asset_transport, name, module_globals[name])
    try:
        return getattr(_asset_transport, function_name)(*args, **kwargs)
    finally:
        for name, value in previous.items():
            setattr(_asset_transport, name, value)

def download_official(*args, **kwargs):
    return _transport_call("download_official", *args, **kwargs)

def pull_official(*args, **kwargs):
    return _transport_call("pull_official", *args, **kwargs)

def refresh_testing_grounds(*args, **kwargs):
    return _transport_call("refresh_testing_grounds", *args, **kwargs)

def refresh_uci_assets(*args, **kwargs):
    return _transport_call("refresh_uci_assets", *args, **kwargs)

def sync_official(*args, **kwargs):
    return _transport_call("sync_official", *args, **kwargs)

def publish_official(*args, **kwargs):
    return _transport_call("publish_official", *args, **kwargs)

def sync_cloudflare_visual_assets(*args, **kwargs):
    return _transport_call("sync_cloudflare_visual_assets", *args, **kwargs)

# ---------------------------------------------------------------------------
# Terrain assets
# ---------------------------------------------------------------------------

# Story-specific Terrain migration/runtime projection lives separately from the
# general asset store. Re-export these entry points to preserve the established
# asset_management API for existing callers.
from .asset_terrain import (
    ensure_story_terrain_assets,
    terrain_world_payload,
    ensure_all_story_terrain_assets,
)
