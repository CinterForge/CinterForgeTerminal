# Developer Tooling

CinterForge Terminal is usable as a **game application without a developer SDK**. All ordinary development—Bash/PowerShell, compilers, source editing, diagnostics, tool installation and the complete `Cinter` lifecycle—runs inside the **Code-OSS workbench**, launched by `code` from the Game Terminal.

There is **one optional unified DevTools release** containing the platform-specific developer toolchain closure for macOS, Windows **and** Linux. It is not three unrelated SDK downloads and is not bundled into each lightweight native Terminal application.

## Managed bootstrap environments

| Producer/target | Required host baseline | Managed Setup closure |
|---|---|---|
| macOS arm64 producer | Xcode, macOS native virtualization authorization | Pinned Python, Node.js, Go, CMake, Ninja, pkg-config, Git LFS, Docker CLI/Buildx, signed/entitled Lima and Colima, archivers, native WebView sources and applicable native compilers |
| Windows x64 | Supported Windows bootstrap (`Cinter.cmd`) | Real MSYS2 UCRT64 shell, GCC/G++, CMake, Ninja, Python, Node.js, WebView2 SDK/runtime and required DLLs |
| Linux amd64 | Supported Linux build baseline | GCC/G++, POSIX shell, CMake, Ninja, Python, Node.js, GTK/WebKitGTK and native Host/build dependencies |
| macOS-hosted Windows/Linux cross-build | Managed toolchains from the Mac producer | ARM64 `c` for LinuxKit only; after Bedrock, QEMU x86-64 `t` for the Debian Linux/Windows MinGW/Wine/Xvfb/GTK OCI image |

**Git LFS is a managed Setup dependency** (pinned conda-forge `git-lfs=3.8.0`), not a host prerequisite. It must be present in the Setup PATH before the pinned Code-OSS Git checkout triggers upstream hooks. Existing prefixes missing Git LFS are reconciled during Setup; Build, Stage and Test do not provision it.

**Tool acquisition is Setup's responsibility.** Setup also executes the same temporary X11/RFB/noVNC connection preflight in **both** the Linux and Windows guest images before starting Wine, with separate retained `Builds/Logs/CinterForgeTerminal/{linux,windows}-gui-preflight.{log,json}` results. Package presence alone is insufficient to pass. Never depend on a contributor's arbitrary Homebrew, Docker Desktop or global Python install. Generated tools, profiles, caches, logs and reports live in `<repo>/../Builds`. Setup uses `c` for LinuxKit/Bedrock inputs, stops it before Bedrock acceptance, then uses the one x86-64 `t` guest for Linux/Windows provisioning and tests. Both are stopped when the owning lifecycle exits; they must not run together. On macOS, the VM-hosting binary must have a verified `com.apple.security.virtualization` entitlement; Setup handles reproducible provision/verification rather than requiring manual binary patching.

**Managed macOS Colima `c` Setup boot:** Mac Bootstrap stages and validates tools without starting Colima. LinuxKit Setup starts the existing `c` Docker producer, streams the live Lima/Colima boot output, records `Builds/Logs/CinterForgeTerminal/linuxkit-colima-c-start.log`, and requires Docker readiness before compiling the kernel and guest inputs. An incomplete boot is a failure; it does not launch an unrelated `w` VM or fabricate a readiness marker.

Platform tools have different executable ABIs. A single unified package means a verified distribution of the **complete three-platform tool set**, not that Linux, Windows and Mac binaries all run natively on every host. Code-OSS exposes only real installed execution environments.

### Managed guest acceptance, connection reuse, and lifetime

On a macOS release producer, `./Cinter setup all` now finishes by exercising the **actual runtime `t` Colima path** after releasing the single `c` producer. It starts one Debian AMD64 guest with separate Linux and Windows/Wine sessions, verifies the real interactive Linux Bash and Windows MSYS2 Bash PTYs by executing commands through each PTY, starts each platform's own X11/VNC/noVNC service in the shared Debian guest, verifies both RFB handshakes and both HTTP viewer endpoints, and **stops the Setup-owned PTYs, the one container and its VM in all completion paths**. Results are kept under sibling `Builds/Logs/CinterForgeTerminal/guest-connections-setup.{json,log}` (with PTY transcripts). `./Cinter test mac` checks this receipt read-only and fails if any connection or shutdown check is missing or failed; it does not provision, restart or download anything.

