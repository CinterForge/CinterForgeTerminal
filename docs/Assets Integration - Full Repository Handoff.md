> **Historical integration handoff (superseded 2026-11-19).** This documents the prior Converter-owned staging arrangement. The current owner and instructions are in [Assets and Content Packs](Assets%20and%20Content%20Packs.md). Do not build Converter to build Terminal.

# CinterForge Terminal — Assets Integration: Full Repository Handoff

**Integration date:** 2026-11-19  
**Status:** First testable Terminal asset import/report/view slice, backed by shared Converter and Server modules. **Not** the completed Blockify/pack-install feature.  
**Baseline:** User-supplied CinterForgeTerminal, Converter, Server and WebApp ZIPs, plus Steps 1/1.5 and 2 changes. No code from Hope or Plugins was copied or modified. The implementation follows `Hope/HopeDocs/AI Context` and `CinterForgeTerminal/docs/Architecture.md` boundaries.

## Deliverables

The **full Terminal source repository** contains all its authored source, not just a patch. A separate complete four-repository workspace archive contains the corresponding **Terminal, Converter, Server and WebApp** sources so Step 2's reusable engines are not missing. Neither archive contains the generated `Builds` products, credentials, VM disks, nor signed/native installed applications. Build/Stage/Test are required on the developer machine.

## Exact integration locations

| Repository and source file | Responsibility / change |
| --- | --- |
| `CinterForgeTerminal/webapp/runtime/CinterForgeAssets.py` | Nine-column `ContentPacks.csv` parser, opaque UCI IDs, safe canonical manifest validation, SHA-256 file inventory |
| `CinterForgeTerminal/webapp/runtime/CinterForgeAssetCloudflare.py` | Existing Host R2 transport adapter, direct per-file R2 uploads and HTTPS retrieval, SHA-256 inventory, non-destructive restore/conflict reporting |
| `CinterForgeTerminal/webapp/runtime/CinterForgeCapabilities.py` | Launch-time Cloudflare/Discord identity probes; fail-closed command grants |
| `CinterForgeTerminal/webapp/runtime/CinterForgeAssetViews.py` | **New Terminal asset bridge:** canonical ZIP/folder import, solid-only GLB/GLTF import with dependencies, UCI collision guard, Converter report/view URL generation, authenticated asset serving, chunked browser import |
| `CinterForgeTerminal/webapp/runtime/CinterForgeCommands.py` | `assets report`, `assets import [path]`, `assets view <uci>`, Cloudflare `assets setup/publish <channel>`, help and hidden-command dispatch |
| `CinterForgeTerminal/webapp/runtime/CinterForgeHTTP.py` | Owned asset action queue and browser upload session state in the existing HTTP server |
| `CinterForgeTerminal/webapp/runtime/CinterForgeHTTPRoutes.py` | `GET /api/assets/next`, `GET /assets-library/...` and authenticated `/api/assets/*` operations; CLI-token + live-session verification for host-path imports |
| `CinterForgeTerminal/webapp/client/CinterForgeAssetTabs.js` | **New client integration:** reusable report/UCI iframe tabs, WebView OS file picker, chunked SHA-256 upload and viewer selection |
| `CinterForgeTerminal/webapp/client/CinterForgeSessions.js` | Display, activate, preserve and close asset iframe tabs using the existing CraftMaster-compatible tab lifecycle |
| `CinterForgeTerminal/webapp/index.html` | Loads `CinterForgeAssetTabs.js` after tab state and CraftMaster map module |
| `CinterForgeTerminal/webapp/launcher/{index.html,app.js,style.css}` | Adds credential-hidden **Import Asset…** action for testing the standard file dialog from all three launcher presentations |
| `CinterForgeTerminal/webapp/client/CinterForgeLaunchers.js` | Validates same-origin launcher messages, queries validated capabilities, forwards file-picker request to parent WebView |
| `CinterForgeTerminal/host/lifecycle/assets_dependency.py` | Verifies the **built** Converter-owned gallery, pinned Babylon libraries and versioned four-module Blockify bundle before distributing to Terminal |
| `CinterForgeTerminal/host/python/manager.py` | Stages Converter viewer and Blockify bytes into `webapp/data/converter/` within the managed Host closure |
| `CinterForgeTerminal/host/lifecycle/universal_package.py` | Stages the same verified Converter assets for Linux/Windows release payloads; authored Terminal client/runtime source is dynamically inventoried |
| `CinterForgeTerminal/docs/Assets and Content Packs.md` | Canonical asset formats, CSV, Cloudflare, shared engine contracts and deferred functionality |
| `CinterForgeConverter/build/shared/blockify_engine.py` | SHA-256 package owner of the **original**, unchanged Babylon Blockify JS modules |
| `CinterForgeConverter/build/shared/package.py` | Builds a versioned Converter web release including the pinned viewer, engine and gallery generator |
| `CinterForgeConverter/api/asset_gallery.py` | **Existing authoritative gallery**, reused by Terminal; generates searchable `AssetGallery.html`, Solid/Block model views and animation playback |
| `CinterForgeWebApp/tools/{build_integrated_webapps.py,test_integrated_webapps.py}` | Consumes verified Converter engine bundles; detects tampering, preserves standalone IF compatibility |
| `CinterForgeServer/cf/ifserver/assets/asset_packaging.py` | Extracted existing complete Minecraft resource+behavior `.mcaddon` merger; preserves original UUIDs and content |
| `CinterForgeServer/cf/ifserver/assets/{asset_command.py,asset_migration_commands.py}` | Both historical migration entry points now delegate to one pack assembler |

