# CinterForgeTerminal — Lifecycle-only testing instructions

**Updated:** 10 November 2026. This is part of the cumulative repository.

**The builder should run only the normal `./Cinter` lifecycle.** There are no
separate manual preflight, regression-test, browser, or log-inspection commands
required to complete this drop.

## Standard source build and verification (macOS builder)

```sh
./Cinter all all
```

This single command runs Setup → Build → Stage → native macOS Test. It does
**not** publish. Setup now checks repository inputs, expected directories,
terminal-style/example JSON and Python syntax automatically *before* expensive
provisioning. It also provisions cross-platform prerequisites and runs its
existing LinuxKit/Bedrock acceptance. Mac Test runs Python and JavaScript
source regressions, then the existing native macOS acceptance; cross-built
Windows and Linux applications are **not** reported as native accepted.

When diagnosing a lifecycle phase, supported independent commands remain:

```sh
./Cinter setup all
./Cinter build all
./Cinter stage all
./Cinter test mac
```

No other test commands are expected from the builder. If one fails, the full
console transcript and a machine-readable result are retained automatically.

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
./Cinter publish public
```

Publish verifies uploads and public downloads, then updates the managed
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

- **Observed in a Linux development container:** 281 Python tests run,
  1 skipped, 0 failures; six Node JavaScript syntax/smoke/guest checks passed.
- **Observed:** an intentionally unsuccessful Stage against empty Builds wrote
  its error transcript, FAIL JSON and latest-result pointer. Setup source
  preflight also completed on this cumulative source.
- **Not observed for this drop:** a full `./Cinter all all` on HOPE-DEV/Kevin,
  native Windows WebView2/ConPTY, native Linux WebKitGTK, live Cloudflare
  upload/download, Gatekeeper/notarization or Lagomorph cold first boot.

Only update acceptance statuses from actual lifecycle logs.
