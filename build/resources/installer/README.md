# CinterForgeOS Installer UI

The installer is driven by `steam_dialogs.applescript` (AppleScriptObjC) and uses native macOS AppKit controls **from AppleScript**. No Swift, Xcode project edits, standalone renderer, or external UI files are required.

- `InstallerBackground.png`: artwork for the left panel.
- `CinterForge-logo.png`: original CinterForge flame/robot logo (transparent).
- `steam_dialogs.applescript`: window layout, text, account rows, actions.
- `InstallerMessage.txt`: legacy message reference.

The app launcher runs Steam preflight before opening Terminal. The Python Steam registration helper passes account names and the bundled UI folder to AppleScript. The script returns the selected account label on stdout, or an empty string for Cancel. Setup → Build → Stage remains in the existing launcher/lifecycle.

To change positions, edit the `NSMakeRect(x, y, width, height)` coordinates in the AppleScript. The window is 1024 × 560 points, with a 430-point left artwork panel and a 594-point right content panel. AppKit uses a bottom-left origin. Background and logo are independent layers. No generated files should be written into this bundle.

**Validation:** The AppleScriptObjC dialog requires execution on macOS to verify layout and selector wiring. This source has not been run on macOS in this environment.

Installer entry point: `Contents/MacOS/install` runs `scripts/steam/register_nonsteam.py --preflight --install-dialog`, which invokes this AppleScript UI before opening Terminal. The selected numeric Steam account is passed through `CFOS_STEAM_USER_ID` to Stage. No standard confirmation dialog should appear.
