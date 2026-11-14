
# CinterForgeTerminal — Complete Beta 1 Master Checklist

**Checklist legend:** 

```
[x] = Source implementation or source-level verification observed; 
[ ] = Pending or native acceptance not yet established. 
```

## 0. CRITICAL BLOCKERS

- [ ] Installed Mac build does not launch.  No idea how to debug this.
- [ ] Installed Windows build does not register in Add/Remove Programs
- [ ] Installed Windows build does not add to start menu
- [ ] Installed Windows build installer defaults to the Downloads folder

## 1. Windows/Linux — MSYS2 Bash startup — BUG

- [X] Native app launches.
- [X] Verify the MSYS2 package closure copied into the Windows release.
- [X] Make package contents relocatable.
- [X] Confirm Windows GUI application launch without an extra console window.
- [ ] Add a real Bash startup smoke test to the Linux and Windows release checks.
- [ ] Real Linux/Win Bash prompt appears inside Terminal.
- [ ] Provide writable temporary runtime state.
- [ ] Check Bash executable paths and argument construction.
- [ ] Confirm tab close/reopen/reconnect behavior using native WebView2/ConPTY.

## 2. Critical CodeOSS issues
- [ ] Cut is Broken - The whole Edit menu is messed up.  We are damaging the CodeOSS Edit menu
- [ ] CodeOSS MD/HTML/Image/etc Preview is broken - as many media types to preview as is easy
- [ ] Cinter button not working

## 2. Infinite-Mac — current timing and first-launch bug
- [x] Current delay values were identified and reported.
- [x] Move configuration values into a documented section near the top of `terminal.js` 
- [ ] Determine why leaving the page and returning allows it to work.
- [ ] Add diagnostic visibility to the first-launch path.
- [ ] Repair the actual cold-start problem without introducing arbitrary long waits.
- [ ] Test clean first launch separately from subsequent warm launches.

## 3. Testing
- [ ] Verify the packaged shell styles connect in the installed macOS application.
- [ ] Verify the Server tab reports the exact runtime log and captures native startup failures.
- [ ] Validate Mac Bash with a real native PTY.
- [ ] Validate Windows Bash on Mac via the supported guest environment.
- [ ] Validate Linux Bash on Mac via the supported guest environment.
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
- [ ] Verify archive contents, manifests, checksums and relocation.
- [ ] Check all generated output stays under sibling `Builds/` or designated `steamapps`.
- [ ] Ensure no source reaches into sibling repositories.
- [ ] Revalidate the complete clean-builder lifecycle on both HOPE-DEV and HOPE-BUIDLER

## 9. Web Site
- [ ] Add a demo for CinterForgeTerminal
- [ ] Make sure all integrated webapps do not show on live site - except terminal
- [ ] Push to main

BETA ACIEVED!
