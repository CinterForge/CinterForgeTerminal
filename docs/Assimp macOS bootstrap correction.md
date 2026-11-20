# Assimp macOS bootstrap correction — 2026-11-19

## Failure observed

`./Cinter setup all` successfully installed conda-forge `assimp 6.0.6` on
macOS ARM64 but failed because `host/mac/mac_bootstrap.py` required
`Builds/Toolchains/mac/env/bin/assimp`. The upstream conda package supplies
Assimp's prebuilt native library and C API headers; it does not install that
CLI on this platform. Re-running micromamba cannot manufacture that file.

## Fix

- Retain `assimp` in Terminal's native macOS micromamba dependency list.
- Detect and verify `include/assimp/cimport.h`, the native `libassimp*.dylib`,
  and its installed conda package metadata/version in
  `host/devtools/assimp_library.py`. Validate that the library stays within
  Terminal's owned toolchain prefix. Do not create a fake `assimp` executable.
- Make `host/mac/mac_bootstrap.py` check the SDK rather than requiring a nonexistent
  CLI; preserve regular tool checks, package locking, live progress, and Setup
  replay. `host/lifecycle/assets_dependency.py` uses the same SDK readiness gate.
- `devtools list` and the version/size report display the native `Assimp SDK`
  and its real library path. Linux/Windows `assimp` executables remain unchanged.
- During `./Cinter test`, compile a small C client **against the prebuilt**
  Assimp library with Xcode clang, then perform a real triangular-OBJ import.
  The C test program is source-controlled at
  `host/devtools/assimp_import_smoke.c`; no upstream Assimp rebuild is needed.

## Verify on macOS

```sh
./Cinter setup all
./Cinter build
./Cinter stage
./Cinter test
```

This patch was syntax-checked and verified using a synthetic installed Assimp
prefix, including missing-library and missing-header negative checks. The
macOS native import smoke, three-platform acceptance, and your continuing
Setup must still be executed in the real build environment.
