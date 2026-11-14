# Build and Release

If you want to get started right away:

```bash
./Cinter all all               # Setup -> Build -> Stage -> Test
./Cinter all all publish       # Same phases, then dryrun Publish; never uploads
./Cinter all win publish internal  # Only Windows: ZIP + MSI to private R2
./Cinter all mac publish beta     # macOS only to beta
./Cinter all linux publish internal  # Linux only to private R2
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
acceptance. The `all all publish` form runs the same phases and, only if Test succeeds,
packages a **dry run** without uploading. To upload explicitly, add `internal`,
`beta` or `release` after `publish`. A failed phase stops the sequence before
publication.

Code-OSS Setup prints its pinned Git, npm, compiler and Web-staging phases,
streams each child command's output into the normal lifecycle log, and reports
elapsed and time-since-last-output every 15 seconds. A child producing no output
for five minutes fails with its last output instead of silently blocking Setup;
Git clone/fetch has a shorter three-minute inactivity limit, a bounded overall
limit, and two HTTP/1.1 fetch attempts. If the pinned upstream commit is already
present in the verified local checkout, Setup skips the network fetch. A changed
CinterForge extension still triggers staging/verification, but macOS `.DS_Store`
metadata cannot invalidate an otherwise current Web product. An interrupted
Code-OSS Setup leaves the existing compiled/source caches available for the
next Setup attempt; no provisioning is moved into Build, Stage, or Test.

Windows Setup also prints START/PASS/FAIL lines and an elapsed-time heartbeat
every 15 seconds during blocking dependency checks, Wine initialization,
and native MSI compilation. These lines appear in the same lifecycle log; they are
not guessed completion percentages; the initial Wine boot now fails after 180 seconds if it cannot complete.

**Setup graphical preflight (both Linux and Windows):** After provisioning the
existing Linux sample and Windows cross images, and **before the first Wine
process**, Setup starts a temporary `Xvfb :99` screen in each isolated guest,
uses `xdpyinfo` to verify an actual 1280×800/24-bit X11 connection, connects
to `x11vnc` over loopback TCP and validates the RFB protocol banner, then
fetches `/vnc.html` from the guest's `websockify`/noVNC endpoint.  These are
real connection checks, not `--help` or executable-presence checks. Any
failure aborts Setup with the service logs and durable results at
`Builds/Logs/CinterForgeTerminal/{linux,windows}-gui-preflight.{log,json}`.
The checks run without published ports or external network access; all
preflight servers stop afterward. Every graphical Windows execution, including
`wineboot --init`, performs the same checks **inside its own container** and
runs Wine on the *verified live display* rather than another Xvfb instance.
This does not certify Windows PE or native GUI behavior; those are separate
acceptance gates. Build, Stage, Test and runtime never provision/repair these
services.

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
| **Setup** | Download/verify and **extract verified published ZIPs/DMGs only here**, into canonical sibling `Builds` when needed; provision pinned toolchains, **native Debian wixl/msitools and a real tiny README MSI smoke**, and run required acceptance | Leave Setup VMs running; shift extraction to Build or Stage |
| **Build** | Compile and sign three unpacked native apps; record the unified DevTools input inventory directly in `Builds` | ZIP/UnZIP, download, Stage or restart |
| **Stage** | Copy already-built unpacked files straight to `steamapps` and validate receipts; skip unchanged DevTools | ZIP/UnZIP, provision, download or rebuild |
| **Test** | Validate staged product offline and record evidence | ZIP/UnZIP, provision or download |
| **Publish** | Create the **Mac DMG, three ZIPs and Windows enterprise MSI only here**; notarize beta/release Mac DMGs; perform read-only CDN checks on dryrun, otherwise upload and verify | Run during ordinary `all all`, upload without an explicit destination or provision toolchains |
| **Reboot** | Restart only named running component: `server`, `docker`, `colima` or `app` | Build, Setup, Stage, ZIP/UnZIP or Publish |

Setup owns first-time toolchain provisioning and all extraction of verified published release ZIPs and Mac DMGs. Build produces an uncompressed four-target `four-built.json` inventory under `Builds/CinterForgeTerminal/Distribution/`; large DevTools source files stay in their canonical `Builds` locations without an extra 7 GB copy. Stage copies those files directly to Steam and verifies them, using fast unchanged-file receipts. Publish alone generates the Mac DMG, three distribution ZIPs, and an independently verified Windows x64 MSI. Reboot never compiles or packages.

## Publish channels (explicit upload authorization)

`./Cinter publish` and `./Cinter publish cfterminal` default to `dryrun`.
They prepare and verify the Mac DMG and the three ZIPs under sibling Builds,
then perform **read-only authenticated list checks** of both configured R2
buckets. Dryrun does **not** upload to either CDN, download the archives,
submit to Apple notarization, or edit README. It may reuse verified local
archives rather than recompressing unchanged DevTools.

| Mode | Destination | Purpose |
| --- | --- | --- |
| `dryrun` (default) | Local `Builds/CinterForgeTerminal/Distribution` | Package and verify both CDN connections; zero uploads |
| `internal` | `cdn-assets-dev` / `CinterForgeTerminal/Distribution/` | Internal developer/test builds |
| `beta` | `cinterforge` / `CinterForgeTerminal/Distribution/beta/` | Externally accessible test builds; verified unsigned HTTP |
| `release` | `cinterforge` / `CinterForgeTerminal/Distribution/` | Production/public release, unchanged stable URLs |

### Target-scoped lifecycle and Windows MSI

Every target selector is accepted in the same lifecycle form:

```bash
./Cinter all mac
./Cinter all win publish internal
./Cinter all linux publish beta
./Cinter all all publish release
```

`mac`, `win`, `linux` and `all` each run Setup → Build → Stage → Test,
with optional explicit Publish mode (`dryrun` if omitted). Targeted Build,
Stage, Test and Publish never package or upload other application platforms
or the optional **DevTools** archive. A Windows-only Setup provisions the
pinned Windows cross-build tools and MSI toolchain without LinuxKit/Bedrock
or Lagomorph Setup. The macOS/Linux Setup commands currently reuse the broader
common toolchain Setup, but still Build/Stage/Test/Publish their selected target.

**Native Debian `wixl` + `msitools`** are provisioned in the pinned Windows-cross
image **only during Setup**. Both are native Linux executables. WiX 3 Windows
binaries, Wine Mono and a PE32 Wine loader probe are not MSI compiler dependencies.
Setup compiles a *real tiny x64 MSI* from `README.txt`, checks Windows Installer
tables with `msiinfo`, and extracts the embedded README with `msiextract`. It also
compiles the identical minimal installer schema with the actual application
launcher filenames. No unsupported WiX UI extension or branding elements are used.
Results are recorded in
`Builds/Logs/CinterForgeTerminal/windows-msi-preflight.json` and
`Builds/CinterForgeTerminal/Toolchains/WindowsMSI/msi-preflight.json`.
Setup fails if those real acceptance checks fail; a tool version probe alone
cannot pass. Only Setup provisions or changes the OCI image; Publish checks the
existing staged image and Setup acceptance receipt. The other patched Wine 11.15
MSYS2 shell/runtime verification remains separate and unchanged, as do the
Setup-managed `Builds/CFTVM` Colima/Wine guest-native volume paths and the
180-second Wine prefix initialization timeout.

Publish consumes the **existing unpacked Windows Build receipt** to create
`Builds/CinterForgeTerminal/Distribution/CinterForgeTerminal-win.msi`.
The MSI defaults to **`Program Files\CinterForge\CinterForge Terminal`**
(64-bit per-machine install), uses deterministic per-version `ProductCode`
and a stable `UpgradeCode`, and registers itself in Windows **Installed Apps / Add
or Remove Programs** with normal MSI uninstall enabled. Its single Start-menu
shortcut is owned by the installed executable's MSI component and removed by
the standard `RemoveShortcuts` uninstall action. It has no separate shortcut
creation script or installer UI and does **not** contain optional DevTools.

Setup compiles the real template MSI and inspects its compiled `Property`,
`Directory`, `CustomAction`, `InstallExecuteSequence`, and `Shortcut` tables for
per-machine scope, a Program Files default, manufacturer/name/version/product
identity, `RegisterProduct`/`PublishProduct`, and managed shortcut creation and
removal. A Type 51 `SetARPINSTALLLOCATION` assignment resolves `[INSTALLDIR]`
after `CostFinalize`, so Windows Installer can populate InstallLocation for
Add/Remove Programs. Windows Installer itself creates its native uninstall
registry entry and `UninstallString`; the package does not write arbitrary
uninstall registry keys. **Wine registry visibility is not a release gate**:
Wine is not native Windows Installed Apps. The MSI Setup receipt records the
compiled table evidence; actual Windows installation, registration, placement,
Start-menu removal, upgrade, and uninstall must be checked on a Windows PC
before promoting the MSI to production. Publish refuses an invalid Setup
receipt, but the receipt must not be represented as native Windows acceptance.

The old Windows ZIP remains available for compatibility.
On Apple Silicon, Setup also provisions pinned `msitools=0.106` through the
existing micromamba-managed macOS prefix under `Builds`, without Homebrew or
global installs. Setup compiles and inspects an independent native ARM64 smoke
MSI and the full installer template. Once this acceptance is recorded in
`Builds/CinterForgeTerminal/Toolchains/WindowsMSI/mac-native-msi-preflight.json`,
Publish uses native `wixl`, `msiinfo` and `msiextract`, avoiding Docker/QEMU for
MSI assembly. Publish **never installs tools**. Older Setup receipts, other
hosts and native compiler failures use the already-provisioned Debian AMD64
Docker toolchain as fallback. The MSI input fingerprint is based on verified
SHA-256 content, release and packaging recipe, not timestamps, so identical
Windows builds reuse the same verified MSI. A changed or damaged MSI is rebuilt
in clean scratch and verified before atomically replacing the distribution MSI.
Actual Windows installation, upgrade and uninstall acceptance remain required
before production deployment. These packages are not Windows code-signed
unless a separate signing identity is configured and verified.

Target-only publishing writes `mac-artifacts.json`, `linux-artifacts.json`, or
`win-artifacts.json` to the existing channel root; separate local
`<target>-packages.json` receipts cache unchanged archives without modifying the
original all-platform `four-artifacts.json`. Windows `win-artifacts.json`
covers the ZIP and MSI. Full publishing retains `four-artifacts.json` for the
original four products and publishes the MSI with an auxiliary scoped
manifest; it verifies the MSI **before uploading any full-release artifacts**.
Only changed scoped artifacts need R2 transfer/round-trip checks, and DevTools
are not consulted in a scoped run. Internal testing publishes to private
`cdn-assets-dev/CinterForgeTerminal/Distribution/`.

Only `beta` and `release` demand public notarization/Gatekeeper verification
of the Mac DMG. Only `release` can update the managed public README download
section and production Lagomorph sidecar. The beta folder has its own
`four-artifacts.json`, isolated from production. Both CDN publications verify
R2 content by SHA-256 after upload, and public channels additionally verify
unsigned HTTP delivery. Use `./Cinter verify beta` or `./Cinter verify public`
if public CDN propagation is delayed; these checks do not re-upload packages.

For repeated publication, the verified local DevTools ZIP is reused using its
Build content fingerprint and archive identity, without another multi-GB hash
or recompression pass. When publishing from a different BUILDER without the
local DevTools ZIP, explicit channels consult only the existing small R2
manifest: a matching Build fingerprint, object size and R2 modification-order
check allow the already published ZIP to be referenced directly, with **no
local ZIP creation, SDK upload or SDK download**. When a Mac/Linux/Windows
package changes, only changed packages are transferred and SHA-checked; the
previously verified DevTools object remains untouched. Any SDK input change or
insufficient remote verification makes Publish create and verify a new ZIP.
Dryrun still prepares/verifies all four local artifacts and never uploads.

The commands remain atomic: no implicit Build, Stage or Setup during Publish.
For combined runs, `./Cinter all all publish` is **still dryrun**;
`./Cinter all all publish beta` and `./Cinter all all publish release` are
explicit uploads, and execute only after all earlier phases pass.

## macOS direct distribution: signed drag-to-Applications DMG

Local `./Cinter all all` may use an ad-hoc macOS signature if no
Developer ID is configured; this is appropriate for local development,
**not** for public downloads. The Xcode project's **Release** Signing &
Capabilities configuration selects the Developer ID Application certificate
and Team for both Xcode and `./Cinter` publication, including in the embedded
Mac Bash shell. The CLI resolves Xcode's macOS SDK-specific signing settings
against valid identities in the host user's Keychain; no duplicate certificate
or manually exported signing-identity variable is required. The private key
never leaves Keychain.

Apple notarization separately requires an authenticated `xcrun notarytool`
Keychain profile. The Xcode Release `CINTER_MAC_NOTARY_PROFILE` setting names
that profile; it is **not** the app provisioning profile. On an explicit public
beta/release Publish, the pipeline checks Xcode's selected profile first, then
its previously validated per-machine profile name under sibling
`Builds/CinterForgeTerminal/Configuration/notary-profile.json`, then the
standard `CinterForgeTerminal` profile name. It validates access to Apple's
notary service **before any MSI packaging or CDN upload**. If the profile is
missing, interactive Mac Bash offers to reuse another stored profile or runs
Apple's own password-masked `notarytool store-credentials` prompt once for the
Xcode-selected Team. The private key and notarization credentials remain in
the user's macOS Keychain; only a nonsecret profile name is remembered in
Builds. A noninteractive builder with no profile fails before packaging. A
network/authentication failure never overwrites stored credentials or causes
an unnotarized public upload. No standalone shell setup command is required;
`./Cinter publish beta` completes this flow inside CinterForgeTerminal.

```sh
./Cinter all all publish release  # explicit full lifecycle and public Release
# Or, separately, from the same integrated Mac Bash shell:
# ./Cinter all all
# ./Cinter publish cfterminal release
```

Build signs the final filtered macOS packaging copy with the Developer ID,
hardened runtime and timestamp, submits a temporary ZIP to Apple notarization,
staples the accepted ticket to a **Publish-only copy** of the already-signed
Build app, revalidates its signature, then builds `CinterForgeTerminal-mac.dmg`
with the app icon, an `/Applications` shortcut, and an installation `README.md`.
Publish notarizes and staples the final DMG, verifies its mounted contents and
Gatekeeper status, and only then uploads the manifest and image.
Stage directly copies the already-signed unpacked Build app. Public Publish
independently checks the *final DMG's mounted app* with `codesign`, `stapler validate` and Gatekeeper before
uploading. A missing identity/profile or rejected notarization must fail
explicitly; it must never silently publish an ad-hoc developer build.

The first actual downloaded DMG and Mac application must still be tested under normal
Gatekeeper quarantine. Notarization and DMG-level checks alone are not a
substitute for native interactive acceptance.

## Repository commands

```sh
./Cinter setup all              # prepare and verify the complete source producer closure
./Cinter build cfterminal all  # compile/sign four unpacked outputs under Builds
./Cinter stage all              # copy validated unpacked files directly into steamapps
./Cinter reboot server          # restart Python and reload JS/HTML/CSS WebView
./Cinter reboot docker          # restart owned engine and both reusable guests
./Cinter reboot colima          # restart owned VM, Docker and both guests
./Cinter reboot app             # restart already-built native application
./Cinter test mac               # target-native macOS acceptance
./Cinter status all             # read-only package/stage status
./Cinter all all                # Setup -> Build -> Stage -> Test; NO publish
./Cinter all all publish        # Setup -> Build -> Stage -> Test -> local dryrun only
./Cinter all all publish beta   # explicit beta upload after Test PASS
./Cinter all all publish release # explicit production upload after Test PASS
./Cinter all mac                # macOS-only Build / Stage / Test after shared Setup
./Cinter all win publish internal   # Windows ZIP + MSI only, private Cloudflare
./Cinter all linux publish internal # Linux ZIP only, private Cloudflare
./Cinter all mac publish beta       # Mac DMG only, public beta
./Cinter windows cross-test     # Windows PE / Wine cross-compatibility evidence
./Cinter publish                # default dryrun: package + read-only test of both CDNs
./Cinter publish cfterminal dryrun  # same explicit dryrun
./Cinter publish cfterminal internal # internal CDN; not public
./Cinter publish cfterminal beta    # public CDN /beta/ folder
./Cinter publish cfterminal release # existing public production location
./Cinter fetch all              # verified private development retrieval
./Cinter fetch public           # verified public retrieval
```

On Windows, the equivalent supported source bootstrap is `Cinter.cmd`; the installed Terminal application is not the repository bootstrap script. Native Host diagnostics and build actions are available under `./Cinter host ...`; there is no separate `CPH` launcher. App-specific publishing, such as Lagomorph media, remains an explicit, separately scoped operation and is not silently included in a normal Terminal app publish.

**`all all` never publishes, and `all all publish` is dryrun-only.** Only `all all publish internal|beta|release` performs an upload, after Test succeeds. The beta and release paths both enforce the notarized-macOS-release gate, SHA-256 checks and unsigned public HTTP verification. Neither dryrun nor beta changes the production README download links. A failed Setup, Build, Stage, Test, or Publish exits nonzero and is captured in the normal lifecycle log. Successful automated Test does not by itself establish interactive native Windows/Linux GUI acceptance. This applies in the Code-OSS workbench as well. A Code-OSS workspace with `CinterForgeTerminal` loaded can edit any permitted source file and invoke this **same** lifecycle through its real Mac, Win and Linux integrated terminals. The host's producer creates all three target applications; genuinely native Windows and Linux GUI acceptance still takes place on their respective target systems.

## Setup from empty generated state

The producer assumes the authored Terminal repository and documented external inputs exist, but **does not assume pre-existing VM disks, caches, OCI images, LinuxKit images, Python installations or local Docker services**. It uses Xcode as the only assumed preinstalled Mac development suite, with normal OS security/virtualization authorizations.

1. Resolve `<repo>/../Builds` and all generated paths, establish pinned source, tools and archive hashes, verify input licenses and acquire the supported toolchain closure.
2. Mac Bootstrap provisions Xcode-dependent tools, signed Lima, Docker CLI/Buildx, LinuxKit CLI and pinned, prebuilt native macOS ARM64 QEMU (both `qemu-img` and `qemu-system-x86_64`) **without starting a VM**. LinuxKit Setup starts the single `c` Docker producer for the ARM64 kernel and guest runtime inputs. Use short physical Lima/Colima paths under `Builds/CFTVM/U`, pinned tools and signed/entitled VM-host processes. No Docker Desktop/Homebrew dependency.
3. Compile/verify the **custom ARM64 LinuxKit kernel with VirtioFS enabled**, package its kernel/initrd/cmdline and Mac LAN relayd launcher, and refresh its generated guest manifest when inputs change. Cache hits require hash/receipt validation; clean state builds from pinned source.
4. Build/verify the ARM64 kernel, guest glibc and ARM64 NetherNet probe through the **same** `c` producer; release `c` before the actual LinuxKit/Bedrock guest acceptance. After Bedrock passes, start the separately managed QEMU **x86-64** `t` Docker guest and build the single Debian AMD64 Linux/Windows image there, not through ARM64 `c` binfmt/Rosetta. Reuse `t` for cross-build, Wine/MSYS2, Linux Bash and both VNC acceptances; release it on success, failure or interruption. Do not introduce a `w` VM or depend on CFOS's comprehensive Ubuntu image.
5. Provision/check portable Python and native runtime closures for each release target, the Code-OSS web workbench and extensions, JS Z-machine, game/IF catalogs and native WebView dependencies. The pinned Code-OSS browser producer skips upstream `extensions/copilot` during its nested npm dependency pass: that GitHub-only extension is excluded from the web product, and its optional platform-specific SDK is not a Setup prerequisite. All other pinned extension dependencies and the CinterForge-owned browser extension remain intact. Connecting a local AI provider (including Pleias) is separate from provisioning the editor; this does not claim that chat inference is configured. For Lagomorph, **pull the complete authored `Builds/Lagomorph` tree from the authenticated internal CDN before generating its HFS disks**. Download a single `Builds-Lagomorph.zip` snapshot from the internal `cdn-assets-dev` R2 bucket; verify its committed manifest, whole-ZIP SHA-256, each file hash and empty directories before updating the local source. Previously published per-file snapshots remain readable for migration. A missing snapshot is a Setup error; the source pull must not contact public release media. Respect independent licensed media inputs rather than fabricating substitutes.
6. With **explicit official Bedrock EULA acceptance**, download/verify the official test archive, start a disposable isolated Bedrock server through the signed LinuxKit guest, inspect readiness, console and LAN behavior, stop the server/guest, and record evidence. Never touch the player's worlds during this producer test.
7. Write versioned setup manifests and logs. On success, error, interrupt or owning-process death, stop the owned `c` LinuxKit producer, `t` AMD64 producer and temporary children; leave unrelated VMs untouched. Persistent managed caches may be retained for the next validated build.

Producer-time official Bedrock acceptance is separate from **end-user Bedrock installation**, which occurs on explicit request in the installed Game Terminal only after the user's own EULA acceptance. No Bedrock executable is bundled as a general-purpose freely redistributable native app component.

## Build: four unpacked outputs (no ZIP)

| Target | Output |
|---|---|
| **macOS** | Signed native app and bundled runtime files |
| **Windows** | Native executable, WebView2 and MSYS2 Bash runtime |
| **Linux** | Native ELF, GTK/WebKitGTK runtime closure |
| **DevTools** | Existing pinned multi-platform toolchains, referenced in-place under `Builds` |

Build creates `Builds/CinterForgeTerminal/Distribution/four-built.json`, containing hashes, paths, modes and fingerprints for all four outputs. It does not compress or extract anything. In particular, it does not duplicate the multi-gigabyte DevTools closure. Signing the filtered macOS app belongs to Build; notarization's temporary upload archive belongs to Publish.

## Stage: direct copy and verify (no ZIP)

`stage all` checks the Build inventory and copies its unpacked files into:

```text
<steamapps>/common/CinterForge/Applications/
  macOS/CinterForge Terminal/<release>/
  Windows/CinterForge Terminal/<release>/
  Linux/CinterForge Terminal/<release>/
  DevTools/CinterForge Terminal/<release>/
