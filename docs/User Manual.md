# CinterForge Terminal — User Manual

**Your starting point for games, classic computers, Minecraft, coding and cross-platform development.**

CinterForge Terminal is a native application for **macOS, Windows and Linux**. You start in a tabbed Terminal window; from there, simple commands open game experiences, historic computer configurations, server tools and the Code-OSS workspace. The app also supplies a reusable WebView/Python application foundation for developers.

[Download CinterForge Terminal](../README.md#download-cinterforgeterminal) · [See the games](Game%20Catalog.md) · [Developer Tooling](Developer%20Tooling.md)

![Overview of CinterForge Terminal and its features](../CinterForgeterminal.png)

## 1. Getting started

1. Download the **macOS Apple-silicon**, **Windows x64** or **Linux x64** ZIP using the links at the top of the [README](../README.md).
2. Extract the archive and open its native application launcher. The Terminal launches into its main Session view.
3. Type `help` to discover commands, `games` to see the catalog, or `code` to enter the development workspace.
4. Navigate between Sessions using the application tabs. Games and consoles open in their own tabs rather than replacing your original terminal.

The three lightweight native applications are separate downloads. A fourth, **optional unified DevTools** archive is only needed for certain development and additional platform environments, not simply to play a game.

## 2. At a glance

| Feature | What it does | Start with |
|---|---|---|
| **68 game commands** | WarGames/JOSHUA plus 67 classic Macintosh games | `games` |
| **16 interactive-fiction titles** | Story-driven text games with a bundled JavaScript Z-machine player | `stories` |
| **48 classic-Mac configurations** | Browse and launch supported historical Macintosh emulator configurations | Museum / `infinite-mac` |
| **19 terminal styles** | Explore historical and modern screen aesthetics, distinct from real OS emulation | Museum / terminal styles |
| **Minecraft Bedrock** | Install/manage an official dedicated server and use its live command console | `bedrock help` |
| **Cross-platform shells** | Actual Mac, Windows and Linux sessions where supported environments are present | Native Terminal menu / Code-OSS |
| **Code-OSS** | Edit and save local projects, use integrated terminals and build tools | `code` |
| **Application platform** | Native WebView shell plus a supervised Python/HTTP backend | [Architecture](Architecture.md) |

The counts come from the checked-in catalog: the game commands, separate interactive-fiction titles, emulator configurations and visual profiles are **different lists**. A style is not a bootable emulator, and historical or third-party media may be separate from the app.

## 3. Commands you will use most

```text
help                 View Terminal commands and usage
help infinite-mac    Explore the classic Macintosh game commands
games                List the 68 game commands
wargames             Launch the original JOSHUA experience
stories              List 16 interactive-fiction titles
infinite-mac         List 67 classic Macintosh game commands
simcity              Example: open a classic Mac game
lagomorph            Launch the special Quadra 650 easter egg
bedrock help         See server operations
bedrock status       Check Bedrock install/run status
minecraft            Attach a live console to an already-running server
code                 Open Code-OSS
```

Type commands in an ordinary Terminal Session, not inside JOSHUA's game dialogue or an emulator's own keyboard buffer. Some commands are routed to separate sessions; they do not destroy or replace the session from which you launched them.

## 4. Games and interactive fiction

**Game #1: `wargames`.** This opens the existing JOSHUA experience, including its interactive game modes and its fictional WarGames narrative. Type `wargames` to launch it; its interface remains separate from the shell.

**Games #2–68: classic Macintosh.** Type `games` or `infinite-mac` to browse the available command aliases, then enter a title such as `simcity`, `loom`, `marathon`, `princeofpersia` or `theoregontrail`. The catalog uses specific machine/media profiles and may load a browser-hosted Infinite Mac experience. Network access or separately licensed game software may be required.

**Interactive fiction.** Type `stories` for 16 distinct text adventures. Depending on the title, you may need to install its authorized story content before launching it. The JavaScript Z-machine interpreter handles compatible stories and supports independently stored saved game state.

**Lagomorph.** Type `lagomorph` for the preserved special Quadra 650 experience. It uses its dedicated classic-Mac boot path, application and disk assets, Battles media and manual; it is not simply a generic terminal theme. Its guest boot and automation are separate from the normal game dispatch.

Browse the complete [Game Catalog](Game%20Catalog.md) for aliases and descriptions.

## 5. The Computer History Museum: emulators versus styles

The configuration data describes **48 classic Macintosh emulator configurations** and **19 visual terminal profiles**. Museum entries span early microcomputers, historical terminal designs and classic Macintosh systems, including Apple II, VT100, IBM PC, Macintosh, BeOS and NeXTSTEP references.

A **terminal style** changes the visual presentation, for example color, character columns and a period-appropriate appearance. A **real emulated system** requires an underlying emulator and, where relevant, system software or boot media. The Museum distinguishes these so a period-inspired theme is not presented as a running historical operating system. The exact selectable experiences depend on installed runtimes and available media.

## 6. Minecraft Bedrock Dedicated Server

CinterForge Terminal manages the official **Minecraft Bedrock Dedicated Server**. It provides world selection, a dedicated server Session with real output, console commands, and a second reconnectable console without launching a duplicate server.

### First-time setup

```text
bedrock status          Check server installation and status
bedrock eula            Review/accept the official EULA
bedrock install         Download and verify the official server after consent
bedrock start           Discover/select worlds; does NOT start a world
bedrock run "My World"  Start a named world in its own server Session
```

After the server reaches the ready state, use its **live Bedrock tab** to type real server-console commands such as `list`. From another Terminal Session, type `minecraft` to attach an additional console to that same running server. You can also use:

```text
bedrock command list    Send a server command
bedrock log             Open runtime/install diagnostics
bedrock restart         Restart the managed server session
bedrock stop            Stop the server gracefully
bedrock help            Show available Bedrock commands
```

**Important distinction:** `bedrock run "<world>"` starts a world; `minecraft` only **attaches** after the server is already running. The application should stop its owned Bedrock runtime and helpers on exit, without deleting player worlds or stopping unrelated virtual machines. On macOS the server uses the managed LinuxKit/Rosetta environment and LAN relay. Other platforms use their appropriate supported execution environments, and actual LAN connectivity depends on the host/network configuration. Minecraft itself and the official server binaries are third-party software with their own requirements and terms.

## 7. Real terminals on three platforms

CinterForge Terminal supports native platform shells and integrated developer sessions; the available options depend on the host operating system and installed support runtimes.

| Environment | Session behavior |
|---|---|
| **macOS** | Real macOS Bash/PTY is available without optional DevTools |
| **Windows** | Native Windows terminal using the packaged runtime and MSYS2 Bash where available |
| **Linux** | Real Linux shell/PTY on the Linux host |
| **Windows or Linux on a Mac** | Requires the supported guest environment and optional unified DevTools package; it never substitutes a disguised Mac shell |

The native menu can create supported terminal or Minecraft console sessions. It must not silently launch a second Bedrock server. To open the **Code-OSS** workbench, type `code`; platform developer terminals are integrated there as well. Ordinary commands such as `pwd`, `ls`, `cd`, Git or a compiler belong in a real shell, not in a game/emulator text field.

## 8. Code-OSS: edit, save and build projects

1. Type `code` in a Terminal Session.
2. Choose **Open Folder** or **Open Workspace** in Code-OSS.
3. Browse, edit and save files using the normal OS permissions for that folder.
4. Open a real integrated terminal for your host or installed guest environments.
5. Return to the saved workspace when you relaunch the workbench.

When your open folder is the `CinterForgeTerminal` source repository, its own **`Cinter`** lifecycle is available from the real developer shell. On macOS, the supported producer can build macOS, Windows and Linux releases; target-native GUI acceptance is a separate target-platform concern, not proof supplied by cross-compiling.

### Building the Terminal itself

```sh
./Cinter status all
./Cinter setup all
./Cinter build all
./Cinter stage all
./Cinter test mac
./Cinter all all
```

`./Cinter all all` runs **Setup → Build → Stage → Test** and **never publishes**. On Windows, the source repository provides a `Cinter.cmd` entry point. Setup handles prerequisites and preflight checks; Test invokes automatic regression and native acceptance checks applicable to the host. Persistent results are saved to sibling `Builds/Logs/CinterForgeTerminal/Lifecycle/`. You should not need to perform a collection of manual preflight commands in addition to the lifecycle.

## 9. Optional DevTools

The three applications are lightweight compared with the **one optional unified DevTools package** containing managed development tools across macOS, Windows and Linux. Where an additional guest/development environment needs it, Terminal presents the advertised download size, installation path and explicit confirmation. It verifies content and tool versions rather than treating extraction alone as success.

In an authorized developer shell, the documented command family is:

| Command | What happens |
|---|---|
| `devtools list` | Inspect tools and their availability by platform |
| `devtools install` | Review size/location; confirm; download with progress and verify contents/versions |
| `devtools uninstall` | Remove only the confirmed managed optional toolchain |
| `devtools report` | Produce a detailed tool/version report under `Builds/Logs/CinterForgeTerminal/` |

The package is not required merely to browse games, terminal styles or the core editor UI. For details, see [Developer Tooling](Developer%20Tooling.md).

## 10. Cloudflare downloads and updates

The [README download section](../README.md#download-cinterforgeterminal) has a platform-specific ZIP link for macOS, Windows and Linux, plus a link to a **release manifest** with checksums and the optional DevTools archive. It is managed by the repository's publishing code. When a verified public release finishes uploading and the public URLs check successfully, the README's managed download links are refreshed. The publisher does not automatically push commits to GitHub.

For maintainers, release publishing is explicit:

```sh
./Cinter publish public
./Cinter verify public
```

Cloudflare R2/CDN distributes the public release archives. Separately authorized private development and original-source material stays in its configured private bucket; public download URLs do not expose publisher credentials.

## 11. Build your own applications on this foundation

For contributors and integrators, Terminal is more than a UI. Its reusable architecture includes:

- A **native WebView app shell** across macOS (WKWebView), Windows (WebView2) and Linux (WebKitGTK).
- An **authenticated loopback HTTP service** exposing purpose-specific APIs to the UI.
- A **customizable Python service/backend** that supervises supported operations and sessions.
- Native **C++ Host** services, process/PTY management and a JavaScript-native bridge.
- A supported public `./Cinter` lifecycle and a linkable `CinterForgePythonHost` library with stable `cph_*` C ABI.
- Cloudflare release publication, versioned manifests and verified downloads.

Use the [Architecture](Architecture.md), [Integrating the Host](Integrating%20the%20Host.md), and [Build and Release](Build%20and%20Release.md) guides when developing an application on the foundation. Native capabilities are explicitly authorized; an arbitrary remote web page does not acquire workstation-wide file or process control.

## 12. Tips and troubleshooting

**A game isn't starting:** Check the exact command in `games` or `infinite-mac`, verify that its permitted media is available, and look for in-app error messages. A style preview is not equivalent to an installed emulator.

**`minecraft` reports no server:** Start a world first with `bedrock run "<world>"`. A second console cannot attach to a server that is stopped.

**A Windows/Linux shell is unavailable on macOS:** The real guest environment or optional DevTools may be missing. Follow the in-app install flow if offered; Terminal will not fake the platform shell.

**A build fails:** Rerun the appropriate `./Cinter` lifecycle command. Prerequisite checks occur during Setup; detailed PASS/FAIL transcripts and structured summaries remain under sibling `Builds/Logs/CinterForgeTerminal/Lifecycle/`.

**Security or content:** The official Bedrock server and third-party games, operating systems and images have their own licenses and access requirements. The local HTTP service is authenticated and loopback-only; do not open control routes on a public network.

## More information

[Games and Experiences](Game%20Catalog.md) · [Architecture](Architecture.md) · [Developer Tooling](Developer%20Tooling.md) · [Build and Release](Build%20and%20Release.md) · [Contributing](Contributing.md)
