"""Bedrock display viewport over CraftMaster's existing live ZFY pyramid.

Only visible, committed tiles cross the existing server-owned display IO share.
No new web service, full-world bitmap, or desktop tab is created.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import shutil
import struct
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

_MAX_MAP_LAYOUT_ENTRIES = 4096

_ZOOMIFY_TILE = re.compile(r'(\d+)-(\d+)-(\d+)\.(?:jpg|jpeg|png)', re.I)
_TILE_GROUP = re.compile(r'TileGroup\d+')



def _map_directories(state: dict) -> list[Path]:
    """Stay under the selected world's Maps folder and select Overworld only."""
    expected = state['directory']
    world_root = expected.parent
    if world_root.is_symlink() or not world_root.is_dir():
        return []
    directories = [expected] if expected.is_dir() and not expected.is_symlink() else []
    try:
        for candidate in world_root.iterdir():
            if (candidate != expected and candidate.is_dir() and not candidate.is_symlink()
                    and candidate.name.casefold() in ('overworld', 'minecraft_overworld', '0')):
                directories.append(candidate)
        # Some CraftMaster ZFY layouts put the Overworld tiles directly under
        # Maps/<world>, rather than making a second dimension directory.
        directories.append(world_root)
    except OSError:
        pass
    return directories


def _find_zoomify_root(dimension: Path) -> Path | None:
    """Locate a produced TileGroup, preferring roots with committed tile files."""
    frontier = [dimension]
    empty_root = None
    for _ in range(4):
        upcoming = []
        for parent in frontier:
            try:
                children = list(parent.iterdir())[:_MAX_MAP_LAYOUT_ENTRIES]
            except OSError:
                continue
            for child in children:
                if child.is_symlink() or not child.is_dir():
                    continue
                if _TILE_GROUP.fullmatch(child.name):
                    if empty_root is None:
                        empty_root = parent
                    try:
                        if any(_ZOOMIFY_TILE.fullmatch(tile.name) and tile.is_file()
                               and not tile.is_symlink() and tile.stat().st_size >= 32
                               for tile in child.iterdir()):
                            return parent
                    except OSError:
                        pass
                    continue
                if (not child.name.startswith('.') and
                        (parent != dimension or child.name.casefold() not in
                         ('nether', 'the_end', 'theend', 'end', 'minecraft_nether', 'minecraft_the_end', 'dim-1', 'dim1'))):
                    upcoming.append(child)
        frontier = upcoming[:_MAX_MAP_LAYOUT_ENTRIES]
    return empty_root


def _map_layout(state: dict) -> tuple[Path, Path | None]:
    candidates = _map_directories(state)
    for dimension in candidates:
        zoomify = _find_zoomify_root(dimension)
        if zoomify is not None:
            return dimension, zoomify
    return (candidates[0], None) if candidates else (state['directory'], None)

DIMENSIONS = ('Overworld', 'Nether', 'End')
TILE = 512
MAX_TILE_BYTES = 8 * 1024 * 1024


def read_spawn(path):
    """Read only the Java level.dat spawn fields (modern and legacy NBT)."""
    try:
        with gzip.open(path, 'rb') as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            return None
        pos = 0
        def take(n):
            nonlocal pos
            if n < 0 or pos+n > len(data): raise ValueError('Truncated NBT')
            value=data[pos:pos+n];pos+=n;return value
        def number(fmt): return struct.unpack('>'+fmt,take(struct.calcsize('>'+fmt)))[0]
        def string(): return take(number('H')).decode('utf-8',errors='replace')
        def payload(kind, depth=0, keep=False):
            if depth > 64: raise ValueError('NBT nesting')
            if kind in (1,2,3,4,5,6): return number({1:'b',2:'h',3:'i',4:'q',5:'f',6:'d'}[kind])
            if kind==8:return string()
            if kind in (7,11,12):
                n=number('i'); unit={7:1,11:4,12:8}[kind]
                if keep and kind==11 and n==3:return [number('i') for _ in range(n)]
                take(n*unit);return None
            if kind==9:
                item=number('B'); n=number('i')
                if not 0<=n<=len(data) or (item==0 and n):raise ValueError('Invalid NBT list')
                values=[] if keep and n==3 else None
                for _ in range(n):
                    v=payload(item,depth+1,keep)
                    if values is not None:values.append(v)
                return values
            if kind==10:
                values={}
                while True:
                    item=number('B')
                    if item==0:break
                    key=string(); selected=keep and key in ('Data','spawn','pos','dimension','SpawnX','SpawnZ')
                    v=payload(item,depth+1,selected)
                    if selected:values[key]=v
                return values
            raise ValueError('Invalid NBT tag')
        if number('B')!=10:return None
        string(); root=payload(10,keep=True).get('Data',{})
        spawn=root.get('spawn',{})
        if isinstance(spawn,dict) and spawn.get('dimension') in ('minecraft:overworld','overworld'):
            xyz=spawn.get('pos')
            if isinstance(xyz,list) and len(xyz)==3:return (float(xyz[0]),float(xyz[2]))
        if 'SpawnX' in root and 'SpawnZ' in root:return (float(root['SpawnX']),float(root['SpawnZ']))
    except (OSError,ValueError,TypeError,struct.error,OverflowError):pass
    return None


