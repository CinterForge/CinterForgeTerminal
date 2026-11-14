"""Integration of Setup's native registry download -> one AMD64 daemon import.

Fake executables stand in for remote registries and Docker; no network access or
VM is claimed by this test. Their actual subprocess output and state is used.
"""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from host import windows_environment as windows

class NativeRegistryHandoffTests(unittest.TestCase):
    def test_real_subprocess_download_import_and_local_buildfile(self):
        with tempfile.TemporaryDirectory() as root:
            builds = Path(root) / 'Builds'
            builds.mkdir()
            mac = builds / 'CinterForgeTerminal/Toolchains/mac/env/bin'
            mac.mkdir(parents=True)
            # Reproduce the actual Mac failure: Conda installs an old v1
            # configuration. The operation must not modify this environment.
            legacy_conf = mac.parent / 'etc/containers/registries.conf'
            legacy_conf.parent.mkdir(parents=True)
            legacy_conf.write_text('[registries.search]\nregistries = [\"docker.io\"]\n')
            skopeo = mac / 'skopeo'
            skopeo.write_text('''#!/usr/bin/env python3
import sys, pathlib
# The actual Mac failure was the default Conda v1 registries.conf. Reject a
# missing/invalid override before acting like a successful registry download.
assert sys.argv[1] == '--registries-conf', sys.argv
registry_conf = pathlib.Path(sys.argv[2])
assert registry_conf.is_file(), registry_conf
assert registry_conf.read_text() == 'unqualified-search-registries = []\\n'
assert sys.argv[3:7] == ['--override-os', 'linux', '--override-arch', 'amd64']
print('NATIVE IMAGE DOWNLOAD STREAMED', flush=True)
archive = pathlib.Path(sys.argv[-1].split('docker-archive:', 1)[1].split('.tar:', 1)[0] + '.tar')
archive.write_bytes(b'fake-oci-archive')
''')
            skopeo.chmod(0o755)
            docker = mac / 'docker'
            docker.write_text('''#!/usr/bin/env python3
import pathlib, sys
state = pathlib.Path(__file__).with_name('imported.txt')
if sys.argv[1:3] == ['image', 'inspect']:
    if sys.argv[-1] in state.read_text().splitlines() if state.exists() else []:
        print('amd64')
        sys.exit(0)
    sys.exit(1)
if sys.argv[1] == 'load':
    source = pathlib.Path(sys.argv[-1])
    assert source.read_bytes() == b'fake-oci-archive'
    name = ':'.join(source.stem.rsplit('-', 1))
    # Local test derives the pinned reference from its filename.
    current = state.read_text() if state.exists() else ''
    state.write_text(current + name + '\\n')
    print('IMPORTED IMAGE STREAMED: ' + name, flush=True)
    sys.exit(0)
sys.exit(2)
''')
            # Use a stricter name mapping than a Docker daemon needs, to ensure
            # the generated OCI archive names are the exact pinned local tags.
            docker.chmod(0o755)
            env = windows.WindowsCrossEnvironment(builds)
            env.docker = str(docker)
            with mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-t'}):
                generated = env._mac_offline_base_dockerfile()
                text = generated.read_text()
                self.assertNotIn('FROM --platform=linux/amd64 ghcr.io/', text)
                self.assertNotIn('FROM --platform=linux/amd64 docker.io/', text)
                for remote, local in windows.AMD64_BASE_IMAGES:
                    self.assertNotIn(remote, text)
                    self.assertIn(local, text)
                self.assertFalse(list((builds / 'Intermediates/CinterForgeTerminal/RegistryInputs').glob('*.tar')))
                self.assertEqual((builds / 'Intermediates/CinterForgeTerminal/RegistryInputs/registries-v2.conf').read_text(), 'unqualified-search-registries = []\n')
                self.assertEqual(legacy_conf.read_text(), '[registries.search]\nregistries = [\"docker.io\"]\n')
                previous = (mac / 'imported.txt').read_text()
                self.assertEqual(previous.count('cinterforge-terminal-'), 2)
                again = env._mac_offline_base_dockerfile()
                self.assertEqual(again, generated)
                self.assertEqual((mac / 'imported.txt').read_text(), previous)

    def test_fails_closed_when_native_registry_tool_missing(self):
        with tempfile.TemporaryDirectory() as root:
            builds = Path(root) / 'Builds'
            builds.mkdir()
            env = windows.WindowsCrossEnvironment(builds)
            env.docker = 'docker'
            with mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-t'}):
                with self.assertRaisesRegex(RuntimeError, 'did not provision pinned native skopeo'):
                    env._mac_offline_base_dockerfile()

if __name__ == '__main__':
    unittest.main()
