"""Encode Bedrock item-frame structures using the official v1 LE-NBT format.

No native ABI offsets or writes to a running world's LevelDB. The behavior
pack must prove that the running server can resolve a newly written structure
before any physical screen is allocated. Slots are indexed before BDS starts.
"""
import hashlib
import json
import os
import re
import struct
from pathlib import Path

NAMESPACE = 'cinterforge_terminal'
SLOTS = 128


def _s(value):
    raw = value.encode('utf-8')
    return struct.pack('<H', len(raw)) + raw


def _tag(kind, name, payload):
    return bytes([kind]) + _s(name) + payload


def _int(value): return struct.pack('<i', value)
def _long(value): return struct.pack('<q', value)
def _short(value): return struct.pack('<h', value)
def _byte(value): return struct.pack('<b', value)
def _float(value): return struct.pack('<f', value)


def _compound(*tags): return b''.join(tags) + b'\0'
def _list(kind, entries): return bytes([kind]) + _int(len(entries)) + b''.join(entries)
def _ints(values): return _list(3, [_int(n) for n in values])


def _structure(size, palette, indices, actors):
    n = size[0]*size[1]*size[2]
    default = _compound(_tag(9, 'block_palette', _list(10, palette)),
                        _tag(10, 'block_position_data', _compound(*actors)))
    return _tag(10, '', _compound(
        _tag(3, 'format_version', _int(1)), _tag(9, 'size', _ints(size)),
        _tag(10, 'structure', _compound(
            _tag(9, 'block_indices', _list(9, [_ints(indices), _ints([-1]*n)])),
            _tag(9, 'entities', _list(10, [])),
            _tag(10, 'palette', _compound(_tag(10, 'default', default))))),
        _tag(9, 'structure_world_origin', _ints([0,0,0]))))


def blank(size=(1,1,1)):
    air = _compound(_tag(8, 'name', _s('minecraft:air')), _tag(10, 'states', _compound()))
    return _structure(size, [air], [0]*(size[0]*size[1]*size[2]), [])


def panel_geometry(q):
    w,h,dx,dz = (q[k] for k in ('width','height','dx','dz'))
    if any(type(v) is not int for v in (w,h,dx,dz)) or not (1<=w<=7 and 1<=h<=4) or abs(dx)+abs(dz)!=1:
        raise ValueError('Invalid screen geometry')
    origin = tuple(q['origin'][k] for k in ('x','y','z'))
    if any(type(v) is not int or abs(v)>30000000 for v in origin):
        raise ValueError('Invalid screen anchor')
    # Origin is the front bottom-left frame; provider labels start top-left.
    cells = [(origin[0]+col*dz, origin[1]+h-1-row, origin[2]-col*dx)
             for row in range(h) for col in range(w)]
    low = tuple(min(p[i] for p in cells) for i in range(3))
    size = tuple(max(p[i] for p in cells)-low[i]+1 for i in range(3))
    facing = {(1,0):5,(-1,0):4,(0,1):3,(0,-1):2}[dx,dz]
    return cells,low,size,facing


def tile_label(screen,index,w,h):
    row,col=divmod(index,w)
    return f'MediaPlayer {screen} - tile {index+1}/{w*h} (row {row+1}, col {col+1})'


def filled_panel(q, ids, *, named=False):
    cells,low,size,facing=panel_geometry(q)
    present=[i for i in ids if i is not None]
    if len(ids)!=len(cells) or not present or len(set(present))!=len(present) or any(type(i) is not int for i in present):
        raise ValueError('Expected distinct native map IDs for every tile')
    frame = _compound(_tag(8, 'name', _s('minecraft:frame')),
                      _tag(10, 'states', _compound(
                          _tag(3, 'facing_direction', _int(facing)),
                          _tag(1, 'item_frame_map_bit', _byte(1)))))
    actors=[]; indices=[-1]*len(cells)
    for n,(p,map_id) in enumerate(zip(cells,ids)):
        x,y,z=(p[i]-low[i] for i in range(3))
        index=(x*size[1]+y)*size[2]+z
        if map_id is None: continue
        indices[index]=0
        tags=[_tag(4,'map_uuid',_long(map_id)),_tag(1,'map_display_players',_byte(0))]
        # Labels are needed only to identify the temporary inventory delivery.
        # On the wall a custom name becomes a floating 3D hover label.
        if named:
            tags.append(_tag(10,'display',_compound(_tag(8,'Name',_s(tile_label(q['screen'],n,q['width'],q['height']))))))
        item = _compound(_tag(8,'Name',_s('minecraft:filled_map')),
                         _tag(1,'Count',_byte(1)), _tag(2,'Damage',_short(0)),
                         _tag(10,'tag',_compound(*tags)))
        actor = _compound(_tag(8,'id',_s('ItemFrame')), _tag(10,'Item',item),
                          _tag(5,'ItemRotation',_float(0)), _tag(5,'ItemDropChance',_float(1)))
        actors.append(_tag(10,str(index),_compound(_tag(10,'block_entity_data',actor))))
    return _structure(size,[frame],indices,actors)


def structure_root(root):
    return Path(root)/'behavior_packs/cinterforge-status-bridge/structures'/NAMESPACE


