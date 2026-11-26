// Minecraft Script API → the existing Bedrock console → the existing Terminal host.
// The pack transports existing events and prepares the operator-requested test wall.
import { world, system, ScriptEventSource, ItemStack, CommandPermissionLevel,
  CustomCommandParamType, CustomCommandStatus, InputPermissionCategory } from '@minecraft/server';

function emit(type, sender, text) {
  const who = String(sender ?? 'World').slice(0, 32).replace(/[\r\n\x00-\x1f]/g, ' ').trim();
  const message = String(text ?? '').replace(/[\r\n\x00]/g, ' ').trim();
  if (!who || !message) return;
  // Send one copy of the command; the content-log transport is line-bounded.
  const payload = type === 'command'
    ? {type, sender: who, command: message.slice(0, 1800)}
    : {type, sender: who, message: message.slice(0, 240)};
  console.warn('CINTERFORGE_MC_STATUS:' + JSON.stringify(payload));
}

// Command registry is available in stable @minecraft/server 2.1.0.
// Native callbacks run in restricted context; inventory changes run next tick.
const TERMINAL = 'cinter:terminal';
const WIDTH = 'cinter:width', HEIGHT = 'cinter:height';
const SIZES = Object.freeze({ '1':[1,1], '2':[2,2], '3':[3,3], '4':[4,4], sm:[2,1], med:[3,2], lg:[4,3], xl:[5,4] });
const sizedId = key => TERMINAL+'_'+key;
function itemDimensions(item) {
  if (!item) return undefined;
  if (item.typeId===TERMINAL) return dimensions(item.getDynamicProperty(WIDTH)??5,item.getDynamicProperty(HEIGHT)??4);
  const key=Object.keys(SIZES).find(k=>item.typeId===sizedId(k));
  return key===undefined?undefined:SIZES[key];
}
function matchesItem(item,w,h) {
  const size=itemDimensions(item); return !!size && size[0]===w && size[1]===h;
}
const MAX_WIDTH = 7, MAX_HEIGHT = 4; // physical MediaPlayer limit, not logical SDK limit
const ok = message => ({status: CustomCommandStatus.Success, message});
const fail = message => ({status: CustomCommandStatus.Failure, message});
function dimensions(width = 5, height = 4) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || width > MAX_WIDTH || height < 1 || height > MAX_HEIGHT) {
    throw new Error('Terminal dimensions must be 1–7 tiles wide and 1–4 tiles high.');
  }
  return [width, height];
}
function terminalItem(width, height, amount=1) {
  const [w,h] = dimensions(width,height);
  const key=Object.keys(SIZES).find(k=>SIZES[k][0]===w && SIZES[k][1]===h);
  if(key!==undefined) return new ItemStack(sizedId(key),amount);
  if(amount!==1) throw new Error('Legacy custom dimensions cannot stack');
  const item = new ItemStack(TERMINAL,1);
  item.setDynamicProperty(WIDTH,w);
  item.setDynamicProperty(HEIGHT,h);
  item.nameTag = 'CinterForge Terminal';
  item.setLore([`${w} × ${h} tiles (${w*128} × ${h*128} pixels)`]);
  return item;
}
function giveTerminal(origin, width, height) {
  const player = origin.sourceEntity;
  if (player?.typeId !== 'minecraft:player') return fail('Run this command as a player.');
  let size;
  // One dimension means a square. Explicit width/height retains existing rectangular items.
  try {
    const key=String(width??'xl').toLowerCase();
    if(height===undefined && Object.hasOwn(SIZES,key)) size=SIZES[key];
    else if(height!==undefined && /^[1-7]$/.test(key)) size=dimensions(Number(key),height);
    else throw new Error('Choose 1, 2, 3, 4, sm, med, lg or xl.');
  } catch (e) { return fail(String(e.message)); }
  system.run(() => {
    try {
      const inventory = player.getComponent('minecraft:inventory')?.container;
      if (!inventory) throw new Error('Your inventory is unavailable.');
      const remainder = inventory.addItem(terminalItem(...size));
      if (remainder) throw new Error('Could not add Terminal to your inventory.');
      player.sendMessage(`CinterForge Terminal ${size[0]} × ${size[1]} added to your inventory.`);
    } catch (error) { try { player.sendMessage(String(error.message ?? error)); } catch (_) {} }
  });
  return ok('Terminal inventory request accepted.');
}
function hostCommand(name, origin, argument) {
  const player = origin.sourceEntity;
  if (player?.typeId !== 'minecraft:player') return fail('Run this command as a player.');
  if (typeof argument !== 'string' || !argument.trim() || argument !== argument.trim() ||
      argument.length > 160 || argument.startsWith('-') || /[\x00-\x1f\x7f]/.test(argument)) return fail('Supply one game or world name (quote names containing spaces).');
  // Preserve the single existing console transport, but use a structured
  // envelope so player data is never interpreted as a new shell command.
  const sender = String(player.name).slice(0,32);
  system.run(() => console.warn('CINTERFORGE_MC_STATUS:' + JSON.stringify({type:'native-command',sender,name,argument})));
  return ok(`Forwarded ${name} request to CinterForge Terminal.`);
}
system.beforeEvents.startup.subscribe(init => {
  const registry = init.customCommandRegistry;
  const admin = CommandPermissionLevel.Admin;
  registry.registerCommand({name:TERMINAL, description:'Give yourself a CinterForge Terminal: 1, 2, 3, 4, sm, med, lg, xl', permissionLevel:admin,
    cheatsRequired:false, optionalParameters:[{name:'size',type:CustomCommandParamType.String},{name:'height',type:CustomCommandParamType.Integer}]},giveTerminal);
  for (const name of ['play','craftmaster']) {
    registry.registerCommand({name:`cinter:${name}`, description:`Run the existing host ${name} command`,permissionLevel:admin,
      cheatsRequired:false,mandatoryParameters:[{name:name==='play'?'game':'world',type:CustomCommandParamType.String}]},
      (origin,argument)=>hostCommand(name,origin,argument));
  }
  registry.registerCommand({name:'cinter:help',description:'Show CinterForge commands',permissionLevel:CommandPermissionLevel.Any,cheatsRequired:false},()=> {
    // Test uses the actual registered native command from the BDS console.
    system.run(()=>console.warn('CINTERFORGE_NATIVE_COMMAND_HELP_READY'));
    return ok('/cinter:terminal [1|2|3|4|sm|med|lg|xl]; /cinter:play <game>; /cinter:craftmaster <world>; /cinter:help');
  });
  console.warn('CINTERFORGE_NATIVE_COMMANDS_REGISTERED');
});
// A physical terminal is owned by MediaPlayer. The pack owns inventory and
// world geometry, and acknowledges placement only after provider materialization.
const DISPLAY_KEY = 'cinterforge:physical_terminals';
const placements = new Map();
const FOCUS_KEY='cinterforge:terminal_focus';
const focused=new Map();
const mapScreens=new Set();
function endFocus(player) {
  focused.delete(player.id);
  const old=player.getDynamicProperty(FOCUS_KEY);
  if(old===undefined)return;
  try { player.camera.clear(); } finally {
    player.inputPermissions.setPermissionCategory(InputPermissionCategory.Movement,old===true);
    player.setDynamicProperty(FOCUS_KEY,undefined);
    focused.delete(player.id);
  }
}
function renewFocus(player,screen) {
  // Each accepted command renews the watchdog; obsolete timers must not
  // release the camera while the next input form is active.
  const token=screenId();focused.set(player.id,token);
  system.runTimeout(()=>{if(focused.get(player.id)===token) {try {providerEvent(player.dimension,'close',{player:player.name,screen});endFocus(player);}catch(_){} focused.delete(player.id);}},2400);
}
function focusDisplay(player,display) {
  if(focused.has(player.id)) {
    providerEvent(player.dimension,'close',{player:player.name,screen:display.screen});
    endFocus(player);return;
  }
  if(!panelOccupied(player.dimension,display)) {
    player.sendMessage("Terminal frames are not all filled; display interaction is unavailable.");
    return;
  }
  renewFocus(player,display.screen);
  try {
    if(Number.isInteger(display.dx) && Number.isInteger(display.dz)) {
      const xs=display.cells.map(p=>p.x),ys=display.cells.map(p=>p.y),zs=display.cells.map(p=>p.z);
      const center={x:(Math.min(...xs)+Math.max(...xs))/2+.5,y:(Math.min(...ys)+Math.max(...ys))/2+.5,z:(Math.min(...zs)+Math.max(...zs))/2+.5};
      const distance=Math.max(1.4,Math.max(display.width,display.height)*.95);
      player.setDynamicProperty(FOCUS_KEY,player.inputPermissions.isPermissionCategoryEnabled(InputPermissionCategory.Movement));
      player.inputPermissions.setPermissionCategory(InputPermissionCategory.Movement,false);
      player.camera.setCamera('minecraft:free',{location:{x:center.x+display.dx*distance,y:center.y,z:center.z+display.dz*distance},facingLocation:center});
    }
    providerEvent(player.dimension,'open',{player:player.name,screen:display.screen});
  } catch(error) {try {endFocus(player);}finally {focused.delete(player.id);} player.sendMessage(String(error.message??error));}
}
const directions = {south:[0,1,180],north:[0,-1,0],east:[1,0,90],west:[-1,0,-90]};
function displays() { return JSON.parse(world.getDynamicProperty(DISPLAY_KEY) ?? '[]'); }
function saveDisplays(records) { world.setDynamicProperty(DISPLAY_KEY,JSON.stringify(records)); }
function providerEvent(dim,id,payload) {
  dim.runCommand(`scriptevent cinter:terminal_${id}_provider ${JSON.stringify(payload)}`);
}
function samePos(a,b) { return a.x===b.x && a.y===b.y && a.z===b.z; }
// Stable block state can prove occupancy, not the underlying map ID.
// The provider's inventory-delivery acknowledgement is not physical proof.
function frameState(dim,p,display) {
  const b=dim.getBlock(p);
  const expected=display.dx===1?5:display.dx===-1?4:display.dz===1?3:2;
  if(b?.typeId!=='minecraft:frame' || b.permutation.getState('facing_direction')!==expected) return 'missing';
  return b.permutation.getState('item_frame_map_bit')===true?'occupied':'empty';
}
function panelOccupied(dim,display) {
  try {return display.cells.length===display.width*display.height && display.cells.every(p=>frameState(dim,p,display)==='occupied');}
  catch(_) {return false;}
}
function tileStates(dim,display) {
  // Native provider orders tiles top-left first; our cells start bottom-left.
  return Array.from({length:display.width*display.height},(_,i)=> {
    const row=Math.floor(i/display.width),col=i%display.width;
    return frameState(dim,display.cells[(display.height-1-row)*display.width+col],display);
  });
}
function mountPayload(id,screen,panel,player,extra={}) {
  return {id,screen,width:panel.width,height:panel.height,player:player.name,
    origin:panel.cells[0],dx:panel.dx,dz:panel.dz,...extra};
}
// Repair the registered display in place, never allocate a new screen. Only
// its original frames or air may be replaced, and all backing must survive.
function repairablePanel(dim,display) {
  if(display.cells.length!==display.width*display.height)throw new Error('Invalid registered Terminal geometry');
  for(const p of display.cells) {
    const backing=dim.getBlock({x:p.x-display.dx,y:p.y,z:p.z-display.dz});
    if(!backing || backing.isAir || backing.isLiquid)throw new Error('Restore the Terminal backing wall before repairing');
    const b=dim.getBlock(p);
    if(!b || (!b.isAir && frameState(dim,p,display)==='missing'))throw new Error('Clear obstructions from the original Terminal rectangle before repairing');
  }
}
function repairDisplay(player,dim,display) {
  if([...placements.values()].some(q=>q.player.id===player.id || q.screen===display.screen))return;
  let id;
  try {
    repairablePanel(dim,display);
    id=screenId().slice(7);
    placements.set(id,{player,panel:display,dim,screen:display.screen,relabel:true});
    providerEvent(dim,'relabel',mountPayload(id,display.screen,display,player));
    system.runTimeout(()=>finishRepair(id,false,'Timed out waiting for Terminal repair'),200);
  } catch(error) {
    if(id)placements.delete(id);
    player.sendMessage(String(error.message??error));
  }
}
function finishRepair(id,ok,reason,screen) {
  const q=placements.get(id);
  if(!q?.relabel)return;
  placements.delete(id);
  try {
    if(!ok || screen!==q.screen || !panelOccupied(q.dim,q.panel))throw new Error(reason||'Terminal repair not verified');
    saveDisplays(displays().map(d=>d.screen===screen?{...d,cleanLabels:true}:d));
    q.player.sendMessage('CinterForge Terminal repaired. Click to enter commands.');
    console.warn('CINTERFORGE_TERMINAL_REPAIR_READY:'+screen);
  } catch(error) {
    console.warn('CINTERFORGE_TERMINAL_REPAIR_FAILED:'+String(error));
    q.player.sendMessage(String(error.message??error));
    // A label refresh is optional if the old display is already complete.
    if(panelOccupied(q.dim,q.panel))focusDisplay(q.player,q.panel);
  }
}
function requestRemoval(player,dim,display) {
  if(display.pending || [...placements.values()].some(q=>q.player.id===player.id || q.screen===display.screen))throw new Error('Finish the pending Terminal operation first.');
  repairablePanel(dim,display);
  const inventory=player.getComponent('minecraft:inventory')?.container;
  if(!inventory)throw new Error('Inventory unavailable');
  const item=terminalItem(display.width,display.height);
  // Reserve a place for the returned item before dismantling.
  let returnSlot=-1;
  for(let i=0;i<inventory.size;i++) {
    const existing=inventory.getItem(i);
    if(!existing || (existing.typeId===item.typeId && item.typeId!==TERMINAL && existing.amount<64)) {returnSlot=i;break;}
  }
  if(returnSlot<0)throw new Error('Free an inventory slot before removing the Terminal.');
  if(focused.has(player.id)){providerEvent(dim,'close',{player:player.name,screen:display.screen});endFocus(player);}
  const id=screenId().slice(7);
  placements.set(id,{player,dim,panel:display,screen:display.screen,remove:true,returnSlot});
  providerEvent(dim,'remove',mountPayload(id,display.screen,display,player));
  system.runTimeout(()=>finishRemoval(id,false,'Timed out waiting for provider removal',display.screen),200);
}
function finishRemoval(id,ok,reason,screen) {
  const q=placements.get(id); if(!q?.remove)return;
  placements.delete(id);
  try {
    if(!ok || screen!==q.screen)throw new Error(reason||'Provider did not remove Terminal');
    for(const p of q.panel.cells)if(frameState(q.dim,p,q.panel)!=='missing')q.dim.setBlockType(p,AIR);
    if(q.panel.cells.some(p=>q.dim.getBlock(p)?.typeId==='minecraft:frame'))throw new Error('Provider left frames behind; inspect the log.');
    // Provider deleted the old screen. Never overwrite another inventory item.
    const inventory=q.player.getComponent('minecraft:inventory')?.container;
    const remainder=inventory.addItem(terminalItem(q.panel.width,q.panel.height));
    if(remainder)q.dim.spawnItem(remainder,q.player.location);
    saveDisplays(displays().filter(d=>d.screen!==screen));
    q.player.sendMessage('Terminal removed and returned. Adjust the backing size if needed, then click the new bottom-left block.');
    console.warn('CINTERFORGE_TERMINAL_REMOVED:'+screen);
  } catch(error){q.player.sendMessage(String(error.message??error));console.warn('CINTERFORGE_TERMINAL_REMOVE_FAILED:'+String(error));}
}
function recoverDisplay(player,dim,display,slot) {
  if([...placements.values()].some(q=>q.player.id===player.id || q.screen===display.screen))return;
  let id;
  try {
    const held=player.getComponent('minecraft:inventory')?.container?.getItem(slot);
    if(!display.pending && player.isSneaking && itemDimensions(held)) {requestRemoval(player,dim,display);return;}
    const states=tileStates(dim,display);
    if(!display.pending && (states.some(state=>state!=='occupied') || player.isSneaking)) {
      if(focused.has(player.id)){focusDisplay(player,display);return;}
      repairDisplay(player,dim,display);return;
    }
    if(states.includes('missing'))throw new Error('Terminal backing frames are missing or changed');
    const empty=states.flatMap((state,i)=>state==='empty'?[i]:[]);
    if(display.pending) {
      const item=player.getComponent('minecraft:inventory')?.container?.getItem(slot);
      if(!matchesItem(item,display.width,display.height))
        throw new Error('Hold the retained Terminal item and click its frames to complete installation');
    }
    if(!empty.length) {
      if(display.pending)throw new Error('Terminal is awaiting a verified inventory acknowledgement; inspect server log');
      focusDisplay(player,display); return;
    }
    id=screenId().slice(7);
    placements.set(id,{player,slot,panel:display,dim,screen:display.screen,recover:true,consume:display.pending===true});
    console.warn('CINTERFORGE_TERMINAL_RECOVERY_REQUEST:'+display.screen+' remaining='+empty.length);
    providerEvent(dim,'recover',mountPayload(id,display.screen,display,player,{empty}));
    system.runTimeout(()=>finishPlacement(id,false,'Timed out waiting for native frame mounting'),200);
  } catch(error) {
    if(id && placements.has(id))finishPlacement(id,false,String(error));
    else player.sendMessage(String(error.message??error));
  }
}