```

Unchanged DevTools files are not copied again. Stage preserves unrelated files and existing user data and retains the macOS app replacement/rollback mechanism. It never downloads, extracts or creates an archive.

## Reboot and application-owned guest lifetime

The native app checks for installed DevTools on launch (and after installation). If available, it starts one dedicated managed Colima `t` profile and Docker engine, with exactly one persistent Debian AMD64 container providing both native Linux Bash and Wine/MSYS2 Windows Bash. Every Terminal and Code-OSS session uses `docker exec` into that same guest with a separate platform PTY; tabs never create a new VM/container. The host filesystem and mounted volumes are made available inside guests, with macOS enforcing host permissions.

The native owner cleans up its single shared Debian guest container, Colima and Python services when quitting. An independent process watches the native app to recover ownership after Xcode Stop or a crash. Unrelated containers and Setup's `c` producer are never terminated. `reboot docker` restarts Docker and guests, `reboot colima` restarts Colima/Docker/guests, `reboot server` swaps the Python service and reloads the WebView, and `reboot app` relaunches already-built native binaries. Development `reboot server` reloads Python and authored web assets directly from the current repository without recompilation. Reboot never invokes Setup, Build, Stage, Test, Publish, ZIP or UnZIP.

The `t` profile has **one writable macOS host-root QEMU/9p share**, mapped to `/host/mac` in the VM and the one shared Debian guest container. This is not a folder allowlist: host paths, including `/Volumes/<disk name>` and paths containing spaces, retain their normal directory hierarchy. Do **not** add nested `/Volumes` or individual-drive shares on top of `/`: Lima rejects overlapping mount locations. Access continues to depend on the host's real filesystem permissions and on which mounted volumes the QEMU/9p mount actually exposes; Terminal never attempts to override those OS decisions. An old disposable `t` profile with overlapping shares is reconfigured at next startup, after checking that no unrelated containers are active. If the host virtualization layer refuses an external volume, that limitation cannot be solved by changing a Terminal-side folder permission.

A new native owner waits for the previous crash guardian to complete Colima cleanup before starting its guests. **`reboot server` keeps the same native app PID and reuses its existing guardian**, so a Python service reload cannot lock itself out or create another VM. If the native worker or HTTP listener dies unexpectedly, the native window closes and the guardian finishes cleanup. During shutdown, if the host Docker socket is unavailable, cleanup inspects the dedicated Colima profile over SSH before stopping it; uncertain or unrelated workloads produce an explicit failure rather than being killed blindly. No app-level restart triggers provisioning or any repository lifecycle phase.

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

`four-artifacts.json` describes the **one macOS DMG + Linux/Windows ZIPs + one unified DevTools ZIP** with sizes, SHA-256 digests, release IDs and target metadata. Explicit private/public Publish selects the configured Cloudflare destination and checks remote results; Fetch verifies the same manifest and bytes before use. Client applications obtain public downloads without redistributing publisher secrets.

For Lagomorph, `./Cinter publish lagomorph public` uploads a single ZIP of the complete original `Builds/Lagomorph` tree to internal `cdn-assets-dev` and its staged ISO plus HFS floppies to the public `cinterforge` bucket. `./Cinter pull lagomorph source` retrieves and verifies only the internal source; this command is automatically part of Setup and `./Cinter all all`. `./Cinter pull lagomorph` explicitly restores internal source **and** published HFS media. The internal source snapshot is independent of the public application release number. Publishing remains explicit; Setup, Build, Stage and Test never upload artwork.

Game/media publishing—including the existing **Lagomorph** image/manual/Battles distribution—is independent and license-gated. Never publish private Steamworks assets, unlicensed Macintosh ROMs/system images, commercial game media, private repositories, or raw Build caches as public releases.

### README download links and publish completion

The first section of the public repository `README.md` contains three **current
native application download links** (macOS arm64, Windows x64, Linux x64),
versioned from the public `four-artifacts.json` release. The separately
installable DevTools archive is referenced through the release manifest, but
is not a fourth application link.

The repository entry point `./Cinter publish cfterminal release` uploads and SHA-checks the
four release archives, verifies access to the **public** HTTPS download URLs,
then updates the README links **locally** to that verified release. The download
block is bounded by stable comments so an update cannot overwrite other README
content. Repeated publication is idempotent and never runs `git push`.

If Cloudflare propagation is delayed (for example, an initial HTTP 403), the
publisher keeps its authenticated verified R2 receipt and leaves README
unchanged. `./Cinter verify public` retries the public HTTP checks without
reuploading release archives and updates README after success. The internal
and beta channels, dryrun, ordinary Build/Stage and Lagomorph-only publication
do not change production README download links. The operator reviews and commits the README
change to Git; public release publication does not automatically publish
source-code changes.

Before committing, inspect `git diff -- README.md` and verify the three URLs
using a browser or the approved public HTTP test. A release should not be
advertised as live merely because the source-level URL tests passed.

## Repository layout, logs and support

Only authored source and public documentation belong in this repository. All SDKs, Python environments, clones, caches, VM disks, logs, intermediate products and release ZIPs/DMGs are generated within sibling `<workspace>/Builds` (or documented installed game/runtime data within `steamapps`). No hidden home-directory cache is a mandatory input. Toolchain and acceptance diagnostics are under `Builds/Logs/CinterForgeTerminal` and named related `Builds/Logs` paths. `status all` is read-only and never upgrades a missing or untested artifact into a successful receipt.

## Standard direct-download Mac DMG convention

For every CinterForge macOS application distributed outside the Mac App Store:

- Build produces a **signed `.app`** with its product logo in `Contents/Resources/AppIcon.icns`; local Stage copies this unpacked app directly and never mounts a DMG.
- Explicit **Publish** alone creates `Product-mac.dmg` with the signed app, `Applications` symlink, and `README.md`. On public releases it notarizes/staples the DMG and verifies the mounted app. The resulting checksum and size go into the release manifest before Cloudflare upload.
- **Setup** alone downloads a verified published DMG, mounts it read-only into a managed `Builds/Intermediates` location and copies the app into the normal `Builds` location. Setup unmounts it even on failure.
- No Developer ID private keys, provisioning profile files or notary credentials ship in the image. Apple Keychain and Xcode own credentials; the existing `CINTER_MAC_SIGN_IDENTITY` / `CINTER_MAC_NOTARY_PROFILE` references remain supported.
- This DMG is a drag-to-Applications **disk image**, not a `.pkg` installer; `Developer ID Application` signs the app, while `Developer ID Installer` is reserved for `.pkg`.

The reusable image helper lives in `host/mac_dmg.py`. It accepts the app path, volume name, README, and target path so the same packaging convention can be applied to other CinterForge Mac applications without introducing another lifecycle phase.

### Lagomorph HFS build on the existing Linux guest

On macOS, Setup reads the author-owned classic Macintosh files and their
FinderInfo/resource forks using the existing `ditto`/AppleDouble process. It
converts them to MacBinary II in sibling `Builds/Intermediates`. The **existing
`t` Debian AMD64 image** (already provisioned for Linux Bash/Wine/MSYS2)
then assembles all three HFS Standard volumes in one network-disabled Docker
execution, round-trips the classic application and verifies its resource fork.
It uses the official Debian Bookworm `hfsutils` 3.2.6-15 AMD64 `.deb` (SHA-256
pinned to Debian's published hash) because Debian Trixie does not ship this
package. The 78 KB `.deb` is downloaded and verified by Mac Setup and extracted
**inside Linux**, with no Homebrew, no host-global installation, no extra VM,
and no Wine-image rebuild. Source assets remain unchanged; generated media are
published only with the separate Lagomorph publish command.

### Diagnostic beta — broad Hardened Runtime experiment

`./Cinter all all publish beta` signs the macOS Beta with **all six** Xcode
Hardened Runtime exceptions enabled at once (JIT, unsigned executable memory,
DYLD variables, disable library validation, disable executable page protection,
and Debugging Tool). The same applies to an explicit Mac-only Beta Publish.
These exceptions apply to the diagnostic Beta only: ordinary Builds and
`publish release` do not enable them. The `Debugging Tool` entitlement is
`com.apple.security.cs.debugger`, **not** `get-task-allow`, which can cause
Apple notarization to reject a release. The final signing and notarization
still must succeed; the Beta is not considered verified until the actual
signed customer artifact passes the existing release checks. Never ship this
broad-exception signing policy in production.
