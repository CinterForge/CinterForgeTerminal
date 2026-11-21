from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Imported from the facade after its core persistence/Blockify/Cloudflare layers
# are initialized. Keeping Terrain isolated prevents story migration policy from
# being interleaved with the general asset store implementation.
from .asset_management import (
    _load_manifest, _safe_token, _sha256, _write_manifest,
    assets_root, log, save_blockify, save_version,
)

def ensure_story_terrain_assets(alias: str, story_id: str = "", title: str = "") -> list[dict[str, Any]]:
    """Migrate legacy story shell block models into the managed Assets library.

    This is intentionally generic: any STORY-* ``shell/*.blockify.json`` pair is
    promoted once.  Thereafter the managed saved asset is canonical and the
    legacy shell is only migration input.
    """
    alias_key = re.sub(r"[^a-z0-9]+", "", str(alias or "").casefold())
    from cf.ifserver.data.project_paths import story_root
    presentation_root = story_root()
    candidates=[]
    for project_dir in presentation_root.glob("STORY-*"):
        story_dir = project_dir / "Story"
        token=re.sub(r"[^a-z0-9]+", "", project_dir.name[6:].casefold())
        if alias_key and token != alias_key: continue
        shell=story_dir/"shell"
        if shell.is_dir(): candidates.extend(sorted(shell.glob("*.blockify.json")))
    if not candidates: return []
    from cf.ifserver.data import world_data as wd
    obj_path=wd.canonical_dir(alias)/"objects.json"
    payload=wd._read(obj_path) or {"schema":"cinterforge.if.world-data.v1","story_id":story_id,"story_alias":alias,"title":title,"kind":"objects","categories":[],"records":{}}
    cats=payload.get("categories") if isinstance(payload.get("categories"),list) else []
    if not any(isinstance(v,dict) and str(v.get("id") or "")=="terrain" for v in cats): cats.insert(0,{"id":"terrain","name":"Terrain"})
    payload["categories"]=cats; records=payload.setdefault("records",{})
    result=[]
    for block_path in candidates:
        stem=block_path.name[:-len('.blockify.json')]
        generated_rid=f"terrain:{re.sub(r'[^a-z0-9]+','-',stem.casefold()).strip('-')}"
        stem_key=re.sub(r"[^a-z0-9]+","",stem.casefold())
        stem_words=set(re.findall(r"[a-z]+",stem.casefold()))

        # A legacy shell can contain independently interactive architecture too.
        # Door/window models remain Objects even when they have a Blockify sidecar;
        # they must never join the Terrain editor or its shared section bounds.
        # Repair records promoted by older builds in place while preserving their
        # managed asset assignment.  This is generic by semantic object class, not
        # story-specific.
        if stem_words & {"door","window"}:
            repaired=False
            for existing_id,existing in list(records.items()):
                if not isinstance(existing,dict): continue
                name_words=set(re.findall(r"[a-z]+",str(existing.get("name") or existing_id).casefold()))
                if not (name_words & {"door","window"}): continue
                if str(existing.get("category") or "").casefold()=="terrain":
                    fixed=dict(existing); fixed["category"]="object"; fixed.pop("terrain_source",None)
                    records[existing_id]=fixed; repaired=True
                    log(f"terrain-migration-repaired-interactive-object alias={alias} id={existing_id} name={fixed.get('name','')}")
            if generated_rid in records and isinstance(records.get(generated_rid),dict):
                generated=records.get(generated_rid) or {}
                if str(generated.get("category") or "").casefold()=="terrain":
                    records.pop(generated_rid,None); repaired=True
                    log(f"terrain-migration-removed-interactive-duplicate alias={alias} id={generated_rid}")
            if repaired:
                result.extend(dict(v) for v in records.values() if isinstance(v,dict) and (set(re.findall(r"[a-z]+",str(v.get("name") or "").casefold())) & {"door","window"}))
            continue

        # A legacy shell is the 3D asset for the story's EXISTING semantic Terrain
        # object when one has the same name.  Do not create a second "Castle" object.
        # Older Terrain builds did create terrain:<stem>; collapse that duplicate here
        # while preserving its managed asset assignment.
        semantic_id=None
        for existing_id,existing in records.items():
            if str(existing_id)==generated_rid or not isinstance(existing,dict): continue
            name_key=re.sub(r"[^a-z0-9]+","",str(existing.get("name") or existing_id).casefold())
            if name_key==stem_key:
                semantic_id=str(existing_id); break
        duplicate=records.get(generated_rid) if isinstance(records.get(generated_rid),dict) else None
        rid=semantic_id or generated_rid
        rec=records.get(rid) if isinstance(records.get(rid),dict) else None
        if semantic_id and duplicate:
            rec=dict(rec or {})
            if not str(rec.get("asset3d") or "").startswith("Builds/Assets/") and str(duplicate.get("asset3d") or "").startswith("Builds/Assets/"):
                rec["asset3d"]=duplicate.get("asset3d")
                rec["assets"]=list(duplicate.get("assets") or [])
            # Promotion is a one-time migration. Once a user has moved an already
            # promoted record to another World category, reconciliation must preserve
            # that authored category instead of silently forcing it back to Terrain.
            if str(rec.get("terrain_source") or "") != "legacy_shell":
                rec["category"] = "terrain"
            rec["terrain_source"] = "legacy_shell"
            records[rid]=rec; records.pop(generated_rid,None)

        # Once assigned to a managed asset, never overwrite/re-import it.
        if rec and str(rec.get("asset3d") or "").startswith("Builds/Assets/"):
            managed=str(rec.get("asset3d") or "")[len("Builds/Assets/"):]
            if (assets_root()/managed).is_file():
                rec=dict(rec)
                if str(rec.get("terrain_source") or "") != "legacy_shell":
                    rec["category"] = "terrain"
                rec["terrain_source"] = "legacy_shell"
                # Clean up duplicate physical versions created by older Terrain migration
                # passes. The record's current asset3d is canonical; only versions with
                # the same bytes AND provider=cinterforge-terrain are eligible for prune.
                canonical_file=assets_root()/managed; managed_folder=canonical_file.parent
                manifest=_load_manifest(managed_folder); canonical_sha=_sha256(canonical_file)
                redundant=[v for v in (manifest.get("versions") or []) if isinstance(v,dict) and str(v.get("file") or "")!=canonical_file.name and str(v.get("sha256") or "")==canonical_sha and str(v.get("provider") or "").casefold()=="cinterforge-terrain"]
                if redundant:
                    manifest["versions"]=[v for v in (manifest.get("versions") or []) if v not in redundant]
                    for version in redundant:
                        stem0=Path(str(version.get("file") or "")).stem
                        for candidate in managed_folder.glob(stem0+".*"):
                            if candidate.is_file():
                                try: candidate.unlink()
                                except OSError: pass
                        log(f"terrain-migration-pruned-duplicate alias={alias} file={version.get('file','')} canonical={canonical_file.name}")
                    manifest["last_viewed"]=canonical_file.name; _write_manifest(managed_folder,manifest)
                # Terrain has one canonical primary 3D representation. Older migration
                # passes could leave both the legacy and managed Castle paths in assets,
                # which made the Assets browser truthfully display Castle (2). Collapse
                # those stale 3D assignments to the canonical asset3d here.
                other=[dict(a) for a in (rec.get("assets") or []) if isinstance(a,dict) and str(a.get("kind") or "").casefold()!="3d"]
                rec["assets"]=[{"kind":"3d","path":str(rec["asset3d"]),"label":stem.replace('_',' ').title(),"primary":True},*other]

                # A managed model alone is not a complete Terrain assignment.  Earlier
                # reconciliation returned here as soon as asset3d existed, even when the
                # current manifest version had lost/never received its Blockify artifact.
                # Map then rendered an empty Terrain row and Local Test failed at the
                # explicit voxel check.  Repair only that missing representation in place;
                # never re-import or rename the already-canonical model.
                current_version=next((v for v in (manifest.get("versions") or [])
                                      if isinstance(v,dict) and str(v.get("file") or "")==canonical_file.name),None)
                current_blockify=current_version.get("blockify") if isinstance(current_version,dict) and isinstance(current_version.get("blockify"),dict) else {}
                current_blockify_file=str(current_blockify.get("file") or "")
                if not current_blockify_file or not (managed_folder/current_blockify_file).is_file():
                    legacy=json.loads(block_path.read_text(encoding="utf-8"))
                    repaired_blockify=save_blockify(
                        str(managed_folder.relative_to(assets_root()).as_posix()),
                        canonical_file.name,
                        width=int(legacy.get("width") or 1),
                        height=int(legacy.get("height") or 1),
                        depth=int(legacy.get("depth") or 1),
                        resolution=int(legacy.get("resolution") or 16),
                        voxels=list(legacy.get("voxels") or []),
                        character_voxels=list(legacy.get("character_voxels") or []),
                        character_rig=legacy.get("character_rig") if isinstance(legacy.get("character_rig"),dict) else None,
                        character_animations=legacy.get("character_animations") if isinstance(legacy.get("character_animations"),dict) else None,
                        product_name=stem.replace('_',' ').title(),
                    )
                    log(f"terrain-migration-repaired-blockify alias={alias} asset=Builds/Assets/{managed} blockify={repaired_blockify.get('file','')}")

                records[rid]=rec; result.append(dict(rec)); continue
        source=block_path.with_name(stem+".glb")
        if not source.is_file(): source=block_path.with_name(stem+".gltf")
        if not source.is_file():
            log(f"terrain-migration-skip alias={alias} blockify={block_path} reason=source-model-missing"); continue

        # Migration is reconciliation, not an import operation. If this exact model is
        # already in the managed folder, reuse that version instead of calling
        # save_version() and manufacturing castle2/castle17/etc on every migration.
        managed_folder=assets_root()/"Objects"/_safe_token(stem)
        manifest=_load_manifest(managed_folder) if managed_folder.is_dir() else {}
        source_sha=_sha256(source)
        matching=[v for v in (manifest.get("versions") or []) if isinstance(v,dict) and str(v.get("sha256") or "")==source_sha and (managed_folder/str(v.get("file") or "")).is_file()]
        chosen=None
        if matching:
            default_name=str(manifest.get("default_version") or "")
            chosen=next((v for v in matching if str(v.get("file") or "")==default_name),matching[0])
            # Remove only redundant versions previously generated by Terrain migration.
            # User-saved versions are never collapsed merely because their bytes match.
            redundant=[v for v in matching if v is not chosen and str(v.get("provider") or "").casefold()=="cinterforge-terrain"]
            if redundant:
                keep=[]
                for version in (manifest.get("versions") or []):
                    if version not in redundant: keep.append(version); continue
                    filename0=str(version.get("file") or ""); stem0=Path(filename0).stem
                    for candidate in managed_folder.glob(stem0+".*"):
                        if candidate.is_file():
                            try: candidate.unlink()
                            except OSError: pass
                    log(f"terrain-migration-pruned-duplicate alias={alias} file={filename0} canonical={chosen.get('file','')}")
                manifest["versions"]=keep; manifest["version_count"]=len(keep); manifest["last_viewed"]=str(chosen.get("file") or ""); _write_manifest(managed_folder,manifest)
            rel=(managed_folder.relative_to(assets_root())/str(chosen.get("file") or "")).as_posix()
            log(f"terrain-migration-reused alias={alias} source={source} asset=Builds/Assets/{rel}")
        else:
            saved=save_version(source_path=source,query=stem,provider="cinterforge-terrain",kind="3d",asset_id=f"{alias}:{stem}",metadata={"title":stem.replace('_',' ').title(),"terrain":True,"story_alias":alias})
            rel=str(saved["relative_path"])
        folder_rel=str(Path(rel).parent.as_posix()); filename=Path(rel).name
        legacy=json.loads(block_path.read_text(encoding="utf-8"))
        # The managed Blockify artifact becomes canonical the instant Terrain is
        # migrated.  Re-running terrain reconciliation must never copy the legacy
        # shell voxels over user block placement/removal or Blockbench round trips.
        # This was the save/load loss: the GLB matched an existing managed version,
        # then migration unconditionally called save_blockify() with the old shell.
        current_manifest=_load_manifest(assets_root()/folder_rel)
        current_version=next((v for v in (current_manifest.get("versions") or []) if isinstance(v,dict) and str(v.get("file") or "")==filename),None)
        current_blockify=current_version.get("blockify") if isinstance(current_version,dict) and isinstance(current_version.get("blockify"),dict) else {}
        current_blockify_file=str(current_blockify.get("file") or "")
        if current_blockify_file and (assets_root()/folder_rel/current_blockify_file).is_file():
            meta=current_blockify
            log(f"terrain-migration-preserved-blockify alias={alias} asset=Builds/Assets/{rel} blockify={current_blockify_file}")
        else:
            meta=save_blockify(folder_rel,filename,width=int(legacy.get("width") or 1),height=int(legacy.get("height") or 1),depth=int(legacy.get("depth") or 1),resolution=int(legacy.get("resolution") or 16),voxels=list(legacy.get("voxels") or []),character_voxels=list(legacy.get("character_voxels") or []),character_rig=legacy.get("character_rig") if isinstance(legacy.get("character_rig"),dict) else None,character_animations=legacy.get("character_animations") if isinstance(legacy.get("character_animations"),dict) else None,product_name=stem.replace('_',' ').title())
        asset_path="Builds/Assets/"+rel
        if rec:
            rec=dict(rec)
            rec["asset3d"] = asset_path
            if str(rec.get("terrain_source") or "") != "legacy_shell":
                rec["category"] = "terrain"
            rec["terrain_source"] = "legacy_shell"
            assets=list(rec.get("assets") or []); assets=[a for a in assets if not (isinstance(a,dict) and str(a.get("kind") or "")=="3d")]
            assets.insert(0,{"kind":"3d","path":asset_path,"label":stem.replace('_',' ').title(),"primary":True}); rec["assets"]=assets
        else:
            rec={"id":rid,"name":stem.replace('_',' ').title(),"description":"World terrain","image":"","asset3d":asset_path,"assets":[{"kind":"3d","path":asset_path,"label":stem.replace('_',' ').title(),"primary":True}],"category":"terrain","tags":["terrain"],"terrain_source":"legacy_shell"}
        records[rid]=rec; result.append(dict(rec))
        log(f"terrain-migrated alias={alias} source={source} asset={asset_path} blockify={meta.get('file','')}")
    obj_path.parent.mkdir(parents=True,exist_ok=True); obj_path.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    editable=wd.editable_dir(alias)
    if editable:
        editable.mkdir(parents=True,exist_ok=True); (editable/"objects.json").write_text(json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    return result






def _canonical_blockify_data_path(resolved: dict[str, Any], resolution: int = 16) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Return the UCI-owned Blockify data path and representation, creating metadata only when needed."""
    folder = (assets_root() / str(resolved.get("folder") or "")).resolve()
    folder.relative_to(assets_root().resolve())
    manifest = resolved.get("manifest") if isinstance(resolved.get("manifest"), dict) else _load_manifest(folder)
    representations = manifest.setdefault("representations", {})
    key = f"block_{int(resolution)}"
    representation = representations.get(key)
    if not isinstance(representation, dict):
        representation = {"resolution": int(resolution)}
        representations[key] = representation
    # The representation key defines the canonical on-disk location.  Do not
    # make the data file discoverable only through mutable manifest metadata:
    # another manifest writer may legitimately refresh representation metadata
    # between save and load.  Identity + representation key is the authority.
    data_rel = f"{key}/blockify.json"
    representation["data"] = data_rel
    data_path = (folder / data_rel).resolve()
    data_path.relative_to(folder)
    return data_path, manifest, representation


def load_uci_blockify(uci: str) -> dict[str, Any]:
    """Load the canonical fixed-resolution Blockify voxel data for a UCI."""
    from cf.ifserver.assets.asset_management import resolve_uci_asset
    resolved = resolve_uci_asset(uci)

    # Bedrock/Terrain authoring has one canonical Blockify resolution: 16.
    # A stale higher-resolution representation entry in manifest.json must not
    # redirect a load away from the representation local-blockify-save writes.
    resolution = 16
    key = f"block_{resolution}"
    manifest = resolved.get("manifest") if isinstance(resolved.get("manifest"), dict) else {}
    representations = manifest.get("representations") if isinstance(manifest.get("representations"), dict) else {}
    representation = representations.get(key)
    if not isinstance(representation, dict):
        raise FileNotFoundError(f"local Blockify representation {key} is missing for UCI {resolved['uci']}")

    data_rel = f"{key}/blockify.json"
    declared_data = str(representation.get("data") or "").strip()
    if declared_data and declared_data != data_rel:
        raise ValueError(
            f"Blockify representation path is non-canonical for UCI {resolved['uci']}: "
            f"{declared_data!r} != {data_rel!r}"
        )
    folder = (assets_root() / str(resolved.get("folder") or "")).resolve()
    data_path = (folder / data_rel).resolve()
    data_path.relative_to(folder)
    if not data_path.is_file():
        raise FileNotFoundError(data_path)
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Blockify data is not an object: {data_path}")
    payload.setdefault("resolution", resolution)
    return payload


def save_uci_blockify(uci: str, blockify: dict[str, Any]) -> dict[str, Any]:
    """Persist Terrain voxel data under its canonical UCI representation.

    The UCI manifest is the authority.  This deliberately does not call the retired
    per-version ``save_blockify`` path, which expects ``manifest.versions``.
    """
    from cf.ifserver.assets.asset_management import resolve_uci_asset
    resolved = resolve_uci_asset(uci)
    payload = dict(blockify or {})
    resolution = int(payload.get("resolution") or 16)
    if resolution != 16:
        raise ValueError(f"Blockify resolution is fixed at 16 for Bedrock voxel space, got {resolution}")
    data_path, manifest, representation = _canonical_blockify_data_path(resolved, resolution)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = data_path.with_name(data_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    temporary.replace(data_path)
    representation["data_sha256"] = _sha256(data_path)
    representation["voxel_count"] = len(payload.get("voxels") or [])
    representation["resolution"] = resolution
    _write_manifest((assets_root() / str(resolved.get("folder") or "")).resolve(), manifest)
    log(f"terrain-blockify-saved uci={uci} data={data_path.relative_to(assets_root()).as_posix()} voxels={representation['voxel_count']}")
    return payload

def terrain_world_payload(alias: str, story_id: str = "", title: str = "") -> list[dict[str, Any]]:
    """Return every World object currently categorized as Terrain.

    Terrain is a presentation category, not a separate asset identity model.  Each
    row keeps the World record id and resolves its primary 3D UCI through the same
    managed Assets library used by every other object.  Map placement is persisted
    separately by Terrain object id in map_shell.json.
    """
    from cf.ifserver.data import world_data as wd
    from cf.ifserver.assets.asset_management import resolve_uci_asset

    objects = wd.load_dataset(alias, "objects") or {}
    rows: list[dict[str, Any]] = []
    model_kinds = {"3d", "object", "model", "character", "animated", "animated3d"}
    legacy_uci_by_path: dict[str, str] = {}
    world_data_changed = False

    for rid, rec in (objects.get("records") or {}).items():
        if not isinstance(rec, dict) or str(rec.get("category") or "").casefold() != "terrain":
            continue

        asset_uci = str(rec.get("asset3d_uci") or "").strip()
        if not asset_uci:
            assignments = rec.get("assets") if isinstance(rec.get("assets"), list) else []
            primary = next((row for row in assignments if isinstance(row, dict)
                            and str(row.get("kind") or "").casefold() in model_kinds
                            and bool(row.get("primary"))), None)
            if primary is None:
                primary = next((row for row in assignments if isinstance(row, dict)
                                and str(row.get("kind") or "").casefold() in model_kinds), None)
            asset_uci = str((primary or {}).get("uci") or "").strip()

        # Pre-UCI WorldData stored an Assets-relative path. Resolve that path through
        # the durable migration table once, then immediately persist the UCI back into
        # this ordinary World object. Terrain does not retain a path-based identity.
        if not asset_uci:
            candidate_paths: list[str] = []
            if isinstance(primary, dict):
                candidate_paths.append(str(primary.get("path") or ""))
            candidate_paths.append(str(rec.get("asset3d") or ""))
            for assignment in rec.get("assets") if isinstance(rec.get("assets"), list) else []:
                if isinstance(assignment, dict) and str(assignment.get("kind") or "").casefold() in model_kinds:
                    candidate_paths.append(str(assignment.get("path") or ""))
            for candidate in candidate_paths:
                normalized = candidate.replace("\\", "/").strip().lstrip("/")
                prefix = "Builds/Assets/"
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix):]
                resolved_uci = legacy_uci_by_path.get(normalized.casefold())
                if resolved_uci:
                    asset_uci = resolved_uci
                    rec["asset3d_uci"] = resolved_uci
                    if isinstance(primary, dict):
                        primary["uci"] = resolved_uci
                    world_data_changed = True
                    log(f"terrain-worlddata-uci-migrated alias={alias} id={rid} source={normalized} uci={resolved_uci}")
                    break

        # Compatibility for old WorldData that still carries a managed relative path.
        # Resolve that path only to discover its UCI; the returned Terrain identity is
        # always UCI and all subsequent operations use UCI.
        legacy_asset = str(rec.get("asset3d") or "").replace("\\", "/").strip()
        row = {
            "id": str(rid),
            "name": str(rec.get("name") or rid),
            "category": "terrain",
            "uci": asset_uci,
            "blockify": None,
        }

        try:
            if not asset_uci and legacy_asset:
                rel = legacy_asset
                prefix = "Builds/Assets/"
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                rel = rel.lstrip("/")
                if ".." not in Path(rel).parts and rel.split("/", 1)[0] in {"Objects", "Animated"}:
                    managed_folder = (assets_root() / Path(rel).parent).resolve()
                    managed_folder.relative_to(assets_root().resolve())
                    manifest = _load_manifest(managed_folder)
                    asset_uci = str(manifest.get("uci") or "").strip()
                    row["uci"] = asset_uci
            if not asset_uci:
                raise ValueError(f"Terrain object has no primary 3D UCI: {rid}")

            resolved = resolve_uci_asset(asset_uci)
            row["name"] = str(rec.get("name") or resolved.get("name") or rid)
            row["folder"] = str(resolved.get("folder") or "")
            # Map/Test Terrain is Blockify-only. Never substitute the solid/original
            # representation: those models are too heavy for the live authoring world.
            model_rel = str(resolved.get("block_model") or "").strip()
            if not model_rel:
                raise ValueError(f"Terrain UCI has no canonical Blockify model: {asset_uci}")
            row["asset3d"] = model_rel
            row["file"] = Path(model_rel).name

            manifest = resolved.get("manifest") if isinstance(resolved.get("manifest"), dict) else {}
            representations = manifest.get("representations") if isinstance(manifest.get("representations"), dict) else {}
            block_rows = []
            for key, value in representations.items():
                match = re.fullmatch(r"block_(\d+)", str(key))
                if match and isinstance(value, dict):
                    block_rows.append((int(match.group(1)), value))
            block_rows.sort(key=lambda item: item[0], reverse=True)
            block_meta = block_rows[0][1] if block_rows else {}
            block_file = str(block_meta.get("data") or block_meta.get("blockify") or block_meta.get("json") or "").strip()

            # Older manifests attach Blockify to the current version rather than the
            # representations table.  This is representation compatibility, not
            # Terrain identity fallback.
            if not block_file:
                versions = manifest.get("versions") if isinstance(manifest.get("versions"), list) else []
                current_name = Path(model_rel).name if model_rel else ""
                version = next((v for v in versions if isinstance(v, dict) and str(v.get("file") or "") == current_name), None)
                if version is None and versions:
                    version = next((v for v in versions if isinstance(v, dict) and isinstance(v.get("blockify"), dict)), None)
                meta = version.get("blockify") if isinstance(version, dict) and isinstance(version.get("blockify"), dict) else {}
                block_file = str(meta.get("file") or "").strip()

            representation_folder = (assets_root() / model_rel).resolve().parent
            asset_folder = (assets_root() / str(resolved.get("folder") or "")).resolve()
            representation_folder.relative_to(asset_folder)

            block_path = None
            if block_file:
                candidate = (asset_folder / block_file).resolve()
                candidate.relative_to(asset_folder)
                if candidate.is_file():
                    block_path = candidate

            # The canonical block_<resolution> representation owns its Blockify
            # payload beside the model. Some current manifests name only the model
            # in representations; that must not make Terrain fall through to GLTF
            # materials when the canonical payload is already present. This is not
            # legacy Story/shell migration or a solid-model fallback.
            if block_path is None:
                candidate = representation_folder / "blockify.json"
                if candidate.is_file():
                    block_path = candidate

            if block_path is None and str(rec.get("terrain_source") or "") == "legacy_shell":
                # One-way legacy migration. Canonical UCI Blockify is always read
                # first. Only if no canonical Blockify payload exists anywhere do we
                # consult Story/shell, and then we seed the canonical representation
                # exactly once. Existing canonical data is never overwritten.
                from cf.ifserver.data.project_paths import project_root

                # A canonical payload can legitimately live in a different block_*
                # representation from the currently selected preview/model GLTF.
                # Search every managed representation before touching legacy data.
                for resolution, representation in block_rows:
                    canonical_candidate = asset_folder / f"block_{resolution}" / "blockify.json"
                    declared = str(
                        representation.get("data")
                        or representation.get("blockify")
                        or representation.get("json")
                        or ""
                    ).strip()
                    candidates = [canonical_candidate]
                    if declared:
                        declared_candidate = (asset_folder / declared).resolve()
                        declared_candidate.relative_to(asset_folder)
                        if declared_candidate not in candidates:
                            candidates.insert(0, declared_candidate)
                    block_path = next((candidate for candidate in candidates if candidate.is_file()), None)
                    if block_path is not None:
                        break

                if block_path is None:
                    shell = project_root(alias) / "Story" / "shell"
                    terrain_name = str(rec.get("name") or rid).strip()
                    candidate_names = [
                        f"{terrain_name}.blockify.json",
                        f"{re.sub(r'[^a-z0-9]+', '-', terrain_name.casefold()).strip('-')}.blockify.json",
                    ]
                    legacy_path = next(
                        (shell / candidate_name for candidate_name in candidate_names
                         if (shell / candidate_name).is_file()),
                        None,
                    )
                    if legacy_path is not None:
                        legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
                        if not isinstance(legacy_payload, dict):
                            raise ValueError(f"legacy Terrain Blockify data is not an object: {legacy_path}")

                        resolution = int(legacy_payload.get("resolution") or 16)
                        canonical_path, canonical_manifest, canonical_representation = _canonical_blockify_data_path(
                            resolved,
                            resolution,
                        )
                        if not canonical_path.is_file():
                            canonical_path.parent.mkdir(parents=True, exist_ok=True)
                            temporary_path = canonical_path.with_name(canonical_path.name + ".tmp")
                            temporary_path.write_text(
                                json.dumps(legacy_payload, separators=(",", ":"), ensure_ascii=False),
                                encoding="utf-8",
                            )
                            # Re-check immediately before promotion. Never replace an
                            # existing canonical payload, even during migration.
                            if canonical_path.is_file():
                                temporary_path.unlink(missing_ok=True)
                            else:
                                temporary_path.replace(canonical_path)

                            canonical_representation["data_sha256"] = _sha256(canonical_path)
                            canonical_representation["voxel_count"] = len(legacy_payload.get("voxels") or [])
                            canonical_representation["resolution"] = resolution
                            _write_manifest(asset_folder, canonical_manifest)
                            log(
                                f"terrain-legacy-blockify-migrated alias={alias} id={rid} "
                                f"uci={asset_uci} source={legacy_path.relative_to(project_root(alias))} "
                                f"target={canonical_path.relative_to(assets_root()).as_posix()}"
                            )
                        else:
                            log(
                                f"terrain-canonical-blockify-preserved alias={alias} id={rid} "
                                f"uci={asset_uci} data={canonical_path.relative_to(assets_root()).as_posix()}"
                            )
                        block_path = canonical_path

            if block_path is None:
                raise FileNotFoundError(
                    f"Terrain UCI canonical Blockify representation has no blockify.json: {asset_uci}"
                )

            row["blockify"] = json.loads(block_path.read_text(encoding="utf-8"))
        except Exception as exc:
            row["error"] = str(exc)
        rows.append(row)

    if world_data_changed:
        target = wd.editable_dir(alias, story_id) or wd.canonical_dir(alias)
        target.mkdir(parents=True, exist_ok=True)
        (target / "objects.json").write_text(
            json.dumps(objects, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        log(f"terrain-worlddata-uci-save alias={alias} path={target / 'objects.json'}")
    return rows

def ensure_all_story_terrain_assets() -> None:
    """Promote any legacy STORY shell assets before Assets assignment browsing."""
    from cf.ifserver.data.world_data import story_alias
    from cf.ifserver.data.project_paths import story_root
    root=story_root()
    for story_dir in root.glob("STORY-*"):
        if not (story_dir/"shell").is_dir(): continue
        sid=story_dir.name; alias=story_alias(sid,sid[6:])
        ensure_story_terrain_assets(alias,sid,sid[6:])
        from cf.ifserver.data import world_data as wd
        wd.migrate_terrain_categories(alias)
