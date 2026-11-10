# Build and Release

If you want to get started right away:

```bash
./Cinter all all               # Setup -> Build -> Stage -> Test
```

CinterForge Terminal is a reproducibly source-built product for **macOS, Windows and Linux**, with a separate optional unified DevTools distribution. A clean developer producer can regenerate its required outputs from an authored checkout and an empty sibling `Builds` tree; a consumer who installs the app does **not** run developer Setup.

This chapter describes the **repository build driver**. The Game Terminal's game commands and the `code` command are described in the [User Manual](User%20Manual.md). When this repository is loaded in Code-OSS, these same `./Cinter` commands run from real integrated platform terminals.

## Automatic preflight, regression tests and durable lifecycle logs

Builders run `./Cinter` lifecycle commands only; there are no separate
manual preflight or source-test commands. `setup all` verifies the authored
repository paths, input JSON and Python source syntax and provisions required
dependencies before Build. `test mac` runs Python and JavaScript regressions
before its native macOS UI acceptance. `all all` performs Setup → Build → Stage
→ Test on the Mac without publishing or claiming Windows/Linux native GUI
acceptance.

Each lifecycle command streams its output to the console **and** writes a
UTC timestamped `.log` plus a PASS/FAIL `.json` summary under
`<workspace>/Builds/Logs/CinterForgeTerminal/Lifecycle/`. Stable
`latest-<command>.json` summaries point to the corresponding full logs.
Failures remain available after console output scrolls away; `status all`
remains read-only and does not create logs. See [Lifecycle Testing](Lifecycle%20Testing.md).

## Lifecycle: one owner for each stage

| Phase | Responsibility | Must not do |
|---|---|---|
| **Inspect** | Identify source, platform/ABI, rights and licenses, required inputs and hashes; report readiness | Change an installed user environment as an implicit side effect |
| **Setup** | Download/provision pinned source and tools, create and verify all missing prerequisites, compile the **custom LinuxKit VirtioFS kernel and guest inputs**, sign the native VM launcher, provision portable runtimes, prepare JavaScript/Code-OSS/game inputs, and run a **real, isolated Bedrock acceptance** after explicit EULA consent | Leave owned temporary VMs or background helpers running; assume warm caches or an unrelated CFOS checkout |
| **Build** | Compile/link the reusable Host and three native apps; assemble full relocatable runtime closures; sign and create **exactly four finished ZIPs** with manifests and checksums | Download/compile missing Setup dependencies; mutate source inputs; defer app signing to Stage |
| **Stage** | Verify the four finished ZIPs and their manifests/signatures as applicable; extract their bytes into the designated installation layout and write receipts | Download, patch, compile, sign, regenerate guests, or modify release ZIP contents |
| **Test** | Launch the **staged** product and execute real native/behavioral tests; record target-specific evidence | Replace native GUI testing with a Wine/headless pass, or run Setup/Build behind the scenes |
| **Publish** | Explicitly upload the verified release products and manifest to the selected configured channel, then verify remote objects | Run implicitly as part of `all all`, or rebuild an archive after signing |
| **Fetch** | Restore a verified published artifact into its declared `Builds` location | Treat an unverified download as a source/build substitute |

Setup owns even expensive first-time toolchain and guest provisioning. Build may start an **already provisioned** cross-builder to compile but may not install its dependencies. Stage is strictly a verify-and-extract step, so its installed contents are byte-for-byte those produced by Build. The mutable developer work tree can be rebuilt and restaged at the same release label; signed/released ZIPs are always assembled afresh by Build and remain immutable once produced.

## Repository commands

```sh
./Cinter setup all              # prepare and verify the complete source producer closure
./Cinter build all              # build, sign and package 3 native apps + unified DevTools
./Cinter stage all              # validate/extract packages without modifying them
./Cinter test mac               # target-native macOS acceptance
./Cinter status all             # read-only package/stage status
./Cinter all all                # Setup -> Build -> Stage -> Test; NO publish
./Cinter all mac                # macOS-focused local lifecycle
./Cinter windows cross-test     # Windows PE / Wine cross-compatibility evidence
./Cinter publish all            # explicit configured private development publication
./Cinter publish public         # explicit configured public release publication
./Cinter fetch all              # verified private development retrieval
./Cinter fetch public           # verified public retrieval
```

On Windows, the equivalent supported source bootstrap is `Cinter.cmd`; the installed Terminal application is not the repository bootstrap script. Native Host diagnostics and build actions are available under `./Cinter host ...`; there is no separate `CPH` launcher. App-specific publishing, such as Lagomorph media, remains an explicit, separately scoped operation and is not silently included in a normal Terminal app publish.

