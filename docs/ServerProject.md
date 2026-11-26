# CinterForge Java / Bedrock Terminal project — Terminal integration drop 02 — server-only lifecycle and native Bedrock commands

## Scope and owners

CraftMaster is the single owner of Java world generation, native Java save
verification, conversion, PFF map/Atlas, and Minecraft server infrastructure.
Its standalone development acceptance is **macOS only**. Terminal owns
cross-platform orchestration/provisioning, installed UI/session management,
the existing communications behavior pack, and the CinterForge Terminal
experience in Minecraft. Terminal's retained Linux x64 and Windows x64
(Wine) environments are used for foreign executable acceptance; no second VM,
new lifecycle phase, second behavior pack, or replacement map engine.

This incremental drop requires the already delivered Java Platform Drop 01 and
current CraftMaster source (including stable beta TestGrounds publication).
The shared CraftMaster publication installer also supports exact-match adoption of the publisher’s existing beta TestGrounds save. No prior experimental Bedrock
display ZIP is required. No standalone `bedrock-display` lifecycle commands.

## Cumulative acceptance status

| Milestone | Evidence / status |
|---|---|
| CraftMaster Mac Setup and EULA | User ran previous version successfully |
| Mac Java generate / host / convert / PFF | Native TestGrounds 3 passed on user Mac; conversion spawn/Survival parity remains outstanding |
| Mac TestGrounds corrected acceptance and beta publication | Native Test passed; publication HTTP 403/receipt recovery pending live verification |
| Terminal staged CraftMaster controller on all three desktop platforms | Source integration implemented, staged rebuild pending |
| Native Java runtime acquisition on Mac/Linux/Windows | Mac JDK pins reused; foreign archives use exact Temurin release and official release asset SHA-256; live foreign acquisition/execution unverified |
| Java Session, install/generate/list/select/host/stop | Implemented, native interactive acceptance pending |
| Java command parity with Bedrock | Partial: report reuses combined report; native console supports server commands; matching persistent op/allowlist, properties, datapack selection, report actions, published-world acquisition remain |
| Terminal staged server-only Java smoke | Starts an existing verified TestGrounds copy, console list and graceful shutdown; Mac and managed AMD64 Linux / Wine Windows paths implemented, native execution pending |
| Existing communication behavior pack | Updated same UUID and module, version 1.0.6, native slash commands, sized item, oak wall and chest fixture previously passed; shared installer native execution pending |
| Endstone/MediaPlayer provisioning | Pinned prebuilt Setup/Build/Stage and existing-server activation implemented; native execution pending |
| Physical screen / placeable Terminal / click interaction | Provider materialization, inventory acknowledgement, persistent screen identity, camera-focused command form implemented; native client acceptance pending |
| Actual framebuffer, prompt, speech, input, IF, catalog, graphical views and map navigation | Live text rendering and host command output implemented; speech, in-world IF/game/Atlas presentation pending |
| Combined actual Bedrock in-world smoke and publication | Pending |

## Current Java workflow

Normal Terminal repository commands: `./Cinter setup all`, `./Cinter build all`,
`./Cinter stage all`, `./Cinter test all` (or the existing scoped platform
commands). Setup acquires verified provider inputs, including foreign native
JDKs, after existing CraftMaster Java EULA consent. Build/Stage ship the
controller/data closure, not Mojang server binaries. Installed `java install`
opens a native session, explicitly accepts EULA when needed, verifies/downloads
the native redistributable JDK and official pinned Java server from upstream.
It writes only under managed Steam server state, not application resources or
an installed user's `Builds`. It does not download on app startup. Developer
JDK archives are verified during Setup, not fetched by Test. The installed app
JDK is acquired on demand rather than prebundled; existing native Java and
Minecraft are not overwritten. Each `java run`, `java start`, named-world
launch and `java generate` invokes the same dependency compatibility gate
as `java install`. The required versions are the packaged CraftMaster server
and JDK pins, not a floating latest release. Verified dependencies are reused;
missing or incompatible dependencies are acquired/repaired before launch, with
progress in the Java Session and its persistent console log. A failed update
blocks launch. Existing worlds, server configuration, EULA consent and other
Java installations are retained; an active Java Session cannot be updated.
Setup provisions dependencies for all selected targets; offline Test uses the
same compatibility gate without downloading. The shared Mac Setup JDK remains
in its CraftMaster toolchain location. Mac, Linux and Windows use native Java, not a VM
in the installed application.

