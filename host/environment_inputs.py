"""Input migration from Server's original requirements-driven environment setup.

Server owns and supplies requirements; CinterForgeTerminal owns the reusable Python
version selection, venv creation, dependency installation, fingerprint and
runtime injection already implemented in host.manager. Stage inputs inside
Builds/Python instead of importing anything from a sibling source checkout.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil


def stage_requirements(builds: Path, environment: str, source: Path) -> dict:
    from host.manager import ENV_PATTERN, python_root, SCHEMA
    if not ENV_PATTERN.fullmatch(environment):
        raise ValueError('Invalid environment name')
    builds = builds.resolve()
    root = python_root(builds)
    source = source.resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in ('.txt', '.in'):
        raise ValueError('Requirements source must be an existing text file')
    if source.is_relative_to(root) or source.is_relative_to(builds):
        raise ValueError('Requirements must come from an owning source, not generated Builds')
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    dest = root / 'Input/Requirements' / environment
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / 'requirements.txt'
    if out.exists() and out.read_bytes() != data:
        # Preserve the old version explicitly; Stage must be reproducible and
        # never silently mutate a foreign producer's published requirements.
        raise RuntimeError(f'Existing staged requirements differ: {out}; coordinate owner refresh')
    if not out.exists():
        out.write_bytes(data)
    # The generic Host manifest deliberately uses PEP508 platform markers in
    # the common requirements file and optional platform overlays. Blank
    # overlays are explicit inputs, not undeclared missing files.
    for key in ('mac', 'linux', 'win'):
        target = dest / ('requirements-' + key + '.txt')
        if not target.exists():
            target.write_text('# Optional platform-specific requirements; managed by application owner.\n', encoding='utf-8')
    manifest = root / 'Input/projects.json'
    if not manifest.is_file():
        sample = Path(__file__).resolve().parents[1] / 'build/projects.example.json'
        if not sample.is_file():
            raise FileNotFoundError(f'Host projects manifest missing: {sample}')
        template = json.loads(sample.read_text(encoding='utf-8'))
        if template.get('schema') != SCHEMA:
            raise RuntimeError('Host projects template has an unexpected schema')
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(template, indent=2) + '\n', encoding='utf-8')
    projects = json.loads(manifest.read_text(encoding='utf-8'))
    if environment not in projects.get('environments', {}):
        raise RuntimeError(f'Environment {environment} is absent from staged project manifest: {manifest}')
    profile = projects['environments'][environment]
    expected = f'Input/Requirements/{environment}/requirements.txt'
    if profile.get('requirements', {}).get('common') != expected:
        raise RuntimeError(f'Staged project manifest has different requirements owner for {environment}')
    receipt = {'schema': 'cinterforge.terminal.requirements-stage.v1',
               'environment': environment, 'sha256': digest,
               'bytes': len(data), 'source_name': source.name,
               'requirements': expected}
    (dest / 'source-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt
