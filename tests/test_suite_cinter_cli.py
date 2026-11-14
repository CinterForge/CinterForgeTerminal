"""Cinter source-management integration: offline, no checkout mutation or network."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
import subprocess


CINTER_PATH = Path(__file__).resolve().parents[1] / 'Cinter'


def _load_command_driver():
    """Load extensionless ./Cinter as a module without launching its CLI."""
    loader = SourceFileLoader('cinterforge_test_driver', str(CINTER_PATH))
    specification = spec_from_loader(loader.name, loader)
    module = module_from_spec(specification)
    loader.exec_module(module)
    return module


class CinterSourceManagementTests(TestCase):
    def setUp(self):
        self.cli = _load_command_driver()
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        manifest = self.root / 'sources.csv'
        manifest.write_text(
            'Name,Git Repo URL,GitHub Fork Source URL,Source Acquisition\n'
            'cinterforge-webview,https://github.com/webview/webview.git,,source\n',
            encoding='utf-8')
        self.cli.SOURCE_REPOSITORIES_CSV = manifest
        self.cli.SOURCE_WORKSPACE_ROOT = self.root

    def test_four_target_mac_build_restores_setup_managed_path(self):
        """The `all all` subprocess does not inherit Setup's PATH changes."""
        from host import amd64_backend, linux_runner, mac_bootstrap, universal_package, xcode_signing
        commands = []
        def record_invoke(*args):
            commands.append((args, os.environ.get('PATH', ''), os.environ.get('DOCKER_CONTEXT')))
        @contextmanager
        def existing_backend(builds, *, setup=False):
            self.assertFalse(setup, 'Build must never provision the VM')
            yield '/usr/bin/docker'
        root = self.root / 'Builds'
        root.mkdir()
        self.cli.BUILDS = root
        managed_bin = str(root / 'CinterForgeTerminal/Toolchains/mac/env/bin')
        with (mock.patch.object(self.cli.platform, 'system', return_value='Darwin'),
              mock.patch.object(self.cli, 'invoke', side_effect=record_invoke),
              mock.patch.object(xcode_signing, 'release_signing_environment', return_value={}),
              mock.patch.object(mac_bootstrap, 'managed_environment',
                                return_value={'PATH': managed_bin + os.pathsep + '/usr/bin'}),
              mock.patch.object(amd64_backend, 'managed_amd64_backend', side_effect=existing_backend),
              mock.patch.object(linux_runner, 'build_terminal'),
              mock.patch.object(universal_package, 'build_four',
                                return_value={'artifacts': {'mac': {}, 'linux': {}, 'win': {}, 'devtools': {}}}),
              mock.patch.dict(os.environ, {'PATH': '/usr/bin', 'DOCKER_CONTEXT': 'stale'})):
            self.assertEqual(self.cli.main(['build', 'all']), 0)
            self.assertTrue(commands)
            for args, path, docker_context in commands:
                self.assertEqual(path.split(os.pathsep)[0], managed_bin, args)
                self.assertIsNone(docker_context, args)
            self.assertTrue(linux_runner.build_terminal.called)

    def test_missing_checkout_dry_run_does_not_contact_network_or_write(self):
        messages = StringIO()
        with mock.patch.object(self.cli.subprocess, 'run', side_effect=AssertionError('network call')), \
                redirect_stdout(messages), redirect_stderr(StringIO()):
            result = self.cli.main(['clone', 'all', '--dry-run'])
        self.assertEqual(result, 1)
        self.assertIn('MISSING', messages.getvalue())
        self.assertFalse((self.root / 'cinterforge-webview').exists())

    def test_existing_checkout_dry_run_preserves_existing_git_directory(self):
        destination = self.root / 'cinterforge-webview'
        (destination / '.git').mkdir(parents=True)
        sentinel = destination / 'user-changes.txt'
        sentinel.write_text('must survive', encoding='utf-8')
        def local_git_only(arguments, **kwargs):
            self.assertEqual(arguments[:2], ['git', '-C'])
            self.assertEqual(arguments[3:], ['rev-parse', '--is-inside-work-tree'])
            return subprocess.CompletedProcess(arguments, 0, stdout='true\n')
        with mock.patch.object(self.cli.subprocess, 'run', side_effect=local_git_only), \
                redirect_stdout(StringIO()):
            result = self.cli.main(['clone', 'all', '--dry-run'])
        self.assertEqual(result, 0)
        self.assertEqual(sentinel.read_text(), 'must survive')

    def test_single_fork_uses_selected_name_not_undefined_loop_variable(self):
        created = []
        def github_info(value):
            if value == 'webview/webview':
                return {'full_name': value}
            return None
        with mock.patch.object(self.cli.shutil, 'which', return_value='/usr/bin/gh'), \
                mock.patch.object(self.cli, '_source_command', return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(self.cli, '_source_gh_info', side_effect=github_info), \
                mock.patch.object(self.cli, '_source_create_github_fork', side_effect=lambda *args: created.append(args)), \
                redirect_stdout(StringIO()):
            result = self.cli.main(['fork', 'cinterforge-webview'])
        self.assertEqual(result, 0)
        self.assertEqual(created, [('webview/webview', 'webview', 'cinterforge-webview')])
