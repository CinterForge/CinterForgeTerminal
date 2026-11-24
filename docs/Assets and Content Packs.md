# CinterForge Terminal — Assets and Content Packs

## Current architecture and integration evidence (2026-11-20)

**Terminal owns the shared asset technology.** No Terminal command, lifecycle
step, viewer, or stage may require `./CC build web` or a checked-out
CinterForgeConverter/WebApp/Server repository. The existing authored Blockify
and animation algorithms were moved byte-for-byte from Converter/WebApp to:

- `CinterForgeTerminal/webapp/data/asset_engine/blockify/CIFBlockify.js`
- `CIFBlockifyPreview.js`, `CIFBlockifyBatch.js`, `CIFBlockifyMinecraftServer.js`
- `webapp/data/asset_engine/api/asset_gallery.py` (Babylon HTML viewer)
- `webapp/data/asset_engine/api/asset_packaging.py` (Bedrock pack assembler)
- `webapp/data/asset_engine/api/asset_minecraft.py` (original per-asset static/animated Bedrock exporter, extracted unchanged)
- `webapp/data/asset_engine/build/browser-dependencies.json` (pinned Babylon)

`./Cinter setup` provisions pinned Babylon bytes and Assimp/FFmpeg/FFprobe/
ImageMagick prebuilt tools inside sibling `Builds` and the managed Linux/Windows
toolchains. `./Cinter build` produces a SHA-256-verified shared engine under:

```
Builds/Shared/CinterForgeTerminal/Assets/2026/
```

`./Cinter stage` embeds the verified renderer, JS modules, Python modules and
Babylon dependencies into native Terminal applications for Mac/Linux/Windows.
Xcode Build & Run must call this same Build/Stage path; no Converter producer.
Optional DevTools holds the prebuilt media executables; the lightweight installed
Terminal should not recreate developer Builds on launch. `./Cinter test`
validates staged engine bytes and runs real miniature Assimp/FFmpeg/FFprobe/
ImageMagick conversions on the three platform toolchains. No media processing
occurs during Setup tests.

**Consumers:** Converter reads the immutable Terminal build and exposes its
queue, batch orchestration, drop UI and management interface; it does not own
Blockify JS, the Babylon gallery, or themed pack assembly. WebApp's IFPlayer
build injects the verified Terminal Blockify modules, and Server's legacy
pack command delegates to the Terminal-produced Python assembler after
verifying its SHA-256 receipt. The old duplicate authored Blockify scripts
were removed from Converter and WebApp. Those apps may be built **after**
Terminal as consumers, but building/running Terminal never requires them.

The original Server's asset-management and export *orchestration* remain
Server-specific. The actual static/animated per-asset Minecraft Add-On
writer is now Terminal-owned and Server calls it through a verified adapter. Terminal now bridges the installed native HTTP service to the **original**
CinterForgeServer saved-asset and migration commands and the CinterForgeWebApp
IFPlayer Asset Browser/Blockify batch scripts. The retained source closure is
under `webapp/data/asset_engine/original` and is staged with the pinned asset
engine receipt. This is an explicit build/stage source snapshot, not a runtime
checkout traversal, new UCI format, or new voxel algorithm. Server path-provider
and original saved-version UCI compatibility adaptors are recorded in that
snapshot. The shell adapter is `webapp/runtime/CinterForgeAssetOriginal.py`.

- `assets import <path>` uses the original Server saved-version writer and
  the original Plugins UCI algorithm for an imported GLB/glTF; preserves the
  imported bytes and existing versions; opens that UCI in the original Asset
  Viewer without silently changing it. The original `CIFBlockifyBatch` batch
  conversion runs only through the separate `assets blockify <CSV>` command.
  Canonical UCI folder/ZIP imports
  copy existing user data unchanged; ZIPs here are user-supplied import archives,
  **not** Cloudflare publication containers.
- `assets report` generates the existing Converter `AssetGallery.html` under
  the selected `Builds/Assets` root. `assets view <uci>` opens only that UCI in
  the original WebApp IFPlayer Babylon Asset Viewer, using original Server saved
  listings, metadata, animation and Blockbench operations. These are separate
  interfaces; neither opens the IFPlayer provider-search page as a report.
