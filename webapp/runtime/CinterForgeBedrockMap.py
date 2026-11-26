"""Bedrock display viewport over CraftMaster's existing live ZFY pyramid.

Only visible, committed tiles cross the existing server-owned display IO share.
No new web service, full-world bitmap, or desktop tab is created.
"""
from __future__ import annotations

import gzip
import json
import math
import shutil
import struct
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

from runtime.CinterForgeCraftMasterMap import _map_layout, _find_zoomify_root

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


def controls(w,h):
    # Two rows on the 1x1 display, a single row on all larger widths.
    actions=('zoom+','zoom-','left','right','up','down','home','dimension','exit')
    cols=5 if w<256 else len(actions)
    rows=math.ceil(len(actions)/cols); bar=rows*22
    return h-bar, [(a,i%cols*w//cols,h-bar+i//cols*22,(i%cols+1)*w//cols,h-bar+(i//cols+1)*22) for i,a in enumerate(actions)]


class DisplayMaps:
    def __init__(self, manager, io):
        self.manager=manager; self.io=io; self.active={}; self.jobs={}; self.lock=threading.Lock()

    def start(self,screen,name,w,h):
        world=self.manager.find_java_world(name)
        if world is None:raise ValueError('No local Java world named '+repr(name))
        root=self.manager.craftmaster_output_root(world)/'Maps'/Path(world['path']).name
        self.active[screen]={'world':world,'root':root,'name':world['name'],'dimension':0,'size':(w,h),'view':None,'spawn':read_spawn(Path(world['path'])/'level.dat')}
        # Reuse existing authored maps. Generate only when absent, without importing
        # or restarting the currently served Bedrock world.
        _, existing=self.layout(self.active[screen])
        if existing is None or not (existing/'ImageProperties.xml').is_file():
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
        state=self.active[screen];w,h=state['size']; vh,buttons=controls(w,h)
        if action=='click':
            x,y=point
            action=next((a for a,x0,y0,x1,y1 in buttons if x0<=x<x1 and y0<=y<y1),'center') if y>=vh else 'center'
        if action=='exit':
            self.active.pop(screen,None);return {'output':'CraftMaster map closed.','mode':'terminal'}
        if action=='dimension':
            state['dimension']=(state['dimension']+1)%3;state['view']=None
        dim,root=self.layout(state)
        result={'mode':'map','map':{'title':state['name'],'dimension':DIMENSIONS[state['dimension']],'width':w,'height':h,'view_height':vh,'buttons':buttons,'tiles':[],'status':''}}
        frame=result['map']
        try:
            if root is None:raise FileNotFoundError('No tiles yet')
            mw,mh,levels=geometry(root)
        except (OSError,ValueError,KeyError,ET.ParseError):
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
                group=root/f'TileGroup{(first+y*cols+x)//256}'
                src=group/f'{index}-{x}-{y}.png'
                try:
                    if group.is_symlink() or src.is_symlink() or not 0<src.stat().st_size<=MAX_TILE_BYTES:continue
                    filename=f'{index}-{x}-{y}.png';shutil.copyfile(src,bundle/filename)
                except OSError:continue
                x0p=round(left+x*step);y0p=round(top+y*step)
                x1p=round(left+(x*TILE+min(TILE,lw-x*TILE))*factor*scale)
                y1p=round(top+(y*TILE+min(TILE,lh-y*TILE))*factor*scale)
                if x1p>x0p and y1p>y0p:frame['tiles'].append([filename,x0p,y0p,x1p,y1p])
        frame['status']=f'{DIMENSIONS[state["dimension"]]}  {scale:.2g}x'
        return result
