"""Persistent Bedrock launch properties and managed-world metadata overrides.

Server properties use the supplied Microsoft BDS property reference and the
actual installed package (including optional commented assignments). World
settings use the little-endian level.dat format of CraftMaster's actual
MCTemplate.mcworld; the runtime edits only the managed server copy, never the
Java input, curated export, or a running world. No extra runtime dependencies.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile

# default, description, allowed values; BDS package defaults take precedence.
SERVER_PROPERTIES = {
    'server-name': ('Dedicated Server', 'Used as the server name.', 'Any string without a semicolon symbol.'),
    'gamemode': ('survival', 'Sets the game mode for new players.', 'survival, creative, or adventure.'),
    'force-gamemode': ('false', 'When force-gamemode is set to true, the server will always send clients the game mode set by the gamemode property above, even if that value is different from the value saved by the server during world creation. When it is false, the server will always send the client the value saved during creation, even if the value of the gamemode property is changed after creation.', ''),
    'difficulty': ('easy', 'Sets the difficulty of the world.', 'peaceful, easy, normal, or hard.'),
    'allow-cheats': ('false', 'If true, cheat commands can be used.', 'true or false.'),
    'max-players': ('10', 'The maximum number of players that can play on the server. Note that higher values have a performance impact.', 'any positive integer.'),
    'online-mode': ('true', "If true, all connected players must be authenticated to Xbox Live. Clients connecting to remote (non-LAN) servers will always require Xbox Live authentication regardless of this setting. If the server accepts connections from the Internet, it's highly recommended to enable online-mode!", 'true or false.'),
    'allow-list': ('false', 'If true, all connected players must be listed in the separate allowlist.json file.', 'true or false.'),
    'server-port': ('19132', 'Which IPv4 port the server should listen to.', 'integers in the range 1–65535. Ports under 1024 are usually reserved for well-known applications.'),
    'server-portv6': ('19133', 'Which IPv6 port the server should listen to.', 'integers in the range 1—65535. Ports under 1024 are usually reserved for well-known applications.'),
    'enable-lan-visibility': ('true', 'Listen and respond to clients that are looking for servers on the LAN. This will cause the server to bind to the default ports (19132 and 19133) even when server-port and server-portv6 have non-default values. Consider turning this off if LAN discovery is not desirable, or when running multiple servers on the same host may lead to port conflicts.', 'true or false.'),
    'view-distance': ('32', 'The maximum allowed view distance in number of chunks.', 'positive integers equal to 5 or greater. Higher values have a performance impact.'),
    'tick-distance': ('4', 'The world will be ticked this many chunks away from any player. Higher values have a performance impact.', 'integers in the range 4–12.'),
    'player-idle-timeout': ('30', 'After a player has idled for this many minutes they will be kicked. If set to 0, players can idle indefinitely.', 'any non-negative integer.'),
    'max-threads': ('8', 'Maximum number of threads the server will try to use. If set to 0 or removed, it will use as many as possible.', 'Non-negative integer (0 uses available threads).'),
    'level-name': ('Bedrock level', 'The level of the name to be used/generated. Each level has its own folder in worlds/.', 'any string that is valid for a filename on the host OS.'),
    'level-seed': ('', 'Seed used to create a new world. Empty generates a random seed; does not reseed an existing save.', 'any string'),
    'default-player-permission-level': ('member', 'Permission level for new players joining for the first time.', 'visitor, member, or operator.'),
    'texturepack-required': ('false', 'If the world uses specific texture packs, setting this to true forces the clients to use them.', 'true or false.'),
    'content-log-file-enabled': ('false', 'Enables logging content errors to a file.', 'true or false.'),
    'compression-threshold': ('1', 'Determines the smallest size of raw network payload to compress. This can be used to experiment with CPU-bandwidth tradeoffs.', 'integers in the range 0—65535.'),
    'compression-algorithm': ('zlib', 'Determines the compression algorithm to use for networking.', 'zlib or snappy.'),
    'server-authoritative-movement-strict': ('false', "If set to true, the server will track the player position more strictly and be less permissive in accepting client information. This will impact the player's movement if there is high latency.", 'true or false.'),
    'server-authoritative-dismount-strict': ('false', 'If set to true, the server will be more strict with regard to player dismount positions, and clients will receive a correction on their dismount positions in higher latency situations.', 'true or false.'),
    'server-authoritative-entity-interactions-strict': ('false', 'If set to true, clients will be more strict towards entity interactions. This will impact players interacting with each other in higher latency situations.', 'true or false.'),
    'player-position-acceptance-threshold': ('0.5', 'This is the tolerance of discrepancies between the client and server player position. This helps prevent sending corrections too frequently for non-cheating players in cases where the server and client have different perceptions about when a motion started, for example, damage knockback or being pushed by pistons. The higher the number, the more tolerant the server will be before asking for a correction. Values beyond 1.0 increase chances of allowing cheating.', 'any positive float.'),
    'player-movement-action-direction-threshold': ('0.85', "The amount that the player's attack direction and look direction can differ. This can be a value between 0.0 and 1.0. A value of 1.0 means the direction of the player's view and the direction the player is attacking must match exactly, while a value of 0.0 means the two directions can differ by up to 90 degrees.", 'any float between 0.0 and 1.0 inclusive.'),
    'server-authoritative-block-breaking': ('false', 'When set to true, the server will compute block mining operations in sync with the client, so it can verify the client should be able to break blocks when it thinks it can.', 'true or false.'),
    'server-authoritative-block-breaking-range-scalar': ('1.5', 'This increases the range of block breaking. The value is squared and multiplied by the default range.', 'any float above 1.0.'),
    'chat-restriction': ('None', 'This represents the level of restriction applied to the chat for each player that joins the server.', 'None: regular free chat. Dropped: chat messages are dropped and never sent to any client. Players receive a message to let them know the feature is disabled. Disabled: unless the player is an operator, the chat UI does not even appear. No information is sent to the player.'),
    'disable-player-interaction': ('false', 'If true, the server will inform clients that they should ignore other players when interacting with the world. This is not server authoritative.', 'true or false.'),
    'client-side-chunk-generation-enabled': ('true', 'If true, the server will inform clients that they have the ability to generate visual level chunks outside of player interaction distances.', 'true or false.'),
    'block-network-ids-are-hashes': ('true', "If true, the server will send hashed block network IDs instead of consecutive IDs starting at 0. These IDs are stable and won't change regardless of other block changes.", 'true or false.'),
    'disable-custom-skins': ('false', 'If true, disable players customized skins that were customized outside of the Minecraft store assets or in game assets. This is used to disable possibly offensive custom skins players make.', 'true or false.'),
    'server-build-radius-ratio': ('Disabled', 'If "Disabled" the server will dynamically calculate how much of the player\'s view it will generate, assigning the rest to the client to build. Otherwise, the ratio tells the server how much to generate (0.0 being none and 1.0 being all), disregarding client hardware capability. This is only used when client-side-chunk-generation-enabled is true.', 'Disabled or any float value in the range 0.0–1.0.'),
    'allow-outbound-script-debugging': ('false', 'Enables the script debugger connect command and the connect mode for script-debugger-auto-attach.', 'true or false.'),
    'allow-inbound-script-debugging': ('false', 'Enables the script debugger listen command and the listen mode for script-debugger-auto-attach.', 'true or false.'),
    'force-inbound-debug-port': ('19144', 'Locks the inbound (listen) debugger port to a specific value. If not set, the default of 19144 will be used. Required when using listen mode for script-debugger-auto-attach.', 'integers in the range of 1–65535.'),
    'script-debugger-auto-attach': ('disabled', 'Automatically attach the script debugger on level load. This requires that either the inbound port or the connect address is set, and that either inbound or outbound connections are enabled.', 'disabled: Do not automatically attach. connect: Attempt to connect to the debugger in listening mode on the host and port specified by script-debugger-auto-attach-connect-address. listen: Listen for inbound connect attempts from the debugger using connect mode on the port specified by force-inbound-debug-port.'),
    'script-debugger-auto-attach-connect-address': ('localhost:19144', "When auto attach mode is set to 'connect', use this address in the form hostname:port. Required when using connect mode for script-debugger-auto-attach.", 'string specifying hostname and port in hostname:port format.'),
    'script-debugger-auto-attach-timeout': ('0', 'Amount of time to wait at world load for debugger to attach in seconds.', 'non-negative integers.'),
    'script-debugger-passcode': ('', 'If this is set, Visual Studio Code will prompt the user for the specified passcode in order to connect to the debugger.', 'string.'),
    'script-watchdog-enable': ('true', 'Enables the watchdog.', 'true or false.'),
    'script-watchdog-enable-exception-handling': ('true', 'Enables watchdog exception handling via the events.beforeWatchdogTerminate event.', 'true or false.'),
    'script-watchdog-enable-shutdown': ('true', 'Enables server shutdown in the case of an unhandled watchdog exception.', 'true or false.'),
    'script-watchdog-hang-exception': ('true', 'Throws a critical exception when a hang occurs, interrupting script execution.', 'true or false.'),
    'script-watchdog-hang-threshold': ('10000', 'Maximum duration of a single script tick hang, in milliseconds.', 'Non-negative integer (milliseconds).'),
    'script-watchdog-spike-threshold': ('100', 'Sets the watchdog threshold for single tick spikes. The warning is disabled if the property is left unset.', 'positive integers.'),
    'script-watchdog-slow-threshold': ('10', 'Sets the watchdog threshold for slow scripts over multiple ticks. The warning is disabled if the property is left unset.', 'positive integers.'),
    'script-watchdog-memory-warning': ('100', 'Produces a content log warning when the combined memory usage exceeds the given threshold in megabytes. Setting this value to 0 disables the warning.', 'integers in the range of 0—2000.'),
    'script-watchdog-memory-limit': ('250', 'Saves and shuts down the world when the combined script memory usage exceeds the given threshold in megabytes. Setting this value to 0 disables the limit.', 'integers in the range of 0—2000.'),
    'diagnostics-capture-auto-start': ('false', 'Starts a diagnostics capture session at level load.', 'true or false.'),
    'diagnostics-capture-max-files': ('5', 'Maximum number of diagnostics capture files to keep before cycling.', 'non-negative integers.'),
    'diagnostics-capture-max-file-size': ('2097152', 'Maximum size in bytes of current diagnostics capture file before system will cycle to a new file.', 'positive integers.'),
    'disable-client-vibrant-visuals': ('true', 'If true, the server will tell clients to use the next best available graphics setting instead of Vibrant Visuals.', 'true or false.'),
    'content-log-console-output-enabled': ('true', 'Write content log messages to the server console. Terminal uses this for in-world status messages.', 'true or false.'),
}

# Exact root tag spellings from CraftMaster's shipped Bedrock template, plus
# the documented Bedrock pvp gamerule (absent from that template). No
# arbitrary NBT editing: only user-facing settings, never internal world IDs,
# protocol/version markers, player data, seeds or generation metadata.
WORLD_PROPERTIES = {
    'educationFeaturesEnabled': (1, 'Enable Bedrock-compatible Education features, including chemistry; camera/portfolio support depends on the client.'),
    'eduOffer': (3, 'Education world marker: 0 = ordinary Bedrock world, 1 = Education world (may prevent ordinary Bedrock clients from opening/joining).'),
    'immutableWorld': (1, 'Prevent ordinary players from modifying the world.'),
    'commandblocksenabled': (1, 'Enable command blocks.'),
    'commandblockoutput': (1, 'Show command block output.'),
    'dodaylightcycle': (1, 'Advance the day/night cycle.'),
    'doentitydrops': (1, 'Allow non-mob entities to drop items.'),
    'dofiretick': (1, 'Allow fire to spread and extinguish naturally.'),
    'doimmediaterespawn': (1, 'Respawn without the death screen.'),
    'doinsomnia': (1, 'Allow phantoms to spawn.'),
    'dolimitedcrafting': (1, 'Restrict crafting to unlocked recipes.'),
    'domobloot': (1, 'Allow mobs to drop loot.'),
    'domobspawning': (1, 'Allow natural mob spawning.'),
    'dotiledrops': (1, 'Allow blocks to drop items.'),
    'doweathercycle': (1, 'Allow weather to change.'),
    'drowningdamage': (1, 'Enable drowning damage.'),
    'falldamage': (1, 'Enable fall damage.'),
    'firedamage': (1, 'Enable fire damage.'),
    'freezedamage': (1, 'Enable freezing damage.'),
    'keepinventory': (1, 'Keep inventory on death.'),
    'mobgriefing': (1, 'Allow mobs to change blocks.'),
    'naturalregeneration': (1, 'Allow natural health regeneration.'),
    'projectilescanbreakblocks': (1, 'Allow projectiles to break eligible blocks.'),
    'respawnblocksexplode': (1, 'Allow beds and respawn anchors to explode in incompatible dimensions.'),
    'sendcommandfeedback': (1, 'Show command feedback.'),
    'showcoordinates': (1, 'Display coordinates.'),
    'showdaysplayed': (1, 'Display days played.'),
    'showdeathmessages': (1, 'Display death messages.'),
    'showrecipemessages': (1, 'Display recipe unlock messages.'),
    'showtags': (1, 'Display entity name tags.'),
    'tntexplodes': (1, 'Allow TNT explosions.'),
    'tntexplosiondropdecay': (1, 'Allow TNT explosions to destroy some block drops.'),
    'locatorbar': (1, 'Enable the player locator bar.'),
    'recipesunlock': (1, 'Enable recipe unlocking.'),
    'pvp': (1, 'Allow players to damage each other.'),
    'randomtickspeed': (3, 'Random block ticks per chunk section per game tick.'),
    'spawnradius': (3, 'Spawn radius in blocks.'),
    'playerssleepingpercentage': (3, 'Percentage of players required to skip the night.'),
    'functioncommandlimit': (3, 'Maximum commands executed by a function.'),
    'maxcommandchainlength': (3, 'Maximum length of a command chain.'),
}

_ASSIGNMENT = re.compile(r'^\s*(#\s*)?([A-Za-z][A-Za-z0-9_-]*)\s*=(.*)$')


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError('Refusing linked Bedrock settings: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'-', delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _read_text(path: Path) -> str:
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError('Refusing linked Bedrock settings: ' + str(path))
    return path.read_text('utf-8-sig') if path.is_file() else ''


def _assignments(text: str, *, commented: bool = False) -> dict:
    result = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.fullmatch(line)
        if match and (commented or not match[1]):
            key, value = match[2].casefold(), match[3].strip()
            if not match[1] or key not in result:
                result[key] = value
    return result


def _package_metadata(text: str) -> dict:
    result, comments = {}, []
    for line in text.splitlines():
        match = _ASSIGNMENT.fullmatch(line)
        if match:
            key = match[2].casefold()
            result[key] = (match[3].strip(), ' '.join(comments))
            comments = []
        elif line.lstrip().startswith('#'):
            comments.append(line.lstrip()[1:].strip())
        else:
            comments = []
    return result


def _merge_text(text: str, updates: dict) -> str:
    lines, seen = [], set()
    for line in text.splitlines():
        match = _ASSIGNMENT.fullmatch(line)
        key = match[2].casefold() if match and not match[1] else None
        if key in updates:
            if key not in seen:
                lines.append(key+'='+updates[key]); seen.add(key)
        else:
            lines.append(line)
    lines.extend(key+'='+value for key,value in updates.items() if key not in seen)
    return '\n'.join(lines)+'\n'


def _validate_server(key: str, value: str, allowed: str) -> str:
    # Preserve free-form strings and future native properties. Enforce known
    # boolean/enumeration/numeric constraints rather than guessing new ones.
    if allowed.casefold().rstrip('.') == 'true or false':
        if value.casefold() not in ('true','false'):
            raise ValueError(key+' requires true or false')
        return value.casefold()
    enums = {
        'gamemode': ('survival','creative','adventure'),
        'difficulty': ('peaceful','easy','normal','hard'),
        'default-player-permission-level': ('visitor','member','operator'),
        'compression-algorithm': ('zlib','snappy'),
        'chat-restriction': ('None','Dropped','Disabled'),
        'script-debugger-auto-attach': ('disabled','connect','listen'),
    }
    if key in enums:
        match = next((x for x in enums[key] if x.casefold() == value.casefold()), None)
        if match is None: raise ValueError(key+' requires '+', '.join(enums[key]))
        return match
    integer_ranges = {
        'server-port': (1,65535), 'server-portv6': (1,65535),
        'force-inbound-debug-port': (1,65535), 'compression-threshold': (0,65535),
        'tick-distance': (4,12), 'view-distance': (5,None), 'max-players': (1,None),
        'player-idle-timeout': (0,None), 'max-threads': (0,None),
        'script-debugger-auto-attach-timeout': (0,None),
        'script-watchdog-hang-threshold': (0,None), 'script-watchdog-spike-threshold': (1,None),
        'script-watchdog-slow-threshold': (1,None), 'script-watchdog-memory-warning': (0,2000),
        'script-watchdog-memory-limit': (0,2000), 'diagnostics-capture-max-files': (0,None),
        'diagnostics-capture-max-file-size': (1,None),
    }
    if key in integer_ranges:
        low, high = integer_ranges[key]
        if not re.fullmatch(r'[0-9]+', value): raise ValueError(key+' requires an integer')
        n = int(value)
        if n < low or high is not None and n > high: raise ValueError(key+': '+allowed)
        return str(n)
    floats = ('player-position-acceptance-threshold','player-movement-action-direction-threshold',
              'server-authoritative-block-breaking-range-scalar','server-build-radius-ratio')
    if key in floats:
        if key == 'server-build-radius-ratio' and value.casefold() == 'disabled': return 'Disabled'
        try: n = float(value)
        except ValueError: raise ValueError(key+': '+allowed) from None
        if (not math.isfinite(n) or n < 0 or
            key == 'player-position-acceptance-threshold' and n <= 0 or
            key == 'server-authoritative-block-breaking-range-scalar' and n <= 1 or
            key in ('player-movement-action-direction-threshold','server-build-radius-ratio') and n > 1):
            raise ValueError(key+': '+allowed)
    if key == 'server-name' and ';' in value: raise ValueError('server-name cannot contain semicolons')
    if key == 'script-debugger-auto-attach-connect-address':
        host, sep, port = value.rpartition(':')
        if not host or not sep or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError(key+' requires hostname:port')
    return value


def _world_value(key: str, value: str) -> str:
    value = value.casefold()
    if value == 'inherit': return value
    if WORLD_PROPERTIES[key][0] == 1:
        if value not in ('true','false'): raise ValueError(key+' requires true, false or inherit')
        return value
    if not re.fullmatch('[0-9]+',value): raise ValueError(key+' requires a non-negative integer or inherit')
    n = int(value)
    high = 1 if key == 'eduOffer' else 100 if key == 'playerssleepingpercentage' else 2147483647
    if n > high: raise ValueError(key+f' requires 0–{high} or inherit')
    return str(n)


def _level_tags(data: bytes) -> tuple[dict, int]:
    """Validate the complete LE NBT document; locate only root-level tags.

    Unlike byte-pattern searching, nested fields with identical names cannot
    be mistaken for top-level gamerules. All unrelated bytes are preserved.
    """
    end = len(data)
    if end < 12 or struct.unpack_from('<I', data, 4)[0] != end-8:
        raise ValueError('Invalid Bedrock level.dat header or payload length')
    def need(pos,n):
        if n < 0 or pos < 8 or pos+n > end: raise ValueError('Truncated or invalid Bedrock NBT')
        return pos+n
    def string(pos):
        need(pos,2); n=struct.unpack_from('<H',data,pos)[0]; pos+=2
        last=need(pos,n); return data[pos:last],last
    def payload(tag,pos,depth):
        if depth>64: raise ValueError('Bedrock NBT nesting limit exceeded')
        sizes={1:1,2:2,3:4,4:8,5:4,6:8}
        if tag in sizes: return need(pos,sizes[tag])
        if tag==8: return string(pos)[1]
        if tag in (7,11,12):
            need(pos,4); n=struct.unpack_from('<i',data,pos)[0]
            return need(pos+4,n*{7:1,11:4,12:8}[tag])
        if tag==9:
            need(pos,5); child=data[pos]; n=struct.unpack_from('<i',data,pos+1)[0]; pos+=5
            if n<0 or n>end-pos or (child==0 and n): raise ValueError('Invalid Bedrock NBT list')
            if child in sizes: return need(pos,n*sizes[child])
            for _ in range(n): pos=payload(child,pos,depth+1)
            return pos
        if tag==10:
            while True:
                need(pos,1); child=data[pos]; pos+=1
                if child==0: return pos
                _,pos=string(pos); pos=payload(child,pos,depth+1)
        raise ValueError('Unknown Bedrock NBT tag '+str(tag))
    if data[8]!=10: raise ValueError('Bedrock level.dat root is not a compound')
    _,pos=string(9); tags={}
    while True:
        need(pos,1); tag=data[pos]; pos+=1
        if tag==0: break
        name,pos=string(pos); last=payload(tag,pos,0)
        if name in tags: raise ValueError('Duplicate Bedrock root NBT tag')
        tags[name]=(tag,pos,last); pos=last
    if pos!=end: raise ValueError('Unexpected bytes after Bedrock root compound')
    return tags,pos-1


def read_world_settings(path: Path) -> dict:
    if path.is_symlink() or path.parent.is_symlink(): raise RuntimeError('Refusing linked level.dat')
    if path.stat().st_size>16*1024*1024: raise ValueError('Bedrock level.dat exceeds settings size limit')
    data=path.read_bytes(); tags,_=_level_tags(data); result={}
    for key,(kind,_) in WORLD_PROPERTIES.items():
        entry=tags.get(key.encode())
        if entry and entry[0]==kind:
            n=struct.unpack_from('<b' if kind==1 else '<i',data,entry[1])[0]
            result[key]=('true' if n else 'false') if kind==1 else str(n)
    return result


def apply_world_settings(path: Path, overrides: dict) -> None:
    if not overrides: return
    if path.is_symlink() or path.parent.is_symlink(): raise RuntimeError('Refusing linked level.dat')
    if path.stat().st_size>16*1024*1024: raise ValueError('Bedrock level.dat exceeds settings size limit')
    original=path.read_bytes(); tags,terminal=_level_tags(original)
    data=bytearray(original); added=bytearray()
    for key,value in overrides.items():
        kind=WORLD_PROPERTIES[key][0]; value=_world_value(key,value)
        if value=='inherit': continue
        name=key.encode(); raw=struct.pack('<b' if kind==1 else '<i',int(value=='true') if kind==1 else int(value))
        entry=tags.get(name)
        if entry:
            if entry[0]!=kind: raise ValueError('Unexpected NBT type for '+key+'; world unchanged')
            data[entry[1]:entry[2]]=raw
        else:
            added+=bytes([kind])+struct.pack('<H',len(name))+name+raw
    data[terminal:terminal]=added
    struct.pack_into('<I',data,4,len(data)-8)
    _level_tags(bytes(data))
    if data!=original: _atomic_write(path,bytes(data))


class BedrockProperties:
    def __init__(self, root: Path, run: Path, package: Path):
        self.path=root/'ServerProperties.json'
        self.run=run/'server.properties'
        self.package=package/'server.properties'

    def _policy(self) -> dict:
        raw=_read_text(self.path)
        if not raw: return {'server':{},'world':{}}
        policy=json.loads(raw)
        if (not isinstance(policy,dict) or policy.get('schema')!='cinterforge.bedrock.properties.v1' or
            any(not isinstance(policy.get(scope),dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in policy[scope].items()) for scope in ('server','world'))):
            raise ValueError('Invalid Bedrock ServerProperties.json')
        for key,value in policy['server'].items():
            if (not re.fullmatch(r'[a-z][a-z0-9_-]*',key) or key=='level-name' or len(value)>4096 or
                    any(ord(c)<32 or ord(c)==127 for c in value)):
                raise ValueError('Invalid persisted Bedrock server property')
        for key,value in policy['world'].items():
            if key not in WORLD_PROPERTIES:
                raise ValueError('Unknown persisted Bedrock world property: '+key)
            _world_value(key,value)
        return policy

    def _catalog(self) -> dict:
        meta=_package_metadata(_read_text(self.package)); native=_assignments(_read_text(self.run),commented=True)
        catalog=dict(SERVER_PROPERTIES)
        for key,(default,desc) in meta.items():
            if key in catalog:
                _,description,allowed=catalog[key]; catalog[key]=(default,description,allowed)
            else: catalog[key]=(default,desc or 'Native installed BDS property.', 'See the installed BDS property comments; string value accepted.')
        for key in (*native, *self._policy()['server']):
            if key not in catalog: catalog[key]=('', 'Existing native server property.', 'Value interpreted by the installed BDS version.')
        return catalog

    def snapshot(self, *, selected: str = '', world_values: dict | None = None) -> list[dict]:
        policy=self._policy(); catalog=self._catalog(); native=_assignments(_read_text(self.run))
        rows=[]
        for key,(default,description,allowed) in sorted(catalog.items()):
            value=policy['server'].get(key,native.get(key,default))
            if key=='level-name':
                value=selected or native.get(key,default)
                description+=' In Terminal this selects an existing world; the native directory is managed automatically.'
                allowed='Existing Java/Bedrock world name or ID.'
            rows.append({'name':key,'value':value,'default':default,'description':description,'allowed':allowed,'scope':'server.properties',
                         'native_value':native.get(key),'overridden':key in policy['server']})
        for key,(kind,description) in sorted(WORLD_PROPERTIES.items(),key=lambda x:x[0].casefold()):
            override=policy['world'].get(key,'inherit'); actual=(world_values or {}).get(key)
            allowed='true, false, inherit' if kind==1 else ('0, 1, inherit' if key=='eduOffer' else '0–100, inherit' if key=='playerssleepingpercentage' else '0–2147483647, inherit')
            rows.append({'name':key,'value':override if override!='inherit' else (actual if actual is not None else 'inherit'),
                         'default':'inherit','description':description,'allowed':allowed,'scope':'level.dat','native_value':actual,'overridden':override!='inherit'})
        return rows

    def set(self, name: str, value: str) -> dict:
        if not isinstance(name,str) or not isinstance(value,str) or len(value)>4096 or any(ord(c)<32 or ord(c)==127 for c in value):
            raise ValueError('Expected one property name and a single-line value')
        key=name.casefold(); catalog=self._catalog(); policy=self._policy()
        world=next((k for k in WORLD_PROPERTIES if k.casefold()==key),None)
        if world:
            scope='world'; key=world; value=_world_value(key,value)
            if value=='inherit': policy[scope].pop(key,None)
            else: policy[scope][key]=value
        else:
            if key not in catalog: raise ValueError('Unknown Bedrock property: '+name+'; use bedrock properties list')
            if key=='level-name': raise ValueError('level-name is the selected world; use the world selection interface')
            scope='server'; value=_validate_server(key,value,catalog[key][2]); policy[scope][key]=value
        _atomic_write(self.path,(json.dumps({'schema':'cinterforge.bedrock.properties.v1',**policy},indent=2)+'\n').encode())
        return {'name':key,'value':value,'scope':scope,'applies':'next server start'}

    def apply_server(self, *, level_name: str | None = None) -> None:
        if not self.run.is_file(): raise RuntimeError('Bedrock server.properties is missing')
        text=_read_text(self.run); current=_assignments(text)
        # Merge actual package defaults for newly introduced settings. Retain
        # local native edits and optional commented defaults verbatim.
        updates={k:v for k,v in _assignments(_read_text(self.package)).items() if k not in current}
        # Preserve the status bridge's historical first-run default, but
        # never overwrite an actual native setting or an explicit override.
        if 'content-log-console-output-enabled' not in current:
            updates.setdefault('content-log-console-output-enabled','true')
        updates.update(self._policy()['server'])
        if level_name is not None: updates['level-name']=level_name
        if updates:
            updated=_merge_text(text,updates)
            if updated!=text: _atomic_write(self.run,updated.encode('utf-8'))

    def apply_world(self, level_dat: Path) -> None:
        apply_world_settings(level_dat,self._policy()['world'])
