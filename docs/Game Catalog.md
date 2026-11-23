# Games and Experiences

**Every game launches by typing its command in any existing Terminal Session, with or without the optional `play` prefix.** There is no separate Game mode or New Game Terminal menu action. Type `joshua` (or `play joshua`, or its `wargames` alias) to open the **original, unchanged JOSHUA** application as a new Session tab; `joshua` is first in `games`. The `games` list then shows each of the 16 actual interactive-fiction title commands (from the authored JSON), one `infinitemac` emulator gallery command, and `bedrock` last. The 67 classic Macintosh game aliases remain available directly but do not clutter `games`. `infinitemac` / `play infinitemac` opens a searchable/filterable gallery of the same 20 OS-version choices (one representative machine per OS release) shown in the machine menus, with each thumbnail opening its emulator in a new tab. `bedrock` / `play bedrock` opens Bedrock help and controls Minecraft; `minecraft` opens a live server console; `lagomorph` retains its hidden original easter egg.

Games run in their existing appropriate player, emulator or service. `wargames` uses JOSHUA's existing standalone web application and original audio, presentation and input logic. Terminal commands remain available from ordinary Mac/Windows/Linux shell Sessions, while `code` opens Code-OSS for development. Native Mac/Windows/Linux and Minecraft console actions remain in the system menu; game names are typed, not selected through a special Game-mode menu.

## Game #1 — WarGames (JOSHUA)

The original WarGames/JOSHUA application retains its **Tic-Tac-Toe**, **Hangman**, **Sudoku**, and fictional **Global Thermonuclear War** narrative/simulation, including its staged boot/greeting, animated presentation, EN/IT localization, Web Speech narration when available, synthesized Web Audio cues, keyboard accessibility and reduced-motion behavior. The `wargames` command opens the existing JOSHUA Session without changing those modes or substituting an implementation. The global-war sequence is fiction, not a real military system.

The JOSHUA source has its own attribution and **AGPL-3.0-only** license obligations; anyone bundling or modifying it must satisfy those obligations. General game/media licensing is separate from the Terminal Host license.

## Infinite Mac 68K clipboard command pipe (startup integration)

Only **System 7.1** and **Mac OS 7.6** mount the
CinterForge Terminal companion disk. Opening one of these **machine cards** starts
its operating system and attempts to open **CinterForge Terminal only**.
`Game.txt` does not auto-launch on boot; `run game.txt` remains an explicit
command. Lagomorph and other museum systems retain their separate behavior.

The 68K Terminal runs continuously alongside launched games. Every second
it polls the classic Macintosh `TEXT` scrap, recognizes only commands
addressed to its OS, and dispatches supported existing commands:

```
cinter:7.1:play tetris
cinter:7.6:play escapevelocity
cinter:7.6:play warcraft
```

The current message exchange is `cinter:<os>:hello <16-hex-token>` followed
by `cinter:<os>:ready <same-token>`. The host will record `CINTERFORGE_READY`
in the Server log **only after reading a matching clipboard response**.
At that moment the macOS host speaks the existing greeting through its Zarvox
startup-speech endpoint. The 68K application does not use MacinTalk or any
Speech Manager trap. `cinter:<os>:ack <command>` acknowledges dispatch. Duplicate unchanged
clipboard text is not executed again. A `play` arriving during discovery is
deferred until its OS-filtered actual-game catalog is ready. A separate game
runs under classic Mac cooperative multitasking; quitting the game returns
to the resident Terminal.