**`all all` never publishes**—including when invoked from the Code-OSS workbench. A Code-OSS workspace with `CinterForgeTerminal` loaded can edit any permitted source file and invoke this **same** lifecycle through its real Mac, Win and Linux integrated terminals. The host's producer creates all three target applications; genuinely native Windows and Linux GUI acceptance still takes place on their respective target systems.

## Setup from empty generated state

The producer assumes the authored Terminal repository and documented external inputs exist, but **does not assume pre-existing VM disks, caches, OCI images, LinuxKit images, Python installations or local Docker services**. It uses Xcode as the only assumed preinstalled Mac development suite, with normal OS security/virtualization authorizations.

1. Resolve `<repo>/../Builds` and all generated paths, establish pinned source, tools and archive hashes, verify input licenses and acquire the supported toolchain closure.
2. Provision managed macOS builders independently: **`w`** (Windows cross-build/Docker, including the LinuxKit kernel image producer) and **`c`** (ARM64 Ubuntu guest-runtime inputs). Use short Lima/Colima paths under `Builds/CFTVM`, exact configured tools, and signed/entitled VM-host processes. No host-global Docker/Homebrew environment is required.
3. Compile/verify the **custom ARM64 LinuxKit kernel with VirtioFS enabled**, package its kernel/initrd/cmdline and Mac LAN relayd launcher, and refresh its generated guest manifest when inputs change. Cache hits require hash/receipt validation; clean state builds from pinned source.
4. Release `w` before starting `c` where the sequence requires it. Build/verify the guest glibc and native ARM64 NetherNet probe through `c`; release `c` before launching the actual LinuxKit guest. Do not combine the two builder VM profiles or substitute CFOS's comprehensive Ubuntu image.
5. Provision/check portable Python and native runtime closures for each release target, the Code-OSS web workbench and extensions, JS Z-machine, game/IF catalogs and native WebView dependencies. For Lagomorph, **pull the complete authored `Builds/Lagomorph` tree from the authenticated internal CDN before generating its HFS disks**. Download a single `Builds-Lagomorph.zip` snapshot from the internal `cdn-assets-dev` R2 bucket; verify its committed manifest, whole-ZIP SHA-256, each file hash and empty directories before updating the local source. Previously published per-file snapshots remain readable for migration. A missing snapshot is a Setup error; the source pull must not contact public release media. Respect independent licensed media inputs rather than fabricating substitutes.
6. With **explicit official Bedrock EULA acceptance**, download/verify the official test archive, start a disposable isolated Bedrock server through the signed LinuxKit guest, inspect readiness, console and LAN behavior, stop the server/guest, and record evidence. Never touch the player's worlds during this producer test.
7. Write versioned setup manifests and logs. On success, error, interrupt or owning-process death, stop the owned `w`/`c` profiles and temporary children; leave unrelated VMs untouched. Persistent managed caches may be retained for the next validated build.

Producer-time official Bedrock acceptance is separate from **end-user Bedrock installation**, which occurs on explicit request in the installed Game Terminal only after the user's own EULA acceptance. No Bedrock executable is bundled as a general-purpose freely redistributable native app component.

## Build: exactly four release ZIPs

| Product | Contents |
|---|---|
| **macOS Terminal** | Signed native **CinterForge Terminal.app**, WebView/Host/UI and required portable runtime closure |
| **Windows Terminal** | Native Windows executable, WebView2/Host/runtime and actual Windows MSYS2 Bash components needed for installed use |
| **Linux Terminal** | Native ELF application, GTK/WebKitGTK/Host/runtime closure |
| **Unified DevTools** | **One** separately installable package with pinned tools and development inputs for **all three** targets |

No obsolete single multi-platform app bundle. The application ZIPs are lightweight compared with the optional unified DevTools archive. Release inputs, target formats and native runtime dependencies are checked. Packaged Windows Bash must include the pinned CPython 3.11 `pywinpty` binding, real MSYS2 PE executables and dependent DLLs; the presence of archive filenames alone is not acceptance. Signed Mac contents cannot be changed after ZIP creation.

## Stage: verify and extract

`stage all` validates release-manifest members, hashes, supported target formats and signatures, then extracts the **already finished archives** under the documented Steam-compatible installation tree:

```text
<steamapps>/common/CinterForge/Applications/
  macOS/CinterForge Terminal/<release>/
  Windows/CinterForge Terminal/<release>/
  Linux/CinterForge Terminal/<release>/
  DevTools/CinterForge Terminal/<release>/
```

