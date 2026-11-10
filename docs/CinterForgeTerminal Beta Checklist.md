
# CinterForgeTerminal — Complete Beta 1 Master Checklist

**Checklist legend:** 

```
`[x]` = Source implementation or source-level verification observed; 
`[ ]` = Pending or native acceptance not yet established. 
```

## 1 Repository Restructuring 

The root-level `build/` contains authored build resources and code. The separate sibling workspace `Builds/` continues to hold all generated artifacts, dependencies, caches and staging.

- [ ] `webapp/` moved to repository root.
- [ ] `webapp/codeoss/` moved under `webapp/`.
- [ ] `build/terminal_bootstrap/` moved into `build/`.
- [ ] `build/resources/` moved into `build/`.
- [ ] `build/fork.py` and `build/library.py` relocated.
- [x] Two example JSON files moved from root into `build/`.
- [x] `profiles.json` renamed `webapp/terminal_styles.json`.
- [x] Finish migrating all callers of `examples/shell/` to `webapp/`.
- [x] Eliminate `examples/shell/` after verifying nothing still references it.
- [x] Eliminate `examples/http/`.
- [x] Remove the obsolete `examples/` directory.
- [ ] Eliminate the obsolete `scripts/` directory, migrating necessary functionality into `Cinter` or `build/`.
- [x] Update all references to `build/resources/` and `build/terminal_bootstrap/`.
- [x] Update all references to `webapp/codeoss/`.
- [ ] Update CMake, native launchers, packaging, staging, tests, server routes and documentation.
- [ ] Remove orphaned files, duplicate directories and dead code.
- [ ] Ensure all relative paths work from source and from a relocated installed application.
- [ ] Ensure no runtime dependency reaches into sibling source repositories.
- [ ] Update `./Cinter build all`.
- [ ] Update `./Cinter stage all`.
- [ ] Update `./Cinter test all`.
- [ ] Verify `./Cinter all all` executes the complete lifecycle.
- [ ] Keep `all all` free of automatic publishing.
- [ ] Preserve read-only `./Cinter status all`.
- [ ] Ensure all relocated Python, JS, native resources, Code-OSS and bootstrap files are included correctly.
- [ ] Ensure packaging does not depend on the source checkout existing on the user's machine.
- [ ] Verify native macOS, Windows and Linux application outputs.

## 2. Consolidate Python Code

Target: one main Python runtime file and one lifecycle driver.

- [x] Merge `webapp/bedrock_move.py` into `webapp/CinterForgeTerminal.py`.
- [x] Merge `webapp/command_cli_move.py` into `webapp/CinterForgeTerminal.py`.
- [x] Merge `webapp/devtools_shell_move.py` into `webapp/CinterForgeTerminal.py`.
- [ ] Merge other authored runtime Python modules into the appropriate main file.
- [ ] Merge the functionality from `build/fork.py` and `build/library.py` into `Cinter` where it belongs.
- [ ] Inventory Python implementation in `linuxos/`, `host/`, and other directories.
- [ ] Consolidate lifecycle, provisioning and orchestration Python into `Cinter` where practical.
- [ ] Preserve the native C++ library and C ABI that the application needs.
- [ ] Preserve standalone regression tests as tests, rather than burying them inside production code.
- [ ] Update all imports, launch commands, subprocess calls and packaging manifests.
- [ ] Delete superseded Python implementation files only after their replacement entry points are wired and checked.
- [ ] Ensure `CinterForgeTerminal.py` runs both locally and from the installed app without a source checkout.

## 3. Eliminate CPH 

- [x] Remove the separate user-facing `CPH` CLI.
- [x] Move its command dispatch into `Cinter`.
- [ ] Preserve all required commands and behavior.
- [x] Preserve the existing `CinterForgePythonHost` native library and `cph_*` ABI where still used.
- [ ] Update callers, documentation, tests and packaging.
- [x] Keep `Cinter.cmd` as a minimal Windows entry-point wrapper, not a duplicate lifecycle implementation.
- [x] Remove the obsolete `CPH` executable/script once all callers have migrated.

## 4. JSON and future web API 

- [x] Rename `profiles.json` to `terminal_styles.json`.
- [x] Update every reference to `profiles.json`.
- [x] Verify terminal style loading from `webapp/terminal_styles.json`.
- [ ] Retain `webapp/if_titles.json` and `webapp/mac_games.json` where the application needs them.
- [ ] Review `build/powershell.packages.example.json`.
- [ ] Review `build/projects.example.json`.
- [ ] Delete either example file only if it has no legitimate documentation, tooling or configuration use.
- [ ] Separate portable web application data from machine-specific runtime configuration.
- [ ] Avoid hardcoding a local source checkout as the configuration location.
- [ ] Keep the web application compatible with future website/API delivery.
- [ ] Preserve current local and Cloudflare behavior; no new API server is required for this cleanup.

## 5. macOS — Xcode, signing and debugging 

