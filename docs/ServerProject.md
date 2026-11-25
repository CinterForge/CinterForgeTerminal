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
| Endstone/MediaPlayer provisioning | Pending compatible live provider integration; earlier prototypes not accepted |
| Physical screen / placeable Terminal / click interaction | Pending; inventory item exists in source but cannot be placed until the display provider is connected |
| Actual framebuffer, prompt, speech, input, IF, catalog, graphical views and map navigation | Pending |
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
Minecraft are not overwritten. Mac, Linux and Windows use native Java, not a VM
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
execution environment; they are not native Windows GUI certification. This
drop does not add Linux/Windows native Bedrock acceptance or claim client login
on any target. Ordinary Minecraft startup may load/generate missing chunks as
part of normal server operation; Terminal does not request terrain generation.
The real installed-app server/PTY lifetime and in-game Terminal acceptance
remain separate pending gates. No native runtime was available here.

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
