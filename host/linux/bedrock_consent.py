"""Bedrock EULA gate for interactive developer Setup.

Packaged applications retain the WebView acceptance flow. Both flows write the
same EULA receipt format under sibling Builds; neither downloads anything.
Lifecycle uses its own CinterForgeLifecycleBedrock implementation.
"""
# CINTERFORGE SOURCE HEADER — host/linux/bedrock_consent.py
# PURPOSE: Implements the Linux bedrock consent owner for guest VM tooling, Bedrock runtime and cross-platform launcher integration.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   require_bedrock_eula: Require recorded consent before the existing Bedrock installer runs.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
from __future__ import annotations

from pathlib import Path
import sys
from typing import Callable


def require_bedrock_eula(builds: Path, platform_name: str = 'mac', *,
                          input_fn: Callable[[str], str] | None = None,
                          interactive: bool | None = None) -> None:
    """Require recorded consent before the existing Bedrock installer runs.

    The prompt is intended for developers using ./Cinter setup all in a real
    terminal. A noninteractive runner must use an existing explicit acceptance
    receipt (for example, one collected in the trusted app WebView).
    """
    builds = Path(builds).resolve()
    from host.lifecycle.CinterForgeLifecycleBedrock import (
        CinterForgeLifecycleBedrock, EULA_URL, PRIVACY_URL, DOWNLOAD_URL,
    )
    manager = CinterForgeLifecycleBedrock(builds, platform_name)
    if manager.accepted():
        return

    print('\n[CinterForgeTerminal] Minecraft Bedrock Dedicated Server license', flush=True)
    print(f'Official EULA:          {EULA_URL}', flush=True)
    print(f'Official Privacy Policy: {PRIVACY_URL}', flush=True)
    print(f'Official server download: {DOWNLOAD_URL}', flush=True)
    print('Review these terms before accepting. No server download has started.', flush=True)

    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise RuntimeError('Bedrock EULA acceptance is required. Run ./Cinter setup all '
                           'in an interactive terminal to review the official links '
                           'and accept, or use the trusted application WebView.')

    response = input_fn if input_fn is not None else input
    try:
        answer = response("Type 'accept' to agree to the Minecraft Bedrock EULA "
                          "and Privacy Policy (anything else declines): ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise RuntimeError('Bedrock EULA not accepted; installation cancelled') from exc
    if answer.strip().lower() != 'accept':
        raise RuntimeError('Bedrock EULA not accepted; no server download is permitted')
    manager.accept(True)  # Same acceptance receipt as the trusted WebView.
    print('[CinterForgeTerminal] Bedrock EULA acceptance recorded under Builds.', flush=True)
