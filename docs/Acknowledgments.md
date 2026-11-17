# CinterForge Terminal · Acknowledgments and third-party work

This reference identifies notable projects, code, services, and media that CinterForge Terminal builds on, calls, or can retrieve. It is a communications inventory, not a substitute for the license files and notices shipped with each release. Verify each item against the exact release artifact before publication.

## Product code and frameworks

| Project / contributor | How Terminal uses it | Credit and license reference |
|---|---|---|
| **JOSHUA Terminal · Marco Iannacone / pippo.com** | WarGames-inspired JOSHUA experience; the Terminal opens this separate web application. | Source NOTICE credits Marco Iannacone and the original project at [pippo.com](https://pippo.com/). The source carries **AGPL-3.0-only**. It is an independent tribute to *WarGames* and is not affiliated with the film’s makers or rights holders. Confirm the notice and corresponding source obligations for the release. |
| **Infinite Mac · Mihai Parparita** | External browser-hosted classic Macintosh emulator and game library; Terminal launches or embeds the service and uses a catalog based on its library. | [Infinite Mac](https://infinitemac.org/) · [upstream source](https://github.com/mihaip/infinite-mac). Terminal does not claim authorship of its emulator or media. Emulated system software and game images have separate rights. |
| **webview/webview** | Cross-platform C++ wrapper for the native system WebView used by the app shell. | [Source repository and license](https://github.com/webview/webview). Upstream repository identifies the project license as MIT. |
| **Microsoft Code-OSS / VS Code repository** | Source base for the Code-OSS development workbench staged with Terminal. | [Microsoft VS Code source repository](https://github.com/microsoft/vscode) and its license/third-party notices. Credit the open-source Code-OSS source; do not imply Terminal bundles Microsoft’s branded Visual Studio Code distribution. |
| **Daniel Lockard · `daniellockard/zmachine`** | JavaScript Z-machine interpreter bundled for interactive-fiction play. | [Source repository](https://github.com/daniellockard/zmachine); upstream identifies MIT licensing. Preserve the upstream license notice in the release. |
| **Matplotlib contributors** | The JOSHUA world-map SVG records Matplotlib 3.10.8 in its embedded metadata. | [Matplotlib](https://matplotlib.org/) and its license. Verify whether the map’s underlying geographic data has separate attribution requirements. |

## On-demand experiences and media

- **Interactive fiction story files:** the catalog retrieves 16 story binaries on request from the [Interactive Fiction Archive](https://ifarchive.org/) and other declared sources, including the `historicalsource` GitHub repositories for Zork I–III. The IF Archive says donated files remain under their authors’ copyright unless the file or description says otherwise. Each story needs title-level author and rights verification; an archive URL alone is not a license grant.
- **Classic Macintosh games, operating-system software, ROMs, and disk images:** the Terminal contains launch/configuration metadata and connects to Infinite Mac; each game or system image has its own rightsholder and terms. Do not imply these media are included or cleared for redistribution.
- **Lagomorph:** the dedicated Quadra 650 experience references separate application, system, disk, Battles, manual, and icon media. Its materials have independent license and publication requirements and need their own clearance record.
- **Minecraft Bedrock Dedicated Server:** Terminal can download Microsoft/Mojang’s official server after the user reviews and accepts the EULA. Minecraft names, software, and assets remain subject to their own terms. Server metadata is sourced from EndstoneMC’s bedrock-server-data project; identify its exact notice/license in the shipped notices.
- **WarGames:** JOSHUA is an independent tribute. *WarGames* and related film marks, story, characters, music, and footage remain the rights of their respective owners. Do not use film clips, soundtrack, or marks in campaign assets without clearance.

## Build and optional tooling inputs

These are build/runtime inputs rather than the headline creative experience. Verify which are present in each final archive, which are used only during production, and which are installed only with optional DevTools:

- [Astral Python Build Standalone](https://github.com/astral-sh/python-build-standalone) provides pinned portable CPython runtimes.
- [LinuxKit](https://github.com/linuxkit/linuxkit), [Lima](https://github.com/lima-vm/lima), [Colima](https://github.com/abiosoft/colima), and Apple virtualization/WebKit frameworks support selected build, guest, or platform paths.
- **Microsoft WebView2** is the Windows native browser runtime/SDK; macOS uses Apple WKWebView and Linux uses WebKitGTK.
- **MSYS2**, **PowerShell**, `pywinpty`, **Node.js**, CMake, Go and other toolchains appear in build or optional development workflows. Include only the components actually redistributed in the applicable release and preserve each component’s license/notices.

This list is a first-pass inventory from source references. It is not yet a complete Software Bill of Materials (SBOM), does not validate every version’s license obligations, and does not clear third-party art, game media, or trademarks. The release engineering owner should reconcile it with the generated manifests, Code-OSS `ThirdPartyNotices.txt`, each bundled component’s license, the exact game/story catalogs, and platform-specific archives.

## Release-specific review checklist

- Match every entry to the exact release tag, archive, and platform.
- Record component version, source URL, license, redistribution status, and notice location.
- Distinguish included code, download-on-demand content, external hosted services, and build-only tools.
- Complete title-level credits and rights checks for all 16 interactive-fiction stories and any media shown in video or screenshots.
- Verify JOSHUA AGPL-3.0-only compliance and retain required notices/source offers.
- Confirm Code-OSS and WebView notices are present in each relevant archive.
- Add a generated SBOM or release manifest reference once engineering publishes one.