- `java` lists local native Java saves and installation/current selection state.
- `java install` provisions native runtime after explicit consent.
- `java generate "My World" --size 1024 --seed 12345` creates a new save directly
  under that platform's real Java saves directory; existing saves are refused.
- `java "My World"` opens a dedicated interactive native Java server Session.
- `java world "My World"` selects; `java start` and `java run` serve selection.
- `java status`, `java stop`, `java report`, `java help`.
- Native server console supports the normal Java server commands. These are not
  equivalent to the remaining Terminal-specific Bedrock management commands.

No existing Bedrock public command is renamed. The native CraftMaster
`generate`/`serve` C++ dispatcher remains Mac-only; Terminal invokes the shared
controller with its native Python, verified native Java and native CraftMaster
CLI for saved-spawn verification on foreign targets. This is intentional until
native C++ process-launch portability is validated.

## Server-only Terminal lifecycle

CraftMaster owns generation, conversion, spawn and PFF acceptance. Terminal
never repeats these operations as lifecycle tests. Setup acquires verified
server inputs only; Build/Stage integrate and install; Test starts servers.
Ordinary Setup no longer installs every published world. The explicit
`./Cinter setup worlds [internal|beta|release]` remains unchanged.
`./Cinter setup testgrounds` runs only the same beta fixture acquisition and
native installation as full Setup; it does not start VMs or provision tools.
Use it after applying this source update if the existing fixture lacks the
native install binding. An already installed, matching beta publication can
upgrade the old snapshot receipt without downloading the package again. If
the native save is absent, Setup downloads and installs the verified package.
If the publisher’s original numbered Java save already exists at the catalog
folder, the shared installer adopts it only after comparing its Java and all
published artifact hashes to the verified package. It writes the two verified
publication receipts, without replacing the save or gameplay. A mismatch is
reported and the existing save is preserved. The stable public name is not
the native folder or conversion export name; numbered exports remain intact.
Test checks the binding before the native suite and remains offline.

Setup prepares one reusable, verified TestGrounds snapshot at
`Builds/Acceptance/CinterForgeTerminal/TestGrounds` (Java save files,
`TestGrounds.mcworld`, and `fixture.json`). It reuses the unchanged snapshot
on subsequent Setup runs when the published beta catalog digest is unchanged.
It downloads only `CinterForge TestGrounds` from the public beta catalog and
uses CraftMaster's package verifier/extractor in a private Builds workspace.
The same verified publication is installed in the normal native Java saves
folder through the existing CraftMaster installer; no duplicate named save or
developer-only launch path. No local-world fallback. The beta package must contain its verified converted
MCWorld. The original user saves and published source remain untouched. No
terrain generation is requested. Missing beta publication or HTTP 403 is a
Setup blocker, not permission to substitute another world.


Java Test hosts a disposable copy using the staged controller and Python,
verified JDK and official JAR, with `serve --check` only. It verifies server
readiness, console `list` and clean shutdown. No spawn or map validation,
conversion, or explicit chunk pregeneration. Mac Bedrock acceptance invokes
`prepare_testgrounds` in the verified staged runtime, using the Setup-installed
beta Java save and its verified MCWorld. It installs the official server from
Setup's existing cached ZIP only if not already installed, after explicit EULA
consent; Test makes no download. The ordinary managed save and installed server
are used directly. There is no disposable or PreparedBedrock world.

`bedrock testgrounds` is a case-insensitive special command: resolve the beta
`CinterForge TestGrounds` publication, carry the resolved Java save ID into
launch (never reselect it by display/export/folder name), verify its publication
receipts and reuse its published Bedrock conversion (never generate or
reconvert it), import it through the normal
managed-world path, install the existing communications pack, and prepare the
wall and chest via native BDS. A finite initial preparation boot loads the
clearing, runs the console-only event, verifies the real fixture, removes the
temporary ticking area and saves via clean shutdown. The visible Bedrock tab
then hosts that exact save using the normal settings. The temporary preparation
policy is restored after shutdown, with a recovery record for interrupted runs.
Both the preparation boot and native acceptance use `online-mode=true` and
`allow-list=false`, with LAN discovery disabled. Existing allowlist entries are
preserved; enforcement is disabled during preparation. Do not disable
authentication for offline acceptance: no player login is required, and Test
uses the already acquired server archive. The normal server defaults are also
`online-mode=true` and `allow-list=false`; explicit persistent user property
settings are retained for normal hosting.
A per-conversion receipt
in the managed world, plus the existing in-world marker, prevents replacing a
played chest or regenerating materials on subsequent launches. A different
published conversion follows the ordinary publication-update path. The full
name `bedrock "CinterForge TestGrounds"` and all other named worlds retain
ordinary hosting semantics; they do not request the special preparation.

