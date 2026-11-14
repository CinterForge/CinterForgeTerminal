#!/usr/bin/env python3
"""Invoke the copied, pinned Code-OSS browser production pipeline from Setup.

A complete existing Builds/InteractiveFiction/CodeOSS/Web is reused.  When
missing or invalid, the existing Server producer is called with refresh=True:
its source checkout, patches, Node/npm, build cache and staging semantics are
preserved. No new independent Code compiler or installer is introduced.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from codeoss import producer
    from codeoss.adapters import codeoss_root
    from codeoss.extension_bundle import render_extension_bundle
else:
    from . import producer
    from .adapters import codeoss_root
    from .extension_bundle import render_extension_bundle


def _extension_matches_source(web: Path, *, authored: Path | None = None) -> bool:
    """Check that the verified Web product contains the current authored extension.

    An upstream Code-OSS build can still be valid after our own extension has
    changed. Compare its actual staged bytes, not timestamps or a stored receipt.
    Bundling is deterministic and this check does not write to source or Builds.
    """
    source = Path(authored) if authored is not None else Path(__file__).resolve().parent / 'extension'
    staged = Path(web) / 'extensions/cinterforge-if'
    if not staged.is_dir():
        return False
    try:
        for authored in source.rglob('*'):
            if not authored.is_file():
                continue
            relative = authored.relative_to(source)
            # macOS Finder metadata is stripped from the production Web stage.
            # It is not authored extension content and must never force an
            # upstream Code-OSS rebuild (or an unnecessary Git fetch).
            if '.DS_Store' in relative.parts or '__pycache__' in relative.parts:
                continue
            target = staged / relative
            if not target.is_file():
                return False
            if relative.as_posix() == 'package.json':
                manifest = json.loads(authored.read_text(encoding='utf-8'))
                # The existing bundler replaces the extension's browser entry.
                manifest['browser'] = './extension.bundle.js'
                if manifest != json.loads(target.read_text(encoding='utf-8')):
                    return False
            elif authored.read_bytes() != target.read_bytes():
                return False
        return (staged / 'extension.bundle.js').read_text(encoding='utf-8') == \
            render_extension_bundle(source)
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return False


def setup(workspace: Path, *, refresh: bool = False) -> dict:
    workspace = Path(workspace).resolve()
    builds = workspace / 'Builds'
    if not builds.is_dir():
        raise RuntimeError(f'Shared Builds directory missing: {builds}')
    web = codeoss_root(workspace) / 'Web'
    if not refresh:
        try:
            producer.validate_codeoss_web(web)
            if _extension_matches_source(web):
                print(f'[CinterForgeTerminal Code] Reusing current verified Code-OSS Web build: {web}', flush=True)
                return {'web': str(web), 'reused': True}
            print('[CinterForgeTerminal Code] Staged extension differs from authored source; refreshing Web product', flush=True)
        except (RuntimeError, OSError) as error:
            print(f'[CinterForgeTerminal Code] Existing artifact missing/invalid: {error}', flush=True)
    # Invoke the original pinned producer; it owns acquisition, patches,
    # compilation, extension bundle and atomic Web staging.
    receipt = producer.ensure_codeoss(workspace, refresh=True)
    producer.validate_codeoss_web(web)
    print(f'[CinterForgeTerminal Code] Staged Code-OSS Web build: {web}', flush=True)
    return {**receipt, 'web': str(web), 'reused': False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--refresh', action='store_true', help='Force the original producer to refresh its pinned source and Web stage')
    args = parser.parse_args(argv)
    try:
        setup(args.workspace, refresh=args.refresh)
    except (OSError, RuntimeError, ValueError) as error:
        print(f'[CinterForgeTerminal Code] {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