def geometry(root):
    props=root/'ImageProperties.xml'
    if props.is_symlink() or props.stat().st_size>65536:raise ValueError('Invalid ZFY geometry')
    a=ET.fromstring(props.read_bytes()).attrib
    w,h,t,n=(int(a[k]) for k in ('WIDTH','HEIGHT','TILESIZE','NUMTILES'))
    if not 0<w<=2**31 or not 0<h<=2**31 or t!=TILE:raise ValueError('Invalid ZFY dimensions')
    return pyramid_geometry(w,h,n)


def pyramid_geometry(w,h,n):
    if not 0<w<=2**31 or not 0<h<=2**31:raise ValueError('Invalid map dimensions')
    levels=[];x,y=w,h
    while True:
        levels.append((x,y,math.ceil(x/TILE),math.ceil(y/TILE)))
        if x<=TILE and y<=TILE:break
        x,y=(x+1)//2,(y+1)//2
    levels.reverse();first=0; result=[]
    for x,y,c,r in levels:
        result.append((x,y,c,r,first));first+=c*r
    if first!=n:raise ValueError('ZFY tile count mismatch')
    return w,h,result


def packed_geometry(path):
    """Same 1064-byte PFF header and finest-first tile order as browser Atlas."""
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('Linked PFF')
    with path.open('rb') as stream:
        header=stream.read(1064)
        if len(header)!=1064:raise ValueError('Truncated PFF')
        version,t,n,w,h=(struct.unpack_from('>I',header,i)[0] for i in (4,108,124,1052,1056))
        if (version,t)!=(106,TILE):raise ValueError('Invalid PFF header')
        size=path.stat().st_size
        if 1064+n*8>=size:raise ValueError('Invalid PFF index')
        stream.seek(1064+(n-1)*8)
        if struct.unpack('>Q',stream.read(8))[0]!=size:raise ValueError('Invalid PFF end offset')
    return pyramid_geometry(w,h,n)


def packed_tile(path,count,index):
    with path.open('rb') as stream:
        start=1064+count*8
        if index:stream.seek(1064+(index-1)*8);start=struct.unpack('>Q',stream.read(8))[0]
        stream.seek(1064+index*8);end=struct.unpack('>Q',stream.read(8))[0]
        if not 1064+count*8<=start<end<=path.stat().st_size or end-start>MAX_TILE_BYTES:raise ValueError('Invalid PFF tile offset')
        stream.seek(start);data=stream.read(end-start)
        if len(data)!=end-start or not data.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid PFF PNG tile')
        return data