### Module boundaries

1. **Terminal** owns native/application commands, authentication, file selection, browser tabs, and Bedrock installation orchestration. It does **not** implement a second voxelizer or animation algorithm.
2. **Converter** owns Babylon Blockify JS and the asset gallery renderer. Terminal consumes a **verified Build artifact** under sibling `Builds`, not sibling Converter source at installed runtime.
3. **Server** owns existing canonical UCI assets, Bedrock exporter and the merger of per-asset `.mcaddon` packages. No Server HTTP daemon is introduced into Terminal.
4. **WebApp / IFEditor** consumes the shared Converter engine when available; its unchanged compatibility files remain available for standalone builds.
5. **Plugins** retains its separate pack publication/domain responsibilities; these new Minecraft behavior packs are not reinterpreted as plugin marketplace Packs.

## Current command behavior

| Command | Implemented behavior | Notes |
| --- | --- | --- |
| `assets` / `assets help` | Shows available asset commands | Only if validated Cloudflare credentials make the command available |
| `assets import /absolute/path/asset.zip` | Imports one canonical UCI archive unchanged; indexes and opens/reselects a 3D viewer | Preserves `solid`, `block_32`, animations, textures and manifest; rejects conflicting UCI |
| `assets import /absolute/path/asset.glb` | Copies original model to a new solid-only UCI asset; opens viewer | Not yet voxelized; original file remains unchanged |
| `assets import /absolute/path/asset.gltf` | Copies GLTF and local referenced texture/buffer dependencies | Relative sibling dependency files must exist; use absolute host path |
| `assets import` | Opens a picker tab/control in the native Terminal WebView | Standard WebView file chooser supports a single `.zip`/`.glb`/`.gltf` file; for GLTF with external dependencies use the typed path or canonical ZIP |
| `assets view <uci>` | Opens/reselects one iframe per asset UCI | Converter gallery viewer currently offers Solid/Block and existing animation playback; **does not yet contain the full IFEditor Blockify editing control panel** |
| `assets report` | Refreshes Converter's searchable `AssetGallery.html` in a single reused tab | Uses **locally pinned** Babylon assets; never silently loads an online CDN copy |
| `assets setup internal\|beta\|release` | Downloads the committed individual Assets files by their original paths and preserves local edits | Credentials/real R2 access required |
| `assets publish internal\|beta\|release` | Uploads original Assets files individually to the existing R2 Assets folder; verifies SHA-256 and commits direct-file index last | This is a **real upload**; removes only previously generated obsolete bundle ZIPs after direct-file verification; no live upload was performed during this integration |
| `assets blockify <csv>` | **Not yet implemented** | No CSV-to-Babylon runtime/exporter adapter yet; intentionally not advertised |
| `packs`, `packs report`, `packs set` | **Not yet implemented** | Hidden, not fake working commands |
| `discord` | Existing command now requires a validated bot identity at launch | No credential is embedded in shell scripts or HTML |

**Pack installation contract:** The Bedrock world will have **two behavior packs** once `packs set` is implemented: the built-in CinterForge Comms behavior pack, and exactly one user-selected pack, plus the selected pack's matching resource pack. Selection will apply at the next Bedrock server launch only. This part is documented, **not yet installed by the new pack commands**.

## File and data layout

For a repository developer run:

```text
<workspace>/
├── CinterForgeConverter/                  # Engine/galleries owner
├── CinterForgeServer/                     # Minecraft asset/pack owner
├── CinterForgeWebApp/                     # IFEditor consumers
├── CinterForgeTerminal/                   # Commands/Host/UI owner
└── Builds/                                # Generated, never checked into repos
    ├── Dependencies/Babylon/9.26.1/      # Converter Setup owns pinned JS
    ├── CreativeSuite/Built/cfconverter/web/2026/
    │   ├── manifest.json
    │   ├── api/asset_gallery.py
    │   ├── build/browser-dependencies.json
    │   ├── frontend/vendor/{babylon.js,babylonjs.loaders.min.js}
    │   └── frontend/ifplayer/blockify/{CIFBlockify*.js,engine-manifest.json}
    └── Assets/
        ├── ContentPacks.csv              # Corrected nine-column user spreadsheet
        ├── <category>/<UCI>/manifest.json
        ├── <category>/<UCI>/solid/
        ├── <category>/<UCI>/block_32/
        ├── ContentPacks/<Pack Name>/<Pack Name>.mcaddon
        ├── _viewer/                      # Generated verified local viewer assets
        └── AssetGallery.html              # Generated, not authored
```

