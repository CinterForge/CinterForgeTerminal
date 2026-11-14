"""Real CLI argument/daemon selection contract for the post-Bedrock AMD64 VM."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from host.amd64_backend import docker_bind_source
from host.windows_environment import WindowsCrossEnvironment


class Amd64ProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.builds = Path(self.temp.name) / 'Builds'
        self.builds.mkdir()
        self.docker = self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker'
        self.docker.parent.mkdir(parents=True)
        self.docker.write_text('#!/bin/sh\n'
                               'case "$*" in\n'
                               '  *Architecture*) echo "${TEST_GUEST_ARCH:-x86_64}";;\n'
                               '  *ServerVersion*) echo "29.5.2";;\n'
                               '  *) echo "$@";;\n'
                               'esac\n')
        self.docker.chmod(0o755)

    def test_cross_image_refuses_arm64_linuxkit_c(self):
        with mock.patch('host.windows_environment.platform.system', return_value='Darwin'), \
             mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-c'}):
            with self.assertRaisesRegex(RuntimeError, 'requires the t QEMU guest'):
                WindowsCrossEnvironment(self.builds)._docker_available()

    def test_cross_image_accepts_qemu_x86_64_guest(self):
        with mock.patch('host.windows_environment.platform.system', return_value='Darwin'), \
             mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-t', 'TEST_GUEST_ARCH': 'x86_64'}):
            env = WindowsCrossEnvironment(self.builds)
            self.assertEqual(env._docker_available(), str(self.docker))

    def test_cross_image_refuses_any_arm64_daemon_even_when_named_t(self):
        with mock.patch('host.windows_environment.platform.system', return_value='Darwin'), \
             mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-t', 'TEST_GUEST_ARCH': 'aarch64'}):
            with self.assertRaisesRegex(RuntimeError, 'Refusing to build AMD64 Wine'):
                WindowsCrossEnvironment(self.builds)._docker_available()

    def test_builds_bind_mounts_use_guest_visible_host_paths(self):
        folder = self.builds / 'With Spaces'
        folder.mkdir()
        with mock.patch('host.amd64_backend.platform.system', return_value='Darwin'), \
             mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-t'}):
            self.assertEqual(docker_bind_source(folder), '/host/mac' + str(folder.resolve()))
        with mock.patch('host.amd64_backend.platform.system', return_value='Linux'), \
             mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-t'}):
            self.assertEqual(docker_bind_source(folder), str(folder.resolve()))


if __name__ == '__main__':
    unittest.main()