Stage may refresh an already installed developer release in place, preserving unrelated files, user worlds and saves. It cannot repair missing inputs by rebuilding, resigning or downloading components. Exact paths and receipts are described by the release manifest. The display name is **CinterForge Terminal** with spaces; internal source/build identifiers remain `CinterForgeTerminal`.

## Test: observable application acceptance

Test the **actual staged native app**, not a loose sample window. The required behaviors include:

- Mac `WKWebView`, Windows WebView2 and Linux WebKitGTK actual launch, keyboard/focus/clipboard, native JS callbacks and the native File/Edit/New Terminal menu.
- Default **Game Terminal** with modified WarGames/JOSHUA, direct game commands, IF story launch/saves, Infinite Mac, museum navigation and unmodified Lagomorph easter egg.
- `code` → Code-OSS open folder/workspace, real file save, relaunch persistence, Mac three integrated platform terminals and Windows/Linux one native terminal; genuine in-workspace `./Cinter` operations.
- `devtools list|install|uninstall|report`, including confirmation, download size/path, hash and tool-version checks, the Markdown report and preservation of user content during removal.
- Bedrock runtime/EULA, official server launch, live Minecraft console attachment/re-attachment, server commands, host-relayed LAN connectivity, and shutdown of exactly the app-owned LinuxKit VM and helpers.
- Real per-platform package launch and exit, no orphaned owned children, no source-checkout dependency and no accidental implicit Publish.

A Mac-target-native run cannot prove Windows/Linux GUI acceptance. A Wine PE test and Linux Xvfb smoke are valuable evidence but do not replace real WebView2 or a native Linux desktop session. Test reports are generated under `Builds/Logs`, not committed as public design docs.

## Publishing, verification and distribution

`four-artifacts.json` describes the **three native applications + one unified DevTools archive** with sizes, SHA-256 digests, release IDs and target metadata. Explicit private/public Publish selects the configured Cloudflare destination and checks remote results; Fetch verifies the same manifest and bytes before use. Client applications obtain public downloads without redistributing publisher secrets.

For Lagomorph, `./Cinter publish lagomorph public` uploads a single ZIP of the complete original `Builds/Lagomorph` tree to internal `cdn-assets-dev` and its staged ISO plus HFS floppies to the public `cinterforge` bucket. `./Cinter pull lagomorph source` retrieves and verifies only the internal source; this command is automatically part of Setup and `./Cinter all all`. `./Cinter pull lagomorph` explicitly restores internal source **and** published HFS media. The internal source snapshot is independent of the public application release number. Publishing remains explicit; Setup, Build, Stage and Test never upload artwork.

Game/media publishing—including the existing **Lagomorph** image/manual/Battles distribution—is independent and license-gated. Never publish private Steamworks assets, unlicensed Macintosh ROMs/system images, commercial game media, private repositories, or raw Build caches as public releases.

### README download links and publish completion

The first section of the public repository `README.md` contains three **current
native application download links** (macOS arm64, Windows x64, Linux x64),
versioned from the public `four-artifacts.json` release. The separately
installable DevTools archive is referenced through the release manifest, but
is not a fourth application link.

The repository entry point `./Cinter publish public` uploads and SHA-checks the
four release archives, verifies access to the **public** HTTPS download URLs,
then updates the README links **locally** to that verified release. The download
block is bounded by stable comments so an update cannot overwrite other README
content. Repeated publication is idempotent and never runs `git push`.

If Cloudflare propagation is delayed (for example, an initial HTTP 403), the
publisher keeps its authenticated verified R2 receipt and leaves README
unchanged. `./Cinter verify public` retries the public HTTP checks without
reuploading release archives and updates README after success. Private dev
`./Cinter publish all`, ordinary Build/Stage and Lagomorph-only publication do
not change README download links. The operator reviews and commits the README
change to Git; public release publication does not automatically publish
source-code changes.

Before committing, inspect `git diff -- README.md` and verify the three URLs
using a browser or the approved public HTTP test. A release should not be
advertised as live merely because the source-level URL tests passed.

## Repository layout, logs and support

Only authored source and public documentation belong in this repository. All SDKs, Python environments, clones, caches, VM disks, logs, intermediate products and release ZIPs are generated within sibling `<workspace>/Builds` (or documented installed game/runtime data within `steamapps`). No hidden home-directory cache is a mandatory input. Toolchain and acceptance diagnostics are under `Builds/Logs/CinterForgeTerminal` and named related `Builds/Logs` paths. `status all` is read-only and never upgrades a missing or untested artifact into a successful receipt.