Test uses the same runtime installer and verifies native Rosetta/NetherNet,
console list, native command readiness and graceful shutdown; it does not reset
player edits when the fixture was previously installed. The Java publication is
unchanged. The Bedrock acceptance receipt records source provenance, the MCWorld
SHA-256, active managed world path and native fixture evidence. Java logs and
receipts remain under `Builds/Logs/CinterForgeTerminal/JavaAcceptance`.


The existing Linux and Windows Java checks use the managed AMD64 Linux/Wine
execution environment; they are not native Windows GUI certification. On Apple
Silicon only, both foreign Java acceptance processes use
`JAVA_TOOL_OPTIONS=-XX:TieredStopAtLevel=1` to limit HotSpot compilation to C1.
The November 25 lifecycle log shows Linux Java acceptance passed with this
setting, but Windows HotSpot crashed in `jvm.dll` and emitted compiler replay
data without it. Windows now uses the same acceptance-only setting before
launching Wine; native and installed JVMs are unchanged. This is a targeted
workaround, not a confirmed diagnosis or a verified Windows pass. The existing
`./Cinter test win` gate must still observe real server readiness, console
`list` and clean shutdown before acceptance succeeds. This
drop does not add Linux/Windows native Bedrock acceptance or claim client login
on any target. Ordinary Minecraft startup may load/generate missing chunks as
part of normal server operation; Terminal does not request terrain generation.
The real installed-app server/PTY lifetime and in-game Terminal acceptance
remain separate pending gates. No native runtime was available here.

The November 25 retry passed Mac and Linux Test. Windows picked up the C1
setting and reached Minecraft server initialization, then CraftMaster's
Python stdout reader failed on Unicode under CP1252. The owning controller
now uses UTF-8 for redirected stdout and reports reader failures to the
waiter. Rebuild and stage Terminal to package that verified CraftMaster
resource; the existing Windows readiness, list and shutdown gate is unchanged
and remains pending. No world or asset publication is required for this fix.

## In-world Terminal: current fixture and next steps

The existing communications pack retains its UUID and console transport. Native
registered commands (Script API 2.1.0) are:

- `/cinter:terminal [width] [height]`: operator gives themselves exactly one
  `cinter:terminal` item; default 5x4, range 1–7 by 1–4 whole 128-pixel tiles.
  Size lives in item dynamic properties and is shown in the item lore. The
  current icon uses vanilla paper as a placeholder.
- `/cinter:play <game>` and `/cinter:craftmaster <world>`: operator commands,
  one quoted name, validated structured envelope on the existing console bridge,
  then quoted existing host Bash dispatcher. No new host listener.
- `/cinter:help`: native command help.

The shared TestGrounds installer uses `scriptevent cinter:terminal_smoke TestGrounds` only from the
server console. The pack derives coordinates from the converted world's spawn,
loads chunks at that spawn, reads the actual topmost terrain (not the saved Y),
locates a clear grass site within four blocks of spawn, refuses to overwrite existing blocks, builds a 5x4 backing and one-block recessed oak trim (7x6 outer rectangle),
places a chest beside the wall, and uses the same item factory as
`/cinter:terminal` to put exactly one 5x4 Terminal in slot zero. It reads back
all wall blocks and the chest inventory (type, amount, dimensions, empty slots)
before emitting `CINTERFORGE_TERMINAL_WALL_READY`. The actual item is left in the
chest, not pre-placed. No live display is installed. Placement currently
refuses without consuming the item. Native display, actual placement, click-to-
enter camera mode, text input, speech and live game/map rendering remain pending.
The existing legacy nonreserved `cinter:*` script event relay is unchanged.

Complete in order:

