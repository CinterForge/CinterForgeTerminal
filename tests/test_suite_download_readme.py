"""README download links: public verification gates, safety, and CLI wiring.

Only local source fixtures and mock publisher receipts are used. No CDN request,
upload, Git mutation, or native platform acceptance happens in this test file.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from io import StringIO
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from host import HOST_RELEASE
from host import universal_package as release


REPO = Path(__file__).resolve().parents[1]
TEST_BUILDS = Path(os.environ.get('CINTER_BUILDS_ROOT', str(REPO.parent / 'Builds')))
TEST_SCRATCH = TEST_BUILDS / 'Intermediates/CinterForgeTerminalTests'


def verified_receipt(version: str = HOST_RELEASE) -> dict:
    return {
        'channel': 'public',
        'bucket': 'cinterforge',
        'prefix': 'CinterForgeTerminal/Distribution',
        'public_http': 'verified',
        'roundtrip': 'sha256-verified',
        'release': version,
        'public_urls': release._release_public_urls(version, include_lagomorph=True),
    }


def load_cinter():
    loader = SourceFileLoader('cinterforge_download_test_driver', str(REPO / 'Cinter'))
    specification = spec_from_loader(loader.name, loader)
    module = module_from_spec(specification)
    loader.exec_module(module)
    return module


class PublicReadmeDownloadTests(unittest.TestCase):
    def setUp(self):
        TEST_SCRATCH.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='readme-download-', dir=TEST_SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'README.md'
        self.path.write_text('# CinterForge Terminal\n\nPreserve this body.\n', encoding='utf-8')

    def test_public_verification_creates_three_correct_links_at_top(self):
        self.assertTrue(release.update_readme_public_downloads(self.path, verified_receipt()))
        text = self.path.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('# CinterForge Terminal\n\n'
                                        '<!-- CINTERFORGE:DOWNLOADS:START -->'))
        self.assertLess(text.index('## Download CinterForgeTerminal'), text.index('Preserve this body.'))
        for target in ('mac', 'win', 'linux', 'devtools'):
            expected = verified_receipt()['public_urls'][target]
            self.assertIn('](' + expected + ')', text)
        self.assertIn('Preserve this body.', text)
        self.assertEqual(text.count('<!-- CINTERFORGE:DOWNLOADS:START -->'), 1)
        self.assertEqual(text.count('<!-- CINTERFORGE:DOWNLOADS:END -->'), 1)
        self.assertFalse(release.update_readme_public_downloads(self.path, verified_receipt()))

    def test_new_release_replaces_only_download_section(self):
        release.update_readme_public_downloads(self.path, verified_receipt())
        prior = self.path.read_text()
        next_version = '2026.12.1'
        self.assertTrue(release.update_readme_public_downloads(
            self.path, verified_receipt(next_version)))
        updated = self.path.read_text()
        self.assertIn('Current public application archives (release `2026.12.1`)', updated)
        self.assertIn('/Distribution/CinterForgeTerminal-mac.zip', updated)
        self.assertIn('/Distribution/CinterForgeTerminal-devtools.zip', updated)
        self.assertEqual(updated.split(release.README_DOWNLOADS_END)[1],
                         prior.split(release.README_DOWNLOADS_END)[1])

    def test_failed_pending_or_private_publish_cannot_update(self):
        original = self.path.read_bytes()
        for patch_values in (
            {'public_http': 'pending'},
            {'public_http': 'failed'},
            {'channel': 'dev'},
            {'bucket': 'cdn-assets-dev'},
            {'roundtrip': 'pending'},
            {'release': '../bad'},
            {'public_urls': {'mac': 'https://attacker.example/file.zip'}},
        ):
            data = verified_receipt()
            data.update(patch_values)
            with self.subTest(changed=patch_values):
                with self.assertRaises(ValueError):
                    release.update_readme_public_downloads(self.path, data)
                self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_markers_fail_without_rewriting(self):
        original = '# CinterForge Terminal\n' + release.README_DOWNLOADS_START + '\n'
        self.path.write_text(original)
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            release.update_readme_public_downloads(self.path, verified_receipt())
        self.assertEqual(self.path.read_text(), original)

    def test_initial_checked_in_readme_is_current_release(self):
        self.path.write_bytes((REPO / 'README.md').read_bytes())
        self.assertFalse(release.update_readme_public_downloads(self.path, verified_receipt()))


class CinterPublicationReadmeTests(unittest.TestCase):
    def setUp(self):
        TEST_SCRATCH.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='cli-readme-', dir=TEST_SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'README.md').write_text('# CinterForge Terminal\n\nUnrelated text\n')
        self.cli = load_cinter()
        self.cli.REPO = self.root

    def test_public_publish_updates_readme_after_verified_upload(self):
        data = verified_receipt()
        with mock.patch.object(release, 'publish_four', return_value=data) as publisher, \
             redirect_stdout(StringIO()):
            self.assertEqual(self.cli.main(['publish', 'public']), 0)
        publisher.assert_called_once_with(self.cli.BUILDS, channel='public')
        self.assertIn(data['public_urls']['win'], (self.root / 'README.md').read_text())

    def test_private_publish_does_not_advertise_public_urls(self):
        report = {'bucket': 'cdn-assets-dev', 'prefix': 'Development'}
        original = (self.root / 'README.md').read_bytes()
        with mock.patch.object(release, 'publish_four', return_value=report) as publisher, \
             redirect_stdout(StringIO()):
            self.assertEqual(self.cli.main(['publish', 'all']), 0)
        publisher.assert_called_once_with(self.cli.BUILDS, channel='dev')
        self.assertEqual((self.root / 'README.md').read_bytes(), original)

    def test_public_verification_updates_readme_after_delayed_cdn(self):
        with mock.patch.object(release, 'verify_public_four', return_value=verified_receipt()) as verifier, \
             redirect_stdout(StringIO()):
            self.assertEqual(self.cli.main(['verify', 'public']), 0)
        verifier.assert_called_once_with(self.cli.BUILDS)
        self.assertIn('## Download CinterForgeTerminal',
                      (self.root / 'README.md').read_text())

    def test_publish_error_does_not_rewrite_readme(self):
        original = (self.root / 'README.md').read_bytes()
        with mock.patch.object(release, 'publish_four', side_effect=RuntimeError('HTTP 403')):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                self.cli.main(['publish', 'public'])
        self.assertEqual((self.root / 'README.md').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
