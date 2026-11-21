# Asset migration/build/gallery command family extracted from asset_command.py.
#
# These operations perform explicit library migration and generated-product work.
# Keeping them here prevents the browser command dispatcher from becoming a second
# asset-management implementation.

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

from .terminal_paths import cinterforgeBuildsRootPath

_HTML_ESCAPE = html.escape

# Generated Blockify runtime GLTF recipe. Bump when runtime geometry/export changes.
BLOCKIFY_RUNTIME_RECIPE = "bedrock16-batched-static-humanoid-presentation-v11"
BLOCKIFY_RUNTIME_RECIPE_FINGERPRINT = hashlib.sha256(BLOCKIFY_RUNTIME_RECIPE.encode("utf-8")).hexdigest()


# Purpose: Internal helper for ok; keep this implementation detail behind the module’s public/domain boundary.
def _ok(payload: Any = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True}
    if isinstance(payload, dict):
        result.update(payload)
    elif payload is not None:
        result["result"] = payload
    result.update(extra)
    return result


# Purpose: Internal helper for log; keep this implementation detail behind the module’s public/domain boundary.
def _log(event: str, **fields: Any) -> None:
    detail = " ".join(f"{key}={value!r}" for key, value in fields.items())
    print(f"[ifplayer assets command] {event}{(' ' + detail) if detail else ''}", flush=True)


# Purpose: Internal helper for migration asset category; keep this implementation detail behind the module’s public/domain boundary.
def _migration_asset_category(source_path: str) -> str:
    parts = {part.casefold() for part in Path(str(source_path or "").replace("\\", "/")).parts}
    if "mixamo" in parts:
        return "character"
    if "fixed" in parts:
        return "creature"
    return "object"


# Purpose: Internal helper for stage block preview; keep this implementation detail behind the module’s public/domain boundary.
def _stage_block_preview(block_folder: Path, staged_folder: Path, asset_name: str) -> str:
    preview_folder = staged_folder / "preview"
    if preview_folder.exists():
        shutil.rmtree(preview_folder)
    shutil.copytree(block_folder, preview_folder)
    preview_model = preview_folder / f"{asset_name}_block.gltf"
    if not preview_model.is_file():
        raise FileNotFoundError(f"Block preview GLTF is missing: {preview_model}")
    return preview_model.relative_to(staged_folder).as_posix()


# Purpose: Internal helper for is asset migration source; keep this implementation detail behind the module’s public/domain boundary.
def _is_asset_migration_source(relative_path: Path) -> bool:
    from .asset_command_jobs import _is_asset_migration_source as implementation
    return implementation(relative_path)


# Purpose: Public/domain operation for execute asset migration command; callers should use this boundary instead of duplicating its filesystem or provider logic.


def _manifest_representation_files_exist(destination: Path, representation: dict[str, Any]) -> bool:
    """Return true only when every canonical artifact declared by a representation exists."""
    declared_paths: list[str] = []
    for key in ("model", "thumbnail", "edits", "minecraft"):
        value = str(representation.get(key) or "").strip()
        if value:
            declared_paths.append(value)
    for value in representation.get("files") or []:
        relative_path = str(value or "").strip()
        if relative_path:
            declared_paths.append(relative_path)

    if not declared_paths:
        return False

    destination = Path(destination).resolve()
    for relative_path in declared_paths:
        candidate = (destination / relative_path).resolve()
        try:
            candidate.relative_to(destination)
        except ValueError:
            return False
        if not candidate.is_file():
            return False
    return True


def _asset_migration_destination_status(
    destination: Path,
    resolution: int,
) -> dict[str, Any]:
    """Explain exactly why a canonical migration destination is reused or regenerated."""
    destination = Path(destination).resolve()
    manifest_path = destination / "manifest.json"
    status: dict[str, Any] = {
        "current": False,
        "recipe_current": False,
        "complete": False,
        "reason": "unknown",
        "missing": [],
    }

    def representation_missing(representation: dict[str, Any]) -> list[str]:
        declared_paths: list[str] = []
        for key in ("model", "thumbnail", "edits", "minecraft"):
            value = str(representation.get(key) or "").strip()
            if value:
                declared_paths.append(value)
        for value in representation.get("files") or []:
            relative_path = str(value or "").strip()
            if relative_path:
                declared_paths.append(relative_path)
        if not declared_paths:
            return ["<no declared canonical artifacts>"]
        missing: list[str] = []
        for relative_path in declared_paths:
            candidate = (destination / relative_path).resolve()
            try:
                candidate.relative_to(destination)
            except ValueError:
                missing.append(f"<outside asset folder>:{relative_path}")
                continue
            if not candidate.is_file():
                missing.append(relative_path)
        return missing

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status["reason"] = "manifest-missing"
        status["missing"] = ["manifest.json"]
        return status
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        status["reason"] = f"manifest-unreadable:{type(error).__name__}"
        return status

    representations = manifest.get("representations") or {}
    solid = representations.get("solid") or {}
    solid_missing = representation_missing(solid)
    if solid_missing:
        status["reason"] = "solid-incomplete"
        status["missing"] = solid_missing
        return status

    if int(resolution) == 0:
        status.update(current=True, recipe_current=True, complete=True, reason="current-solid-only")
        return status

    block_key = f"block_{int(resolution)}"
    block = representations.get(block_key) or {}
    stored_recipe = str(block.get("runtime_recipe") or "").strip()
    stored_fingerprint = str(block.get("runtime_recipe_fingerprint") or "").strip()
    recipe_is_current = (
        stored_recipe == BLOCKIFY_RUNTIME_RECIPE
        or stored_fingerprint == BLOCKIFY_RUNTIME_RECIPE_FINGERPRINT
    )
    status["recipe_current"] = recipe_is_current
    status["stored_recipe"] = stored_recipe or "<missing>"
    status["expected_recipe"] = BLOCKIFY_RUNTIME_RECIPE
    if not recipe_is_current:
        status["reason"] = "recipe-stale"
        return status

    block_missing = representation_missing(block)
    if block_missing:
        status["reason"] = "block-incomplete"
        status["missing"] = block_missing
        return status

    status.update(current=True, complete=True, reason="current-and-complete")
    return status


