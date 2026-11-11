# CinterForge Terminal — Xcode integration

Open `CinterForgeTerminal.xcodeproj` in Xcode. The shared **CinterForge Terminal** scheme creates and launches a real native `.app` using the repository's existing C++/CMake Host, Python HTTP service, resources and Application Shell. This is an **Xcode front-end to the existing producer**, not another application implementation.

## First-time setup and everyday development

1. Run `./Cinter setup all` once in a real Mac producer checkout to provision dependencies into sibling `Builds`. Xcode never downloads/provisions dependencies or silently runs Setup. In **Xcode Settings → Locations**, configure Derived Data and Archives under sibling `Builds/Xcode` to avoid Xcode's default home-directory caches.
2. Open the project and select the **CinterForge Terminal** scheme and **My Mac**. Use **Run** for the application's LLDB console, debugging and process inspection. Source files are visible in the project navigator. The native C++ binary is compiled by the existing CMake producer with `RelWithDebInfo` when Xcode runs Debug, enabling source-level LLDB breakpoints. The normal `./Cinter build all` automatically returns to the existing Release configuration. Do not run an Xcode Debug build and a CLI Release build concurrently against the same managed `Builds` tree.
3. Choose **Signing & Capabilities**, select your Apple Developer **Team**. Debug initially uses local ad-hoc signing and can be changed to Apple Development if required; Release is configured for **Developer ID Application** direct distribution. Keychain holds the private key. No certificate or credentials are committed to Git.
4. For direct-download CLI builds, under the target's **Build Settings → User-Defined**, set `CINTER_MAC_NOTARY_PROFILE` to the name of an existing `xcrun notarytool store-credentials` Keychain profile. This field is a name, not the password or API key.
5. **Product → Archive** creates an Xcode Organizer archive of the existing Terminal runtime, using Xcode's Release settings. Organizer can export/notarize a Developer ID build. This Xcode archive is an *additional manual distribution/testing route*; it is **not** the four-ZIP Cloudflare release and does not implicitly publish anything.

## Command-line release remains authoritative

`./Cinter all all` still performs Setup → Build → Stage → Test, creates the three platform ZIPs and one shared DevTools ZIP, and never publishes. During `build all` on a Mac, `host.xcode_signing` reads the **same Xcode project's Release Team and certificate kind**, resolves the installed Developer ID Application certificate in the macOS Keychain, and passes its name and the configured notarization profile to the existing Mac release code. Set no Team to retain local ad-hoc development signing. Never provide private signing keys in the repository.

**Important:** The Developer ID/notarization implementation is in the separately supplied November 11 **3-Blockers** delta. Apply/reconcile that accepted signing implementation against your actual checkout before relying on downloadable public macOS release ZIPs. This Xcode addition does not duplicate or replace it. A public release is accepted only after the full Build/Stage/Test, Developer ID signing, notarization/stapling, and downloaded-app Gatekeeper verification succeed on a Mac.

## TestFlight is a separate future distribution gate

Xcode supports uploading macOS archives to App Store Connect/TestFlight, but **Developer ID direct distribution and Mac App Store/TestFlight are different signing/distribution targets**. Mac App Store validation may require app sandboxing and entitlement/runtime architecture changes for Terminal's Python, local services, shell execution, guests and virtualization. This project does **not** claim TestFlight eligibility and does not enable an App Store signing profile prematurely. Keep the existing direct distribution functional first; introduce a separately reviewed App Store configuration only after those restrictions have been tested.

## Boundaries and outputs

- Xcode project, plist and scripts: authored source in this repository.
- Xcode Build products, archives and intermediate outputs: sibling `Builds/Xcode` as directed by build settings. For Xcode's default Organizer Archive path, choose `Builds/Xcode/Archives` when prompted or use `xcodebuild -archivePath`.
- Existing CMake/native products, Python service, Code-OSS, Bedrock and Lagomorph: only existing `Builds` outputs, never a duplicate implementation.
- Installed product: existing Steam `steamapps` policy, unchanged by Xcode Build.
- Explicit Cloudflare publication: only the existing `./Cinter publish ...` commands.
