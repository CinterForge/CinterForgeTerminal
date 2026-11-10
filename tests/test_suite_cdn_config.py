"""R2 config discovery regressions for managed Mac Setup HOME."""
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest import mock

from host.publishing import cloudflare_r2_sync as r2

# Test fixture scratch stays under sibling Builds.
TEST_SCRATCH = Path(__file__).resolve().parents[2] / 'Builds/Intermediates/CinterForgeTerminalTests'


class ManagedCloudflareConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEST_SCRATCH.mkdir(parents=True, exist_ok=True)

    def test_login_home_cfdev_wins_when_vm_home_is_sandboxed(self):
        with tempfile.TemporaryDirectory(prefix='cinter-r2-', dir=TEST_SCRATCH) as folder:
            home = Path(folder) / 'login'
            sandbox = Path(folder) / 'Builds/CFTVM/home'
            sandbox.mkdir(parents=True)
            real_config = home / 'cfdev/config.json'
            real_config.parent.mkdir(parents=True)
            real_config.write_text('{"providers":{"cloudflare_r2_private":{"enabled":true,'
                '"bucket":"cdn-assets-dev","endpoint_url":"https://example.r2.cloudflarestorage.com",'
                '"access_key_id":"fake-test-key","secret_access_key":"fake-test-secret"}}}')
            with mock.patch.dict(os.environ, {'HOME': str(sandbox)}, clear=False), \
                 mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(home))):
                config = r2._load_config(sandbox/'cfdev/config.json', 'auto', required_bucket='cdn-assets-dev')
            self.assertEqual(config['bucket'], 'cdn-assets-dev')
            self.assertEqual(config['endpoint_url'], 'https://example.r2.cloudflarestorage.com')

    def test_named_dev_cdn_provider_with_dev_env_keys(self):
        """Real cfdev config shape: no cloudflare_r2 prefix on the provider name.

        Use synthetic credentials; real secrets must never be copied into tests.
        """
        import json
        with tempfile.TemporaryDirectory(prefix='cinter-r2-', dir=TEST_SCRATCH) as folder:
            config = Path(folder) / 'config.json'
            config.write_text(json.dumps({'providers': {'cdn-assets-dev': {
                'enabled': True, 'kind': 's3_compatible_object_storage',
                'env': {
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_ENABLED': '1',
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_BUCKET': 'cdn-assets-dev',
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_PUBLIC_BASE_URL': 'https://cdn-assets-dev.ucreg.io',
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_OBJECT_PREFIX': 'cdn-assets-dev',
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_ENDPOINT_URL': 'https://example.r2.cloudflarestorage.com',
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_REGION': 'auto',
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_ACCESS_KEY_ID': 'dummy-key',
                    'CINTERFORGE_CLOUDFLARE_R2_DEV_SECRET_ACCESS_KEY': 'dummy-secret',
                }}}}))
            # This tests the supplied fixture, not any real login user's R2
            # accounts (the production loader intentionally searches those).
            with mock.patch.object(r2, '_candidate_config_paths', return_value=[config]):
                result = r2._load_config(config, 'auto', required_bucket='cdn-assets-dev')
            self.assertEqual(result['bucket'], 'cdn-assets-dev')
            self.assertEqual(result['endpoint_url'], 'https://example.r2.cloudflarestorage.com')
            self.assertEqual(result['object_prefix'], 'cdn-assets-dev')
            self.assertEqual(result['access_key_id'], 'dummy-key')

    def test_named_dev_cdn_provider_never_used_for_public_bucket(self):
        import json
        with tempfile.TemporaryDirectory(prefix='cinter-r2-', dir=TEST_SCRATCH) as folder:
            config = Path(folder) / 'config.json'
            config.write_text(json.dumps({'cdn-assets-dev': {'enabled': True, 'env': {
                'CINTERFORGE_CLOUDFLARE_R2_DEV_BUCKET': 'cdn-assets-dev',
                'CINTERFORGE_CLOUDFLARE_R2_DEV_ENDPOINT_URL': 'https://example.r2.cloudflarestorage.com',
                'CINTERFORGE_CLOUDFLARE_R2_DEV_ACCESS_KEY_ID': 'dummy-key',
                'CINTERFORGE_CLOUDFLARE_R2_DEV_SECRET_ACCESS_KEY': 'dummy-secret',
            }}}))
            with mock.patch.object(r2, '_candidate_config_paths', return_value=[config]):
                with self.assertRaisesRegex(KeyError, "bucket 'cinterforge'"):
                    r2._load_config(config, 'auto', required_bucket='cinterforge')

    def test_cloudflare_config_never_switches_private_to_public_bucket(self):
        with tempfile.TemporaryDirectory(prefix='cinter-r2-', dir=TEST_SCRATCH) as folder:
            home = Path(folder)
            config = home / 'cfdev/config.json'
            config.parent.mkdir(parents=True)
            config.write_text('{"cloudflare_r2_public":{"enabled":true,"bucket":"cinterforge",'
                  '"endpoint_url":"https://example.r2.cloudflarestorage.com",'
                  '"access_key_id":"test","secret_access_key":"test"}}')
            with mock.patch.dict(os.environ, {'HOME': str(home),
                                               'CINTERFORGE_CLOUDFLARE_CONFIG': str(config)}), \
                 mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(home))):
                with self.assertRaisesRegex(KeyError, "bucket 'cdn-assets-dev'"):
                    r2._load_config(config, 'auto', required_bucket='cdn-assets-dev')