1. Get corrected Mac CraftMaster native acceptance and publish the exact
   `CinterForge TestGrounds` receipt with PFF Output first. Do not delete existing
   worlds; retain the stable smoke publication name. Resolve the existing
   converter's Creative/relocated Bedrock spawn before claiming survival parity.
2. Run normal Terminal platform Test gates with real native JDKs and exact
   staged scripts; correct any Java runtime/PTY/session shutdown or portability
   failures. Complete the remaining Java management command parity and UI.
3. Pin compatible Endstone and MediaPlayer upstream releases in their owning
   server lifecycle, stage/activate the unchanged providers with the existing
   Bedrock installation. Verify real BDS loads both and successfully commits
   pixels, on all supported server hosts. No previously rejected display
   prototype should be required to apply this drop. Provider currently exposes
   pixel updates for a logical screen; actual map-frame materialization needs
   its in-world placement workflow. Resolve programmatic placement and test
   it on an unmodified client before claiming a physical Terminal.
4. Use the exact converted Mac smoke artifact (SHA-256 checked receipt) as the
   Bedrock Terminal test input, not a duplicate world generator. Construct and inspect the wall and chest at the converted spawn; the player takes
   the Terminal from the chest and places it; materialize the real map tiles
   on its backing. Verify the live real startup screen including
   prompt, not a bitmap of a proposed UI. Keep the exact greeting and speech
   behavior. Persist the placed Terminal identity in the same communications
   pack.
5. Implement click-to-enter camera-aligned 2D mode with player movement
   restored on Back/disconnect/error. Only explicit text submissions, no
   arbitrary host Bash access, no client modifications. Route to a real
   isolated CinterForge application command session, not the existing raw host
   PTY command relay. Use real output to render the screen.
6. Reuse the existing games catalog and IF engine, including actual playable
   stories; graphical game sessions remain host-controlled/view-only from
   Minecraft. No invented catalog or graphical control. Atlas generation or
   update uses existing CraftMaster and PFF output, with simple key navigation
   and Back to Terminal, one view at a time. Verify screen changes live.
7. Combined acceptance in normal `test` stage, no new lifecycle subcommands:
   host the existing verified Java and converted Bedrock fixtures, unmodified
   client observation, real text input and IF, live game/map view, graceful cleanup.
   Generation, conversion and mapping acceptance remain exclusively in CraftMaster.
   Verify actual installed Mac/Windows/Linux Terminal separately. Publish only
   after the acceptance gates succeed, with a complete cumulative status.

## Validation for this drop

Executed: six Terminal RepositoryContractSmoke checks; 46 existing CraftMaster
Python regression tests; syntax checks of modified JavaScript modules and the
full Terminal Python source through existing repository checks; loaded actual
CraftMaster version pins. These are offline checks, not live Java or BDS tests.
No available independent LevelDB checkout or staged apps/native JDKs in this
workspace. Foreign official release asset URLs and hashes will be verified
by actual Setup; not independently fetched here. No Windows/Linux/Mac native
server, managed guest, physical Bedrock display or CDN publication run here.

### Drop 02 validation

Six existing Terminal RepositoryContractSmoke checks passed, including updated
lifecycle boundaries; Node syntax check passed for the communications script;
Python source parsed; pack installation checked offline with the actual installer.
No native Mac, Linux/Wine Java or BDS process was executed here. The beta package
was not available in this workspace. No live display or client test completed.
No CraftMaster source changed. Native slash command, item and chest behavior
require actual BDS verification during Test. The server-only smoke is currently
implemented for Mac Bedrock (LinuxKit/Rosetta); foreign Bedrock Test remains
pending.

### November 25 lifecycle evidence

The supplied lifecycle log reports successful macOS Java hosting, signed app
UI acceptance, LinuxKit/Rosetta Bedrock startup, the communications pack and
wall/chest smoke, and prepared-world retention. Linux Java hosting returned
exit 1; the child output was saved only in its separate serve.log, which was
not supplied. Foreign Java acceptance now streams the real child output and
heartbeat as well as preserving serve.log, and includes the exit context in
the lifecycle failure. The underlying Java failure is not diagnosed until
that output is available. Windows Java acceptance was not reached. This
change does not publish a prepared world or implement physical Terminal
placement.

### Installed TestGrounds acceptance

