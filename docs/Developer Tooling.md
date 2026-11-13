# Developer Tooling

CinterForge Terminal is usable as a **game application without a developer SDK**. All ordinary development—Bash/PowerShell, compilers, source editing, diagnostics, tool installation and the complete `Cinter` lifecycle—runs inside the **Code-OSS workbench**, launched by `code` from the Game Terminal.

There is **one optional unified DevTools release** containing the platform-specific developer toolchain closure for macOS, Windows **and** Linux. It is not three unrelated SDK downloads and is not bundled into each lightweight native Terminal application.

## Managed bootstrap environments

| Producer/target | Required host baseline | Managed Setup closure |
|---|---|---|
| macOS arm64 producer | Xcode, macOS native virtualization authorization | Pinned Python, Node.js, Go, CMake, Ninja, pkg-config, Docker CLI/Buildx, signed/entitled Lima and Colima, archivers, native WebView sources and applicable native compilers |
| Windows x64 | Supported Windows bootstrap (`Cinter.cmd`) | Real MSYS2 UCRT64 shell, GCC/G++, CMake, Ninja, Python, Node.js, WebView2 SDK/runtime and required DLLs |
| Linux amd64 | Supported Linux build baseline | GCC/G++, POSIX shell, CMake, Ninja, Python, Node.js, GTK/WebKitGTK and native Host/build dependencies |
| macOS-hosted Windows/Linux cross-build | Managed toolchains from the Mac producer | Isolated `w` MinGW/Wine/Xvfb OCI tools and Linux GTK/WebKitGTK build environment; separate `c` for ARM64 Ubuntu guest inputs |

**Tool acquisition is Setup's responsibility.** Setup also executes the same temporary X11/RFB/noVNC connection preflight in **both** the Linux and Windows guest images before starting Wine, with separate retained `Builds/Logs/CinterForgeTerminal/{linux,windows}-gui-preflight.{log,json}` results. Package presence alone is insufficient to pass. Never depend on a contributor's arbitrary Homebrew, Docker Desktop or global Python install. Generated tools, profiles, caches, logs and reports live in `<repo>/../Builds`. Managed `w` and `c` VMs are distinct and are stopped by their owning lifecycle when that lifecycle exits. On macOS, the VM-hosting binary must have a verified `com.apple.security.virtualization` entitlement; Setup handles reproducible provision/verification rather than requiring manual binary patching.

**Managed macOS Colima `w` Setup boot:** Lima SSH readiness (steps 1–4) is not a successful VM boot. Setup monitors the actual fifth boot-script step, captures the managed guest cloud-init/Lima journals at `Builds/Logs/CinterForgeTerminal/mac-colima-w-boot.log`, and applies one bounded retry only if `cloud-final.service` has failed. If current-boot Lima markers match but persistent-disk provisioning has left Docker stopped, Setup starts Docker after provisioning. An unfinished boot fails within a bounded window and stops only the Setup-owned `w` VM; it never fabricates a boot marker, changes the Debian application images, or requires a manual shell repair.

Platform tools have different executable ABIs. A single unified package means a verified distribution of the **complete three-platform tool set**, not that Linux, Windows and Mac binaries all run natively on every host. Code-OSS exposes only real installed execution environments.

### Guest Bash inside the single Code-OSS workbench

**New Mac Bash** creates a native macOS PTY tab immediately; opening it never probes or installs DevTools. Selecting **Open Linux Bash** or **Open Windows Bash** opens or focuses one platform-specific integrated terminal **inside Code-OSS**, not a Session tab. Only these non-Mac selections check the receipt-backed unified DevTools package. When missing, the requested Code-OSS terminal explains the missing runtime, displays the release's real advertised compressed size and installation destination, asks the user to type `INSTALL`, then streams download, verification, extraction and startup progress. Declining does not download tools or replace the original Session. If installed, the prompt is skipped.

The optional multi-GiB public download uses bounded **64 MiB HTTPS byte-range requests** with a validated `Content-Range` and bounded automatic retries for interrupted TLS/network reads, including `BrokenPipeError`. Received bytes are saved under the selected Steam library's versioned `Downloads/<release>/` root as a `.partial` file; a retry or separately authorized subsequent installation resumes from that byte offset rather than silently starting over. A failed download does not install an SDK or damage an existing Mac session. Completion requires the published whole-archive SHA-256 check **before** extraction and the existing per-member validation and version checks. The installer only removes the partial archive after a completed install; cancellation before download leaves no new archive. Native network/CDN acceptance is required to establish reliable multi-GiB delivery; small in-memory fault injection does not prove the public download succeeds.

On Apple Silicon, these Code-OSS integrated guest terminals use the DevTools-bundled macOS Docker/Colima/Lima tools, a Terminal-owned managed guest profile named `t`, and the verified Linux/amd64 OCI image or the Windows MSYS2 Bash environment under Wine. **These are real guest processes, not a macOS Bash prompt with a Windows/Linux label.** The runtime profile's disks and state live in managed `Builds` and are stopped when the owning Terminal application ends. It is distinct from Setup's `w` and `c` profiles. If the installed SDK is incomplete, a guest cannot start, or its version cannot be verified, the requested Code-OSS terminal reports that failure instead of silently falling back to Mac Bash. Mac Bash runs directly on the host without installing DevTools. The Linux and Windows Launchers are separate singleton graphical Session tabs, opened on demand and displaying application windows launched from the same guest environment. They are not additional Bash sessions.


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

On macOS, the *optional* Windows/Linux guest Bash sessions use the one DevTools package and dedicated Colima profile `t`; runtime sockets, VM data and caches are placed under a short managed `Builds/CFTVM/T` path. When an app carries a deeply nested `Contents/Resources/Builds`, the runtime selects the outer workspace Builds when present rather than creating Unix sockets inside the signed app bundle. Neither backend silently falls back to Mac Bash.


The **single Code-OSS Session** remains mounted while changing Session tabs. Its editor and live PTYs are disposed only when the user closes the Code-OSS tab. The File/Edit/Selection/View/Go/Run/Terminal/Help menu skeleton remains identical in Code-OSS and other modes; editor-only commands appear disabled outside Code-OSS. Mac, Windows, and Linux Launcher actions focus or open their one corresponding Session each.
