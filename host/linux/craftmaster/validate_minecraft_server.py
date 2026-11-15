#!/usr/bin/env python3
"""Minecraft Bedrock runtime acceptance, invoked by the owning CFOS/Cinter Test.

The CFOS LinuxKit test uses its own isolated official BDS installation under
Builds/Intermediates. Consumer smoke uses the persistent Applications product.
Neither path constructs a second LinuxKit or launches it from update-data.
"""
from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
import sys

# The validator runs directly from its file during offline acceptance, and
# can also be imported as a package by source-level lifecycle tests.
if __package__:
    from .workspace_paths import cinterforgeBuildsRootPath
    from .build_craftmaster_server import resolve_product_root, smoke
else:
    from workspace_paths import cinterforgeBuildsRootPath
    from build_craftmaster_server import resolve_product_root, smoke



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--cfos-linuxkit-test-server", type=Path,
                        help="CFOS-owned isolated Bedrock test installation")
    parser.add_argument("--cfos-root", type=Path,
                        help="CFOS source checkout providing the tested LinuxKit launcher")
    args = parser.parse_args()

    host = platform.system()
    if host not in ("Darwin", "Windows", "Linux"):
        raise RuntimeError(f"Unsupported Minecraft acceptance host: {host}")

    if args.cfos_linuxkit_test_server:
        if host != 'Darwin' or not args.cfos_root:
            raise RuntimeError('CFOS LinuxKit acceptance requires a macOS host and --cfos-root')
        server = args.cfos_linuxkit_test_server.resolve()
        if not server.is_relative_to((cinterforgeBuildsRootPath(args.repo_root.resolve()) / 'Intermediates').resolve()):
            raise RuntimeError('CFOS LinuxKit acceptance must use isolated Builds/Intermediates test server')
        if not (server / 'bedrock_server').is_file():
            raise RuntimeError(f'CFOS LinuxKit test install missing bedrock_server: {server}')
        cli = (args.cfos_root.resolve() / 'Cinter')
        if not cli.is_file():
            raise RuntimeError(f'CFOS launcher missing: {cli}')
        if __package__:
            from .craftmaster_mac_bootstrap import smoke as smoke_mac
        else:
            from craftmaster_mac_bootstrap import smoke as smoke_mac
        proof = smoke_mac(None, server, launcher_command=[
            str(cli), 'launch', 'cinterforge-os', 'linuxkit', 'Release',
            '--server', str(server),
        ])
        if proof.get('status') != 'pass':
            raise RuntimeError(f'CFOS LinuxKit Bedrock acceptance failed: {proof}')
        print('[minecraft-acceptance] CFOS LINUXKIT PASS ' + json.dumps(proof, sort_keys=True), flush=True)
        return 0

    product_root = resolve_product_root()
    platform_directory = "Windows" if host == "Windows" else "Linux"
    server = product_root / "Applications" / platform_directory / "Minecraft Bedrock Server"
    executable_name = "bedrock_server.exe" if host == "Windows" else "bedrock_server"
    executable = server / executable_name
    if not executable.is_file() or executable.is_symlink():
        raise RuntimeError(
            f"Persistent Minecraft installation missing {executable_name}: {server}. "
            "Smoke cannot use Builds/CraftMaster/Server as a fallback."
        )
    if not server.resolve().is_relative_to((product_root / "Applications").resolve()):
        raise RuntimeError(f"Minecraft server resolves outside persistent Applications: {server}")

    print(f"[minecraft-acceptance] Persistent server: {server}", flush=True)
    print("[minecraft-acceptance] Builds server is NOT used for runtime smoke", flush=True)
    if host == "Darwin":
        # An ordinary installed server is also tested with the CFOS-owned VM;
        # Test may not recreate the retired MacBootstrap construction path.
        cfos = args.cfos_root or args.repo_root
        cli = cfos.resolve() / 'Cinter'
        if not cli.is_file():
            raise RuntimeError(f'CFOS LinuxKit launcher missing: {cli}')
        if __package__:
            from .craftmaster_mac_bootstrap import smoke as smoke_mac
        else:
            from craftmaster_mac_bootstrap import smoke as smoke_mac
        proof = smoke_mac(None, server, launcher_command=[
            str(cli), 'launch', 'cinterforge-os', 'linuxkit', 'Release',
            '--server', str(server),
        ])
    else:
        proof = smoke(server)

    if proof.get("status") != "pass":
        raise RuntimeError(f"Minecraft server smoke did not pass: {proof}")
    print("[minecraft-acceptance] PASS " + json.dumps(proof, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
