#!/usr/bin/env python3
"""Inventory the legacy Cloudflare asset library for the one-time UCI migration.

Phase 1 is intentionally non-destructive.  It inventories the canonical local
Builds/Assets tree first, enumerates the authenticated Cloudflare
Assets prefix, downloads only files missing locally directly into that same tree, and
writes an editable XLSX catalog.  No source asset is converted, renamed, moved,
deleted, or published.  The migration never creates a second copy of the asset tree.

The catalog is the migration contract for the later all-at-once pass.  Existing
rows keep their assigned UCI and Desired Block Size when the catalog is refreshed.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow this script to be executed directly while importing shared build_cinterforge modules.
_BUILD_CINTERFORGE_ROOT = Path(__file__).resolve().parent.parent
if str(_BUILD_CINTERFORGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUILD_CINTERFORGE_ROOT))

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parent.parents[2]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from cf.io.CinterFileIO import cinterforgeBuildsRootPath

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

CATALOG_SCHEMA = "cinterforge.asset-migration-catalog.v1"
DEFAULT_BLOCK_SIZE = 1
ASSET_UCI_PUBLISHER = "CINTER"
ASSET_UCI_TYPE = "ASST"
MODEL_SUFFIXES = {".glb", ".gltf"}

COLUMNS = [
    "Asset Name",
    "Top Level Folder",
    "UCI",
    "Source Format",
    "Desired Block Size",
    "Notes",
]



def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", value or "").upper()
    return (cleaned[:8] or "ASSET")


def _asset_uci(asset_name: str, migration_timestamp: str) -> str:
    identifier = _safe_identifier(asset_name)
    return f"{ASSET_UCI_PUBLISHER}{ASSET_UCI_TYPE}{migration_timestamp}#{identifier}en"


def _is_legacy_migration_uci(value: str) -> bool:
    return bool(re.fullmatch(r"CINTERASST\d{8}[A-Z0-9]+#\d+en", value or ""))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _legacy_row(root: Path, source: Path, migration_timestamp: str) -> dict[str, Any]:
    relative = source.relative_to(root).as_posix()
    parts = Path(relative).parts
    top_level = parts[0] if len(parts) > 1 else ""
    source_hash = _sha256(source)
    stem = source.stem
    folder = source.parent
    blockify_path = folder / f"{stem}.blockify.json"
    edits_path = folder / f"{stem}.blockify.edits.json"
    addon_path = folder / f"{stem}.mcaddon"
    manifest_candidates = [folder / "manifest.json", folder / f"{stem}.manifest.json"]
    manifest_path = next((path for path in manifest_candidates if path.is_file()), None)
    blockify = _json(blockify_path) if blockify_path.is_file() else {}

    existing_size: Any = ""
    for key in ("block_size", "size_blocks", "minecraft_block_size"):
        if blockify.get(key) not in (None, ""):
            existing_size = blockify[key]
            break

    return {
        "Legacy Asset Path": relative,
        "Top Level Folder": top_level,
        "Asset Name": stem,
        "Source Format": source.suffix.casefold().lstrip("."),
        "Source SHA256": source_hash,
        "Source Bytes": source.stat().st_size,
        "UCI": _asset_uci(stem, migration_timestamp),
        "Existing Blockify": blockify_path.is_file(),
        "Existing Block Size": existing_size,
        "Existing Blockify Resolution": blockify.get("resolution", ""),
        "Existing Edits": edits_path.is_file() or isinstance(blockify.get("voxel_edits"), dict),
        "Existing MCAddon": addon_path.is_file(),
        "Existing Manifest": manifest_path is not None,
        "Desired Block Size": DEFAULT_BLOCK_SIZE,
        "Notes": "",
    }


def inventory_assets(root: Path, migration_timestamp: str) -> list[dict[str, Any]]:
    root = root.resolve()
    sources = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in MODEL_SUFFIXES),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    return [_legacy_row(root, source, migration_timestamp) for source in sources]


def _existing_user_values(workbook_path: Path) -> dict[str, dict[str, Any]]:
    if not workbook_path.is_file():
        return {}
    workbook = load_workbook(workbook_path, data_only=False)
    if "Assets" not in workbook.sheetnames:
        return {}
    sheet = workbook["Assets"]
    headers = [str(cell.value or "") for cell in sheet[1]]
    positions = {name: index for index, name in enumerate(headers)}
    retained: dict[str, dict[str, Any]] = {}
    for values in sheet.iter_rows(min_row=2, values_only=True):
        # Old catalogs had Legacy Asset Path; new catalogs intentionally expose only
        # the six migration fields.  Asset Name + Top Level Folder is sufficient
        # to preserve user-entered values across refreshes in the new sheet.
        if "Legacy Asset Path" in positions:
            identity = str(values[positions["Legacy Asset Path"]] or "").strip()
        else:
            name = str(values[positions["Asset Name"]] or "").strip() if "Asset Name" in positions else ""
            top = str(values[positions["Top Level Folder"]] or "").strip() if "Top Level Folder" in positions else ""
            identity = f"{top}\0{name}" if name else ""
        if not identity:
            continue
        uci_column = "UCI" if "UCI" in positions else "Assigned UCI"
        notes_column = "Notes" if "Notes" in positions else "Migration Notes"
        retained[identity] = {
            "UCI": values[positions[uci_column]] if uci_column in positions else "",
            "Desired Block Size": values[positions["Desired Block Size"]] if "Desired Block Size" in positions else DEFAULT_BLOCK_SIZE,
            "Notes": values[positions[notes_column]] if notes_column in positions else "",
        }
    return retained


def write_catalog(rows: list[dict[str, Any]], workbook_path: Path, *, source_root: Path, migration_timestamp: str) -> None:
    retained = _existing_user_values(workbook_path)
    for row in rows:
        legacy_identity = str(row["Legacy Asset Path"])
        compact_identity = f'{row["Top Level Folder"]}\0{row["Asset Name"]}'
        previous = retained.get(legacy_identity) or retained.get(compact_identity)
        if not previous:
            continue
        previous_uci = str(previous.get("UCI") or "").strip()
        if previous_uci and not _is_legacy_migration_uci(previous_uci):
            row["UCI"] = previous_uci
        desired = previous.get("Desired Block Size")
        if desired not in (None, ""):
            row["Desired Block Size"] = desired
        if previous.get("Notes") not in (None, ""):
            row["Notes"] = previous["Notes"]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Assets"
    sheet.append(COLUMNS)
    for row in rows:
        sheet.append([row.get(column, "") for column in COLUMNS])

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = {"A": 32, "B": 24, "C": 44, "D": 16, "E": 24, "F": 40}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)
    if rows:
        table = Table(displayName="AssetMigrationCatalog", ref=f"A1:F{len(rows)+1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True, showFirstColumn=False, showLastColumn=False)
        sheet.add_table(table)

    info = workbook.create_sheet("Migration Info")
    info_rows = [
        ("Schema", CATALOG_SCHEMA),
        ("Generated UTC", datetime.now(timezone.utc).isoformat()),
        ("Migration Timestamp", migration_timestamp),
        ("Downloaded Root", str(source_root.resolve())),
        ("Asset Count", len(rows)),
        ("Default Desired Block Size", DEFAULT_BLOCK_SIZE),
        ("Rule", "Desired Block Size is the authoritative sizing input; Blockify resolution is derived as size × 16."),
        ("Phase", "Inventory only: no conversion, rename, delete, or publish is performed."),
    ]
    for key, value in info_rows:
        info.append([key, value])
    info.column_dimensions["A"].width = 30
    info.column_dimensions["B"].width = 100
    for cell in info[1]:
        cell.font = Font(bold=True)

    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(workbook_path)


def sync_missing_cloudflare_assets(local_assets_root: Path) -> tuple[int, int]:
    from cf.ifserver.assets.asset_management import _authenticated_cloudflare_asset_keys, _cloudflare_download_to

    local_assets_root = local_assets_root.resolve()
    local_assets_root.mkdir(parents=True, exist_ok=True)
    keys = _authenticated_cloudflare_asset_keys()
    downloaded = 0
    already_local = 0
    for index, relative in enumerate(keys, start=1):
        target = local_assets_root / Path(relative)
        if target.is_file():
            already_local += 1
            continue
        _cloudflare_download_to(relative, target)
        downloaded += 1
        print(f"[asset-migration] download-missing {index}/{len(keys)} {relative}", flush=True)
    return downloaded, already_local


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare the CinterForge one-time asset migration catalog.")
    parser.add_argument("--catalog", action="store_true", help="Inventory the normal local Assets tree, download only Cloudflare files missing there, and write the migration XLSX.")
    parser.add_argument("--source-root", type=Path, help="Inventory this existing tree without Cloudflare synchronization (test/offline use).")
    builds_root = cinterforgeBuildsRootPath()
    parser.add_argument("--assets-root", type=Path, default=builds_root / "Assets", help="Canonical local asset root in external Builds.")
    parser.add_argument("--spreadsheet", type=Path, default=builds_root / "InteractiveFiction" / "AssetMigration.xlsx", help="Output XLSX path in external Builds.")
    parser.add_argument("--migration-timestamp", default=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"), help="UTC YYYYMMDDHHMMSS used when minting initial asset UCIs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.catalog:
        raise SystemExit("Specify --catalog. The destructive conversion/build phase is intentionally not enabled until the one-asset format proof is complete.")
    if not re.fullmatch(r"\d{14}", str(args.migration_timestamp)):
        raise SystemExit("--migration-timestamp must be UTC YYYYMMDDHHMMSS")

    source_root = args.source_root.resolve() if args.source_root else args.assets_root.resolve()
    spreadsheet = args.spreadsheet.resolve()

    if args.source_root is None:
        downloaded, already_local = sync_missing_cloudflare_assets(source_root)
        print(
            f"[asset-migration] Cloudflare sync root={source_root} "
            f"already-local={already_local} downloaded-missing={downloaded}",
            flush=True,
        )

    rows = inventory_assets(source_root, str(args.migration_timestamp))
    if not rows:
        raise RuntimeError(f"No GLB/GLTF assets found under migration source: {source_root}")
    write_catalog(rows, spreadsheet, source_root=source_root, migration_timestamp=str(args.migration_timestamp))
    print(f"[asset-migration] catalog assets={len(rows)} spreadsheet={spreadsheet}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
