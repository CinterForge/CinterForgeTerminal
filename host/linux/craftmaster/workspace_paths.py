"""Same sibling Builds root consumed by the original Server/CFOS Bedrock scripts.

Dedicated to migrated CraftMaster scripts; never reads sibling source repos.
"""
from pathlib import Path

def cinterforgeBuildsRootPath(repo: Path) -> Path:
    repo = Path(repo).resolve()
    if not (repo / "Cinter").is_file():
        raise RuntimeError(f"Expected CinterForgeTerminal checkout: {repo}")
    return repo.parent / "Builds"
