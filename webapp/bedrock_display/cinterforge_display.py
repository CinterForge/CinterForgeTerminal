"""Endstone consumer of the prebuilt MediaPlayer SDK. Main-thread SDK calls only."""
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from endstone import Player
from endstone.event import event_handler, ScriptMessageEvent, PlayerQuitEvent
from endstone.form import ModalForm, TextInput
from endstone.plugin import Plugin
from endstone_mediaplayer import mediaplayer
from PIL import Image, ImageDraw, ImageFont
from terminal_mount import SLOTS, blank, filled_panel, panel_geometry, tile_label, write_slot, remember_map_ids, repair_map_ids

GREETING = 'Welcome to CinterForge. SHALL WE PLAY A GAME?'
PROMPT = GREETING+'\nClick screen to enter a command.\n> '
FORM_TITLE = 'CinterForge Terminal — Command'
FOCUS_SECONDS = 120
NAME = re.compile(r'cinter_[0-9a-f]{32}\Z')


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
    os.replace(tmp,path)


def wrap_pixels(line,font,width):
    """Keep explicit line breaks and measure real glyph advances, not M columns."""
    line=line.expandtabs(4)
    if not line: return ['']
    result=[]
    while font.getlength(line)>width:
        lo,hi=1,len(line)
        while lo<hi:
            mid=(lo+hi+1)//2
            if font.getlength(line[:mid])<=width:lo=mid
            else:hi=mid-1
        end=lo
        # Prefer a word boundary, but do not split long names or padded tables
        # much earlier than the actual edge simply because they contain spaces.
        boundary=max(line.rfind(' ',0,end+1),line.rfind('\t',0,end+1))
        if boundary>0 and line[:boundary].strip() and font.getlength(line[:boundary])>=width*.8:end=boundary
        result.append(line[:end].rstrip())
        line=line[end:].lstrip()
    if line or not result:result.append(line)
    return result


