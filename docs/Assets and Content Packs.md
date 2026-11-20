# CinterForge Terminal — Assets and Content Packs

## Current architecture (2026-11-19)

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
writer is now Terminal-owned and Server calls it through a verified adapter. Terminal already has UCI ZIP import,
reports, view tabs, and Cloudflare backup, but complete local GLB-to-Blockify
execution, CSV batch conversion, selected user-pack installation, and Converter
queue/UI adaptation remain follow-on integration work. Do not advertise
unfinished commands. The existing Bedrock Comms behavior pack remains installed
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
no alternate asset libraries invented by the Host. Without an explicitly
selected library, Terminal falls back to the installed writable state root; a
missing library error includes the exact directory it checked.

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

`assets publish` uploads every file beneath the selected `Builds/Assets`
**individually** to the matching key in the existing R2 Assets folder. Files
retain the original hierarchy, names, and web content types. For example,
`Builds/Assets/Animated/Mixamo/<UCI>/solid/model.glb` is stored at
`<provider-prefix>/assets/beta/Animated/Mixamo/<UCI>/solid/model.glb`
for beta, directly retrievable over the bucket's normal public HTTPS URL.
No ZIP bundle is generated or uploaded. The terminal prints file and byte
progress, downloads each newly uploaded object to confirm its SHA-256, then
commits `assets/index.json` with schema `cinterforge.assets-files.v1`.
Unchanged previously verified direct objects can be reused on later uploads.
Publishing refuses to drop files from an existing direct inventory merely
because the local tree is incomplete.

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