A packaged installed Terminal uses its runtime-selected mutable Assets root, not a repository `Builds` directory. The Host's verified Converter viewer modules are in its staged `webapp/data/converter/` subtree.

### Corrected CSV

The latest `ContentPacks.csv` has **512 asset rows and 512 unique UCI identifiers**; pack membership comes from `Pack Name`, **not** `Top Level Folder`. Two command-name collisions exist; the UCI remains the key. Place the supplied CSV at `Builds/Assets/ContentPacks.csv` when testing batch processing later. It is an **input asset**, not a source-code file that Build regenerates.

## How to run the first native test

On the developer Mac, with the repositories adjacent to their shared `Builds` folder:

```bash
cd CinterForgeConverter
./CC setup web        # Provisions version-pinned Babylon dependencies in sibling Builds
./CC build web        # Produces verified gallery + Blockify bundle

cd ../CinterForgeTerminal
./Cinter setup        # Only if this Terminal's normal Setup has not already run
./Cinter build
./Cinter stage
./Cinter test
```

Do not run Terminal Build/Stage against a missing or partial Converter bundle. Setup does not itself publish anything. Keep existing Terminal native Build/Xcode workflows and stage acceptance as the real gate.

In a Terminal Session **with working Cloudflare credentials detected at launch**:

```text
help
assets help
assets import "/absolute/path/CINTERASST20260927150819#DEFAULTCen.zip"
assets report
assets view "CINTERASST20260927150819#DEFAULTCen"
assets import
```

For the file-dialog test, open any Mac/Linux/Windows **Launcher** tab, then choose **Import Asset…** (the action appears only when the assets capability was validated). Choose the UCI ZIP or one GLB. The native WebView picker transfers the selected bytes to the same authenticated Terminal HTTP server; import completion reselects the asset viewer. The launcher's actual guest VNC does not get arbitrary host file-system access.

You can separately test the existing backup commands with real provider credentials:

```text
assets publish internal
assets setup internal
```

**Warning:** `assets publish` uploads the entire current Assets tree to your configured R2 bucket. Review the selected channel and your configured provider before issuing it. These commands were unit/offline tested only, **not** exercised against live Cloudflare.

## Verification performed for this drop

- Python `compileall` of relevant Terminal, Converter, Server, WebApp source modules: passed.
- Node `--check` of new/edited Terminal browser JavaScript: passed.
- Real supplied canonical UCI ZIP: imported with its preserved UCI, original `solid` and `block_32`, all 11 manifest-referenced members validated; repeated import deduplicated.
- Solid-only GLB UCI allocation; renderer command queued: passed. The smoke used a synthetic GLB header, **not** a full Babylon mesh-rendering acceptance.
- Converter gallery generated a two-asset HTML report using synthetic local viewer-file substitutes; this verifies URLs, queueing and model references **but not actual Babylon rendering**.
- Cross-category duplicate UCI and ZIP traversal rejection: passed.
- Converter's original Blockify animation contract and verified engine bundle tests: passed.
- Real *Beyond the Stars* `.mcaddon` pack reconstruction: **36 source assets, 315/315 matching nested file paths and SHA-256 values** against the supplied complete pack.

**Not tested here:** Xcode/native WKWebView `NSOpenPanel` behavior, native WebView2/WebKitGTK file pickers, complete GLB rendering, a real Cloudflare publish/restore, integrated Bedrock pack installation, or the 512-row Blockify batch. No native binaries or signed distribution packages are claimed in this source ZIP.

## Next work after the first test

1. Attach full IFEditor Blockify/animation editing controls to `assets view`, using the verified staged Converter engine and Server exporter; no duplicate algorithm.
2. Implement `assets blockify <csv>` using the corrected 512-row project schema, outputting completed packs to `Builds/Assets/ContentPacks/`.
3. Implement `packs` discovery, `packs report`, and `packs set` with the **two-behavior-pack** Bedrock contract.
4. Complete native Windows/Linux/Mac acceptance, explicit Cloudflare backup test, and actual pack launch/play tests.

## Troubleshooting boundaries

- **`assets` absent from `help`:** Cloudflare access failed the launch-time signed check. The command must stay hidden until a restart with valid configured credentials; do not install fake fallbacks.
- **"Converter gallery not staged" / "Pinned Babylon viewer unavailable":** Run `./CC setup web && ./CC build web` first, then rebuild/restage Terminal. Neither runtime nor Stage downloads a CDN JavaScript fallback.
- **`assets import <path>` denied:** This privileged path form requires a live local native Terminal session and its internal CLI token; remote browser users use the file-selection/upload path.
- **GLTF imported through picker lacks textures:** Import its original dependency directory using the absolute-path shell command, or package it as a canonical UCI ZIP.
- **Repeated asset view/report:** Tabs intentionally reuse their UCI/report identity and refresh the existing iframe.
- **No `packs` command:** This is intentional pending the pack implementation; CinterForge Comms remains the existing server-owned behavior pack.
