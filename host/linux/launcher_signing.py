"""Sign and validate the macOS Virtualization.framework launcher."""
# CINTERFORGE SOURCE HEADER — host/linux/launcher_signing.py
# PURPOSE: Implements the Linux launcher signing owner for guest VM tooling, Bedrock runtime and cross-platform launcher integration.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   has_virtualization_entitlement: Handle has virtualization entitlement through this component's established interfaces.
#   sign_launcher: Handle sign launcher through this component's established interfaces.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

ENTITLEMENT_KEY = "com.apple.security.virtualization"
ENTITLEMENTS = Path(__file__).with_name("CinterBDSVM.entitlements")


def has_virtualization_entitlement(binary: Path) -> bool:
    if not binary.is_file():
        return False
    signature = subprocess.run(
        ["codesign", "--verify", "--strict", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    if signature.returncode != 0:
        return False
    details = subprocess.run(
        ["codesign", "--display", "--entitlements", ":-", str(binary)],
        capture_output=True,
        check=False,
    )
    if details.returncode != 0:
        return False
    try:
        return plistlib.loads(details.stdout).get(ENTITLEMENT_KEY) is True
    except (ValueError, TypeError, plistlib.InvalidFileException):
        return False


def sign_launcher(binary: Path, *, identity: str = '-') -> None:
    if not ENTITLEMENTS.is_file():
        raise FileNotFoundError(f"Missing VM entitlement definition: {ENTITLEMENTS}")
    flags = (["--options", "runtime", "--timestamp"] if identity != '-'
             else ["--timestamp=none"])
    subprocess.run(
        ["codesign", "--force", "--sign", identity, *flags,
         "--entitlements", str(ENTITLEMENTS), str(binary)],
        check=True,
    )
    if not has_virtualization_entitlement(binary):
        raise RuntimeError(f"VM launcher lacks valid virtualization entitlement: {binary}")
