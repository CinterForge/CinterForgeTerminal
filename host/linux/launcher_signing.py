"""Sign and validate the macOS Virtualization.framework launcher."""
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
