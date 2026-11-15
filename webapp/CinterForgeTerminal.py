"""CinterForge Terminal — native Python service entry and explicit command dispatch.

PURPOSE: Load independent runtime component owners and expose the two required
PythonHost RPC entry points, `handle` and `main`, in the existing file path.
Every actual HTTP handler, guest connection, downloader and process manager
now lives under runtime/CinterForge*.py with direct module-to-module callers.
STARTUP: Host worker imports this source; CLI uses it as its -B entry script.
SHUTDOWN: App-owned VM cleanup is handled by CinterForgeVMOperations and the
runtime watchdog, never host/lifecycle. No shared lifecycle implementation.
"""
from __future__ import annotations

from runtime import CinterForgeSettings
from runtime import CinterForgeApplication
from runtime import CinterForgeIF
from runtime import CinterForgeSessions
from runtime import CinterForgeAuthentication
from runtime import CinterForgeDiscovery
from runtime import CinterForgeDiscord
from runtime import CinterForgeHTTP
from runtime import CinterForgeService
from runtime import CinterForgeBedrock
from runtime import CinterForgeCommands
from runtime import CinterForgeDevTools
from runtime import CinterForgeRuntimeBootstrap
from runtime import CinterForgeGuestShells
from runtime import CinterForgeRuntimeWatchdog
from runtime import CinterForgeContainers
from runtime import CinterForgeVMOperations
from runtime import CinterForgeLaunchers

# External PythonHost and shell CLI contracts: these are the real owner methods.
handle = CinterForgeService.handle
main = CinterForgeApplication.main

if __name__ == '__main__':
    raise SystemExit(main())
