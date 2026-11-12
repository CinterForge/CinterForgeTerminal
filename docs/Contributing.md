# Contributing

CinterForge Terminal is a public, source-built native application and reusable Host for macOS, Windows and Linux. Changes should **extend the existing implementation** rather than replace it with new mock games, competing terminal stacks, alternate Bedrock bridges or duplicate build tools.

## Product contract

The application opens in **Game** mode (modified Cinter-styled WarGames/JOSHUA), with direct support for Infinite Mac, interactive-fiction titles, Minecraft/Bedrock and the special `lagomorph` easter egg. Its other terminal modes are **Minecraft**, **Mac**, **Win** and **Linux**. Mac/Win/Linux are **real Code-OSS integrated shells**. Native system menus own New Terminal and Edit commands; HTML must not render competing transient Terminal/Edit menus. Developer commands and Bash belong in Code-OSS, not the Game Terminal.

The `code` command launches a true local-folder/workspace editor, persists the selected workspace across relaunch and provides three Mac-hosted platform terminals or one host-native terminal on Windows/Linux. With this source repository open, the user may edit, build, stage, test and explicitly publish the real four-artifact release from those terminals. `devtools install|uninstall|list|report` controls the optional **single unified** developer-tool package.

See the [User Manual](User%20Manual.md), [Architecture](Architecture.md) and [Build and Release](Build%20and%20Release.md) before modifying any of these entry points.

## Source and artifact boundaries

- Keep authored source in its owning repository. No direct sibling-repository source imports, hidden dependencies or build scripts that assume another checkout layout.
- All acquired SDKs, VM disks, caches, cloned tool sources, compiled outputs, manifests and logs go under the sibling `<repo>/../Builds`, except documented installed runtime/game data under `steamapps`. Do not create mandatory hidden home-directory caches.
- Preserve the existing `CinterForgePythonHost` library, stable `cph_*` C ABI and `./Cinter host ...` lifecycle entry point. Consumers link verified staged artifacts rather than reaching into private sources.
- Prefer updating an existing implementation and its call sites/tests to creating a parallel dispatcher, platform-specific duplicate or unused helper.
- Do not lose classic Mac app resource forks, HFS media structure, IF save behavior, Bedrock world data or Lagomorph manual/Battles/disk/launch mechanics in the course of a menu or packaging change.

## Lifecycle responsibility

**Setup** acquires/builds/verifies all prerequisites and performs a real consented, isolated LinuxKit/Bedrock acceptance test. **Build** compiles, signs and produces exactly **three native app ZIPs and one unified DevTools ZIP**. **Stage** verifies/extracts the finished ZIPs without changing them. **Test** launches actual staged applications and checks real user actions; **Publish** is always a distinct explicit command. The developer `all all` command never publishes.

Mac producer Setup manages independent `w` Windows cross-builder and `c` Linux guest-input builder profiles and shuts them and their owned helpers down even after failures/interruption. The installed application owns its live Bedrock runtime separately, and closes that runtime when its owning app exits. Retain the original Terminal-owned LinuxKit/Rosetta virtual machine and the existing CinterBDSNetRelay producer; do not introduce a second networking service or depend on CFOS's unrelated Ubuntu image. End-user Bedrock installation requires explicit EULA consent.

## Tests and acceptance

Tests should verify actual observable behavior, not merely that a function was invoked:

- Real native WebView launch, keyboard input, menu stability, JS/native callbacks, copy/paste, and correct window shutdown on each supported platform.
- Game Terminal's WarGames behavior and complete game catalog; direct Infinite Mac launch; IF saves; **Lagomorph regression** including special entry point, media and return navigation.
- Real Code-OSS local folder open, file edit/save, last-workspace persistence, integrated platform shells, and complete repository lifecycle from the workbench.
- `devtools` size/confirmation, manifest integrity, version reporting and safe uninstall; no startup SDK download.
- Bedrock license gate, signed LinuxKit server with the LAN relay, console attachment and command/reconnect, client LAN connectivity and cleanup of exactly owned processes.
- Empty-state Setup; no Build provisioning or Stage mutations; Mac DMG and three ZIPs; installed target-native startup. Wine and headless container tests are supporting evidence, never a substitute for Windows WebView2 or Linux desktop execution.

## Licensing and security

Preserve JOSHUA attribution and satisfy the upstream **AGPL-3.0-only** license terms when distributing a modified/bundled JOSHUA experience; do not assume that a source archive being available grants unrestricted relicensing. Do not check in licensed system ROMs, commercial game images, private Steamworks SDK packages, secrets or generated developer state. Public distribution must respect each game's, emulator's, compiler's and runtime's redistribution rights. Local folder access requires explicit user choice and normal OS permissions; the HTML/game layer must not be granted unrestricted native file or shell execution. Versioned transports authenticate their callers and verify transferred content.

## Documentation

Write README and `docs/*.md` for developers discovering the repository. Use relative links, concrete public commands, product behavior and phase ownership. **Do not include dated development-status notes, local machine paths, unverified claims of test success, or internal handoff/task checklists in the public repo.** Keep plans and observed blockers in separately maintained engineering documents.
