# Games and Experiences

**Every game launches by typing its command in any existing Terminal Session.** There is no separate Game mode or New Game Terminal menu action. Type `wargames` to open the **original, unchanged JOSHUA** application as a new Session tab; it is game #1 in the 68-game `games` list. The other 67 entries are classic Macintosh games, launched with their existing commands. `stories` lists 16 independent interactive-fiction titles; their original filename commands still work. `infinite-mac` lists the 67 classic Macintosh games; `bedrock` manages Minecraft; `minecraft` opens a live server console; `lagomorph` retains its hidden original easter egg.

Games run in their existing appropriate player, emulator or service. `wargames` uses JOSHUA's existing standalone web application and original audio, presentation and input logic. Terminal commands remain available from ordinary Mac/Windows/Linux shell Sessions, while `code` opens Code-OSS for development. Native Mac/Windows/Linux and Minecraft console actions remain in the system menu; game names are typed, not selected through a special Game-mode menu.

## Game #1 — WarGames (JOSHUA)

The original WarGames/JOSHUA application retains its **Tic-Tac-Toe**, **Hangman**, **Sudoku**, and fictional **Global Thermonuclear War** narrative/simulation, including its staged boot/greeting, animated presentation, EN/IT localization, Web Speech narration when available, synthesized Web Audio cues, keyboard accessibility and reduced-motion behavior. The `wargames` command opens the existing JOSHUA Session without changing those modes or substituting an implementation. The global-war sequence is fiction, not a real military system.

The JOSHUA source has its own attribution and **AGPL-3.0-only** license obligations; anyone bundling or modifying it must satisfy those obligations. General game/media licensing is separate from the Terminal Host license.

## Games #2–68 — classic Macintosh games

`infinite-mac` (or `help infinite-mac`) lists the classic Macintosh titles. The catalog is based on the [Infinite Mac](https://infinitemac.org/) game library; titles use their command aliases rather than arbitrary file paths. Infinite Mac's browser runtime and game media may require network access, and each game's appropriate Mac profile/software media must be respected.

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

## Interactive fiction

`stories` lists the Z-machine story catalog. A title's **case-sensitive filename stem** is its command. The app downloads story files only on request from their declared upstream sources; it does not redistribute them with the native Terminal application.

| Command | Story |
|---|---|
| `Advent` | Colossal Cave Adventure |
| `Hat` | Oh no, Mr.Crab stole your hat! |
| `HugeCave` | Adventure In Humongous Cave |
| `Forest_Demo` | Forest Demo |
| `zork1` | Zork I: The Great Underground Empire |
| `zork2` | Zork II: The Wizard of Frobozz |
| `zork3` | Zork III: The Dungeon Master |
| `library_of_horror` | The Library of Horror |
| `404-Life_not_found` | 404- Life not Found (Beta) |
| `BedTime` | Bed Time |
| `QuestForTheMagicMuffin` | The Quest for The Magic Muffin |
| `Elephants` | When I Was Shot By Elephants III |
| `cluedo` | Mini-Cluedo |
| `HellsBasement` | Hell's Basement |
| `QuestForTheMagicMuffin3` | The Quest for The Magic Bagel...Err Muffin 3 |
| `devours` | All Things Devours |

Use `<command> install`, `<command> status`, `<command>` (or `<command> start`), and `<command> stop` as appropriate. The JavaScript Z-machine interpreter is bundled with the Terminal, while individual story files live under the user's designated `steamapps` content tree. CONV/Quetzal saves persist independently of the application archive.

## Minecraft Bedrock

Minecraft is a managed game/server experience, not a generic developer terminal. `bedrock install` obtains the official server after EULA acceptance. `bedrock start` discovers worlds; `bedrock run <world>` launches the chosen managed copy; `minecraft` opens a new console attached to that same running server. The console can be closed without stopping the server. `bedrock stop` and application shutdown end the managed server and guests.

Bedrock uses the LinuxKit/Rosetta guest and host LAN relay on macOS. Client connectivity and discovery must be verified on the host LAN; the guest does not receive its own LAN IP. Details are in the [User Manual](User%20Manual.md).

## Lagomorph

The **`lagomorph`** command is a preserved, dedicated easter egg, not an ordinary Infinite Mac alias. It retains its exact working classic Macintosh **Quadra 650** launch sequence and original experience: authored application with resource fork, HFS disk/application image, six **Battles**, manual, icons, Finder layout, boot/return controls, local serving, URL/asset contracts and independently scoped publication inputs.

A game catalog or WarGames integration **dispatches to the existing Lagomorph launcher**. It must not rebuild the user experience as a generic page or silently rename the original media and launch paths. The classic-media package must remain independently licensed and verifiable.

Lagomorph-specific media and other third-party game/system images have independent publication and licensing requirements. They are not automatically part of every public game download.