def write_slot(root,name,data):
    if name != 'probe' and name not in {f'panel{i:03}' for i in range(SLOTS)}:
        raise ValueError('Invalid reserved structure slot')
    folder=structure_root(root)
    if not folder.is_dir() or folder.is_symlink(): raise RuntimeError('Terminal structure directory unavailable')
    target=folder/(name+'.mcstructure')
    tmp=folder/(name+'.tmp')
    if target.is_symlink() or tmp.is_symlink(): raise RuntimeError('Linked Terminal structure slot')
    tmp.write_bytes(data)
    os.replace(tmp,target)
    return NAMESPACE+':'+name


# The old encoder included the exact screen/tile name adjacent to the map ID.
# Archive these authored slots before resetting them. Never guess a map ID
# or rewrite a screen unless its archived bytes match our old encoder exactly.
_NAMED_TILE = re.compile(re.escape(bytes([4])+_s('map_uuid'))+rb'(.{8})'+
    re.escape(_tag(1,'map_display_players',_byte(0))+bytes([10])+_s('display')+bytes([8])+_s('Name'))+
    rb'..MediaPlayer (cinter_[0-9a-f]{32}) - tile ([0-9]+)/([0-9]+) \(row ([0-9]+), col ([0-9]+)\)',re.DOTALL)


def archive_named_slots(root):
    folder=structure_root(root)
    if not folder.is_dir(): return
    archive=Path(root)/'.cinter/display-mount-history'
    for source in folder.glob('panel[0-9][0-9][0-9].mcstructure'):
        if source.is_symlink() or source.stat().st_size>65536: continue
        data=source.read_bytes(); screens={m[2].decode('ascii') for m in _NAMED_TILE.finditer(data)}
        if len(screens)!=1: continue
        target=archive/next(iter(screens)); target.mkdir(parents=True,exist_ok=True)
        dest=target/(hashlib.sha256(data).hexdigest()+'.mcstructure')
        if not dest.exists():
            tmp=dest.with_suffix('.tmp'); tmp.write_bytes(data); os.replace(tmp,dest)


def legacy_map_ids(root,q):
    w,h=q['width'],q['height']; name=q['screen']; result={}
    if not re.fullmatch(r'cinter_[0-9a-f]{32}',name): raise ValueError('Invalid screen ID')
    archive=Path(root)/'.cinter/display-mount-history'/name
    for p in archive.glob('*.mcstructure'):
        if p.is_symlink() or p.stat().st_size>65536: continue
        data=p.read_bytes(); ids=[None]*(w*h)
        for m in _NAMED_TILE.finditer(data):
            idx,total,row,col=map(int,m.groups()[2:]); i=idx-1
            if m[2].decode('ascii')!=name or total!=w*h or not 0<=i<w*h or (row-1,col-1)!=divmod(i,w) or ids[i] is not None:
                raise ValueError('Archived map metadata does not match registered screen')
            ids[i]=struct.unpack('<q',m[1])[0]
        if not any(v is not None for v in ids) or filled_panel(q,ids,named=True)!=data:
            continue
        for i,v in enumerate(ids):
            if v is None:continue
            if i in result and result[i]!=v:raise ValueError('Conflicting archived map IDs')
            result[i]=v
    if len(result)!=w*h or len(set(result.values()))!=w*h:
        raise ValueError('No complete verified legacy map identity; existing screen left intact')
    return [result[i] for i in range(w*h)]


def _identity(root,q):
    name=q['screen']
    if not re.fullmatch(r'cinter_[0-9a-f]{32}',name): raise ValueError('Invalid screen ID')
    panel_geometry(q)
    geometry={k:q[k] for k in ('screen','width','height','origin','dx','dz')}
    return Path(root)/'.cinter/display-mount-history'/name/'identity.json', geometry


def remember_map_ids(root,q,ids):
    path,geometry=_identity(root,q)
    # The provider delivered these IDs, never infer them from position alone.
    filled_panel(q,ids)
    if path.is_symlink():raise ValueError('Linked Terminal map identity')
    if path.is_file():
        data=json.loads(path.read_text('utf-8'))
        if data['geometry']!=geometry:raise ValueError('Stored map geometry mismatch')
        previous=data['ids']
        if len(previous)!=len(ids):raise ValueError('Stored map dimensions mismatch')
        for i,v in enumerate(previous):
            if v is not None and ids[i] is not None and v!=ids[i]:raise ValueError('Conflicting Terminal map IDs')
        ids=[v if v is not None else previous[i] for i,v in enumerate(ids)]
        filled_panel(q,ids)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'geometry':geometry,'ids':ids}),encoding='utf-8')
    os.replace(tmp,path)


def repair_map_ids(root,q):
    path,geometry=_identity(root,q)
    if path.is_symlink():raise ValueError('Linked Terminal map identity')
    if path.is_file():
        data=json.loads(path.read_text('utf-8'))
        if data['geometry']!=geometry:raise ValueError('Stored map geometry mismatch')
        ids=data['ids']
        if len(ids)==q['width']*q['height'] and all(type(v) is int for v in ids):
            filled_panel(q,ids)
            return ids
    # First migration of already-mounted screens, before the old slots reset.
    ids=legacy_map_ids(root,q)
    remember_map_ids(root,q,ids)
    return ids


def seed_slots(root):
    folder=structure_root(root)
    if any(p.is_symlink() for p in (folder,folder.parent,folder.parent.parent)):
        raise RuntimeError('Linked Terminal structure directory')
    if not (folder.parent.parent/'manifest.json').is_file(): return
    folder.mkdir(parents=True,exist_ok=True)
    archive_named_slots(root)
    data=blank()
    for name in ['probe',*(f'panel{i:03}' for i in range(SLOTS))]:
        write_slot(root,name,data)
