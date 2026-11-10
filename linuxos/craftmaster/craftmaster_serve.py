#!/usr/bin/env python3
"""Prepare a converted .mcworld for an interactive CraftMaster BDS session."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow this script to be executed directly while importing shared build_cinterforge modules.
_BUILD_CINTERFORGE_ROOT = Path(__file__).resolve().parent.parent
if str(_BUILD_CINTERFORGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUILD_CINTERFORGE_ROOT))

import argparse
import json
import os
import platform
import shutil
import struct
import uuid
import zipfile
from pathlib import Path, PurePosixPath


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members=[]
    for info in zf.infolist():
        p=PurePosixPath(info.filename)
        if p.is_absolute() or '..' in p.parts:
            raise RuntimeError(f'unsafe path in mcworld: {info.filename}')
        members.append(info)
    return members


def _read_level_name(staging: Path, fallback: str) -> str:
    levelname=staging/'levelname.txt'
    if levelname.is_file():
        name=levelname.read_text(encoding='utf-8',errors='replace').strip()
        if name: return name
    return fallback


def _set_property(path: Path, key: str, value: str) -> None:
    lines=path.read_text(encoding='utf-8',errors='replace').splitlines() if path.exists() else []
    out=[]; replaced=False
    for line in lines:
        if line.strip().startswith(key+'='):
            if not replaced:
                out.append(f'{key}={value}'); replaced=True
        else:
            out.append(line)
    if not replaced: out.append(f'{key}={value}')
    path.write_text('\n'.join(out)+'\n',encoding='utf-8')



def _read_world_game_mode(level_dat: Path) -> int:
    """Return the authored Bedrock GameType from level.dat (0..3), failing loudly."""
    data=level_dat.read_bytes()
    if len(data) < 11:
        raise RuntimeError(f'Bedrock level.dat is too small to contain GameType: {level_dat}')
    payload_len=struct.unpack_from('<I',data,4)[0]
    end=8+payload_len
    if payload_len > len(data)-8:
        raise RuntimeError(f'invalid Bedrock level.dat payload length: {level_dat}')
    def need(pos,n):
        if pos < 0 or pos+n > end: raise RuntimeError(f'truncated Bedrock level.dat NBT: {level_dat}')
    def u16(pos): need(pos,2); return struct.unpack_from('<H',data,pos)[0],pos+2
    def i32(pos): need(pos,4); return struct.unpack_from('<i',data,pos)[0],pos+4
    def string(pos):
        n,pos=u16(pos); need(pos,n)
        return data[pos:pos+n].decode('utf-8'),pos+n
    found=[]
    def payload(tag,pos):
        if tag==1: need(pos,1); return pos+1
        if tag==2: need(pos,2); return pos+2
        if tag in (3,5): need(pos,4); return pos+4
        if tag in (4,6): need(pos,8); return pos+8
        if tag==7:
            n,pos=i32(pos); need(pos,n); return pos+n
        if tag==8: return string(pos)[1]
        if tag==9:
            need(pos,1); child=data[pos]; pos+=1; n,pos=i32(pos)
            if n < 0: raise RuntimeError(f'invalid negative NBT length in {level_dat}')
            for _ in range(n): pos=payload(child,pos)
            return pos
        if tag==10:
            while True:
                need(pos,1); child=data[pos]; pos+=1
                if child==0: return pos
                name,pos=string(pos)
                if name=='GameType' and child==3:
                    need(pos,4); found.append(struct.unpack_from('<i',data,pos)[0])
                pos=payload(child,pos)
        if tag==11:
            n,pos=i32(pos); need(pos,n*4); return pos+n*4
        if tag==12:
            n,pos=i32(pos); need(pos,n*8); return pos+n*8
        raise RuntimeError(f'unsupported NBT tag {tag} in {level_dat}')
    pos=8; need(pos,1); root=data[pos]; pos+=1
    if root != 10: raise RuntimeError(f'Bedrock level.dat root is not TAG_Compound: {level_dat}')
    _,pos=string(pos); payload(10,pos)
    if len(found) != 1 or found[0] not in (0,1,2,3):
        raise RuntimeError(f'Bedrock level.dat must contain exactly one valid GameType: {level_dat}')
    return found[0]


def prepare_world(mcworld: Path, server: Path) -> str:
    mcworld=mcworld.expanduser().resolve(); server=server.expanduser().resolve()
    if not mcworld.is_file(): raise RuntimeError(f'mcworld not found: {mcworld}')
    if mcworld.suffix.lower() != '.mcworld': raise RuntimeError(f'expected .mcworld file: {mcworld}')
    executable='bedrock_server.exe' if platform.system() == 'Windows' else 'bedrock_server'
    if not (server/executable).is_file(): raise RuntimeError(f'BDS executable not found: {server / executable}')
    staging=server/'.cinter'/'world-import'
    shutil.rmtree(staging,ignore_errors=True); staging.mkdir(parents=True)
    with zipfile.ZipFile(mcworld) as zf:
        members=_safe_members(zf); zf.extractall(staging,members)
    if not (staging/'level.dat').is_file() or not (staging/'db').is_dir():
        raise RuntimeError(f'mcworld is not a Bedrock world (missing level.dat or db/): {mcworld}')
    game_mode=_read_world_game_mode(staging/'level.dat')
    level_name=_read_level_name(staging,mcworld.stem)
    if '/' in level_name or '\\' in level_name or level_name in {'.','..'}:
        raise RuntimeError(f'invalid Bedrock level name: {level_name!r}')
    dest=server/'worlds'/level_name
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.rmtree(dest,ignore_errors=True)
    staging.replace(dest)
    props=server/'server.properties'
    _set_property(props,'level-name',level_name)
    _set_property(props,'server-name',level_name)
    _set_property(props,'online-mode','true')
    _set_property(props,'allow-list','false')
    _set_property(props,'allow-cheats','true')
    _set_property(props,'content-log-console-output-enabled','true')
    game_mode_name={0:'survival',1:'creative',2:'adventure',3:'spectator'}[game_mode]
    _set_property(props,'gamemode',game_mode_name)
    _set_property(props,'force-gamemode','false')
    print(f'CINTER_CRAFTMASTER_WORLD_READY name={level_name}')
    print(f'CINTER_CRAFTMASTER_WORLD_DIR {dest}')
    return level_name



def prepare_world_directory(source_world: Path, server: Path) -> str:
    """Deploy a local Bedrock world directory into BDS without modifying the source save."""
    source_world=source_world.expanduser().resolve(); server=server.expanduser().resolve()
    if not source_world.is_dir(): raise RuntimeError(f'Bedrock world directory not found: {source_world}')
    if not (source_world/'level.dat').is_file() or not (source_world/'db').is_dir():
        raise RuntimeError(f'Bedrock world directory is missing level.dat or db/: {source_world}')
    executable='bedrock_server.exe' if platform.system() == 'Windows' else 'bedrock_server'
    if not (server/executable).is_file(): raise RuntimeError(f'BDS executable not found: {server / executable}')
    level_name=_read_level_name(source_world,source_world.name)
    if '/' in level_name or '\\' in level_name or level_name in {'.','..'}:
        raise RuntimeError(f'invalid Bedrock level name: {level_name!r}')
    game_mode=_read_world_game_mode(source_world/'level.dat')
    dest=server/'worlds'/level_name
    staging=server/'.cinter'/'world-directory-import'
    shutil.rmtree(staging,ignore_errors=True)
    staging.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(source_world,staging)
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.rmtree(dest,ignore_errors=True)
    staging.replace(dest)
    props=server/'server.properties'
    _set_property(props,'level-name',level_name)
    _set_property(props,'server-name',level_name)
    _set_property(props,'online-mode','true')
    _set_property(props,'allow-list','false')
    _set_property(props,'allow-cheats','true')
    _set_property(props,'content-log-console-output-enabled','true')
    game_mode_name={0:'survival',1:'creative',2:'adventure',3:'spectator'}[game_mode]
    _set_property(props,'gamemode',game_mode_name)
    _set_property(props,'force-gamemode','false')
    print(f'CINTER_CRAFTMASTER_WORLD_READY name={level_name}')
    print(f'CINTER_CRAFTMASTER_WORLD_DIR {dest}')
    return level_name


def _safe_pack_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members=[]
    for info in zf.infolist():
        path=PurePosixPath(info.filename)
        if path.is_absolute() or '..' in path.parts:
            raise RuntimeError(f'unsafe path in Minecraft Add-On pack: {info.filename}')
        members.append(info)
    return members

def _read_pack_manifest(pack_bytes: bytes, label: str) -> tuple[dict, zipfile.ZipFile, object]:
    import io
    stream=io.BytesIO(pack_bytes)
    archive=zipfile.ZipFile(stream)
    try:
        manifest=json.loads(archive.read('manifest.json').decode('utf-8'))
    except Exception as exc:
        archive.close(); stream.close()
        raise RuntimeError(f'Minecraft Add-On pack has no valid manifest.json: {label}: {exc}') from exc
    return manifest,archive,stream

def install_mcaddon(mcaddon: Path, server: Path, world_name: str) -> dict:
    """Install one generated .mcaddon into BDS and bind its packs to one deployed world.

    This only mutates the BDS deployment. The canonical .mcaddon and authored source
    world remain untouched, on both Windows and macOS/Linux BDS layouts.
    """
    mcaddon=mcaddon.expanduser().resolve(); server=server.expanduser().resolve()
    if not mcaddon.is_file(): raise RuntimeError(f'Minecraft Add-On not found: {mcaddon}')
    world=server/'worlds'/world_name
    if not (world/'level.dat').is_file(): raise RuntimeError(f'deployed Bedrock world not found: {world}')
    references={'data':[], 'resources':[]}
    installed=[]
    with zipfile.ZipFile(mcaddon) as outer:
        members=_safe_pack_members(outer)
        pack_members=[info for info in members if not info.is_dir() and PurePosixPath(info.filename).suffix.lower()=='.mcpack']
        if not pack_members: raise RuntimeError(f'Minecraft Add-On contains no .mcpack files: {mcaddon}')
        for member in pack_members:
            payload=outer.read(member)
            manifest,pack,stream=_read_pack_manifest(payload,member.filename)
            try:
                header=manifest.get('header') or {}; uuid=str(header.get('uuid') or '').strip(); version=header.get('version')
                modules=manifest.get('modules') or []; types={str(m.get('type') or '').strip().casefold() for m in modules if isinstance(m,dict)}
                if not uuid or not isinstance(version,list) or len(version)!=3:
                    raise RuntimeError(f'Minecraft Add-On pack has invalid header UUID/version: {member.filename}')
                if 'resources' in types: kind='resources'; pack_root=server/'resource_packs'
                elif 'data' in types: kind='data'; pack_root=server/'behavior_packs'
                else: raise RuntimeError(f'Minecraft Add-On pack has neither resources nor data module: {member.filename}')
                folder=pack_root/f'cinterforge_{uuid}'
                staging=server/'.cinter'/'addon-import'/uuid
                shutil.rmtree(staging,ignore_errors=True); staging.mkdir(parents=True,exist_ok=True)
                pack.extractall(staging,_safe_pack_members(pack))
                folder.parent.mkdir(parents=True,exist_ok=True); shutil.rmtree(folder,ignore_errors=True); staging.replace(folder)
                references[kind].append({'pack_id':uuid,'version':version}); installed.append(str(folder))
            finally:
                pack.close(); stream.close()
    for kind,filename in (('data','world_behavior_packs.json'),('resources','world_resource_packs.json')):
        path=world/filename
        try: current=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else []
        except Exception as exc: raise RuntimeError(f'invalid {filename} in deployed world: {exc}') from exc
        if not isinstance(current,list): raise RuntimeError(f'{filename} must contain a JSON array: {path}')
        by_id={str(row.get('pack_id') or ''):row for row in current if isinstance(row,dict) and row.get('pack_id')}
        for row in references[kind]: by_id[row['pack_id']]=row
        path.write_text(json.dumps(list(by_id.values()),indent=2)+'\n',encoding='utf-8')
    print(f'CINTER_CRAFTMASTER_ADDON_READY addon={mcaddon.name} world={world_name}',flush=True)
    return {'addon':str(mcaddon),'world':world_name,'installed':installed,'behavior_packs':references['data'],'resource_packs':references['resources']}


_CINTER_UTILITY_PACK_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://cinterforge.com/minecraft/cinter-utility-pack"))
_CINTER_UTILITY_MODULE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://cinterforge.com/minecraft/cinter-utility-pack/module"))


def install_cinter_utility_pack(server: Path, world_name: str) -> dict:
    """Create the world-level Cinter command pack from the Cinter items installed in this world."""
    server = server.expanduser().resolve()
    world = server / "worlds" / world_name
    behavior_references_path = world / "world_behavior_packs.json"
    try:
        behavior_references = json.loads(behavior_references_path.read_text(encoding="utf-8")) if behavior_references_path.is_file() else []
    except Exception as exc:
        raise RuntimeError(f"invalid world_behavior_packs.json in deployed world: {exc}") from exc
    if not isinstance(behavior_references, list):
        raise RuntimeError(f"world_behavior_packs.json must contain a JSON array: {behavior_references_path}")

    referenced_pack_ids = {
        str(reference.get("pack_id") or "").strip()
        for reference in behavior_references
        if isinstance(reference, dict)
    }
    item_identifiers = set()
    behavior_root = server / "behavior_packs"
    if behavior_root.is_dir():
        for pack_root in behavior_root.iterdir():
            manifest_path = pack_root / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"invalid behavior-pack manifest: {manifest_path}: {exc}") from exc
            pack_id = str((manifest.get("header") or {}).get("uuid") or "").strip()
            if pack_id not in referenced_pack_ids:
                continue
            items_root = pack_root / "items"
            if not items_root.is_dir():
                continue
            for item_path in items_root.rglob("*.json"):
                try:
                    item_document = json.loads(item_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise RuntimeError(f"invalid Minecraft item definition: {item_path}: {exc}") from exc
                identifier = str(
                    (((item_document.get("minecraft:item") or {}).get("description") or {}).get("identifier")) or ""
                ).strip()
                if identifier.startswith("cinter:"):
                    item_identifiers.add(identifier)

    utility_root = behavior_root / f"cinterforge_{_CINTER_UTILITY_PACK_ID}"
    shutil.rmtree(utility_root, ignore_errors=True)
    (utility_root / "functions" / "cinter").mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": 2,
        "header": {
            "name": "Cinter Utility Commands",
            "description": "World-level utility commands generated by CinterForge.",
            "uuid": _CINTER_UTILITY_PACK_ID,
            "version": [1, 0, 0],
            "min_engine_version": [1, 20, 0],
        },
        "modules": [
            {
                "type": "data",
                "uuid": _CINTER_UTILITY_MODULE_ID,
                "version": [1, 0, 0],
            }
        ],
    }
    (utility_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    sorted_items = sorted(item_identifiers)
    chest_capacity = 27
    chest_columns = 5
    chest_spacing = 2
    chest_forward_offset = 3
    function_lines = []
    for item_index, identifier in enumerate(sorted_items):
        chest_index = item_index // chest_capacity
        slot_index = item_index % chest_capacity
        column = chest_index % chest_columns
        row = chest_index // chest_columns
        local_x = column * chest_spacing
        local_z = chest_forward_offset + row * chest_spacing
        if slot_index == 0:
            function_lines.append(f"setblock ^{local_x} ^ ^{local_z} chest replace")
        function_lines.append(
            f"replaceitem block ^{local_x} ^ ^{local_z} slot.container {slot_index} {identifier} 1"
        )
    make_chests = "\n".join(function_lines)
    if make_chests:
        make_chests += "\n"
    (utility_root / "functions" / "cinter" / "makechests.mcfunction").write_text(make_chests, encoding="utf-8")

    utility_reference = {"pack_id": _CINTER_UTILITY_PACK_ID, "version": [1, 0, 0]}
    by_id = {
        str(reference.get("pack_id") or ""): reference
        for reference in behavior_references
        if isinstance(reference, dict) and reference.get("pack_id")
    }
    by_id[_CINTER_UTILITY_PACK_ID] = utility_reference
    behavior_references_path.write_text(json.dumps(list(by_id.values()), indent=2) + "\n", encoding="utf-8")
    print(
        f"CINTER_CRAFTMASTER_UTILITY_READY world={world_name} items={len(item_identifiers)} command=/function cinter/makechests",
        flush=True,
    )
    return {
        "world": world_name,
        "pack": str(utility_root),
        "items": sorted(item_identifiers),
        "command": "/function cinter/makechests",
    }

def resolve_cfos_launcher(server: Path, override: Path | None = None) -> Path:
    """Resolve the moved CinterForgeTerminal LinuxKit CLI, not sibling CFOS source.

    Keep the old function signature for CraftMaster callers. Installed products
    supply CINTER_TERMINAL_LINUXKIT_CLI as an explicit staged runtime dependency.
    """
    explicit = override or os.environ.get('CINTER_TERMINAL_LINUXKIT_CLI', '').strip()
    if explicit:
        launcher = Path(explicit).expanduser().resolve()
    else:
        repo = next((p for p in Path(__file__).resolve().parents
                     if p.name == 'CinterForgeTerminal' and (p / 'Cinter').is_file()), None)
        if repo is None:
            raise RuntimeError('CinterForgeTerminal runtime CLI is not packaged; '
                               'set CINTER_TERMINAL_LINUXKIT_CLI to the installed launcher')
        launcher = repo / 'Cinter'
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError(f'CinterForgeTerminal LinuxKit launcher missing or not executable: {launcher}')
    return launcher


def launch_server(server: Path, cfos: Path | None = None) -> None:
    """Launch Bedrock through CinterForgeTerminal on macOS, or natively on Linux/Windows."""
    server = server.expanduser().resolve()
    host = platform.system()
    if host == 'Darwin':
        launcher = resolve_cfos_launcher(server, cfos)
        print(f'CINTER_CRAFTMASTER_SERVER_LAUNCH platform=macos terminal={launcher} server={server}', flush=True)
        os.execv(str(launcher), [str(launcher), 'launch', 'cinterforge-os', 'linuxkit',
                                  'Release', '--server', str(server)])
        return
    if cfos is not None:
        raise RuntimeError('--cfos is only supported for macOS Bedrock guests')
    if host == 'Windows':
        executable = server / 'bedrock_server.exe'
    elif host == 'Linux':
        executable = server / 'bedrock_server'
    else:
        raise RuntimeError(f'Unsupported CraftMaster Bedrock host: {host!r}')
    if not executable.is_file():
        raise RuntimeError(f'Native Bedrock executable not found: {executable}')
    if host == 'Linux' and not os.access(executable, os.X_OK):
        raise RuntimeError(f'Native Bedrock executable is not executable: {executable}')
    print(f'CINTER_CRAFTMASTER_SERVER_LAUNCH platform={host.lower()} server={server}', flush=True)
    os.chdir(server)
    os.execv(str(executable), [str(executable)])

def main() -> None:
    ap=argparse.ArgumentParser(description='Import a converted .mcworld and launch its CraftMaster BDS server')
    ap.add_argument('mcworld',type=Path); ap.add_argument('--server',required=True,type=Path)
    ap.add_argument('--cfos',type=Path,help='Explicit installed CFOS public entry point (macOS only)')
    ap.add_argument('--prepare-only',action='store_true',help='import/select the world without launching BDS')
    args=ap.parse_args()
    prepare_world(args.mcworld,args.server)
    if not args.prepare_only:
        launch_server(args.server,args.cfos)


if __name__=='__main__': main()
