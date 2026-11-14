# CinterForgeTerminal — Lifecycle-only testing instructions

**Updated:** 10 November 2026. This is part of the cumulative repository.

**The builder should run only the normal `./Cinter` lifecycle.** There are no
separate manual preflight, regression-test, browser, or log-inspection commands
required to complete this drop.

## Standard source build and verification (macOS builder)

```sh
./Cinter all all
```

This single command runs **Setup → Build → Stage → Test**. It does not
publish or launch the Terminal GUI. The explicit Test phase is the ONLY
place where release acceptance runs. Setup provisions all toolchains,
Bedrock/NetherNet inputs and guest images; Build compiles unpacked artifacts;
Stage copies/verifies those artifacts. None invokes the root smoke suite.

All **12** consolidated smoke checks live in **root `test.py`**. The former
`tests/` directory, hundreds of micro-tests and JavaScript smoke harnesses
were physically deleted. `test.py` has a permanent rule: keep at most 50 tests;
if the count ever reaches 75, consolidate to fewer than 50. Real system tests
are preferred over fake Docker replies or monkeypatches. The suite performs
read-only receipt/signature/resource checks and real Bedrock/Linux/Windows
Bash/VNC/MSI acceptance on the staged macOS build machine. The official
Bedrock acceptance uses a disposable Builds workspace, never a player's world.
Missing setup inputs cause Test to fail; Test must not acquire dependencies.

Linux/Windows guest acceptance and its complete VM cleanup report are under
`Builds/Logs/CinterForgeTerminal/guest-connections-test.{json,log}`. The Bedrock
acceptance receipt remains at `Builds/Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json`.
**A PASS requires the real Mac lifecycle**; source syntax checks performed
elsewhere cannot establish that guest VMs or native signing work.

When diagnosing a lifecycle phase, supported independent commands remain:

```sh
./Cinter setup all
./Cinter build all
./Cinter stage all
./Cinter test mac
```

No other test commands are expected from the builder. If one fails, the full
console transcript and a machine-readable result are retained automatically.

## Incremental DevTools Build (persistent dirty checking)

`./Cinter build all` stores a small per-target input/ZIP metadata cache in
`<workspace>/Builds/Intermediates/CinterForgeTerminal/ReleaseInputCache/<release>/`.
The first run with the new implementation may perform a full checksum comparison
of an existing artifact to establish the cache. Subsequent builds with unchanged
source toolchains and output ZIP identity print:

```text
[TERMINAL BUILD] devtools clean (cached inputs and ZIP): reusing ...zip
```

Changes to tools or the archive invalidate the fast path and trigger the
existing full ZIP and source SHA-256 verification. A real changed toolchain is
rebuilt; application-only changes cannot dirty DevTools. The cache does not
change release contents, publication requirements, or stage receipts.

**Builder action:** only the normal `./Cinter all all` lifecycle or the
existing `./Cinter build all` phase. The entire decision and final PASS/FAIL
appear automatically in `Builds/Logs/CinterForgeTerminal/Lifecycle/`.
If Build fails, provide the dated `.log` named by `latest-build-all.json`;
a prior successful Build log cannot explain a new failure.

## Automatic macOS Stage recovery (restart-safe-v2)

The November 10 `./Cinter all all` log shows Setup/Build **PASS** and Stage
**FAIL** because `CinterForge Terminal.app.previous-stage` already exists.
The exact error is emitted by the older Stage source, not the recovery code.
The corrected Stage prints this marker in the persisted lifecycle log:

```text
[TERMINAL STAGE] macOS recovery protocol: restart-safe-v2
```

After validating the release ZIP and candidate app signature, Stage recovers
interrupted states automatically:

- **App absent, backup present:** Restore the previous app before replacement.
- **App and backup both present:** Preserve the stale rollback copy under
  sibling `Builds/Intermediates/CinterForgeTerminal/StageRecovery/macOS/`.
  Works even when the Steam and Builds directories are on different devices.
- **Final installed app fails verification:** Roll back to the previous app.
- **Unsafe symlink or corrupted package:** Stop with an explicit failure; do
  not delete or overwrite untrusted contents.

A successful recovery includes `RECOVERED ...` in the Stage log if a stale
backup was found, then an `installed mac -> ...` message and a PASS JSON result.
No recovery line is expected when no stale backup exists.

**No Finder action or manual preflight is required.** To resume from the
already built ZIPs, the builder runs only:

