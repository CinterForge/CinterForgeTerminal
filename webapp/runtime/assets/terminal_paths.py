"""Terminal host adapter for the original CinterForgeServer Assets modules.

Only replaces the source-checkout path provider. No asset identity, converter,
manifest, renderer or migration implementation exists in this adapter.
"""
from pathlib import Path

def cinterforgeBuildsRootPath(*args, **kwargs):
    from .asset_management import assets_root
    return assets_root().parent
