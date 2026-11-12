"""README download links: public verification gates, safety, and CLI wiring.

Only local source fixtures and mock publisher receipts are used. No CDN request,
upload, Git mutation, or native platform acceptance happens in this test file.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from io import StringIO
import hashlib
import json
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
        self.assertIn('/Distribution/CinterForgeTerminal-mac.dmg', updated)
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

    def test_initial_checked_in_readme_is_publisher_idempotent(self):
        # A real README may have been updated by a newer verified publish.
        # Only assert that a verified receipt yields a stable, repeatable block.
        self.path.write_bytes((REPO / 'README.md').read_bytes())
        release.update_readme_public_downloads(self.path, verified_receipt())
        self.assertFalse(release.update_readme_public_downloads(self.path, verified_receipt()))
        text = self.path.read_text(encoding='utf-8')
        self.assertEqual(text.count(release.README_DOWNLOADS_START), 1)
        self.assertEqual(text.count(release.README_DOWNLOADS_END), 1)
        for target in ('mac', 'linux', 'win', 'devtools'):
            self.assertIn(verified_receipt()['public_urls'][target], text)


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


class PublishChannelTests(unittest.TestCase):
    """Read-only dryrun, isolated CDN routes, and explicit CLI publication."""

    def setUp(self):
        TEST_SCRATCH.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='publish-channels-', dir=TEST_SCRATCH)
        self.addCleanup(temp.cleanup)
        self.builds = Path(temp.name) / 'Builds'
        self.builds.mkdir()
        self.distribution = self.builds / release.CLOUDFLARE_OBJECT_ROOT
        self.distribution.mkdir(parents=True)
        self.files = {}
        for name in ('four-artifacts.json',) + tuple(release.artifact_filename(t) for t in release.FOUR_TARGETS):
            path = self.distribution / name
            path.write_bytes(('fixture ' + name).encode('ascii'))
            self.files[name] = path
        self.report = {'schema': release.FOUR_SCHEMA, 'release': HOST_RELEASE,
                       'artifacts': {target: {
                           'filename': release.artifact_filename(target),
                           'bytes': self.files[release.artifact_filename(target)].stat().st_size,
                           'sha256': hashlib.sha256(
                               self.files[release.artifact_filename(target)].read_bytes()).hexdigest()}
                                     for target in release.FOUR_TARGETS}}

    def test_canonical_channels_target_separate_buckets_and_prefixes(self):
        self.assertEqual(release._publish_destination('internal'),
                         ('dev', 'CinterForgeTerminal/Distribution'))
        self.assertEqual(release._publish_destination('beta'),
                         ('public', 'CinterForgeTerminal/Distribution/beta'))
        self.assertEqual(release._publish_destination('release'),
                         ('public', 'CinterForgeTerminal/Distribution'))
        with self.assertRaisesRegex(ValueError, 'Unknown publishing channel'):
            release._publish_destination('unknown')

    def test_dryrun_checks_both_buckets_without_upload_or_download(self):
        from host.publishing import cloudflare_r2_sync as r2
        seen = []
        def cfg(_config=None, *, channel='dev'):
            return {'bucket': 'cdn-assets-dev' if channel == 'internal' else 'cinterforge'}
        def inspect(config, prefix):
            seen.append((config['bucket'], prefix))
            return {}
        with (mock.patch.object(release, '_distribution_files', return_value=(self.distribution, self.report, self.files)),
              mock.patch.object(release, '_cloudflare_config', side_effect=cfg),
              mock.patch.object(r2, '_list_remote', side_effect=inspect),
              mock.patch.object(r2, '_put_file', side_effect=AssertionError('dryrun uploaded')),
              mock.patch.object(r2, '_get_file', side_effect=AssertionError('dryrun downloaded')),
              mock.patch.object(release, '_require_notarized_mac_release', side_effect=AssertionError('dryrun gated'))):
            receipt = release._publish_four_archives(self.builds, release=HOST_RELEASE, channel='dryrun')
        self.assertFalse(receipt['uploaded'])
        self.assertEqual(seen, [('cdn-assets-dev', 'CinterForgeTerminal/Distribution'),
                                ('cinterforge', 'CinterForgeTerminal/Distribution')])
        self.assertFalse((self.builds / 'CinterForgeTerminal/Publish/Receipts').exists())

    def test_beta_publishes_only_to_beta_prefix_and_never_adds_lagomorph(self):
        from host.publishing import cloudflare_r2_sync as r2
        keys = []
        downloads = []
        def listing(cfg, prefix):
            return {}
        def download(builds, version, cfg, dest, *, prefix):
            downloads.append(prefix)
            return self.report
        with (mock.patch.object(release, '_distribution_files', return_value=(self.distribution, self.report, self.files)),
              mock.patch.object(release, '_cloudflare_config', return_value={'bucket': 'cinterforge'}),
              mock.patch.object(release, '_require_notarized_mac_release'),
              mock.patch.object(release, '_lagomorph_public_disk', side_effect=AssertionError('beta touched media')),
              mock.patch.object(r2, '_list_remote', side_effect=listing),
              mock.patch.object(r2, '_put_file', side_effect=lambda cfg, key, file, **kw: keys.append(key)),
              mock.patch.object(release, '_download_four_to', side_effect=download),
              mock.patch.object(release, '_finalize_public_receipt', side_effect=lambda path, receipt, report, files: receipt)):
            result = release._publish_four_archives(self.builds, release=HOST_RELEASE, channel='beta')
        self.assertEqual(downloads, ['CinterForgeTerminal/Distribution/beta'])
        self.assertEqual(result['channel'], 'beta')
        self.assertEqual(result['prefix'], 'CinterForgeTerminal/Distribution/beta')
        self.assertTrue(all(x.startswith('CinterForgeTerminal/Distribution/beta/') for x in keys))
        self.assertEqual(keys[-1], 'CinterForgeTerminal/Distribution/beta/four-artifacts.json')
        self.assertNotIn('Lagomorph-Quadra650.iso', result['files'])
        self.assertIn('/Distribution/beta/CinterForgeTerminal-mac.dmg', result['public_urls']['mac'])

    def test_changed_linux_upload_does_not_reupload_or_download_unchanged_devtools(self):
        from host.publishing import cloudflare_r2_sync as r2
        previous = json.loads(json.dumps(self.report))
        previous['artifacts']['linux']['sha256'] = '0' * 64
        old_bytes = json.dumps(previous).encode('utf-8')
        remote = {'four-artifacts.json': {'size': len(old_bytes), 'etag': 'old-manifest',
                                         'last_modified': '2026-11-12T13:00:00Z'}}
        for target in release.FOUR_TARGETS:
            name = release.artifact_filename(target)
            remote[name] = {'size': self.files[name].stat().st_size,
                            'etag': 'old-multipart-etag-2',
                            'last_modified': '2026-11-12T12:00:00Z'}
        uploaded = []
        downloads = []
        def download(builds, version, cfg, dest, *, prefix, reuse_verified=None):
            downloads.append(reuse_verified or {})
            return self.report
        with (mock.patch.object(release, '_distribution_files', return_value=(self.distribution, self.report, self.files)),
              mock.patch.object(release, '_cloudflare_config', return_value={'bucket': 'cinterforge'}),
              mock.patch.object(release, '_require_notarized_mac_release'),
              mock.patch.object(r2, '_list_remote', return_value=remote),
              mock.patch.object(r2, '_request', return_value=(200, {}, old_bytes)),
              mock.patch.object(r2, '_put_file', side_effect=lambda cfg, key, file, **kw: uploaded.append(key)),
              mock.patch.object(release, '_download_four_to', side_effect=download),
              mock.patch.object(release, '_finalize_public_receipt', side_effect=lambda path, receipt, report, files: receipt)):
            result = release._publish_four_archives(self.builds, release=HOST_RELEASE, channel='beta')
        self.assertEqual([key.rsplit('/', 1)[-1] for key in uploaded],
                         ['CinterForgeTerminal-linux.zip', 'four-artifacts.json'])
        self.assertEqual(len(downloads), 1)
        self.assertIn('CinterForgeTerminal-devtools.zip', downloads[0])
        self.assertEqual(result['files']['CinterForgeTerminal-devtools.zip'],
                         self.report['artifacts']['devtools']['sha256'])

    def test_builder_fetches_tiny_manifest_to_skip_remote_devtools_zip(self):
        from host.publishing import cloudflare_r2_sync as r2
        entry = {'target': 'devtools', 'filename': 'CinterForgeTerminal-devtools.zip',
                 'sha256': 'a' * 64, 'bytes': 123456, 'build_fingerprint': 'b' * 64}
        with (mock.patch.object(release, '_cloudflare_config', return_value={'bucket': 'cinterforge'}),
              mock.patch.object(r2, '_list_remote', return_value={}),
              mock.patch.object(release, '_remote_manifest_reusable', return_value={entry['filename']: entry}),
              mock.patch('host.unpacked_release._load', return_value={
                  'targets': {'devtools': {'fingerprint': 'b' * 64}}}),
              mock.patch.object(release, 'prepare_publish_packages') as packaging,
              mock.patch.object(release, '_publish_four_archives', return_value={'channel': 'beta'})):
            release.publish_four(self.builds, release=HOST_RELEASE, channel='beta')
        packaging.assert_called_once_with(self.builds, release=HOST_RELEASE,
                                          notarize=True, reuse_remote_devtools=entry)

    def test_remote_sdk_modified_after_manifest_is_not_reused(self):
        from host.publishing import cloudflare_r2_sync as r2
        prior = json.dumps(self.report).encode('utf-8')
        remote = {'four-artifacts.json': {'size': len(prior),
                                         'last_modified': '2026-11-12T12:00:00Z'}}
        for target in release.FOUR_TARGETS:
            name = release.artifact_filename(target)
            remote[name] = {'size': self.files[name].stat().st_size,
                            'last_modified': '2026-11-12T11:00:00Z'}
        remote['CinterForgeTerminal-devtools.zip']['last_modified'] = '2026-11-12T13:00:00Z'
        with mock.patch.object(r2, '_request', return_value=(200, {}, prior)):
            verified = release._remote_manifest_reusable({'bucket': 'cinterforge'},
                     'CinterForgeTerminal/Distribution/beta', remote, HOST_RELEASE)
        self.assertNotIn('CinterForgeTerminal-devtools.zip', verified)

    def test_download_skips_previously_verified_sdk_archive(self):
        from host.publishing import cloudflare_r2_sync as r2
        manifest = json.dumps(self.report).encode('utf-8')
        remote = {'four-artifacts.json': {'size': len(manifest)}}
        remote.update({name: {'size': path.stat().st_size} for name, path in self.files.items()
                       if name != 'four-artifacts.json'})
        downloads = []
        def fetch(config, key, dest, *, expected_size):
            name = key.rsplit('/', 1)[-1]
            downloads.append(name)
            dest.write_bytes(manifest if name == 'four-artifacts.json' else self.files[name].read_bytes())
        sdk = release.artifact_filename('devtools')
        # The Mac artifact fixture above contains text, not a real DMG.
        # Exercise the macOS verification call without invoking hdiutil on that
        # synthetic payload. Production verification remains unmodified.
        with (mock.patch('sys.platform', 'darwin'),
              mock.patch.object(r2, '_list_remote', return_value=remote),
              mock.patch.object(r2, '_get_file', side_effect=fetch),
              mock.patch('host.mac_dmg.verify_dmg') as verify_mac_dmg,
              mock.patch.object(release, '_verify_release_zip'),
              mock.patch.object(release, '_replace_generated_output', side_effect=lambda src,dst,builds: src.replace(dst))):
            result = release._download_four_to(
                self.builds, HOST_RELEASE, {'bucket': 'cinterforge'},
                self.builds / 'CinterForgeTerminal/Publish/TestDestination',
                prefix='CinterForgeTerminal/Distribution/beta',
                reuse_verified={sdk: self.report['artifacts']['devtools']['sha256']})
        verify_mac_dmg.assert_called_once_with(
            self.builds.resolve() / 'Intermediates/CinterForgeTerminal/CloudflareDownloads'
            / HOST_RELEASE / release.artifact_filename('mac'),
            self.builds, app_name=release.APP_ROOTS['mac'])
        self.assertEqual(result, self.report)
        self.assertNotIn(sdk, downloads)
        self.assertIn(release.artifact_filename('linux'), downloads)

    def test_publish_command_defaults_to_dryrun_and_updates_readme_only_for_release(self):
        cli = load_cinter()
        root = self.builds.parent
        (root / 'README.md').write_text('# Temporary README\n')
        with (mock.patch.object(cli, 'REPO', root),
              mock.patch.object(cli, 'BUILDS', self.builds),
              mock.patch.object(release, 'publish_four', return_value={
                  'channel': 'dryrun', 'bucket': 'n/a', 'prefix': 'n/a', 'release': HOST_RELEASE,
                  'connections': {}, 'uploaded': False}) as publish,
              redirect_stdout(StringIO())):
            self.assertEqual(cli.main(['publish']), 0)
            self.assertEqual(cli.main(['publish', 'cfterminal']), 0)
        self.assertEqual(publish.call_count, 2)
        self.assertTrue(all(call.kwargs == {'channel': 'dryrun'} for call in publish.call_args_list))
        self.assertEqual((root / 'README.md').read_text(), '# Temporary README\n')

    def test_publish_wrapper_dryrun_never_requests_apple_notarization(self):
        with (mock.patch.object(release, 'prepare_publish_packages') as packaging,
              mock.patch.object(release, '_publish_four_archives', return_value={'channel': 'dryrun'}) as upload):
            self.assertEqual(release.publish_four(self.builds, channel='dryrun')['channel'], 'dryrun')
        packaging.assert_called_once_with(self.builds, release=HOST_RELEASE, notarize=False, reuse_remote_devtools=None)
        upload.assert_called_once_with(self.builds, release=HOST_RELEASE, config=None, channel='dryrun')

    def test_combined_lifecycle_never_uploads_without_named_channel(self):
        cli = load_cinter()
        with (mock.patch.object(cli, 'invoke') as invoke,
              redirect_stdout(StringIO())):
            self.assertEqual(cli.main(['all', 'all', 'publish']), 0)
        self.assertEqual(invoke.call_count, 5)
        self.assertEqual(invoke.call_args_list[-1].args[-3:], ('publish', 'cfterminal', 'dryrun'))
        with (mock.patch.object(cli, 'invoke') as invoke,
              redirect_stdout(StringIO())):
            self.assertEqual(cli.main(['all', 'all', 'publish', 'beta']), 0)
        self.assertEqual(invoke.call_args_list[-1].args[-3:], ('publish', 'cfterminal', 'beta'))

    def test_cli_beta_and_internal_never_update_production_readme(self):
        cli = load_cinter()
        root = self.builds.parent
        (root / 'README.md').write_text('# Temporary README\n')
        with (mock.patch.object(cli, 'REPO', root),
              mock.patch.object(cli, 'BUILDS', self.builds),
              mock.patch.object(release, 'publish_four', return_value={
                  'channel': 'beta', 'bucket': 'cinterforge', 'prefix': 'CinterForgeTerminal/Distribution/beta'}) as publish,
              redirect_stdout(StringIO())):
            self.assertEqual(cli.main(['publish', 'cfterminal', 'beta']), 0)
            self.assertEqual(cli.main(['publish', 'cfterminal', 'internal']), 0)
            with self.assertRaisesRegex(ValueError, 'Usage'):
                cli.main(['publish', 'cfterminal', 'unknown'])
        self.assertEqual([call.kwargs['channel'] for call in publish.call_args_list], ['beta', 'dev'])
        self.assertEqual((root / 'README.md').read_text(), '# Temporary README\n')



if __name__ == '__main__':
    unittest.main()