- `assets blockify <absolute CSV>` validates the existing ContentPacks columns,
  persists the CSV under `Builds/Assets/ContentPacks.csv` without replacing a
  different authored file, queues the exact WebApp `CIFBlockifyBatch` authoring
  implementation, streams progress, and delegates pack assembly to the original
  Server migration implementation. No new voxelizer or pack generator exists.

**Evidence limits:** Python import/UCI/lookup and source-versus-stage SHA checks
passed on a temporary Assets library. A real Chromium DOM check rendered the
original saved-asset card and action controls. This container's Chromium could
not initialize WebGL and the native Mac/Linux/Windows application was not run;
animated GLB playback, full voxelization, and installed-app acceptance therefore
remain unverified. An original Mixamo animation search index is required by
some original authoring paths. The Server's standalone Minecraft test-server
controls are not yet wired into Terminal's existing Bedrock lifecycle. Selected
user-pack installation and Converter queue/UI adaptation remain separate work.
Do not advertise unverified capabilities as accepted. The existing Bedrock Comms behavior pack remains installed
alongside the one user-selected behavior pack (two in total) once selection is
implemented. Neither this patch nor the offline tests is evidence of a live
Cloudflare publish or native Mac/Linux/Windows toolchain success.

## Canonical source data and outputs

`ContentPacks.csv` has nine columns (`Asset Name`, `Minecraft Name`,
`Command Name`, `Pack Name`, `Top Level Folder`, `UCI`, `Source Format`,
`Blockify Resolution`, `Notes`), 512 unique UCIs and two command-name
collisions. Preserve identities, animation/solid/block representations, and
CSV pack assignments; do not silently deduplicate command names. The source
example asset's `manifest.json`, `solid/` and `block_32/` are authoritative.
Use the existing complete two-nested-pack `.mcaddon` assembly semantics.

```
Builds/Assets/ContentPacks.csv
Builds/Assets/ContentPacks/<pack>/<pack>.mcaddon
Builds/Assets/<canonical UCI directories>
```

## Existing Assets library and Cloudflare contract

The developer's authoritative local asset library is the selected
`Builds/Assets`, including nested directories such as
`Animated/Mixamo/<UCI>` and `Animated/Fixed/<UCI>`. The installed lightweight
Terminal retains its Steam state root, but Assets report/import/view and
Assets setup/publish use the ONE explicitly selected DevTools Builds root when
configured. `CINTERFORGE_ASSETS_ROOT` is an optional explicit absolute-path
override. No automatic search of `~/Source`, no new Builds on app launch, and
no alternate asset libraries invented by the Host. Without an explicitly selected shared library, import, view and report
refuse to create another independent Assets tree under Steam and report the
selection error. The installed app's mutable Steam state remains for its own
runtime state only.

Cloudflare uses the **existing Host R2 provider, bucket mapping and provider
object-prefix**; Assets is a folder inside those buckets, not a bucket or a
new credential provider. This matches Server
`asset_transport._cloudflare_prefix()`:

- `internal`: existing private `cdn-assets-dev` bucket,
  `<configured-object-prefix>/assets/` (or `assets/` when there is no prefix).
- `beta`: existing public `cinterforge` bucket, same Assets prefix with a
  `beta/` subfolder to isolate the channel.
- `release`: existing public `cinterforge` bucket, the Assets root.

The historical Server implementation uses **lowercase `assets`** in R2 keys.
The local directory remains capitalized `Builds/Assets`; key spelling is
not normalized or renamed by Terminal. For example, a provider configured
with `object_prefix=cinterforge` uses the R2 key prefix
`cinterforge/assets` (or `cinterforge/assets/beta`), inside its existing
configured bucket. Never confuse this prefix with the bucket name.

`assets publish` uploads **every regular file and directory** beneath the selected
`Builds/Assets` tree, without a category, extension, name, provenance, or
hidden-file allowlist. Hidden metadata, provider downloads, arbitrary nested
folders, user ZIPs, and `GameIcons` are included. Files are uploaded
**individually** to the matching key in the existing R2 Assets folder.
Empty directories are included in the manifest and recreated by Setup.
Unsafe symlinks and paths fail explicitly instead of being skipped.
**Beta and release channels expose uploaded files publicly; review the complete
Assets folder for secrets, private content, and redistribution rights before
publishing.**

