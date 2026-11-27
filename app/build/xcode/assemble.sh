#!/bin/bash
# CINTERFORGE SOURCE HEADER — app/build/xcode/assemble.sh
# PURPOSE: Runs Xcode source-edit Build & Run against the existing staged Host tools and prepares only the current app bundle.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# INTERFACE: This file declares presentation/build rules rather than callable methods.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
# Xcode is a native UI front-end to the existing CinterForgeTerminal lifecycle.
# This phase requires Setup to have provisioned all dependencies. It does not
# download tools, install SDKs, run Setup, or mutate source files.
set -euo pipefail
REPO=$(CDPATH= cd -- "${PROJECT_DIR:?}/.." && pwd -P)
cd "$REPO"
BUILDS="$REPO/../Builds"
if [[ ! -d "$BUILDS" ]]; then
  echo "error: CinterForgeTerminal Setup is missing: run ./Cinter setup all first" >&2
  exit 1
fi
# Xcode does not inherit the interactive shell's PATH. Use the toolchain
# installed by Setup for the native build and authored-resource refresh.
export PATH="$BUILDS/Toolchains/mac/env/bin:$PATH"
# An ordinary Xcode Build or Run is ALWAYS the local source-edit loop. Xcode
# uses ACTION=install for Archive, which is the only Xcode action that needs
# the signed Release producer. Selecting Release manually for Run must not
# silently trigger the full release lifecycle.
XCODE_ARCHIVE=0
if [[ "${ACTION:-build}" == install && "${CONFIGURATION:-Debug}" == Release ]]; then
  XCODE_ARCHIVE=1
  # This resolves signing references from the already-configured Xcode
  # Release identity. Apple notarization happens only in explicit Publish.
  IDENTITY="$(python3 -B -m host.mac.xcode_signing --field identity)"
  PROFILE="$(python3 -B -m host.mac.xcode_signing --field profile)"
  if [[ -n "$IDENTITY" ]]; then
    export CINTER_MAC_SIGN_IDENTITY="$IDENTITY"
    export CINTER_MAC_NOTARY_PROFILE="$PROFILE"
  fi
  echo "[Xcode] Archive: preparing signed Release bundle (no notarization)" >&2
else
  unset CINTER_MAC_SIGN_IDENTITY CINTER_MAC_NOTARY_PROFILE
  export CINTER_XCODE_DEBUG_NATIVE=1
  echo "[Xcode] Build & Run: fast local source build, configuration=${CONFIGURATION:-Debug}, action=${ACTION:-build}" >&2
fi
# Xcode Build & Run is a source-edit loop. Archive alone uses the release
# producer; Setup is still responsible for provisioning changed dependencies.
SOURCE="$BUILDS/CinterForgeTerminal/Applications/mac/arm64/Release/CinterForge Terminal.app"
DEST="${TARGET_BUILD_DIR:?}/${WRAPPER_NAME:?}"
if [[ "$XCODE_ARCHIVE" == 0 ]]; then
  # Compile current C/C++ and restage current Python, JS, HTML, and CSS.
  echo "[Xcode] Compiling native C/C++ with existing Host CMake target" >&2
  ./Cinter host build --application-shell
  echo "[Xcode] Refreshing staged Python/JS/HTML/CSS runtime" >&2
  ./Cinter host stage
  if [[ ! -f "$SOURCE/Contents/Resources/HostRelease.txt" ]]; then
    echo 'error: Initial runtime closure missing; run ./Cinter all all once before Xcode Debug.' >&2
    exit 1
  fi
  IFS= read -r HOST_RELEASE < "$SOURCE/Contents/Resources/HostRelease.txt"
  case "$HOST_RELEASE" in
    ''|*/*|*\\*) echo 'error: Invalid staged Host release identifier' >&2; exit 1 ;;
  esac
  HOST_STAGE="$BUILDS/Python/Host/mac/arm64/$HOST_RELEASE"
  if [[ ! -f "$HOST_STAGE/bin/CinterForgeTerminal" ]]; then
    echo 'error: Current native Host executable is missing after Build' >&2
    exit 1
  fi
  # Existing Code-OSS was acquired by Setup. Refresh only our authored
  # extension, which may also have changed since the last release build.
  echo "[Xcode] Refreshing authored Code-OSS extension" >&2
  python3 -B -c 'from pathlib import Path; from host.codeoss.producer import _stage_cinterforge_extension; import sys; _stage_cinterforge_extension(Path(sys.argv[1]))' \
    "$BUILDS/InteractiveFiction/CodeOSS/Web"
  echo "[Xcode] Updating local app bundle resources" >&2
  mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
  /usr/bin/ditto "$SOURCE/Contents/Resources" "$DEST/Contents/Resources"
  /usr/bin/ditto "$HOST_STAGE" "$DEST/Contents/Resources/Builds/Python/Host/mac/arm64/$HOST_RELEASE"
  # The signed release app contains the last RELEASE Code-OSS snapshot. Debug
  # must run the just-refreshed authored extension in the native Mac Code-OSS tab.
  /usr/bin/ditto "$BUILDS/InteractiveFiction/CodeOSS/Web" "$DEST/Contents/Resources/Builds/CodeOSS"
  if [[ "$REPO/app/build/resources/AppIcon.png" -nt "$DEST/Contents/Resources/AppIcon.icns" ]]; then
    echo "[Xcode] Refreshing authored Mac application icon" >&2
    python3 -B -c 'from pathlib import Path; from host.mac.mac_app import build_icns; import sys; build_icns(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))' \
      "$REPO/app/build/resources/AppIcon.png" "$DEST/Contents/Resources/AppIcon.icns" "$BUILDS"
  fi
  /usr/bin/ditto "$HOST_STAGE/bin/CinterForgeTerminal" "$DEST/Contents/MacOS/CinterForgeTerminal"
  echo "[Xcode] Build & Run refreshed macOS application sources only; Builds=$BUILDS"
else
  # Archive alone retains the existing signed Release Build producer.
  ./Cinter build mac
  ./Cinter host stage
  python3 -B -m host.mac.mac_app stage --builds "$BUILDS"
  if [[ ! -f "$SOURCE/Contents/MacOS/CinterForgeTerminal" ]]; then
    echo 'error: Cinter build did not produce the native macOS app' >&2
    exit 1
  fi
  mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
  /usr/bin/ditto "$SOURCE/Contents/MacOS/CinterForgeTerminal" "$DEST/Contents/MacOS/CinterForgeTerminal"
  /usr/bin/ditto "$SOURCE/Contents/Resources" "$DEST/Contents/Resources"
  echo "[Xcode] Embedded release runtime: $DEST"
fi
# Xcode owns final app Info.plist and signing phases.