When a **receipt-verified unified DevTools** package is installed, the **native app** acquires one exclusive lifetime watchdog, then starts the single `t` QEMU x86-64 VM with one Debian AMD64 container serving both platforms. It creates exactly **one persistent interactive Bash PTY per platform** and keeps these alive independently of the Code-OSS tab. Opening/reopening Code-OSS attaches to these existing PTYs without `docker run`, without a fresh Bash process and without a second VM; closing Code-OSS detaches but does not terminate them. Unexpected PTY exit is repaired only inside the same owned guest, with bounded attempts and a visible Server Log error if reconnect fails. A periodic VNC health check restarts only missing helpers inside that same shared guest, recording persistent failures in the Server Log. Linux and Windows graphical Launcher Sessions attach **only to the already-running VNC endpoints**, never to Bash; the VNC helpers run on separate X displays inside the one owned Debian container and are idempotent. Native Mac Bash is independent and remains available on partial guest startup failures.

On Quit, the Host closes the PTYs, removes the one labeled owned container (thereby terminating both desktops and Wine helpers), then stops the owned `t` VM. An independent process holding exclusive ownership of the same `t` profile also performs VM/container cleanup if the native app dies; it serializes subsequent app launches until cleanup completes. Never stop unrelated Docker containers or another application's Colima profile. **The VM and guest readiness acceptance above is Mac-only and must actually pass on the Mac builder; no Linux-host simulation is a substitute.**

### Guest Bash inside the single Code-OSS workbench

**New Mac Bash** creates a native macOS PTY tab immediately; opening it never probes or installs DevTools. Selecting **Open Linux Bash** or **Open Windows Bash** opens or focuses one platform-specific integrated terminal **inside Code-OSS**, not a Session tab. Only these non-Mac selections check the receipt-backed unified DevTools package. When missing, the requested Code-OSS terminal explains the missing runtime, displays the release's real advertised compressed size and installation destination, asks the user to type `INSTALL`, then streams download, verification, extraction and startup progress. Declining does not download tools or replace the original Session. If installed, the prompt is skipped.

The optional multi-GiB public download uses bounded **64 MiB HTTPS byte-range requests** with a validated `Content-Range` and bounded automatic retries for interrupted TLS/network reads, including `BrokenPipeError`. Received bytes are saved under the selected Steam library's versioned `Downloads/<release>/` root as a `.partial` file; a retry or separately authorized subsequent installation resumes from that byte offset rather than silently starting over. A failed download does not install an SDK or damage an existing Mac session. Completion requires the published whole-archive SHA-256 check **before** extraction and the existing per-member validation and version checks. The installer only removes the partial archive after a completed install; cancellation before download leaves no new archive. Native network/CDN acceptance is required to establish reliable multi-GiB delivery; small in-memory fault injection does not prove the public download succeeds.

On Apple Silicon, these Code-OSS integrated guest terminals use the DevTools-bundled macOS Docker/Colima/Lima tools, a Terminal-owned managed guest profile named `t`, and the verified Linux/amd64 OCI image or the Windows MSYS2 Bash environment under Wine. **These are real guest processes, not a macOS Bash prompt with a Windows/Linux label.** The developer runtime's VM scratch lives under sibling `Builds/CFTVM/T`; an installed app uses `~/Library/Caches/CFTVM/T` and NEVER creates a developer Builds tree. In both cases the app-owned VM stops when the owning Terminal application ends. It reuses the architecture of Setup's post-Bedrock x86-64 `t` producer, but an installed app owns its own cache-backed `t` state; it never adopts or modifies the developer Build VM. If the installed SDK is incomplete, a guest cannot start, or its version cannot be verified, the requested Code-OSS terminal reports that failure instead of silently falling back to Mac Bash. Mac Bash runs directly on the host without installing DevTools. The Linux and Windows Launchers are separate singleton graphical Session tabs, opened on demand and displaying application windows launched from the same guest environment. They are not additional Bash sessions.


## Developer commands inside Code-OSS