```sh
./Cinter stage all
./Cinter test mac
```

Alternatively, `./Cinter all all` repeats Setup → Build → Stage → Test.
The Stage log and `latest-stage-all.json` must show PASS on the real Mac
before considering this fix host-accepted. Container tests alone are not native
macOS acceptance.

## Development and installed-runtime log separation

The mode of execution is authoritative. Invoking `./Cinter` lifecycle commands
or launching Terminal explicitly in repository/VS Code development mode uses the
sibling workspace `Builds` for developer artifacts, dependencies, caches,
and `Builds/Logs/CinterForgeTerminal/Runtime/` for development server logs.

The shared Xcode **Run/Debug** scheme passes `--developer`; the native
launcher resolves the workspace's sibling `Builds` from its Xcode build product
path (under `Builds/Xcode/Products`) **before** starting the Python server.
Its runtime logs are at `Builds/Logs/CinterForgeTerminal/Runtime/`, and its
managed `t` guest state is under `Builds/CFTVM/T`. Xcode Debug is not sandboxed
so its already-verified developer toolchains can run in the sibling workspace;
Release signing and installed-app mode remain unchanged. No files inside
`Contents/Resources/Builds` are used as writable developer state.

An installed/downloaded native app uses the actual installed Steam library for
writable state, optional DevTools, and live Server and shell session logs:

```text
<selected-steamapps>/common/CinterForge/Logs/CinterForgeTerminal/server-*.log
<selected-steamapps>/common/CinterForge/Logs/CinterForgeTerminal/Sessions/*.log
```

**The installed app must never create or write `Builds`.** The packaged
read-only `Contents/Resources/Builds` directory is merely the historical
resource closure name, not a runtime output location. The Server Terminal tab
streams the same authenticated log and displays its exact disk location.

## Where all results are stored

```text
<workspace>/Builds/Logs/CinterForgeTerminal/Lifecycle/
    YYYYMMDD-HHMMSS-ffffff-PID-setup-all.log
    YYYYMMDD-HHMMSS-ffffff-PID-setup-all.json
    latest-setup-all.json
    YYYYMMDD-HHMMSS-ffffff-PID-build-all.log
    latest-build-all.json
    YYYYMMDD-HHMMSS-ffffff-PID-stage-all.log
    latest-stage-all.json
    YYYYMMDD-HHMMSS-ffffff-PID-test-mac.log
    latest-test-mac.json
    YYYYMMDD-HHMMSS-ffffff-PID-all-all.log
    latest-all-all.json
```

Log names include unique UTC timestamps and process IDs. Nested phases each
write a log, and `all all` keeps a complete parent transcript. Each JSON result
records the command, UTC start/finish, exit code, duration, PASS/FAIL and exact
log location. An interrupted invocation records INTERRUPTED. `status all` is
read-only and does not create logs. Generated content stays under the sibling
Builds tree, **never under the source repository**.

## Public release (only after acceptance)

```sh
./Cinter publish cfterminal release
```

Only `release` uploads to the production destination. Omitting the destination
runs a local dryrun and checks both CDN connections without uploading.
`internal` uploads privately; `beta` uploads under the public `/beta/` folder.

Release Publish verifies uploads and public downloads, then updates the managed
macOS/Windows/Linux links at the top of README. Failed publication cannot
advance the README. If CDN propagation is delayed and a valid upload receipt
already exists, the supported lifecycle retry is:

```sh
./Cinter verify public
```

Both commands also write full logs and JSON results automatically. This does
not automatically commit/push README to Git. No `curl`/browser checks, manual
preflight or independent `python`/`node` commands are required of the builder.

## Validation status of this delivered source

- **Observed in a Linux development container for this single-suite source:**
  all six repository-structure and authored-input checks pass; the root
  `test.py` discovers 12 test cases in total. The six hardware-dependent
  Mac release checks cannot run in the Linux container.
- **Observed in the user's prior Mac lifecycle log before this restructuring:**
  Setup, Build and Stage passed, including actual Bedrock, cross-toolchain,
  Bash/VNC, app packaging and VM shutdown. The old simulated regression suite
  subsequently stalled during Test and was removed from this source.
- **Not observed for this new drop:** a full `./Cinter all all` on the actual
  Mac builder after the lifecycle/test separation, native Windows WebView2,
  native Linux WebKitGTK, live beta publish or first-boot cold acceptance.

Only update acceptance statuses from actual lifecycle logs.
