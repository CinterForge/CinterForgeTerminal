"""Retired installed-guest Stage copy.

All CinterForge applications use the one Setup-provisioned Builds/CFTVM/T VM.
Stage may install application files, but must never duplicate or replace a VM.
This compatibility guard intentionally rejects any remaining legacy callers.
"""
from __future__ import annotations


def stage_installed_guest(*_args, **_kwargs) -> None:
    raise RuntimeError('VM Stage-copy retired: all runtimes use the shared Builds/CFTVM/T')
