# Integrating the Host

CinterForgeTerminal supplies a reusable native Host for applications that need an embedded WebView, Python services, **real platform terminal sessions**, and optional application-to-application capabilities. A consumer links the **versioned staged Host** rather than invoking code from a sibling checkout. Terminal's own default Game/WarGames UI is a client of this Host, not a requirement placed on every consuming application.

## Linked C++ and C APIs

The reusable library is **`CinterForgePythonHost`**; its stable C ABI uses the **`cph_*`** prefix. The **`./Cinter host ...`** subcommands expose Host lifecycle diagnostics and build automation without a separate `CPH` script. Native applications use the linked C++ interfaces or C ABI for operations such as:

- starting and stopping a private Python service;
- sending requests and receiving events;
- inspecting readiness and process lifetime;
- creating native windows and embedded browser views;
- connecting terminal streams and authorized JavaScript messages;
- using optional network transport and service discovery.

C++ includes the Host's staged public headers; Swift, C, and other languages can use the C ABI without coupling to Python internals. A service is not mandatory for HTML-only applications.

## Application Shell contract

The common shell wraps the platform's native WebView and offers focus, clipboard, input events, navigation, dialogs, window lifecycle, and a structured JavaScript-native bridge. The bridge must validate message origin and capability permissions, correlate replies with requests, and marshal UI calls onto the correct native thread.

A service-backed application declares a local service endpoint, starts its packaged worker, waits for readiness, loads the UI through its authorized origin, and shuts everything down when the app closes. Never expose a loopback-only service to the network solely because LAN discovery is available.

## Real terminal sessions, native menus and Code-OSS

The Host owns authentic platform PTY/ConPTY sessions, their input/output streams, working directories, resize events, process exit and cleanup. A consumer requests an explicit session type and receives a session identifier; it does not implement a second fake shell inside a WebView. The five Terminal product modes are **Game**, **Minecraft**, **Mac**, **Win** and **Linux**, but **Mac/Win/Linux platform shells are integrated into Code-OSS**. On a Mac, the default Code-OSS workbench has one real integrated terminal for each installed Mac/Windows/Linux execution environment; Windows and Linux default to one native shell.

Terminal's **native operating-system menu** owns New Game, New Minecraft, New Mac, New Win and New Linux Terminal actions and standard Edit actions. The HTML document does not duplicate the native Terminal/Edit menu or own a transient copy of the available-platform list. Host capability discovery returns installed, verified terminal backends and reports absent environments explicitly. A Game Terminal opens games directly; `bedrock run <world>` opens a dedicated server-owning Bedrock Session PTY with live stdin/stdout while leaving the invoking Session intact. `minecraft` creates a client console for the same live server, not a second server. Closing a Minecraft client console detaches; closing the Bedrock owner tab stops its server; native app exit stops all owned processes and guest VMs.

`code` launches the shared Code-OSS workbench and an actual **user-selected local folder/workspace**. Its native file capability supports safe directory browsing, edit/save and last-workspace persistence across relaunch, with normal user filesystem permissions, explicit workspace trust and scoped, authenticated bridge operations. The same workbench recognizes a trusted `CinterForgeTerminal` checkout and runs that repository's actual `./Cinter` lifecycle from real integrated shells. The bridge must not expose arbitrary filesystem paths or unauthenticated process execution to a game page.

The optional `devtools install|uninstall|list|report` family belongs to this development environment. It operates on the **single unified three-platform** toolchain archive and emits a Markdown tool report under `Builds/Logs`, while preserving the core game's lightweight runtime and all user content. Developer Setup/Build/Stage/Test/Publish remain distinct operations; calling `all all` never publishes.

## Python environment and runtime closure

Each consuming application declares its environment, pinned common/platform-specific dependencies, service entry point and capabilities. Setup prepares a named environment and verifies it; release packaging includes the interpreter, standard library, native modules and service code needed at runtime. Relocating the bundle must not leave `sys.prefix` or native imports pointing at a developer checkout.

The common staging model is:

```text
<workspace>/Builds/
  Python/Host/<platform>/<architecture>/<release>/
  Python/Environments/<platform>/<architecture>/<environment>/
  Python/Services/<application>/
  Applications/<application>/Input/
  Applications/<application>/Staged/<target>/
  Logs/
  Intermediates/
```

Consumers receive versioned staged inputs and verify them by manifest. The source repository of another product is not an application dependency.

## HTTP transport and Cloudflare

`cinter-curl` is the platform-neutral HTTP client component. Its contract covers HTTP methods, headers and bodies, secure HTTPS, retries for eligible operations, cancellation, streaming, progress reporting, and optional size/hash verification of downloads. An R2-compatible publisher adds authenticated signing and configured destination handling; credentials never live in packaged client code. Public CDN reads do not require private credentials.

The transport is usable without starting a WebView, Python worker, or Steam client.

## Product declarations and artwork

An application owns its manifest, branding/skin, launch assets, UI files and service permissions. A representative design-shaped manifest is:

```json
{
  "schema": "cinterforge.application/v1",
  "id": "io.example.app",
  "name": "Example App",
  "ui": { "entry": "www/index.html", "webview": "auto" },
  "services": [{ "name": "api", "environment": "exampleenv", "readiness": "/health" }],
  "capabilities": ["example.read"],
  "distributions": ["direct", "steam"]
}
```

The example illustrates the intended declaration boundary; it is not a promise that every consumer accepts these exact JSON fields. Skins cannot acquire native permissions or replace executable code without a verified, separately authorized package.

## Optional Steam adapter and Creative Mesh

A Steam adapter can implement the same observable HTML-surface/input/bridge contract using the Steam browser surface, while keeping Steamworks SDK types and restricted redistributables out of the Host core. A direct native WebView distribution does not need a Steam account.

Creative Mesh is a permissioned local capability protocol. Applications advertise declared operations and supported input/output types; a caller requests work with identity, deadline, cancellation, and explicit authorization. Discovery is not permission to execute code or access arbitrary files. Large payloads use verified artifacts rather than unrestricted JSON blobs.

## Ownership and contribution boundaries

The Terminal repository owns the shared Host, bridge, managed terminal/session logic, and its own application lifecycle. Code, Voxel, Converter, OS, Server, and other repositories own their product-specific source and publishing. They consume staged, versioned artifacts through `Builds`. See [Architecture](Architecture.md), [Build and Release](Build%20and%20Release.md), and [Contributing](Contributing.md).
