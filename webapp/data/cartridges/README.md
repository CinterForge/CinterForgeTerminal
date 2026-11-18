# CinterForge game cartridges — JSON profiles

These are **standalone proposed cartridge data files**, not a wired-in patch to the Terminal launcher.

- `Games/`: 69 regular games (Lagomorph, WarGames/JOSHUA + 67 Infinite Mac games), matching `docs/Game Catalog.md` and `webapp/data/mac_games.json`.
- `InteractiveFiction/`: 16 story cartridges, matching `webapp/data/if_titles.json`.

## Contract

Each file is one `cinterforge.terminal.game-cartridge.v1` object with a `card` display section and launch/media metadata. This is a new proposed schema; the supplied Terminal source does **not** currently read or execute these files. To make a card clickable, add a loader mapping `command` or `launch` to the existing Terminal routes. Do not treat presence of a JSON file as auto-launch implementation.

Classic Mac machine/system disk settings reflect the current Terminal code, **not verified per-title compatibility**. Most games are missing verified HFS executable paths, color depth, and true per-title resolutions; these fields are `null` until verified. 640×480 is a *suggested* starting size, not a claimed requirement. `library_path` identifies the source Infinite HD library entry, not the path of a program inside the emulated Mac. The source has an unresolved mismatch for three `needs_mac_os_x` games: its existing launcher chooses Mac OS 9.2 despite that requirement. Do not silently reclassify them as working.

IF download URLs are copied from the existing Terminal manifest and have **not** been live-verified in this delivery. The downloads remain opt-in per story with existing format and size checks; story bytes are not redistributed. Story filenames and commands are case-sensitive and retain the source spelling.

Lagomorph retains the existing verified 640×480, 4-bit (16-color) requirement and fallback startup sequence. Guest AppleScript is a proposed enhancement, *not* a verified replacement. Do not replace the working sequence until both guest AppleScript execution and a working display-depth scripting addition have been tested.

## Suggested integration

Read all `Games/*.json`, `InteractiveFiction/*.json`, and optionally `Special/*.json` at cartridge/catalog loading time. Use the `card` section to render the face and `command` to route existing Terminal commands. Use cartridge settings in a future generic launcher only when the settings have been verified for that title. Keep `source` for traceability and don't fetch unverified direct media URLs.
