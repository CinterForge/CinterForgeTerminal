"""Launch-time fail-closed external service capabilities.

Credentials are read by the existing Python providers only. This module
never returns, persists, logs or places credentials in shell initialization,
HTML, session logs or HTTP responses. Checks are real signed service requests,
not presence tests of config keys. Disconnected/unavailable services are hidden
until the next application launch by design.
"""
from __future__ import annotations

import threading
from typing import Callable


def _bounded_probe(check: Callable[[], bool], seconds: float = 6.0) -> bool:
    result: list[bool] = []

    def run():
        try:
            result.append(bool(check()))
        except Exception:
            result.append(False)

    thread = threading.Thread(target=run, daemon=True, name='cinterforge-credential-probe')
    thread.start()
    thread.join(seconds)
    return bool(result and result[0])


def _discord_identity() -> bool:
    from runtime import CinterForgeDiscord
    identity = CinterForgeDiscord._discord_api('/users/@me')
    return bool(isinstance(identity, dict) and str(identity.get('id', '')).isdigit()
                and identity.get('bot') is True)


def detect() -> dict[str, bool]:
    """Probe buckets and Discord concurrently; return only boolean grants."""
    from runtime.assets import CinterForgeAssetCloudflare
    checks = {
        'cloudflare_internal': lambda: CinterForgeAssetCloudflare.probe_read('internal'),
        'cloudflare_public': lambda: CinterForgeAssetCloudflare.probe_read('release'),
        'discord': _discord_identity,
    }
    results: dict[str, bool] = {}
    threads = []
    for name, check in checks.items():
        def call(key=name, fn=check):
            results[key] = _bounded_probe(fn)
        thread = threading.Thread(target=call, daemon=True, name='cinterforge-capability-' + name)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(7.0)
    internal = results.get('cloudflare_internal', False)
    public = results.get('cloudflare_public', False)
    return {'cloudflare_internal': internal, 'cloudflare_public': public,
            'assets': internal or public, 'packs': False,  # Enabled when pack commands are implemented
            'publish': internal or public, 'discord': results.get('discord', False)}
