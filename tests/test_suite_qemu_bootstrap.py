"""Offline archive and actual-executable integration tests for QEMU staging.

Mac binaries can only be executed on macOS. This test validates Setup's integrity,
archive, executable and environment contracts using real Linux executables.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from host import mac_bootstrap


class QemuBootstrapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='qemu-bootstrap-')
        self.addCleanup(temp.cleanup)
        self.builds = Path(temp.name) / 'Builds'
        self.builds.mkdir()

    def _archive(self, *, malicious=False):
        root = self.builds / 'CinterForgeTerminal/Toolchains/mac/downloads'
        root.mkdir(parents=True)
        archive = root / f'{mac_bootstrap.QEMU_PACKAGE}-{mac_bootstrap.QEMU_VERSION}.tgz'
        with tarfile.open(archive, 'w:gz') as tar:
            paths = {
                'package/package.json': json.dumps({'name': mac_bootstrap.QEMU_PACKAGE,
                    'version': mac_bootstrap.QEMU_VERSION}).encode(),
                'package/share/qemu/bios.bin': b'bios',
            }
            for name in ('qemu-img', 'qemu-system-x86_64'):
                # Execute the actual fixture binary in the test; not a mock.
                script = b'#!/bin/sh\necho "QEMU emulator version test"\nexit 0\n' + b'# filler\n' * 140
                paths['package/bin/' + name] = script
            if malicious:
                paths['package/../escape'] = b'escape'
            for name, data in paths.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if '/bin/' in name else 0o644
                tar.addfile(info, io.BytesIO(data))
        integrity = base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
        return archive, integrity

    def test_pinned_portable_package_is_installed_then_reverified(self):
        _archive, integrity = self._archive()
        with mock.patch.object(mac_bootstrap, 'QEMU_SHA512_BASE64', integrity):
            binaries = mac_bootstrap.ensure_qemu(self.builds)
            again = mac_bootstrap.ensure_qemu(self.builds)
        self.assertEqual(binaries, again)
        self.assertTrue(Path(binaries['qemu-img']).is_file())
        self.assertTrue(Path(binaries['qemu-system-x86_64']).is_file())
        self.assertFalse((self.builds / 'CFTVM').exists())

    def test_sha512_tamper_is_rejected_before_execution(self):
        archive, _ = self._archive()
        archive.write_bytes(archive.read_bytes() + b'tamper')
        with self.assertRaisesRegex(RuntimeError, 'SHA-512 mismatch'):
            mac_bootstrap.ensure_qemu(self.builds)
        self.assertFalse((self.builds / 'CinterForgeTerminal/Toolchains/mac/qemu').exists())

    def test_archive_path_traversal_is_rejected(self):
        _archive, integrity = self._archive(malicious=True)
        with mock.patch.object(mac_bootstrap, 'QEMU_SHA512_BASE64', integrity):
            with self.assertRaisesRegex(RuntimeError, 'Unsafe QEMU archive member'):
                mac_bootstrap.ensure_qemu(self.builds)

    def test_runtime_sdk_requires_both_qemu_executables(self):
        from webapp import CinterForgeTerminal as terminal
        import inspect
        source = inspect.getsource(terminal.sdk_root)
        self.assertIn('mac/qemu/bin/qemu-system-x86_64', source)
        self.assertIn('mac/qemu/bin/qemu-img', source)
        source = inspect.getsource(terminal._env)
        self.assertIn('mac/qemu/bin', source)


    def test_unified_devtools_bundle_includes_prebuilt_qemu_closure(self):
        from host.universal_package import _devtools_assets
        roots = ('CinterForgeTerminal/Toolchains/mac',
                 'CinterForgeTerminal/Toolchains/WindowsCross',
                 'CinterForgeTerminal/Toolchains/Windows/MSYS2',
                 'Toolchains/WebView', 'Toolchains/WebView2')
        for root in roots:
            location = self.builds / root
            location.mkdir(parents=True, exist_ok=True)
            (location / 'test-staged-component').write_text('staged')
        base = self.builds / 'CinterForgeTerminal/Toolchains/mac/qemu'
        for relative in ('bin/qemu-system-x86_64', 'bin/qemu-img',
                         'lib/libglib-2.0.dylib', 'share/qemu/bios.bin',
                         'cinter-qemu-receipt.json'):
            output = base / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'fixture')
        archive = _devtools_assets(self.builds)
        prefix = 'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/qemu/'
        for relative in ('bin/qemu-system-x86_64', 'bin/qemu-img',
                         'lib/libglib-2.0.dylib', 'share/qemu/bios.bin',
                         'cinter-qemu-receipt.json'):
            self.assertIn(prefix + relative, archive)

    def test_managed_linuxkit_environment_advertises_setup_qemu(self):
        from linuxos import managed_backend
        with mock.patch.object(mac_bootstrap, 'SOCKET_PATH_LIMIT', 512), \
             mock.patch.object(managed_backend, 'backend_environment',
                 return_value={'LIMA_HOME': str(self.builds/'CFTVM/U/l'),
                               'COLIMA_HOME': str(self.builds/'CFTVM/U/c'),
                               'DOCKER_CONFIG': str(self.builds/'CFTVM/U/docker')}):
            result = mac_bootstrap.managed_environment(self.builds)
        self.assertIn('Toolchains/mac/qemu/bin', result['PATH'])


if __name__ == '__main__':
    unittest.main()