| Command | Contract |
|---|---|
| `devtools list` | Enumerate the **individual tools** with version, target platform and installed/available state. Report missing or unusable items plainly. |
| `devtools install` | Show the unified package's **exact advertised size in GB**, destination and channel; request explicit confirmation; download/install with progress, SHA-256 and safe extraction; verify actual tools and versions before success. |
| `devtools uninstall` | Show the exact **managed DevTools installation** to be removed, request confirmation and remove it safely. Do not remove the lightweight Terminal, essential shell/runtime components, source checkouts, user files, games or saves. |
| `devtools report` | Generate a detailed **Markdown** report under `Builds/Logs/CinterForgeTerminal/` for Mac/Win/Linux tool lists, versions, paths, size, checksums, availability, missing inputs and verification results; show/open its path in Code-OSS. |

The older `install devtools` spelling may be retained as a compatibility alias. The canonical interface is **`devtools <install|uninstall|list|report>`**. These commands require an authorized developer session, not the default Game Terminal.

## Local folder and workspace workflow

1. In the Terminal shell, enter `code` (or select the existing Code-OSS machine). The first workspace is the **host user's `~/Source`**, if it exists, otherwise **`~/Documents`** (the home directory is a final fallback when neither exists).
2. Inside Code-OSS, open the Command Palette and run **CinterForge: Open Local Folder…** or **CinterForge: Open Local Workspace…**. Enter an **absolute path on the Terminal host** to a directory or an existing `.code-workspace` file. The browser-hosted workbench uses a path prompt, not an invented OS-native folder dialog. Multi-root workspaces expose each named local folder in Explorer.
3. Browse, edit, and save files using the current host user's normal filesystem permissions. Closing and reopening Code-OSS restores the last selected folder/workspace and the dark/light preference. Dark is the initial default; use **CinterForge: Toggle Light/Dark Mode** or the clickable theme control in Code-OSS's status bar to switch.
4. The integrated terminals start automatically in the selected workspace directory. **Mac: Mac shell, read-only Server Log, Linux shell, Windows shell (four tabs)**. **Windows/Linux: host shell and read-only Server Log (two tabs)**. Mac-hosted Linux/Windows are each limited to one integrated shell and use actual optional DevTools guest environments, not rebranded Mac Bash; Code-OSS mounts the selected host folder at `/workspace` in each guest.
The native Mac Bash Terminal Sessions use the same selected workspace root as Code-OSS (normally the source-repository parent). On macOS the outer HTML menubar is hidden by default because the native system menu owns those commands. For development inspection, type `htmlmenus show`, `htmlmenus hide` or `htmlmenus toggle` in a Mac shell Session; this only changes visibility of the existing HTML row. Linux/Windows retain one HTML menu row directly above the Session tabs.

5. Edit and save authored files, use Git and shell tools, then invoke the repository's supported `./Cinter setup|build|stage|test|all|status|publish|fetch` commands directly. Publishing is separately explicit.

A workspace associated with a random folder cannot magically acquire Terminal release commands: the workbench identifies the repository by trusted real source/entry point and scoped permissions. On a Mac producer, the same lifecycle produces all three app releases; the target OS still supplies target-native GUI acceptance.

## Useful repository diagnostics

```sh
./Cinter status all
./Cinter setup all
./Cinter build all
./Cinter stage all
./Cinter all all
./Cinter windows cross-test
./Cinter host test
```

Only `./Cinter publish ...` performs a release upload. The lower-level `./Cinter host ...` commands exercise the reusable native Host. Builds' source-level tests, bridge/terminal interaction, live Bedrock tests, native app launch, package hash/format checks and shutdown checks are separate acceptance dimensions. A successful file check or Wine smoke does not constitute a native Windows GUI pass.

## Reproducibility and security

All tools and inputs are pinned or hash-verified. Archive extraction must reject path traversal, dangerous external links and unsafe files while retaining required binaries and executable bits. Generated dependencies/receipts never modify the authored checkout. Only capabilities explicitly granted by the trusted native Host can expose local files or run commands; a game WebView must not gain arbitrary workstation access. Tooling diagnostics are emitted as local Markdown/log artifacts rather than embedded machine-specific paths in the public docs.

