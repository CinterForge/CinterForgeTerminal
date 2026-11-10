"""Shared test imports/paths only. No product logic or VM provisioning."""
from __future__ import annotations

# The migrated LinuxKit modules still import sibling modules by bare name.
# Make their own package directory visible without a machine-specific path.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linuxos"))

# Standard Python test and subprocess facilities.
import contextlib
import ctypes
import hashlib
import importlib.machinery
import importlib.util
import io
import errno
import json
import os
import plistlib
import runpy
import shutil
import stat
import subprocess
import tempfile
import tarfile
import threading
import time
import unittest
import zipfile
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Modules under test. No test environment is installed outside sibling Builds.
from webapp.codeoss import producer, setup as code_setup
from webapp.codeoss.adapters import codeoss_root
from webapp.CinterForgeTerminal import BedrockManager
from host import (
    HOST_RELEASE, bootstrap_bundle, cross_build, devtooling, linux_runner, mac_bootstrap,
    manager, three_samples, toolchain, webview, win_cross, windows_environment,
    windows_install, windows_native,
)
from host import bootstrap_bundle as bootstrap
from host import mac_bootstrap as mac
from host import universal_package as universal
from host import windows_dependencies as dep
from host import windows_dependencies as deps
from host import windows_environment as win
from host import windows_msys2_cross as msys2
from host import windows_webview2_runtime as runtime
from host.environment_inputs import stage_requirements
from host.publishing import release
from host.windows_environment import WindowsCrossEnvironment
from linuxos import mac_tools
from linuxos.bedrock_consent import require_bedrock_eula


# ============================================================================
# Bootstrap Bundle — Runtime bundle validation tests; all scratch state stays under sibling Builds.
# ============================================================================
BOOTSTRAP_BUNDLE_BUILDS = Path(os.environ.get('CINTER_BUILDS_ROOT', str(Path(__file__).resolve().parents[2] / 'Builds')))



# Root-level product paths are calculated here, not copied between test modules.
ROOT = Path(__file__).resolve().parents[1]
LINUX_WINDOWS_CROSS_DISPATCH_ROOT = ROOT
CINTERFORGETERMINAL_COMMANDS_REPO = ROOT
CINTERFORGETERMINAL_COMMANDS_BUILDS = Path(os.environ.get('CINTER_BUILDS_ROOT', str(ROOT.parent / 'Builds')))
UNIVERSAL_PACKAGE_BUILDS = Path(os.environ.get('CINTER_BUILDS_ROOT', str(ROOT.parent / 'Builds'))).resolve()
TOOLCHAIN_SIGN_OF_LIFE_ROOT = ROOT.parent / 'Builds/Intermediates/CinterForgeTerminalTests'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def load_entry():
    spec = importlib.util.spec_from_loader('cinter_windows_cross_cli_test', SourceFileLoader('cinter_windows_cross_cli_test', str(ROOT / 'Cinter')))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
