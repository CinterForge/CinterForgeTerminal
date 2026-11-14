"""Small offline checks of Code-OSS Setup progress and cached source reuse."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from webapp.codeoss import adapters, producer, setup
from webapp.codeoss.extension_bundle import build_extension_bundle


class CodeOSSSetupProgressTests(unittest.TestCase):
    def setUp(self):
        builds = Path(__file__).resolve().parents[2] / 'Builds'
        scratch = builds / 'Intermediates' / 'CinterForgeTerminalTests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='codeoss-progress-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_finder_metadata_cannot_force_codeoss_refresh(self):
        authored = self.root / 'authored'
        shutil.copytree(Path(setup.__file__).resolve().parent / 'extension', authored)
        staged = self.root / 'Web/extensions/cinterforge-if'
        staged.parent.mkdir(parents=True)
        shutil.copytree(authored, staged)
        build_extension_bundle(staged)
        (authored / '.DS_Store').write_bytes(b'finder metadata only')
        self.assertTrue(setup._extension_matches_source(self.root / 'Web', authored=authored))
        (authored / 'extension.js').write_text('// changed extension', encoding='utf-8')
        self.assertFalse(setup._extension_matches_source(self.root / 'Web', authored=authored))

    def test_pinned_local_git_commit_reuses_cache_without_fetch(self):
        if not shutil.which('git'):
            self.skipTest('Git is installed by Setup on the supported host')
        source = self.root / 'Source'
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        (source / 'README').write_text('pinned local checkout', encoding='utf-8')
        subprocess.run(['git', '-C', str(source), 'add', 'README'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'init'], check=True)
        commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(producer._sync_checkout(source, refresh=True, ref=commit), commit)
        self.assertIn('skipping network fetch', out.getvalue())

    def test_long_command_streams_output_and_reports_pass(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = adapters.run_process(
                [sys.executable, '-u', '-c', 'print("first step",flush=True); print("second step",flush=True)'],
                live=True, label='test build', timeout=5, inactivity_timeout=2,
                heartbeat=.2, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn('first step', stream.getvalue())
        self.assertIn('PASS test build', stream.getvalue())

    def test_no_output_stall_is_bounded_and_kills_owned_process(self):
        stream = io.StringIO()
        with redirect_stdout(stream), self.assertRaises(subprocess.TimeoutExpired):
            adapters.run_process(
                [sys.executable, '-u', '-c', 'import time; time.sleep(20)'],
                live=True, label='silent test', timeout=5, inactivity_timeout=.8,
                heartbeat=.2, capture_output=True, text=True,
            )
        self.assertIn('RUNNING silent test', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