- [ ] Create a native Xcode project for CinterForgeTerminal.
- [ ] Include the existing application sources rather than maintaining a separate implementation.
- [ ] Provide Debug and Release configurations.
- [ ] Enable breakpoints, LLDB, crash diagnostics and native logs.
- [ ] Configure bundle identifier, signing Team, entitlements and hardened runtime.
- [ ] Provide Developer ID signing for direct distribution.
- [ ] Add notarization and stapling to the release process.
- [ ] Ensure users can open the distributed application without manually overriding Gatekeeper.
- [ ] Direct DerivedData and build products into sibling `Builds/`.
- [ ] Keep the `./Cinter` lifecycle authoritative.
- [ ] Ensure signing occurs after staging all embedded content.
- [ ] Verify signatures and notarization tickets on the final distributable.
- [ ] Verify that Xcode and `./Cinter` use consistent native sources and runtime resources.
- [ ] Test actual macOS WKWebView startup under the debugger.

## 6. Windows — MSYS2 Bash startup — BUG

Native Windows failure observed

- [ ] Native Windows app launches.
- [ ] Real UCRT64 Bash prompt appears inside Terminal.
- [ ] Fix missing `/tmp` directory.
- [ ] Provide writable temporary runtime state.
- [ ] Investigate read-only `/dev/shm` and `/dev/mqueue` initialization.
- [ ] Restore missing `pacman-key` support scripts:
  - `message.sh`
  - `parseopts.sh`
  - `utils_fixed_path.sh`
- [ ] Verify the MSYS2 package closure copied into the Windows release.
- [ ] Check Bash executable paths and argument construction.
- [ ] Check `MSYSTEM`, `PATH`, root mapping and related environment variables.
- [ ] Make package contents relocatable.
- [ ] Ensure no console or temporary state is written into an inappropriate read-only source directory.
- [ ] Add a real Bash startup smoke test to the Windows release checks.
- [ ] Verify `pwd`, `ls`, `uname`, interactive input and resize.
- [ ] Verify `pacman-key` no longer fails at startup.
- [ ] Confirm Windows GUI application launch without an extra console window.
- [ ] Confirm tab close/reopen/reconnect behavior using native WebView2/ConPTY.

## 7. Infinite-Mac — current timing and first-launch bug

- [x] Current delay values were identified and reported.
- [x] Existing automation's configured delay total was reported as 26.23 seconds, including the 18-second post-`emulator_loaded` wait.
- [x] Verify those constants directly against the current newest source.
- [x] Move configuration values into a documented section near the top of `terminal.js` without changing them.
- [x] Preserve the exact launch sequence, event order, clicks and guest inputs.
- [ ] Determine why leaving the page and returning allows it to work.
- [ ] Add diagnostic visibility to the first-launch path.
- [ ] Repair the actual cold-start problem without introducing arbitrary long waits.
- [ ] Test clean first launch separately from subsequent warm launches.
- [ ] Verify Lagomorph disk opens and the application launches.
- [ ] Preserve game image, manual, Battles and other Lagomorph assets.

## 8. Native terminals and DevTools
- [ ] Fix local project loading
- [x] Include locked native-platform Copilot SDK dependencies during Code-OSS Setup.
- [ ] Verify Code-OSS Setup completes after the Copilot SDK correction on macOS.
- [x] Source implementation for macOS native PTY, Win/Linux Session tabs, and optional DevTools previously delivered.
- [x] Source correction for Windows `pywinpty` provisioning previously delivered.
- [x] Source correction for the short managed guest socket path previously delivered.
- [ ] Revalidate Mac Bash with a real native PTY.
- [ ] Revalidate Windows Bash on Mac via the supported guest environment.
- [ ] Revalidate Linux Bash on Mac via the supported guest environment.
- [ ] Verify tabs open in the existing Terminal window, not separate windows.
- [ ] Verify native menu actions target the correct real shell.
- [ ] Verify interactive command input, output, resizing, closing, reopening and tab switching.
- [ ] Verify Mac Bash requires no optional DevTools installation prompt.
- [ ] Verify Win/Linux DevTools prompts occur only when required.
- [ ] Show exact DevTools archive size and installation location before consent.
- [ ] Require explicit `INSTALL` consent.
- [ ] Verify download, extract and installation progress is displayed.
- [ ] Verify checksum and actual tool versions after installation.
- [ ] Confirm declining installation leaves the session unchanged.
- [ ] Confirm an already-installed toolchain launches without another prompt.
- [ ] Confirm installed tools remain usable after app restart.
- [ ] Test `./Cinter setup all` in all 3 terminals
- [ ] Test `./Cinter build all`.
- [ ] Test `./Cinter stage all`.
- [ ] Test `./Cinter test all`.
- [ ] Verify `./Cinter all all` executes the complete lifecycle.
- [ ] Verify the optional unified DevTools archive.
- [x] Ensure exactly four current distribution artifacts: mac, linux, win and devtools.
- [ ] Verify archive contents, manifests, checksums and relocation.
- [ ] Ensure signed macOS content is not modified after signing.
- [ ] Check all generated output stays under sibling `Builds/` or designated `steamapps`.
- [ ] Eliminate hidden generated dependencies, caches and runtime directories outside those permitted locations.
- [ ] Ensure no source reaches into sibling repositories.
- [ ] Revalidate the complete clean-builder lifecycle on both HOPE-DEV and Kevin's Mac.

## 9. Web Site
- [ ] Add a demo for CinterForgeTerminal
- [ ] Make sure all integrated webapps do not show on live site - except terminal
- [ ] Push to main

BETA ACIEVED!