function mountStructure(data) {
  const q=placements.get(data.id);
  if(!q || q.screen!==data.screen)return;
  const {panel,dim}=q;
  let started=false;
  const reply=(ok,reason,mounted=[])=>providerEvent(dim,'mount_ack',{id:data.id,screen:data.screen,ok,reason,mounted});
  try {
    if(!/^cinterforge_terminal:panel[0-9]{3}$/.test(data.structure))throw new Error('Unrecognized structure slot');
    const before=tileStates(dim,panel);
    if(q.relabel)repairablePanel(dim,panel);
    const empty=q.relabel?Array.from({length:panel.width*panel.height},(_,i)=>i):before.flatMap((state,i)=>state==='empty'?[i]:[]);
    if((!q.relabel && before.includes('missing')) || !Array.isArray(data.indices) || JSON.stringify(empty)!==JSON.stringify(data.indices))
      throw new Error('Target frames changed before mounting');
    const low={},size={};
    for(const k of ['x','y','z']) {
      low[k]=Math.min(...panel.cells.map(p=>p[k]));
      size[k]=Math.max(...panel.cells.map(p=>p[k]))-low[k]+1;
      if(low[k]!==data.low[k] || size[k]!==data.size[k])throw new Error('Mismatched mount geometry');
    }
    const structure=world.structureManager.get(data.structure);
    if(!structure || ['x','y','z'].some(k=>structure.size[k]!==size[k]))throw new Error('Server did not resolve the generated map structure');
    // Verify the native loader sees frame states rather than its startup placeholder.
    const facing=panel.dx===1?5:panel.dx===-1?4:panel.dz===1?3:2;
    for(const i of empty) {
      const row=Math.floor(i/panel.width),col=i%panel.width;
      const p=panel.cells[(panel.height-1-row)*panel.width+col];
      const b=structure.getBlockPermutation({x:p.x-low.x,y:p.y-low.y,z:p.z-low.z});
      if(b?.type?.id!=='minecraft:frame' || b.getState('facing_direction')!==facing || b.getState('item_frame_map_bit')!==true)
        throw new Error('Generated frame structure does not match target');
    }
    started=true;
    world.structureManager.place(structure,dim,low,{includeEntities:false});
    // The actual blocks, not the structure data or provider's backend, prove placement.
    system.runTimeout(()=> {
      try {
        const after=tileStates(dim,panel),mounted=empty.filter(i=>after[i]==='occupied');
        reply(after.every(s=>s==='occupied') && mounted.length===empty.length,
          mounted.length===empty.length?'':'Native structure load did not fill every frame',mounted);
      } catch(error) {reply(false,String(error));}
    },1);
  } catch(error) {
    const mounted=started?data.indices.filter(i=>tileStates(dim,panel)[i]==='occupied'):[];
    reply(false,String(error),mounted);
  }
}