Files retain the original hierarchy, names, and web content types. For example,
`Builds/Assets/Animated/Mixamo/<UCI>/solid/model.glb` is stored at
`<provider-prefix>/assets/beta/Animated/Mixamo/<UCI>/solid/model.glb`
for beta, directly retrievable over the bucket's normal public HTTPS URL.
No ZIP bundle is generated or uploaded. The terminal prints file and byte
progress, downloads each newly uploaded object to confirm its SHA-256, then
commits `assets/index.json` with schema `cinterforge.assets-files.v1`.
Unchanged previously verified direct objects can be reused on later uploads.
Files previously indexed but now absent from the local tree are deleted from
that channel when publishing the new snapshot. Unindexed remote objects are
left untouched.

`assets setup` reads that **direct-file** index, fetches individual objects,
checks every size and SHA-256, and merges them into the local library without
overwriting user edits. It never extracts a ZIP or falls back to ZIP transport.
If no current direct index exists, Setup reports that the local library must
be published first; a failed first publication must not be treated as a
successful restore.

When converting a previously published bundle-based Assets channel, only the
**old publisher's generated ZIP bundles** are removed from the remote Assets
prefix, **after** the new direct files and index are verified. This includes
orphaned generated bundles left by an interrupted upload. It does not delete
unrelated/author-supplied ZIP files or other Assets data. There is no ZIP
compatibility path in new publications or restores. Stop any old publisher
before starting the direct-file publisher, to avoid simultaneous writes.
There is no `.cinterforge-asset-backup.json` marker and no new bucket.

- External credentials remain in their existing `cfdev/config.json` provider
  (or explicitly configured alternative); never write them into source, stage,
  browser pages or logs.
- Launch-time signed bucket checks and Discord bot identity checks determine
  advertised command availability. Missing credentials fail closed.
- This code has only been exercised with local transport compatibility tests;
  the user's actual R2 inventory, three native stages and remote CDN URLs have
  not been verified in this environment. Run Test after Stage, and verify
  actual remote inventory before publishing any library.

## Complete CSV Blockify output and authoring gate

The original `CIFBlockifyBatch.MigrateCatalogAsset` creates resolution-zero
Solid/animation resources without fabricating visible thumbnails. Nonzero
resolutions produce Solid GLTF/resources + thumbnail, Block GLTF/resources +
thumbnail, resolution-specific `edits.json`, original voxel/rig/animation
metadata, and an authored per-asset `.mcaddon`. The original Server saves the
UCI manifest, preserves original saved versions and Blockify edits, and stages
per-asset `MCAddons/<pack>/<command>/` resources with `asset.json`, previews,
artifacts and textures. Terminal then calls the original `asset-migration-build-packs`
operation, producing `MCAddons/<pack>/<pack>.mcaddon`; the Terminal Server
adapter distributes only the finished original pack to
`ContentPacks/<pack>/<pack>.mcaddon` without writing another Minecraft pack.
The original `asset-migration-pack-galleries` adds root/per-pack `index.html`,
viewer runtime, authored banner/thumbnail artwork and animated previews. The
existing Converter AssetGallery generates `Assets/AssetGallery.html`.

The original same-origin WebApp authoring requests and progress use a narrowly
authorized browser HTTP route with exact Origin and Fetch-Site checks; every
other privileged native API retains its existing token checks. The normal
Terminal 64 KiB control-message limit remains unchanged for other APIs. The
original JSON GLTF authoring command has a 256 MiB per-request safety cap.
Missing artwork, incomplete staged UCIs, assembly errors, or failed gallery
creation fail the batch and leave source and staged files for reconciliation;
no destructive pack reset or invented placeholder UCI is performed.

**Verification:** Source-level integration tests exercised a real complete
`DefaultCharacter` asset ZIP through original Server stage-existing, themed
pack `.mcaddon` assembly, and the Converter AssetGallery in temporary Builds.
The original Pack gallery generator was observed refusing absent pinned
Babylon in the source-only test; staged Babylon and all required pack artwork
were not supplied for a complete Pack-gallery acceptance. WebGL voxelization
and signed native macOS/Windows/Linux acceptance remain unverified.
