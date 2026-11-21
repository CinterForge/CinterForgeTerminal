"""Authoritative Bedrock themed-pack assembler extracted from asset_command.

Input: legacy per-asset .mcaddon folders, each with asset.json. Output: the
same two nested RP/BP .mcpack archives and UUID derivation as the original
asset-migration-build-packs operation. This is a library, not a second pack
implementation; CLI and hosted consumers must delegate here.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Sequence


def build_themed_content_packs(
    stage_root: Path, *, pack_version: Sequence[int], output_root: Path | None = None,
) -> list[dict]:
    """Build complete .mcaddon files from already-exported Minecraft assets.

    Stage files remain the canonical inputs; optional output_root gives Terminal
    its Builds/Assets/ContentPacks/<pack>/<pack>.mcaddon deliverables. No
    conversion, data import, source updates, or remote I/O happens here.
    """
    root = Path(stage_root)
    if root.is_symlink() or not root.is_dir():
        raise FileNotFoundError(f"Missing staged per-asset MCAddons: {root}")
    if len(pack_version) != 3 or any(type(v) is not int or v < 0 for v in pack_version):
        raise ValueError("Expected Bedrock three-part integer pack version")
    if output_root is not None:
        output_root = Path(output_root)
        if output_root.is_symlink():
            raise ValueError(f"Content Packs output cannot be a symlink: {output_root}")
        output_root.mkdir(parents=True, exist_ok=True)
    built: list[dict] = []

    def pack_uuid(pack_name: str, suffix: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://cinterforge.com/minecraft/themed/{pack_name}/{suffix}"))

    for pack_folder in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name.casefold()):
        if pack_folder.is_symlink():
            raise ValueError(f"Staged pack directory cannot be a symlink: {pack_folder}")
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
            if asset_folder.is_symlink():
                raise ValueError(f"Staged asset directory cannot be a symlink: {asset_folder}")
            metadata = json.loads((asset_folder / "asset.json").read_text(encoding="utf-8"))
            command_name = str(metadata.get("command_name") or asset_folder.name)
            if (not re.fullmatch(r"[A-Za-z0-9._-]+", command_name)
                    or command_name in (".", "..")):
                raise ValueError(f"Unsafe Minecraft command name: {command_name}")
            addon = asset_folder / f"{command_name}.mcaddon"
            if addon.is_symlink():
                raise ValueError(f"Staged asset archive cannot be a symlink: {addon}")
            if not addon.is_file():
                raise FileNotFoundError(f"Themed pack source Add-On is missing: {addon}")
            if not icon_png:
                thumb = asset_folder / f"{command_name}.png"
                if thumb.is_symlink():
                    raise ValueError(f"Staged thumbnail cannot be a symlink: {thumb}")
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

        version = list(pack_version)
        rp_id = pack_uuid(pack_folder.name, "rp")
        rpm_id = pack_uuid(pack_folder.name, "rp-module")
        bp_id = pack_uuid(pack_folder.name, "bp")
        bpm_id = pack_uuid(pack_folder.name, "bp-module")

        rp_manifest = {
            "format_version": 2,
            "header": {
                "name": f"CinterForge - {pack_folder.name}",
                "description": f"CinterForge themed asset pack: {pack_folder.name}",
                "uuid": rp_id,
                "version": version,
                "min_engine_version": [1, 21, 80],
            },
            "modules": [
                {
                    "type": "resources",
                    "uuid": rpm_id,
                    "version": version,
                }
            ],
        }
        bp_manifest = {
            "format_version": 2,
            "header": {
                "name": f"CinterForge - {pack_folder.name}",
                "description": f"CinterForge themed asset pack: {pack_folder.name}",
                "uuid": bp_id,
                "version": version,
                "min_engine_version": [1, 21, 80],
            },
            "modules": [
                {
                    "type": "data",
                    "uuid": bpm_id,
                    "version": version,
                }
            ],
            "dependencies": [
                {
                    "uuid": rp_id,
                    "version": version,
                }
            ],
        }
        item_texture = {
            "resource_pack_name": f"CinterForge - {pack_folder.name}",
            "texture_name": "atlas.items",
            "texture_data": texture_data,
        }
        resource_files["textures/item_texture.json"] = (
            json.dumps(item_texture, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
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

            if output_root is not None:
                # Preserve the legacy MCAddons location for IFPlayer gallery
                # consumers. Commit the new ContentPacks artifact atomically.
                pack_output = output_root / pack_folder.name
                if pack_output.is_symlink() or (pack_output.exists() and not pack_output.is_dir()):
                    raise ValueError(f"Unsafe Content Pack output: {pack_output}")
                pack_output.mkdir(parents=True, exist_ok=True)
                selected = pack_output / destination.name
                if selected.is_symlink():
                    raise ValueError(f"Unsafe Content Pack target: {selected}")
                pending = pack_output / (destination.name + ".partial")
                try:
                    if pending.is_symlink():
                        raise ValueError(f"Unsafe Content Pack temporary file: {pending}")
                    shutil.copyfile(destination, pending)
                    pending.replace(selected)
                finally:
                    pending.unlink(missing_ok=True)
                destination = selected
            built.append({"name": pack_folder.name, "assets": len(asset_folders), "file": str(destination), "bytes": destination.stat().st_size})

    return built