function panelCells(origin,dx,dz,w,h) {
  const cells=[];
  for(let y=0;y<h;y++) for(let x=0;x<w;x++)
    cells.push({x:origin.x+x*dz,y:origin.y+y,z:origin.z-x*dx});
  return cells;
}
function anchoredPanel(dim,hit,face,w,h) {
  const normal=directions[face.toLowerCase()];
  if(!normal) throw new Error('Place the Terminal against a vertical wall.');
  const [dx,dz,yaw]=normal;
  // The clicked backing block is always the player's bottom-left corner.
  // MediaPlayer discovers the exposed backing from the player's feet row,
  // so reject ambiguous larger rectangles rather than moving the anchor.
  const origin={x:hit.x,y:hit.y,z:hit.z};
  const cells=panelCells(origin,dx,dz,w,h);
  // Block.isSolid is pre-release, not part of our stable 2.1.0 contract.
  // Check stable block properties here; MediaPlayer performs the definitive
  // native backing/physical-screen validation before inventory consumption.
  const inspect=p=>{
    const backing=dim.getBlock(p), front=dim.getBlock({x:p.x+dx,y:p.y,z:p.z+dz});
    return {position:p,backing:backing?.typeId??'unloaded',front:front?.typeId??'unloaded',
      valid:!!backing && backing.isAir===false && backing.isLiquid===false && front?.isAir===true};
  };
  const clearBacking=p=>inspect(p).valid;
  for(const p of cells) {
    const cell=inspect(p);
    if(!cell.valid) throw new Error(`Invalid backing at ${p.x},${p.y},${p.z}: ${cell.backing}; front: ${cell.front}. Click the bottom-left backing block of a clear ${w} × ${h} vertical wall.`);
  }
  for(let j=0;j<h;j++) {
    if(clearBacking({x:origin.x-dz,y:origin.y+j,z:origin.z+dx}) ||
       clearBacking({x:origin.x+w*dz,y:origin.y+j,z:origin.z-w*dx}))
      throw new Error(`MediaPlayer requires an exposed backing exactly ${w} blocks wide. Click its bottom-left block.`);
  }
  for(let i=0;i<w;i++) if(clearBacking({x:origin.x+i*dz,y:origin.y+h,z:origin.z-i*dx}))
    throw new Error(`MediaPlayer requires an exposed backing exactly ${h} blocks high above the clicked row.`);
  return {origin,dx,dz,yaw,width:w,height:h,cells:cells.map(p=>({x:p.x+dx,y:p.y,z:p.z+dz}))};
}
function screenId() {
  return 'cinter_'+Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('');
}
function finishPlacement(id,ok,reason,screen) {
  const request=placements.get(id);
  if(!request) return;
  placements.delete(id);
  const {player,slot,old,rotation,panel,dim}=request;
  const record={screen:request.screen,dimension:dim.id,cells:panel.cells,dx:panel.dx,dz:panel.dz,width:panel.width,height:panel.height,cleanLabels:true};
  try {
    if(old)player.teleport(old,{dimension:dim,rotation});
    if(!ok) throw new Error(reason || 'Display materialization failed');
    if(screen!==request.screen) throw new Error('Mismatched display acknowledgement');
    if(!panelOccupied(dim,panel)) throw new Error('Provider did not fill all Terminal frames; placement not completed');
    if(request.consume) {
      const inventory=player.getComponent('minecraft:inventory')?.container;
      const item=inventory?.getItem(slot);
      if(!matchesItem(item,panel.width,panel.height))
        throw new Error('Terminal item moved during placement');
      if(item.amount===1) inventory.setItem(slot,undefined);
      else {item.amount--;inventory.setItem(slot,item);}
    }
    saveDisplays([...displays().filter(d=>d.screen!==screen),record]);
    player.sendMessage('CinterForge Terminal installed. Click the screen to enter commands.');
    console.warn('CINTERFORGE_TERMINAL_PLACEMENT_READY:'+screen);
  } catch(error) {
    // Keep exact locations of provider-created frames for an automatic retry.
    // An old registered screen never owes another Terminal item.
    try {
      if(tileStates(dim,panel).every(s=>s!=='missing'))
        saveDisplays([...displays().filter(d=>d.screen!==request.screen),{...record,pending:request.consume===true}]);
    } catch(_) {}
    console.warn('CINTERFORGE_TERMINAL_PLACEMENT_FAILED:'+JSON.stringify({id,screen:request.screen,reason:String(error)}));
    try {player.sendMessage(String(error.message ?? error)+'; see server log.');} catch(_) {}
  }
}
world.beforeEvents.playerInteractWithBlock.subscribe(event=> {
  const player=event.player, dim=event.block.dimension, hit=event.block.location;
  const face=String(event.blockFace).toLowerCase();
  const display=displays().find(d=>d.dimension===dim.id && d.cells.some(p=>samePos(p,hit) ||
    (directions[face]?.[0]===d.dx && directions[face]?.[1]===d.dz && samePos({x:p.x-d.dx,y:p.y,z:p.z-d.dz},hit))));
  if(display) {
    event.cancel=true;
    if(event.isFirstEvent) {
      const slot=player.selectedSlotIndex;
      if(mapScreens.has(display.screen) && !player.isSneaking && !display.pending) {
        const local=event.faceLocation;
        const cell=display.cells.findIndex(p=>samePos(p,hit)||samePos({x:p.x-display.dx,y:p.y,z:p.z-display.dz},hit));
        // Cells are registered bottom-left first. Project the native face hit
        // onto the same right/up axes used by the physical map tiles.
        const frac=local && (display.dz===1?local.x:display.dz===-1?1-local.x:display.dx===-1?local.z:1-local.z);
        const pixel=v=>Math.max(0,Math.min(127,Math.floor(v*128)));
        const point=local?[cell%display.width*128+pixel(frac),(display.height-1-Math.floor(cell/display.width))*128+pixel(1-local.y)]:null;
        system.run(()=>{
          if(!panelOccupied(dim,display)) {recoverDisplay(player,dim,display,slot);return;}
          if(cell>=0 && point && point.every(Number.isFinite))providerEvent(dim,'map_click',{player:player.name,screen:display.screen,point});
        });
      } else system.run(()=>recoverDisplay(player,dim,display,slot));
    }
    return;
  }
  const chosen=itemDimensions(event.itemStack);
  if(!chosen)return;
  event.cancel=true;
  if(!event.isFirstEvent || [...placements.values()].some(q=>q.player.id===player.id)) return;
  const slot=player.selectedSlotIndex;
  const [width,height]=chosen;
  system.run(()=> {
    let id;
    try {
      const [w,h]=dimensions(width,height);
      const panel=anchoredPanel(dim,hit,face,w,h);
      const screen=screenId(); id=screen.slice(7);
      const old=player.location,rotation=player.getRotation();
      const inventory=player.getComponent('minecraft:inventory')?.container;
      if(!matchesItem(inventory?.getItem(slot),w,h)) throw new Error('Hold the selected Terminal item to place it');
      placements.set(id,{player,slot,old,rotation,panel,dim,screen,consume:true});
      const p=panel.cells[Math.floor(w/2)];
      player.teleport({x:p.x+.5,y:p.y+.05,z:p.z+.5},{dimension:dim,rotation:{x:0,y:panel.yaw}});
      console.warn('CINTERFORGE_TERMINAL_PLACEMENT_REQUEST:'+JSON.stringify({id,screen,width:w,height:h,player:player.name,origin:panel.origin}));
      providerEvent(dim,'place',mountPayload(id,screen,panel,player));
      system.runTimeout(()=>finishPlacement(id,false,'Timed out waiting for native frame mounting'),200);
    } catch(error) {
      if(id && placements.has(id)) finishPlacement(id,false,String(error));
      else {
        console.warn('CINTERFORGE_TERMINAL_PLACEMENT_FAILED:'+JSON.stringify({player:player.name,dimension:dim.id,hit,face,width,height,reason:String(error)}));
        player.sendMessage(String(error.message??error));
      }
    }
  });
});
// The physical provider persists its screens. Refresh the greeting after a
// world is entered, without rebuilding any frames or replacing player data.
world.afterEvents.playerSpawn.subscribe(event=> {
  if(!event.initialSpawn)return;
  try {endFocus(event.player);}catch(error){console.warn('CINTERFORGE_TERMINAL_FOCUS_FAILED:'+String(error));}
  system.runTimeout(()=> {
    for(const display of displays()) if(!display.pending && display.dimension===event.player.dimension.id && panelOccupied(event.player.dimension,display))
      providerEvent(event.player.dimension,'restore',{screen:display.screen});
  },40);
});

