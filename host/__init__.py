"""CinterForgeTerminal Host lifecycle and managed release identifier.

Build and Stage reconcile build-owned files in the same version-labelled
working directories. The release number identifies a distribution, not a
reason to reject normal source changes or generated runtime files.
"""
# CINTERFORGE SOURCE HEADER — host/__init__.py
# PURPOSE: Maintains   init   within CinterForgeTerminal without duplicating external ownership.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# INTERFACE: HOST_RELEASE selects the current unified distribution/stage identifier; importing the package does not start a service.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.

HOST_RELEASE = '2026.11.23'