def controls(w,h):
    # Two rows on the 1x1 display, a single row on all larger widths.
    actions=('zoom+','zoom-','left','right','up','down','home','dimension','exit')
    cols=5 if w<256 else len(actions)
    rows=math.ceil(len(actions)/cols); bar=rows*22
    return h-bar, [(a,i%cols*w//cols,h-bar+i//cols*22,(i%cols+1)*w//cols,h-bar+(i//cols+1)*22) for i,a in enumerate(actions)]


class DisplayMaps:
    def __init__(self, manager, io, *, town_names=None, dialog_controls=False):
        self.manager=manager; self.io=io; self.active={}; self.jobs={}; self.lock=threading.Lock()
        self.town_names=town_names; self.dialog_controls=dialog_controls

    def start(self,screen,name,w,h, *, generate_missing=True):
        world=self.manager.find_java_world(name)
        if world is None:raise ValueError('No local Java world named '+repr(name))
        root=self.manager.craftmaster_output_root(world)/'Maps'/Path(world['path']).name
        self.active[screen]={'world':world,'root':root,'name':world['name'],'dimension':0,'size':(w,h),'view':None,'spawn':read_spawn(Path(world['path'])/'level.dat')}
        # Reuse existing authored maps. Generate only when absent, without importing
        # or restarting the currently served Bedrock world.
        dim, existing=self.layout(self.active[screen])
        packed=dim/(Path(world['path']).name+' Zoomify.pff')
        if generate_missing and not packed.is_file() and (existing is None or not (existing/'ImageProperties.xml').is_file()):
            key=world['id']
            with self.lock:
                if key not in self.jobs or self.jobs[key]['state']=='failed':
                    self.jobs[key]={'state':'working','message':'Preparing CraftMaster map…'}
                    def convert():
                        try:
                            self.manager.run_native_craftmaster(world)
                            with self.lock:self.jobs[key]={'state':'ready','message':'Map ready'}
                        except Exception as e:
                            self.manager._append_log('CINTERFORGE_DISPLAY_MAP_FAILED '+str(e))
                            with self.lock:self.jobs[key]={'state':'failed','message':'Map generation failed; see server log'}
                    threading.Thread(target=convert,name='bedrock-zoomify-'+key[:8],daemon=True).start()

    def layout(self,state):
        root=state['root']; dim=root/DIMENSIONS[state['dimension']]
        if root.is_symlink() or dim.is_symlink():return dim,None
        if state['dimension']==0:return _map_layout({'directory':dim})
        return dim,_find_zoomify_root(dim) if dim.is_dir() else None

    def turn(self,screen,request,action='refresh',point=None):
        state=self.active[screen];w,h=state['size']; vh,buttons=(h,[]) if self.dialog_controls else controls(w,h)
        if action=='click':
            x,y=point
            action=next((a for a,x0,y0,x1,y1 in buttons if x0<=x<x1 and y0<=y<y1),'center') if y>=vh else 'center'
        if action=='exit':
            self.active.pop(screen,None);return {'output':'CraftMaster map closed.','mode':'terminal'}
        if action=='dimension':
            state['dimension']=(state['dimension']+1)%3;state['view']=None
        elif action in ('overworld','nether','end'):
            dimension=('overworld','nether','end').index(action)
            if dimension!=state['dimension']:state['dimension']=dimension;state['view']=None
        dim,root=self.layout(state)
        result={'mode':'map','map':{'title':state['name'],'dimension':DIMENSIONS[state['dimension']],'width':w,'height':h,'view_height':vh,'buttons':buttons,'tiles':[],'status':''}}
        frame=result['map']
        packed=dim/(Path(state['world']['path']).name+' Zoomify.pff')
        use_pff=False
        try:
            if packed.is_file():
                mw,mh,levels=packed_geometry(packed);use_pff=True
            else:
                if root is None:raise FileNotFoundError('No tiles yet')
                mw,mh,levels=geometry(root)
        except (OSError,ValueError,KeyError,ET.ParseError,struct.error):
            with self.lock:job=self.jobs.get(state['world']['id'],{})
            frame['status']=job.get('message','No map for this dimension yet')
            return result
        fit=min(w/mw,vh/mh,1)
        if state['view'] is None or state['view'][3:]!=(mw,mh):state['view']=(mw/2,mh/2,fit,mw,mh)
        cx,cy,scale,_,_=state['view']
        if action=='zoom+':scale=min(8,scale*2)
        elif action=='zoom-':scale=max(fit,scale/2)
        elif action=='left':cx-=w/scale*.35
        elif action=='right':cx+=w/scale*.35
        elif action=='up':cy-=vh/scale*.35
        elif action=='down':cy+=vh/scale*.35
        elif action=='center' and point is not None and point[1]<vh:
            cx+=(point[0]-w/2)/scale;cy+=(point[1]-vh/2)/scale
        elif action=='fit':cx,cy,scale=mw/2,mh/2,fit
        elif action=='home':
            cx,cy,scale=mw/2,mh/2,fit
            if state['dimension']==0 and state['spawn'] is not None:
                try:
                    origin=json.loads((dim/'LiveMap POIs.json').read_text('utf-8'))
                    sx,sz=state['spawn']; sx-=origin['originX'];sz-=origin['originZ']
                    if 0<=sx<mw and 0<=sz<mh:cx,cy,scale=sx,sz,max(fit, min(1, min(w,vh)/512))
                except (OSError,ValueError,KeyError,TypeError):pass
        cx=max(0,min(mw,cx));cy=max(0,min(mh,cy));state['view']=(cx,cy,scale,mw,mh)
        left=w/2-cx*scale;top=vh/2-cy*scale
        index=max(0,len(levels)-1-max(0,round(-math.log2(scale))))
        lw,lh,cols,rows,first=levels[index]; factor=2**(len(levels)-1-index); step=TILE*factor*scale
        x0=max(0,math.floor(-left/step));x1=min(cols-1,math.floor((w-left)/step))
        y0=max(0,math.floor(-top/step));y1=min(rows-1,math.floor((vh-top)/step))
        if (x1-x0+1)*(y1-y0+1)>64:raise RuntimeError('Viewport exceeds tile budget')
        bundle=self.io/(request+'.tiles');bundle.mkdir(exist_ok=False)
        for y in range(y0,y1+1):
            for x in range(x0,x1+1):
                filename=f'{index}-{x}-{y}.png'
                try:
                    if use_pff:
                        count=sum(c*r for _,_,c,r,_ in levels)
                        packed_index=count-first-cols*rows+y*cols+x
                        (bundle/filename).write_bytes(packed_tile(packed,count,packed_index))
                    else:
                        group=root/f'TileGroup{(first+y*cols+x)//256}';src=group/filename
                        if group.is_symlink() or src.is_symlink() or not 0<src.stat().st_size<=MAX_TILE_BYTES:continue
                        shutil.copyfile(src,bundle/filename)
                except (OSError,ValueError,struct.error):continue
                x0p=round(left+x*step);y0p=round(top+y*step)
                x1p=round(left+(x*TILE+min(TILE,lw-x*TILE))*factor*scale)
                y1p=round(top+(y*TILE+min(TILE,lh-y*TILE))*factor*scale)
                if x1p>x0p and y1p>y0p:frame['tiles'].append([filename,x0p,y0p,x1p,y1p])
        frame['overlays']=map_overlays(state,dim,left,top,scale,w,vh,self.town_names)
        frame['status']=f'{DIMENSIONS[state["dimension"]]}  {scale:.2g}x'
        return result


# Same keys, palette, coordinate transform and generated-name algorithm as
# CraftMaster's authored browser Atlas. No second annotation store.
PALETTE = ('#f97316','#38bdf8','#a3e635','#f472b6','#a78bfa','#facc15','#2dd4bf','#fb7185')


def _map_json(path, limit=32*1024*1024):
    try:
        if any(p.is_symlink() for p in (path,*path.parents)) or path.stat().st_size>limit:return {}
        data=json.loads(path.read_text('utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError,TypeError):return {}


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value) and abs(value)<=30000001


def _town_name(v,signs,key,names):
    candidates=[]
    for sign in signs:
        if not isinstance(sign,dict) or not all(_finite(sign.get(k)) for k in ('x','z')):continue
        if not v['minX']<=sign['x']<=v['maxX'] or not v['minZ']<=sign['z']<=v['maxZ']:continue
        text=sign.get('text')
        if not isinstance(text,str):continue
        m=re.match(r'^(?:welcome\s+to|town\s+of|village\s+of)(?:\s+|\s*[:\-–—]\s*)(.+)$',text.strip(),re.I|re.S)
        if not m:continue
        name=m[1].strip().rstrip('.!?').strip()
        if not name or len(name)>60 or len(name.split())>5 or not name[0].isalpha() or not all(c.isalnum() or c in " '’ -" or __import__('unicodedata').category(c).startswith('M') for c in name):continue
        candidates.append(((sign['x']-v['bellX'])**2+(sign['z']-v['bellZ'])**2,sign['x'],sign['z'],name))
    if candidates:return min(candidates)[-1]
    name=v.get('name')
    if isinstance(name,str) and name.strip() and not re.match(r'^Village[_ ]',name,re.I):return name.strip()
    prefixes,suffixes=names.get('prefixes'),names.get('suffixes')
    if isinstance(prefixes,list) and prefixes and isinstance(suffixes,list) and suffixes and all(isinstance(t,str) for t in prefixes+suffixes):
        h=2166136261
        for c in key:h=((h^ord(c))*16777619)&0xffffffff
        return prefixes[h%len(prefixes)]+suffixes[(h//len(prefixes))%len(suffixes)]
    return name if isinstance(name,str) and name else 'Village'


def map_overlays(state,dim,left,top,scale,w,vh,town_names):
    origin=_map_json(dim/'LiveMap POIs.json')
    if not all(_finite(origin.get(k)) for k in ('originX','originZ')):return []
    root=state['root']; folder=Path(state['world']['path']).name
    record=_map_json(dim/(folder+' POIs.json'))
    edits=_map_json(root/'MapAnnotations.json',1024*1024)
    names=_map_json(town_names or dim/'TownNames.json')
    dimension=DIMENSIONS[state['dimension']].lower()
    def point(x,z):return [round(left+(x-origin['originX'])*scale),round(top+(z-origin['originZ'])*scale)]
    def edit(key):return edits.get(key) if isinstance(edits.get(key),dict) else {}
    def color(key,i):
        c=edit(key).get('color','')
        return c if isinstance(c,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',c) else PALETTE[i%len(PALETTE)]
    def label(value):return value[:100] if isinstance(value,str) else ''
    out=[]; seen=set()
    fields=('minX','minZ','maxX','maxZ')
    for i,v in enumerate(record.get('villages',[])):
        if not isinstance(v,dict) or not all(_finite(v.get(k)) for k in (*fields,'bellX','bellZ')):continue
        key=f"village:{dimension}:{v['bellX']}:{v['bellZ']}"
        if key in seen or edit(key).get('hidden') is True:continue
        seen.add(key); v=dict(v); bounds=edit(key).get('bounds')
        if isinstance(bounds,dict) and all(type(bounds.get(k)) is int and abs(bounds[k])<=30000000 for k in fields) and bounds['minX']<=bounds['maxX'] and bounds['minZ']<=bounds['maxZ']:v.update({k:bounds[k] for k in fields})
        a,b=point(v['minX'],v['minZ']); c,d=point(v['maxX']+1,v['maxZ']+1)
        if c<0 or d<0 or a>=w or b>=vh:continue
        name=label(edit(key).get('name')).strip() or _town_name(v,record.get('signs',[]),folder+':'+key,names)
        out.append({'kind':'village','box':[a,b,max(a+2,c),max(b+2,d)],'color':color(key,i),'name':label(name)})
    players=_map_json(root/'MapPlayers.json').get('players',[])
    order=edit('player-order').get('order',[]); ranks={key:i for i,key in enumerate(order) if isinstance(key,str)} if isinstance(order,list) else {}
    ordered=[]
    for i,p in enumerate(players):
        if not isinstance(p,dict) or not isinstance(p.get('uuid'),str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',p['uuid']):continue
        key='player:'+p['uuid']; seed=edit(key).get('number',p.get('number',i+1)); seed=seed if type(seed) is int and seed>0 else i+1
        ordered.append((ranks.get(key,100000),seed,i,p,key))
    for number,(_,seed,i,p,key) in enumerate(sorted(ordered,key=lambda q:q[:3]),1):
        if p.get('hasPosition') and p.get('dimension')==dimension and all(_finite(p.get(k)) for k in ('posX','posZ')):
            x,y=point(p['posX'],p['posZ'])
            if 0<=x<w and 0<=y<vh:
                profile=_map_json(root/'Profiles'/(p['uuid']+'.json'))
                name=label(edit(key).get('name')) or label(profile.get('name')) or label(p.get('playerName')) or str(number)
                out.append({'kind':'player','point':[x,y],'color':color(key,seed-1),'number':number,'name':name})
    if dimension=='overworld' and state.get('spawn') is not None:
        x,y=point(*state['spawn'])
        if 0<=x<w and 0<=y<vh:out.append({'kind':'spawn','point':[x,y],'color':'#ffffff','name':'World spawn'})
    return out[:4096]
