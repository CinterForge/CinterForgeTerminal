#!/bin/bash
# Xcode is a native UI front-end to the existing CinterForgeTerminal lifecycle.
# This phase requires Setup to have provisioned all dependencies. It does not
# download tools, install SDKs, run Setup, or mutate source files.
set -euo pipefail
cd "${PROJECT_DIR:?}"
BUILDS="${PROJECT_DIR}/../Builds"
if [[ ! -d "$BUILDS" ]]; then
  echo "error: CinterForgeTerminal Setup is missing: run ./Cinter setup all first" >&2
  exit 1
fi
if [[ "${CONFIGURATION:-Debug}" == Debug ]]; then
  # For Run/LLDB, Xcode itself owns outer app signing. Do not request
  # publication credentials or submit to the notary service during Debug.
  unset CINTER_MAC_SIGN_IDENTITY CINTER_MAC_NOTARY_PROFILE
  export CINTER_XCODE_DEBUG_NATIVE=1
fi
# For Archive, reuse the Xcode Release Team/Developer ID selection so the
# nested VM and Python Mach-O code are signed before Xcode seals the bundle.
# No network/notarization is triggered by this step.
if [[ "${CONFIGURATION:-Debug}" == Release ]]; then
  IDENTITY="$(python3 -B -m host.xcode_signing --field identity)"
  PROFILE="$(python3 -B -m host.xcode_signing --field profile)"
  if [[ -n "$IDENTITY" ]]; then
    export CINTER_MAC_SIGN_IDENTITY="$IDENTITY"
    export CINTER_MAC_NOTARY_PROFILE="$PROFILE"
  fi
fi
# Use the existing producer. Never add a competing compiler or Python Host.
./Cinter build mac
./Cinter host stage
# App composition is Build-owned, despite its historical internal stage name.
python3 -B -m host.mac_app stage --builds "$BUILDS"
SOURCE="$BUILDS/CinterForgeTerminal/Applications/mac/arm64/Release/CinterForge Terminal.app"
DEST="${TARGET_BUILD_DIR:?}/${WRAPPER_NAME:?}"
if [[ ! -f "$SOURCE/Contents/MacOS/CinterForgeTerminal" ]]; then
  echo "error: Existing Cinter build did not produce the native macOS app" >&2
  exit 1
fi
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
/usr/bin/ditto "$SOURCE/Contents/MacOS/CinterForgeTerminal" "$DEST/Contents/MacOS/CinterForgeTerminal"
/usr/bin/ditto "$SOURCE/Contents/Resources" "$DEST/Contents/Resources"
# Xcode's own ProcessInfoPlistFile and CodeSign phases supply the project
# metadata/certificate and sign this complete Xcode application product.
echo "[Xcode] Embedded the verified existing Terminal runtime: $DEST"