The native macOS host clipboard is accessed through the already existing
`cinterClipboard` bridge; the guest uses its existing Scrap Manager traps.
**Unattended host-to-guest clipboard synchronization across the Infinite Mac
cross-origin iframe has not been verified.** Manual Copy/Paste working is not
proof of unsolicited Scrap Manager synchronization. This integration must be
accepted by live boot/READY and actual `play` testing in both supported emulators; the
host retries Finder launch with a 25-second recovery interval until a matching
`READY` is received or the emulator tab is closed. One mouse-click sequence is
never counted as confirmation. It does not keep clicking when the emulator tab
is hidden, and it only closes the overlapping Infinite HD window on the first
attempt. Unacknowledged HELLO messages rotate ownership after 110 seconds so a
second guest is not blocked; the nonce is regenerated for each lease. This
cannot guarantee READY unless Infinite Mac actually synchronizes the guest
Scrap Manager clipboard and the native macOS clipboard. Server log entries
for launch attempts and waiting-for-READY are diagnostic, not success markers.
No host Game Cards have been implemented yet.

## 68K Games Launcher icon folder

From the existing System 7.1 or Mac OS 7.6 68K Terminal, choose **Games → Games
Launcher** or enter `games launcher`. This opens a third, independent classic
Macintosh window, using the discovered Finder applications and their actual
resource-fork icons. The fixed **8×8** (System 7.1, up to 50 games) or **6×6**
(Mac OS 7.6, up to 32 games) folder layout leaves unused cells blank to make
screenshots consistently sliceable. Finder names appear below the icons; one
click on a populated cell launches that exact application, as in Games Report.
The original Terminal window, Game Card report and emulator boot behavior do
not change. This view does not require or export an icon ZIP.

## Classic Macintosh games — direct commands