def render_surface(w,h,text):
    im=Image.new('RGBA',(w,h),(0,0,0,255))
    # Use a line pitch that divides 128: horizontal tile boundaries are in
    # interline space, never through a normal text row. Same canvas for all tiles.
    size=max(9,min(13,w//35)); pitch=16
    font=ImageFont.load_default(size=size)
    lines=[]
    for line in text.split('\n'):
        lines.extend(wrap_pixels(line,font,max(1,w-12)))
    rows=max(1,h//pitch)
    for i,line in enumerate(lines[-rows:]):
        # Reserve one blank pixel on each side of a horizontal tile edge.
        row=Image.new('RGBA',(w,pitch-2),(0,0,0,255))
        ImageDraw.Draw(row).text((6,0),line,font=font,anchor='lt',fill=(80,255,100,255))
        im.paste(row,(0,i*pitch+1))
    return im


class CinterForgeDisplay(Plugin):
    api_version = '0.11'
    depend = ['mediaplayer']

    def on_enable(self):
        self.mp = mediaplayer()
        self.io = Path.cwd()/'.cinter/display-io'
        self.io.mkdir(parents=True,exist_ok=True)
        self.pending = {}
        self.transcripts = {}
        self.story_modes = set()
        self.map_modes = {}
        self.map_refresh = {}
        self.map_clicks = {}
        self.focus = {}
        self.mounts = {}
        self.mount_ready = False
        self.mount_probe_count = 0
        self.mount_slot = 0
        self.register_events(self)
        self.server.scheduler.run_task(self,self.poll,delay=1,period=5)
        probe = self.mp.create_screen('cinter_probe_'+uuid.uuid4().hex,1,1,backend='logical')
        try:
            self.render(probe, GREETING)
            if probe.stats().generation < 1:
                raise RuntimeError('MediaPlayer did not commit a frame')
        finally:
            probe.delete()
        self.logger.info('CINTERFORGE_DISPLAY_PROVIDER_READY')
        try:
            write_slot(Path.cwd(),'probe',blank((2,1,1)))
            self.mount_probe_token=uuid.uuid4().hex
        except Exception as exc:
            self.mount_probe_token=None
            self.logger.error('CINTERFORGE_TERMINAL_MOUNT_UNAVAILABLE: '+str(exc))

    def on_disable(self):
        if getattr(self,'mp',None) is not None:
            self.mp.close()

    def render(self, screen, text):
        info=screen.info(); w,h=info.pixel_width,info.pixel_height
        im=render_surface(w,h,text)
        # A single complete opaque surface, not a separate text layout per tile.
        screen.update_region(0,0,w,h,im.tobytes(),format='rgba8888')

    def result(self, request, success, reason=''):
        data={'id':request['id'],'ok':success,'screen':request.get('screen',''),'reason':reason[:300]}
        self.server.dispatch_command(self.server.command_sender,'scriptevent cinter:terminal_display_result '+json.dumps(data,separators=(',',':'),ensure_ascii=True))

    @event_handler
    def script(self,event: ScriptMessageEvent):
        if event.message_id not in ('cinter:terminal_place_provider','cinter:terminal_open_provider','cinter:terminal_restore_provider','cinter:terminal_recover_provider','cinter:terminal_mount_ack_provider','cinter:terminal_mount_probe_ack_provider','cinter:terminal_close_provider','cinter:terminal_relabel_provider','cinter:terminal_remove_provider','cinter:terminal_map_click_provider'):
            return
        # Only the server-side behavior pack emits these events. A player can
        # never use a /scriptevent payload as another player's host command.
        if isinstance(event.sender,Player):
            return
        try:
            q=json.loads(event.message)
            if event.message_id=='cinter:terminal_mount_probe_ack_provider':
                if q.get('token')==self.mount_probe_token and q.get('ok') is True and not self.mount_ready:
                    self.mount_ready=True
                    self.logger.info('CINTERFORGE_TERMINAL_MOUNT_READY')
                return
            if event.message_id=='cinter:terminal_mount_ack_provider':
                self.mount_ack(q); return
            if not NAME.fullmatch(q.get('screen','')): raise ValueError('invalid screen ID')
            if event.message_id=='cinter:terminal_restore_provider':
                screen=self.mp.screen(q['screen'])
                self.restore_screen(q['screen'])
                return
            player=self.server.get_player(q['player'])
            if player is None: raise ValueError('player left the world')
            if event.message_id=='cinter:terminal_close_provider':
                if self.focus.pop(str(player.unique_id),None) is not None:
                    player.close_form()
                return
            if event.message_id=='cinter:terminal_map_click_provider':
                if q['screen'] in self.map_modes:
                    self.map_request(q['screen'],'click',q.get('point'),player)
                return
            if event.message_id=='cinter:terminal_open_provider':
                self.open_form(player,q['screen']); return
            if event.message_id=='cinter:terminal_relabel_provider':
                self.relabel(player,q); return
            if event.message_id=='cinter:terminal_remove_provider':
                self.remove(player,q); return
            self.place(player,q,recover=event.message_id=='cinter:terminal_recover_provider')
        except Exception as exc:
            self.logger.error('CINTERFORGE_TERMINAL_PROVIDER_FAILED: '+str(exc))
            if event.message_id in ('cinter:terminal_place_provider','cinter:terminal_recover_provider') and isinstance(locals().get('q'),dict) and 'id' in q:
                self.result(q,False,str(exc))
            elif event.message_id=='cinter:terminal_relabel_provider' and isinstance(locals().get('q'),dict) and 'id' in q:
                self.emit_mount('relabel_result',{'id':q['id'],'screen':q['screen'],'ok':False,'reason':str(exc)[:300]})
            elif event.message_id=='cinter:terminal_remove_provider' and isinstance(locals().get('q'),dict) and 'id' in q:
                self.emit_mount('remove_result',{'id':q['id'],'screen':q['screen'],'ok':False,'reason':str(exc)[:300]})
            elif event.message_id=='cinter:terminal_open_provider' and locals().get('player') is not None:
                self.end_focus(player)

    def emit_mount(self, event, data):
        self.server.dispatch_command(self.server.command_sender, 'scriptevent cinter:terminal_'+event+' '+json.dumps(data,separators=(',',':'),ensure_ascii=True))

    def place(self,player,q,recover=False):
        cells,low,size,facing=panel_geometry(q)
        w,h=q['width'],q['height']
        if q['id'] in self.mounts or any(v['player']==player.name for v in self.mounts.values()):
            raise RuntimeError('Terminal placement already in progress')
        if not self.mount_ready:
            raise RuntimeError('Live Bedrock structure loading is not verified; maps and Terminal retained')
        if self.mount_slot>=SLOTS: raise RuntimeError('Terminal structure slots exhausted; restart server')
        if not player.is_op:
            raise PermissionError('Physical placement requires a server operator; use op <gamertag> in the server console')
        indices=q.get('empty',list(range(w*h))) if recover else list(range(w*h))
        if not indices or len(set(indices))!=len(indices) or any(type(i) is not int or not 0<=i<w*h for i in indices):
            raise ValueError('Invalid empty frame indices')
        inventory=player.inventory
        if not recover:
            if sum(inventory.get_item(i) is None for i in range(inventory.size))<w*h:
                raise RuntimeError(f'MediaPlayer needs {w*h} temporary empty inventory slots')
            screen=self.mp.create_screen(q['screen'],w,h,backend='logical')
            logical=screen.info().backend
            try:
                self.render(screen,PROMPT)
                player.perform_command('mpv materialize '+q['screen'])
                if screen.info().backend==logical:
                    raise RuntimeError('MediaPlayer did not create the managed screen')
            except Exception:
                if screen.info().backend==logical: screen.delete()
                raise
        else:
            screen=self.mp.screen(q['screen'])
        if recover:
            self.render(screen,PROMPT)
        if screen.info().pixel_width!=w*128 or screen.info().pixel_height!=h*128:
            raise RuntimeError('MediaPlayer screen dimensions do not match the wall')
        labels={tile_label(q['screen'],i,w,h):i for i in indices}
        tiles={}
        for slot in range(inventory.size):
            item=inventory.get_item(slot)
            if item is None or item.type.id!='minecraft:filled_map': continue
            meta=item.item_meta
            label=getattr(meta,'display_name',None)
            if label not in labels: continue
            i=labels[label]
            if i in tiles or item.amount!=1 or not getattr(meta,'has_map_id',False):
                raise RuntimeError('Duplicate or unverifiable MediaPlayer map tile')
            tiles[i]=(slot,int(meta.map_id),label)
        if set(tiles)!=set(indices):
            raise RuntimeError(f'Expected {len(indices)} matching map tiles, found {len(tiles)}; retain tiles for recovery')
        ids=[tiles[i][1] if i in tiles else None for i in range(w*h)]
        slot=f'panel{self.mount_slot:03}'; self.mount_slot+=1
        remember_map_ids(Path.cwd(),q,ids)
        structure=write_slot(Path.cwd(),slot,filled_panel(q,ids))
        self.mounts[q['id']]={'request':q,'player':player.name,'tiles':tiles,'started':time.monotonic()}
        self.emit_mount('mount_load',{'id':q['id'],'screen':q['screen'],'structure':structure,
                                    'low':dict(zip('xyz',low)),'size':dict(zip('xyz',size)), 'indices':indices})

    def remove(self,player,q):
        if not player.is_op: raise PermissionError('Only an operator may remove a Terminal')
        panel_geometry(q)
        if any(v['request']['screen']==q['screen'] for v in self.mounts.values()):
            raise RuntimeError('Screen mount already in progress')
        screen=self.mp.screen(q['screen'])
        if screen.info().pixel_width!=q['width']*128 or screen.info().pixel_height!=q['height']*128:
            raise RuntimeError('Registered display dimensions changed')
        if q['screen'] in self.story_modes or q['screen'] in self.map_modes or any(v[0]==q['screen'] for v in self.pending.values()):
            raise RuntimeError('Exit the active application or wait for the command before removing this Terminal')
        screen.delete()
        self.transcripts.pop(q['screen'],None)
        self.emit_mount('remove_result',{'id':q['id'],'screen':q['screen'],'ok':True,'reason':''})

    def relabel(self,player,q):
        if not player.is_op: raise PermissionError('Only an operator may refresh existing map labels')
        if not self.mount_ready or self.mount_slot>=SLOTS: raise RuntimeError('Native mount unavailable')
        if any(v['request']['screen']==q['screen'] for v in self.mounts.values()):
            raise RuntimeError('Screen mount already in progress')
        screen=self.mp.screen(q['screen'])
        if screen.info().pixel_width!=q['width']*128 or screen.info().pixel_height!=q['height']*128:
            raise RuntimeError('Registered display dimensions changed')
        ids=repair_map_ids(Path.cwd(),q)
        _,low,size,_=panel_geometry(q)
        slot=f'panel{self.mount_slot:03}'; self.mount_slot+=1
        structure=write_slot(Path.cwd(),slot,filled_panel(q,ids))
        self.mounts[q['id']]={'request':q,'player':player.name,'relabel':True,'started':time.monotonic()}
        self.emit_mount('mount_load',{'id':q['id'],'screen':q['screen'],'structure':structure,
                                    'low':dict(zip('xyz',low)),'size':dict(zip('xyz',size)), 'indices':list(range(len(ids)))})

    def mount_ack(self,ack):
        state=self.mounts.pop(ack.get('id'),None)
        if not state:return
        q=state['request']
        if state.get('relabel'):
            ok=ack.get('screen')==q['screen'] and ack.get('ok') is True and ack.get('mounted')==list(range(q['width']*q['height']))
            self.emit_mount('relabel_result',{'id':q['id'],'screen':q['screen'],'ok':ok,'reason':str(ack.get('reason',''))[:300]})
            if ok:
                self.logger.info('CINTERFORGE_TERMINAL_LABELS_REMOVED '+q['screen'])
                self.restore_screen(q['screen'])
            return
        tiles=state['tiles']
        if ack.get('screen')!=q['screen']:
            self.result(q,False,'Mismatched frame mount response');return
        player=self.server.get_player(state['player'])
        if player is None:
            self.result(q,False,'Player left during frame mounting');return
        inventory=player.inventory
        # Only retract exact delivered tiles whose corresponding frame was
        # independently observed occupied after the native structure load.
        mounted=ack.get('mounted',[])
        if not isinstance(mounted,list) or any(type(i) is not int or i not in tiles for i in mounted) or len(set(mounted))!=len(mounted):
            self.result(q,False,'Invalid frame mount response');return
        try:
            for i in mounted:
                slot,map_id,label=tiles[i]
                item=inventory.get_item(slot)
                meta=item.item_meta if item is not None else None
                if item is None or item.type.id!='minecraft:filled_map' or item.amount!=1 or getattr(meta,'map_id',None)!=map_id or getattr(meta,'display_name',None)!=label:
                    raise RuntimeError('A delivered map changed slots during mounting')
            for i in mounted: inventory.set_item(tiles[i][0],None)
            if ack.get('ok') is not True or set(mounted)!=set(tiles):
                raise RuntimeError(ack.get('reason') or 'Native structure load did not fill every requested frame')
            text=PROMPT
            self.transcripts[q['screen']]=text
            self.logger.info('CINTERFORGE_TERMINAL_MATERIALIZED '+q['screen']+'; mounted '+str(len(mounted))+' map tiles')
            self.result(q,True)
        except Exception as exc:
            self.logger.error('CINTERFORGE_TERMINAL_MOUNT_FAILED: '+str(exc))
            self.result(q,False,str(exc))

    def end_focus(self,player):
        self.server.dispatch_command(self.server.command_sender, 'scriptevent cinter:terminal_focus_end '+json.dumps({'player':player.name},separators=(',',':')))

    def open_form(self,player,name):
        screen=self.mp.screen(name)
        key=str(player.unique_id)
        if key in self.focus:return
        if name in self.map_modes:
            self.end_focus(player)
            self.emit_mount('map_mode',{'screen':name,'active':True})
            return
        self.render(screen,self.transcripts.get(name,PROMPT))
        self.show_command_form(player,name)

    def show_command_form(self,player,name):
        # A Bedrock ModalForm response consumes the native form. Keep the
        # camera session, and issue a fresh blank form after native teardown.
        key=str(player.unique_id)
        token=uuid.uuid4().hex
        self.focus[key]=(name,player.name,time.monotonic()+FOCUS_SECONDS,token)
        def active():
            state=self.focus.get(key)
            return state is not None and state[3]==token
        def close(p):
            if not active():return
            self.focus.pop(key,None)
            self.end_focus(p)
        def submit(p,values):
            if not active():return
            # Invalidate this response immediately; a late close or duplicate
            # submit from the consumed form cannot terminate the next form.
            next_token=uuid.uuid4().hex
            self.focus[key]=(name,p.name,time.monotonic()+FOCUS_SECONDS,next_token)
            try:
                if isinstance(values,str):values=json.loads(values)
                if not isinstance(values,list) or len(values)!=1 or not isinstance(values[0],str):
                    raise ValueError('Invalid Terminal form response')
                command=values[0].strip()
                if command.casefold()=='exit' and name not in self.story_modes:
                    self.focus.pop(key,None)
                    self.end_focus(p)
                    return
                if command or name in self.story_modes:
                    if any(v[0]==name for v in self.pending.values()):
                        raise RuntimeError('Wait for the current Terminal command to finish.')
                    if len(command)>180 or any(ord(c)<32 for c in command):
                        raise ValueError('Invalid Terminal command')
                    if command and name not in self.story_modes and command.split(maxsplit=1)[0].casefold() in ('play','craftmaster') and not p.is_op:
                        raise PermissionError('This command requires a server operator.')
                    request=uuid.uuid4().hex
                    info=self.mp.screen(name).info()
                    write_json(self.io/(request+'.request.json'),{'screen':name,'player':p.name,'command':command,'operator':bool(p.is_op),'width':info.pixel_width,'height':info.pixel_height})
                    self.pending[request]=(name,command,time.monotonic())
                    if name not in self.story_modes:
                        self.transcripts[name]=(self.transcripts.get(name,PROMPT).rstrip()+' '+command+'\n')[-16384:]
                        self.render(self.mp.screen(name),self.transcripts[name])
                    self.logger.info('CINTERFORGE_TERMINAL_COMMAND_SUBMITTED '+name)
            except Exception as exc:
                self.logger.error('CINTERFORGE_TERMINAL_COMMAND_FAILED: '+str(exc))
                p.send_message(str(exc))
            def reopen():
                state=self.focus.get(key)
                if state is None or state[3]!=next_token:return
                current=self.server.get_player(state[1])
                if current is None or str(current.unique_id)!=key:
                    self.focus.pop(key,None)
                    return
                try:
                    self.show_command_form(current,name)
                except Exception as exc:
                    self.logger.error('CINTERFORGE_TERMINAL_FORM_FAILED: '+str(exc))
                    current.send_message(str(exc))
            # Do not wait for the host command: poll() renders its output while
            # the player is already typing the next command.
            try:
                self.emit_mount('focus_keepalive',{'player':p.name,'screen':name})
                self.server.scheduler.run_task(self,reopen,delay=3)
            except Exception:
                self.focus.pop(key,None)
                self.end_focus(p)
                raise
        try:
            player.send_form(ModalForm(title=FORM_TITLE,controls=[TextInput('',placeholder='Enter command')],on_submit=submit,on_close=close))
            self.emit_mount('focus_keepalive',{'player':player.name,'screen':name})
            self.logger.info('CINTERFORGE_TERMINAL_FORM_OPEN '+name)
        except Exception:
            close(player)
            raise

    def restore_screen(self,name):
        screen=self.mp.screen(name)
        if name in self.map_modes:
            im=self.map_modes[name]
            screen.update_region(0,0,im.width,im.height,im.tobytes(),format='rgba8888')
        else:
            self.render(screen,self.transcripts.get(name,PROMPT))

    def map_request(self,name,action,point=None,player=None):
        if any(v[0]==name for v in self.pending.values()):
            if action=='click':self.map_clicks[name]=(point,player.name if player else None)
            return
        request=uuid.uuid4().hex
        data={'screen':name,'command':'','action':action}
        if point is not None:data['point']=point
        if player is not None:data['player']=player.name
        write_json(self.io/(request+'.request.json'),data)
        self.pending[request]=(name,'',time.monotonic())
        self.map_refresh[name]=time.monotonic()+5

    def render_map(self,name,frame,bundle):
        screen=self.mp.screen(name);info=screen.info();w,h=info.pixel_width,info.pixel_height
        if (frame['width'],frame['height'])!=(w,h):raise ValueError('Map surface dimensions changed')
        vh=frame['view_height'];im=Image.new('RGBA',(w,h),(0,0,0,255))
        if not 0<vh<h or len(frame['tiles'])>64:raise ValueError('Invalid map viewport')
        for filename,x0,y0,x1,y1 in frame['tiles']:
            if not re.fullmatch(r'\d+-\d+-\d+\.png',filename):raise ValueError('Invalid map tile')
            a,b,c,d=max(0,x0),max(0,y0),min(w,x1),min(vh,y1)
            if a>=c or b>=d:continue
            try:
                with Image.open(bundle/filename) as tile:
                    if tile.width>512 or tile.height>512:raise ValueError('Invalid map tile dimensions')
                    # Crop to the visible source area before resampling; never allocate a full zoomed tile.
                    box=((a-x0)*tile.width/(x1-x0),(b-y0)*tile.height/(y1-y0),(c-x0)*tile.width/(x1-x0),(d-y0)*tile.height/(y1-y0))
                    pixels=tile.convert('RGBA').resize((c-a,d-b),Image.Resampling.BILINEAR,box=box)
                    im.paste(pixels,(a,b))
            except (OSError,ValueError) as exc:
                self.logger.warning('CINTERFORGE_DISPLAY_MAP_TILE_RETRY '+str(exc))
        draw=ImageDraw.Draw(im);font=ImageFont.load_default(size=10 if w<256 else 13)
        title=(frame['title']+' · '+frame['status'])
        while len(title)>1 and font.getlength(title)>w-8:title=title[:-1]
        draw.rectangle((0,0,w-1,15),fill=(10,16,18,255));draw.text((4,2),title,font=font,fill='white')
        if not frame['tiles']:
            msg=frame['status']
            for i,line in enumerate(wrap_pixels(msg,font,w-8)[:max(1,(vh-20)//13)]):draw.text((4,20+i*13),line,font=font,fill='white')
        labels={'zoom+':'+','zoom-':'-','left':'<','right':'>','up':'^','down':'v','home':'H','dimension':{'Overworld':'OW','Nether':'N','End':'E'}.get(frame['dimension'],'D'),'exit':'X'}
        for action,x0,y0,x1,y1 in frame['buttons']:
            draw.rectangle((x0,y0,x1-1,y1-1),fill=(18,29,26,255),outline=(112,145,128,255))
            label=labels[action]
            if x1-x0>=42:label={'home':'Home','exit':'Exit','dimension':frame['dimension'][:3]}.get(action,label)
            draw.text(((x0+x1)/2,(y0+y1)/2),label,font=font,anchor='mm',fill='white')
        screen.update_region(0,0,w,h,im.tobytes(),format='rgba8888')
        entering=name not in self.map_modes
        self.map_modes[name]=im;self.map_refresh[name]=time.monotonic()+5
        if entering:
            # Release the native command form and camera, so the player can aim
            # at the actual map controls. Do not change the normal/IF form path.
            for key,state in list(self.focus.items()):
                if state[0]!=name:continue
                self.focus.pop(key,None)
                player=self.server.get_player(state[1])
                if player is not None:
                    try:player.close_form()
                    except Exception as exc:self.logger.warning('CINTERFORGE_DISPLAY_MAP_FORM_CLOSE '+str(exc))
                    finally:self.end_focus(player)
            self.emit_mount('map_mode',{'screen':name,'active':True})
            self.logger.info('CINTERFORGE_DISPLAY_MAP_READY '+name)

    @event_handler
    def quit(self,event: PlayerQuitEvent):
        self.focus.pop(str(event.player.unique_id),None)

    def poll(self):
        for key,(_,name,deadline,_) in list(self.focus.items()):
            if time.monotonic()>=deadline:
                self.focus.pop(key,None)
                player=self.server.get_player(name)
                if player is not None:
                    try: player.close_form()
                    finally: self.end_focus(player)
        if not self.mount_ready and self.mount_probe_token and self.mount_probe_count<20:
            self.mount_probe_count+=1
            self.emit_mount('mount_probe',{'token':self.mount_probe_token})
            if self.mount_probe_count==20:
                self.logger.warning('CINTERFORGE_TERMINAL_MOUNT_PROBE_NO_ACK')
        for request,state in list(self.mounts.items()):
            if time.monotonic()-state['started']>5:
                self.mounts.pop(request,None)
                if state.get('relabel'):
                    q=state['request']; self.emit_mount('relabel_result',{'id':q['id'],'screen':q['screen'],'ok':False,'reason':'Label refresh timed out'})
                else:self.result(state['request'],False,'Native frame mount acknowledgement timed out; inventory retained')
        for request,(name,command,started) in list(self.pending.items()):
            path=self.io/(request+'.response.json')
            if not path.is_file() and time.monotonic()-started<150: continue
            try:
                result=json.loads(path.read_text('utf-8')) if path.is_file() else {'output':'Terminal command timed out.','mode':'if' if name in self.story_modes else 'terminal'}
                if result.get('mode')=='map':
                    self.render_map(name,result['map'],self.io/(request+'.tiles'))
                    continue
                if name in self.map_modes:
                    self.map_modes.pop(name,None);self.map_refresh.pop(name,None);self.map_clicks.pop(name,None)
                    self.emit_mount('map_mode',{'screen':name,'active':False})
                output=result.get('output','')
                if result.get('mode')=='if':
                    self.story_modes.add(name)
                else:
                    self.story_modes.discard(name)
                text=output if result.get('replace') else self.transcripts.get(name,GREETING)+output
                if name not in self.story_modes: text+='\n> '
                self.transcripts[name]=text[-16384:]
                self.render(self.mp.screen(name),self.transcripts[name])
            except Exception as exc: self.logger.error('CINTERFORGE_TERMINAL_RENDER_FAILED: '+str(exc))
            finally:
                path.unlink(missing_ok=True)
                shutil.rmtree(self.io/(request+'.tiles'),ignore_errors=True)
                self.pending.pop(request,None)
        for name,(point,player_name) in list(self.map_clicks.items()):
            if name not in self.map_modes:self.map_clicks.pop(name,None);continue
            if any(v[0]==name for v in self.pending.values()):continue
            self.map_clicks.pop(name,None)
            player=self.server.get_player(player_name) if player_name else None
            if player is not None:
                try:self.map_request(name,'click',point,player)
                except Exception as exc:self.logger.error('CINTERFORGE_DISPLAY_MAP_CLICK_FAILED '+str(exc))
        for name,due in list(self.map_refresh.items()):
            if time.monotonic()>=due:
                try:self.map_request(name,'refresh')
                except Exception as exc:self.logger.error('CINTERFORGE_DISPLAY_MAP_REFRESH_FAILED '+str(exc))
