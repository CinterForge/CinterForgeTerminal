# CinterForge Terminal Client — Browser UI and JavaScript

**Primary source:** `webapp/terminal.js` (the file referred to as “CinterForgeTerminal.js” in the original request).  
**Supporting files:** `webapp/index.html`, `webapp/terminal.css`, `webapp/startup.js`, `webapp/launcher/`, `webapp/joshua/`, `webapp/data/`, `webapp/views/`, and the separately staged `webapp/codeoss/` workbench.  
**Scope:** the current implementation and a proposed refactor, not a rewrite.

## Role and system boundary

The client is the **one served Terminal user interface** on Mac, Windows, Linux and supported LAN browsers. It renders tabs, terminal output, native-inspired menus, Code-OSS, machine emulators, the game/IF catalog, Bedrock EULA interactions, Discord and Launcher views. It does **not** own host processes or declare VM readiness. It asks the Python service to create/verify sessions and reads the resulting capability state. Native window, menu, clipboard and inspector operations use narrowly defined C++ WebView bindings when present.

## Major areas in `terminal.js`

| Area | Current code responsibilities |
| --- | --- |
| Authenticated bridge | `api()` sends same-origin JSON requests with the document-provided token; `nativeEvent()` reports authorized UI events to the WebView host |
| VT renderer | `VTOutput` consumes ANSI output, tracks cursor/scrollback, and renders the terminal surface |
| Global client state | Profile/catalog metadata, tab list, current session, `devtoolsInstalled`, `guestState`, `guestReady`, `guestVncReady` |
| Embedded computers | Code-OSS iframe and command bridge; Infinite Mac profiles and Lagomorph timing/boot sequence |
| Tab manager | Create, activate, close, preserve current output and focus per Session; tab order and persistence |
| Server Log | `openServerTab()` and `pollServerTab()` display the server's read-only rolling runtime log |
| Launchers | Windows/Linux/Mac launcher views and guest-backed staged-app requests; VNC availability must be independently verified |
| Games/IF/Discord | Separate session ownership for interpreters, save/restore, message history and command routing |
| PTY sessions | `openSession()`, `pollTab()`, `send()`, resize, close, reconnect and output generation/offset handling |
| Input, clipboard, keys | Focus/keyboard target, key translation, paste/copy and native clipboard integration |
| Menus | Shared menu actions, native/HTML menu dispatch, Code-OSS commands and action availability |
| Initialization and restoration | Fetch status and profiles, choose default Mac Bash or Server Log, poll guest capabilities, restore only supported saved tabs |

## Architecture diagram

```mermaid
flowchart LR
    USER["Keyboard / menu / tabs"] --> CLIENT["terminal.js\nUI state + VT renderer"]
    CLIENT --> API["Authenticated same-origin\n/api/* HTTP requests"]
    API --> SERVER["CinterForgeTerminal.py\nHTTP Handler"]
    SERVER --> PTY["PTY / ConPTY\nTerminalSession"]
    SERVER --> STATE["Guest status\nBash and VNC readiness"]
    SERVER --> LOG["ServerRuntimeLog"]
    CLIENT --> CODE["Code-OSS iframe\nexisting extension bridge"]
    CLIENT --> EMULATOR["Machine / IF / Launcher\nSession content"]
    CLIENT <-->|"authorized native callbacks"| NATIVE["C++ WebView shell"]
    STATE --> CLIENT
    LOG --> CLIENT
    PTY --> SERVER
```

## Data flow: a shell keystroke

1. The user types into the focusable Terminal input. The client translates control keys and routes them to the **active tab**, not a global shell process.
2. `send()` uses the real terminal session ID and makes `/api/terminal/input` requests. The host-owned `TerminalSession` writes to its PTY/ConPTY (or verified guest PTY for that backend).
3. `pollTab()` requests `/api/terminal/poll` and uses its generation/offset to append output to the tab's renderer. A tab switch preserves the session and transcript without creating another worker.
4. Resize and close operations travel through `/api/terminal/resize` and `/api/terminal/close`. Code-OSS terminal requests use the existing `/host/code-shell-*` bridge and must not be confused with normal HTML Session tabs.