The short `bedrock testgrounds` command prepares the normal installed
`CinterForge TestGrounds` save and then starts the visible server tab. The full
name hosts the same managed save normally. Lifecycle Test invokes the shared
installer from the staged app and stops cleanly. Close the installed Bedrock
server before running Test. Existing material receipts preserve gameplay rather
than refilling the chest or resetting the designated world. Native placement
and display remain pending. No separate modified-world publication is needed.

Spawn-relative layout revision 2 supersedes old preparation receipts. A correctly positioned existing fixture is preserved; a misplaced legacy fixture is relocated only if its exact original Terminal and wall remain intact. Otherwise preparation fails without replacing player materials. Logs include actual spawn-surface and chest coordinates.

### Bedrock diagnostics and display-provider status

Bedrock installation and raw console logs now use the canonical Terminal logs
root, under `Bedrock/bedrock-install.log` and `Bedrock/CinterForgeTerminal.log`
for the default application ID. The console path is printed when a visible
server session starts and by `bedrock log`; developer lifecycle logs use the
same `Bedrock/` subdirectory of `Builds/Logs/CinterForgeTerminal`. Raw console
bytes are written before access-policy processing and UI filtering. Existing
old log files are left in place. New server installations enable content
console logging by default; explicit existing server settings are preserved
and the CLI warns if script diagnostics are disabled.

The existing behavior pack logs `CINTERFORGE_TERMINAL_PLACEMENT_REQUEST` and
`CINTERFORGE_TERMINAL_PLACEMENT_FAILED` with dimension, block, face and item
dimensions. Placement remains cancelled and the item remains in inventory.
The provider is not yet integrated. Endstone 0.11.16 lists BDS 1.26.65 support
(upstream changelog); MediaPlayer 2.3.1 exposes logical screen/pixel operations
but its documented physical materialization is a player command, not an SDK
call. Compatibility of the two binaries and the guest runtime is unverified.
Do not confuse a logged request or logical screen with a placed live Terminal.
No new provider, alternate server, or fake display is shipped by this change.

## Display integration update

Setup acquires prebuilt Endstone 0.11.16 (CPython 3.11) and MediaPlayer 2.3.1
Linux x64 / Windows x64, including the published SDK wheel. Build/Stage embed
the verified provider closure, not BDS itself. The existing LinuxKit/Rosetta
guest loads the provider in the same BDS process. No upstream build or second
VM is required. The guest launch changed, so a successful normal Setup is
required before Build and Stage. All new Bedrock launches require the
compatible provider; the shared installer upgrades incompatible BDS packages.

The real provider must report `CINTERFORGE_DISPLAY_PROVIDER_READY` after a
logical screen pixel commit; native TestGrounds acceptance waits for it.
Placement uses the SDK logical screen and the upstream physical materialization
command from the placing server operator. No automatic OP privilege changes
are made. Click the exact bottom-left backing block as seen from the front.
The display extends right and up from that anchor; there is no search or
automatic relocation. A decorative border is not required, but MediaPlayer
requires an exposed solid backing rectangle of the exact requested width and
height, with air in each display cell and no exposed continuation to either
side or above. A larger uninterrupted wall is not supported by this provider
command. The behavior pack uses stable air/liquid checks rather than the
pre-release `Block.isSolid` property; the native provider makes the final
backing validation. Failed placement logs the exact clicked position, face,
and first invalid backing/front block. For the generated wall, click the
leftmost recessed plank immediately above the bottom trim, not the trim itself. The pack positions the operator for physical materialization; the
provider must verify physical mounting before the pack consumes the item. The pinned provider does not implement that mounting step; see the limitation below. Screen names are stored in the
world. Clicking a registered screen focuses the camera and opens the compact
Terminal command form. Enter and the visible Enter button submit the same
form action. The native form closes on submission, so the provider keeps
camera focus, queues the command, and sends a new blank form three ticks
later while host output continues to render. No chat interception. Each
submission renews the idle watchdog; Escape/close, `exit`, timeout, or rejoin
restores movement. The client resource pack controls the compact layout.
Typed commands go to the existing host application CLI with explicit bounded
arguments, not host Bash. `help`, `games`, `play <title>` and
`craftmaster <world>` are routed; host-launch commands require OP.

This drop has not executed the pinned native providers: network resolution for
the GitHub releases failed in the editing environment. Repository and syntax
checks do not certify physical placement, frame display, or native server
startup. The actual text screen interaction is implemented; in-world story
interpreter interaction, graphical view streaming, Atlas navigation and speech
remain unimplemented. Do not publish as fully accepted without native evidence.


