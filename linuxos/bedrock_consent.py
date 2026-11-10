"""Bedrock EULA gate for interactive developer Setup.

Packaged applications retain the WebView acceptance flow. Both flows write the
same BedrockManager receipt under sibling Builds; neither downloads anything.
"""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
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
    source = Path(__file__).resolve().parents[1] / 'webapp/CinterForgeTerminal.py'
    spec = spec_from_file_location('cinterforge_terminal_bedrock_license', source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Bedrock license manager missing: {source}')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    manager = module.BedrockManager(builds, platform_name)
    if manager.accepted():
        return

    print('\n[CinterForgeTerminal] Minecraft Bedrock Dedicated Server license', flush=True)
    print(f'Official EULA:          {module.EULA_URL}', flush=True)
    print(f'Official Privacy Policy: {module.PRIVACY_URL}', flush=True)
    print(f'Official server download: {module.DOWNLOAD_URL}', flush=True)
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
