"""Same sibling Builds root consumed by the original Server/CFOS Bedrock scripts.

Dedicated to migrated CraftMaster scripts; never reads sibling source repos.
"""
# CINTERFORGE SOURCE HEADER — host/linux/craftmaster/workspace_paths.py
# PURPOSE: Implements the Linux workspace paths owner for guest VM tooling, Bedrock runtime and cross-platform launcher integration.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   cinterforgeBuildsRootPath: Handle cinterforge builds root path through this component's established interfaces.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
from pathlib import Path

def cinterforgeBuildsRootPath(repo: Path) -> Path:
    repo = Path(repo).resolve()
    if not (repo / "Cinter").is_file():
        raise RuntimeError(f"Expected CinterForgeTerminal checkout: {repo}")
    return repo.parent / "Builds"
