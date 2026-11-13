"""Windows MSI/setup and target-scoped publication contracts (offline fixtures)."""
from __future__ import annotations

from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from host import HOST_RELEASE
from host import msi_installer as msi
from host import target_publish

REPO = Path(__file__).resolve().parents[1]
# Dispatch tests use a simulated signer; the real release-signing resolver
# is tested independently and publication still refuses a missing key.
TEST_RELEASE_SIGNING = {'CINTER_MAC_SIGN_IDENTITY': 'Developer ID Application: fixture',
                        'CINTER_MAC_NOTARY_PROFILE': ''}


def cli_driver():
    loader = SourceFileLoader('cinterforge_msi_test_driver', str(REPO / 'Cinter'))
    spec = spec_from_loader(loader.name, loader)
    result = module_from_spec(spec)
    loader.exec_module(result)
    return result


class WindowsMSIContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.builds = Path(self.temp.name) / 'Builds'
        self.builds.mkdir()

    def test_wix_document_has_stable_upgrade_identity_per_machine_and_no_sdk(self):
        readme = self.builds / 'readme.txt'
        xml = ET.fromstring(msi._document({'Terminal.cmd': readme,
                                             'CinterForge Terminal.exe': readme},
                                            release='2026.11.23'))
        ns = {'w': msi.NS}
        product = xml.find('w:Product', ns)
        self.assertEqual(product.attrib['Version'], '26.11.23')
        self.assertEqual(product.attrib['UpgradeCode'], msi.UPGRADE_CODE)
        self.assertEqual(product.find('w:Package', ns).attrib['InstallScope'], 'perMachine')
        self.assertNotIn('Platform', product.find('w:Package', ns).attrib)
        self.assertIsNotNone(product.find('w:MajorUpgrade', ns))
        self.assertEqual(product.find('w:MediaTemplate', ns).attrib, {'EmbedCab': 'yes'})
        self.assertIsNotNone(xml.find('.//w:Directory[@Id="ProgramFiles64Folder"]', ns))
        for unsupported in ('UIRef', 'WixVariable', 'Icon', 'Shortcut', 'RegistryValue'):
            self.assertFalse(xml.findall('.//w:' + unsupported, ns), unsupported)
        names = [item.attrib['Name'] for item in xml.findall('.//w:File', ns)]
        self.assertEqual(sorted(names), ['CinterForge Terminal.exe', 'Terminal.cmd'])
        self.assertNotIn('devtools', ET.tostring(xml).decode('utf-8').lower())
        a = ET.fromstring(msi._document({'Terminal.cmd': readme}, release='2026.11.23'))
        b = ET.fromstring(msi._document({'Terminal.cmd': readme}, release='2026.11.24'))
        self.assertEqual(a.find('w:Product', ns).attrib['UpgradeCode'],
                         b.find('w:Product', ns).attrib['UpgradeCode'])
        self.assertEqual(a.find('.//w:Component', ns).attrib['Guid'],
                         b.find('.//w:Component', ns).attrib['Guid'])

    def test_wix_version_rejects_unmappable_and_oversize_versions(self):
        self.assertEqual(msi._msi_version('2026.11.23'), '26.11.23')
        for value in ('../bad', '2026.13.1', '2026.11.99999', '3026.11.1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                msi._msi_version(value)

    def test_smoke_msi_contains_readme_and_no_developer_payload(self):
        root = ET.fromstring(msi._document({'README.txt': self.builds / 'README.txt'},
                                         release='2026.11.0', smoke=True))
        ns = {'w': msi.NS}
        self.assertEqual(root.find('w:Product', ns).attrib['Name'], 'CinterForge MSI Preflight')
        self.assertEqual([x.attrib['Name'] for x in root.findall('.//w:File', ns)], ['README.txt'])
        self.assertIsNone(root.find('w:Product/w:UIRef', ns))

    def test_publish_refuses_missing_setup_msi_preflight(self):
        with self.assertRaisesRegex(RuntimeError, 'Setup MSI tools missing'):
            msi._check_tools(self.builds)

    def test_windows_cross_image_provisions_msys2_patched_wine_and_gui(self):
        dockerfile = (REPO / 'host/docker/windows-cross.Dockerfile').read_text('utf-8')
        self.assertIn('ghcr.io/msys2/msys2-docker-build-base@sha256:', dockerfile)
        self.assertIn('COPY --from=patched-wine /wine /', dockerfile)
        self.assertIn("wine-11.15", dockerfile)
        self.assertIn('debian:trixie-slim', dockerfile)
        self.assertIn('xvfb xauth openbox x11vnc websockify novnc feh', dockerfile)
        self.assertIn('procps', dockerfile)
        self.assertIn('cabextract msitools wixl', dockerfile)
        self.assertNotIn('SigLevel = Never', dockerfile)
        self.assertNotIn('winehq-stable=', dockerfile)

    def test_msi_setup_verifies_native_wixl_and_never_invokes_pe32(self):
        cross = mock.Mock()
        cross.execute.side_effect = [
            mock.Mock(returncode=0, stdout=msi.WIXL_PACKAGE_VERSION),
            mock.Mock(returncode=0, stdout='wixl 0.106'),
        ]
        with redirect_stdout(StringIO()):
            self.assertEqual(msi._verify_native_msi_tools(cross, cwd=self.builds), 'wixl 0.106')
        self.assertEqual(cross.execute.call_args_list[0].args[0],
                         ['dpkg-query', '-W', '-f=${Version}', 'wixl'])
        self.assertEqual(cross.execute.call_args_list[1].args[0], ['wixl', '--version'])
        self.assertNotIn('wine', str(cross.execute.call_args_list).lower())
        cross.execute.reset_mock(side_effect=True)
        cross.execute.return_value = mock.Mock(returncode=0, stdout='0.105')
        with self.assertRaisesRegex(RuntimeError, 'Missing/incompatible native wixl'):
            msi._verify_native_msi_tools(cross, cwd=self.builds)

    def test_msi_compile_uses_native_paths_and_no_wine(self):
        work = self.builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/Smoke'
        work.mkdir(parents=True)
        readme = work / 'README.txt'
        readme.write_text('test')
        cross = mock.Mock()
        cross.path.side_effect = lambda path: '/builds/' + Path(path).name
        output = work / 'InstallerSmoke.msi'
        def fake_run(*args, **kwargs):
            self.assertEqual(args[2][0:3], ['wixl', '--arch', 'x64'])
            self.assertNotIn('wine', args[2])
            output.write_bytes(b'fixture' * 250)
        with mock.patch.object(msi, '_run', side_effect=fake_run):
            self.assertEqual(msi._compile(self.builds, cross, work=work,
                             sources={'README.txt': readme}, release='2026.11.0', smoke=True), output)
        wxs = (work / 'Installer.wxs').read_text('utf-8')
        self.assertIn('/builds/README.txt', wxs)
        self.assertNotIn('Z:', wxs)

    def test_full_installer_uses_same_minimal_wixl_schema_as_smoke(self):
        work = self.builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/TemplateSmoke'
        work.mkdir(parents=True)
        readme = work / 'CinterForge Terminal.exe'
        readme.write_bytes(b'stub')
        cross = mock.Mock()
        cross.path.side_effect = lambda path: '/builds/' + Path(path).name
        output = work / 'CinterForgeTerminal-win.msi'
        commands = []
        def fake_run(*args, **kwargs):
            commands.append(args[2])
            output.write_bytes(b'fixture' * 250)
        with mock.patch.object(msi, '_run', side_effect=fake_run):
            msi._compile(self.builds, cross, work=work,
                         sources={'CinterForge Terminal.exe': readme},
                         release='2026.11.0', smoke=False)
        self.assertEqual(commands[0][:3], ['wixl', '--arch', 'x64'])
        self.assertNotIn('--ext', commands[0])
        self.assertNotIn('wine', commands[0])
        markup = (work / 'Installer.wxs').read_text('utf-8')
        for unsupported in ('WixVariable', 'WixUI_', 'Platform=', 'CompressionLevel=', '<Shortcut', '<Icon'):
            self.assertNotIn(unsupported, markup)
        self.assertIn('ProgramFiles64Folder', markup)

    def test_setup_compiles_and_extracts_real_msi_before_pass(self):
        work = self.builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/Smoke'
        work.mkdir(parents=True)
        smoke = work / 'InstallerSmoke.msi'
        smoke.write_bytes(b'fixture-msi')
        template_work = self.builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/TemplateSmoke'
        template_work.mkdir(parents=True)
        template = template_work / 'CinterForgeTerminal-win.msi'
        template.write_bytes(b'fixture-template-msi')
        digest = hashlib.sha256(smoke.read_bytes()).hexdigest()
        cross = mock.Mock(image='cinterforge-terminal-windows-cross:fixture')
        with (mock.patch.object(msi, '_cross', return_value=cross),
              mock.patch.object(msi, '_verify_native_msi_tools', return_value='wixl 0.106') as checker,
              mock.patch.object(msi, '_compile', side_effect=[smoke, template]) as compiler,
              mock.patch.object(msi, '_verify_msi', side_effect=[
                  {'sha256': digest, 'bytes': smoke.stat().st_size},
                  {'sha256': hashlib.sha256(template.read_bytes()).hexdigest(), 'bytes': template.stat().st_size}]) as inspector,
              redirect_stdout(StringIO())):
            receipt = msi.setup(self.builds)
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['compiler'], 'debian-wixl')
        self.assertEqual(receipt['execution'], 'native-linux-wixl-x64')
        self.assertEqual(compiler.call_count, 2)
        self.assertTrue(compiler.call_args_list[0].kwargs['smoke'])
        self.assertFalse(compiler.call_args_list[1].kwargs['smoke'])
        self.assertEqual(inspector.call_args_list[0].kwargs['smoke_file'], 'README.txt')
        self.assertIn('CinterForge MSI preflight', (work / 'README.txt').read_text())
        self.assertTrue(checker.called)
        self.assertEqual(json.loads((self.builds / 'Logs/CinterForgeTerminal/windows-msi-preflight.json').read_text())['status'], 'passed')

    def test_publish_requires_matching_setup_image_and_smoke_digest(self):
        work = self.builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/Smoke'
        work.mkdir(parents=True)
        smoke = work / 'InstallerSmoke.msi'
        smoke.write_bytes(b'test-msi')
        template_work = self.builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/TemplateSmoke'
        template_work.mkdir(parents=True)
        template = template_work / 'CinterForgeTerminal-win.msi'
        template.write_bytes(b'template-msi')
        stage = self.builds / 'CinterForgeTerminal/Toolchains/WindowsMSI'
        stage.mkdir(parents=True)
        proof = {'schema': msi.SCHEMA, 'status': 'passed',
                 'wixl_package_version': msi.WIXL_PACKAGE_VERSION,
                 'wixl_version': 'wixl 0.106', 'image': 'image:expected',
                 'smoke_sha256': hashlib.sha256(smoke.read_bytes()).hexdigest(),
                 'template_sha256': hashlib.sha256(template.read_bytes()).hexdigest()}
        (stage / 'msi-preflight.json').write_text(json.dumps(proof))
        with mock.patch('host.windows_environment.WindowsCrossEnvironment.validate_staged',
                        return_value=(stage, {'image': 'image:expected'})):
            msi._check_tools(self.builds)
        with mock.patch('host.windows_environment.WindowsCrossEnvironment.validate_staged',
                        return_value=(stage, {'image': 'image:stale'})):
            with self.assertRaisesRegex(RuntimeError, 'image changed'):
                msi._check_tools(self.builds)
        smoke.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'receipt invalid'):
            msi._check_tools(self.builds)

    def test_setup_records_failed_acceptance_and_does_not_issue_pass(self):
        with mock.patch.object(msi, '_cross', side_effect=RuntimeError('toolchain unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'toolchain unavailable'):
                msi.setup(self.builds)
        status = json.loads((self.builds / 'CinterForgeTerminal/Toolchains/WindowsMSI/msi-preflight.json').read_text())
        self.assertEqual(status['status'], 'failed')


class TargetLifecycleContracts(unittest.TestCase):
    def test_all_platforms_route_through_same_selected_lifecycle(self):
        cli = cli_driver()
        for target in ('mac', 'win', 'linux', 'all'):
            for channel in (None, 'internal', 'beta', 'release'):
                argv = ['all', target] + (['publish', channel] if channel else [])
                # Never depend on a real Builder key to exercise dispatch.
                # The real preflight remains covered by its negative test.
                with self.subTest(target=target, channel=channel), \
                     mock.patch.object(cli, 'invoke') as invoker, \
                     mock.patch('host.xcode_signing.release_signing_environment', return_value=TEST_RELEASE_SIGNING), \
              mock.patch('host.xcode_signing.ensure_public_notary_profile', return_value='fixture-notary-profile'), \
                     mock.patch.dict(os.environ, {'CINTER_MAC_SIGN_IDENTITY': 'Developer ID Application: fixture'}), \
                     redirect_stdout(StringIO()):
                    self.assertEqual(cli.main(argv), 0)
                actions = [call.args[3:] for call in invoker.call_args_list]
                self.assertEqual(actions[:4], [('setup', target), ('build', target),
                                              ('stage', target), ('test', target)])
                if channel:
                    self.assertEqual(actions[4], ('publish', 'cfterminal', target, channel))
                else:
                    self.assertEqual(len(actions), 4)

    def test_windows_only_setup_never_invokes_full_setup_or_bedrock(self):
        cli = cli_driver()
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(cli, 'BUILDS', Path(temporary) / 'Builds'), \
             mock.patch.object(cli, '_preflight_source_layout'), \
             mock.patch.object(cli.platform, 'system', return_value='Linux'), \
             mock.patch.object(cli, 'invoke') as invoke, redirect_stdout(StringIO()):
            self.assertEqual(cli.main(['setup', 'win']), 0)
        commands = [' '.join(map(str, call.args)) for call in invoke.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn('host.windows_dependencies', commands[0])
        self.assertIn('host.msi_installer', commands[1])
        self.assertTrue(all('linuxos' not in x and 'lagomorph' not in x for x in commands))

    def test_full_publish_packages_msi_before_other_upload_and_preserves_mac_gate(self):
        cli = cli_driver()
        operations = []
        from host import universal_package as full
        with (mock.patch.object(cli, '_prepare_windows_release_msi',
                                side_effect=lambda builds: operations.append('msi') or {
                                    'filename': 'CinterForgeTerminal-win.msi'}) as prep,
              mock.patch.object(full, 'publish_four',
                                side_effect=lambda builds, **kwargs: operations.append('four') or {
                                    'bucket': 'cdn-assets-dev', 'prefix': 'Distribution'}) as four,
              mock.patch('host.target_publish.publish_msi_addendum',
                         side_effect=lambda *args, **kw: operations.append('msi-upload')),
              redirect_stdout(StringIO())):
            self.assertEqual(cli.main(['publish', 'cfterminal', 'all', 'internal']), 0)
        self.assertEqual(operations, ['msi', 'four', 'msi-upload'])
        self.assertEqual(four.call_args.kwargs['channel'], 'dev')

    def test_public_release_notary_preflight_happens_before_msi_packaging(self):
        cli = cli_driver()
        from host import universal_package as full
        with (mock.patch.object(cli.platform, 'system', return_value='Darwin'),
              mock.patch('host.xcode_signing.release_signing_environment', return_value=TEST_RELEASE_SIGNING), \
              mock.patch('host.xcode_signing.ensure_public_notary_profile',
                         side_effect=RuntimeError('notary profile missing')) as notary,
              mock.patch.dict(os.environ, {'CINTER_MAC_SIGN_IDENTITY': 'fixture',
                                           'CINTER_MAC_NOTARY_PROFILE': ''}),
              mock.patch.object(cli, '_prepare_windows_release_msi') as package,
              mock.patch.object(full, 'publish_four') as upload):
            with self.assertRaisesRegex(RuntimeError, 'notary profile missing'):
                cli.main(['publish', 'cfterminal', 'all', 'beta'])
        notary.assert_called_once_with(cli.BUILDS)
        package.assert_not_called()
        upload.assert_not_called()

    def test_public_release_requires_private_key_before_msi_packaging(self):
        cli = cli_driver()
        from host import universal_package as full
        with (mock.patch.dict(os.environ, {'CINTER_MAC_SIGN_IDENTITY': ''}),
              mock.patch('host.xcode_signing.release_signing_environment', return_value={}),
              mock.patch.object(cli, '_prepare_windows_release_msi') as msi_package,
              mock.patch.object(full, 'publish_four') as four):
            with self.assertRaisesRegex(RuntimeError, 'private key'):
                cli.main(['publish', 'cfterminal', 'all', 'release'])
            msi_package.assert_not_called()
            four.assert_not_called()

    def test_all_publish_stops_before_other_upload_if_msi_compiler_fails(self):
        cli = cli_driver()
        from host import universal_package as full
        with (mock.patch.object(cli, '_prepare_windows_release_msi', side_effect=RuntimeError('WiX linker failed')),
              mock.patch.object(full, 'publish_four') as four):
            with self.assertRaisesRegex(RuntimeError, 'WiX linker failed'):
                cli.main(['publish', 'cfterminal', 'all', 'internal'])
            four.assert_not_called()


class ScopedPublishContracts(unittest.TestCase):
    def test_dryrun_checks_connections_but_never_sends_msis_or_zips(self):
        from host import universal_package as full
        from host.publishing import cloudflare_r2_sync as r2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'Builds'
            root.mkdir()
            payload = root / 'CinterForgeTerminal-win.msi'
            payload.write_bytes(b'MSI-file')
            with (mock.patch.object(full, '_cloudflare_config', return_value={'bucket': 'mock'}),
                  mock.patch.object(r2, '_list_remote', return_value={}) as inspect,
                  mock.patch.object(r2, '_put_file', side_effect=AssertionError('unexpected upload')),
                  mock.patch.object(r2, '_get_file', side_effect=AssertionError('unexpected download'))):
                result = target_publish._publish_files(
                    root, channel='dryrun', target='win', files={payload.name: payload}, document={})
            self.assertEqual(inspect.call_count, 2)
            self.assertFalse(result['uploaded'])

    def test_windows_only_upload_sends_no_devtools_or_other_platforms(self):
        from host import universal_package as full
        from host.publishing import cloudflare_r2_sync as r2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'Builds'
            root.mkdir()
            win_zip = root / 'CinterForgeTerminal-win.zip'
            win_msi = root / 'CinterForgeTerminal-win.msi'
            win_zip.write_bytes(b'windows-app-zip')
            win_msi.write_bytes(b'windows-installer')
            files = {p.name: p for p in (win_zip, win_msi)}
            entries = {name: {'bytes': path.stat().st_size,
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                       for name, path in files.items()}
            calls = []
            def put(cfg, key, path, **kwargs):
                calls.append(key)
            def get(cfg, key, target, **kwargs):
                target.write_bytes(files[key.rsplit('/', 1)[-1]].read_bytes())
            def request(cfg, method, *, key):
                return (200, {}, (root / 'CinterForgeTerminal/Distribution/win-artifacts.json').read_bytes())
            with (mock.patch.object(full, '_cloudflare_config', return_value={'bucket': 'cdn-assets-dev'}),
                  mock.patch.object(r2, '_list_remote', return_value={}),
                  mock.patch.object(r2, '_put_file', side_effect=put),
                  mock.patch.object(r2, '_get_file', side_effect=get),
                  mock.patch.object(r2, '_request', side_effect=request),
                  redirect_stdout(StringIO())):
                result = target_publish._publish_files(root, channel='internal', target='win', files=files,
                                   document={'release': HOST_RELEASE, 'artifacts': entries})
            self.assertEqual([x.rsplit('/', 1)[-1] for x in calls],
                             ['CinterForgeTerminal-win.zip', 'CinterForgeTerminal-win.msi', 'win-artifacts.json'])
            self.assertNotIn('CinterForgeTerminal-devtools.zip', str(calls))
            self.assertEqual(result['bucket'], 'cdn-assets-dev')


if __name__ == '__main__':
    unittest.main()