world.afterEvents.playerLeave.subscribe(event=>focused.delete(event.playerId));

// Prepare the actual installed TestGrounds near the saved Overworld spawn.
// The host first queries the spawn so it can preload the correct chunks.
const FIXTURE_KEY = 'cinterforge:terminal_smoke_wall';
const OAK = 'minecraft:oak_planks';
const GRASS = 'minecraft:grass_block';
const AIR = 'minecraft:air';
function savedFixture() {
  const value = world.getDynamicProperty(FIXTURE_KEY);
  if (value === undefined) return undefined;
  const prior = JSON.parse(value);
  if (prior.columns !== 5 || prior.rows !== 4 || !prior.chest ||
      ![prior.x,prior.y,prior.z,prior.chest.x,prior.chest.y,prior.chest.z].every(Number.isSafeInteger) ||
      prior.chest.x !== prior.x+4 || prior.chest.y !== prior.y || prior.chest.z !== prior.z-5) {
    throw new Error('Invalid existing TestGrounds preparation marker');
  }
  return prior;
}
function spawnLocation() {
  const p = world.getDefaultSpawnLocation();
  return {x:Math.floor(p.x),y:Math.floor(p.y),z:Math.floor(p.z)};
}
function reportTerminalSpawn(event) {
  if (event.sourceType !== ScriptEventSource.Server) return;
  const previous = savedFixture();
  console.warn('CINTERFORGE_TERMINAL_SPAWN:' + JSON.stringify({spawn:spawnLocation(),previous}));
}
function fixtureCells({x,y,z}) {
  const cells = [];
  for (let j=0;j<6;j++) for (let i=-3;i<=3;i++) {
    const trim = j===0 || j===5 || i===-3 || i===3;
    cells.push({x:x+i,y:y+j,z:z-(trim?7:6)});
  }
  return cells;
}
function fixtureItem(inventory) {
  const item = inventory?.getItem(0);
  return inventory?.size === 27 && item?.typeId === TERMINAL && item.amount === 1 &&
    item.getDynamicProperty(WIDTH) === 5 && item.getDynamicProperty(HEIGHT) === 4 &&
    inventory.emptySlotsCount === inventory.size-1 ? item : undefined;
}
const STOCK_VERSION=1;
const SHOWROOM_VERSION=1;
function stockFixture(inventory) {
  if(inventory?.size!==27) throw new Error('TestGrounds chest is missing or invalid');
  const keys=Object.keys(SIZES), slots=[];
  for(let i=0;i<inventory.size;i++) if(!inventory.getItem(i))slots.push(i);
  if(slots.length<keys.length)throw new Error('Free eight chest slots for the new Terminal sizes; existing contents were preserved.');
  const written=[];
  try {
    keys.forEach((key,i)=> {
      const slot=slots[i]; written.push(slot);
      inventory.setItem(slot,terminalItem(...SIZES[key],64));
      const item=inventory.getItem(slot);
      if(item?.typeId!==sizedId(key)||item.amount!==64)throw new Error('Terminal stack verification failed: '+key);
    });
  } catch(error) {for(const slot of written)inventory.setItem(slot,undefined); throw error;}
}
// Eight freestanding, exactly sized rectangles; no connected backing or frame trim.
function showroom(dim,spawn,y) {
  const occupied=new Set(), result=[], key=p=>`${p.x},${p.y},${p.z}`;
  for(const [size,[w,h]] of Object.entries(SIZES)) {
    let chosen;
    for(const dz of [15,-15,20,-20,12,-12,24,-24]) {
      for(let dx=-22;dx<=22;dx++) {
        const x=spawn.x+dx,z=spawn.z+dz;
        if((dx+w+2)**2+(Math.abs(dz)+3)**2>28**2 || dx*dx+(Math.abs(dz)+3)**2>28**2)continue;
        const reserve=[], cells=[];
        for(let i=-2;i<w+2;i++)for(let j=0;j<=h;j++)for(let k=-1;k<=3;k++)reserve.push({x:x+i,y:y+j,z:z+k});
        if(reserve.some(p=>occupied.has(key(p)) || !dim.getBlock(p)?.isAir))continue;
        if(Array.from({length:w},(_,i)=>({x:x+i,y:y-1,z})).some(p=>dim.getBlock(p)?.typeId!==GRASS))continue;
        for(let i=0;i<w;i++)for(let j=0;j<h;j++)cells.push({x:x+i,y:y+j,z});
        chosen={size,width:w,height:h,origin:{x,y,z},face:'south',cells,reserve};break;
      }
      if(chosen)break;
    }
    if(!chosen)throw new Error('No clear showroom site for '+size+'; existing structures preserved.');
    chosen.reserve.forEach(p=>occupied.add(key(p))); result.push(chosen);
  }
  const added=[];
  try {
    for(const wall of result)for(const p of wall.cells){dim.setBlockType(p,OAK);added.push(p);}
    for(const wall of result)if(wall.cells.some(p=>dim.getBlock(p)?.typeId!==OAK))throw new Error('Showroom wall verification failed');
    return result.map(({cells,reserve,...wall})=>wall);
  } catch(error){for(const p of added.reverse())try{dim.setBlockType(p,AIR);}catch(_){} throw error;}
}
function prepareTerminalWall(event) {
  if (event.sourceType !== ScriptEventSource.Server) return;
  if (String(event.message ?? '').trim() !== 'TestGrounds') throw new Error('Expected TestGrounds');
  const dim = world.getDimension('overworld');
  const spawn = spawnLocation();
  const previous = savedFixture();
  const top = dim.getTopmostBlock({x:spawn.x,z:spawn.z});
  if (!top) throw new Error('TestGrounds spawn surface is not loaded');
  // The saved spawn Y may be a sentinel or stale conversion height. Read the
  // actual terrain, not the metadata height or a fixed absolute coordinate.
  const surfaceY = top.location.y+1;
  const nearSpawn = p => Math.abs(p.x-spawn.x)<=4 && Math.abs(p.z-spawn.z)<=4 && p.y===surfaceY;
  const announce = (proof,previouslyPrepared=false) => {
    const result = {...proof,item:TERMINAL,amount:1,stockVersion:STOCK_VERSION,showroomVersion:SHOWROOM_VERSION,stock:Object.keys(SIZES).map(key=>({size:key,width:SIZES[key][0],height:SIZES[key][1],item:sizedId(key),amount:64})),previouslyPrepared};
    world.setDynamicProperty(FIXTURE_KEY,JSON.stringify(result));
    console.warn('CINTERFORGE_TERMINAL_WALL_READY:' + JSON.stringify(result));
  };
  // Existing correctly positioned installations remain player-owned, even if
  // the player removed the Terminal or changed the wall after installation.
  if (previous && nearSpawn(previous)) {
    if(previous.stockVersion!==STOCK_VERSION) {
      const chest=dim.getBlock(previous.chest);
      if(chest?.typeId!=='minecraft:chest')throw new Error('Restore the TestGrounds chest before stocking the new sizes.');
      stockFixture(chest.getComponent('minecraft:inventory')?.container);
      previous.stockVersion=STOCK_VERSION; world.setDynamicProperty(FIXTURE_KEY,JSON.stringify(previous));
    }
    const walls=previous.showroomVersion===SHOWROOM_VERSION?previous.walls:showroom(dim,spawn,surfaceY);
    announce({...previous,layout:2,spawn:{...spawn,y:surfaceY},walls},true);
    return;
  }
  // Relocate a misplaced legacy installation ONLY if its original wall and
  // exact single Terminal are still intact. Never mint a replacement item.
  let oldInventory, oldItem, oldCells = [];
  if (previous) {
    oldCells = fixtureCells(previous);
    const oldChest = dim.getBlock(previous.chest);
    if (oldChest?.typeId !== 'minecraft:chest') throw new Error('Existing TestGrounds chest was changed; refusing to duplicate player materials.');
    oldInventory = oldChest.getComponent('minecraft:inventory')?.container;
    oldItem = fixtureItem(oldInventory);
    if (!oldItem || oldCells.some(p=>dim.getBlock(p)?.typeId!==OAK)) {
      throw new Error('Existing TestGrounds fixture is not at spawn and has been changed; refusing to overwrite or duplicate player materials. Move it manually or reinstall a fresh TestGrounds save.');
    }
  }
  let plan;
  // Deterministic small search near spawn; a flat grass pad and clear front
  // are required. No modification of the circle, torches or existing builds.
  const offsets = [0,-2,2,-4,4];
  for (const dz of offsets) {
    for (const dx of offsets) {
      const x=spawn.x+dx,z=spawn.z+dz,y=surfaceY;
      if (y < -60 || y > 313 || dim.getBlock({x,y:y-1,z})?.typeId!==GRASS) continue;
      const cells=fixtureCells({x,y,z}), chest={x:x+4,y,z:z-5};
      if (cells.some(p=>!dim.getBlock(p)?.isAir || !dim.getBlock({...p,z:p.z+1})?.isAir)) continue;
      if (cells.filter(p=>p.y===y).some(p=>dim.getBlock({...p,y:y-1})?.typeId!==GRASS)) continue;
      if (!dim.getBlock(chest)?.isAir || !dim.getBlock({...chest,y:y+1})?.isAir ||
          dim.getBlock({...chest,y:y-1})?.typeId!==GRASS) continue;
      plan={x,y,z,cells,chest}; break;
    }
    if (plan) break;
  }
  if (!plan) throw new Error('No clear grass wall and chest site near TestGrounds spawn '+JSON.stringify({x:spawn.x,y:surfaceY,z:spawn.z}));
  const {x,y,z,cells,chest}=plan;
  const added=[], removed=[];
  let chestAdded=false, oldChestRemoved=false, oldItemRemoved=false;
  try {
    for (const p of cells) { dim.setBlockType(p,OAK); added.push(p); }
    if (cells.some(p=>dim.getBlock(p)?.typeId!==OAK)) throw new Error('Wall verification failed');
    dim.setBlockType(chest,'minecraft:chest'); chestAdded=true;
    const inventory=dim.getBlock(chest)?.getComponent('minecraft:inventory')?.container;
    if (!inventory || inventory.size!==27 || inventory.emptySlotsCount!==inventory.size) throw new Error('Smoke chest inventory unavailable or not empty');
    if (oldItem) { oldInventory.setItem(0,undefined); oldItemRemoved=true; }
    if(oldItem) inventory.setItem(0,oldItem);
    stockFixture(inventory);
    if (previous) {
      dim.setBlockType(previous.chest,AIR); oldChestRemoved=true;
      for (const p of oldCells) { dim.setBlockType(p,AIR); removed.push(p); }
    }
    const walls=showroom(dim,spawn,surfaceY);
    announce({layout:2,spawn:{...spawn,y:surfaceY},x,y,z,columns:5,rows:4,face:'south',backingZ:z-6,chest,walls});
  } catch (error) {
    if (chestAdded) try { dim.setBlockType(chest,AIR); } catch (_) {}
    for (const p of added.reverse()) try { dim.setBlockType(p,AIR); } catch (_) {}
    for (const p of removed) try { dim.setBlockType(p,OAK); } catch (_) {}
    if (oldChestRemoved) try { dim.setBlockType(previous.chest,'minecraft:chest'); } catch (_) {}
    if (oldItemRemoved) try { dim.getBlock(previous.chest)?.getComponent('minecraft:inventory')?.container?.setItem(0,oldItem); } catch (_) {}
    throw error;
  }
  emit('status','CinterForge Terminal','TestGrounds wall and chest verified near spawn; eight full stacks of sized Terminals await placement.');
}

