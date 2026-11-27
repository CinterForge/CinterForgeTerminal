#========= Copyright 2025+, CinterForge, Inc. All rights reserved. ============
"""Blockify, Minecraft add-on, and Blockbench asset editing implementation.

Extracted from asset_management so voxel conversion/editor round-trip concerns are
kept separate from saved-library persistence and cloud transport.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .asset_management import (
    BLOCKIFY_ADDON_PACK_VERSION,
    BLOCKIFY_ADDON_SCHEMA,
    _load_manifest,
    _normalize_version_artifacts,
    _resolve_folder,
    _safe_token,
    _sha256,
    _write_manifest,
    assets_root,
    log,
)

def _blockify_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", str(value or "").casefold()).strip("_")
    return slug[:48] or "blockified_asset"


def _uuid_from_seed(seed: str) -> str:
    raw = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x50
    raw[8] = (raw[8] & 0x3F) | 0x80
    h = raw.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    """Encode a small RGBA PNG without adding an image-library runtime dependency."""
    import struct
    import zlib

    if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
        raise ValueError("Invalid PNG dimensions/pixel buffer")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    scanlines = b"".join(b"\x00" + pixels[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def _blockify_front_icon_png(voxels: list[dict[str, int]], *, size: int = 64) -> bytes:
    """Render a small transparent front-view icon from Blockify voxels.

    This deliberately has no image-library dependency.  The icon is used by the
    generated Statue item so inventory/hotbar/hand show the Blockified asset
    rather than Minecraft's missing-item placeholder.
    """
    occupied = list(voxels or [])
    if not occupied:
        return _png_rgba(1, 1, bytes((0, 0, 0, 0)))

    min_x = min(v["x"] for v in occupied); max_x = max(v["x"] for v in occupied)
    min_y = min(v["y"] for v in occupied); max_y = max(v["y"] for v in occupied)
    # Front-most voxel wins for each x/y cell.  This gives a deterministic,
    # recognizable thumbnail while preserving the generated voxel colors.
    front: dict[tuple[int, int], dict[str, int]] = {}
    for voxel in occupied:
        key = (voxel["x"], voxel["y"])
        prior = front.get(key)
        if prior is None or voxel["z"] < prior["z"]:
            front[key] = voxel

    src_w = max_x - min_x + 1
    src_h = max_y - min_y + 1
    pad = max(2, size // 16)
    scale = max(1, min((size - 2 * pad) // max(1, src_w), (size - 2 * pad) // max(1, src_h)))
    draw_w = src_w * scale
    draw_h = src_h * scale
    ox = (size - draw_w) // 2
    oy = (size - draw_h) // 2
    rgba = bytearray(size * size * 4)

    for (x, y), voxel in front.items():
        px0 = ox + (x - min_x) * scale
        # Minecraft/geometry Y grows upward; PNG rows grow downward.
        py0 = oy + (max_y - y) * scale
        r, g, b = voxel["r"], voxel["g"], voxel["b"]
        # Small highlight on the top/left edge helps the icon read as voxels.
        hi = (min(255, r + 24), min(255, g + 24), min(255, b + 24), 255)
        base = (r, g, b, 255)
        for yy in range(py0, min(size, py0 + scale)):
            for xx in range(px0, min(size, px0 + scale)):
                color = hi if (xx == px0 or yy == py0) else base
                off = (yy * size + xx) * 4
                rgba[off:off + 4] = bytes(color)
    return _png_rgba(size, size, bytes(rgba))



def _blockify_fill_polygon(rgba: bytearray, size: int, points: list[tuple[float, float]], color: tuple[int, int, int, int]) -> None:
    """Tiny dependency-free convex polygon rasterizer for generated Minecraft artwork."""
    if len(points) < 3:
        return
    min_y = max(0, int(min(y for _, y in points)))
    max_y = min(size - 1, int(max(y for _, y in points)) + 1)
    for py in range(min_y, max_y + 1):
        scan_y = py + 0.5
        xs: list[float] = []
        for i, (x1, y1) in enumerate(points):
            x2, y2 = points[(i + 1) % len(points)]
            if (y1 <= scan_y < y2) or (y2 <= scan_y < y1):
                dy = y2 - y1
                if abs(dy) > 1e-9:
                    xs.append(x1 + (scan_y - y1) * (x2 - x1) / dy)
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            start = max(0, int(xs[i]))
            end = min(size - 1, int(xs[i + 1]) + 1)
            for px in range(start, end + 1):
                off = (py * size + px) * 4
                rgba[off:off + 4] = bytes(color)


def _blockify_hero_icon_png(voxels: list[dict[str, int]], *, size: int = 256, transparent: bool = False) -> bytes:
    """Render a square three-quarter hero view from the frame-zero voxel geometry.

    This deliberately mirrors the Minecraft preview's useful default framing:
    the asset alone determines the shot; no ground/environment bounds participate.
    """
    occupied = list(voxels or [])
    if not occupied:
        return _png_rgba(1, 1, bytes((0, 0, 0, 0)))

    bg = (0, 0, 0, 0) if transparent else (76, 127, 183, 255)
    rgba = bytearray(bg * (size * size))
    # Orthographic three-quarter projection.  These basis vectors give a view
    # close to the hero orbit used by the Babylon Minecraft preview.
    def project(x: float, y: float, z: float) -> tuple[float, float]:
        return ((x - z) * 0.8660254, (x + z) * 0.38 - y * 1.08)

    corners: list[tuple[float, float]] = []
    for v in occupied:
        x, y, z = float(v['x']), float(v['y']), float(v['z'])
        for dx in (0.0, 1.0):
            for dy in (0.0, 1.0):
                for dz in (0.0, 1.0):
                    corners.append(project(x + dx, y + dy, z + dz))
    min_x=min(x for x,_ in corners); max_x=max(x for x,_ in corners)
    min_y=min(y for _,y in corners); max_y=max(y for _,y in corners)
    pad=max(4, size//14)
    span_x=max(1e-6,max_x-min_x); span_y=max(1e-6,max_y-min_y)
    scale=min((size-2*pad)/span_x,(size-2*pad)/span_y)
    ox=(size-(min_x+max_x)*scale)/2.0
    oy=(size-(min_y+max_y)*scale)/2.0
    def screen(p: tuple[float,float]) -> tuple[float,float]:
        return (ox+p[0]*scale, oy+p[1]*scale)

    # Painter order: back/high-depth cubes first, then nearer cubes.
    ordered=sorted(occupied,key=lambda v:(float(v['x'])+float(v['z'])+float(v['y'])*0.02))
    for v in ordered:
        x,y,z=float(v['x']),float(v['y']),float(v['z'])
        r,g,b=int(v['r']),int(v['g']),int(v['b'])
        top=(min(255,r+28),min(255,g+28),min(255,b+28),255)
        left=(max(0,int(r*0.82)),max(0,int(g*0.82)),max(0,int(b*0.82)),255)
        right=(max(0,int(r*0.66)),max(0,int(g*0.66)),max(0,int(b*0.66)),255)
        p000=screen(project(x,y,z)); p100=screen(project(x+1,y,z)); p001=screen(project(x,y,z+1));
        p101=screen(project(x+1,y,z+1)); p010=screen(project(x,y+1,z)); p110=screen(project(x+1,y+1,z));
        p011=screen(project(x,y+1,z+1)); p111=screen(project(x+1,y+1,z+1))
        _blockify_fill_polygon(rgba,size,[p010,p110,p111,p011],top)
        _blockify_fill_polygon(rgba,size,[p000,p010,p011,p001],left)
        _blockify_fill_polygon(rgba,size,[p000,p100,p110,p010],right)
    return _png_rgba(size,size,bytes(rgba))


def _blockify_egg_icon_png(voxels: list[dict[str, int]], *, size: int = 64) -> bytes:
    """Generate a transparent inventory/hand image for an animated entity egg."""
    # Use the same frame-zero hero render rather than a generic Minecraft egg so
    # the item is visually tied to the actual asset in chest/hotbar/hand views.
    return _blockify_hero_icon_png(voxels, size=size, transparent=True)

def _animation_motion_kind(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(name or "").casefold()).strip()
    if re.search(r"\b(run|running|sprint|sprinting|jog|jogging)\b", text):
        return "run"
    if re.search(r"\b(walk|walking)\b", text):
        return "walk"
    return "stationary"


def _clean_behavior_profile(raw: Any, animations: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the one behavior model shared by previews and Bedrock export."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(list(raw or [])):
        if not isinstance(item, dict):
            continue
        clip = str(item.get("clip") or item.get("id") or "").strip()
        if not clip or clip not in animations or clip in seen:
            continue
        seen.add(clip)
        definition = animations.get(clip) if isinstance(animations.get(clip), dict) else {}
        definition_label = str(definition.get("label") or "").strip()
        profile_name = str(item.get("name") or "").strip()

        # The currently captured animation identity is authoritative. Saved
        # behavior profiles are allowed to tune a clip, but they must never
        # reassign locomotion semantics after animations are reordered or
        # replaced (for example, an old profile saying clip_2 is Walk while
        # the current capture labels clip_3 as Walk). Generic clip_N labels
        # carry no semantics, so only those may fall back to the saved name.
        generic_definition_label = bool(re.fullmatch(r"(?:clip|animation)[ _-]*\d+", definition_label.casefold()))
        name = (profile_name if generic_definition_label and profile_name else definition_label or profile_name or clip).strip() or clip
        kind = _animation_motion_kind(name)
        saved_kind = str(item.get("kind") or "").strip().casefold()
        if kind == "stationary" and generic_definition_label and not profile_name and saved_kind in {"walk", "run", "stationary"}:
            kind = saved_kind

        try:
            clip_length = max(0.033, float(definition.get("animation_length") or item.get("clip_length") or 1.0))
        except Exception:
            clip_length = 1.0

        default_cycles = 2 if kind in {"walk", "run"} else 1
        # Preserve saved tuning only when the saved semantic role still agrees
        # with the current animation. A stale role must not drag its old cycle
        # count or speed onto a newly assigned clip.
        semantics_match = saved_kind == kind or not saved_kind
        try:
            cycles = max(1, min(8, int(item.get("cycles") if semantics_match and item.get("cycles") is not None else default_cycles)))
        except Exception:
            cycles = default_cycles

        logical_speed = 1.65 if kind == "run" else (0.72 if kind == "walk" else 0.0)
        if kind in {"walk", "run"} and semantics_match and item.get("speed") is not None:
            try:
                logical_speed = max(0.0, float(item.get("speed")))
            except Exception:
                pass
        rows.append({
            "clip": clip,
            "name": name,
            "kind": kind,
            "clip_length": clip_length,
            "cycles": cycles,
            "hold_seconds": max(0.033, clip_length * cycles),
            "speed": logical_speed,
            "variant": len(rows) + 1,
        })
    return rows


def _write_blockify_mcaddon(
    destination: Path, *, source: Path, source_sha256: str, width: int, height: int, depth: int,
    voxels: list[dict[str, Any]], character_voxels: list[dict[str, Any]] | None = None,
    character_rig: dict[str, Any] | None = None, character_animations: dict[str, Any] | None = None,
    behavior_profile: list[dict[str, Any]] | None = None, product_name: str = "",
    blockbench_override: dict[str, Any] | None = None, item_icon_png: bytes | None = None,
    command_name: str = "",
) -> dict[str, Any]:
    """Create the Bedrock package from the same animation/behavior data used by previews.

    Static assets retain their existing placeable representation.
    Rigged animated assets export exactly one placeable item/entity using the
    asset name itself (for example ``cinter:defaultcharacter``). Character voxels
    keep exact source-bone ownership and captured animation clips are emitted as
    native Bedrock bone animations.
    """
    import math

    display_name = str(product_name or "").strip() or source.stem.replace("_", " ").strip() or "3D Asset"
    title_name = " ".join(word[:1].upper() + word[1:] for word in display_name.split())
    slug = _blockify_slug(command_name or display_name)
    rig = character_rig if isinstance(character_rig, dict) else {}
    rig_bones = [b for b in list(rig.get("bones") or []) if isinstance(b, dict) and str(b.get("name") or "")]
    rig_names = {str(b.get("name") or "") for b in rig_bones}
    animated = bool(rig_bones and character_voxels)
    # Stable, command-friendly identifiers. Animated assets intentionally expose
    # one Minecraft object only: the placeable item and the live entity share the
    # same identifier, e.g. ``cinter:defaultcharacter``. There is no separate
    # statue for animated assets. Static assets keep their existing statue form.
    # Every migrated asset has one command-stable public identifier.  Static and
    # animated assets both use cinter:<Command Name>; implementation details such
    # as "statue" must never leak into the player-facing /give command.
    statue_entity_identifier = "" if animated else f"cinter:{slug}"
    statue_item_identifier = statue_entity_identifier
    entity_identifier = f"cinter:{slug}"
    egg_item_identifier = entity_identifier if animated else ""
    character_geometry_id = f"geometry.cinterforge.{slug}.character" if animated else f"geometry.cinterforge.{slug}"
    statue_geometry_id = f"geometry.cinterforge.{slug}.statue" if animated else character_geometry_id
    render_controller_id = f"controller.render.cinterforge_{slug}"
    seed = f"{source_sha256}:{width}:{height}:{depth}:16:addon-assets-v3:{slug}"
    rp_uuid = _uuid_from_seed(seed + ":rp")
    rp_module_uuid = _uuid_from_seed(seed + ":rp-module")
    bp_uuid = _uuid_from_seed(seed + ":bp")
    bp_module_uuid = _uuid_from_seed(seed + ":bp-module")

    statue_occupied = list(voxels or [])
    character_occupied = list(character_voxels or statue_occupied)
    if not statue_occupied:
        raise ValueError("Cannot build a Minecraft Add-On from an empty Blockify result")
    if animated and not character_occupied:
        raise ValueError("Animated Blockify result has no character voxels")

    def build_geometry(occupied: list[dict[str, Any]], geometry_id: str, *, articulated: bool):
        if articulated:
            # The browser-side voxelizer deliberately permits a small number of
            # surface samples with no exact skin owner (<= 8%). Those samples
            # remain in the static voxelization, but assigning them to an
            # articulated Bedrock bone would invent ownership.
            unresolved = [v for v in occupied if str(v.get("bone") or "") not in rig_names]
            unresolved_ratio = len(unresolved) / len(occupied) if occupied else 1.0
            if unresolved_ratio > 0.08:
                raise ValueError(
                    f"Animated Blockify character has {len(unresolved)} of {len(occupied)} voxels "
                    f"without exact source-bone ownership ({unresolved_ratio * 100:.1f}%)"
                )
            occupied = [v for v in occupied if str(v.get("bone") or "") in rig_names]
            if not occupied:
                raise ValueError("Animated Blockify character has no voxels with exact source-bone ownership")
        min_x = min(v["x"] for v in occupied); max_x = max(v["x"] for v in occupied)
        min_y = min(v["y"] for v in occupied); max_y = max(v["y"] for v in occupied)
        min_z = min(v["z"] for v in occupied); max_z = max(v["z"] for v in occupied)
        # Voxel centers in the Babylon preview are centered around
        # (min+max)/2.  Cube origins below account for their 1-voxel size
        # separately; bone pivots must use this exact center or every Bedrock
        # rotation is displaced by half a voxel in X/Z.
        center_x = (min_x + max_x) / 2.0
        center_z = (min_z + max_z) / 2.0
        atlas_w = max(1, int(math.ceil(math.sqrt(len(occupied)))))
        atlas_h = max(1, int(math.ceil(len(occupied) / atlas_w)))
        rgba = bytearray(atlas_w * atlas_h * 4)
        cubes_by_bone: dict[str, list[dict[str, Any]]]
        if articulated:
            cubes_by_bone = {name: [] for name in rig_names}
        else:
            cubes_by_bone = {"root": []}
        for index, voxel in enumerate(occupied):
            u, vv = index % atlas_w, index // atlas_w
            off = (vv * atlas_w + u) * 4
            rgba[off:off + 4] = bytes((voxel["r"], voxel["g"], voxel["b"], 255))
            face = {"uv": [u, vv], "uv_size": [1, 1]}
            # Match the recovered source-rig work: flip X/Z once into Bedrock's
            # forward convention instead of rotating a root bone later.
            # Cubes are specified by their minimum corner.  The intended
            # center is -(voxel-center), matching RenderAnimatedBlockifiedBehavior.
            ox = -(voxel["x"] - center_x) - 0.5
            oy = voxel["y"] - min_y
            oz = -(voxel["z"] - center_z) - 0.5
            bone = str(voxel.get("bone") or "") if articulated else "root"
            cubes_by_bone[bone].append({
                "origin": [ox, oy, oz], "size": [1, 1, 1],
                "uv": {name: dict(face) for name in ("north", "south", "east", "west", "up", "down")},
            })
        if articulated:
            wrapper = "cinterforge_root"
            bones: list[dict[str, Any]] = [{"name": wrapper, "pivot": [0, 0, 0]}]
            for raw in rig_bones:
                name = str(raw.get("name") or "")
                parent = str(raw.get("parent") or "")
                pivot_raw = raw.get("pivot") if isinstance(raw.get("pivot"), list) else None
                if pivot_raw and len(pivot_raw) >= 3:
                    px, py, pz = map(float, pivot_raw[:3])
                    pivot = [-(px - center_x), py - min_y, -(pz - center_z)]
                else:
                    pivot = [0.0, 0.0, 0.0]
                entry: dict[str, Any] = {"name": name, "parent": wrapper, "pivot": pivot}
                if cubes_by_bone.get(name):
                    entry["cubes"] = cubes_by_bone[name]
                bones.append(entry)
        else:
            bones = [{"name": "root", "pivot": [0, 0, 0], "cubes": cubes_by_bone["root"]}]
        geometry = {
            "format_version": "1.12.0",
            "minecraft:geometry": [{
                "description": {
                    "identifier": geometry_id, "texture_width": atlas_w, "texture_height": atlas_h,
                    "visible_bounds_width": max(1.0, float(max(width, depth)) + 0.5),
                    "visible_bounds_height": max(1.0, float(height) + 0.5),
                    "visible_bounds_offset": [0, float(height) / 2.0, 0],
                },
                "bones": bones,
            }],
        }
        return geometry, _png_rgba(atlas_w, atlas_h, bytes(rgba)), (min_x, max_x, min_y, max_y, min_z, max_z)

    statue_geometry, statue_texture_png, statue_bounds = build_geometry(statue_occupied, statue_geometry_id, articulated=False)
    if animated:
        character_geometry, character_texture_png, character_bounds = build_geometry(character_occupied, character_geometry_id, articulated=True)
    else:
        character_geometry, character_texture_png, character_bounds = statue_geometry, statue_texture_png, statue_bounds

    raw_animations = character_animations if isinstance(character_animations, dict) else {}
    animations_out: dict[str, Any] = {}
    clip_to_client_key: dict[str, str] = {}
    # The in-app Block preview makes the persistent motion root the sole owner
    # of global X/Z locomotion.  The saved flat world-space bake still contains
    # the source clip's authored horizontal root travel in every bone position,
    # so normalize that same shared X/Z component here before writing Bedrock.
    # Keep Y untouched: jump/crouch/bob are pose animation, not entity motion.
    # Candidate order matters.  Blockify rigs include a synthetic
    # ``cinterforge_root`` wrapper before the authored skeleton.  That wrapper
    # is not the Mixamo locomotion joint and usually has no captured animation
    # position channel, so it must never shadow Hips/Pelvis simply because its
    # terminal token is "root".
    def _locomotion_token(value: Any) -> str:
        token = str(value or "").strip().casefold().replace("\\", "/")
        for separator in ("|", "/", ":"):
            if separator in token:
                token = token.rsplit(separator, 1)[-1]
        if token.startswith("mixamorig"):
            token = token[len("mixamorig"):].lstrip("_-. ")
        return token

    locomotion_candidates: list[str] = []
    for preferred in ("hips", "pelvis", "root"):
        for bone in rig_bones:
            name = str(bone.get("name") or "")
            if not name or name in locomotion_candidates:
                continue
            canonical = str(bone.get("canonical") or "").strip().casefold()
            tokens = {_locomotion_token(name), _locomotion_token(bone.get("source_name"))}
            if canonical == preferred or preferred in tokens:
                locomotion_candidates.append(name)

    def _position_at(channel: dict[str, Any], time_key: str) -> list[float]:
        value = channel.get(str(time_key))
        if isinstance(value, list) and len(value) >= 3:
            return value
        try:
            target = float(time_key)
        except Exception:
            target = 0.0
        candidates: list[tuple[float, list[float]]] = []
        for key, row in channel.items():
            if not isinstance(row, list) or len(row) < 3:
                continue
            try:
                candidates.append((float(key), row))
            except Exception:
                continue
        if not candidates:
            return [0.0, 0.0, 0.0]
        return min(candidates, key=lambda pair: abs(pair[0] - target))[1]

    for clip_key, raw in raw_animations.items():
        if not isinstance(raw, dict):
            continue
        src_bones = raw.get("bones") if isinstance(raw.get("bones"), dict) else {}
        locomotion_position: dict[str, Any] = {}
        # Choose the first preferred candidate that is actually animated in this
        # clip.  This keeps locomotion selection data-driven and prevents a
        # synthetic wrapper/root with no position keys from disabling X/Z
        # normalization for the authored Hips channel.
        for locomotion_bone_name in locomotion_candidates:
            locomotion_channels = src_bones.get(locomotion_bone_name) if isinstance(src_bones.get(locomotion_bone_name), dict) else {}
            candidate_position = locomotion_channels.get("position") if isinstance(locomotion_channels.get("position"), dict) else {}
            if candidate_position:
                locomotion_position = candidate_position
                break
        clean_bones: dict[str, Any] = {}
        for bone_name, channels in src_bones.items():
            if str(bone_name) not in rig_names or not isinstance(channels, dict):
                continue
            rotation = channels.get("rotation") if isinstance(channels.get("rotation"), dict) else None
            position = channels.get("position") if isinstance(channels.get("position"), dict) else None
            clean_channels: dict[str, Any] = {}
            if rotation:
                clean_rotation: dict[str, list[float]] = {}
                for time_key, value in rotation.items():
                    if not isinstance(value, list) or len(value) < 3:
                        continue
                    try:
                        # CinterForge stores the flat-baked rotation in the Babylon/voxel
                        # basis. Blockbench/Minecraft Bedrock animation JSON needs the
                        # corresponding raw Bedrock Euler signs: X and Z are inverted,
                        # Y is preserved. This is independent of the position channel.
                        clean_rotation[str(time_key)] = [
                            round(-float(value[0]), 4),
                            round(float(value[1]), 4),
                            round(-float(value[2]), 4),
                        ]
                    except Exception:
                        continue
                if clean_rotation:
                    clean_channels["rotation"] = clean_rotation
            if position:
                clean_position: dict[str, list[float]] = {}
                for time_key, value in position.items():
                    if not isinstance(value, list) or len(value) < 3:
                        continue
                    try:
                        root = _position_at(locomotion_position, str(time_key)) if locomotion_position else [0.0, 0.0, 0.0]
                        desired = [
                            float(value[0]) - float(root[0]),
                            float(value[1]),
                            float(value[2]) - float(root[2]),
                        ]
                        # Bedrock position is the bone's translation channel relative
                        # to its bind pivot.  The flat bake already stores the desired
                        # entity-space pivot displacement, so emit it directly.  Do not
                        # rotate this vector by the animation rotation; doing so turns
                        # raised near-90-degree arms into large downward displacements.
                        clean_position[str(time_key)] = [round(float(axis), 4) for axis in desired]
                    except Exception:
                        continue
                if clean_position:
                    clean_channels["position"] = clean_position
            if clean_channels:
                clean_bones[str(bone_name)] = clean_channels
        if not clean_bones:
            continue
        safe_key = _safe_token(str(clip_key), fallback=f"clip_{len(animations_out)+1}").casefold()
        animation_id = f"animation.cinterforge.{slug}.{safe_key}"
        try:
            length = max(0.033, float(raw.get("animation_length") or 0.033))
        except Exception:
            length = 0.033
        animations_out[str(clip_key)] = {
            "id": animation_id,
            "label": str(raw.get("label") or clip_key),
            "animation_length": length,
            "bones": clean_bones,
        }
        clip_to_client_key[str(clip_key)] = safe_key

    behavior = _clean_behavior_profile(behavior_profile, animations_out)
    # Any captured clip omitted from a supplied profile still participates using
    # the same name-based locomotion classification used by the previews.
    profiled = {row["clip"] for row in behavior}
    for clip_key, definition in animations_out.items():
        if clip_key in profiled:
            continue
        label = str(definition.get("label") or clip_key)
        kind = _animation_motion_kind(label)
        length = float(definition.get("animation_length") or 1.0)
        behavior.append({
            "clip": clip_key, "name": label, "kind": kind, "clip_length": length,
            "cycles": 2 if kind in {"walk", "run"} else 1,
            "hold_seconds": length * (2 if kind in {"walk", "run"} else 1),
            "speed": 1.65 if kind == "run" else (0.72 if kind == "walk" else 0.0),
            "variant": len(behavior) + 1,
        })

    character_texture_name = f"cinterforge_{slug}"
    statue_texture_name = f"cinterforge_{slug}_statue"
    character_texture_path = f"textures/entity/{character_texture_name}"
    statue_texture_path = f"textures/entity/{statue_texture_name}"
    icon_name = f"cinterforge_{slug}_statue"
    egg_icon_name = f"cinterforge_{slug}" if animated else ""
    pack_title = f"CinterForge {title_name} - {'Animated' if animated else '3D'}"

    bedrock_animation_blend_seconds = 0.18
    bedrock_animations: dict[str, Any] = {}
    client_refs: dict[str, str] = {}
    animate_rules: list[Any] = []
    for row in behavior:
        clip = animations_out.get(row["clip"])
        if not clip:
            continue
        safe_key = clip_to_client_key[row["clip"]]
        anim_id = str(clip["id"])
        # CinterForge strips shared Hips X/Z root travel from every exported
        # locomotion bone, so Walk/Run may safely repeat in place while Bedrock's
        # entity movement/navigation owns world-space translation.  This is the
        # key separation: the skeleton loops; the entity position never resets.
        # Stationary clips hold their final pose until the next controller state
        # arrives, avoiding a bind-pose flash during replication gaps.
        loop = True if str(row.get("kind") or "") in {"walk", "run"} else "hold_on_last_frame"
        bedrock_animations[anim_id] = {"loop": loop, "animation_length": clip["animation_length"], "bones": clip["bones"]}
        client_refs[safe_key] = anim_id
        animate_rules.append({safe_key: f"query.variant == {int(row['variant'])}"})

    animation_controller_id = f"controller.animation.cinterforge_{slug}.cycle"
    animation_controller: dict[str, Any] | None = None
    if animated and behavior and client_refs:
        states: dict[str, Any] = {}
        valid_rows = [row for row in behavior if row.get("clip") in animations_out and clip_to_client_key.get(str(row.get("clip"))) in client_refs]

        def _controller_state_name(row_index: int, row: dict[str, Any]) -> str:
            return f"clip_{row_index}"

        for index, row in enumerate(valid_rows):
            safe_key = clip_to_client_key[str(row["clip"])]
            state_name = _controller_state_name(index, row)
            transitions = []
            # Locomotion is intentionally one-shot in Bedrock.  Never transition
            # back into the same Walk/Run clip to simulate looping: re-entering
            # an animation state necessarily restarts that clip at frame zero.
            # A different server-selected variant blends directly out of the
            # current authored pose; if replication is late, hold_on_last_frame
            # keeps the final pose visible instead of exposing a reset.
            for other_index, other in enumerate(valid_rows):
                if other_index == index:
                    continue
                transitions.append({
                    _controller_state_name(other_index, other):
                    f"query.variant == {int(other['variant'])}"
                })
            state: dict[str, Any] = {
                "animations": [safe_key],
                "blend_transition": bedrock_animation_blend_seconds,
                "blend_via_shortest_path": True,
            }
            if transitions:
                state["transitions"] = transitions
            states[state_name] = state
        if states:
            first_state = _controller_state_name(0, valid_rows[0])
            animation_controller = {
                # query.state_time is available in animation controllers from
                # Bedrock format 1.21.20 onward.
                "format_version": "1.21.20",
                "animation_controllers": {
                    animation_controller_id: {
                        "initial_state": first_state,
                        "states": states,
                    }
                },
            }

    # Optional Blockbench round-trip edits override the generated Bedrock-facing
    # geometry/texture/animation files only.  The canonical CinterForge voxel
    # bake remains intact so Cancel/Reset can always return to generated data.
    override = blockbench_override if isinstance(blockbench_override, dict) else {}
    override_geometry = override.get("geometry") if isinstance(override.get("geometry"), dict) else None
    if override_geometry:
        override_geometry = json.loads(json.dumps(override_geometry))
        try:
            rows = override_geometry.get("minecraft:geometry") or []
            if rows and isinstance(rows[0], dict):
                description = rows[0].setdefault("description", {})
                description["identifier"] = character_geometry_id if animated else statue_geometry_id
            if animated:
                character_geometry = override_geometry
            else:
                statue_geometry = override_geometry
                character_geometry = override_geometry
        except Exception:
            pass
    override_texture = str(override.get("texture_data_url") or "")
    if override_texture.startswith("data:image/png;base64,"):
        try:
            edited_png = base64.b64decode(override_texture.split(",", 1)[1], validate=True)
            if edited_png.startswith(b"\x89PNG\r\n\x1a\n"):
                if animated:
                    character_texture_png = edited_png
                else:
                    statue_texture_png = edited_png
                    character_texture_png = edited_png
        except Exception:
            pass
    override_animations = override.get("animations") if isinstance(override.get("animations"), dict) else None
    if animated and override_animations and isinstance(override_animations.get("animations"), dict):
        edited_animations = override_animations.get("animations") or {}
        # Client/controller references are generated from the canonical clip IDs.
        # Save validation guarantees these keys remain present; preserve any
        # additional Blockbench-authored animations as well.  However, a saved
        # Blockbench document may have been created by an older exporter and can
        # therefore carry obsolete Bedrock playback semantics (for example
        # ``loop: true`` plus baked Hips X/Z travel).  Blockbench owns authored
        # keyframe edits, but CinterForge owns runtime transport semantics.  Reapply
        # those semantics after accepting the edited animation table so a stale
        # round-trip cannot silently reintroduce a frame-zero reset.
        if all(str(defn.get("id") or "") in edited_animations for defn in animations_out.values()):
            merged_animations = json.loads(json.dumps(edited_animations))
            behavior_by_id: dict[str, dict[str, Any]] = {}
            for row in behavior:
                definition = animations_out.get(str(row.get("clip") or ""))
                if isinstance(definition, dict) and definition.get("id"):
                    behavior_by_id[str(definition["id"])] = row

            for anim_id, canonical in bedrock_animations.items():
                edited = merged_animations.get(anim_id)
                if not isinstance(edited, dict):
                    continue
                # Loop/hold is a runtime contract, not an editable Blockbench
                # property.  Always preserve the current exporter contract.
                edited["loop"] = canonical.get("loop", "hold_on_last_frame")

                row = behavior_by_id.get(anim_id) or {}
                if str(row.get("kind") or "") not in {"walk", "run"}:
                    continue
                bones = edited.get("bones") if isinstance(edited.get("bones"), dict) else {}
                locomotion_position: dict[str, Any] = {}
                for locomotion_bone_name in locomotion_candidates:
                    channels = bones.get(locomotion_bone_name) if isinstance(bones.get(locomotion_bone_name), dict) else {}
                    candidate = channels.get("position") if isinstance(channels.get("position"), dict) else {}
                    if candidate:
                        locomotion_position = candidate
                        break
                if not locomotion_position:
                    continue
                for channels in bones.values():
                    if not isinstance(channels, dict):
                        continue
                    position = channels.get("position") if isinstance(channels.get("position"), dict) else None
                    if not position:
                        continue
                    normalized: dict[str, list[float]] = {}
                    for time_key, value in position.items():
                        if not isinstance(value, list) or len(value) < 3:
                            continue
                        try:
                            root = _position_at(locomotion_position, str(time_key))
                            normalized[str(time_key)] = [
                                round(float(value[0]) - float(root[0]), 4),
                                round(float(value[1]), 4),
                                round(float(value[2]) - float(root[2]), 4),
                            ]
                        except Exception:
                            continue
                    if normalized:
                        channels["position"] = normalized
            bedrock_animations = merged_animations

    def client_entity(identifier: str, *, spawn_egg: bool, animate: bool, geometry_id: str, texture_path: str) -> dict[str, Any]:
        description: dict[str, Any] = {
            "identifier": identifier,
            "materials": {"default": "entity"}, "textures": {"default": texture_path},
            "geometry": {"default": geometry_id}, "render_controllers": [render_controller_id],
        }
        if animate and client_refs:
            refs = dict(client_refs)
            if animation_controller is not None:
                refs["cycle_controller"] = animation_controller_id
                description["scripts"] = {"animate": ["cycle_controller"]}
            else:
                description["scripts"] = {"animate": animate_rules}
            description["animations"] = refs
        if spawn_egg:
            avg_r = sum(v["r"] for v in character_occupied) // len(character_occupied)
            avg_g = sum(v["g"] for v in character_occupied) // len(character_occupied)
            avg_b = sum(v["b"] for v in character_occupied) // len(character_occupied)
            description["spawn_egg"] = {
                "base_color": f"#{avg_r:02X}{avg_g:02X}{avg_b:02X}",
                "overlay_color": f"#{min(255, avg_r+64):02X}{min(255, avg_g+64):02X}{min(255, avg_b+64):02X}",
            }
        return {"format_version": "1.10.0", "minecraft:client_entity": {"description": description}}

    animated_client = client_entity(entity_identifier, spawn_egg=False, animate=animated, geometry_id=character_geometry_id, texture_path=character_texture_path)
    statue_client = None if animated else client_entity(statue_entity_identifier, spawn_egg=False, animate=False, geometry_id=statue_geometry_id, texture_path=statue_texture_path)
    render_controller = {"format_version": "1.8.0", "render_controllers": {render_controller_id: {"geometry": "Geometry.default", "materials": [{"*": "Material.default"}], "textures": ["Texture.default"]}}}

    min_x, max_x, min_y, max_y, min_z, max_z = character_bounds
    collision_width = max(0.1, float(max(max_x-min_x+1, max_z-min_z+1)) / 16.0)
    collision_height = max(0.1, float(max_y-min_y+1) / 16.0)
    base_components: dict[str, Any] = {
        "minecraft:physics": {}, "minecraft:collision_box": {"width": collision_width, "height": collision_height},
        "minecraft:health": {"value": 20, "max": 20}, "minecraft:nameable": {},
    }
    component_groups: dict[str, Any] = {}
    events: dict[str, Any] = {}
    if animated and behavior:
        # Keep the navigation brain alive across animation-state changes.  If
        # random_stroll/navigation are removed and recreated with every clip,
        # Bedrock discards the active destination/path and a later Walk/Run
        # starts a new trip.  Persistent navigation plus per-state movement
        # speed lets stationary clips pause in place and locomotion clips resume
        # from the same world position toward the same destination.
        base_components.update({
            "minecraft:movement.basic": {},
            "minecraft:navigation.walk": {"can_path_over_water": False, "avoid_water": True},
            "minecraft:behavior.random_stroll": {"priority": 6, "speed_multiplier": 1.0, "interval": 20, "xz_dist": 10, "y_dist": 4},
            "minecraft:jump.static": {},
        })
        group_names = [f"cinter:clip_{index}" for index, _ in enumerate(behavior)]
        for index, row in enumerate(behavior):
            group = group_names[index]
            next_event = f"cinter:next_animation_{index}"
            movement = 0.0
            if row["kind"] in {"walk", "run"}:
                # Logical walk/run classification and speed ratio come from the
                # same saved behavior profile used by both Babylon previews.
                # Bedrock's own entity examples use ~0.25 as an ordinary walking
                # speed.  Use that as the Walk baseline, preserve the authored
                # Walk/Run ratio, and cap Run conservatively so the navigation
                # controller remains stable while the in-place gait supplies the
                # visual cadence.
                movement = max(0.10, min(0.45, 0.25 * (float(row["speed"]) / 0.72)))
            # Locomotion may span multiple authored gait cycles.  The animation
            # loops in place while persistent Bedrock navigation moves the entity
            # through the world, so keep the logical state's full requested hold
            # duration instead of forcing a transition after one stride.
            timer_seconds = max(0.05, float(row["hold_seconds"]))
            components: dict[str, Any] = {
                "minecraft:variant": {"value": int(row["variant"])},
                "minecraft:timer": {"time": timer_seconds, "looping": False, "time_down_event": {"event": next_event, "target": "self"}},
                # Only speed changes with the clip.  Navigation/pathfinding stays
                # resident in base_components so clip switches do not throw away
                # the current destination.
                "minecraft:movement": {"value": movement},
            }
            component_groups[group] = components

        def randomize_event(exclude_index: int | None) -> dict[str, Any]:
            candidates = [name for i, name in enumerate(group_names) if exclude_index is None or i != exclude_index]
            if not candidates:
                candidates = list(group_names)
            return {
                "sequence": [
                    {"remove": {"component_groups": group_names}},
                    {"randomize": [{"weight": 1, "add": {"component_groups": [name]}} for name in candidates]},
                ]
            }

        # Startup may choose any clip. Thereafter each current clip transitions
        # through its own event that excludes itself when another clip exists.
        # This matches Babylon's auto-cycle rule and avoids visible immediate
        # repeats while preserving entity position/orientation across clips.
        base_components["minecraft:timer"] = {"time": 0.05, "looping": False, "time_down_event": {"event": "cinter:start_animation", "target": "self"}}
        events["cinter:start_animation"] = randomize_event(None)
        for index in range(len(group_names)):
            events[f"cinter:next_animation_{index}"] = randomize_event(index)

    animated_behavior_entity: dict[str, Any] = {
        "description": {"identifier": entity_identifier, "is_spawnable": True, "is_summonable": True, "is_experimental": False},
        "components": base_components,
    }
    if component_groups:
        animated_behavior_entity["component_groups"] = component_groups
    if events:
        animated_behavior_entity["events"] = events
    animated_behavior = {"format_version": "1.21.80", "minecraft:entity": animated_behavior_entity}
    statue_behavior = {
        "format_version": "1.21.80",
        "minecraft:entity": {
            "description": {"identifier": statue_entity_identifier, "is_spawnable": False, "is_summonable": True, "is_experimental": False},
            "components": {"minecraft:physics": {}, "minecraft:collision_box": {"width": collision_width, "height": collision_height}, "minecraft:pushable": {"is_pushable": False, "is_pushable_by_piston": False}},
        },
    }
    statue_item = {
        "format_version": "1.21.80",
        "minecraft:item": {
            "description": {"identifier": statue_item_identifier, "menu_category": {"category": "construction"}},
            "components": {
                "minecraft:display_name": {"value": f"{title_name} Statue"},
                "minecraft:icon": {"textures": {"default": icon_name}}, "minecraft:max_stack_size": 16,
                "minecraft:entity_placer": {"entity": statue_entity_identifier},
            },
        },
    }
    egg_item: dict[str, Any] | None = None
    if animated:
        egg_item = {
            "format_version": "1.21.80",
            "minecraft:item": {
                "description": {"identifier": egg_item_identifier, "menu_category": {"category": "items"}},
                "components": {
                    "minecraft:display_name": {"value": title_name},
                    "minecraft:icon": {"textures": {"default": egg_icon_name}},
                    "minecraft:max_stack_size": 16,
                    "minecraft:hand_equipped": True,
                    "minecraft:entity_placer": {"entity": entity_identifier},
                },
            },
        }
    if animated:
        texture_data: dict[str, Any] = {egg_icon_name: {"textures": f"textures/items/{egg_icon_name}_chest"}}
        lang_lines = [f"entity.{entity_identifier}.name={title_name}"]
    else:
        texture_data = {icon_name: {"textures": f"textures/items/{icon_name}_chest"}}
        lang_lines = [f"entity.{statue_entity_identifier}.name={title_name} Statue"]
    item_texture = {"resource_pack_name": pack_title, "texture_name": "atlas.items", "texture_data": texture_data}
    lang = "\n".join(lang_lines + [""])
    languages = ["en_US"]
    # Both resource- and behavior-pack thumbnails are the same improved
    # frame-zero hero view used to identify the generated asset.
    pack_icon_png = _blockify_hero_icon_png(statue_occupied, size=256, transparent=False)
    rendered_item_icon = bytes(item_icon_png or b"")
    statue_chest_png = rendered_item_icon or _blockify_hero_icon_png(statue_occupied, size=64, transparent=True)
    statue_hand_png = rendered_item_icon or _blockify_hero_icon_png(statue_occupied, size=128, transparent=True)
    egg_chest_png = (rendered_item_icon or _blockify_egg_icon_png(statue_occupied, size=64)) if animated else b""
    egg_hand_png = (rendered_item_icon or _blockify_egg_icon_png(statue_occupied, size=128)) if animated else b""

    # Keep UUIDs stable so worlds retain the same logical pack, but increment
    # the pack version whenever the generated Bedrock contract changes.  Using
    # a permanent 1.0.0 caused Minecraft to retain an already-installed stale
    # pack even after CinterForge exported corrected geometry/animations.
    pack_version = list(BLOCKIFY_ADDON_PACK_VERSION)
    rp_manifest = {"format_version": 2, "header": {"name": pack_title, "description": f"CinterForge Minecraft asset: {title_name}", "uuid": rp_uuid, "version": pack_version, "min_engine_version": [1,21,80]}, "modules": [{"type": "resources", "uuid": rp_module_uuid, "version": pack_version}]}
    bp_manifest = {"format_version": 2, "header": {"name": pack_title, "description": f"CinterForge Minecraft asset: {title_name}", "uuid": bp_uuid, "version": pack_version, "min_engine_version": [1,21,80]}, "modules": [{"type": "data", "uuid": bp_module_uuid, "version": pack_version}], "dependencies": [{"uuid": rp_uuid, "version": pack_version}]}
    def as_json(value: Any) -> bytes:
        return json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")

    # Generated-pack provenance is part of the artifact itself, not only the
    # sidecar asset manifest.  Pack version alone is insufficient: two exporter
    # revisions can accidentally share a version and leave an older .mcaddon on
    # disk looking current.  Export validation reads this marker from BOTH
    # nested packs before deciding an existing add-on can be reused.
    export_marker = {
        "schema": BLOCKIFY_ADDON_SCHEMA,
        "pack_version": list(BLOCKIFY_ADDON_PACK_VERSION),
        "source_sha256": source_sha256,
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="cinterforge-blockify-addon-") as td:
        td_path = Path(td); rp_pack = td_path / f"{slug}_RP.mcpack"; bp_pack = td_path / f"{slug}_BP.mcpack"
        with zipfile.ZipFile(rp_pack, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("manifest.json", as_json(rp_manifest)); zf.writestr("pack_icon.png", pack_icon_png)
            zf.writestr("cinterforge_export.json", as_json(export_marker))
            if animated:
                zf.writestr(f"models/entity/{slug}_character.geo.json", as_json(character_geometry))
                zf.writestr(f"textures/entity/{character_texture_name}.png", character_texture_png)
                zf.writestr(f"entity/{slug}.entity.json", as_json(animated_client))
                if bedrock_animations:
                    zf.writestr(f"animations/{slug}.animation.json", as_json({"format_version": "1.8.0", "animations": bedrock_animations}))
                if animation_controller is not None:
                    zf.writestr(f"animation_controllers/{slug}.animation_controllers.json", as_json(animation_controller))
            else:
                zf.writestr(f"models/entity/{slug}_statue.geo.json", as_json(statue_geometry))
                zf.writestr(f"textures/entity/{statue_texture_name}.png", statue_texture_png)
                zf.writestr(f"entity/{slug}_statue.entity.json", as_json(statue_client))
            zf.writestr(f"render_controllers/{slug}.render_controllers.json", as_json(render_controller))
            zf.writestr("textures/item_texture.json", as_json(item_texture))
            if animated:
                zf.writestr(f"textures/items/{egg_icon_name}_chest.png", egg_chest_png)
                zf.writestr(f"textures/items/{egg_icon_name}_hand.png", egg_hand_png)
            else:
                zf.writestr(f"textures/items/{icon_name}_chest.png", statue_chest_png)
                zf.writestr(f"textures/items/{icon_name}_hand.png", statue_hand_png)
            zf.writestr("texts/en_US.lang", lang.encode("utf-8")); zf.writestr("texts/languages.json", as_json(languages))
        with zipfile.ZipFile(bp_pack, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("manifest.json", as_json(bp_manifest)); zf.writestr("pack_icon.png", pack_icon_png)
            zf.writestr("cinterforge_export.json", as_json(export_marker))
            if animated:
                zf.writestr(f"entities/{slug}.json", as_json(animated_behavior))
                if egg_item is not None:
                    zf.writestr(f"items/{slug}.item.json", as_json(egg_item))
            else:
                zf.writestr(f"entities/{slug}_statue.json", as_json(statue_behavior))
                zf.writestr(f"items/{slug}_statue.item.json", as_json(statue_item))
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(rp_pack, rp_pack.name); zf.write(bp_pack, bp_pack.name)
    if animated:
        commands = {
            "give": f"/give @s {egg_item_identifier}",
            "summon": f"/summon {entity_identifier}",
        }
    else:
        commands = {
            "statue": f"/give @s {statue_item_identifier}",
            "summon_statue": f"/summon {statue_entity_identifier}",
        }
    return {
        "file": destination.name, "identifier": egg_item_identifier or statue_item_identifier,
        "entity_identifier": entity_identifier, "egg_item_identifier": egg_item_identifier,
        "statue_entity_identifier": statue_entity_identifier, "statue_item_identifier": statue_item_identifier,
        "commands": commands, "behavior_profile": behavior,
        "addon_schema": BLOCKIFY_ADDON_SCHEMA, "pack_version": list(BLOCKIFY_ADDON_PACK_VERSION),
    }


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Read PNG IHDR dimensions without adding an image-library runtime dependency."""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        raise ValueError("Minecraft picture texture must be PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width < 1 or height < 1:
        raise ValueError("Minecraft picture texture has invalid dimensions")
    return width, height


def _write_image_mcaddon(
    destination: Path, *, source: Path, source_sha256: str, width: int, height: int,
    colors: list[dict[str, Any]], statue_png: bytes, picture_png: bytes, product_name: str = "",
) -> dict[str, Any]:
    """Build one Bedrock add-on containing a textured block statue and wall picture."""
    display_name = str(product_name or "").strip() or source.stem.replace("_", " ").strip() or "Image Asset"
    title_name = " ".join(word[:1].upper() + word[1:] for word in display_name.split())
    slug = _blockify_slug(display_name)
    statue_entity = f"cinter:{slug}_statue"
    statue_item = statue_entity
    picture_entity = f"cinter:{slug}_picture"
    picture_item = picture_entity
    statue_geometry_id = f"geometry.cinterforge.{slug}.image_statue"
    picture_geometry_id = f"geometry.cinterforge.{slug}.picture"
    picture_frame_geometry_id = f"geometry.cinterforge.{slug}.picture_frame"
    statue_texture_name = f"cinterforge_{slug}_image_statue"
    picture_texture_name = f"cinterforge_{slug}_picture"
    picture_frame_texture_name = f"cinterforge_{slug}_picture_frame"
    render_statue = f"controller.render.cinterforge_{slug}_image_statue"
    render_picture = f"controller.render.cinterforge_{slug}_picture"
    render_picture_frame = f"controller.render.cinterforge_{slug}_picture_frame"
    seed = f"{source_sha256}:{width}:{height}:image-addon-v1:{slug}"
    rp_uuid, rpm_uuid = _uuid_from_seed(seed+":rp"), _uuid_from_seed(seed+":rpm")
    bp_uuid, bpm_uuid = _uuid_from_seed(seed+":bp"), _uuid_from_seed(seed+":bpm")
    pack_version = [1, 1, 0]
    pack_title = f"CinterForge {title_name} - Image"

    by_xy = {(int(v["x"]), int(v["y"])): v for v in colors}
    statue_tex_w,statue_tex_h=_png_dimensions(statue_png)
    expected_statue_size=(width*16,height*16)
    if (statue_tex_w,statue_tex_h) != expected_statue_size:
        raise ValueError(f"Minecraft statue texture must be {expected_statue_size[0]}x{expected_statue_size[1]} pixels")
    cubes=[]
    for y in range(height):
        for x in range(width):
            if (x,y) not in by_xy:
                continue
            # Geometry stays at one full Minecraft cube per block. Each face uses
            # the corresponding 16x16 texel tile from the statue atlas, preserving
            # Minecraft's native block-face texture resolution without micro-voxels.
            u=x*16; vv=(height-1-y)*16
            north={"uv":[u,vv],"uv_size":[16,16]}
            south={"uv":[u+16,vv],"uv_size":[-16,16]}
            tile={"uv":[u,vv],"uv_size":[16,16]}
            cubes.append({
                "origin":[(x-width/2.0)*16.0, y*16.0, -8.0], "size":[16,16,16],
                "uv":{"north":north,"south":south,"east":dict(tile),"west":dict(tile),"up":dict(tile),"down":dict(tile)},
            })
    if not cubes:
        raise ValueError("Image has no visible blocks for a Minecraft statue")
    tex_w,tex_h=_png_dimensions(picture_png)
    statue_geometry={"format_version":"1.12.0","minecraft:geometry":[{"description":{"identifier":statue_geometry_id,"texture_width":statue_tex_w,"texture_height":statue_tex_h,"visible_bounds_width":max(1.0,float(width)+1),"visible_bounds_height":max(1.0,float(height)+1),"visible_bounds_offset":[0,float(height)/2,0]},"bones":[{"name":"root","pivot":[0,0,0],"cubes":cubes}]}]}
    # A very thin textured box gives the picture a front/back surface while keeping
    # its authored physical size in Minecraft blocks.  Width/height use 16 model
    # units per block; the picture itself is 1 model unit thick.
    picture_faces={
        "north":{"uv":[0,0],"uv_size":[tex_w,tex_h]},
        "south":{"uv":[tex_w,0],"uv_size":[-tex_w,tex_h]},
        "east":{"uv":[0,0],"uv_size":[1,tex_h]},"west":{"uv":[0,0],"uv_size":[1,tex_h]},
        "up":{"uv":[0,0],"uv_size":[tex_w,1]},"down":{"uv":[0,0],"uv_size":[tex_w,1]},
    }
    picture_geometry={"format_version":"1.12.0","minecraft:geometry":[{"description":{"identifier":picture_geometry_id,"texture_width":tex_w,"texture_height":tex_h,"visible_bounds_width":max(1.0,float(width)+1),"visible_bounds_height":max(1.0,float(height)+1),"visible_bounds_offset":[0,float(height)/2,0]},"bones":[{"name":"picture","pivot":[0,0,0],"cubes":[{"origin":[-width*8.0,0,-0.5],"size":[width*16.0,height*16.0,1],"uv":picture_faces}]}]}]}
    # Picture is deliberately NOT voxel/block geometry.  Render the continuous
    # image as one thin canvas and surround it with a second geometry pass for a
    # Minecraft-style frame.  Multiple render controllers let the same entity
    # use the real image texture for the canvas and a tiny dedicated frame
    # texture for the border.
    # Keep the entire Picture, including its frame, exactly one Bedrock model
    # pixel deep: 1 model unit = 1/16 of a Minecraft block.  The image canvas
    # above is already 1 unit deep; the frame must not protrude to 2 units.
    frame=1.0; picture_depth=1.0; picture_z=-picture_depth/2.0; full_w=width*16.0; full_h=height*16.0; frame_uv={name:{"uv":[0,0],"uv_size":[16,16]} for name in ("north","south","east","west","up","down")}
    frame_cubes=[
        {"origin":[-full_w/2-frame,-frame,picture_z],"size":[full_w+2*frame,frame,picture_depth],"uv":frame_uv},
        {"origin":[-full_w/2-frame,full_h,picture_z],"size":[full_w+2*frame,frame,picture_depth],"uv":frame_uv},
        {"origin":[-full_w/2-frame,0,picture_z],"size":[frame,full_h,picture_depth],"uv":frame_uv},
        {"origin":[full_w/2,0,picture_z],"size":[frame,full_h,picture_depth],"uv":frame_uv},
    ]
    picture_frame_geometry={"format_version":"1.12.0","minecraft:geometry":[{"description":{"identifier":picture_frame_geometry_id,"texture_width":16,"texture_height":16,"visible_bounds_width":max(1.0,float(width)+1),"visible_bounds_height":max(1.0,float(height)+1),"visible_bounds_offset":[0,float(height)/2,0]},"bones":[{"name":"frame","pivot":[0,0,0],"cubes":frame_cubes}]}]}
    # Simple warm wood-like frame swatch.  The image itself remains untouched.
    frame_pixels=[]
    for fy in range(16):
        for fx in range(16):
            band=((fx//4)+(fy//4))&1
            frame_pixels.extend((110+18*band,72+12*band,38+8*band,255))
    picture_frame_png=_png_rgba(16,16,bytes(frame_pixels))

    def client(identifier, geometry, texture, controller):
        return {"format_version":"1.10.0","minecraft:client_entity":{"description":{"identifier":identifier,"materials":{"default":"entity_alphatest"},"textures":{"default":f"textures/entity/{texture}"},"geometry":{"default":geometry},"render_controllers":[controller],"spawn_egg":{"base_color":"#6a6a6a","overlay_color":"#dddddd"}}}}
    def picture_client():
        return {"format_version":"1.10.0","minecraft:client_entity":{"description":{"identifier":picture_entity,"materials":{"default":"entity_alphatest","frame":"entity"},"textures":{"default":f"textures/entity/{picture_texture_name}","frame":f"textures/entity/{picture_frame_texture_name}"},"geometry":{"default":picture_geometry_id,"frame":picture_frame_geometry_id},"render_controllers":[render_picture,render_picture_frame],"spawn_egg":{"base_color":"#6a6a6a","overlay_color":"#dddddd"}}}}
    def render(controller):
        return {"format_version":"1.8.0","render_controllers":{controller:{"geometry":"Geometry.default","materials":[{"*":"Material.default"}],"textures":["Texture.default"]}}}
    def behavior(identifier, collision_width, collision_height):
        return {"format_version":"1.21.80","minecraft:entity":{"description":{"identifier":identifier,"is_spawnable":False,"is_summonable":True,"is_experimental":False},"components":{"minecraft:physics":{},"minecraft:collision_box":{"width":collision_width,"height":collision_height},"minecraft:pushable":{"is_pushable":False,"is_pushable_by_piston":False}}}}
    def item(identifier, entity, label, icon):
        return {"format_version":"1.21.80","minecraft:item":{"description":{"identifier":identifier,"menu_category":{"category":"construction"}},"components":{"minecraft:display_name":{"value":label},"minecraft:icon":{"textures":{"default":icon}},"minecraft:max_stack_size":16,"minecraft:entity_placer":{"entity":entity}}}}

    statue_icon=f"cinterforge_{slug}_statue"; picture_icon=f"cinterforge_{slug}_picture"
    item_texture={"resource_pack_name":pack_title,"texture_name":"atlas.items","texture_data":{statue_icon:{"textures":f"textures/items/{statue_icon}"},picture_icon:{"textures":f"textures/items/{picture_icon}"}}}
    rp_manifest={"format_version":2,"header":{"name":pack_title,"description":f"CinterForge Minecraft image asset: {title_name}","uuid":rp_uuid,"version":pack_version,"min_engine_version":[1,21,80]},"modules":[{"type":"resources","uuid":rpm_uuid,"version":pack_version}]}
    bp_manifest={"format_version":2,"header":{"name":pack_title,"description":f"CinterForge Minecraft image asset: {title_name}","uuid":bp_uuid,"version":pack_version,"min_engine_version":[1,21,80]},"modules":[{"type":"data","uuid":bpm_uuid,"version":pack_version}],"dependencies":[{"uuid":rp_uuid,"version":pack_version}]}
    marker={"schema":"cinterforge.image-addon.v4","source_sha256":source_sha256,"width_blocks":width,"height_blocks":height,"pixels_per_block":16,"statue_texture_size":[statue_tex_w,statue_tex_h],"picture_texture_size":[tex_w,tex_h],"picture_representation":"continuous-framed-wall-picture","picture_thickness_model_units":1,"picture_thickness_blocks":0.0625}
    as_json=lambda value: json.dumps(value,indent=2,ensure_ascii=False).encode("utf-8")
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cinterforge-image-addon-") as td:
        root=Path(td); rp=root/f"{slug}_RP.mcpack"; bp=root/f"{slug}_BP.mcpack"
        with zipfile.ZipFile(rp,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
            zf.writestr("manifest.json",as_json(rp_manifest));zf.writestr("cinterforge_export.json",as_json(marker));zf.writestr("pack_icon.png",picture_png)
            zf.writestr(f"models/entity/{slug}_statue.geo.json",as_json(statue_geometry));zf.writestr(f"models/entity/{slug}_picture.geo.json",as_json(picture_geometry));zf.writestr(f"models/entity/{slug}_picture_frame.geo.json",as_json(picture_frame_geometry))
            zf.writestr(f"textures/entity/{statue_texture_name}.png",statue_png);zf.writestr(f"textures/entity/{picture_texture_name}.png",picture_png);zf.writestr(f"textures/entity/{picture_frame_texture_name}.png",picture_frame_png)
            zf.writestr(f"entity/{slug}_statue.entity.json",as_json(client(statue_entity,statue_geometry_id,statue_texture_name,render_statue)))
            zf.writestr(f"entity/{slug}_picture.entity.json",as_json(picture_client()))
            rc=render(render_statue);rc["render_controllers"].update(render(render_picture)["render_controllers"]);rc["render_controllers"][render_picture_frame]={"geometry":"Geometry.frame","materials":[{"*":"Material.frame"}],"textures":["Texture.frame"]};zf.writestr(f"render_controllers/{slug}.render_controllers.json",as_json(rc))
            zf.writestr("textures/item_texture.json",as_json(item_texture));zf.writestr(f"textures/items/{statue_icon}.png",statue_png);zf.writestr(f"textures/items/{picture_icon}.png",picture_png)
            zf.writestr("texts/en_US.lang",f"entity.{statue_entity}.name={title_name} Statue\nentity.{picture_entity}.name={title_name} Picture\n".encode());zf.writestr("texts/languages.json",as_json(["en_US"]))
        with zipfile.ZipFile(bp,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
            zf.writestr("manifest.json",as_json(bp_manifest));zf.writestr("cinterforge_export.json",as_json(marker));zf.writestr("pack_icon.png",picture_png)
            zf.writestr(f"entities/{slug}_statue.json",as_json(behavior(statue_entity,max(0.25,float(width)),max(0.25,float(height)))))
            zf.writestr(f"entities/{slug}_picture.json",as_json(behavior(picture_entity,max(0.05,float(width)),max(0.25,float(height)))))
            zf.writestr(f"items/{slug}_statue.item.json",as_json(item(statue_item,statue_entity,f"{title_name} Statue",statue_icon)))
            zf.writestr(f"items/{slug}_picture.item.json",as_json(item(picture_item,picture_entity,f"{title_name} Picture",picture_icon)))
        with zipfile.ZipFile(destination,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
            zf.write(rp,rp.name);zf.write(bp,bp.name)
    return {"file":destination.name,"statue_entity_identifier":statue_entity,"statue_item_identifier":statue_item,"picture_entity_identifier":picture_entity,"picture_item_identifier":picture_item,"commands":{"statue":f"/give @s {statue_item}","summon_statue":f"/summon {statue_entity}","picture":f"/give @s {picture_item}","summon_picture":f"/summon {picture_entity}"},"addon_schema":"cinterforge.image-addon.v4","pack_version":pack_version}


def save_image_minecraft(
    folder_rel: str, filename: str, *, height: int, width: int,
    pixels: list[dict[str, Any]], statue_texture_data_url: str, texture_data_url: str, product_name: str = "",
) -> dict[str, Any]:
    """Persist an image's Minecraft statue + picture representations and add-on."""
    folder=_resolve_folder(folder_rel);source=(folder/filename).resolve()
    if folder not in source.parents or not source.is_file(): raise FileNotFoundError(filename)
    height=max(1,min(64,int(height)));width=max(1,int(width))
    if width > 256: raise ValueError("Derived Minecraft image width exceeds 256 blocks; reduce block height")
    statue_match=re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=\r\n]+)",str(statue_texture_data_url or "").strip(),re.IGNORECASE)
    if not statue_match: raise ValueError("Minecraft statue texture must be a PNG data URL")
    statue_png=base64.b64decode(statue_match.group(1),validate=True); statue_w,statue_h=_png_dimensions(statue_png)
    if (statue_w,statue_h)!=(width*16,height*16): raise ValueError(f"Minecraft statue texture must be {width*16}x{height*16} pixels")
    match=re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=\r\n]+)",str(texture_data_url or "").strip(),re.IGNORECASE)
    if not match: raise ValueError("Minecraft picture texture must be a PNG data URL")
    picture_png=base64.b64decode(match.group(1),validate=True); tex_w,tex_h=_png_dimensions(picture_png)
    if tex_w>1024 or tex_h>1024: raise ValueError("Minecraft picture texture must be at most 1024 pixels on either side")
    clean=[]
    for raw in list(pixels or []):
        if not isinstance(raw,dict): continue
        x,y=int(raw.get("x",-1)),int(raw.get("y",-1))
        if not (0<=x<width and 0<=y<height): continue
        clean.append({"x":x,"y":y,"r":max(0,min(255,int(raw.get("r",0)))),"g":max(0,min(255,int(raw.get("g",0)))),"b":max(0,min(255,int(raw.get("b",0)))),"a":max(0,min(255,int(raw.get("a",255))))})
    if not clean: raise ValueError("Minecraft image preview has no sampled pixels")
    source_sha=_sha256(source); artifact=folder/f"{source.stem}.image-minecraft.json"; picture=folder/f"{source.stem}.picture.png"; addon=folder/f"{source.stem}.mcaddon"
    picture_tmp=picture.with_suffix(picture.suffix+".tmp");addon_tmp=addon.with_suffix(addon.suffix+".tmp");artifact_tmp=artifact.with_suffix(artifact.suffix+".tmp")
    picture_tmp.write_bytes(picture_png)
    try:
        meta=_write_image_mcaddon(addon_tmp,source=source,source_sha256=source_sha,width=width,height=height,colors=clean,statue_png=statue_png,picture_png=picture_png,product_name=product_name)
        payload={"schema":"cinterforge-image-minecraft-v4","source_file":source.name,"source_sha256":source_sha,"width":width,"height":height,"depth":1,"resolution":16,"pixels_per_block":16,"block_count":len(clean),"voxel_count":len(clean),"voxels":[{"x":v["x"],"y":v["y"],"z":0,"r":v["r"],"g":v["g"],"b":v["b"],"a":v["a"]} for v in clean],"statue_texture_size":[statue_w,statue_h],"picture_texture_size":[tex_w,tex_h],"product_name":str(product_name or "").strip()}
        artifact_tmp.write_text(json.dumps(payload,separators=(",",":"),ensure_ascii=False),encoding="utf-8")
        picture_tmp.replace(picture);addon_tmp.replace(addon);artifact_tmp.replace(artifact)
    finally:
        picture_tmp.unlink(missing_ok=True);addon_tmp.unlink(missing_ok=True);artifact_tmp.unlink(missing_ok=True)
    manifest=_load_manifest(folder);version=next((v for v in manifest.get("versions") or [] if str(v.get("file") or "")==source.name),None)
    if not isinstance(version,dict): raise ValueError(f"Saved asset version is not present in manifest: {folder_rel}/{filename}")
    version["blockify"]={"file":artifact.name,"width":width,"height":height,"depth":1,"resolution":16,"pixels_per_block":16,"image_texture_contract":"opaque-block-v4","source_sha256":source_sha,"voxel_count":len(clean),"block_count":len(clean),"mcaddon_file":addon.name,"picture_file":picture.name,"minecraft_identifier":meta["statue_item_identifier"],"minecraft_entity_identifier":meta["statue_entity_identifier"],"minecraft_statue_entity_identifier":meta["statue_entity_identifier"],"minecraft_statue_item_identifier":meta["statue_item_identifier"],"minecraft_picture_entity_identifier":meta["picture_entity_identifier"],"minecraft_picture_item_identifier":meta["picture_item_identifier"],"minecraft_commands":dict(meta["commands"]),"addon_schema":meta["addon_schema"],"pack_version":list(meta["pack_version"]),"product_name":str(product_name or "").strip(),"image_asset":True}
    _normalize_version_artifacts(version);_write_manifest(folder,manifest)
    log(f"asset-image-minecraft source={source.relative_to(assets_root()).as_posix()} addon={addon.name} blocks={width}x{height} texture={tex_w}x{tex_h}")
    return dict(version["blockify"])

def save_blockify(
    folder_rel: str, filename: str, *, width: int, height: int, depth: int, resolution: int,
    voxels: list[dict[str, Any]], character_voxels: list[dict[str, Any]] | None = None,
    character_rig: dict[str, Any] | None = None, character_animations: dict[str, Any] | None = None,
    behavior_profile: list[dict[str, Any]] | None = None, behavior_signature: str = "", source_up_normalized: bool = False, product_name: str = "",
    regenerate_base: bool = False,
) -> dict[str, Any]:
    """Persist frame-zero/statue voxels and optional source-rig animation data."""
    folder = _resolve_folder(folder_rel)
    source = (folder / filename).resolve()
    if folder not in source.parents or not source.is_file():
        raise FileNotFoundError(filename)
    width = max(1, min(64, int(width))); height = max(1, min(64, int(height))); depth = max(1, min(64, int(depth)))
    resolution = int(resolution)
    if resolution != 16:
        raise ValueError("Blockify resolution must be 16 voxels per Minecraft block")
    limits = (width * resolution, height * resolution, depth * resolution)
    rig = character_rig if isinstance(character_rig, dict) else {}
    rig_names = {str(b.get("name") or "") for b in list(rig.get("bones") or []) if isinstance(b, dict) and str(b.get("name") or "")}
    def clean(rows: list[dict[str, Any]], *, preserve_bones: bool) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw in list(rows or []):
            if not isinstance(raw, dict):
                continue
            x,y,z = int(raw.get("x",-1)),int(raw.get("y",-1)),int(raw.get("z",-1))
            if not (0 <= x < limits[0] and 0 <= y < limits[1] and 0 <= z < limits[2]):
                continue
            row = {"x":x,"y":y,"z":z,"r":max(0,min(255,int(raw.get("r",255)))),"g":max(0,min(255,int(raw.get("g",255)))),"b":max(0,min(255,int(raw.get("b",255)))),"samples":max(1,int(raw.get("samples",1)))}
            try:
                palette_index = int(raw.get("palette_index"))
                if palette_index >= 0: row["palette_index"] = palette_index
            except (TypeError, ValueError): pass
            bone = str(raw.get("bone") or "")
            if preserve_bones and bone in rig_names:
                row["bone"] = bone
                try: row["bone_confidence"] = max(0.0,min(1.0,float(raw.get("bone_confidence",1.0))))
                except Exception: row["bone_confidence"] = 1.0
            result.append(row)
        return result
    statue_clean = clean(voxels, preserve_bones=False)
    character_clean = clean(character_voxels or [], preserve_bones=True) if rig_names else []

    # Reduce each Blockified model to at most one chest (27) construction colors.
    # Keep r/g/b for existing consumers, but make those values exact palette colors
    # and persist palette_index so RPG can expose these as discrete block types.
    def quantize_palette(rows: list[dict[str, Any]], maximum: int = 27) -> list[dict[str, int]]:
        if not rows: return []
        boxes: list[list[int]] = [list(range(len(rows)))]
        while len(boxes) < maximum:
            best_index=-1; best_channel=0; best_score=-1
            for box_index, indices in enumerate(boxes):
                if len(indices)<2: continue
                ranges=[max(rows[i][c] for i in indices)-min(rows[i][c] for i in indices) for c in ("r","g","b")]
                channel=max(range(3),key=lambda n:ranges[n]); weight=sum(max(1,int(rows[i].get("samples",1))) for i in indices); score=ranges[channel]*weight
                if score>best_score: best_index,best_channel,best_score=box_index,channel,score
            if best_index<0 or best_score<=0: break
            indices=boxes.pop(best_index); key=("r","g","b")[best_channel]
            ordered=sorted(indices,key=lambda i:(rows[i][key],rows[i]["r"],rows[i]["g"],rows[i]["b"],i))
            total=sum(max(1,int(rows[i].get("samples",1))) for i in ordered); acc=0; cut=1
            for n,i in enumerate(ordered[:-1]):
                acc+=max(1,int(rows[i].get("samples",1))); cut=n+1
                if acc*2>=total: break
            boxes.extend((ordered[:cut],ordered[cut:]))
        palette=[]
        for index,indices in enumerate(boxes):
            weight=sum(max(1,int(rows[i].get("samples",1))) for i in indices)
            color={c:int(round(sum(rows[i][c]*max(1,int(rows[i].get("samples",1))) for i in indices)/weight)) for c in ("r","g","b")}
            palette.append({"index":index,**color})
        return palette

    # Quantization creates an initial palette. Once an artifact exists, its palette
    # is authored data and voxel edits must not regenerate/recolor it.
    existing_palette: list[dict[str, int]] = []
    existing_payload: dict[str, Any] = {}
    existing_artifact = folder / f"{source.stem}.blockify.json"
    if existing_artifact.is_file():
        try:
            existing_payload = json.loads(existing_artifact.read_text(encoding="utf-8"))
            for raw_entry in list(existing_payload.get("palette") or []):
                if isinstance(raw_entry, dict):
                    existing_palette.append({"index":len(existing_palette),"r":max(0,min(255,int(raw_entry.get("r",255)))),"g":max(0,min(255,int(raw_entry.get("g",255)))),"b":max(0,min(255,int(raw_entry.get("b",255))))})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            existing_payload = {}; existing_palette = []
    # Keep generated voxels and authored/collaborative edits as separate data.
    # Normal Build saves recompute the edit overlay against the stored generated
    # base. Re-Blockify replaces the base and replays that overlay verbatim.
    previous_base = list(existing_payload.get("base_voxels") or []) if isinstance(existing_payload, dict) else []
    previous_current = list(existing_payload.get("voxels") or []) if isinstance(existing_payload, dict) else []
    previous_edits = existing_payload.get("voxel_edits") if isinstance(existing_payload, dict) else None
    generated_base = [dict(v) for v in statue_clean]
    voxel_edits: dict[str, Any] = {"removed": [], "upsert": []}
    authored_keys: set[tuple[int, int, int]] = set()
    key=lambda v:(int(v.get("x",-1)),int(v.get("y",-1)),int(v.get("z",-1)))

    def derive_edits(base_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]) -> dict[str, Any]:
        base={key(v):v for v in base_rows if isinstance(v,dict)}
        current={key(v):v for v in current_rows if isinstance(v,dict)}
        removed=[{"x":k[0],"y":k[1],"z":k[2]} for k in sorted(set(base)-set(current))]
        upsert=[]
        for k in sorted(current):
            v=current[k]; b=base.get(k)
            if b is None or any(int(v.get(c,255))!=int(b.get(c,255)) for c in ("r","g","b")):
                upsert.append(dict(v))
        return {"removed":removed,"upsert":upsert}

    if regenerate_base:
        if isinstance(previous_edits,dict):
            voxel_edits={
                "removed":[dict(v) for v in list(previous_edits.get("removed") or []) if isinstance(v,dict)],
                "upsert":[dict(v) for v in list(previous_edits.get("upsert") or []) if isinstance(v,dict)],
            }
        elif previous_base:
            voxel_edits=derive_edits(previous_base,previous_current)
        # A legacy artifact with no base cannot safely distinguish generated voxels
        # from authored edits. Do not invent an overlay in that case.
        merged={key(v):dict(v) for v in generated_base}
        for v in voxel_edits["removed"]: merged.pop(key(v),None)
        for v in voxel_edits["upsert"]: merged[key(v)]=dict(v)
        authored_keys={key(v) for v in voxel_edits["upsert"]}
        statue_clean=list(merged.values())
    elif previous_base:
        # Every local or remotely-merged Build edit updates the canonical overlay.
        voxel_edits=derive_edits(previous_base,statue_clean)
        authored_keys={key(v) for v in voxel_edits["upsert"]}
    elif isinstance(previous_edits,dict):
        voxel_edits={
            "removed":[dict(v) for v in list(previous_edits.get("removed") or []) if isinstance(v,dict)],
            "upsert":[dict(v) for v in list(previous_edits.get("upsert") or []) if isinstance(v,dict)],
        }
    palette = ([] if regenerate_base else existing_palette) or quantize_palette(generated_base or character_clean,27)
    def apply_palette(rows: list[dict[str, Any]], *, preserve_authored: bool) -> None:
        nonlocal palette
        for row in rows:
            rgb=(row["r"],row["g"],row["b"]); exact=next((p for p in palette if (p["r"],p["g"],p["b"])==rgb),None)
            if preserve_authored:
                entry=exact
                if entry is None:
                    entry={"index":len(palette),"r":rgb[0],"g":rgb[1],"b":rgb[2]}; palette.append(entry)
                row["palette_index"]=entry["index"]
            elif palette:
                entry=exact or min(palette,key=lambda p:(row["r"]-p["r"])**2+(row["g"]-p["g"])**2+(row["b"]-p["b"])**2)
                row["palette_index"]=entry["index"]; row["r"],row["g"],row["b"]=entry["r"],entry["g"],entry["b"]
    if palette:
        # A regenerated base gets the new generated palette.  Replayed authored
        # colors are then retained exactly (and may extend that palette).
        if regenerate_base:
            apply_palette(generated_base,preserve_authored=False)
            base_palette_by_key={(int(v["x"]),int(v["y"]),int(v["z"])):v for v in generated_base}
            for row in statue_clean:
                k=(int(row["x"]),int(row["y"]),int(row["z"]))
                if k in authored_keys: apply_palette([row],preserve_authored=True)
                elif k in base_palette_by_key: row.update({c:base_palette_by_key[k][c] for c in ("r","g","b","palette_index")})
            apply_palette(character_clean,preserve_authored=False)
        else:
            apply_palette(statue_clean,preserve_authored=bool(existing_palette)); apply_palette(character_clean,preserve_authored=bool(existing_palette))
    animations = character_animations if isinstance(character_animations, dict) else {}
    artifact = folder / f"{source.stem}.blockify.json"; addon = folder / f"{source.stem}.mcaddon"
    payload = {
        "schema":"cinterforge-blockify-v3", "source_file":source.name, "source_sha256":_sha256(source),
        "width":width,"height":height,"depth":depth,"resolution":resolution,
        "palette_size":len(palette),"palette":palette,
        "base_voxels":([dict(v) for v in generated_base] if regenerate_base else (previous_base or [dict(v) for v in statue_clean])),
        "voxel_edits":voxel_edits,
        "voxel_count":len(statue_clean),"voxels":statue_clean,
        "character_voxel_count":len(character_clean),"character_voxels":character_clean,
        "character_rig":rig,"character_animations":animations,
        "behavior_profile":list(behavior_profile or []),"behavior_signature":str(behavior_signature or ""),
        "source_up_normalized":bool(source_up_normalized),"product_name":str(product_name or "").strip(),
    }
    # Saves can overlap when the user places/removes blocks quickly.  Never share a
    # fixed .tmp pathname between requests: one request could replace/unlink the
    # other request's temporary mcaddon, producing FileNotFoundError and flicker.
    artifact_fd, artifact_temp_name = tempfile.mkstemp(prefix=f".{artifact.name}.", suffix=".tmp", dir=folder)
    os.close(artifact_fd)
    addon_fd, addon_temp_name = tempfile.mkstemp(prefix=f".{addon.name}.", suffix=".tmp", dir=folder)
    os.close(addon_fd)
    artifact_temp=Path(artifact_temp_name); addon_temp=Path(addon_temp_name)
    # _write_blockify_mcaddon owns creation of its output archive.
    addon_temp.unlink(missing_ok=True)
    artifact_temp.write_text(json.dumps(payload,separators=(",",":"),ensure_ascii=False),encoding="utf-8")
    try:
        addon_meta=_write_blockify_mcaddon(addon_temp,source=source,source_sha256=payload["source_sha256"],width=width,height=height,depth=depth,voxels=statue_clean,character_voxels=character_clean or None,character_rig=rig or None,character_animations=animations,behavior_profile=list(behavior_profile or []),product_name=payload["product_name"])
        payload["behavior_profile"] = list(addon_meta.get("behavior_profile") or [])
        artifact_temp.write_text(json.dumps(payload,separators=(",",":"),ensure_ascii=False),encoding="utf-8")
        artifact_temp.replace(artifact);addon_temp.replace(addon)
    finally:
        artifact_temp.unlink(missing_ok=True);addon_temp.unlink(missing_ok=True)
    manifest=_load_manifest(folder)
    version=next((v for v in manifest.get("versions") or [] if str(v.get("file") or "")==source.name),None)
    if not isinstance(version,dict):
        raise ValueError(f"Saved asset version is not present in manifest: {folder_rel}/{filename}")
    version["blockify"]={
        "file":artifact.name,"width":width,"height":height,"depth":depth,"resolution":resolution,
        "source_sha256":payload["source_sha256"],"palette_size":len(palette),"palette":palette,
        "voxel_count":len(statue_clean),"character_voxel_count":len(character_clean),
        "mcaddon_file":addon.name,"minecraft_identifier":addon_meta["identifier"],"minecraft_entity_identifier":addon_meta["entity_identifier"],
        "minecraft_egg_item_identifier":addon_meta.get("egg_item_identifier") or "",
        "minecraft_statue_entity_identifier":addon_meta["statue_entity_identifier"],"minecraft_statue_item_identifier":addon_meta["statue_item_identifier"],
        "minecraft_commands":dict(addon_meta.get("commands") or {}),
        "behavior_signature":str(behavior_signature or ""),"behavior_profile":payload["behavior_profile"],
        "source_up_normalized":bool(source_up_normalized),"product_name":payload["product_name"],
        "addon_schema":str(addon_meta.get("addon_schema") or BLOCKIFY_ADDON_SCHEMA),
        "pack_version":list(addon_meta.get("pack_version") or BLOCKIFY_ADDON_PACK_VERSION),
    }
    _normalize_version_artifacts(version)
    _write_manifest(folder,manifest)
    log(f"asset-blockified source={source.relative_to(assets_root()).as_posix()} artifact={artifact.name} addon={addon.name} entity={addon_meta['entity_identifier']} blocks={width}x{height}x{depth} statue_voxels={len(statue_clean)} character_voxels={len(character_clean)} animations={len(animations)}")
    return dict(version["blockify"])

def load_blockify(folder_rel: str, filename: str) -> dict[str, Any]:
    """Load a cached Blockify result only when its source and artifact still exist and match."""
    folder = _resolve_folder(folder_rel)
    source = (folder / filename).resolve()
    if folder not in source.parents or not source.is_file():
        raise FileNotFoundError(filename)
    manifest = _load_manifest(folder)
    version = next((v for v in manifest.get("versions") or [] if str(v.get("file") or "") == source.name), None)
    meta = version.get("blockify") if isinstance(version, dict) else None
    if not isinstance(meta, dict) or not str(meta.get("file") or ""):
        raise FileNotFoundError("Blockify artifact is not present")
    artifact = (folder / str(meta["file"])).resolve()
    addon_name = str(meta.get("mcaddon_file") or "").strip()
    addon = (folder / addon_name).resolve() if addon_name else None
    if folder not in artifact.parents or not artifact.is_file():
        version.pop("blockify", None)
        _normalize_version_artifacts(version)
        _write_manifest(folder, manifest)
        raise FileNotFoundError(str(meta.get("file") or "Blockify artifact"))
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    # Legacy Blockify JSON did not always carry a source hash. A present hash
    # remains authoritative; an absent hash is accepted for preview compatibility.
    payload_source_sha = str(payload.get("source_sha256") or meta.get("source_sha256") or "").strip().lower()
    current_source_sha = _sha256(source).lower()
    if payload_source_sha and payload_source_sha != current_source_sha:
        version.pop("blockify", None)
        _normalize_version_artifacts(version)
        _write_manifest(folder, manifest)
        raise ValueError("Blockify artifact is stale because the source 3D asset changed")
    # The .mcaddon is not required to preview voxels. Preserve old assets whose
    # Blockify cache predates add-on generation; export regenerates it on demand.
    if addon is not None and (folder not in addon.parents or not addon.is_file()):
        meta.pop("mcaddon_file", None)
        _normalize_version_artifacts(version)
        _write_manifest(folder, manifest)
    return payload


def blockify_addon_is_current(addon: Path) -> bool:
    """Return True only when the actual .mcaddon bytes match this exporter.

    Sidecar metadata is never authoritative for generated bytes.  Check both
    nested pack manifests AND the exporter marker embedded in each pack so an
    older add-on cannot be reused merely because it shares the current numeric
    pack version.
    """
    addon = Path(addon).expanduser().resolve()
    if not addon.is_file():
        return False
    expected_version = list(BLOCKIFY_ADDON_PACK_VERSION)
    expected_schema = str(BLOCKIFY_ADDON_SCHEMA)
    try:
        with zipfile.ZipFile(addon) as outer:
            pack_names = [name for name in outer.namelist() if name.casefold().endswith(".mcpack")]
            if len(pack_names) < 2:
                return False
            matched = 0
            for pack_name in pack_names:
                with zipfile.ZipFile(io.BytesIO(outer.read(pack_name))) as pack:
                    manifest = json.loads(pack.read("manifest.json"))
                    marker = json.loads(pack.read("cinterforge_export.json"))
                version = list(((manifest.get("header") or {}).get("version") or []))
                marker_version = list(marker.get("pack_version") or [])
                marker_schema = str(marker.get("schema") or "")
                if version != expected_version:
                    return False
                if marker_version != expected_version or marker_schema != expected_schema:
                    return False
                matched += 1
            return matched >= 2
    except Exception:
        return False


def regenerate_blockify_addon(folder_rel: str, filename: str, *, product_name: str = "") -> dict[str, Any]:
    """Regenerate a missing/outdated .mcaddon from an existing Blockify artifact.

    This intentionally does not re-voxelize the GLB. It lets legacy saved assets
    keep a working Minecraft Preview and gain the current add-on structure at
    export time.
    """
    folder = _resolve_folder(folder_rel)
    source = (folder / filename).resolve()
    if folder not in source.parents or not source.is_file():
        raise FileNotFoundError(filename)
    payload = load_blockify(folder_rel, filename)
    manifest = _load_manifest(folder)
    version = next((v for v in manifest.get("versions") or [] if str(v.get("file") or "") == source.name), None)
    if not isinstance(version, dict):
        raise ValueError(f"Saved asset version is not present in manifest: {folder_rel}/{filename}")
    meta = version.get("blockify") if isinstance(version.get("blockify"), dict) else {}
    addon = folder / f"{source.stem}.mcaddon"
    addon_temp = addon.with_suffix(addon.suffix + ".tmp")
    width=max(1,int(payload.get("width") or meta.get("width") or 1))
    height=max(1,int(payload.get("height") or meta.get("height") or 1))
    depth=max(1,int(payload.get("depth") or meta.get("depth") or 1))
    source_sha=str(payload.get("source_sha256") or meta.get("source_sha256") or _sha256(source))
    blockbench_override = None
    blockbench_file = str(meta.get("blockbench_file") or "").strip()
    if blockbench_file:
        override_path = (folder / blockbench_file).resolve()
        if folder in override_path.parents and override_path.is_file():
            try:
                blockbench_override = json.loads(override_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log(f"asset-blockbench-override-invalid file={override_path.name} error={exc!r}")
    try:
        addon_meta = _write_blockify_mcaddon(
            addon_temp, source=source, source_sha256=source_sha, width=width, height=height, depth=depth,
            voxels=list(payload.get("voxels") or []),
            character_voxels=list(payload.get("character_voxels") or []) or None,
            character_rig=payload.get("character_rig") if isinstance(payload.get("character_rig"),dict) else None,
            character_animations=payload.get("character_animations") if isinstance(payload.get("character_animations"),dict) else None,
            behavior_profile=list(payload.get("behavior_profile") or []),
            product_name=str(product_name or payload.get("product_name") or meta.get("product_name") or ""),
            blockbench_override=blockbench_override,
        )
        addon_temp.replace(addon)
    finally:
        addon_temp.unlink(missing_ok=True)
    resolved_product_name = str(product_name or payload.get("product_name") or meta.get("product_name") or "").strip()
    if resolved_product_name and str(payload.get("product_name") or "") != resolved_product_name:
        payload["product_name"] = resolved_product_name
        artifact_name = str(meta.get("file") or f"{source.stem}.blockify.json")
        artifact_path = folder / artifact_name
        if artifact_path.is_file():
            artifact_path.write_text(json.dumps(payload,separators=(",",":"),ensure_ascii=False),encoding="utf-8")
    meta.update({
        "mcaddon_file": addon.name,
        "minecraft_identifier": addon_meta["identifier"],
        "minecraft_entity_identifier": addon_meta["entity_identifier"],
        "minecraft_egg_item_identifier": addon_meta.get("egg_item_identifier") or "",
        "minecraft_statue_entity_identifier": addon_meta["statue_entity_identifier"],
        "minecraft_statue_item_identifier": addon_meta["statue_item_identifier"],
        "minecraft_commands": dict(addon_meta.get("commands") or {}),
        "behavior_profile": list(addon_meta.get("behavior_profile") or payload.get("behavior_profile") or []),
        "product_name": str(product_name or payload.get("product_name") or meta.get("product_name") or ""),
        "addon_schema": str(addon_meta.get("addon_schema") or BLOCKIFY_ADDON_SCHEMA),
        "pack_version": list(addon_meta.get("pack_version") or BLOCKIFY_ADDON_PACK_VERSION),
    })
    version["blockify"] = meta
    _normalize_version_artifacts(version)
    _write_manifest(folder, manifest)
    log(f"asset-blockify-addon-regenerated source={source.relative_to(assets_root()).as_posix()} addon={addon.name} entity={addon_meta['entity_identifier']}")
    return dict(meta)


def _blockbench_resource_session(folder_rel: str, filename: str, *, product_name: str = "", regenerate: bool = True) -> dict[str, Any]:
    """Return the exact current Bedrock resource files for an embedded Blockbench session."""
    folder = _resolve_folder(folder_rel)
    source = (folder / filename).resolve()
    if folder not in source.parents or not source.is_file():
        raise FileNotFoundError(filename)
    manifest = _load_manifest(folder)
    version = next((v for v in manifest.get("versions") or [] if str(v.get("file") or "") == source.name), None)
    if not isinstance(version, dict):
        raise ValueError(f"Saved asset version is not present in manifest: {folder_rel}/{filename}")
    current_meta = version.get("blockify") if isinstance(version.get("blockify"), dict) else {}
    # Image assets already own an exact generated Statue + Picture add-on.  Do
    # not route them through the 3D Blockify regenerator: that would reinterpret
    # one-image-block cells as 16^3 microvoxels.  Blockbench edits the Statue
    # resource model in the existing image add-on; Picture remains separate.
    if current_meta.get("image_asset"):
        meta = dict(current_meta)
    elif regenerate:
        meta = regenerate_blockify_addon(folder_rel, filename, product_name=product_name)
    else:
        meta = dict(current_meta)
    addon = (folder / str(meta.get("mcaddon_file") or "")).resolve()
    if folder not in addon.parents or not addon.is_file():
        raise FileNotFoundError(str(meta.get("mcaddon_file") or "Minecraft Add-On"))
    with zipfile.ZipFile(addon, "r") as outer:
        rp_name = next((name for name in outer.namelist() if name.lower().endswith("_rp.mcpack")), "")
        if not rp_name:
            raise ValueError("Minecraft Add-On has no resource pack")
        import io
        with zipfile.ZipFile(io.BytesIO(outer.read(rp_name)), "r") as rp:
            names = rp.namelist()
            geo_name = next((name for name in names if name.startswith("models/entity/") and name.endswith("_character.geo.json")), "")
            animated = bool(geo_name)
            if not geo_name:
                geo_name = next((name for name in names if name.startswith("models/entity/") and name.endswith("_statue.geo.json")), "")
            if not geo_name:
                raise ValueError("Minecraft resource pack has no editable entity geometry")
            geometry = json.loads(rp.read(geo_name).decode("utf-8"))
            animation_name = next((name for name in names if name.startswith("animations/") and name.endswith(".animation.json")), "")
            animations = json.loads(rp.read(animation_name).decode("utf-8")) if animation_name else {"format_version":"1.8.0","animations":{}}
            texture_name = ""
            if meta.get("image_asset"):
                texture_name = next((name for name in names if name.startswith("textures/entity/") and name.endswith("_image_statue.png")), "")
            elif animated:
                texture_name = next((name for name in names if name.startswith("textures/entity/") and name.endswith(".png") and not name.endswith("_statue.png")), "")
            if not texture_name:
                texture_name = next((name for name in names if name.startswith("textures/entity/") and name.endswith(".png")), "")
            texture_data_url = ""
            if texture_name:
                texture_data_url = "data:image/png;base64," + base64.b64encode(rp.read(texture_name)).decode("ascii")
    return {
        "folder": folder_rel,
        "file": filename,
        "product_name": str(product_name or meta.get("product_name") or source.stem),
        "model_name": Path(geo_name).name,
        "texture_name": Path(texture_name).name if texture_name else f"{source.stem}.png",
        "geometry": geometry,
        "animations": animations,
        "texture_data_url": texture_data_url,
        "animated": animated,
        "required_animation_ids": sorted(str(key) for key in (animations.get("animations") or {}).keys()),
        "blockbench_edited": bool(meta.get("blockbench_file")),
    }


def _uci_blockbench_addon(uci: str) -> tuple[dict[str, Any], Path]:
    """Resolve the existing resource package using the canonical UCI identity."""
    from runtime.assets.asset_management import resolve_uci_asset

    resolved = resolve_uci_asset(uci)
    block_model_rel = str(resolved.get("block_model") or "").replace("\\", "/").lstrip("/")
    if not block_model_rel:
        raise FileNotFoundError(f"local Blockify representation is missing for UCI {resolved['uci']}")
    root = assets_root().resolve()
    block_source = root / block_model_rel
    if block_source.is_symlink():
        raise ValueError("Symlinked Blockify model is not editable")
    block_model = block_source.resolve()
    try:
        block_model.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Blockify representation escapes Assets root: {block_model_rel}") from exc
    if not block_model.is_file():
        raise FileNotFoundError(block_model)

    representation_folder = block_model.parent
    asset_folder = (root / resolved["folder"]).resolve()
    if representation_folder != (asset_folder / f"block_{resolved['block_resolution']}") or block_model.suffix.lower() != ".gltf":
        raise ValueError("Blockify editor requires the canonical Block GLTF folder")
    addons = sorted(representation_folder.glob("*.mcaddon"))
    if len(addons) != 1:
        raise RuntimeError(
            f"UCI {resolved['uci']} Blockify representation must contain exactly one .mcaddon; found {len(addons)}"
        )
    addon = addons[0]
    if addon.is_symlink():
        raise ValueError("Symlinked Minecraft Add-On is not editable")
    addon.resolve().relative_to(representation_folder)
    return resolved, addon


def regenerate_uci_blockify(uci: str, payload: dict[str, Any], *, source_file: str,
                            preview_files: list[dict[str, Any]]) -> dict[str, Any]:
    """Commit one complete generated Block representation, not just its voxel JSON.

    Reuse the existing migration GLTF exporter and Minecraft package producer.
    The selected representation keeps its paths and public command identity.
    Terrain brush saves remain on their existing data-only path.
    """
    import shutil
    from .asset_management import resolve_uci_asset, _runtime_descriptor_filename
    from .asset_migration_commands import BLOCKIFY_RUNTIME_RECIPE, BLOCKIFY_RUNTIME_RECIPE_FINGERPRINT

    resolved = resolve_uci_asset(uci)
    root = assets_root().resolve()
    folder = (root / resolved["folder"]).resolve()
    folder.relative_to(root)
    manifest = resolved["manifest"]
    source = folder / source_file
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(folder):
        raise ValueError("Blockify requires an owned saved source file")
    dimensions = {axis: payload.get(axis) for axis in ("width", "height", "depth")}
    if any(type(value) is not int or not 1 <= value <= 64 for value in dimensions.values()):
        raise ValueError("Blockify dimensions must be integers from 1 to 64 blocks")
    for field in ("voxels", "character_voxels"):
        for voxel in payload.get(field) or []:
            if not isinstance(voxel, dict) or any(type(voxel.get(coord)) is not int or not 0 <= voxel[coord] < dimensions[axis] * 16
                    for coord, axis in zip(("x", "y", "z"), dimensions)):
                raise ValueError("Blockify voxel lies outside its authored dimensions")
    if not payload.get("voxels"):
        raise ValueError("Cannot save empty Blockify geometry")
    if payload.get("character_animations") and not (payload.get("character_voxels") and payload.get("character_rig", {}).get("bones")):
        raise ValueError("Animated Blockify cannot lose its character rig")
    resolution_key = int(resolved.get("block_resolution") or 16)
    # Migrated higher-density representations keep their existing storage key;
    # the actual Bedrock voxel grid always contains 16 units per block.
    block_folder = folder / f"block_{resolution_key}"
    if block_folder.is_symlink():
        raise ValueError("Symlinked Blockify representation is not writable")
    block_folder.mkdir(parents=True, exist_ok=True)
    reps = manifest.setdefault("representations", {})
    rep = reps.setdefault(block_folder.name, {})
    name = _safe_token(str(manifest.get("name") or source.stem), "asset")
    def owned(field: str, default: str, suffix: str) -> Path:
        target = folder / str(rep.get(field) or f"{block_folder.name}/{default}")
        if target.parent != block_folder or target.suffix.lower() != suffix or target.is_symlink():
            raise ValueError(f"Invalid owned Blockify {field} path")
        return target
    model = owned("model", f"{name}_block.gltf", ".gltf")
    addon = owned("minecraft", f"{name}.mcaddon", ".mcaddon")
    thumbnail = owned("thumbnail", f"{name}_block_thumbnail.png", ".png")
    data_path = block_folder / "blockify.json"
    if any(path != addon for path in block_folder.glob("*.mcaddon")):
        raise ValueError("Multiple Minecraft Add-Ons exist in the selected Blockify representation")
    # Verify the complete browser exporter result before touching saved files.
    artifacts: dict[Path, bytes] = {}
    document = None
    for row in preview_files or []:
        filename = str(row.get("name") or "")
        suffix = Path(filename).suffix.lower()
        if not filename or Path(filename).name != filename or "\\" in filename or suffix not in (".gltf", ".bin", ".png", ".jpg", ".jpeg"):
            raise ValueError("Invalid Blockify GLTF export resource")
        if suffix == ".gltf":
            if document is not None or row.get("encoding") != "text":
                raise ValueError("Expected exactly one readable Blockify GLTF")
            document = json.loads(row["data"])
            target, data = model, row["data"].encode("utf-8")
        else:
            if row.get("encoding") != "base64":
                raise ValueError("Blockify GLTF resource must be binary")
            target, data = block_folder / filename, base64.b64decode(row["data"], validate=True)
        if target in artifacts or target in (addon, thumbnail, data_path) or target.is_symlink():
            raise ValueError("Conflicting Blockify GLTF export resource")
        artifacts[target] = data
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0" or not document.get("meshes") or not document.get("accessors") or not document.get("buffers"):
        raise ValueError("Blockify export has no GLTF mesh")
    for entry in (document.get("buffers") or []) + (document.get("images") or []):
        uri = entry.get("uri")
        if not isinstance(uri, str) or Path(uri).name != uri or block_folder / uri not in artifacts:
            raise ValueError("Blockify GLTF references an unowned resource")
        if "byteLength" in entry and len(artifacts[block_folder / uri]) != int(entry["byteLength"]):
            raise ValueError("Blockify GLTF buffer length does not match its document")
    if payload.get("character_rig", {}).get("bones") and (not document.get("skins") or len(document.get("animations") or []) != len(payload.get("character_animations") or {})):
        raise ValueError("Blockify export lost character rig or animations")
    relative = lambda path: path.relative_to(folder).as_posix()
    source_sha = _sha256(source)
    payload = {**payload, "source_file": source_file, "source_sha256": source_sha,
               "file": relative(data_path), "thumbnail": relative(thumbnail), "resolution": 16}
    png = _blockify_hero_icon_png(payload["voxels"], size=512, transparent=False)
    artifacts[thumbnail] = png
    with tempfile.TemporaryDirectory(dir=block_folder, prefix="blockify-") as scratch:
        staging = Path(scratch)
        staged_addon = staging / addon.name
        meta = _write_blockify_mcaddon(staged_addon, source=source, source_sha256=source_sha,
            **dimensions, voxels=payload["voxels"], character_voxels=payload.get("character_voxels"),
            character_rig=payload.get("character_rig"), character_animations=payload.get("character_animations"),
            behavior_profile=payload.get("behavior_profile"),
            product_name=str(manifest.get("minecraft_name") or payload.get("product_name") or resolved["name"]),
            command_name=str(manifest.get("command_name") or resolved["name"]), item_icon_png=png)
        payload["mcaddon_file"] = relative(addon)
        payload["minecraft_identifier"] = meta["identifier"]
        payload["minecraft_entity_identifier"] = meta["entity_identifier"]
        payload["minecraft_egg_item_identifier"] = meta["egg_item_identifier"]
        payload["minecraft_statue_entity_identifier"] = meta["statue_entity_identifier"]
        artifacts[data_path] = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        data_digest = hashlib.sha256(artifacts[data_path]).hexdigest()
        rep.update(**dimensions, resolution=resolution_key, model=relative(model), thumbnail=relative(thumbnail),
                   minecraft=relative(addon), minecraft_sha256=_sha256(staged_addon), minecraft_data_sha256=data_digest,
                   data=relative(data_path), data_sha256=data_digest, voxel_count=len(payload["voxels"]),
                   files=[relative(path) for path in artifacts if path not in (data_path, thumbnail)],
                   source_up_normalized=bool(payload.get("source_up_normalized")),
                   runtime_recipe=BLOCKIFY_RUNTIME_RECIPE, runtime_recipe_fingerprint=BLOCKIFY_RUNTIME_RECIPE_FINGERPRINT)
        rep.pop("blockbench_edited", None)
        rep.pop("blockbench_export_frame", None)
        summary = {key: value for key, value in payload.items() if key not in ("voxels", "character_voxels", "character_rig", "character_animations")}
        for version in manifest.get("versions") or []:
            if version.get("file") == source_file:
                version["blockify"] = summary
        # Stage the real generated artifacts and back up every file that the
        # original manifest writer can replace, including runtime descriptors.
        staged = {addon: staged_addon}
        for index, (target, data) in enumerate(artifacts.items()):
            temp = staging / f"new-{index}"
            temp.write_bytes(data)
            staged[target] = temp
        manifest_path = folder / "manifest.json"
        descriptors = [folder / _runtime_descriptor_filename(v) for v in manifest.get("versions") or [] if isinstance(v, dict)]
        targets = list(dict.fromkeys([*staged, manifest_path, *descriptors]))
        backups = {}
        for index, target in enumerate(targets):
            if target.is_symlink() or not target.resolve().is_relative_to(folder):
                raise ValueError("Invalid Blockify replacement target")
            if target.exists():
                backup = staging / f"old-{index}"
                shutil.copy2(target, backup)
                backups[target] = backup
        try:
            for target, temp in staged.items():
                temp.replace(target)
            _write_manifest(folder, manifest)
        except BaseException:
            for target in reversed(targets):
                if target in backups:
                    backups[target].replace(target)
                else:
                    target.unlink(missing_ok=True)
            raise
    log(f"asset-blockify-uci-regenerated uci={uci} blocks={dimensions} addon={addon.name}")
    return payload


def blockbench_resource_session_for_uci(uci: str, *, product_name: str = "") -> dict[str, Any]:
    """Open the existing local Blockify resource pack for a UCI without regenerating it.

    UCI is asset identity.  Blockbench edits the already-persisted Blockify
    representation; opening the editor must never reinterpret the solid model or
    require the retired per-version manifest layout.
    """
    resolved, addon = _uci_blockbench_addon(uci)
    with zipfile.ZipFile(addon, "r") as outer:
        rp_name = next((name for name in outer.namelist() if name.lower().endswith("_rp.mcpack")), "")
        if not rp_name:
            raise ValueError("Minecraft Add-On has no resource pack")
        import io
        with zipfile.ZipFile(io.BytesIO(outer.read(rp_name)), "r") as rp:
            names = rp.namelist()
            geo_name = next((name for name in names if name.startswith("models/entity/") and name.endswith("_character.geo.json")), "")
            animated = bool(geo_name)
            if not geo_name:
                geo_name = next((name for name in names if name.startswith("models/entity/") and name.endswith("_statue.geo.json")), "")
            if not geo_name:
                raise ValueError("Minecraft resource pack has no editable entity geometry")
            geometry = json.loads(rp.read(geo_name).decode("utf-8"))
            animation_name = next((name for name in names if name.startswith("animations/") and name.endswith(".animation.json")), "")
            animations = json.loads(rp.read(animation_name).decode("utf-8")) if animation_name else {"format_version":"1.8.0","animations":{}}
            texture_name = next((name for name in names if name.startswith("textures/entity/") and name.endswith(".png") and not name.endswith("_statue.png")), "") if animated else ""
            if not texture_name:
                texture_name = next((name for name in names if name.startswith("textures/entity/") and name.endswith(".png")), "")
            texture_data_url = ""
            if texture_name:
                texture_data_url = "data:image/png;base64," + base64.b64encode(rp.read(texture_name)).decode("ascii")
    return {
        "uci": resolved["uci"],
        "product_name": str(product_name or resolved.get("name") or resolved["uci"]),
        "model_name": Path(geo_name).name,
        "texture_name": Path(texture_name).name if texture_name else f"{addon.stem}.png",
        "geometry": geometry,
        "animations": animations,
        "texture_data_url": texture_data_url,
        "animated": animated,
        "required_animation_ids": sorted(str(key) for key in (animations.get("animations") or {}).keys()),
        "blockbench_edited": bool((resolved["manifest"].get("representations") or {}).get(f"block_{resolved['block_resolution']}", {}).get("blockbench_edited")),
        "revision": _sha256(addon),
    }


def prepare_blockbench_preview_for_uci(
    uci: str, *, geometry: dict[str, Any], animations: dict[str, Any] | None,
    texture_data_url: str = "", product_name: str = "", texture_rgba: str = "",
) -> dict[str, Any]:
    """Reverse the authored Bedrock grid, never the original Solid model.

    Migrated UCI assets may have only a Block GLTF, not a voxel JSON. The
    previous Bedrock geometry supplies the grid in that case. Keep its origin
    fixed after the first save so adding/removing edge blocks cannot shift it.
    """
    import math
    resolved, addon = _uci_blockbench_addon(uci)
    session = blockbench_resource_session_for_uci(uci, product_name=product_name)
    resolution = int(resolved["block_resolution"])
    block_key = f"block_{resolution}"
    folder = (assets_root() / resolved["folder"]).resolve()
    rep = resolved["manifest"]["representations"][block_key]
    data_path = addon.parent / "blockify.json"
    if data_path.is_symlink():
        raise ValueError("Symlinked Blockify data is not editable")
    data_path.resolve().relative_to(folder)
    if rep.get("blockbench_edited") and (not data_path.is_file() or _sha256(data_path) != rep.get("data_sha256")):
        raise ValueError("Edited Blockify data failed its checksum")
    base = json.loads(data_path.read_text(encoding="utf-8")) if data_path.is_file() else {}
    old_models = session["geometry"]["minecraft:geometry"]
    old_bones = old_models[0].get("bones") or []
    old_cubes = [c for b in old_bones for c in b.get("cubes") or []]
    if not old_cubes:
        raise ValueError("Existing Blockify geometry has no blocks")
    source_rows = base.get("character_voxels" if session["animated"] else "voxels") or []
    frame = base.get("blockbench_frame")
    if not frame:
        if source_rows:
            frame = [(min(v["x"] for v in source_rows)+max(v["x"] for v in source_rows))/2,
                     min(v["y"] for v in source_rows),
                     (min(v["z"] for v in source_rows)+max(v["z"] for v in source_rows))/2]
        else:
            frame = [max(float(c["origin"][0])+float(c["size"][0])-.5 for c in old_cubes),
                     -min(float(c["origin"][1]) for c in old_cubes),
                     max(float(c["origin"][2])+float(c["size"][2])-.5 for c in old_cubes)]
    if not isinstance(frame, list) or len(frame) != 3 or not all(math.isfinite(float(x)) for x in frame):
        raise ValueError("Invalid saved Blockify grid origin")
    texture = texture_data_url or session["texture_data_url"]
    pixels = None
    if texture:
        if not texture.startswith("data:image/png;base64,"):
            raise ValueError("Blockbench texture must be PNG")
        png = base64.b64decode(texture.split(",", 1)[1], validate=True)
        width, height = _png_dimensions(png)
        if len(png) > 16 * 1024 * 1024 or width * height > 16 * 1024 * 1024:
            raise ValueError("Blockbench texture exceeds the local authoring limit")
        rgba = base64.b64decode(texture_rgba, validate=True)
        if len(rgba) != width * height * 4:
            raise ValueError("Decoded Voxel texture does not match its PNG dimensions")
        pixels = (width, height, rgba)
    voxels = _blockbench_static_geometry_to_voxels(geometry, "", base, frame=frame,
                                                    preserve_bones=session["animated"], pixels=pixels)
    # Existing GLTF was exported in the source asset's local frame. Retain its
    # scale/translation via the existing browser exporter, not a new mesh engine.
    reference = source_rows or _blockbench_static_geometry_to_voxels(
        session["geometry"], "", base, frame=frame,
        preserve_bones=session["animated"])
    dimensions = {axis: max(int(base.get(axis) or max(1, resolution // 16)),
                            math.ceil((max(v[coord] for v in voxels)-min(v[coord] for v in voxels)+1)/16))
                  for axis, coord in (("width", "x"), ("height", "y"), ("depth", "z"))}
    payload = {**base, **dimensions, "schema": "cinterforge-blockify-v3", "resolution": 16,
               "voxels": [{k: v for k, v in row.items() if k != "bone"} for row in voxels],
               "voxel_count": len(voxels), "blockbench_frame": frame,
               "blockbench_edited": True, "source_up_normalized": True,
               "product_name": session["product_name"]}
    if session["animated"]:
        # CinterForge exports an intentionally flat, world-space baked rig.
        # Recover exactly that supported rig and numeric animation channels.
        bones = geometry["minecraft:geometry"][0].get("bones") or []
        prior_bones = {b["name"]: b for b in (base.get("character_rig") or {}).get("bones") or []}
        rig = []
        for bone in bones:
            name = str(bone.get("name") or "")
            if name == "cinterforge_root" and not bone.get("cubes"):
                continue
            if bone.get("parent") not in (None, "", "cinterforge_root"):
                raise ValueError("Blockify preview requires the existing flat Bedrock rig")
            pivot = bone.get("pivot") or [0, 0, 0]
            rig.append({**prior_bones.get(name, {}), "name": name, "parent": "",
                        "pivot": [frame[0]-float(pivot[0]), frame[1]+float(pivot[1]), frame[2]-float(pivot[2])]})
        names = {b["name"] for b in rig}
        if not rig or any(v.get("bone") not in names for v in voxels):
            raise ValueError("Edited character blocks must retain valid bone ownership")
        clips = {}
        prior_clips = base.get("character_animations") or {}
        prior_keys = {_safe_token(str(key)).casefold(): key for key in prior_clips}
        for identifier, clip in (animations or {}).get("animations", {}).items():
            suffix = identifier.rsplit(".", 1)[-1]
            key = prior_keys.get(suffix, identifier)
            previous = prior_clips.get(key) or {}
            channels = {}
            for name, tracks in (clip.get("bones") or {}).items():
                if name not in names or any(k not in ("rotation", "position") for k in tracks):
                    raise ValueError("Blockify preview supports numeric rotation/position on the flat rig")
                row = {}
                for kind, samples in tracks.items():
                    if isinstance(samples, list): samples = {"0": samples}
                    if not isinstance(samples, dict):
                        raise ValueError("Blockify preview requires numeric animation keyframes")
                    clean = {}
                    for t, v in samples.items():
                        if not isinstance(v, list) or len(v) != 3 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v) or not math.isfinite(float(t)):
                            raise ValueError("Blockify preview requires numeric XYZ animation keyframes")
                        clean[str(t)] = [float(x) for x in v]
                    if kind == "position":
                        row[kind] = clean
                    else:
                        # Inverse of the existing exporter: its two X/Z sign
                        # changes cancel, leaving the sampled XYZ Euler basis.
                        quaternions = {}
                        for t, v in clean.items():
                            x,y,z = (math.radians(q)/2 for q in v)
                            cx,cy,cz = math.cos(x),math.cos(y),math.cos(z)
                            sx,sy,sz = math.sin(x),math.sin(y),math.sin(z)
                            quaternions[t] = [sx*cy*cz-cx*sy*sz, cx*sy*cz+sx*cy*sz,
                                              cx*cy*sz-sx*sy*cz, cx*cy*cz+sx*sy*sz]
                        row["rotation"] = {t: [-v[0],v[1],-v[2]] for t,v in clean.items()}
                        row["rotation_quaternion"] = quaternions
                if row: channels[name] = row
            clips[key] = {**previous, "label": previous.get("label") or suffix,
                          "animation_length": float(clip.get("animation_length") or 1),
                          "bones": channels, "bedrock_flat_baked": True}
        payload.update(character_voxels=voxels, character_voxel_count=len(voxels),
                       character_rig={**(base.get("character_rig") or {}), "bones": rig},
                       character_animations=clips)
    else:
        for key in ("character_voxels", "character_voxel_count", "character_rig", "character_animations", "behavior_profile"):
            payload.pop(key, None)
    return {"blockify": payload, "reference_voxels": reference,
            "model_path": resolved["block_model"], "block_resolution": resolution,
            "revision": _sha256(addon), "export_frame": rep.get("blockbench_export_frame")}


def save_blockbench_override_for_uci(
    uci: str, *, geometry: dict[str, Any], animations: dict[str, Any] | None,
    texture_data_url: str = "", product_name: str = "", texture_rgba: str = "",
    preview_files: list[dict[str, Any]] | None = None, export_frame: dict[str, Any] | None = None,
    revision: str = "",
) -> dict[str, Any]:
    """Save the edited document into the same local add-on the editor opens.

    Preserve pack identities, behavior files and all unrelated resource entries.
    This does not regenerate the solid model or publish to Cloudflare.
    """
    resolved, addon = _uci_blockbench_addon(uci)
    session = blockbench_resource_session_for_uci(uci, product_name=product_name)
    models = geometry.get("minecraft:geometry") if isinstance(geometry, dict) else None
    if not isinstance(models, list) or not models or not isinstance(models[0], dict):
        raise ValueError("Blockbench did not return valid Bedrock geometry")
    old_id = session["geometry"]["minecraft:geometry"][0]["description"]["identifier"]
    if models[0].get("description", {}).get("identifier") != old_id:
        raise ValueError("Blockbench must preserve the existing Bedrock geometry identifier")
    animation_payload = animations if isinstance(animations, dict) else {"format_version": "1.8.0", "animations": {}}
    edited = animation_payload.get("animations")
    if not isinstance(edited, dict):
        raise ValueError("Blockbench did not return valid animations")
    missing = set(session["required_animation_ids"]) - set(edited)
    if missing:
        raise ValueError("Blockbench must preserve existing animation identifiers: " + ", ".join(sorted(missing)))
    texture = None
    if texture_data_url:
        if not texture_data_url.startswith("data:image/png;base64,"):
            raise ValueError("Blockbench texture must be PNG")
        texture = base64.b64decode(texture_data_url.split(",", 1)[1], validate=True)
        if len(texture) > 16 * 1024 * 1024 or not texture.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Blockbench texture PNG is invalid or too large")
    if not revision or _sha256(addon) != revision:
        raise ValueError("Voxel asset changed while editing; reopen the editor before saving")
    prepared = prepare_blockbench_preview_for_uci(uci, geometry=geometry, animations=animation_payload,
                                                   texture_data_url=texture_data_url, product_name=product_name, texture_rgba=texture_rgba)
    payload = prepared["blockify"]
    if not isinstance(preview_files, list) or not preview_files or not isinstance(export_frame, dict):
        raise ValueError("Save Back requires the regenerated Blockify GLTF and export frame")
    import math
    scale = export_frame.get("scale")
    center = export_frame.get("center")
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0 or not isinstance(center, dict) or not all(isinstance(center.get(k), (int, float)) and math.isfinite(center[k]) for k in ("x", "y", "z")):
        raise ValueError("Invalid Blockify export frame")
    folder = (assets_root() / resolved["folder"]).resolve()
    block_model = (assets_root() / resolved["block_model"]).resolve()
    block_model.relative_to(folder)
    rep = resolved["manifest"]["representations"][f"block_{resolved['block_resolution']}"]
    thumbnail = (folder / str(rep["thumbnail"])) if rep.get("thumbnail") else block_model.with_name(block_model.stem + "_thumbnail.png")
    if thumbnail.is_symlink() or thumbnail.parent != addon.parent or thumbnail.resolve().parent != addon.parent or thumbnail.suffix.lower() != ".png":
        raise ValueError("Unsafe Blockify thumbnail path")
    artifacts = {}
    document = None
    for row in preview_files:
        name = str(row.get("name") or "")
        if not name or Path(name).name != name or "\\" in name or Path(name).suffix.lower() not in (".gltf", ".bin", ".png", ".jpg", ".jpeg"):
            raise ValueError("Invalid Blockify export resource name")
        if name.lower().endswith(".gltf"):
            if document is not None or row.get("encoding") != "text":
                raise ValueError("Expected exactly one readable Blockify GLTF")
            document = json.loads(row["data"])
            target = block_model
            data = row["data"].encode("utf-8")
        else:
            target = addon.parent / name
            if row.get("encoding") != "base64":
                raise ValueError("Blockify export resource must be binary")
            data = base64.b64decode(row["data"], validate=True)
        if target in artifacts or target in (addon, thumbnail, addon.parent / "blockify.json") or target.is_symlink():
            raise ValueError("Invalid duplicate or conflicting Blockify export resource")
        artifacts[target] = data
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0" or not document.get("meshes") or not document.get("buffers") or not document.get("accessors"):
        raise ValueError("Blockify export has no GLTF mesh")
    for entry in (document.get("buffers") or []) + (document.get("images") or []):
        uri = entry.get("uri")
        if not isinstance(uri, str) or Path(uri).name != uri or addon.parent / uri not in artifacts:
            raise ValueError("Blockify GLTF references an unowned resource")
        if "byteLength" in entry and len(artifacts[addon.parent / uri]) != int(entry["byteLength"]):
            raise ValueError("Blockify GLTF buffer length does not match its document")
    if session["animated"] and (not document.get("skins") or len(document.get("animations") or []) != len(payload.get("character_animations") or {})):
        raise ValueError("Blockify export lost character rig or animations")
    data_path = addon.parent / "blockify.json"
    payload["file"] = data_path.relative_to(folder).as_posix()
    payload["thumbnail"] = thumbnail.relative_to(folder).as_posix()
    artifacts[data_path] = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    artifacts[thumbnail] = _blockify_hero_icon_png(payload["voxels"], size=512, transparent=False)
    rep.update(width=payload["width"], height=payload["height"], depth=payload["depth"],
               data=payload["file"], data_sha256=hashlib.sha256(artifacts[data_path]).hexdigest(),
               voxel_count=len(payload["voxels"]), thumbnail=thumbnail.relative_to(folder).as_posix(),
               files=[path.relative_to(folder).as_posix() for path in artifacts if path not in (data_path, thumbnail)],
               blockbench_edited=True, blockbench_export_frame=export_frame)
    with zipfile.ZipFile(addon) as outer:
        outer_rows = [(info, outer.read(info)) for info in outer.infolist()]
    rp_name = next(info.filename for info, _ in outer_rows if info.filename.lower().endswith("_rp.mcpack"))
    rp_bytes = next(data for info, data in outer_rows if info.filename == rp_name)
    with zipfile.ZipFile(io.BytesIO(rp_bytes)) as rp:
        rows = [(info, rp.read(info)) for info in rp.infolist()]
    geo_name = next(info.filename for info, _ in rows if info.filename.startswith("models/entity/") and Path(info.filename).name == session["model_name"])
    texture_name = next((info.filename for info, _ in rows if info.filename.startswith("textures/entity/") and Path(info.filename).name == session["texture_name"]), "")
    animation_name = next((info.filename for info, _ in rows if info.filename.startswith("animations/") and info.filename.endswith(".animation.json")), "")
    if texture is not None and not texture_name:
        raise ValueError("Existing resource pack has no editable texture")
    if edited and not animation_name:
        raise ValueError("Existing resource pack has no animation document")
    replacements = {geo_name: json.dumps(geometry, ensure_ascii=False).encode("utf-8")}
    if texture is not None:
        replacements[texture_name] = texture
    if animation_name:
        replacements[animation_name] = json.dumps(animation_payload, ensure_ascii=False).encode("utf-8")
    updated = io.BytesIO()
    with zipfile.ZipFile(updated, "w", compression=zipfile.ZIP_DEFLATED) as rp:
        for info, data in rows:
            rp.writestr(info, replacements.get(info.filename, data))
    # Stage every derivative before replacing the existing files. On an I/O
    # failure restore the previous versions, including the add-on and manifest.
    from .asset_management import _normalize_legacy_manifest_paths, _validate_manifest_relative_paths
    import shutil
    manifest = resolved["manifest"]
    _normalize_legacy_manifest_paths(manifest)
    _validate_manifest_relative_paths(manifest)
    manifest_path = folder / "manifest.json"
    artifacts[manifest_path] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.TemporaryDirectory(dir=addon.parent, prefix="blockbench-") as scratch:
        staging = Path(scratch)
        staged_addon = staging / "addon.mcaddon"
        with zipfile.ZipFile(staged_addon, "w", compression=zipfile.ZIP_DEFLATED) as outer:
            for info, data in outer_rows:
                outer.writestr(info, updated.getvalue() if info.filename == rp_name else data)
        rep["minecraft_sha256"] = _sha256(staged_addon)
        rep["minecraft_data_sha256"] = rep["data_sha256"]
        artifacts[manifest_path] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        staged = {addon: staged_addon}
        for index, (target, data) in enumerate(artifacts.items()):
            staged_file = staging / f"new-{index}"
            staged_file.write_bytes(data)
            staged[target] = staged_file
        backups = {}
        replaced = []
        for index, target in enumerate(staged):
            if target.is_symlink():
                raise ValueError("Cannot replace a symlinked Blockify artifact")
            if target.exists():
                backup = staging / f"old-{index}"
                shutil.copy2(target, backup)
                os.chmod(staged[target], target.stat().st_mode & 0o777)
                backups[target] = backup
        try:
            for target, staged_file in staged.items():
                staged_file.replace(target)
                replaced.append(target)
        except BaseException:
            for target in reversed(replaced):
                if target in backups:
                    backups[target].replace(target)
                else:
                    target.unlink(missing_ok=True)
            raise
    log(f"asset-blockbench-uci-saved uci={resolved['uci']} addon={addon.name} voxels={len(payload['voxels'])}")
    return {"uci": resolved["uci"], "addon": addon.relative_to(assets_root().resolve()).as_posix(),
            "blockify": payload, "thumbnail": thumbnail.relative_to(assets_root().resolve()).as_posix()}


def _blockbench_static_geometry_to_voxels(geometry: dict[str, Any], texture_data_url: str, base: dict[str, Any], *, frame: list[float] | None = None, preserve_bones: bool = False, pixels: tuple[int, int, bytes] | None = None) -> list[dict[str, Any]]:
    """Recover CinterForge static voxel coordinates/colors from an edited Bedrock cube model.

    CinterForge's static Blockbench session is deliberately one 1x1x1 Bedrock cube
    per Blockify voxel.  Converting those cubes back to the original integer voxel
    grid makes Blockbench a true round trip instead of a Minecraft-export-only
    override.  Non-unit/off-grid cubes fail loudly rather than being re-voxelized.
    """
    import math
    rows=list(base.get("voxels") or [])
    if frame is None:
        if not rows: raise ValueError("Blockbench round trip has no base Blockify voxels")
        min_x=min(int(v["x"]) for v in rows); max_x=max(int(v["x"]) for v in rows)
        min_y=min(int(v["y"]) for v in rows); min_z=min(int(v["z"]) for v in rows); max_z=max(int(v["z"]) for v in rows)
        center_x=(min_x+max_x)/2.0; center_z=(min_z+max_z)/2.0
    else:
        center_x, min_y, center_z = map(float, frame)
    if pixels is None and texture_data_url:
        if not texture_data_url.startswith("data:image/png;base64,"): raise ValueError("Blockbench texture must be PNG")
        try:
            from PIL import Image
            with Image.open(io.BytesIO(base64.b64decode(texture_data_url.split(",",1)[1],validate=True))) as source_image:
                image=source_image.convert("RGBA")
                pixels=(image.width,image.height,image.tobytes())
        except Exception as exc: raise ValueError(f"Blockbench texture PNG could not be read: {exc}") from exc
    palette=list(base.get("palette") or [])
    def cube_rgb(cube: dict[str,Any]) -> tuple[int,int,int]:
        uv=cube.get("uv")
        if pixels is not None and isinstance(uv,dict):
            image_width,image_height,rgba=pixels
            for face in ("north","south","east","west","up","down"):
                q=uv.get(face) if isinstance(uv.get(face),dict) else None
                pos=q.get("uv") if q else None
                if isinstance(pos,list) and len(pos)>=2:
                    size=q.get("uv_size") or [1,1]
                    x=max(0,min(image_width-1,int(math.floor(float(pos[0])+float(size[0])/2)))); y=max(0,min(image_height-1,int(math.floor(float(pos[1])+float(size[1])/2))))
                    offset=(y*image_width+x)*4
                    r,g,b,a=rgba[offset:offset+4]
                    if a: return int(r),int(g),int(b)
        if pixels is not None and isinstance(uv,list) and len(uv)>=2:
            image_width,image_height,rgba=pixels
            size=cube.get("size") or [1,1,1]
            x=max(0,min(image_width-1,int(math.floor(float(uv[0])+float(size[2])+float(size[0])/2))))
            y=max(0,min(image_height-1,int(math.floor(float(uv[1])+float(size[2])+float(size[1])/2))))
            offset=(y*image_width+x)*4
            r,g,b,a=rgba[offset:offset+4]
            if a: return int(r),int(g),int(b)
        if palette:
            q=palette[0]; return int(q.get("r",255)),int(q.get("g",255)),int(q.get("b",255))
        return 255,255,255
    geometries=geometry.get("minecraft:geometry") if isinstance(geometry,dict) else None
    if not isinstance(geometries,list) or not geometries: raise ValueError("Blockbench returned no Minecraft geometry")
    cubes=[]
    for bone in list((geometries[0] or {}).get("bones") or []):
        if isinstance(bone,dict):
            if frame is not None and any(abs(float(v))>1e-4 for v in bone.get("rotation") or []):
                raise ValueError("Blockify round trip requires an unrotated bind rig")
            cubes.extend((str(bone.get("name") or ""), c) for c in list(bone.get("cubes") or []) if isinstance(c,dict))
    out=[]; seen=set()
    for bone_name, cube in cubes:
        if frame is not None and (any(abs(float(v))>1e-4 for v in cube.get("rotation") or []) or abs(float(cube.get("inflate") or 0))>1e-4):
            raise ValueError("Blockify round trip requires unrotated, uninflated block cubes")
        origin=cube.get("origin"); size=cube.get("size")
        if not (isinstance(origin,list) and len(origin)>=3 and isinstance(size,list) and len(size)>=3):
            raise ValueError("Blockbench cube has no valid origin or size")
        if any(abs(float(size[i])-1.0)>1e-4 for i in range(3)):
            raise ValueError("CinterForge Blockbench round trip requires 1x1x1 block cubes; resize blocks only by adding/removing cubes")
        bx=float(origin[0])+0.5; by=float(origin[1]); bz=float(origin[2])+0.5
        x=center_x-bx; y=min_y+by; z=center_z-bz
        xyz=(int(round(x)),int(round(y)),int(round(z)))
        if max(abs(x-xyz[0]),abs(y-xyz[1]),abs(z-xyz[2]))>1e-4:
            raise ValueError("CinterForge Blockbench round trip requires cubes snapped to the block grid")
        identity=(bone_name, *xyz) if preserve_bones else xyz
        if identity in seen: continue
        seen.add(identity); r,g,b=cube_rgb(cube)
        row={"x":xyz[0],"y":xyz[1],"z":xyz[2],"r":r,"g":g,"b":b,"samples":1}
        if preserve_bones: row["bone"] = bone_name
        out.append(row)
    if not out: raise ValueError("Blockbench round trip returned no blocks")
    return out

def save_blockbench_override(
    folder_rel: str, filename: str, *, geometry: dict[str, Any], animations: dict[str, Any] | None,
    texture_data_url: str = "", product_name: str = "",
) -> dict[str, Any]:
    """Persist Blockbench edits as a portable override and rebuild the Minecraft Add-On."""
    folder = _resolve_folder(folder_rel)
    source = (folder / filename).resolve()
    if folder not in source.parents or not source.is_file():
        raise FileNotFoundError(filename)
    if not isinstance(geometry, dict) or not isinstance(geometry.get("minecraft:geometry"), list) or not geometry.get("minecraft:geometry"):
        raise ValueError("Blockbench did not return valid Bedrock geometry")
    session = _blockbench_resource_session(folder_rel, filename, product_name=product_name)
    required = set(session.get("required_animation_ids") or [])
    animation_payload = animations if isinstance(animations, dict) else {"format_version":"1.8.0","animations":{}}
    edited = animation_payload.get("animations") if isinstance(animation_payload.get("animations"), dict) else {}
    missing = sorted(required - set(str(key) for key in edited.keys()))
    if missing:
        raise ValueError("Blockbench animation rename/removal is not yet allowed for CinterForge runtime clips: " + ", ".join(missing[:8]))
    texture_data_url = str(texture_data_url or "")
    if texture_data_url and not texture_data_url.startswith("data:image/png;base64,"):
        raise ValueError("Blockbench texture must be PNG")
    if texture_data_url:
        raw = base64.b64decode(texture_data_url.split(",",1)[1], validate=True)
        if len(raw) > 16 * 1024 * 1024 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Blockbench texture PNG is invalid or too large")
    manifest = _load_manifest(folder)
    version = next((v for v in manifest.get("versions") or [] if str(v.get("file") or "") == source.name), None)
    if not isinstance(version, dict):
        raise ValueError(f"Saved asset version is not present in manifest: {folder_rel}/{filename}")
    existing_meta = version.get("blockify") if isinstance(version.get("blockify"), dict) else {}
    # Static 3D assets round-trip through canonical Blockify voxels.  Blockbench
    # additions/removals therefore immediately become the next Minecraft Preview
    # and future exports instead of living only in a Bedrock resource override.
    if not existing_meta.get("image_asset") and not bool(session.get("animated")):
        base = load_blockify(folder_rel, filename)
        edited_voxels = _blockbench_static_geometry_to_voxels(geometry, texture_data_url or str(session.get("texture_data_url") or ""), base)
        updated = save_blockify(
            folder_rel, filename, width=int(base.get("width") or existing_meta.get("width") or 1),
            height=int(base.get("height") or existing_meta.get("height") or 1), depth=int(base.get("depth") or existing_meta.get("depth") or 1),
            resolution=int(base.get("resolution") or 16), voxels=edited_voxels, product_name=str(product_name or session.get("product_name") or source.stem),
        )
        # Keep the exact Blockbench document as an owned representation too.  The
        # voxel bake drives CinterForge preview/editing; the override preserves
        # Blockbench-specific geometry metadata and remains the Bedrock export view.
        override_name=f"{source.stem}.blockbench.json"; override_path=folder/override_name
        override_path.write_text(json.dumps({"schema":"cinterforge.blockbench-override.v2","source_file":source.name,"product_name":str(product_name or session.get("product_name") or source.stem),"saved_at":datetime.now(timezone.utc).isoformat(),"geometry":geometry,"animations":animation_payload,"texture_data_url":texture_data_url},ensure_ascii=False,separators=(",",":")),encoding="utf-8")
        manifest=_load_manifest(folder); version=next((v for v in manifest.get("versions") or [] if str(v.get("file") or "")==source.name),None)
        if not isinstance(version,dict): raise ValueError(f"Saved asset version is not present in manifest: {folder_rel}/{filename}")
        meta=version.get("blockify") if isinstance(version.get("blockify"),dict) else {}; meta["blockbench_file"]=override_name; meta["blockbench_edited"]=True; version["blockify"]=meta; _normalize_version_artifacts(version); _write_manifest(folder,manifest)
        updated=regenerate_blockify_addon(folder_rel,filename,product_name=str(product_name or session.get("product_name") or source.stem))
        log(f"asset-blockbench-voxel-roundtrip source={source.relative_to(assets_root()).as_posix()} voxels={len(edited_voxels)} override={override_name}")
        return {"blockify":updated,"blockbench_file":override_name,"voxel_roundtrip":True,"voxel_count":len(edited_voxels)}
    if existing_meta.get("image_asset"):
        addon = (folder / str(existing_meta.get("mcaddon_file") or "")).resolve()
        if folder not in addon.parents or not addon.is_file():
            raise FileNotFoundError(str(existing_meta.get("mcaddon_file") or "Minecraft Image Add-On"))
        import io
        with zipfile.ZipFile(addon, "r") as outer:
            outer_rows = [(info, outer.read(info.filename)) for info in outer.infolist()]
        rp_name = next((info.filename for info, _ in outer_rows if info.filename.lower().endswith("_rp.mcpack")), "")
        if not rp_name:
            raise ValueError("Minecraft Image Add-On has no resource pack")
        rp_bytes = next(data for info, data in outer_rows if info.filename == rp_name)
        with zipfile.ZipFile(io.BytesIO(rp_bytes), "r") as rp:
            rp_rows = [(info, rp.read(info.filename)) for info in rp.infolist()]
        geo_name = next((info.filename for info, _ in rp_rows if info.filename.startswith("models/entity/") and info.filename.endswith("_statue.geo.json")), "")
        texture_name = next((info.filename for info, _ in rp_rows if info.filename.startswith("textures/entity/") and info.filename.endswith("_image_statue.png")), "")
        if not geo_name or not texture_name:
            raise ValueError("Minecraft Image Add-On has no editable Statue geometry/texture")
        geometry_bytes = json.dumps(geometry, indent=2, ensure_ascii=False).encode("utf-8")
        texture_bytes = raw if texture_data_url else next(data for info, data in rp_rows if info.filename == texture_name)
        rp_out = io.BytesIO()
        with zipfile.ZipFile(rp_out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
            for info, data in rp_rows:
                if info.filename == geo_name: data = geometry_bytes
                elif info.filename == texture_name: data = texture_bytes
                zout.writestr(info, data)
        addon_tmp = addon.with_suffix(addon.suffix + ".blockbench.tmp")
        try:
            with zipfile.ZipFile(addon_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
                for info, data in outer_rows:
                    zout.writestr(info, rp_out.getvalue() if info.filename == rp_name else data)
            addon_tmp.replace(addon)
        finally:
            addon_tmp.unlink(missing_ok=True)
        override_name = f"{source.stem}.blockbench.json"
        override_path = folder / override_name
        override_path.write_text(json.dumps({
            "schema":"cinterforge.image-blockbench-override.v1",
            "source_file":source.name,
            "product_name":str(product_name or session.get("product_name") or source.stem),
            "saved_at":datetime.now(timezone.utc).isoformat(),
            "geometry":geometry,
            "texture_data_url":texture_data_url,
        }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        existing_meta["blockbench_file"] = override_name
        existing_meta["blockbench_edited"] = True
        version["blockify"] = existing_meta
        _normalize_version_artifacts(version)
        _write_manifest(folder, manifest)
        log(f"asset-image-blockbench-saved source={source.relative_to(assets_root()).as_posix()} override={override_name}")
        return {"blockify":dict(existing_meta), "blockbench_file":override_name}
    version = next((v for v in manifest.get("versions") or [] if str(v.get("file") or "") == source.name), None)
    if not isinstance(version, dict):
        raise ValueError(f"Saved asset version is not present in manifest: {folder_rel}/{filename}")
    meta = existing_meta
    override_name = f"{source.stem}.blockbench.json"
    override_path = folder / override_name
    payload = {
        "schema": "cinterforge.blockbench-override.v1",
        "source_file": source.name,
        "product_name": str(product_name or session.get("product_name") or source.stem),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "geometry": geometry,
        "animations": animation_payload,
        "texture_data_url": texture_data_url,
    }
    override_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    meta["blockbench_file"] = override_name
    meta["blockbench_edited"] = True
    version["blockify"] = meta
    _normalize_version_artifacts(version)
    _write_manifest(folder, manifest)
    updated = regenerate_blockify_addon(folder_rel, filename, product_name=str(product_name or session.get("product_name") or ""))
    log(f"asset-blockbench-saved source={source.relative_to(assets_root()).as_posix()} override={override_name}")
    return {"blockify": updated, "blockbench_file": override_name}

