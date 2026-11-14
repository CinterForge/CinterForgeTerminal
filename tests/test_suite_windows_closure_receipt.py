"""The Windows closure must not require the obsolete LinuxKit c VM receipt."""
from tests._support import *


class WindowsClosureReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.builds = Path(self.tmp.name) / 'Builds'
        self.builds.mkdir()
        self.mac = self.builds / 'CinterForgeTerminal/Toolchains/mac'
        for rel in ('env/bin/docker', 'env/bin/docker-buildx', 'env/bin/skopeo',
                    'qemu/bin/qemu-img', 'qemu/bin/qemu-system-x86_64',
                    'lima/bin/limactl', 'mamba/bin/micromamba',
                    'mamba-cache/cache.json', 'downloads/input.tar.gz',
                    'bootstrap.json', 'mac-packages-explicit.txt'):
            file = self.mac / rel
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('verified fixture')

    def test_closure_passes_with_no_c_receipt_and_preserves_qemu(self):
        # This is the real Setup topology: c has already stopped and t ran
        # the Wine/MSYS2 acceptance; no c backend receipt was ever emitted.
        self.assertFalse((self.builds / 'CFTVM/U/backend.json').exists())
        entries = dep._mac_transfer_entries(self.builds)
        self.assertNotIn('MacContainerBackend', entries)
        self.assertEqual(entries['MacQEMU'], self.mac / 'qemu')
        # The real ensure() call has already staged Windows dependencies.
        # Supply tiny validated stand-ins for its other required categories.
        for required in dep.REQUIRED:
            sample = self.builds / 'WindowsFixture' / required
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_bytes(b'verified Windows tool payload')
            entries[required] = sample
        report = dep._verify_staged_closure(self.builds, entries)
        self.assertEqual(report['status'], 'verified-local')
        self.assertEqual(report['categories']['MacQEMU'],
                         'CinterForgeTerminal/Toolchains/mac/qemu')
        self.assertTrue(all('CFTVM' not in path for path in report['categories'].values()))

    def test_obsolete_c_receipt_not_adopted(self):
        old = self.builds / 'CFTVM/U/backend.json'
        old.parent.mkdir(parents=True)
        old.write_text('{"schema":"stale","profile":"w"}')
        entries = dep._mac_transfer_entries(self.builds)
        self.assertNotIn('MacContainerBackend', entries)
        self.assertFalse(any(value == old.parent for value in entries.values()))

    def test_missing_qemu_binary_fails_closed(self):
        (self.mac / 'qemu/bin/qemu-system-x86_64').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Missing Mac cross-build executable'):
            dep._mac_transfer_entries(self.builds)

    def test_missing_portable_mac_input_fails_before_windows_provisioning(self):
        (self.mac / 'qemu/bin/qemu-img').unlink()
        with mock.patch.object(dep.platform, 'system', return_value='Darwin'), \
             mock.patch.object(dep, 'WindowsCrossEnvironment') as cross:
            with self.assertRaisesRegex(RuntimeError, 'Missing Mac cross-build executable'):
                dep.ensure(self.builds)
            cross.assert_not_called()
        report = json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-dependencies.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertIn('qemu-img', report['error'])

    def test_symlinked_mac_tool_root_is_rejected(self):
        qemu = self.mac / 'qemu'
        shutil.rmtree(qemu)
        other = self.builds / 'OtherQEMU'
        other.mkdir()
        qemu.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Missing staged macOS Windows-build dependency MacQEMU'):
            dep._mac_transfer_entries(self.builds)


if __name__ == '__main__':
    unittest.main()