// /scriptevent cinter:play tetris → a single "play tetris" host Bash input.
// Reserved terminal_* events never enter the existing shell route.
system.afterEvents.scriptEventReceive.subscribe(event => {
  if (!event.id.startsWith('cinter:')) return;
  const sender = event.sourceEntity?.name ?? event.initiator?.name ??
    (event.sourceBlock ? 'CommandBlock' : 'World');
  if (event.id === 'cinter:status') {
    emit('status', sender, event.message);
    return;
  }
  // Reserved application events never fall through to the existing host shell.
  if (event.id === TERMINAL || event.id.startsWith('cinter:terminal_')) {
    if (event.id === 'cinter:terminal_mount_probe' && event.sourceType === ScriptEventSource.Server) {
      try {
        const r=JSON.parse(event.message);
        const probe=world.structureManager.get('cinterforge_terminal:probe');
        const ok=probe?.size?.x===2 && probe.size.y===1 && probe.size.z===1;
        providerEvent(world.getDimension('overworld'),'mount_probe_ack',{token:r.token,ok});
      } catch(error) {console.warn('CINTERFORGE_TERMINAL_MOUNT_PROBE_FAILED:'+String(error));}
    } else if (event.id === 'cinter:terminal_mount_load' && event.sourceType === ScriptEventSource.Server) {
      try {mountStructure(JSON.parse(event.message));}
      catch(error) {console.warn('CINTERFORGE_TERMINAL_MOUNT_FAILED:'+String(error));}
    } else if (event.id === 'cinter:terminal_map_mode' && event.sourceType === ScriptEventSource.Server) {
      try {const r=JSON.parse(event.message);if(/^cinter_[0-9a-f]{32}$/.test(r.screen)){if(r.active===true)mapScreens.add(r.screen);else mapScreens.delete(r.screen);}}catch(error){console.warn('CINTERFORGE_TERMINAL_MAP_MODE_FAILED:'+String(error));}
    } else if (event.id === 'cinter:terminal_focus_keepalive' && event.sourceType === ScriptEventSource.Server) {
      try {const r=JSON.parse(event.message);const player=world.getAllPlayers().find(p=>p.name===r.player);if(player && focused.has(player.id))renewFocus(player,r.screen);}catch(error){console.warn('CINTERFORGE_TERMINAL_FOCUS_FAILED:'+String(error));}
    } else if (event.id === 'cinter:terminal_focus_end' && event.sourceType === ScriptEventSource.Server) {
      try {const r=JSON.parse(event.message);const player=world.getAllPlayers().find(p=>p.name===r.player);if(player){endFocus(player);focused.delete(player.id);}}catch(error){console.warn('CINTERFORGE_TERMINAL_FOCUS_FAILED:'+String(error));}
    } else if (event.id === 'cinter:terminal_remove_result' && event.sourceType === ScriptEventSource.Server) {
      try {const r=JSON.parse(event.message);finishRemoval(r.id,r.ok,r.reason,r.screen);}
      catch(error){console.warn('CINTERFORGE_TERMINAL_REMOVE_FAILED:'+String(error));}
    } else if (event.id === 'cinter:terminal_relabel_result' && event.sourceType === ScriptEventSource.Server) {
      try {const r=JSON.parse(event.message);finishRepair(r.id,r.ok,r.reason,r.screen);}
      catch(error) {console.warn('CINTERFORGE_TERMINAL_REPAIR_FAILED:'+String(error));}
    } else if (event.id === 'cinter:terminal_display_result' && event.sourceType === ScriptEventSource.Server) {
      try { const r=JSON.parse(event.message); finishPlacement(r.id,r.ok,r.reason,r.screen); }
      catch(error) { console.warn('CINTERFORGE_TERMINAL_PLACEMENT_FAILED:'+String(error)); }
    } else if (event.id === 'cinter:terminal_smoke' || event.id === 'cinter:terminal_spawn') {
      try {
        if (event.id === 'cinter:terminal_spawn') reportTerminalSpawn(event);
        else prepareTerminalWall(event);
      }
      catch (error) { console.warn('CINTERFORGE_TERMINAL_WALL_FAILED:' + String(error)); }
    }
    return;
  }
  const name = event.id.slice('cinter:'.length);
  if (!name) return;
  const command = name + (event.message ? ' ' + event.message : '');
  emit('command', sender, command);
});
// Typed chat is optional, and must never prevent the command adapter from
// initializing if this Bedrock Script API build does not expose chatSend.
if (world.afterEvents.chatSend?.subscribe) {
  world.afterEvents.chatSend.subscribe(event => {
    emit('chat', event.sender?.name ?? 'Player', event.message);
  });
}
console.warn('CINTERFORGE_MC_STATUS_BRIDGE_READY');
