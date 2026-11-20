# CinterForge Terminal — Terminal-owned Asset Engine integration

Date: 2026-11-19

## Architecture

**Terminal is the provider.** Converter, WebApp/IFEditor and Server consume
Terminal-built, SHA-256-verified shared assets. Terminal's Setup, Build, Stage,
Test and Xcode Build & Run must never require a separately built Converter.
The source of truth is `CinterForgeTerminal/webapp/data/asset_engine/`.

## Source moved into Terminal

| Technology | Terminal-owned source | Origin |
| --- | --- | --- |
| Blockify & animation logic | `webapp/data/asset_engine/blockify/CIFBlockify.js` | unchanged WebApp/Converter original |
| Blockify preview | `.../CIFBlockifyPreview.js` | unchanged original |
| Batch Blockify | `.../CIFBlockifyBatch.js` | unchanged original |
| Minecraft browser Blockify | `.../CIFBlockifyMinecraftServer.js` | unchanged original |
| Babylon asset gallery | `webapp/data/asset_engine/api/asset_gallery.py` | Converter; adapted to Terminal paths, no online fallback |
| Bedrock themed pack assembly | `webapp/data/asset_engine/api/asset_packaging.py` | Server; existing assembly logic |
| Per-asset Bedrock static/animated writer | `webapp/data/asset_engine/api/asset_minecraft.py` | unchanged Server functions, now Terminal-owned |
| Browser dependency pins | `webapp/data/asset_engine/build/browser-dependencies.json` | pinned Babylon 9.26.1 |

Removed duplicate authored Blockify JS sources from Converter and WebApp.
The original four browser classes and behavior/animation algorithms are unchanged.

## Lifecycle integration

- `Cinter` Setup: `host/lifecycle/assets_dependency.py` provisions and SHA-256
  verifies pinned Babylon files in `Builds/Dependencies/Babylon/9.26.1`.
  Updated Setup-cache checks ensure prior Setup receipts cannot hide missing
  media binaries or an obsolete guest image.
- Mac Setup: `host/mac/mac_bootstrap.py` installs official prebuilt Conda
  Assimp **library and C API headers**, FFmpeg, FFprobe and ImageMagick in
  `Builds/Toolchains/mac/env`. Conda-forge Assimp on macOS ARM64 does not
  provide `bin/assimp`; Setup verifies the dylib, C headers and installed
  package receipt rather than incorrectly expecting that CLI.
- Linux Setup: `host/docker/windows-cross.Dockerfile` includes Debian
  `assimp-utils`, `ffmpeg`, `imagemagick` in the managed x86-64 image.
- Windows Setup: `host/windows/windows_install.py` installs UCRT64 MSYS2
  Assimp, FFmpeg and ImageMagick, and explicitly verifies FFprobe.
- Build: `host/python/manager.py` builds the own source bundle to
  `Builds/Shared/CinterForgeTerminal/Assets/2026` and verifies the complete
  manifest plus Blockify receipt, unchanged animation classes and pinned
  Babylon dependencies.
- Stage: `host/python/manager.py` and
  `host/lifecycle/universal_package.py` embed the verified engine in all
  three native apps. The media executable closures are kept in the optional
  unified DevTools archive, not forced into the lightweight app.
- Test: single `host/lifecycle/test.py` acceptance stage checks the verified
  Build and uses real Assimp OBJ import through a compiled native C API probe
  on macOS (the prebuilt dylib, not a simulated CLI), FFmpeg sample PNG creation, FFprobe
  validation and ImageMagick resize across macOS, Linux and Windows. It does
  not invent or mock executables. `devtools list`/`report` surface the tools.

## Consumer changes

- Converter `build/shared/blockify_engine.py` copies and verifies Terminal's
  released modules; `api/asset_gallery.py` and web packaging use Terminal's
  gallery. Converter does not own authored Blockify or pack conversion code.
- WebApp `tools/build_integrated_webapps.py` requires the Terminal Build
  receipt, verifies its entries and injects the Blockify classes in IFPlayer.
- Server `cf/ifserver/assets/asset_packaging.py` and the writer entry points
  in `asset_blockify.py` are compatibility adapters to Terminal-produced,
  hash-verified Bedrock pack and asset writers; historical commands
  still use their existing call signature.
- CinterForgeConverter's future responsibility is queues, UI, asset drop,
  batch automation and management rather than owning conversion algorithms.

## Verify in your real macOS workspace

```
cd CinterForgeTerminal
./Cinter setup
./Cinter build
./Cinter stage
./Cinter test
```

No manual Converter build is part of Terminal lifecycle. Xcode Build & Run
should use the same current Build/Stage outputs. Once Terminal is built,
Converter and WebApp can consume `Builds/Shared/CinterForgeTerminal/Assets/2026`.

## What was verified here

- Python compilation of touched components.
- Byte-for-byte equality of all four Terminal Blockify modules against the
  uploaded original WebApp source.
- Actual Node.js execution of the original Blockify animation-profile and
  inheritance contract.
- A real one-voxel OBJ-to-`.mcaddon` invocation of the Terminal writer,
  yielding one resources `.mcpack` and one behavior `.mcpack`.
- The uploaded *Beyond the Stars* real pack regression: 36 inputs, 315
  nested files identical (SHA-256) to the existing combined `.mcaddon`.
- WebApp fail-closed tests and tests not requiring actual native tools.

## Not yet established by this integration

- No live macOS Xcode Build, Linux container run, or Windows PE run in this
  environment. New tool installers and Test smoke require execution on the
  user's managed Mac and guests. These are implementation paths, not claims
  of a tested three-platform release.
- No live Cloudflare publish or backup was performed.
- The legacy Server `asset_management.py`, asset persistence/round-trip
  actions in `asset_blockify.py`, and transport retain Server-specific
  dependencies. The actual per-asset Bedrock writer and themed-pack assembler
  have moved into Terminal. Full source-side UCI persistence, round-trip
  editing and export orchestration still need extraction.
- Terminal GLB -> Blockify batch command, full 512-row CSV generation, and
  `packs set` to install the user-selected behavior pack alongside built-in
  Comms are not ready. Converter queue/UI remains to be updated to consume the
  new API. Do not expose unfinished commands in help.
- Assimp/FFmpeg/ImageMagick license obligations and binary distribution must
  be reviewed before public releases; Conda/OS packages supply actual prebuilt
  binaries and their dependency closures, not licenses to redistribute them.
- The three tool package versions depend on the Setup package repositories;
  explicit resolved Conda lock and MSYS2 closure receipts are recorded, but
  fully reproducible clean-version pinning across machines remains a follow-up.