**Other flows:** Code-OSS is a same-origin workbench iframe and existing extension; Launcher actions request `/api/launcher/*`; game/IF, Bedrock and Discord use their dedicated `/api/*` routes. The Server Log tab reads `/api/server/log`; it is a view of server events, not a second logging engine.

## Startup and readiness

The script first calls `/api/status`, obtains the platform and installed DevTools/guest state, then loads `/terminal_styles.json` and `/mac_games.json`. It populates the shared profile/menu model and initializes tabs.

- **Mac without verified installed DevTools:** immediately open real **Mac Bash** Session mode. The optional guest VM is not required.
- **Mac with DevTools:** immediately show **Server Log**. Boot is managed by Python, not by JS; the UI polls `/api/lifecycle/guest-status` and displays live output.
- **Linux/Windows native app:** open the normal native terminal session.
- **Code-OSS, guest Bash, and VNC:** disabled until their **own live** readiness condition is met. Installation receipt, started VM, or active Docker container is not proof of a usable terminal or VNC socket.
- **Saved layout:** preserve the prior layout during the temporary Server Log view; reopen only the tabs whose connections have been established. A failed endpoint remains closed/unavailable while other healthy endpoints continue.

This is the intended contract and the current code already has separate `guestReady` and `guestVncReady` sets; the implementation should not reintroduce a single all-or-nothing readiness flag.

## Shutdown and reconnection

The browser saves its tab layout through `saveTerminalLayout()` and the `pagehide` hook. Switching or closing tabs should detach/close only the correct session; it must not stop another tab's shell or an app-owned VM. `beforeunload` is **not** the VM shutdown owner: Python and the native shell are. On the next launch, the client rehydrates the saved view only after server status says the endpoint is live. Connection failure must visibly update the menu and Server Log, not leave a blank VNC iframe or stale Code-OSS terminal.

## Proposed JavaScript refactor — staged, no parallel UI

The present `terminal.js` coordinates too many independently stateful features. Move code **within the existing webapp**, preserving DOM IDs, API routes, native bindings, state schema, and the same entry point. Recommended boundaries:

| Responsibility | Suggested module | Single source of truth |
| --- | --- | --- |
| Server communication/auth/response errors | `client/CinterForgeAPI.js` | Current `api()` token protocol |
| Readiness and platform capabilities | `client/CinterForgeCapabilities.js` | Response from Python `/api/status` and `/api/lifecycle/guest-status` |
| Tab/session ownership and restoration | `client/CinterForgeSessions.js` | One active tab registry and persisted layout |
| VT output rendering | `client/CinterForgeTerminals.js` | One VT buffer per real terminal tab |
| Menu commands and enabled states | `client/mCinterForgeMenus.js` | One action registry reused by native and HTML presentations |
| Code/Launcher/Emulator integration | Focused feature modules | Existing Code extension, Launcher and museum runtimes; do not fork them |
| Main initialization | Thin `CinterForgeTerminal.js` orchestration | Starts subscriptions and first view once |

Do **not** create new transport protocols, duplicate menu models, or move shell/VM decisions into JS. Prefer one typed capability snapshot and explicit transitions—`not-installed`, `starting`, per-endpoint `ready`, `failed`, `stopping`—over scattered booleans. Treat each retry as a new generation so late responses cannot revive a stale tab. Keep restoration idempotent: opening an already-open Code-OSS or guest session focuses it rather than allocating a second instance. Preserve existing acceptance and navigation rather than introducing a test framework.

## Maintenance contract and failure visibility

When changing the client, trace the whole action: UI event → API/binding → server state change → log/result → tab/menu state. Failures should show the endpoint, stage, cause, and the Server Log location. An empty VNC viewer is never a success state. Browser-only views must not pretend a local PTY or native capability exists. Never put tokens or secrets in JavaScript logs or persisted layouts.

**Related:** [Python server](CinterForgeTerminalServer.md), [native shell](../app/CinterForgeTerminal.md), [architecture](../docs/Architecture.md).