### Display-compatible BDS receipt

The Endstone 0.11.16 provider is paired with the official 1.26.65.1 BDS
archive (registry version 1.26.65). Full Setup acquires both pinned official
ZIPs (Linux x64 for macOS/Linux, Windows x64 for Windows), after EULA consent.
Scoped Windows Setup also acquires its archive. Both are required Setup cache
outputs, not application payloads or redistributed servers. Mac Test passes
the exact Linux archive version and URL into the normal installed package
receipt. Linux and Windows offline Test verify their pinned archive hashes;
this does not constitute native display/server execution on those platforms.
Older receipts with a null version are migrated in place only when their
platform and recorded official archive digest match the pinned release.
No server replacement, world reset, new VM or network request is needed for
that migration. Incompatible receipts are not relabelled; the pinned official server must be
installed and verified. Diagnostics retain the recorded version and digest.
Ordinary hosting requires the same compatible server and display provider.

Every new Bedrock launch (named worlds, `bedrock run`, restart, and
`bedrock testgrounds`) checks the installed official package receipt and
uses the same EULA-gated installer to acquire the pinned compatible release
if missing or incompatible. The first-install worker uses the same release.
A compatible installation is reused offline; an already-running server is
not upgraded in place. A failed update prevents a new server from starting. Offline Test
uses the same code with Setup's checksum-verified cached ZIP, even if an
older server is already installed. The Run tree refreshes package-owned
files when its package digest changes; existing worlds, plugins, settings
and access files are retained. The pin follows the packaged Endstone provider,
not the newest arbitrary Mojang release. Retail users do not need Developer
Tools or Setup.

### Physical map mounting (MediaPlayer 2.3.1)

Native acceptance showed that `mpv materialize` creates empty frames and
hands named map tiles to the operator. The backend transition is not proof of
installation. Endstone 0.11.16 has no public frame-item setter. The mounting
path encodes the delivered map IDs into a Bedrock little-endian NBT structure
with the exact frame orientation and item data. Reserved structure names are
seeded at server launch, and their files are replaced atomically. The provider
requires the running server to resolve a changed probe structure before
allocating a screen; lifecycle Test checks the native probe acknowledgement.
A restart is needed after 128 mounting attempts to replenish reserved names.

The pack checks frame geometry, loaded structure dimensions and permutations,
and the native frame occupancy after placement. Only the matching inventory
tiles for observed occupied frames are removed, and the Terminal item is
consumed after full acknowledgement. Previously registered incomplete screens
can be recovered by clicking a frame with their matching maps in inventory;
partial recovery writes only empty cells. New incomplete screens retain a
pending record and require the retained Terminal item on retry. No direct
writes to the live LevelDB or upstream binary changes are used. The existing
operator requirement and temporary inventory capacity requirement remain.

The stable script API proves occupancy but cannot read the frame's map ID;
identity is derived from the delivered map metadata and encoded structure.
The runtime probe establishes only live structure resolution, not successful
block actor loading. Actual native structure hot-read and frame NBT loading,
map rendering, and Windows/Linux execution still require acceptance. If the
probe fails, the provider refuses to allocate a screen. Do not interpret a
structure file or an occupied frame bit as proof that the display rendered.


### Screen presentation and repair

Native user screenshots confirmed physical mounting and live Terminal text.
Render text onto a single opaque surface using measured glyph widths, not an
estimated character count. Use 16-pixel row pitch with blank horizontal tile
boundaries. This addresses text layout at horizontal map edges; native
physical frame seams still require visual acceptance. Newly installed maps
have no custom display name in their NBT, avoiding the MediaPlayer tile hover
labels. The temporary inventory tiles keep their labels for verified ID matching.

Persist the original map IDs and exact screen geometry before mounting, merge
partial recoveries only when IDs agree, and archive old named mount structures
before resetting launch slots. A click on a damaged registered screen or its
exposed backing starts operator-only repair in place. Sneak-click can refresh a
complete screen. An older complete screen is refreshed on first click to
remove its labels. Repair restores the original IDs without allocating another
screen or consuming another Terminal. Missing backing or unrelated front
blocks are refused. If no complete verified legacy map identity survives,
repair refuses rather than guessing IDs. The old mount structures must still
exist when this update first starts; earlier resets may have discarded them.

