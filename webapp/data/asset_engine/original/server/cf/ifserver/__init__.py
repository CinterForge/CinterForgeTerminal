# =============================================================================
# CINTERFORGE IF FILE PURPOSE
# Defines the Interactive Fiction package boundary.
# Implementation code is organized beneath model, world, language, reasoning, data, presentation, media, and engine.
# External CinterForge code should use cf.ifserver.main rather than reaching into internal modules.
# =============================================================================

"""CinterForge Interactive Fiction subsystem.

All implementation is organized into logical subpackages. Because ``if`` is a
Python keyword, outside Python code should load the single public entry point
with ``importlib.import_module("cf.ifserver.main")``. External callers must not
depend on internal package layout.
"""