### Windows native Session dependency and managed guest sockets

The lightweight native Windows application includes a pinned, SHA-256-checked `pywinpty` CPython 3.11 wheel so the Windows ConPTY Session backend can start without a post-install `pip` command. The package is acquired in Setup, independently of MSYS2's unrelated `winpty.exe`, and verified again when building the Windows artifact. The installed Windows Terminal executable is a GUI-subsystem process, while its Python Host subprocess is hidden and communicates through supervised pipes. The normal launch target is `CinterForge Terminal.exe`, not a persistent PowerShell or CMD window.

On macOS, the *optional* Windows/Linux guest Bash sessions use the one DevTools package and dedicated Colima profile `t`. Repository development uses the short sibling `Builds/CFTVM/T` directory, while an installed app uses the real user cache at `~/Library/Caches/CFTVM/T`, with persistent runtime logs under its Steam `steamapps` Logs tree. No installed app writes a developer Builds tree or places Unix sockets inside its signed `Contents/Resources/Builds` payload. Neither backend silently falls back to Mac Bash.


The **single Code-OSS Session** remains mounted while changing Session tabs. Its editor and live PTYs are disposed only when the user closes the Code-OSS tab. The File/Edit/Selection/View/Go/Run/Terminal/Help menu skeleton remains identical in Code-OSS and other modes; editor-only commands appear disabled outside Code-OSS. Mac, Windows, and Linux Launcher actions focus or open their one corresponding Session each.


## Managed QEMU for macOS AMD64 guest

Mac Bootstrap installs an exact prebuilt macOS ARM64 QEMU package
(`qemu-portable-darwin-arm64` 0.2.1, SHA-512 pinned in
`host/mac_bootstrap.py`) into sibling
`Builds/CinterForgeTerminal/Toolchains/mac/qemu`. It executes both
`qemu-img --version` and `qemu-system-x86_64 --version` and checks the
QEMU machine list before allowing Setup to proceed. The provider's bundled
`bin`, `lib`, and `share/qemu` tree is included in optional unified DevTools.
No Homebrew installation or QEMU source build is part of Setup. This QEMU
installation is a dependency provision, **not** a second guest. The native
Terminal adds that owned binary directory to its guest startup environment.

The LinuxKit/Bedrock `c` backend continues to use its existing ARM64/VZ
path. Only after Minecraft acceptance is the one x86-64 QEMU `t` guest
started for Debian, Linux Bash/VNC, and Wine/MSYS2 Windows Bash/VNC.

During Windows Setup, the verified Mac dependency closure consists of the
portable, Setup-owned tools (including QEMU), not the disposable `c` or `t`
Colima state. The ARM64 LinuxKit producer is stopped before Bedrock testing;
Windows setup then verifies its own build and Wine/MSYS2 execution on `t`.
Windows closure validation must not demand `Builds/CFTVM/U/backend.json`:
that receipt is issued only by the legacy explicit `ensure_docker()` entry point
and is not part of the current LinuxKit producer lifecycle. No VM disk, live
Docker socket, or profile directory is copied into the Windows toolchain
closure.

### Native registry transport for the AMD64 guest

On Apple Silicon, Setup stages a pinned macOS ARM64 `skopeo` executable in the
existing Mac toolchain. Once LinuxKit/Bedrock acceptance passes and QEMU `t`
is running, Setup downloads the **digest-pinned** Debian AMD64 and MSYS2 Wine
base images over native macOS HTTPS and imports them into `t` with Docker load.
The generated `Builds`-owned Dockerfile uses these verified local base tags,
so Buildx does not need to complete registry TLS handshakes inside the
emulated x86-64 guest. Compilation, package installation, Linux Bash,
Wine/MSYS2, and both graphical sessions still execute **inside one x86-64**
`t` guest. Downloaded intermediary archives are removed after import. The
authored Dockerfile retains the immutable registry digests. The native
Skopeo invocation explicitly selects a Setup-generated v2-format
`registries.conf` under `Builds/Intermediates/CinterForgeTerminal/RegistryInputs`;
Conda's packaged legacy v1 configuration is left untouched. This avoids
registry configuration errors without disabling certificate verification.