Repository and local encoder/layout checks are not native acceptance. Existing
map-frame relabel/repair, native chat routing, and actual client seams must
be verified against the installed game.

### TestGrounds Terminal showroom

The one-time showroom revision adds eight freestanding, exact-size backing walls near native spawn without moving existing player builds. The existing chest receives one stack of 64 of each distinct item: `1` (1×1), `2` (2×2), `3` (3×3), `4` (4×4), `sm` (2×1), `med` (3×2), `lg` (4×3), `xl` (5×4). Names and icons are supplied by the managed Terminal resource pack. Existing stock is not replenished. `/cinter:terminal <size>` gives one item. Use the bottom-left backing block to install. Normal screen clicks open the command form; sneak-click with another Terminal item requests removal and returns the original size, then use a matching wall for the new size. Sneak-click without a Terminal requests repair. The title-scoped resource-pack UI presents a compact bottom input and Enter button; submission uses the original Endstone form callback and reopens a blank command form without releasing camera focus. Client layout and native removal require native acceptance.

### Interactive fiction on a Bedrock Terminal

A catalog story selected with `play <title>` or its short command on a placed Terminal takes over that screen, rather than issuing the desktop `/api/if/start` tab action. The existing host IF installer and ZMachine 0.3.2 / WebIOAdapter run the original story in detached DOM nodes in the installed native WebView. The Bedrock session owns the turn queue; the authenticated native WebView returns the transcript at each interpreter input boundary. Each screen has one game session and one outstanding turn. While the interpreter is active all text, including `quit`, its confirmation, and commands that would otherwise be host commands, is story input. Only actual VM termination (or a reported interpreter failure) returns the host prompt. Closing the input form merely leaves camera focus; clicking again resumes the same story. In-world autosaves use the existing CONV/Quetzal format under the existing Steam IF Saves tree, scoped to the story and screen ID (`Bedrock/<screen>/autosave.conv.json`), without replacing desktop saves. The host application must remain open; there is no new service or Node/DevTools dependency. Native WebView background execution and in-game command turns require native verification.

The managed Terminal resource pack is a declared behavior-pack dependency and
is registered at the top of the world resource stack. The native
`texturepack-required` property remains under the user's control; the Terminal
installer must not force the entire world resource stack on clients. To undo
the previous automatic override on existing installations, stop the server and
run `bedrock texturepack-required false` before restarting. Existing pack IDs,
item IDs, placed screen identities, showroom stock and the compact form layout
are unchanged. The IF turn bridge repeats unacknowledged jobs, deduplicates by
turn ID in the native WebView, and retries completed responses rather than
losing game input if a loopback HTTP response is interrupted. `play zork`
and `zork` resolve to the existing Zork I catalog entry on an in-world screen.

### CraftMaster Zoomify on a Bedrock Terminal

Enter `craftmaster <local Java world>` on the existing placed Terminal. The
Bedrock-owned display worker resolves the native CraftMaster world ID and its
world-local `CinterForge/Maps/<save folder>` ZFY tree. It does not issue the
desktop map-tab command. Existing maps are reused; when absent, the existing
native CraftMaster conversion runs in a worker without importing its export
or stopping the live Bedrock server. No new server, dependency, or giant
map bitmap is created. The existing authored 512-pixel tile geometry and
256-tile groups determine the visible pyramid level and tile filenames.
Only visible tiles are copied through the existing display IO share, then
composited into one MediaPlayer RGBA surface using its actual pixel size.
The viewport refreshes every five seconds, including during conversion.

Map mode releases the command form and camera so the player can aim at and
right-click the physical screen. The bottom toolbar supports `+`, `-`, `<`,
`>`, `^`, `v`, `H` (Home), `OW/N/E` (cycle dimensions), and `X` (Exit).
Clicking the map centers the clicked location. Home reads the Java world's
actual legacy or modern initial spawn and the authored map origin; where
spawn data is unavailable or on another dimension, it fits the entire map.
The small 1x1 screen uses two rows of controls. Exit restores the Terminal
transcript/prompt; click again to enter commands. Normal Terminal and IF
input, existing screen/item IDs, pack policy, wall placement, mounting and
networking remain unchanged. Sneak-click retains the existing repair path.
Native client hit coordinates and provider rendering require in-game
acceptance; local geometry and source checks do not substitute for it.
