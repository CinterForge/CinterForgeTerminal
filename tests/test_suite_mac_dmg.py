"""DMG contract tests with mocked macOS tools; no signing or image mount needed."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from host import mac_dmg


class DragToApplicationsDMGTests(unittest.TestCase):
    def setUp(self):
        repo = Path(__file__).resolve().parents[1]
        builds = Path(os.environ.get('CINTER_BUILDS_ROOT', repo.parent / 'Builds'))
        base = builds / 'Intermediates/CinterForgeTerminalTests'
        base.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix='dmg-contract-', dir=base)
        self.addCleanup(scratch.cleanup)
        self.builds = Path(scratch.name) / 'Builds'
        self.builds.mkdir()
        self.app = self.builds / 'Applications/Test App.app'
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        (self.app / 'Contents/Resources').mkdir(parents=True)
        (self.app / 'Contents/MacOS/TestApp').write_bytes(b'MachO fixture')
        (self.app / 'Contents/Resources/MyLogo.icns').write_bytes(b'icns fixture')
        with (self.app / 'Contents/Info.plist').open('wb') as stream:
            plistlib.dump({'CFBundleIconFile':'MyLogo'}, stream)
        self.dmg = self.builds / 'Applications/TestApp-mac.dmg'

    def test_generic_product_icon_uses_bundle_plist(self):
        with mock.patch.object(mac_dmg, '_run') as runner:
            mac_dmg._validate_app(self.app)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[0][:4],
                         ['/usr/bin/codesign', '--verify', '--deep', '--strict'])

    def test_rejects_symlinks_escaping_app(self):
        (self.app / 'Contents/Resources/escape').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(RuntimeError, 'Unsafe application bundle symlink'):
            mac_dmg._validate_app(self.app)

    def test_only_publish_creates_app_shortcut_readme_dmg(self):
        commands = []
        def fake_hdiutil(args):
            commands.append(args)
            if len(args) > 1 and args[1] == 'create':
                content = Path(args[args.index('-srcfolder') + 1])
                self.assertTrue((content / self.app.name / 'Contents/Info.plist').exists())
                self.assertTrue((content / 'Applications').is_symlink())
                self.assertEqual(os.readlink(content / 'Applications'), '/Applications')
                self.assertIn('Drag', (content / 'README.md').read_text())
                Path(args[-1]).write_bytes(b'fake-dmg-content')
        with mock.patch.object(mac_dmg, '_require_mac'), \
             mock.patch.object(mac_dmg, '_validate_app'), \
             mock.patch.object(mac_dmg, '_run', side_effect=fake_hdiutil), \
             mock.patch.object(mac_dmg, 'verify_dmg') as verify:
            result = mac_dmg.create_dmg(self.app, self.dmg, self.builds,
                                        volume_name='Test App')
        self.assertEqual(result['filename'], 'TestApp-mac.dmg')
        self.assertEqual(result['format'], 'dmg')
        self.assertEqual(result['sha256'], hashlib.sha256(b'fake-dmg-content').hexdigest())
        self.assertEqual(len([x for x in commands if x[1]=='create']), 1)
        verify.assert_called_once()
        self.assertFalse(verify.call_args.kwargs['public'])

    def test_notary_receipt_required_for_public_dmg(self):
        with mock.patch.object(mac_dmg, '_require_mac'), \
             mock.patch.object(mac_dmg, '_validate_app'), \
             mock.patch.object(mac_dmg, '_run', side_effect=lambda args:
                 Path(args[-1]).write_bytes(b'fake-dmg') if args[1]=='create' else None), \
             mock.patch.object(mac_dmg, 'verify_dmg'), \
             mock.patch.object(mac_dmg, '_notarize_dmg') as notary:
            mac_dmg.create_dmg(self.app, self.dmg, self.builds,
                               volume_name='Test App', public=True, notary_profile='stored-profile')
        notary.assert_called_once()
        self.assertEqual(notary.call_args.args[1], 'stored-profile')

    def test_mounted_dmg_always_detaches_on_error(self):
        self.dmg.write_bytes(b'image')
        commands = []
        with mock.patch.object(mac_dmg, '_require_mac'), \
             mock.patch.object(mac_dmg, '_run', side_effect=lambda args: commands.append(args)):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                with mac_dmg.mounted_dmg(self.dmg, self.builds):
                    raise RuntimeError('simulated')
        self.assertEqual([c[1] for c in commands], ['attach', 'detach'])
        self.assertIn('-readonly', commands[0])
        self.assertIn('-nobrowse', commands[0])

    def test_setup_replaces_only_app_with_atomic_backup(self):
        self.dmg.write_bytes(b'image')
        app = self.builds / 'CinterForgeTerminal/Applications/mac/arm64/Release/Test App.app'
        (app / 'Contents').mkdir(parents=True)
        (app / 'Contents/old.txt').write_text('previous app')
        @contextmanager
        def fake_mount(dmg, builds):
            self.assertEqual(dmg, self.dmg)
            yield self.app.parent
        with mock.patch.object(mac_dmg, '_require_mac'), \
             mock.patch.object(mac_dmg, 'mounted_dmg', side_effect=fake_mount), \
             mock.patch.object(mac_dmg, '_validate_app'):
            mac_dmg.restore_dmg(self.dmg, self.builds, app_name='Test App.app', destination=app)
        self.assertTrue((app / 'Contents/MacOS/TestApp').is_file())
        self.assertFalse((app / 'Contents/old.txt').exists())
        self.assertFalse((app.parent / 'Test App.app.setup-previous').exists())


if __name__ == '__main__':
    unittest.main()
