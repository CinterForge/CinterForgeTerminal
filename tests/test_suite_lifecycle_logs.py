"""Lifecycle diagnostics remain visible after the Cinter console has closed.

No network, VM or release artifacts are required for these regression tests.
The intentionally failing Stage probe runs against an empty temporary Builds
root, so it can prove that failures are preserved without starting Setup.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
CINTER = REPOSITORY / 'Cinter'


class LifecycleLogTests(unittest.TestCase):
    def test_setup_preflight_on_current_authored_tree(self):
        module = runpy.run_path(str(CINTER), run_name='cinter_source_test')
        module['_preflight_source_layout']()

    def test_failed_stage_writes_full_log_and_machine_readable_result(self):
        with tempfile.TemporaryDirectory(prefix='cft-lifecycle-') as temporary:
            builds = Path(temporary) / 'Builds'
            environment = dict(os.environ)
            environment['CINTER_BUILDS_ROOT'] = str(builds)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            completed = subprocess.run(
                [sys.executable, '-B', str(CINTER), 'stage', 'all'],
                cwd=REPOSITORY, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=40,
            )
            self.assertNotEqual(completed.returncode, 0)
            folder = builds / 'Logs' / 'CinterForgeTerminal' / 'Lifecycle'
            latest = folder / 'latest-stage-all.json'
            self.assertTrue(latest.is_file(), completed.stdout)
            summary = json.loads(latest.read_text(encoding='utf-8'))
            self.assertEqual(summary['result'], 'FAIL')
            self.assertEqual(summary['exit_code'], completed.returncode)
            self.assertEqual(summary['command'], ['Cinter', 'stage', 'all'])
            log_path = Path(summary['log'])
            self.assertTrue(log_path.is_file())
            log_text = log_path.read_text(errors='replace')
            self.assertIn('Run ./Cinter build all before stage all', log_text)
            self.assertIn('RESULT FAIL', log_text)
            self.assertIn('Full log:', completed.stdout)
            self.assertTrue(list(folder.glob('*.json')))

    def test_status_does_not_create_lifecycle_logs(self):
        with tempfile.TemporaryDirectory(prefix='cft-read-only-') as temporary:
            builds = Path(temporary) / 'Builds'
            environment = dict(os.environ)
            environment['CINTER_BUILDS_ROOT'] = str(builds)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            subprocess.run(
                [sys.executable, '-B', str(CINTER), 'status', 'all'],
                cwd=REPOSITORY, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
            )
            folder = builds / 'Logs' / 'CinterForgeTerminal' / 'Lifecycle'
            self.assertFalse(folder.exists(), 'Read-only status unexpectedly wrote lifecycle logs')


if __name__ == '__main__':
    unittest.main()
