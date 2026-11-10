"""Offline, exact-object tests for split public/internal Lagomorph lifecycle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host import lagomorph_cloudflare as sync
from host.universal_package import LAGOMORPH_STAGE, LAGOMORPH_PUBLIC_NAME, LAGOMORPH_FLOPPIES


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []

    def list(self, cfg, prefix):
        prefix += '/'
        return {k[len(prefix):]: {'key': k, 'size': len(v), 'etag': hashlib.md5(v).hexdigest()}
                for (bucket, k), v in self.objects.items()
                if bucket == cfg['bucket'] and k.startswith(prefix)}

    def put(self, cfg, key, path, **kwargs):
        payload = Path(path).read_bytes()
        self.objects[(cfg['bucket'], key)] = payload
        self.puts.append((cfg['bucket'], key))

    def get(self, cfg, key, path, expected_size=None):
        body = self.objects[(cfg['bucket'], key)]
        if expected_size is not None and len(body) != expected_size:
            raise RuntimeError('R2 size mismatch')
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self.gets.append((cfg['bucket'], key))


class LagomorphCloudflareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.builds = Path(self.tmp.name) / 'Builds'
        source = self.builds / 'Lagomorph'
        (source/'game'/'nested').mkdir(parents=True)
        (source/'empties'/'empty').mkdir(parents=True)
        (source/'game'/'nested'/'original.dat').write_bytes(b'authored-original-bytes')
        (source/'Lagomorph Manual.pdf').write_bytes(b'pdf-content')
        self.stage = self.builds / LAGOMORPH_STAGE
        self.stage.mkdir(parents=True)
        self.media = {name: self.stage / name
                      for name in (LAGOMORPH_PUBLIC_NAME, *LAGOMORPH_FLOPPIES)}
        for idx, file in enumerate(self.media.values()):
            file.write_bytes(b'HFS-test-image-' + bytes([idx]))
        (self.stage/'manifest.json').write_text('{"schema":"fixture"}')
        self.fake = FakeR2()
        def config(_path=None, *, channel):
            return {'bucket': 'cdn-assets-dev' if channel == 'dev' else 'cinterforge'}
        self.patches = [patch('host.universal_package._cloudflare_config', config),
                        patch.object(sync.r2, '_list_remote', self.fake.list),
                        patch.object(sync.r2, '_put_file', self.fake.put),
                        patch.object(sync.r2, '_get_file', self.fake.get)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _publish_fixture(self):
        index = sync.publish_private_source(self.builds, media=self.media)
        for name, path in self.media.items():
            self.fake.objects[('cinterforge', f'{sync.PUBLIC_ROOT}/{index["release"]}/{name}')] = path.read_bytes()
        return index

    def test_private_source_uploads_everything_public_receives_no_source(self):
        index = self._publish_fixture()
        self.assertEqual(len(index['files']), 2)
        self.assertIn('empties/empty', index['directories'])
        self.assertIn('game/nested/original.dat', index['files'])
        self.assertTrue(all(bucket == 'cdn-assets-dev' for bucket, _ in self.fake.puts))
        self.assertIn(('cdn-assets-dev', sync.INDEX_KEY), self.fake.puts)
        self.assertEqual(self.fake.puts[-1], ('cdn-assets-dev', sync.INDEX_KEY))
        self.assertEqual(index['schema'], sync.SNAPSHOT_SCHEMA)
        self.assertEqual(index['archive']['key'], sync.SOURCE_ARCHIVE_KEY)
        self.assertIn(('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY), self.fake.objects)
        self.assertNotIn(('cdn-assets-dev', sync.SOURCE_PREFIX + '/game/nested/original.dat'), self.fake.objects)
        self.assertNotIn(('cinterforge', sync.SOURCE_ARCHIVE_KEY), self.fake.objects)

    def test_pull_restores_nested_source_empty_directories_and_public_media(self):
        self._publish_fixture()
        source = self.builds/'Lagomorph'
        # Leave an unrelated local change intact; pull should not delete it.
        (source/'local-only.txt').write_text('my local change')
        (source/'game'/'nested'/'original.dat').write_bytes(b'corrupted')
        (source/'Lagomorph Manual.pdf').unlink()
        for p in self.media.values():
            p.unlink()
        (self.stage/'manifest.json').unlink()
        with patch('host.lagomorph_setup._classic_hfs_signature', lambda path: True):
            sync.pull_lagomorph(self.builds)
        self.assertEqual((source/'game'/'nested'/'original.dat').read_bytes(), b'authored-original-bytes')
        self.assertTrue((source/'empties'/'empty').is_dir())
        self.assertEqual((source/'local-only.txt').read_text(), 'my local change')
        self.assertEqual((self.stage/'manifest.json').read_text(), '{"schema":"fixture"}')
        self.assertEqual({p.name for p in self.media.values() if p.exists()}, set(self.media))

    def test_pull_rejects_remote_content_modified_after_manifest(self):
        self._publish_fixture()
        target = self.builds/'Lagomorph'/'game'/'nested'/'original.dat'
        target.write_bytes(b'old-local-version')
        self.fake.objects[('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY)] = b'X'*len(self.fake.objects[('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY)])
        with patch('host.lagomorph_setup._classic_hfs_signature', lambda path: True):
            with self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
                sync.pull_lagomorph(self.builds)
        self.assertEqual(target.read_bytes(), b'old-local-version')

    def test_pull_rejects_snapshot_path_traversal_without_mutating_builds(self):
        index = self._publish_fixture()
        index['files']['../escape.txt'] = {'bytes': 2, 'sha256': '0'*64}
        self.fake.objects[('cdn-assets-dev', sync.INDEX_KEY)] = (json.dumps(index)+'\n').encode()
        with self.assertRaisesRegex(ValueError, 'Unsafe Lagomorph remote'):
            sync.pull_lagomorph(self.builds)
        self.assertFalse((self.builds/'escape.txt').exists())

    def test_publish_refuses_symlink_to_external_content(self):
        other = Path(self.tmp.name) / 'outside.txt'
        other.write_text('not to be published')
        (self.builds/'Lagomorph'/'outside').symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            sync.publish_private_source(self.builds, media=self.media)
        self.assertEqual(self.fake.puts, [])

    def test_zip_contains_original_folder_and_empty_directories(self):
        import zipfile, io
        self._publish_fixture()
        data = self.fake.objects[('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY)]
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn('Lagomorph/game/nested/original.dat', archive.namelist())
            self.assertIn('Lagomorph/empties/empty/', archive.namelist())
            self.assertIn('Lagomorph/Lagomorph Manual.pdf', archive.namelist())
            self.assertEqual(archive.read('Lagomorph/game/nested/original.dat'), b'authored-original-bytes')

    def test_archive_member_sha_mismatch_does_not_modify_existing_files(self):
        import zipfile, io
        index = self._publish_fixture()
        target = self.builds/'Lagomorph'/'game'/'nested'/'original.dat'
        target.write_bytes(b'original-local')
        # A valid R2 digest for a different archive still cannot change the
        # files because each source is checked against the committed index.
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('Lagomorph/game/nested/original.dat', b'NOT-AUTHORED-BYTES!!!!')
            archive.writestr('Lagomorph/Lagomorph Manual.pdf', b'pdf-content')
            archive.writestr('Lagomorph/game/', b'')
            archive.writestr('Lagomorph/game/nested/', b'')
            archive.writestr('Lagomorph/empties/', b'')
            archive.writestr('Lagomorph/empties/empty/', b'')
        payload = raw.getvalue()
        self.fake.objects[('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY)] = payload
        index['archive'].update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.fake.objects[('cdn-assets-dev', sync.INDEX_KEY)] = (json.dumps(index)+'\n').encode()
        with self.assertRaisesRegex(RuntimeError, 'ZIP file (size|SHA-256) mismatch'):
            sync.pull_lagomorph(self.builds, source_only=True)
        self.assertEqual(target.read_bytes(), b'original-local')

    def test_classic_mac_forks_survive_private_zip_and_reapply_when_data_unchanged(self):
        import base64
        from unittest.mock import patch
        source = self.builds / 'Lagomorph/game/nested/original.dat'
        finder = b'APPL' + b'GAME' + bytes(24)
        fork = b'CLASSIC-MAC-RESOURCE-FORK' * 200
        payload = json.dumps({
            'com.apple.FinderInfo': base64.b64encode(finder).decode('ascii'),
            'com.apple.ResourceFork': base64.b64encode(fork).decode('ascii')
        }, sort_keys=True, separators=(',', ':')).encode()
        with patch.object(sync, '_read_mac_metadata', side_effect=lambda p: payload if p == source else None):
            index = self._publish_fixture()
        self.assertIn('game/nested/original.dat', index['mac_metadata'])
        self.assertEqual(index['schema'], sync.SNAPSHOT_SCHEMA)
        calls = []
        with patch.object(sync, '_apply_mac_metadata', side_effect=lambda path, data: calls.append((path, data))):
            # Same data fork already exists: Mac metadata must STILL be restored.
            sync.pull_lagomorph(self.builds, source_only=True)
        self.assertEqual(calls, [(source, payload)])
        import zipfile, io
        with zipfile.ZipFile(io.BytesIO(self.fake.objects[('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY)])) as archive:
            self.assertEqual(archive.read('MacMetadata/game/nested/original.dat.json'), payload)

    def test_classic_mac_fork_metadata_sha_must_match_index(self):
        import base64
        from unittest.mock import patch
        source = self.builds / 'Lagomorph/game/nested/original.dat'
        payload = json.dumps({'com.apple.FinderInfo': base64.b64encode(b'APPL'+bytes(28)).decode()}).encode()
        with patch.object(sync, '_read_mac_metadata', side_effect=lambda p: payload if p == source else None):
            index = self._publish_fixture()
        index['mac_metadata']['game/nested/original.dat']['sha256'] = '0' * 64
        self.fake.objects[('cdn-assets-dev', sync.INDEX_KEY)] = (json.dumps(index) + '\n').encode()
        target = self.builds / 'Lagomorph/game/nested/original.dat'
        original = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'metadata SHA-256 mismatch'):
            sync.pull_lagomorph(self.builds, source_only=True)
        self.assertEqual(original, target.read_bytes())

    def test_setup_source_only_pull_does_not_require_public_media(self):
        self._publish_fixture()
        source = self.builds / 'Lagomorph'
        (source / 'game/nested/original.dat').unlink()
        (source / 'Lagomorph Manual.pdf').unlink()
        for path in self.media.values():
            path.unlink()
        original_config = __import__('host.universal_package', fromlist=['_cloudflare_config'])._cloudflare_config
        def internal_only(config=None, *, channel):
            if channel != 'dev':
                raise AssertionError('Setup must not check the public Lagomorph bucket')
            return original_config(config, channel=channel)
        with patch('host.universal_package._cloudflare_config', internal_only):
            index = sync.pull_lagomorph(self.builds, source_only=True)
        self.assertIn('game/nested/original.dat', index['files'])
        self.assertEqual((source / 'game/nested/original.dat').read_bytes(), b'authored-original-bytes')
        self.assertEqual((source / 'Lagomorph Manual.pdf').read_bytes(), b'pdf-content')
        self.assertEqual({p.name for p in self.media.values() if p.exists()}, set())
        self.assertFalse(any(bucket == 'cinterforge' for bucket, key in self.fake.gets))

    def test_unversioned_internal_source_can_bootstrap_a_new_release(self):
        self._publish_fixture()
        index = sync.pull_lagomorph(self.builds, release='NEXT-VERSION', source_only=True)
        self.assertEqual(index['release'], sync.HOST_RELEASE)
        with patch('host.lagomorph_setup._classic_hfs_signature', lambda path: True):
            with self.assertRaisesRegex(RuntimeError, 'snapshot/release mismatch'):
                sync.pull_lagomorph(self.builds, release='NEXT-VERSION')

    def test_setup_can_read_pre_zip_snapshot_until_new_zip_is_published(self):
        index = self._publish_fixture()
        # Previous drop already published a v1 per-file index. A clean Setup
        # must be able to pull it before its first ZIP publication.
        index['schema'] = sync.LEGACY_SNAPSHOT_SCHEMA
        index['prefix'] = sync.SOURCE_PREFIX
        index.pop('archive')
        for rel, source in index['files'].items():
            self.fake.objects[('cdn-assets-dev', sync.SOURCE_PREFIX+'/'+rel)] = \
                (self.builds/'Lagomorph'/rel).read_bytes()
        self.fake.objects[('cdn-assets-dev', sync.INDEX_KEY)] = (json.dumps(index)+'\n').encode()
        self.fake.objects.pop(('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY))
        source = self.builds/'Lagomorph/game/nested/original.dat'
        source.unlink()
        restored = sync.pull_lagomorph(self.builds, source_only=True)
        self.assertEqual(restored['schema'], sync.LEGACY_SNAPSHOT_SCHEMA)
        self.assertEqual(source.read_bytes(), b'authored-original-bytes')

    def test_source_only_pull_requires_authentic_committed_snapshot(self):
        source = self.builds / 'Lagomorph'
        original = (source / 'game/nested/original.dat').read_bytes()
        with self.assertRaisesRegex(FileNotFoundError, 'snapshot missing'):
            sync.pull_lagomorph(self.builds, source_only=True)
        self.assertEqual((source / 'game/nested/original.dat').read_bytes(), original)

    def test_republish_skips_unchanged_large_source_objects(self):
        self._publish_fixture()
        self.fake.puts.clear()
        sync.publish_private_source(self.builds, media=self.media)
        self.assertNotIn(('cdn-assets-dev', sync.SOURCE_ARCHIVE_KEY), self.fake.puts)
        self.assertEqual(self.fake.puts[-1], ('cdn-assets-dev', sync.INDEX_KEY))


if __name__ == '__main__':
    unittest.main()
