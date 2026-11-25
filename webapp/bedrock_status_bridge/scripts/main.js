// Minecraft Script API → the existing Bedrock console → the existing Terminal host.
// The pack transports existing events and prepares the operator-requested test wall.
import { world, system, ScriptEventSource, ItemStack, CommandPermissionLevel,
  CustomCommandParamType, CustomCommandStatus } from '@minecraft/server';

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
const MAX_WIDTH = 7, MAX_HEIGHT = 4; // physical MediaPlayer limit, not logical SDK limit
const ok = message => ({status: CustomCommandStatus.Success, message});
const fail = message => ({status: CustomCommandStatus.Failure, message});
function dimensions(width = 5, height = 4) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || width > MAX_WIDTH || height < 1 || height > MAX_HEIGHT) {
    throw new Error('Terminal dimensions must be 1–7 tiles wide and 1–4 tiles high.');
  }
  return [width, height];
}
function terminalItem(width, height) {
  const [w,h] = dimensions(width,height);
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
  try { size = dimensions(width,height); } catch (e) { return fail(String(e.message)); }
  system.run(() => {
    try {
      const inventory = player.getComponent('minecraft:inventory')?.container;
      if (!inventory || inventory.emptySlotsCount < 1) throw new Error('Your inventory is full.');
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
  registry.registerCommand({name:TERMINAL, description:'Give yourself one sized CinterForge Terminal', permissionLevel:admin,
    cheatsRequired:false, optionalParameters:[{name:'width',type:CustomCommandParamType.Integer},{name:'height',type:CustomCommandParamType.Integer}]},giveTerminal);
  for (const name of ['play','craftmaster']) {
    registry.registerCommand({name:`cinter:${name}`, description:`Run the existing host ${name} command`,permissionLevel:admin,
      cheatsRequired:false,mandatoryParameters:[{name:name==='play'?'game':'world',type:CustomCommandParamType.String}]},
      (origin,argument)=>hostCommand(name,origin,argument));
  }
  registry.registerCommand({name:'cinter:help',description:'Show CinterForge commands',permissionLevel:CommandPermissionLevel.Any,cheatsRequired:false},()=> {
    // Test uses the actual registered native command from the BDS console.
    system.run(()=>console.warn('CINTERFORGE_NATIVE_COMMAND_HELP_READY'));
    return ok('/cinter:terminal [width] [height]; /cinter:play <game>; /cinter:craftmaster <world>; /cinter:help');
  });
  console.warn('CINTERFORGE_NATIVE_COMMANDS_REGISTERED');
});
// Do not consume the item or fabricate a display if no physical provider has
// been integrated. A real placement acknowledgement must precede consumption.
world.beforeEvents.playerInteractWithBlock.subscribe(event => {
  if (event.itemStack?.typeId !== TERMINAL) return;
  event.cancel = true;
  if (!event.isFirstEvent) return;
  const player = event.player;
  system.run(()=> {
    try { player.sendMessage('CinterForge Terminal display provider is not available for placement yet. Item retained.'); } catch (_) {}
  });
});

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
    const result = {...proof,item:TERMINAL,amount:1,previouslyPrepared};
    world.setDynamicProperty(FIXTURE_KEY,JSON.stringify(proof));
    console.warn('CINTERFORGE_TERMINAL_WALL_READY:' + JSON.stringify(result));
  };
  // Existing correctly positioned installations remain player-owned, even if
  // the player removed the Terminal or changed the wall after installation.
  if (previous && nearSpawn(previous)) {
    announce({...previous,layout:2,spawn:{...spawn,y:surfaceY}},true);
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
    inventory.setItem(0,oldItem ?? terminalItem(5,4));
    if (!fixtureItem(inventory)) throw new Error('Smoke chest Terminal item verification failed');
    if (previous) {
      dim.setBlockType(previous.chest,AIR); oldChestRemoved=true;
      for (const p of oldCells) { dim.setBlockType(p,AIR); removed.push(p); }
    }
    announce({layout:2,spawn:{...spawn,y:surfaceY},x,y,z,columns:5,rows:4,face:'south',backingZ:z-6,chest});
  } catch (error) {
    if (chestAdded) try { dim.setBlockType(chest,AIR); } catch (_) {}
    for (const p of added.reverse()) try { dim.setBlockType(p,AIR); } catch (_) {}
    for (const p of removed) try { dim.setBlockType(p,OAK); } catch (_) {}
    if (oldChestRemoved) try { dim.setBlockType(previous.chest,'minecraft:chest'); } catch (_) {}
    if (oldItemRemoved) try { dim.getBlock(previous.chest)?.getComponent('minecraft:inventory')?.container?.setItem(0,oldItem); } catch (_) {}
    throw error;
  }
  emit('status','CinterForge Terminal','TestGrounds wall and chest verified near spawn; one 5 × 4 Terminal awaits placement.');
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
    if (event.id === 'cinter:terminal_smoke' || event.id === 'cinter:terminal_spawn') {
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