def _asset_migration_destination_is_current_and_complete(
    destination: Path,
    resolution: int,
) -> bool:
    return bool(_asset_migration_destination_status(destination, resolution).get("current"))

def execute_asset_migration_command(operation: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if operation == "asset-migration-block-thumbnail":
            import base64
            from runtime.assets.asset_management import _blockify_hero_icon_png
            voxels = [row for row in list(body.get("voxels") or []) if isinstance(row, dict)]
            if not voxels:
                raise ValueError("migration block thumbnail requires voxels")
            png = _blockify_hero_icon_png(voxels, size=512, transparent=False)
            return _ok(data_url="data:image/png;base64," + base64.b64encode(png).decode("ascii"))


        if operation in {"asset-migration-save", "asset-migration-save-proof"}:
            import base64
            from runtime.assets.asset_management import assets_root
            from runtime.assets.asset_management import _write_blockify_mcaddon

            top_level = str(body.get("top_level") or "").strip()
            uci = str(body.get("uci") or "").strip()
            source_path = str(body.get("source_path") or "").strip().replace("\\", "/")
            name = str(body.get("name") or "").strip()
            minecraft_name = str(body.get("minecraft_name") or name).strip()
            command_name = str(body.get("command_name") or "").strip()
            pack_name = str(body.get("pack_name") or "").strip()
            resolution = int(body.get("blockify_resolution") or 0)
            if not top_level or not uci or not source_path or not name or not minecraft_name or not command_name:
                # A standalone IFEditor import has no CSV-assigned ContentPack.
                # Original pack staging below is already conditional on pack_name.
                raise ValueError("migration proof requires top_level, uci, source_path, asset name, Minecraft name, and command name")
            if resolution < 0 or (resolution > 0 and resolution % 16):
                raise ValueError("asset migration Blockify Resolution must be 0 or a positive multiple of 16")

            root = assets_root().resolve()
            source = (root / source_path).resolve()
            if root not in source.parents or not source.is_file():
                raise FileNotFoundError(source_path)
            destination = (root / top_level / uci).resolve()
            if root not in destination.parents:
                raise ValueError("invalid migration proof destination")
            # The proof is intentionally rerunnable. Generated representation files may be
            # replaced, but authored edits.json is preserved below.
            destination_existed = destination.exists()
            destination.mkdir(parents=True, exist_ok=True)

            def write_export_files(folder: Path, rows: list[dict[str, Any]], representation: str) -> list[str]:
                written: list[str] = []
                folder.mkdir(parents=True, exist_ok=True)
                for row in rows:
                    relative = str(row.get("name") or "").replace("\\", "/").lstrip("/")
                    # Keep exporter resource names (.bin and textures) untouched. Only the
                    # human-facing GLTF gets the asset name. Its internal URI references
                    # therefore continue to point at the exporter-named resources.
                    if Path(relative).suffix.casefold() == ".gltf":
                        relative = f"{name}_{representation}.gltf"
                    if not relative or ".." in Path(relative).parts:
                        raise ValueError(f"invalid exported GLTF resource path: {relative}")
                    target = (folder / relative).resolve()
                    if folder.resolve() not in target.parents:
                        raise ValueError(f"exported GLTF resource escapes representation folder: {relative}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    encoding = str(row.get("encoding") or "text")
                    payload = row.get("data") or ""
                    if encoding == "base64":
                        target.write_bytes(base64.b64decode(str(payload), validate=True))
                    else:
                        target.write_text(str(payload), encoding="utf-8")
                    written.append(target.relative_to(destination).as_posix())
                return written

            solid_rows = list(body.get("solid_files") or [])
            block_rows = list(body.get("block_files") or [])
            import_animation = body.get("import_animation")
            if import_animation is not None:
                if (not isinstance(import_animation, dict) or
                        not str(import_animation.get("id") or "").strip() or
                        not str(import_animation.get("name") or "").strip()):
                    raise ValueError("Imported animation identity must name a real catalog clip")
                import_animation = {"id": str(import_animation["id"]),
                                    "name": str(import_animation["name"])}
            if not solid_rows:
                raise ValueError("asset migration requires a Solid GLTF export")
            if resolution > 0 and not block_rows:
                raise ValueError("blockified asset migration requires a Block GLTF export")
            if import_animation:
                solid_document = next((row for row in solid_rows if
                    str(row.get("name") or "").lower().endswith(".gltf") and
                    str(row.get("encoding") or "text") == "text"), None)
                if not solid_document:
                    raise ValueError("Imported animation has no authored Solid GLTF")
                solid_model = json.loads(str(solid_document.get("data") or "{}"))
                joints = {index for skin in solid_model.get("skins") or []
                          for index in skin.get("joints") or []}
                if not any(channel.get("target", {}).get("node") in joints
                           for animation in solid_model.get("animations") or []
                           for channel in animation.get("channels") or []):
                    raise ValueError("Imported Solid GLTF has no playable skinned animation")

            try:
                solid_files = write_export_files(destination / "solid", solid_rows, "solid")
                block_folder = destination / f"block_{resolution}" if resolution > 0 else None
                block_files = write_export_files(block_folder, block_rows, "block") if block_folder else []

                def write_thumbnail(folder: Path, data_url: str) -> str:
                    header, encoded = str(data_url or "").split(",", 1)
                    if ";base64" not in header.casefold():
                        raise ValueError("thumbnail must be a base64 data URL")
                    representation = "solid" if folder.name == "solid" else "block"
                    path = folder / f"{name}_{representation}_thumbnail.png"
                    path.write_bytes(base64.b64decode(encoded, validate=True))
                    return path.relative_to(destination).as_posix()

                solid_thumbnail_data = str(body.get("solid_thumbnail") or "")
                if solid_thumbnail_data:
                    solid_thumbnail = write_thumbnail(destination / "solid", solid_thumbnail_data)
                elif resolution == 0:
                    # Animation-only catalog rows may legitimately contain no visible
                    # mesh. They still get a canonical Solid GLTF, but no fabricated
                    # thumbnail is required. Remove any stale thumbnail left by an older
                    # migration so the manifest and filesystem remain truthful.
                    stale_thumbnail = destination / "solid" / f"{name}_solid_thumbnail.png"
                    if stale_thumbnail.exists():
                        stale_thumbnail.unlink()
                    solid_thumbnail = None
                else:
                    raise ValueError("placeable asset migration requires a Solid thumbnail")
                block_thumbnail = None
                edits = None
                if block_folder:
                    block_thumbnail = write_thumbnail(block_folder, str(body.get("block_thumbnail") or ""))
                    edits = block_folder / "edits.json"
                    if not edits.exists():
                        edits.write_text(json.dumps({"version": 1, "edits": []}, indent=2) + "\n", encoding="utf-8")

                # Remove only obsolete generic names from the earlier proof format.
                # Never remove exporter resources, textures, or authored edits.
                obsolete_paths = [
                    destination / "solid" / "model.gltf",
                    destination / "solid" / "thumbnail.png",
                ]
                if block_folder:
                    obsolete_paths.extend([
                        block_folder / "model.gltf",
                        block_folder / "thumbnail.png",
                        block_folder / "minecraft.mcaddon",
                    ])
                for obsolete in obsolete_paths:
                    if obsolete.exists():
                        obsolete.unlink()

                voxels = list(body.get("voxels") or [])
                character_voxels = list(body.get("character_voxels") or [])
                character_rig = body.get("character_rig") or {}
                character_animations = body.get("character_animations") or {}
                behavior_profile = list(body.get("behavior_profile") or [])
                source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
                representations = {
                    "solid": {
                        "model": f"solid/{name}_solid.gltf",
                        "thumbnail": solid_thumbnail,
                        "files": solid_files,
                    },
                }
                if block_folder:
                    addon = block_folder / f"{name}.mcaddon"
                    cube_blocks = max(1, resolution // 16)
                    _write_blockify_mcaddon(
                        addon,
                        source=source,
                        source_sha256=source_sha256,
                        width=cube_blocks,
                        height=cube_blocks,
                        depth=cube_blocks,
                        voxels=voxels,
                        character_voxels=character_voxels,
                        character_rig=character_rig,
                        character_animations=character_animations,
                        behavior_profile=behavior_profile,
                        product_name=minecraft_name,
                        item_icon_png=(block_folder / f"{name}_block_thumbnail.png").read_bytes(),
                        command_name=command_name,
                    )

                    # Stage a distribution copy by themed pack without disturbing the
                    # canonical UCI-owned representation under Assets.  Only the compact
                    # Minecraft artifact + rendered thumbnail + metadata are duplicated.
                    # The large Solid/Block GLTF trees remain single-copy canonical data.
                    if pack_name:
                        from runtime.assets.asset_management import project_root
                        safe_pack = re.sub(r"[^A-Za-z0-9 &'-]+", "", pack_name).strip()
                        staged_folder = cinterforgeBuildsRootPath() / "Assets" / "MCAddons" / safe_pack / command_name
                        staged_folder.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(addon, staged_folder / f"{command_name}.mcaddon")
                        shutil.copy2(block_folder / f"{name}_block_thumbnail.png", staged_folder / f"{command_name}.png")
                        preview_model = _stage_block_preview(block_folder, staged_folder, name)
                        (staged_folder / "asset.json").write_text(
                            json.dumps({
                                "asset_name": name,
                                "minecraft_name": minecraft_name,
                                "command_name": command_name,
                                "pack_name": pack_name,
                                "uci": uci,
                                "give_command": f"/give @s cinter:{command_name}",
                                "preview_model": preview_model,
                                "category": _migration_asset_category(source_path),
                            }, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8",
                        )
                    representations[f"block_{resolution}"] = {
                        "resolution": resolution,
                        "model": f"block_{resolution}/{name}_block.gltf",
                        "thumbnail": block_thumbnail,
                        "edits": f"block_{resolution}/edits.json",
                        "minecraft": f"block_{resolution}/{name}.mcaddon",
                        "files": block_files,
                        "runtime_recipe": BLOCKIFY_RUNTIME_RECIPE,
                        "runtime_recipe_fingerprint": BLOCKIFY_RUNTIME_RECIPE_FINGERPRINT,
                    }

                manifest = {
                    "uci": uci,
                    "name": name,
                    "minecraft_name": minecraft_name,
                    "command_name": command_name,
                    "pack_name": pack_name,
                    "version": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "legacy_source": source_path,
                    "category": _migration_asset_category(source_path),
                    "representations": representations,
                }
                if import_animation:
                    manifest["import_animation"] = import_animation
                manifest_path = destination / "manifest.json"
                if manifest_path.is_file():
                    # Server's IFEditor save_version and its original migration
                    # writer both own different parts of the same UCI.  Never
                    # replace the versioned source/animations with a v1 record.
                    previous = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if previous.get("uci") != uci:
                        raise ValueError("Existing UCI manifest identity differs from migration")
                    preserved = dict(previous)
                    prior_reps = previous.get("representations")
                    merged_reps = dict(prior_reps) if isinstance(prior_reps, dict) else {}
                    merged_reps.update(representations)
                    preserved.update(manifest)
                    if body.get("local_import") and not import_animation:
                        preserved.pop("import_animation", None)
                    if not body.get("local_import") and not import_animation and isinstance(previous.get("import_animation"), dict):
                        preserved["import_animation"] = previous["import_animation"]
                    preserved["representations"] = merged_reps
                    if isinstance(previous.get("versions"), list):
                        # Native viewer and animation assignments depend on these
                        # original Server saved-version fields.
                        for field in ("schema", "versions", "default_version", "last_viewed",
                                      "assigned_animations", "default_animation", "subgroup", "category"):
                            if field in previous and (not body.get("local_import") or field not in {"category", "subgroup"}):
                                preserved[field] = previous[field]
                    manifest = preserved
                if body.get("local_import"):
                    # The import's final classification is determined by actual
                    # clips, not by the original rig or provisional source path.
                    # Embedded non-Mixamo animations count without retargeting.
                    from runtime.assets.asset_management import inspect_model
                    inspection = inspect_model(source)
                    category = "Animated" if (
                        inspection.get("has_embedded_animations")
                        or inspection.get("mixamo_compatible_rig")
                        or import_animation
                    ) else "Objects"
                    manifest["category"] = category
                    manifest["subgroup"] = ""
                    desired = (root / category / uci).resolve()
                    if desired != destination:
                        if desired.exists() or desired.is_symlink():
                            raise FileExistsError(f"Cannot relocate imported UCI; destination already exists: {desired}")
                        desired.parent.mkdir(parents=True, exist_ok=True)
                        # All UCI representations and original bytes move as one
                        # owned directory. No UCI or source content is changed.
                        destination.rename(desired)
                        destination = desired
                    manifest["legacy_source"] = (destination / source.name).relative_to(root).as_posix()
                    manifest_path = destination / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except Exception:
                # A first-time failed proof may be removed wholesale. On a rerun, never
                # destroy an existing UCI directory because it can contain authored edits.
                if not destination_existed:
                    shutil.rmtree(destination, ignore_errors=True)
                raise

            return _ok(
                proof=True,
                destination=destination.relative_to(root).as_posix(),
                manifest=manifest,
            )

        if operation in {"asset-image-convert-local", "asset-image-convert-publish"}:
            from runtime.assets.asset_management import assets_root
            source_relative = str(body.get("path") or "").strip().replace("\\", "/").strip("/")
            root = assets_root().resolve()
            source = (root / source_relative).resolve()
            if root not in source.parents or not source.is_file():
                raise FileNotFoundError(source_relative)
            name = source.stem
            code = "".join(ch for ch in name.upper() if ch.isalnum())[:8] or "IMAGE"
            stamp = datetime.now(timezone.utc).replace(microsecond=0)
            while True:
                uci = f"CINTERASST{stamp.strftime('%Y%m%d%H%M%S')}#{code}en"
                destination = root / "Images" / uci
                if not destination.exists():
                    break
                stamp += timedelta(seconds=1)
            solid = destination / "solid"
            solid.mkdir(parents=True, exist_ok=False)
            copied = solid / source.name
            shutil.copy2(source, copied)
            manifest = {"uci": uci, "name": name, "version": 1, "timestamp": datetime.now(timezone.utc).isoformat(), "legacy_source": source_relative, "representations": {"solid": {"file": f"solid/{source.name}"}}}
            (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            files = {}
            rows = []
            for item in sorted(path for path in destination.rglob("*") if path.is_file()):
                rel = item.relative_to(root).as_posix(); files[rel] = item
                rows.append({"path": rel, "name": item.name, "size": item.stat().st_size, "kind": "image", "uci": uci})
            # Conversion is an authoring operation. Keep the generated managed
            # asset local; Push/Build own publication to Cloudflare. The legacy
            # operation name remains accepted for compatibility, but it no longer
            # performs an implicit publish.
            return _ok(path=destination.relative_to(root).as_posix(), uci=uci, files=len(files))

        if operation == "asset-managed-publish":
            from runtime.assets.asset_management import assets_root, publish_cloudflare_asset_bundle
            relative = str(body.get("path") or "").strip().replace("\\", "/").strip("/")
            if not relative:
                raise ValueError("managed asset path is required")
            root = assets_root().resolve()
            folder = (root / relative).resolve()
            if root not in folder.parents or not folder.is_dir():
                raise FileNotFoundError(relative)
            manifest_path = folder / "manifest.json"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"managed asset manifest is missing: {relative}/manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = {}
            rows = []
            for source in sorted(path for path in folder.rglob("*") if path.is_file()):
                rel = source.relative_to(root).as_posix()
                files[rel] = source
                rows.append({"path": rel, "name": source.name, "size": source.stat().st_size, "kind": "managed", "uci": str(manifest.get("uci") or "")})
            publish_cloudflare_asset_bundle(files, rows)
            return _ok(path=relative, uci=str(manifest.get("uci") or ""), files=len(files))

        if operation == "asset-migration-stage-existing":
            from runtime.assets.asset_management import assets_root, project_root

            top_level = str(body.get("top_level") or "").strip()
            uci = str(body.get("uci") or "").strip()
            name = str(body.get("name") or "").strip()
            command_name = str(body.get("command_name") or "").strip()
            pack_name = str(body.get("pack_name") or "").strip()
            resolution = int(body.get("blockify_resolution") or 0)
            if not top_level or not uci or not name or not command_name:
                raise ValueError("existing migration staging requires top_level, uci, name, and command_name")

            root = assets_root().resolve()
            destination = (root / top_level / uci).resolve()
            if root not in destination.parents or not destination.is_dir():
                raise FileNotFoundError(f"managed asset does not exist: {top_level}/{uci}")

            manifest_path = destination / "manifest.json"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"existing managed asset manifest is missing: {top_level}/{uci}/manifest.json")

            addon_relative = ""
            if resolution > 0 and pack_name:
                block_folder = destination / f"block_{resolution}"
                addon = block_folder / f"{name}.mcaddon"
                thumbnail = block_folder / f"{name}_block_thumbnail.png"
                if not addon.is_file():
                    raise FileNotFoundError(f"existing managed asset MCAddon is missing: {addon}")
                if not thumbnail.is_file():
                    raise FileNotFoundError(f"existing managed asset Block thumbnail is missing: {thumbnail}")

                safe_pack = re.sub(r"[^A-Za-z0-9 &'-]+", "", pack_name).strip()
                staged_folder = cinterforgeBuildsRootPath() / "Assets" / "MCAddons" / safe_pack / command_name
                staged_folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(addon, staged_folder / f"{command_name}.mcaddon")
                shutil.copy2(thumbnail, staged_folder / f"{command_name}.png")
                preview_model = _stage_block_preview(block_folder, staged_folder, name)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                minecraft_name = str(manifest.get("minecraft_name") or body.get("minecraft_name") or name).strip()
                (staged_folder / "asset.json").write_text(
                    json.dumps({
                        "asset_name": name,
                        "minecraft_name": minecraft_name,
                        "command_name": command_name,
                        "pack_name": pack_name,
                        "uci": uci,
                        "give_command": f"/give @s cinter:{command_name}",
                        "preview_model": preview_model,
                        "category": str(manifest.get("category") or _migration_asset_category(str(manifest.get("legacy_source") or ""))),
                    }, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                addon_relative = addon.relative_to(root).as_posix()

            return _ok(path=destination.relative_to(root).as_posix(), addon=addon_relative)

        if operation == "asset-migration-pack-staging-status":
            from runtime.assets.asset_management import project_root

            root = cinterforgeBuildsRootPath() / "Assets" / "MCAddons"
            staged_ucis: list[str] = []
            pack_files: list[str] = []
            if root.is_dir():
                for metadata_path in root.glob("*/*/asset.json"):
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    uci = str(metadata.get("uci") or "").strip()
                    if uci:
                        staged_ucis.append(uci)
                for pack_folder in root.iterdir():
                    if not pack_folder.is_dir():
                        continue
                    pack_file = pack_folder / f"{pack_folder.name}.mcaddon"
                    if pack_file.is_file():
                        pack_files.append(pack_folder.name)
            return _ok(
                path=str(root),
                staged_ucis=sorted(set(staged_ucis)),
                pack_files=sorted(set(pack_files), key=str.casefold),
            )

        if operation == "asset-migration-pack-reset":
            from runtime.assets.asset_management import project_root
            root = cinterforgeBuildsRootPath() / "Assets" / "MCAddons"
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)
            return _ok(path=str(root))


        if operation == "asset-migration-build-packs":
            import uuid
            from runtime.assets.asset_management import project_root, BLOCKIFY_ADDON_PACK_VERSION

            root = cinterforgeBuildsRootPath() / "Assets" / "MCAddons"
            root.mkdir(parents=True, exist_ok=True)
            built: list[dict[str, Any]] = []

            def pack_uuid(pack_name: str, suffix: str) -> str:
                return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://cinterforge.com/minecraft/themed/{pack_name}/{suffix}"))

            for pack_folder in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name.casefold()):
                asset_folders = sorted(
                    (path for path in pack_folder.iterdir() if path.is_dir() and (path / "asset.json").is_file()),
                    key=lambda path: path.name.casefold(),
                )
                if not asset_folders:
                    continue
                resource_files: dict[str, bytes] = {}
                behavior_files: dict[str, bytes] = {}
                texture_data: dict[str, Any] = {}
                language_lines: list[str] = []
                icon_png = b""

                for asset_folder in asset_folders:
                    metadata = json.loads((asset_folder / "asset.json").read_text(encoding="utf-8"))
                    command_name = str(metadata.get("command_name") or asset_folder.name)
                    addon = asset_folder / f"{command_name}.mcaddon"
                    if not addon.is_file():
                        raise FileNotFoundError(f"Themed pack source Add-On is missing: {addon}")
                    if not icon_png:
                        thumb = asset_folder / f"{command_name}.png"
                        if thumb.is_file():
                            icon_png = thumb.read_bytes()
                    with zipfile.ZipFile(addon) as outer:
                        nested = [row for row in outer.infolist() if not row.is_dir() and row.filename.casefold().endswith(".mcpack")]
                        if len(nested) != 2:
                            raise RuntimeError(f"Expected one RP and one BP in {addon}; found {len(nested)} packs")
                        for nested_info in nested:
                            payload = outer.read(nested_info)
                            with zipfile.ZipFile(io.BytesIO(payload)) as inner:
                                manifest = json.loads(inner.read("manifest.json").decode("utf-8"))
                                module_types = {str(module.get("type") or "") for module in manifest.get("modules") or [] if isinstance(module, dict)}
                                target = resource_files if "resources" in module_types else behavior_files if "data" in module_types else None
                                if target is None:
                                    raise RuntimeError(f"Unknown nested pack type in {addon}: {nested_info.filename}")
                                for info in inner.infolist():
                                    if info.is_dir() or info.filename in {"manifest.json", "pack_icon.png", "cinterforge_export.json"}:
                                        continue
                                    data = inner.read(info)
                                    if target is resource_files and info.filename == "textures/item_texture.json":
                                        item_texture = json.loads(data.decode("utf-8"))
                                        for key, value in dict(item_texture.get("texture_data") or {}).items():
                                            if key in texture_data and texture_data[key] != value:
                                                raise RuntimeError(f"Themed pack texture key collision: {pack_folder.name}: {key}")
                                            texture_data[key] = value
                                        continue
                                    if target is resource_files and info.filename == "texts/en_US.lang":
                                        language_lines.extend(line for line in data.decode("utf-8").splitlines() if line.strip())
                                        continue
                                    if target is resource_files and info.filename == "texts/languages.json":
                                        continue
                                    if info.filename in target and target[info.filename] != data:
                                        raise RuntimeError(f"Themed pack file collision: {pack_folder.name}: {info.filename}")
                                    target[info.filename] = data

                version = list(BLOCKIFY_ADDON_PACK_VERSION)
                rp_id, rpm_id = pack_uuid(pack_folder.name, "rp"), pack_uuid(pack_folder.name, "rp-module")
                bp_id, bpm_id = pack_uuid(pack_folder.name, "bp"), pack_uuid(pack_folder.name, "bp-module")
                rp_manifest = {"format_version": 2, "header": {"name": f"CinterForge - {pack_folder.name}", "description": f"CinterForge themed asset pack: {pack_folder.name}", "uuid": rp_id, "version": version, "min_engine_version": [1, 21, 80]}, "modules": [{"type": "resources", "uuid": rpm_id, "version": version}]}
                bp_manifest = {"format_version": 2, "header": {"name": f"CinterForge - {pack_folder.name}", "description": f"CinterForge themed asset pack: {pack_folder.name}", "uuid": bp_id, "version": version, "min_engine_version": [1, 21, 80]}, "modules": [{"type": "data", "uuid": bpm_id, "version": version}], "dependencies": [{"uuid": rp_id, "version": version}]}
                resource_files["textures/item_texture.json"] = (json.dumps({"resource_pack_name": f"CinterForge - {pack_folder.name}", "texture_name": "atlas.items", "texture_data": texture_data}, indent=2, sort_keys=True) + "\n").encode("utf-8")
                resource_files["texts/en_US.lang"] = ("\n".join(dict.fromkeys(language_lines)) + "\n").encode("utf-8")
                resource_files["texts/languages.json"] = (json.dumps(["en_US"], indent=2) + "\n").encode("utf-8")

                safe_name = re.sub(r"[^A-Za-z0-9]+", "_", pack_folder.name).strip("_")
                with tempfile.TemporaryDirectory(prefix="cinterforge-themed-pack-") as td:
                    td_path = Path(td)
                    rp_path = td_path / f"{safe_name}_RP.mcpack"
                    bp_path = td_path / f"{safe_name}_BP.mcpack"
                    for path, manifest, files in ((rp_path, rp_manifest, resource_files), (bp_path, bp_manifest, behavior_files)):
                        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                            zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                            if icon_png:
                                zf.writestr("pack_icon.png", icon_png)
                            for name, data in sorted(files.items()):
                                zf.writestr(name, data)
                    destination = pack_folder / f"{pack_folder.name}.mcaddon"
                    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                        zf.write(rp_path, rp_path.name)
                        zf.write(bp_path, bp_path.name)
                built.append({"name": pack_folder.name, "assets": len(asset_folders), "file": str(destination), "bytes": destination.stat().st_size})
            return _ok(packs=built, count=len(built))

        if operation == "asset-migration-pack-galleries":
            root = cinterforgeBuildsRootPath() / "Assets" / "MCAddons"
            if not root.is_dir():
                raise FileNotFoundError(f"Generated Minecraft pack directory is missing: {root}")
            content_root = cinterforgeBuildsRootPath() / "Assets" / "ContentPacks"
            background_width = 1536
            background_height = 864

            # Work from the packs that the ORIGINAL pack builder actually
            # assembled, not from a historic list of possible content themes.
            # Artwork is optional presentation input: an absent banner must
            # never discard a successfully assembled pack or its HTML catalog.
            preferred_order = [
                "Heroes of the Realm", "Monsters of the Realm", "Dragons & Wild Things",
                "Dungeons & Adventures", "Beyond the Stars", "Wild Kingdom",
                "Things in the Dark", "Everyday Heroes", "People & Places",
                "Modern Life", "City Stories", "Moves & Actions", "Unassigned",
            ]
            assembled = {
                folder.name for folder in root.iterdir()
                if folder.is_dir() and not folder.is_symlink()
                and (folder / f"{folder.name}.mcaddon").is_file()
            }
            if not assembled:
                raise FileNotFoundError(f"No assembled Minecraft packs found in {root}")
            generated_pack_names = ([name for name in preferred_order if name in assembled]
                                    + sorted(assembled - set(preferred_order)))

            # Keep the original pinned viewer libraries; never use a CDN or
            # substitute runtime. Verify their staged bytes before publishing.
            runtime_root = Path(__file__).resolve().parents[2] / "data/asset_engine/vendor"
            viewer_root = root / "_viewer"
            viewer_root.mkdir(parents=True, exist_ok=True)
            for library_name in ("babylon.js", "babylonjs.loaders.min.js"):
                source_library = runtime_root / library_name
                if not source_library.is_file():
                    raise FileNotFoundError(f"Babylon gallery runtime is missing: {source_library}")
                shutil.copy2(source_library, viewer_root / library_name)

            artwork_warnings = []
            for pack_name in generated_pack_names:
                pack_folder = root / pack_name
                art_folder = content_root / pack_name
                if art_folder.is_symlink():
                    raise ValueError(f"Pack artwork directory is a symlink: {art_folder}")
                # Keep the original neutral Unassigned covers when Pillow is
                # installed, but do not make Pillow a requirement for viewing
                # otherwise completed Minecraft packs.
                if pack_name == "Unassigned" and not all(
                        (pack_folder / name).is_file()
                        for name in ("banner.png", "thumbnail.png")):
                    try:
                        from PIL import Image, ImageDraw, ImageFont
                    except ImportError:
                        pass
                    else:
                        for name, size, subtitle in (
                            ("banner.png", (1536, 864), "Review · classify · run Blockify All again"),
                            ("thumbnail.png", (512, 288), "Assets awaiting pack assignment"),
                        ):
                            destination = pack_folder / name
                            if destination.is_symlink():
                                raise ValueError(f"Pack cover is a symlink: {destination}")
                            if destination.is_file():
                                continue
                            cover = Image.new("RGB", size, (18, 26, 34))
                            draw = ImageDraw.Draw(cover)
                            font = ImageFont.load_default()
                            draw.rectangle((0, 0, size[0] - 1, size[1] - 1),
                                           outline=(90, 116, 135), width=max(2, size[0] // 256))
                            draw.text((size[0] * .06, size[1] * .40), "UNASSIGNED",
                                      fill=(235, 241, 245), font=font)
                            draw.text((size[0] * .06, size[1] * .52), subtitle,
                                      fill=(156, 181, 198), font=font)
                            cover.save(destination, format="PNG")
                for art_name in ("banner.png", "thumbnail.png"):
                    source = art_folder / art_name
                    destination = pack_folder / art_name
                    if source.is_symlink() or destination.is_symlink():
                        raise ValueError(f"Pack cover is a symlink: {source} or {destination}")
                    if source.is_file():
                        shutil.copy2(source, destination)
                    elif not destination.is_file():
                        artwork_warnings.append(f"{pack_name}/{art_name}")

            nav_rows = [
                {"name": name, "href": f"../{url_quote(name, safe='')}/index.html",
                 "thumbnail": (f"../{url_quote(name, safe='')}/thumbnail.png"
                               if (root / name / "thumbnail.png").is_file() else "")}
                for name in generated_pack_names
            ]
            total_assets = 0
            generated_packs = 0

            # Write a navigable root entry point before any individual asset
            # page is validated. A later failure remains a failure, but can no
            # longer strand a successful pack build without any index.html.
            root_index_path = root / "index.html"
            first_pack_href = url_quote(f"{generated_pack_names[0]}/index.html", safe="/")
            root_index = (
                '<!doctype html><meta charset="utf-8"><title>CinterForge Content Packs</title>'
                f'<script>location.replace({json.dumps(first_pack_href)})</script>'
                f'<a href="{_HTML_ESCAPE(first_pack_href)}">Open CinterForge Content Packs</a>\n'
            )
            root_index_path.write_text(root_index, encoding="utf-8")

            page_template = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__ - CinterForge</title><style>
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;background:#050a0f;font-family:Arial,Helvetica,sans-serif;color:#fff}body{padding:18px;background:radial-gradient(circle at 50% 0,#17344a 0,#0a1721 38%,#050a0f 100%)}#stage{width:min(1500px,100%);margin:0 auto;display:grid;grid-template-columns:235px minmax(0,1fr) 465px;grid-template-rows:auto auto 1fr;gap:15px;padding:15px;border:1px solid #29485d;border-radius:16px;background:#08131d;box-shadow:0 18px 60px #000a}#packRail{grid-column:1;grid-row:1/4;display:flex;flex-direction:column;gap:7px;max-height:820px;overflow-y:auto;padding-right:5px;scrollbar-width:thin;scrollbar-color:#168dcc #05090d}.pack{display:grid;grid-template-columns:62px minmax(0,1fr);align-items:center;gap:9px;min-height:53px;border:1px solid #20394a;border-radius:8px;background:#0b1822;color:#fff;text-decoration:none;padding:6px}.pack:hover{border-color:#72d9ff}.pack.active{border-color:#42bfff;background:#0b4164;box-shadow:0 0 16px #009ee855}.pack img{width:62px;height:38px;object-fit:cover;border-radius:5px}.pack span{font-size:14px;line-height:1.08;font-weight:800}#hero{grid-column:2;grid-row:1;display:grid;gap:11px;min-width:0}#banner{display:block;width:100%;height:225px;object-fit:cover;border:1px solid #35546a;border-radius:12px;background:#000}#stats{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.stat{text-align:center;padding:8px 3px;border:1px solid #29485d;border-radius:8px;background:#0b1822}.stat b{display:block;font-size:21px}.stat small{font-size:10px;letter-spacing:.07em;color:#9fb4c3}#viewer{grid-column:3;grid-row:1/3;position:relative;min-height:330px;border:1px solid #35546a;border-radius:12px;overflow:hidden;background:linear-gradient(#496879,#263d4b)}#renderCanvas{width:100%;height:100%;min-height:330px;display:block;outline:0;touch-action:none}#assetInfo{position:absolute;left:12px;top:12px;padding:7px 10px;background:#07121ddd;border-radius:6px;font-weight:900;text-shadow:0 2px 3px #000}#assetInfo small{display:block;color:#c2ccd5;margin-top:2px}#play{position:absolute;right:12px;top:12px;border:1px solid #73d9ff;background:#087ec3;color:#fff;border-radius:6px;padding:8px 14px;font-weight:900;cursor:pointer}#actions{grid-column:2;grid-row:2;display:flex;gap:9px}#download,#minecraft{flex:1;min-height:44px;display:flex;align-items:center;justify-content:center;border-radius:8px;font-weight:900}#download{border:1px solid #e4ad2b;background:linear-gradient(#ffc94c,#d99a16);color:#171008;text-decoration:none}#minecraft{border:1px solid #35546a;background:#102332;color:#fff}#gridWrap{grid-column:2/4;grid-row:3;min-width:0}#gridHeader{display:flex;justify-content:space-between;align-items:end;margin:2px 2px 8px}#gridHeader strong{font-size:17px}#gridHeader small{color:#91a8b8}#grid{display:grid;grid-template-columns:repeat(13,minmax(0,1fr));grid-auto-rows:86px;gap:7px}.slot{position:relative;padding:0;border:2px solid #29485d;border-radius:7px;overflow:hidden;background:#111a21;cursor:pointer}.slot img{display:block;width:100%;height:100%;object-fit:contain;background:#101820}.slot span{position:absolute;left:0;right:0;bottom:0;padding:3px 4px;background:#05080be8;font-size:8px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.slot.character{border-color:#087fbd}.slot.creature{border-color:#a61d2a}.slot.object{border-color:#a36c0c}.slot.character:hover,.slot.character.selected{border-color:#6bdcff;box-shadow:0 0 10px #00a9ff}.slot.creature:hover,.slot.creature.selected{border-color:#ff7782;box-shadow:0 0 10px #ff3345}.slot.object:hover,.slot.object.selected{border-color:#ffe36c;box-shadow:0 0 10px #ffbd22}.slot.empty{cursor:default;border-color:#15232d;background:#0a1117}@media(max-width:1100px){#stage{grid-template-columns:190px minmax(0,1fr)}#packRail{grid-row:1/4}#hero{grid-column:2}#viewer{grid-column:2;grid-row:2;min-height:360px}#actions{grid-column:2;grid-row:3}#gridWrap{grid-column:1/3;grid-row:4;overflow-x:auto}#grid{grid-template-columns:repeat(13,90px)}}</style></head><body><main id="stage"><nav id="packRail"></nav><section id="hero"><img id="banner" src="banner.png" alt=""><div id="stats"></div></section><div id="viewer"><canvas id="renderCanvas"></canvas><div id="assetInfo">Select an asset<small>Click a thumbnail below</small></div><button id="play" type="button">Play</button></div><div id="actions"><a id="download" download>Download Pack</a><button id="minecraft" type="button">Set as Current Pack</button></div><section id="gridWrap"><div id="gridHeader"><strong>Pack Assets</strong><small>Characters → Creatures → Objects</small></div><div id="grid"></div></section></main>
    <script>window.CINTERFORGE_PACK_DATA=__DATA__;</script><script src="../_viewer/babylon.js"></script><script src="../_viewer/babylonjs.loaders.min.js"></script><script>(()=>{"use strict";const d=window.CINTERFORGE_PACK_DATA,$=id=>document.getElementById(id);for(const p of d.packs){const a=document.createElement("a");a.className="pack"+(p.name===d.currentPack?" active":"");a.href=p.href;const i=document.createElement(p.thumbnail?"img":"span");if(p.thumbnail){i.src=p.thumbnail;i.alt=""}else{i.textContent="Pack";i.className="pack-no-art"}const s=document.createElement("span");s.textContent=p.name;a.append(i,s);$("packRail").appendChild(a)}const ordered=[...d.assets.filter(a=>a.type==="character").sort(byName),...d.assets.filter(a=>a.type==="creature").sort(byName),...d.assets.filter(a=>a.type==="object").sort(byName)];const c={character:ordered.filter(a=>a.type==="character").length,creature:ordered.filter(a=>a.type==="creature").length,object:ordered.filter(a=>a.type==="object").length};$("stats").innerHTML=[["ASSETS",ordered.length],["CHARACTERS",c.character],["CREATURES",c.creature],["OBJECTS",c.object]].map(([k,v])=>`<div class="stat"><b>${v}</b><small>${k}</small></div>`).join("");if(d.mcaddon){$("download").href=d.mcaddon}else{$("download").style.visibility="hidden"}const buttons=[];const slotCount=Math.max(39,ordered.length);for(let n=0;n<slotCount;n++){const x=ordered[n],b=document.createElement("button");b.type="button";if(!x){b.className="slot empty";b.disabled=true}else{b.className="slot "+x.type;const i=document.createElement("img");i.src=x.thumbnail;i.alt=x.name;i.loading="lazy";const s=document.createElement("span");s.textContent=x.name;b.append(i,s);b.onclick=()=>select(x,b)}$("grid").appendChild(b);buttons.push(b)}let engine=null,scene=null,camera=null,container=null;if(window.BABYLON){engine=new BABYLON.Engine($("renderCanvas"),true,{alpha:true,preserveDrawingBuffer:true,stencil:true});scene=new BABYLON.Scene(engine);scene.clearColor=new BABYLON.Color4(.22,.32,.38,1);camera=new BABYLON.ArcRotateCamera("camera",-Math.PI/2,Math.PI/2.25,4,new BABYLON.Vector3(0,1,0),scene);camera.attachControl($("renderCanvas"),true);camera.wheelPrecision=45;const key=new BABYLON.HemisphericLight("key",new BABYLON.Vector3(0,1,0),scene);key.intensity=2.15;const fill=new BABYLON.HemisphericLight("fill",new BABYLON.Vector3(0,-1,0),scene);fill.intensity=.8;engine.runRenderLoop(()=>scene.render());addEventListener("resize",()=>engine.resize())}async function select(a,b){document.querySelectorAll(".slot.selected").forEach(x=>x.classList.remove("selected"));b.classList.add("selected");$("assetInfo").innerHTML=esc(a.name)+`<small>${a.type[0].toUpperCase()+a.type.slice(1)}${a.animated?" - Animated":""}</small>`;$("play").style.display=a.animated?"block":"none";if(!scene||!a.gltf)return;try{if(container){container.removeAllFromScene();container.dispose();container=null}container=await BABYLON.SceneLoader.LoadAssetContainerAsync("",a.gltf,scene);container.addAllToScene();frame();playFirst()}catch(e){console.error("[CinterForge pack viewer] preview load failed",a,e);$("assetInfo").innerHTML=esc(a.name)+"<small>Preview failed</small>"}}function frame(){const m=container.meshes.filter(x=>x.getBoundingInfo&&x.isEnabled());if(!m.length)return;let min=new BABYLON.Vector3(Infinity,Infinity,Infinity),max=new BABYLON.Vector3(-Infinity,-Infinity,-Infinity);for(const x of m){x.computeWorldMatrix(true);const b=x.getBoundingInfo().boundingBox;min=BABYLON.Vector3.Minimize(min,b.minimumWorld);max=BABYLON.Vector3.Maximize(max,b.maximumWorld)}camera.target=min.add(max).scale(.5);camera.radius=Math.max(max.subtract(min).length()*1.25,.75)}function playFirst(){if(!container)return;for(const g of container.animationGroups)g.stop();if(container.animationGroups.length)container.animationGroups[0].start(true)}$("play").onclick=playFirst;function byName(a,b){return a.name.localeCompare(b.name)}function esc(s){const e=document.createElement("div");e.textContent=s;return e.innerHTML}if(ordered.length)select(ordered[0],buttons[0])})();</script></body></html>'''

            for pack_name in generated_pack_names:
                pack_folder = root / pack_name
                asset_rows = []
                for metadata_path in sorted(pack_folder.glob("*/asset.json"), key=lambda path: path.parent.name.casefold()):
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    command_name = str(metadata.get("command_name") or metadata_path.parent.name)
                    preview_model = str(metadata.get("preview_model") or "").strip()
                    if not preview_model:
                        continue
                    model_path = metadata_path.parent / preview_model
                    if not model_path.is_file():
                        raise FileNotFoundError(f"Pack preview model is missing: {model_path}")
                    category = str(metadata.get("category") or "").strip().casefold()
                    if category not in {"character", "creature", "object"}:
                        raise ValueError(f"Pack asset category is invalid for {metadata_path}: {category!r}")
                    try:
                        model_document = json.loads(model_path.read_text(encoding="utf-8"))
                        animated = bool(model_document.get("animations"))
                    except Exception as exc:
                        raise RuntimeError(f"Cannot inspect pack preview GLTF {model_path}: {exc}") from exc
                    thumbnail = metadata_path.parent / f"{command_name}.png"
                    if not thumbnail.is_file():
                        raise FileNotFoundError(f"Pack thumbnail is missing: {thumbnail}")
                    asset_rows.append({
                        "name": str(metadata.get("minecraft_name") or metadata.get("asset_name") or command_name),
                        "type": category,
                        "thumbnail": f"{command_name}/{command_name}.png",
                        "gltf": f"{command_name}/{preview_model}",
                        "animated": animated,
                    })
                addon_name = f"{pack_name}.mcaddon"
                addon = pack_folder / addon_name
                data = {
                    "currentPack": pack_name,
                    "packs": nav_rows,
                    "mcaddon": addon_name if addon.is_file() else "",
                    "assets": asset_rows,
                }
                data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
                page = (
                    page_template
                    .replace("__TITLE__", _HTML_ESCAPE(pack_name))
                    .replace("__DATA__", data_json)
                    .replace("__CANVAS_WIDTH__", str(background_width))
                    .replace("__CANVAS_HEIGHT__", str(background_height))
                )
                if not (pack_folder / "banner.png").is_file():
                    # Text-only fallback: do not fabricate copyrighted/authored
                    # pack artwork, and do not expose a broken image.
                    page = page.replace(
                        '<img id="banner" src="banner.png" alt="">',
                        '<div id="banner" role="img" style="display:flex;align-items:center;'
                        'justify-content:center;font-size:26px;font-weight:bold">'
                        + _HTML_ESCAPE(pack_name) + '</div>',
                    )
                (pack_folder / "index.html").write_text(page, encoding="utf-8")
                generated_packs += 1
                total_assets += len(asset_rows)

            return _ok(packs=generated_packs, assets=total_assets, path=str(root_index_path),
                       missing_artwork=artwork_warnings)

        if operation == "asset-migration-gallery":
            from cf.ifserver.assets.asset_migration_path import generate_asset_gallery
            return _ok(generate_asset_gallery())
