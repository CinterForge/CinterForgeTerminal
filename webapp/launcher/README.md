# CinterForge Terminal Launchers

Mac, Windows and Linux share one minimal HTML Launcher: the same bar with **one**
CinterForge Terminal icon, backed by three independent background PNG files:

- `assets/backgrounds/mac.png` — Mac Launcher
- `assets/backgrounds/windows.png` — Windows Launcher
- `assets/backgrounds/linux.png` — Linux Launcher

Replace a PNG with the same filename to change that platform's background.
There is no desktop badge or second platform selector. Launcher tabs remain
closed until requested and at most one tab per platform may be open.

The only icon is an allowlisted action for the **staged Steam build** matching
this release. Mac opens the signed `.app` in the host OS. Linux invokes the
staged `Terminal.sh` under the existing Linux guest shell, and Windows invokes
`Terminal.cmd` under Wine in the existing Windows guest; their real windows
appear in their corresponding VNC-backed Launcher tab. The launcher never
runs the raw compiler output or installs dependencies. Missing stage or guest
readiness reports an error. VSCode continues to own the Windows and Linux
Bash terminals. No KDE or full desktop shell is installed.
