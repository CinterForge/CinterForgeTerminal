"""Terminal's developer Setup consent adapter; CraftMaster owns EULA policy."""
from pathlib import Path
from typing import Callable


def require_bedrock_eula(builds: Path, platform_name: str = 'mac', *,
                          input_fn: Callable[[str], str] | None = None,
                          interactive: bool | None = None) -> None:
    from host.lifecycle.craftmaster_dependency import craftmaster_bedrock_module
    owner = craftmaster_bedrock_module(builds, 'CinterForgeLifecycleBedrock')
    owner.require_bedrock_eula(builds, platform_name, input_fn=input_fn,
                               interactive=interactive, app='CinterForgeTerminal')