`infinitemac` opens the 20-OS-version emulator gallery; individual classic Macintosh game aliases remain available as direct commands but are not separate `games` entries. The catalog is based on the [Infinite Mac](https://infinitemac.org/) game library; titles use their command aliases rather than arbitrary file paths. Infinite Mac's browser runtime and game media may require network access, and each game's appropriate Mac profile/software media must be respected.

The game commands are:

| Command | Title |
|---|---|
| `3wiz` | 3Wiz! |
| `a10attack` | A-10 Attack! |
| `abuse` | Abuse |
| `anotherworld` | Another World |
| `apeiron102` | Apeiron 1.0.2 |
| `apeiron103` | Apeiron 1.0.3 |
| `avara` | Avara |
| `barrack` | Barrack |
| `battlechess` | Battle Chess |
| `blobbolite` | Blobbo Lite |
| `bolo` | Bolo |
| `boom` | BOOM |
| `bubbletrouble` | Bubble Trouble |
| `chainshot` | ChainShot! |
| `chiral` | Chiral |
| `civilization` | Civilization |
| `continuum` | Continuum |
| `cosmicosmo` | Cosmic Osmo |
| `darkcastle` | Dark Castle |
| `escapevelocity` | Escape Velocity |
| `fa18hornet` | F-A-18 Hornet |
| `glider` | Glider |
| `hellcatsoverthepacific` | Hellcats Over the Pacific |
| `indyandhisdesktopadventures` | Indy and His Desktop Adventures |
| `indyandthefateofatlantis` | Indy and The Fate of Atlantis |
| `indyandthelastcrusade` | Indy and The Last Crusade |
| `lemmings` | Lemmings |
| `loom` | Loom |
| `maelstrom` | Maelstrom |
| `marathon` | Marathon |
| `marathon2` | Marathon 2 |
| `marathoninfinity` | Marathon Infinity |
| `minotaur` | Minotaur |
| `missilecommand` | Missile Command |
| `nsshaft` | NS-SHAFT |
| `okasheepgame` | ok - A sheep game |
| `pararena` | Pararena |
| `pathwaysintodarkness` | Pathways Into Darkness |
| `pathwaysintodarknessextras` | Pathways Into Darkness (Extras) |
| `pipedream` | Pipe Dream |
| `pongsaver111` | PongSaver 1.1.1 |
| `princeofpersia` | Prince of Persia |
| `quinn131` | Quinn 1.3.1 |
| `risk` | Risk |
| `scarabofra` | Scarab of Ra |
| `shufflepuckcafe` | Shufflepuck Café |
| `simcity` | SimCity |
| `simcity2000` | SimCity 2000 |
| `snood` | Snood |
| `snood30` | Snood 3.0 |
| `sokoban` | Sokoban |
| `solarianii` | Solarian II |
| `spectre` | Spectre |
| `starbound` | Starbound |
| `strategicconquest30` | Strategic Conquest 3.0 |
| `strategicconquest401` | Strategic Conquest 4.0.1 |
| `strategicconquestplus` | Strategic Conquest Plus |
| `stuntcopter` | StuntCopter |
| `swoop` | Swoop |
| `tetris` | Tetris |
| `theoregontrail` | The Oregon Trail |
| `thesecretofmonkeyisland` | The Secret of Monkey Island |
| `troubledsouls` | Troubled Souls |
| `ultimatedoom` | Ultimate Doom |
| `warcraftorcshumans` | Warcraft - Orcs & Humans |
| `warcraftiitidesofdarkness` | Warcraft II - Tides of Darkness |
| `wesleyantetris` | Wesleyan Tetris |

### Per-game historical Macintosh starting profiles

The existing host-side 67-game `webapp/data/mac_games.json` catalog now
specifies `emulator_profile` for each command. Both a newly opened game tab
and restoration of a previously opened game tab resolve that ID through the
real emulator definitions in `webapp/data/terminal_styles.json` rather than
using Mac OS 7.6 for everything, or an unsupported Mac OS 9.2 value for an
OS X-only item. These are **initial compatibility candidates**, not tested
minimum operating-system requirements. The existing legacy game-command path
still launches the Infinite Mac environment; automated opening of the game
inside the guest is **not proven**.

| Target emulator | Guest OS | Typical game era/use |
|---|---|---|
| Mac II | System 7.1 | Earlier 68K/black-and-white classics; some may work better under System 6, where the 68K companion is not available |
| Quadra 650 | Mac OS 7.6 | System 7-era colour games and 68040 titles |
| Power Macintosh G3 (Blue & White) | Mac OS 9.0.4 | Available as a fallback if a game demonstrably needs Mac OS 9; no game is assigned here without evidence |
| Power Macintosh G4 (PCI Graphics) | Mac OS X 10.3 | Only the existing catalog entries explicitly marked as requiring Mac OS X |

### Current Infinite HD guest scan: OS candidates by exact copied name

The 2026-11-22 guest clipboard sample contains 111 `APPL` records.
The curated filter excludes 28 identified tools, guides, installers and
help viewers, leaving 83 candidate entries before excluding the PowerPC-only
Loom copy; the 68K guest now allowlists 82 *entries* (including versions and same-named
copies). This table is the **curated allowlist for the actual guest filesystem
scan**: the 68K app still resolves genuine executable FSSpecs and icons from
Infinite HD, but includes an APPL only in the guest OS column assigned here.
The Terminal `games` list, `games report` cards, `games copy`, `games export`,
`play`/Finder-name launch and `Game.txt` launch all share that OS-scoped
catalog. Unknown APPLs remain hidden pending inspection. OS versions are first-test choices
based on the title's era and expected architecture; they are **not
verified to work**. A System 6 build may be better for some earliest games,
but the 68K Terminal disk only targets the two OS versions above. Variants
not present in this allowlist remain hidden until identified and assigned.

| Guest command from clipboard | Finder name | Starting OS candidate |
|---|---|---|
| `3wizcolunr` | 3Wiz! col./unr. | System 7.1 |
| `a10attack15` | A-10 Attack! 1.5 | Mac OS 7.6 |
| `apeiron` | Apeiron | Mac OS 7.6 |
| `apeiron2` | Apeiron | Mac OS 7.6 |
| `avara` | Avara | Mac OS 7.6 |
| `avaracommuter` | Avara Commuter | Mac OS 7.6 (inspect) |
| `barrack` | Barrack | Mac OS 7.6 |
| `bubbletrouble` | Bubble Trouble | Mac OS 7.6 |
| `chiral` | Chiral | Mac OS 7.6 |
| `escapevelocity` | Escape Velocity | Mac OS 7.6 |
| `maelstrom` | Maelstrom | System 7.1 |
| `swoop` | Swoop | Mac OS 7.6 |
| `outofthisworld103` | Out of this World™ 1.0.3 | System 7.1 |
| `battlechess` | Battle Chess | System 7.1 |
| `blobbo102lite` | Blobbo 1.0.2 Lite | Mac OS 7.6 |
| `bolo0997` | Bolo 0.99.7 | System 7.1 |
| `boom10` | BOOM 1.0 | Mac OS 7.6 |
| `abuse101` | Abuse 1.01 | Mac OS 7.6 |
| `marathonv12` | Marathon v1.2 | Mac OS 7.6 |
| `marathon2` | Marathon 2 | Mac OS 7.6 |
| `marathon15` | Marathon ∞ 1.5 | Mac OS 7.6 |
| `minotaur` | Minotaur | System 7.1 |
| `minotaur2` | Minotaur | System 7.1 |
| `minotaur3` | Minotaur | System 7.1 |
| `minotaur4` | Minotaur | System 7.1 |
| `minotaur5` | Minotaur | System 7.1 |
| `minotaur6` | Minotaur | System 7.1 |
| `minotaur7` | Minotaur | System 7.1 |
| `pathwaysintodarkness` | Pathways Into Darkness | Mac OS 7.6 |
| `chainshot` | ChainShot! | System 7.1 |
| `civilization` | Civilization™ | System 7.1 |
| `continuum` | Continuum | System 7.1 |
| `cosmicosmo` | Cosmic Osmo | System 7.1 |
| `darkcastle` | Dark Castle | System 7.1 |
| `hornetace10` | Hornet Ace 1.0 | Mac OS 7.6 (inspect) |
| `fa18hornetclassic` | F/A-18 Hornet Classic | Mac OS 7.6 |
| `glider406` | Glider 4.06 | System 7.1 |
| `glider409` | Glider 4.09 | System 7.1 |
| `zero220` | Zero 2-20 | System 7.1 (inspect) |
| `hellcats` | Hellcats | Mac OS 7.6 |
| `lemmings` | Lemmings | System 7.1 |
| `indysdesktopadventures` | Indy’s Desktop Adventures | Mac OS 7.6 |
| `fateofatlantis11` | Fate of Atlantis 1.1 | System 7.1 |
| `fateofatlantisv15` | Fate of Atlantis v.1.5 | System 7.1 |
| `indy` | Indy™ | System 7.1 |
| `loom` | Loom™ | System 7.1 |
| `pipedream` | Pipe Dream™ | System 7.1 |
| `monkeyisland` | Monkey Island | System 7.1 |
| `missilecommand30` | MissileCommand 3.0 | System 7.1 |
| `nsshaft` | NS-SHAFT | Mac OS 7.6 |
| `ok020` | ok 0.2.0 | System 7.1 |
| `pararena206m` | Pararena 2.06M | System 7.1 |
| `pipedream2` | Pipe Dream™ | System 7.1 |
| `princeofpersia` | Prince of Persia | System 7.1 |
| `colorrisk241` | ColorRisk 2.41 | System 7.1 |
| `scarabofra14` | Scarab of RA 1.4 | System 7.1 |
| `shufflepuckcafv10` | Shufflepuck Café v1.0 | System 7.1 |
| `simcityclassic` | Sim City Classic | System 7.1 |
| `simcity200012` | SimCity 2000® 1.2 | Mac OS 7.6 |
| `snood` | Snood | Mac OS 7.6 |
| `sokoban242a` | Sokoban 2.4.2a | System 7.1 |
| `solarianii` | Solarian II | System 7.1 |
| `spectre11` | Spectre 1.1 | System 7.1 |
| `starbound` | StarBound | Mac OS 7.6 |
| `strategicconquest301` | Strategic Conquest 3.01 | System 7.1 |
| `strategicconquest401` | Strategic Conquest 4.0.1 | Mac OS 7.6 |
| `strategicconquestplus20` | Strategic Conquest Plus 2.0 | Mac OS 7.6 |
| `stuntcopter10` | StuntCopter 1.0 | System 7.1 |
| `stuntcopter12` | StuntCopter 1.2 | System 7.1 |
| `stuntcopter15` | StuntCopter 1.5 | System 7.1 |
| `stuntcopter16` | StuntCopter 1.6 | System 7.1 |
| `stuntcopter20` | StuntCopter 2.0 | System 7.1 |
| `stuntcopterpatchedforan` | StuntCopter--patched for any OS | System 7.1 |
| `tetris` | TETRIS | System 7.1 |
| `tetriscolor` | TETRIS.color | System 7.1 |
| `oregontrail` | OREGON TRAIL | System 7.1 |
| `troubledsouls` | Troubled Souls | Mac OS 7.6 |
| `ultimatedoomi` | Ultimate Doom I | Mac OS 7.6 |
| `warcraft` | Warcraft | Mac OS 7.6 |
| `warcraftii100k` | Warcraft II 100% [k] | Mac OS 7.6 (inspect) |
| `warcraftiiv122` | Warcraft II v.1.22 | Mac OS 7.6 |
| `wesleyantetris` | Wesleyan Tetris | System 7.1 |

The three provisional entries marked **(inspect)** are included on their
assigned OS for examination, not confirmed as independently identified games,
and must be reviewed from their actual executables. Repeated `Minotaur`, `Apeiron` and `Pipe Dream` copies are
preserved; the scanner cannot determine which is the best copy from Finder
name alone. The extra game versions have OS recommendations here but are
**not automatically wired** as additional host shell aliases; the existing
67 authored host aliases use their `emulator_profile` fields.

The classic-Mac **guest** `games` commands still launch applications found
on its mounted Infinite HD by their exact FSSpec. An already booted emulator
cannot switch its Mac OS in place. To choose a different OS automatically,
launch the corresponding game command through the *host* Terminal, which opens
another emulator Session; guest-to-host launch forwarding is not implemented.
Only a real guest launch establishes whether a chosen OS and machine work.

## Interactive fiction

`stories` lists the Z-machine story catalog. Each title has a short lowercase command in the checked-in JSON; original filename stems remain accepted as aliases. The app downloads story files only on request from their declared upstream sources; it does not redistribute them with the native Terminal application.

| Command | Story |
|---|---|
| `advent` | Colossal Cave Adventure |
| `hat` | Oh no, Mr.Crab stole your hat! |
| `huge` | Adventure In Humongous Cave |
| `forest` | Forest Demo |
| `zork1` | Zork I: The Great Underground Empire |
| `zork2` | Zork II: The Wizard of Frobozz |
| `zork3` | Zork III: The Dungeon Master |
| `library` | The Library of Horror |
| `fourofour` | 404- Life not Found (Beta) |
| `bed` | Bed Time |
| `muffin` | The Quest for The Magic Muffin |
| `elephants` | When I Was Shot By Elephants III |
| `cluedo` | Mini-Cluedo |
| `hells` | Hell's Basement |
| `muffin3` | The Quest for The Magic Bagel...Err Muffin 3 |
| `devours` | All Things Devours |

Use `<command> install`, `<command> status`, `<command>` (or `<command> start`), and `<command> stop` as appropriate. The JavaScript Z-machine interpreter is bundled with the Terminal, while individual story files live under the user's designated `steamapps` content tree. CONV/Quetzal saves persist independently of the application archive.

## Minecraft Bedrock

The `games` list places `bedrock` last, after JOSHUA, the 16 individual interactive-fiction titles, and `infinitemac`. Typing `bedrock` displays Bedrock-specific help; `bedrock report` opens the local HTML overview of Java and Bedrock worlds with Minecraft icons, sanitized world names, Set Current, Download, and Java-world Update for Bedrock and Map controls.

Choose the single Current World through the report or with `bedrock world <name>`. Each world independently remembers its selected CinterForge pack, which can be chosen with `bedrock pack <name>` or through `bedrock packs` (available without developer credentials). Use `bedrock pack none` to clear the selected pack for that world only. The server commands are `bedrock start`, `bedrock run` and `bedrock stop`; neither start command accepts a world name. If the Java source changed since its converted Bedrock world was created, Bedrock asks to update the conversion or run the older conversion explicitly. The report and map viewer show the same readiness state, and conversion outputs reside inside the source Java save's `CinterForge/` folder.

The official Bedrock Dedicated Server is installed only as needed after explicit EULA acceptance. It runs the selected Bedrock world in its own interactive terminal tab, retaining the existing managed server copy and authoritative saves. Existing server-wide operator and allowlist commands remain under `bedrock op` and `bedrock allowlist`. For the complete current command flow, see the [User Manual](User%20Manual.md).

## Lagomorph

The **`lagomorph`** command is a preserved, dedicated easter egg, not an ordinary Infinite Mac alias. It retains its exact working classic Macintosh **Quadra 650** launch sequence and original experience: authored application with resource fork, HFS disk/application image, six **Battles**, manual, icons, Finder layout, boot/return controls, local serving, URL/asset contracts and independently scoped publication inputs.

A game catalog or WarGames integration **dispatches to the existing Lagomorph launcher**. It must not rebuild the user experience as a generic page or silently rename the original media and launch paths. The classic-media package must remain independently licensed and verifiable.

Lagomorph-specific media and other third-party game/system images have independent publication and licensing requirements. They are not automatically part of every public game download.

## Infinite HD and the native 68K Games Report

Compatible classic Macintosh System 7, Mac OS 7/8 and Mac OS 9 emulator
profiles request **Infinite HD** using Infinite Mac's `infinite_hd=true`
argument. **CinterForge Terminal** (`CinterForgeTerminal.68k.img`, delivered by
`disk_url`) mounts **only** on System 7.1 and Mac OS 7.6,
subject to published-media HTTPS range/CORS verification. Other System 7/8/9
versions, System 1–6, Mac OS X, NeXTSTEP, BeOS and Lagomorph's separately
calibrated floppy launch do **not** receive this 68K application disk.
Normal Mac OS 7.6 game sessions are eligible; the dedicated Lagomorph
Quadra 650 museum session remains unchanged.
Infinite Mac remains responsible for determining which profiles support
Infinite HD; `infinite_hd=true` is a request, not a guarantee.

For **System 7.1 and Mac OS 7.6**, an ordinary machine
card mounts the CinterForge Terminal companion disk, boots the chosen OS,
and never initiates a game launch. On Mac II (7.1) and Quadra 650 (7.6),
the host makes one Finder double-click attempt after boot to open the
**CinterForge Terminal** application. The 7.6 sequence first closes the
obscuring Infinite HD Finder window, then activates the companion disk window.
Mac OS 8.1 retains its historical emulator profile but receives no
CinterForge Terminal companion disk, clipboard commands, or automatic Finder
launch. Loom PPC is intentionally excluded from the 68K game catalog.

The guest only becomes confirmed ready when the host receives a matching
`cinter:<os>:ready <token>` clipboard response. A matching READY triggers
server-log output and host-only Zarvox greeting. The automated clipboard
transfer has not been verified in a live guest; a missing READY is not proof
that the app failed to launch and **must not trigger repeated double-clicks**.
Lagomorph's special profile is unchanged.

On CinterForge Terminal, the genuine classic-Mac **CinterForge Terminal**
application can also be opened manually. The HFS Standard disk uses Lagomorph's established disk-assembly
and resource-fork path: the `CinterForge Terminal` APPL and editable `Game.txt`
are at the root, with its README and card artwork inside `Support Files`.
The application opens a native, white-background old-style Mac terminal window.
On System 7+ emulators, the HFS image's Finder window-open directory points
at the **root of the CinterForge Terminal disk**, so mounting the disk requests a Finder
window with `CinterForge Terminal` and `Game.txt` immediately visible. This
**does not execute the application by itself**; the host's scoped
Finder action on 7.1 and 7.6 requests execution after startup. The image
cannot forcibly close Infinite HD's existing Finder window: that other disk
retains its own Finder state. The
existing Lagomorph media and boot automation remain unchanged.

The native 68K `games` command scans only the `Games` directory on Infinite HD
(recursively through publisher and title subdirectories) and includes only
files with Macintosh Finder type `APPL` **and the OS-scoped curated allowlist
shown above**. System 7.1 currently maps 50 entries and Mac OS 7.6 maps
32 (including seven formerly assigned to 8.1); counts include duplicates/versions and
are not guaranteed availability on a given Infinite HD snapshot. Non-game helpers whose Finder
application names contain `manual`, `register`/`registration`,
`documentation`, or `basics` are excluded case-insensitively at catalog
insertion. The filter now also excludes specifically identified non-game applications from the
actual Infinite HD scan: readmes, Bungie's Forge/Anvil/Torch editors, Avara's
MicroTracker, BSP and sound tools, Pathways guides/cheat tools, F/A-18's
Mission Chooser, Glider/SimCity/Snood/Warcraft editor or converter tools,
TeachText, MacroMind Player and Microsoft Help. The explicitly allowlisted
versions and same-named copies remain discoverable on their assigned OS;
unknown applications are intentionally hidden pending inspection. The same filter applies to
`games`, `games report`, clipboard copy and text export. Every accepted application gets a
unique lowercase single-word command and retains its complete Finder name and
exact FSSpec for launch. The text listing, `play <name>` / bare-name command,
and native Games Report use that same real catalog. The Terminal history and
Game Card window use native System 7 scrolling and resize-safe redraw; the
Games Report is not opened by default. The Games Report always uses five columns of Game Cards, scaling their artwork
and panel width horizontally to fill the entire available content width when
the Macintosh window is resized, without leaving empty space beside the cards. Selecting a card shows its full Finder name and command in the
report's status line. No numerical game count is shown in the ordinary UI.
The native 68K app displays its normal greeting without calling MacinTalk or
the classic Speech Manager. Once the host receives a token-matched clipboard
`READY` response, the macOS host speaks the existing greeting with Zarvox.
Without a verified clipboard round trip, the guest remains silent.

`games report` opens **a separate Macintosh window** with Game Cards from
the authored `GameCard.png` and discovered application icon resources.
Single-click a card, type `play "Finder name"`, or type the exact Finder
application name to launch that application. Each card shows the real Finder
icon in color plus two lines of unscaled 12-point Macintosh system text;
there is no redundant "OPEN" caption. The Terminal window remains resident
when the Games Report window is closed.

To discover launch names, open and edit `Game.txt` on CinterForge Terminal in
SimpleText, save, and use **Games → Run Game.txt** in CinterForge Terminal;
this does not restart the emulated machine. `Game.txt` is explicit-only: it never auto-launches at startup. Empty
`Game.txt` leaves the terminal available. The disk's guest-side edits are
**session-only** and do not alter the published master.

Game names and icons are discovered from the real guest filesystem—no
hardcoded games or synthetic initial-letter icons. When a name is ambiguous,
the launcher reports it and lets the user choose a card. Support is subject
to the chosen machine's Mac OS version and each application's requirements;
actual game functionality must be recorded during guest testing.
