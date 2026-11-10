#!/usr/bin/env python3
"""Invoke the copied, pinned Code-OSS browser production pipeline from Setup.

A complete existing Builds/InteractiveFiction/CodeOSS/Web is reused.  When
missing or invalid, the existing Server producer is called with refresh=True:
its source checkout, patches, Node/npm, build cache and staging semantics are
preserved. No new independent Code compiler or installer is introduced.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from codeoss import producer
    from codeoss.adapters import codeoss_root
else:
    from . import producer
    from .adapters import codeoss_root


def setup(workspace: Path, *, refresh: bool = False) -> dict:
    workspace = Path(workspace).resolve()
    builds = workspace / 'Builds'
    if not builds.is_dir():
        raise RuntimeError(f'Shared Builds directory missing: {builds}')
    web = codeoss_root(workspace) / 'Web'
    if not refresh:
        try:
            producer.validate_codeoss_web(web)
            print(f'[CinterForgeTerminal Code] Reusing verified Code-OSS Web build: {web}', flush=True)
            return {'web': str(web), 'reused': True}
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
