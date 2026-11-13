"""Focused release regression contracts. Native target acceptance is separate."""
from tests._support import *

class PublishContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cph-publish-tests-')
        self.addCleanup(self.tmp.cleanup)
        self.builds = Path(self.tmp.name) / 'Builds'
        self.version = 'test.1'
        for platform in release.TARGETS:
            stage = self.builds / 'Python/Host' / platform / 'x64' / self.version
            (stage / 'bin').mkdir(parents=True)
            payload = f'{platform}-executable'.encode()
            (stage / 'bin/host').write_bytes(payload)
            contents = {'schema': 'cinterforge.python-host.stage.v1',
                        'platform': platform, 'arch': 'x64',
                        'files': {'bin/host': sha(payload)}}
            (stage / 'manifest.json').write_text(json.dumps(contents))
            app = self.builds / 'CinterForgeTerminal/Staged' / platform / 'x64' / self.version
            (app / 'bin').mkdir(parents=True)
            (app / 'bin/host').write_bytes(payload)
            app_data = dict(contents, schema=release.APP_STAGE_SCHEMA,
                            entrypoint='bin/host',
                            distribution={'public_approved': True, 'license': 'MIT'})
            (app / 'manifest.json').write_text(json.dumps(app_data))

    def _receipt(self, platform, execution='native', *, stage_sha=None, product='terminal'):
        base = 'CinterForgeTerminal' if product == 'terminal' else 'Python/Host'
        stage = self.builds / base / ('Staged' if product == 'terminal' else '') / platform / 'x64' / self.version
        test = self.builds / base / 'Tests' / platform / 'x64' / self.version / 'receipt.json'
        test.parent.mkdir(parents=True, exist_ok=True)
        test.write_text(json.dumps({'schema': release.APP_TEST_SCHEMA if product == 'terminal' else release.TEST_SCHEMA, 'result': 'passed',
             'platform': platform, 'arch': 'x64', 'release': self.version,
             'execution': execution,
             'stage_manifest_sha256': stage_sha or sha((stage / 'manifest.json').read_bytes())}))

    def test_dev_archive_is_deterministic_and_includes_all_three_platforms(self):
        outbox, result = release.prepare(self.builds, self.version, 'dev', product='host-sdk')
        self.assertEqual({x['platform'] for x in result['artifacts']}, set(release.TARGETS))
        self.assertEqual(result['bucket'], 'cdn-assets-dev')
        self.assertFalse(result['native_certified'])
        for item in result['artifacts']:
            with zipfile.ZipFile(outbox / item['file']) as z:
                self.assertEqual(set(z.namelist()), {'CinterForgeTerminal/bin/host', 'CinterForgeTerminal/manifest.json'})
                self.assertEqual(sha((outbox / item['file']).read_bytes()), item['sha256'])
        self.assertEqual(release.prepare(self.builds, self.version, 'dev', product='host-sdk')[0], outbox)

    def test_missing_target_rejects_publish_all(self):
        missing = self.builds / 'Python/Host/win/x64' / self.version / 'manifest.json'
        missing.unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'No verified staged win'):
            release.prepare(self.builds, self.version, 'dev', product='host-sdk')

    def test_tampered_stage_or_unlisted_file_fails_closed(self):
        stage = self.builds / 'Python/Host/mac/x64' / self.version
        (stage / 'bin/host').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            release.prepare(self.builds, self.version, 'dev', product='host-sdk')
        (stage / 'bin/host').write_bytes(b'mac-executable')
        (stage / 'secrets.txt').write_bytes(b'should not ship')
        with self.assertRaisesRegex(ValueError, 'file set differs'):
            release.prepare(self.builds, self.version, 'dev', product='host-sdk')

    def test_public_requires_approval_rights_and_native_receipts(self):
        with self.assertRaisesRegex(RuntimeError, 'development-only'):
            release.prepare(self.builds, self.version, 'production', product='host-sdk', approve_production=True)
        with self.assertRaisesRegex(RuntimeError, 'approve-production'):
            release.prepare(self.builds, self.version, 'production')
        with self.assertRaisesRegex(RuntimeError, 'native test receipt'):
            release.prepare(self.builds, self.version, 'production', approve_production=True)
        for platform in release.TARGETS:
            self._receipt(platform)
        self._receipt('win', execution='wine')
        with self.assertRaisesRegex(RuntimeError, 'win/x64: emulated'):
            release.prepare(self.builds, self.version, 'production', approve_production=True)
        self._receipt('win', execution='native', stage_sha='1' * 64)
        with self.assertRaisesRegex(RuntimeError, 'win/x64: invalid'):
            release.prepare(self.builds, self.version, 'production', approve_production=True)
        self._receipt('win')
        app_manifest = self.builds / 'CinterForgeTerminal/Staged/linux/x64' / self.version / 'manifest.json'
        data = json.loads(app_manifest.read_text())
        data['distribution']['public_approved'] = False
        app_manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, 'rights are not approved'):
            release.prepare(self.builds, self.version, 'production', approve_production=True)
        data['distribution']['public_approved'] = True
        app_manifest.write_text(json.dumps(data))
        self._receipt('linux')
        outbox, result = release.prepare(self.builds, self.version, 'production', approve_production=True)
        self.assertTrue(outbox.exists())
        self.assertTrue(result['native_certified'])
        self.assertEqual(result['bucket'], 'cinterforge')
        self.assertEqual(result['product'], 'terminal')

    def test_application_is_default_and_requires_real_stage(self):
        outbox, result = release.prepare(self.builds, self.version, 'dev')
        self.assertEqual(result['product'], 'terminal')
        self.assertTrue(result['object_prefix'].startswith('CinterForgeTerminal/Applications/'))
        self.assertTrue(all(x['file'].startswith('CinterForgeTerminal-terminal-') for x in result['artifacts']))
        stage = self.builds / 'CinterForgeTerminal/Staged/mac/x64' / self.version / 'manifest.json'
        data = json.loads(stage.read_text())
        data.pop('entrypoint')
        stage.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'executable entrypoint'):
            release.prepare(self.builds, self.version, 'dev')

    def test_dry_run_never_resolves_credentials_or_makes_network_request(self):
        with mock.patch.object(release.r2, '_load_config', side_effect=AssertionError('read credentials')), \
             mock.patch.object(release.r2, 'upload_sync', side_effect=AssertionError('upload')):
            result = release.publish(self.builds, self.version, 'dev', product='host-sdk',
                                     config=Path('does-not-exist'), dry_run=True)
        self.assertEqual(result['channel'], 'dev')

    def test_channel_routing_and_pointer_only_after_verified_upload(self):
        writes = []
        data = {}
        def request(cfg, method, key='', body=b'', **kwargs):
            if method == 'PUT':
                data[key] = body
                writes.append(key)
                return 200, {}, b''
            if method == 'GET':
                return 200, {}, data[key]
            raise AssertionError(method)
        def uploader(cfg, folder, prefix, ext, **kwargs):
            self.assertTrue(kwargs['no_delete'])
            for file in folder.iterdir():
                data[f'{prefix}/{file.name}'] = file.read_bytes()
        def listed(cfg, prefix):
            return {key[len(prefix)+1:]: {'size': len(body)} for key, body in data.items()
                    if key.startswith(prefix + '/')}
        with mock.patch.object(release.r2, '_load_config', return_value={'bucket': 'cdn-assets-dev'}) as load, \
             mock.patch.object(release.r2, 'upload_sync', side_effect=uploader) as upload, \
             mock.patch.object(release.r2, '_list_remote', side_effect=listed), \
             mock.patch.object(release.r2, '_get_file', side_effect=lambda cfg, key, path, expected_size=None: path.write_bytes(data[key])), \
             mock.patch.object(release.r2, '_request', side_effect=request):
            result = release.publish(self.builds, self.version, 'dev', product='host-sdk', config=self.builds / 'no-creds')
        self.assertEqual(load.call_args.kwargs['required_bucket'], 'cdn-assets-dev')
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(writes, ['CinterForgeTerminal/Host/channels/dev.json'])
        self.assertTrue(result['object_prefix'] + '/manifest.json' in data)

    def test_immutable_remote_cannot_be_overwritten(self):
        outbox, result = release.prepare(self.builds, self.version, 'dev', product='host-sdk')
        prefix = result['object_prefix']
        tampered = next(x for x in outbox.iterdir() if x.suffix == '.zip')
        with mock.patch.object(release.r2, '_list_remote', return_value={
            tampered.name: {'size': tampered.stat().st_size}}), \
            mock.patch.object(release.r2, '_get_file', side_effect=lambda cfg, key, path, expected_size=None: path.write_bytes(b'x' * tampered.stat().st_size)):
            with self.assertRaisesRegex(RuntimeError, 'differs in content'):
                release._verify_existing_remote({'bucket': 'cdn-assets-dev'}, prefix, outbox)

class VerifiedStageRetirement(unittest.TestCase):
    def setUp(self):
        # Keep even test fixtures under the one shared sibling Builds directory.
        source = Path(manager.__file__).resolve().parents[1]
        scratch = Path(os.environ.get('CINTER_BUILDS_ROOT', str(source.parent / 'Builds'))) / 'Intermediates/CinterForgeTerminalTests'
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='host-retire-', dir=scratch)
        self.addCleanup(temp.cleanup)
        self.builds = Path(temp.name) / 'Builds'
        self.builds.mkdir()
        self.patch_system = mock.patch.object(manager, 'current_platform', return_value='mac')
        self.patch_arch = mock.patch.object(manager, 'architecture', return_value='arm64')
        self.patch_system.start()
        self.patch_arch.start()
        self.addCleanup(self.patch_arch.stop)
        self.addCleanup(self.patch_system.stop)
        self.root = self.builds / 'Python/Host/mac/arm64'
        self._fake_cmake_build()

    def _fake_cmake_build(self):
        build = self.builds / 'Intermediates/PythonHost/mac/arm64/cmake'
        build.mkdir(parents=True)
        (build / 'CMakeCache.txt').write_text('CPH_ENABLE_ZEROCONF:BOOL=OFF\n')
        for name in ('libCinterForgePythonHost.a', 'libCinterForgePythonHost.dylib',
                     'cinter-python-host', 'cinter-python-host-smoke',
                     'cinter-host-utilities-smoke'):
            (build / name).write_bytes(('fake compiled ' + name).encode())
        self.build = build

    def _old_release(self, version='2026.11', *, system='mac', arch='arm64'):
        root = self.builds / 'Python/Host' / system / arch / version
        payload = root / 'lib/old.bin'
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b'old immutable host artifact')
        manifest = {'schema': 'cinterforge.python-host.stage.v1',
                    'platform': system, 'arch': arch,
                    'files': {'lib/old.bin': hashlib.sha256(payload.read_bytes()).hexdigest()}}
        (root / 'manifest.json').write_text(json.dumps(manifest))
        return root

    def test_new_version_is_verified_before_old_stage_is_deleted(self):
        old = self._old_release()
        other_platform = self._old_release(system='linux', arch='x64')
        unknown = self.root / 'random-user-folder'
        unknown.mkdir(parents=True)
        (unknown / 'notes.txt').write_text('keep unrelated files')
        receipt = manager.stage_host(self.builds)
        new = self.root / HOST_RELEASE
        self.assertEqual(receipt['release'], HOST_RELEASE)
        self.assertTrue(new.is_dir())
        self.assertEqual(manager._verify_host_stage(
            new, system='mac', arch='arm64', release=HOST_RELEASE), receipt)
        self.assertFalse(old.exists())
        self.assertTrue(other_platform.is_dir())
        self.assertEqual((unknown / 'notes.txt').read_text(), 'keep unrelated files')

    def test_untracked_bytecode_and_user_files_never_block_stage(self):
        receipt = manager.stage_host(self.builds)
        product = self.root / HOST_RELEASE
        cache = product / 'webapp/__pycache__'
        cache.mkdir()
        (cache / 'bedrock.cpython-311.pyc').write_bytes(b'bytecode outside manifest')
        (product / 'user-notes.txt').write_text('leave this in place')
        self.assertEqual(manager.stage_host(self.builds), receipt)
        self.assertTrue((cache / 'bedrock.cpython-311.pyc').is_file())
        self.assertEqual((product / 'user-notes.txt').read_text(), 'leave this in place')
        self.assertEqual(manager._verify_host_stage(
            product, system='mac', arch='arm64', release=HOST_RELEASE), receipt)

    def test_existing_manifest_directory_does_not_block_host_stage(self):
        manager.stage_host(self.builds)
        product = self.root / HOST_RELEASE
        (product / 'manifest.json').unlink()
        (product / 'manifest.json').mkdir()
        (product / 'manifest.json/notes.txt').write_text('user data')
        rebuilt = manager.stage_host(self.builds)
        self.assertEqual(rebuilt['release'], HOST_RELEASE)
        self.assertTrue((product / 'manifest.json').is_file())
        quarantined = list((self.builds / 'Intermediates/PythonHost/StageConflicts').rglob('notes.txt'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(), 'user data')


    def test_reuse_of_verified_new_stage_also_retires_old_stage(self):
        manager.stage_host(self.builds)
        old = self._old_release()
        manager.stage_host(self.builds)
        self.assertFalse(old.exists())
        self.assertTrue((self.root / HOST_RELEASE / 'manifest.json').is_file())

    def test_manifest_verification_failure_does_not_delete_previous_stage(self):
        old = self._old_release()
        with mock.patch.object(manager, '_verify_host_stage', side_effect=RuntimeError('bad digest')):
            with self.assertRaisesRegex(RuntimeError, 'bad digest'):
                manager.stage_host(self.builds)
        self.assertTrue(old.is_dir())
        # A newly promoted candidate cannot be called accepted if verification fails.
        self.assertTrue((self.root / HOST_RELEASE).is_dir())

    def test_same_version_stage_rebuild_reconciles_without_bump(self):
        old = self._old_release()
        new = self._old_release(version=HOST_RELEASE)
        (new / 'user-notes.txt').write_text('keep notes')
        receipt = manager.stage_host(self.builds)
        self.assertFalse(old.exists())
        self.assertEqual(receipt['release'], HOST_RELEASE)
        self.assertTrue((new / 'bin/cinter-python-host').is_file())
        self.assertEqual((new / 'user-notes.txt').read_text(), 'keep notes')
        self.assertEqual((new / 'lib/old.bin').read_bytes(), b'old immutable host artifact')

    def test_changed_staged_lifecycle_source_is_restaged_in_place(self):
        manager.stage_host(self.builds)
        product = self.root / HOST_RELEASE
        relative = 'lifecycle/host/portable_python.py'
        old_bytes = b'previous staged packaging helper\n'
        (product / relative).write_bytes(old_bytes)
        manifest_path = product / 'manifest.json'
        receipt = json.loads(manifest_path.read_text(encoding='utf-8'))
        receipt['files'][relative] = hashlib.sha256(old_bytes).hexdigest()
        manifest_path.write_text(json.dumps(receipt, sort_keys=True) + '\n', encoding='utf-8')
        rebuilt = manager.stage_host(self.builds)
        expected = (Path(manager.__file__).resolve().parents[1] / 'host/portable_python.py').read_bytes()
        self.assertEqual((product / relative).read_bytes(), expected)
        self.assertEqual(rebuilt['files'][relative], hashlib.sha256(expected).hexdigest())
        self.assertEqual(manager._verify_host_stage(
            product, system='mac', arch='arm64', release=HOST_RELEASE), rebuilt)

    def test_symlinked_old_stage_is_refused_not_deleted(self):
        other = self.builds / 'OtherProject'
        other.mkdir()
        old = self.root / '2026.11'
        old.parent.mkdir(parents=True, exist_ok=True)
        old.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlinked older Host release'):
            manager.stage_host(self.builds)
        self.assertTrue(old.is_symlink())
        self.assertTrue(other.is_dir())

class UniversalPackageTests(unittest.TestCase):
    def setUp(self):
        base = UNIVERSAL_PACKAGE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        base.mkdir(parents=True, exist_ok=True)
        self.parent = Path(tempfile.mkdtemp(dir=base))
        self.builds = self.parent / 'Builds'
        self.builds.mkdir()
        self.release = 'test-release'
        self.arches = {'mac':'arm64', 'linux':'x64', 'win':'x64'}
        self.fixed_browser = self.builds / 'Toolchains/WebView2/FixedFixture/msedgewebview2.exe'
        self.fixed_browser.parent.mkdir(parents=True)
        self.fixed_browser.write_bytes(b'MZtest-browser')
        # Packaging verifies the browser's contents against the provisioner's
        # authenticated stage, not merely its executable filename.
        verifier = patch('host.windows_fixed_runtime.verify',
                         return_value=self.fixed_browser.parent)
        verifier.start()
        self.addCleanup(verifier.stop)

    def tearDown(self):
        shutil.rmtree(self.parent)

    def make_target(self, target):
        root = self.builds / 'CinterForgeTerminal/Bundles' / target / self.arches[target] / self.release
        exe = root / 'Host/bin' / ('CinterForgeTerminal.exe' if target == 'win' else 'CinterForgeTerminal')
        exe.parent.mkdir(parents=True)
        exe.write_bytes({'mac': b'\xcf\xfa\xed\xfeM', 'linux': b'\x7fELFL', 'win':b'MZWW'}[target])
        (root/'manifest.json').write_text('{}')
        if target == 'win':
            browser = root / 'WebView2FixedRuntime/msedgewebview2.exe'
            browser.parent.mkdir(parents=True)
            browser.write_bytes(self.fixed_browser.read_bytes())
        return root

    def accepted(self, p):
        target=p.parent.parent.name
        return {'platform': target, 'arch':self.arches[target], 'release':self.release,
                'relocated_runtime_smoke':{'execution':'native-relocated'}}

    def test_refuses_missing_platform_without_artifact(self):
        self.make_target('mac')
        self.make_target('linux')
        with patch.object(universal,'verify_bundle',side_effect=self.accepted):
            with self.assertRaisesRegex(RuntimeError, 'unaccepted win'):
                universal.assemble(self.builds, release=self.release, arches=self.arches)
        self.assertFalse((self.builds/'CinterForgeTerminal/Distribution/CinterForgeTerminal.zip').exists())

    def test_refuses_wine_only_accepted_windows(self):
        for t in universal.TARGETS: self.make_target(t)
        def fake_verify(p):
            d=self.accepted(p)
            if d['platform']=='win': d['relocated_runtime_smoke']['execution']='wine-only'
            return d
        with patch.object(universal,'verify_bundle',side_effect=fake_verify):
            with self.assertRaisesRegex(RuntimeError, 'native relocated acceptance for win'):
                universal.assemble(self.builds, release=self.release, arches=self.arches)

    def test_one_relocatable_app_with_three_launchers_and_manifest(self):
        for t in universal.TARGETS: self.make_target(t)
        with patch.object(universal,'verify_bundle',side_effect=self.accepted):
            zpath=universal.assemble(self.builds, release=self.release, arches=self.arches)
        with zipfile.ZipFile(zpath) as z:
            self.assertIsNone(z.testzip())
            names=set(z.namelist())
            for rel in ('CinterForgeTerminal.app/Contents/Info.plist', 'CinterForgeTerminal.app/Terminal.sh',
                        'CinterForgeTerminal.app/Terminal.cmd',
                        'CinterForgeTerminal.app/Contents/MacOS/CinterForgeTerminal',
                        'CinterForgeTerminal.app/Contents/Resources/Builds/mac/Host/bin/CinterForgeTerminal',
                        'CinterForgeTerminal.app/Contents/Resources/Builds/linux/Host/bin/CinterForgeTerminal',
                        'CinterForgeTerminal.app/Contents/Resources/Builds/win/Host/bin/CinterForgeTerminal.exe'):
                self.assertIn(rel,names)
            self.assertIn(b'%~dp0Contents\\Resources\\Builds\\win',z.read('CinterForgeTerminal.app/Terminal.cmd'))
            self.assertIn(b'--python', z.read('CinterForgeTerminal.app/Terminal.cmd'))
            self.assertIn(b'--worker', z.read('CinterForgeTerminal.app/Terminal.sh'))
            self.assertIn(b'--service', z.read('CinterForgeTerminal.app/Contents/MacOS/CinterForgeTerminal'))
            self.assertIn(b'$(dirname -- "$0")',z.read('CinterForgeTerminal.app/Terminal.sh'))
            self.assertEqual((z.getinfo('CinterForgeTerminal.app/Terminal.sh').external_attr>>16)&0o111,0o111)
            m=json.loads(z.read('CinterForgeTerminal.app/Contents/Resources/Builds/universal-manifest.json'))
            self.assertEqual(m['schema'],universal.SCHEMA)
            self.assertEqual(set(m['targets']),set(universal.TARGETS))

    def test_rejects_symlink_source(self):
        for t in universal.TARGETS: self.make_target(t)
        source=self.builds/'CinterForgeTerminal/Bundles/mac/arm64'/self.release
        (source/'rogue').symlink_to('/bin/sh')
        with patch.object(universal,'verify_bundle',side_effect=self.accepted):
            with self.assertRaisesRegex(RuntimeError, 'symlinks'):
                universal.assemble(self.builds, release=self.release, arches=self.arches)

class FourArtifactSteamReleaseTests(unittest.TestCase):
    """Build/Stage operate on unpacked files; Setup/Publish own archives."""
    def setUp(self):
        base = UNIVERSAL_PACKAGE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        base.mkdir(parents=True, exist_ok=True)
        self.scratch = Path(tempfile.mkdtemp(dir=base, prefix='four-unpacked-'))
        self.builds = self.scratch / 'Builds'
        self.builds.mkdir()
        self.steamapps = self.scratch / 'steamapps'
        self.steamapps.mkdir()
        self.release = '2026.11.fixture'
        self.assets = {}
        for target in ('mac', 'linux', 'win'):
            root = 'CinterForge Terminal.app' if target == 'mac' else 'CinterForge Terminal'
            binary = ('Contents/MacOS/CinterForgeTerminal' if target == 'mac'
                      else 'CinterForge Terminal.exe' if target == 'win' else 'CinterForge Terminal')
            magic = {'mac': b'\xcf\xfa\xed\xfe', 'linux': b'\x7fELF', 'win': b'MZ'}[target]
            self.assets[target] = {
                f'{root}/{binary}': magic + b'fixture-binary',
                f'{root}/Builds/Python/Host/{target}/x64/{self.release}/webapp/CinterForgeTerminal.py':
                    b'print("ready")',
                f'{root}/Builds/Run/README.txt': b'Bedrock excluded until user EULA'}
        for name in universal.WINDOWS_SHELL_REQUIRED:
            self.assets['win'][universal.WINDOWS_SHELL_PREFIX + name] = b'MZ Windows Bash fixture'
        self.assets['devtools'] = {
            'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/bin/cmake': b'cmake',
            'DevTools/Builds/CinterForgeTerminal/Toolchains/LinuxSample/oci.tar': b'Linux OCI fixture',
            'DevTools/Builds/CinterForgeTerminal/Toolchains/WindowsCross/oci.tar': b'Windows OCI fixture',
            'DevTools/README.txt': b'Optional SDK distribution documentation'}
        self.patch_apps = mock.patch.object(universal, '_app_assets',
                                             side_effect=lambda b, t, r: self.assets[t])
        self.patch_tools = mock.patch.object(universal, '_devtools_assets',
                                              side_effect=lambda b: self.assets['devtools'])
        self.patch_apps.start(); self.patch_tools.start()
        # Native hdiutil is macOS-only. Fixture replaces only disk-image I/O;
        # lifecycle dispatch, checksums, manifest names and Setup routing stay real.
        def fake_dmg_create(app, output, builds, **kwargs):
            self.assertTrue(app.is_dir())
            self.assertEqual(kwargs['volume_name'], 'CinterForge Terminal')
            output.write_bytes(b'FIXTURE-DMG:'+self.release.encode())
            return {'filename': output.name, 'sha256': sha(output.read_bytes()),
                    'bytes': output.stat().st_size, 'target': 'mac',
                    'format': 'dmg', 'app_name': app.name,
                    'notarized': bool(kwargs.get('public'))}
        def fake_dmg_verify(path, builds, **kwargs):
            self.assertTrue(path.read_bytes().startswith(b'FIXTURE-DMG:'))
            self.assertEqual(kwargs['app_name'], 'CinterForge Terminal.app')
        def fake_dmg_restore(path, builds, **kwargs):
            fake_dmg_verify(path, builds, app_name=kwargs['app_name'])
            destination = kwargs['destination']
            signed = (self.builds / 'CinterForgeTerminal/Prepared/MacSigned/MacSignedRelease'
                      / 'CinterForge Terminal.app')
            shutil.copytree(signed, destination, dirs_exist_ok=True)
        self.patch_dmg_create = mock.patch('host.mac_dmg.create_dmg', side_effect=fake_dmg_create)
        self.patch_dmg_verify = mock.patch('host.mac_dmg.verify_dmg', side_effect=fake_dmg_verify)
        self.patch_dmg_restore = mock.patch('host.mac_dmg.restore_dmg', side_effect=fake_dmg_restore)
        self.patch_dmg_create.start(); self.patch_dmg_verify.start(); self.patch_dmg_restore.start()
        self.fixture_signature = mock.patch('host.mac_app._verify_signature')
        self.fixture_signature.start()

    def tearDown(self):
        self.patch_dmg_create.stop(); self.patch_dmg_verify.stop(); self.patch_dmg_restore.stop()
        self.fixture_signature.stop()
        self.patch_apps.stop(); self.patch_tools.stop()
        shutil.rmtree(self.scratch)

    def built(self):
        result = universal.build_four(self.builds, release=self.release)
        # Fixture assets are small bytes rather than a real Xcode-signed app.
        # Materialize Build's signed-app destination for mock hdiutil tests.
        signed = (self.builds / 'CinterForgeTerminal/Prepared/MacSigned/MacSignedRelease'
                  / 'CinterForge Terminal.app')
        from host.unpacked_release import _load
        mac_files = _load(self.builds, self.release)['targets']['mac']['files']
        for name, detail in mac_files.items():
            relative = Path(name).relative_to('CinterForge Terminal.app')
            dest = signed / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(detail['source'], dest)
        return result

    def stage(self, targets=None):
        kw = {'release': self.release, 'steamapps': self.steamapps}
        if targets: kw['targets'] = targets
        return universal.stage_four(self.builds, **kw)

    def test_release_zip_preserves_runtime_python_and_devtools_executable_modes(self):
        runtime = self.scratch / 'python'
        runtime.write_bytes(b'\xcf\xfa\xed\xfe-mac-python-fixture')
        runtime.chmod(0o644)
        app_python = ('CinterForge Terminal.app/Contents/Resources/Builds/Python/'
                      'Environments/mac/arm64/cinterterminalenv/bin/python')
        output = self.scratch / 'mac-fixture.zip'
        universal._write_release_zip(output, {app_python: runtime}, target='mac',
                                     release=self.release, root='CinterForge Terminal.app')
        with zipfile.ZipFile(output) as archive:
            self.assertEqual((archive.getinfo(app_python).external_attr >> 16) & 0o777, 0o755)
        tool = self.scratch / 'cmake'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
        name = 'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/env/bin/cmake'
        output = self.scratch / 'devtools-fixture.zip'
        universal._write_release_zip(output, {name: tool}, target='devtools',
                                     release=self.release, root='DevTools')
        with zipfile.ZipFile(output) as archive:
            self.assertEqual((archive.getinfo(name).external_attr >> 16) & 0o777, 0o755)

    def test_build_four_unpacked_and_stage_all_without_zip(self):
        with mock.patch.object(universal, '_write_release_zip', side_effect=AssertionError('ZIP in Build')):
            result = self.built()
            self.assertEqual(set(result['artifacts']), set(universal.FOUR_TARGETS))
            self.assertEqual(list((self.builds / 'CinterForgeTerminal/Distribution').glob('*.zip')), [])
            self.stage()
        for target in universal.FOUR_TARGETS:
            root = universal._steam_release_path(self.steamapps, target, self.release)
            self.assertTrue((root / universal.APP_ROOTS[target]).is_dir())
            self.assertTrue((root / 'stage-receipt.json').is_file())
        self.assertFalse(list(self.steamapps.rglob('bedrock_server*')))

    def test_build_status_and_stage_status_are_separate(self):
        self.assertFalse(universal.status_four(self.builds, release=self.release,
                           steamapps=self.steamapps)['complete_build_receipt'])
        self.built()
        before = universal.status_four(self.builds, release=self.release,
                                       steamapps=self.steamapps)
        self.assertTrue(before['complete_build_receipt'])
        self.assertFalse(before['complete_stage_receipt'])
        self.stage()
        after = universal.status_four(self.builds, release=self.release,
                                      steamapps=self.steamapps)
        self.assertTrue(after['complete_stage_receipt'])

    def test_publish_alone_creates_mac_dmg_and_three_zips(self):
        self.built(); self.stage()
        directory = self.builds / 'CinterForgeTerminal/Distribution'
        self.assertEqual(list(directory.glob('*.zip')), [])
        result = universal.prepare_publish_packages(self.builds, release=self.release)
        self.assertEqual(set(result['artifacts']), set(universal.FOUR_TARGETS))
        self.assertEqual({p.name for p in directory.glob('*.zip')},
                         {universal.artifact_filename(name) for name in ('linux', 'win', 'devtools')})
        self.assertTrue((directory / 'CinterForgeTerminal-mac.dmg').is_file())
        for name in ('linux', 'win', 'devtools'):
            universal._verify_release_zip(directory / universal.artifact_filename(name),
                                          target=name, release=self.release)
        self.assertEqual(result['artifacts']['mac']['format'], 'dmg')

    def test_setup_only_extracts_published_packages_to_builds(self):
        from host.unpacked_release import setup_unpack_published
        self.built()
        universal.prepare_publish_packages(self.builds, release=self.release)
        with mock.patch('sys.platform', 'darwin'):
            restored = setup_unpack_published(self.builds, release=self.release)
        self.assertEqual(set(restored['restored_files']), set(universal.FOUR_TARGETS))
        self.assertTrue((self.builds / 'CinterForgeTerminal/Applications/mac/arm64/Release'
                         / 'CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal').is_file())
        self.assertEqual((self.builds / 'CinterForgeTerminal/Prepared/DevTools/README.txt').read_bytes(),
                         b'Optional SDK distribution documentation')
        self.assertTrue((self.builds / 'CinterForgeTerminal/Toolchains/mac/bin/cmake').is_file())

    def test_second_build_reuses_unchanged_unpacked_file_receipt(self):
        first = self.built()
        with mock.patch.object(universal, '_write_release_zip', side_effect=AssertionError('Build zipped')):
            second = self.built()
        self.assertEqual(first, second)

    def test_second_stage_skips_unchanged_devtools_copies(self):
        self.built(); self.stage()
        from host import unpacked_release
        with mock.patch.object(unpacked_release, '_copy_verified',
                               side_effect=AssertionError('Unchanged 7GB DevTools copied')):
            self.stage(targets=('devtools',))

    def test_stage_rejects_modified_build_input_before_install(self):
        self.built()
        from host import unpacked_release
        receipt = unpacked_release._load(self.builds, self.release)
        item = next(iter(receipt['targets']['linux']['files'].values()))
        Path(item['source']).write_bytes(b'corrupted')
        with self.assertRaisesRegex(RuntimeError, 'Build input changed'):
            self.stage(targets=('linux',))
        root = universal._steam_release_path(self.steamapps, 'linux', self.release)
        self.assertFalse((root / 'CinterForge Terminal/CinterForge Terminal').exists())

    def test_stage_does_not_delete_unowned_installed_files(self):
        self.built(); self.stage()
        root = universal._steam_release_path(self.steamapps, 'linux', self.release)
        extra = root / 'user-note.txt'
        extra.write_text('preserve me')
        self.assets['linux']['CinterForge Terminal/CinterForge Terminal'] = b'\x7fELF-updated'
        self.built(); self.stage(targets=('linux',))
        self.assertEqual(extra.read_text(), 'preserve me')
        self.assertEqual((root / 'CinterForge Terminal/CinterForge Terminal').read_bytes(),
                         b'\x7fELF-updated')

    def test_devtools_input_change_invalidates_its_manifest_only(self):
        first = self.built()
        self.assets['devtools']['DevTools/Builds/CinterForgeTerminal/Toolchains/mac/bin/cmake'] = b'new-cmake'
        second = self.built()
        self.assertNotEqual(first['artifacts']['devtools']['fingerprint'],
                            second['artifacts']['devtools']['fingerprint'])
        for target in ('mac', 'win', 'linux'):
            self.assertEqual(first['artifacts'][target], second['artifacts'][target])

    def test_same_size_rewrite_even_if_mtime_restored_invalidates_build(self):
        executable = self.builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/cmake'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b'12345678')
        name = 'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/bin/cmake'
        self.assets['devtools'][name] = executable
        first = self.built()
        original = executable.stat().st_mtime_ns
        executable.write_bytes(b'ABCDEFGH')
        os.utime(executable, ns=(original, original))
        second = self.built()
        self.assertNotEqual(first['artifacts']['devtools']['fingerprint'],
                            second['artifacts']['devtools']['fingerprint'])

    def test_publish_cached_four_packages_unchanged(self):
        self.built()
        first = universal.prepare_publish_packages(self.builds, release=self.release)
        with mock.patch.object(universal, '_write_release_zip', side_effect=AssertionError('recompressed')):
            second = universal.prepare_publish_packages(self.builds, release=self.release)
        self.assertEqual(first, second)

    def test_unchanged_devtools_package_skips_rehash_and_recompression(self):
        self.built()
        from host import unpacked_release
        first = universal.prepare_publish_packages(self.builds, release=self.release)
        devtools_path = self.builds / 'CinterForgeTerminal/Distribution/CinterForgeTerminal-devtools.zip'
        real_sha = unpacked_release._sha
        def fail_sdk_hash(path):
            if Path(path) == devtools_path:
                raise AssertionError('Rehashed multi-GB SDK')
            return real_sha(path)
        with (mock.patch.object(unpacked_release, '_sha', side_effect=fail_sdk_hash),
              mock.patch.object(universal, '_write_release_zip', side_effect=AssertionError('recompressed'))):
            second = universal.prepare_publish_packages(self.builds, release=self.release)
        self.assertEqual(first, second)
        real_archive_hash = universal._hash
        def fail_archive_hash(path, *args, **kwargs):
            if Path(path) == devtools_path:
                raise AssertionError('Rehashed multi-GB SDK during manifest validation')
            return real_archive_hash(path, *args, **kwargs)
        with mock.patch.object(universal, '_hash', side_effect=fail_archive_hash):
            universal._distribution_files(self.builds, self.release)

    def test_new_builder_reuses_remote_sdk_without_creating_zip(self):
        self.built()
        first = universal.prepare_publish_packages(self.builds, release=self.release)
        devtools = first['artifacts']['devtools']
        path = self.builds / 'CinterForgeTerminal/Distribution/CinterForgeTerminal-devtools.zip'
        path.unlink()  # A fresh BUILDER has only its canonical unpacked inputs.
        from host import unpacked_release
        real_writer = universal._write_release_zip
        def no_sdk_zip(output, inputs, **kwargs):
            if kwargs.get('target') == 'devtools':
                raise AssertionError('Recompressed unchanged remotely published DevTools')
            return real_writer(output, inputs, **kwargs)
        with mock.patch.object(universal, '_write_release_zip', side_effect=no_sdk_zip):
            result = universal.prepare_publish_packages(
                self.builds, release=self.release, reuse_remote_devtools=devtools)
        self.assertTrue(result['artifacts']['devtools']['remote_only'])
        self.assertFalse(path.exists())
        _, report, files = universal._distribution_files(self.builds, self.release)
        self.assertIn('devtools', report['artifacts'])
        self.assertNotIn('CinterForgeTerminal-devtools.zip', files)

    def test_publish_remote_only_devtools_end_to_end_without_sdk_transfer(self):
        self.built()
        original = universal.prepare_publish_packages(self.builds, release=self.release)
        # This fixture stands in for an already-notarized public Mac release;
        # only the remote DevTools reuse path is under test here.
        original['artifacts']['mac']['notarized'] = True
        (self.builds / 'CinterForgeTerminal/Distribution/four-artifacts.json').write_text(
            json.dumps(original))
        old_manifest = (self.builds / 'CinterForgeTerminal/Distribution/four-artifacts.json').read_bytes()
        sdk = 'CinterForgeTerminal-devtools.zip'
        (self.builds / 'CinterForgeTerminal/Distribution' / sdk).unlink()
        remote = {'four-artifacts.json': {'size': len(old_manifest), 'etag': 'manifest-old',
                                         'last_modified': '2026-11-12T13:00:00Z'}}
        for target in universal.FOUR_TARGETS:
            item = original['artifacts'][target]
            remote[item['filename']] = {'size': item['bytes'], 'etag': 'multipart-etag-5',
                                         'last_modified': '2026-11-12T12:00:00Z'}
        sent = []
        downloads = []
        from host.publishing import cloudflare_r2_sync as r2
        def download(builds, release, cfg, dest, *, prefix, reuse_verified=None):
            downloads.append(dict(reuse_verified or {}))
            return json.loads((self.builds / 'CinterForgeTerminal/Distribution/four-artifacts.json').read_text())
        with (mock.patch.object(universal, '_cloudflare_config', return_value={'bucket': 'cinterforge'}),
              mock.patch.object(universal, '_require_notarized_mac_release'),
              mock.patch.object(universal, '_finalize_public_receipt', side_effect=lambda *args: args[1]),
              mock.patch.object(universal, '_download_four_to', side_effect=download),
              mock.patch.object(r2, '_list_remote', return_value=remote),
              mock.patch.object(r2, '_request', return_value=(200, {}, old_manifest)),
              mock.patch.object(r2, '_put_file', side_effect=lambda cfg,key,path,**kw: sent.append(key)),
              mock.patch.object(universal, '_write_release_zip', side_effect=AssertionError('SDK or other ZIP recompressed')),
              mock.patch.dict(os.environ, {'CINTER_MAC_SIGN_IDENTITY': 'Developer ID Application: fixture'})):
            receipt = universal.publish_four(self.builds, release=self.release, channel='beta')
        self.assertEqual([key.rsplit('/', 1)[-1] for key in sent], ['four-artifacts.json'])
        self.assertFalse((self.builds / 'CinterForgeTerminal/Distribution' / sdk).exists())
        self.assertEqual(len(downloads), 1)
        self.assertIn(sdk, downloads[0])
        self.assertEqual(receipt['files'][sdk], original['artifacts']['devtools']['sha256'])

    def test_public_dmg_repackages_unnotarized_dev_dmg(self):
        self.built()
        original = universal.prepare_publish_packages(self.builds, release=self.release)
        self.assertFalse(original['artifacts']['mac']['notarized'])
        from host import unpacked_release
        with mock.patch.dict(os.environ, {
                'CINTER_MAC_SIGN_IDENTITY': 'Developer ID Application: fixture',
                'CINTER_MAC_NOTARY_PROFILE': 'fixture-notary-profile'}), \
             mock.patch.object(universal, '_notarize_mac_app') as notarize:
            result = universal.prepare_publish_packages(self.builds, release=self.release, notarize=True)
        notarize.assert_called_once()
        self.assertTrue(result['artifacts']['mac']['notarized'])
        for target in ('linux', 'win', 'devtools'):
            self.assertEqual(original['artifacts'][target], result['artifacts'][target])

    def test_manifest_refuses_legacy_mac_zip_filename(self):
        self.built()
        universal.prepare_publish_packages(self.builds, release=self.release)
        receipt = self.builds / 'CinterForgeTerminal/Distribution/four-artifacts.json'
        content = json.loads(receipt.read_text())
        content['artifacts']['mac']['filename'] = 'CinterForgeTerminal-mac.zip'
        receipt.write_text(json.dumps(content))
        with self.assertRaisesRegex(RuntimeError, 'Unsafe release package name'):
            universal._distribution_files(self.builds, self.release)

    def test_corrupt_published_zip_repaired_only_by_publish(self):
        self.built()
        first = universal.prepare_publish_packages(self.builds, release=self.release)
        path = self.builds / 'CinterForgeTerminal/Distribution/CinterForgeTerminal-devtools.zip'
        with path.open('ab') as output:
            output.write(b'CORRUPTED')
        self.stage(targets=('devtools',))  # Stage ignores ZIPs completely.
        universal.prepare_publish_packages(self.builds, release=self.release)
        self.assertEqual(path.stat().st_size, first['artifacts']['devtools']['bytes'])

    def test_build_rejects_bedrock_server_without_consent(self):
        self.assets['win']['CinterForge Terminal/Builds/Servers/Bedrock/bedrock_server.exe'] = b'MZbad'
        with self.assertRaisesRegex(RuntimeError, 'Unsafe unpacked release path'):
            self.built()
        self.assertFalse((self.builds / 'CinterForgeTerminal/Distribution/four-built.json').exists())

    def test_mac_stage_recovers_backup_when_app_was_moved_aside(self):
        self.built(); self.stage(targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        app = root / 'CinterForge Terminal.app'
        backup = root / 'CinterForge Terminal.app.previous-stage'
        app.replace(backup)
        self.stage(targets=('mac',))
        self.assertTrue((app / 'Contents/MacOS/CinterForgeTerminal').is_file())
        self.assertFalse(backup.exists())

    def test_mac_stage_preserves_old_app_on_failed_verification(self):
        self.built(); self.stage(targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        exe = root / 'CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal'
        old = exe.read_bytes()
        self.assets['mac']['CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal'] = b'\xcf\xfa\xed\xfe-new'
        self.built()
        with mock.patch('platform.system', return_value='Darwin'), \
             mock.patch('host.mac_app._verify_signature', side_effect=RuntimeError('Invalid signature')):
            with self.assertRaisesRegex(RuntimeError, 'Invalid signature'):
                self.stage(targets=('mac',))
        self.assertEqual(exe.read_bytes(), old)

    def test_mac_stage_rejects_symlinked_backup(self):
        self.built(); self.stage(targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        backup = root / 'CinterForge Terminal.app.previous-stage'
        backup.symlink_to(self.scratch)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                self.stage(targets=('mac',))
        finally:
            backup.unlink(missing_ok=True)

    def test_published_zip_archives_contain_no_python_caches(self):
        self.built(); universal.prepare_publish_packages(self.builds, release=self.release)
        self.assertTrue((self.builds / 'CinterForgeTerminal/Distribution/CinterForgeTerminal-mac.dmg').is_file())
        for file in (self.builds / 'CinterForgeTerminal/Distribution').glob('*.zip'):
            with zipfile.ZipFile(file) as archive:
                self.assertFalse(any('__pycache__' in name or name.endswith('.pyc')
                                     for name in archive.namelist()))

    def test_setup_cache_skips_download_when_provisioned(self):
        from host.unpacked_release import setup_restore_if_needed
        for relative in ('CinterForgeTerminal/Toolchains/mac/env/bin/colima',
                         'CinterForgeTerminal/Toolchains/Windows/MSYS2/usr/bin/bash.exe',
                         'CinterForgeTerminal/Toolchains/LinuxSample'):
            path = self.builds / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.name == 'LinuxSample': path.mkdir()
            else: path.write_bytes(b'fixture')
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('Setup redownload')):
            result = setup_restore_if_needed(self.builds, release=self.release)
        self.assertFalse(result['downloaded'])


class PortablePythonSymlinkTests(unittest.TestCase):
    """A pinned CPython archive must flatten safe aliases, not chase loops."""
    def setUp(self):
        base = UNIVERSAL_PACKAGE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        base.mkdir(parents=True, exist_ok=True)
        self.scratch = Path(tempfile.mkdtemp(dir=base))
    def tearDown(self):
        shutil.rmtree(self.scratch)
    def archive(self, links):
        archive = self.scratch / 'cpython.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for directory in ('python', 'python/bin', 'python/lib',
                              'python/share', 'python/share/terminfo', 'python/share/terminfo/n'):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755  # Real Python tarball directories are traversable.
                tar.addfile(info)
            for name, contents in (('python/bin/python3', b'\x7fELFfake'),
                                   ('python/lib/stdlib.py', b'print(1)')):
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                tar.addfile(info, io.BytesIO(contents))
            for name, target in links.items():
                info = tarfile.TarInfo('python/' + name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tar.addfile(info)
        return archive
    def test_internal_tar_hardlink_is_flattened(self):
        from host.portable_python import _extract_portable
        archive = self.archive({})
        # Hardlinks in tar archives have root-relative targets, unlike
        # symlinks; process both without asking tarfile to dereference them.
        with tarfile.open(archive, 'w:gz') as tar:
            for directory in ('python', 'python/bin'):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755  # Real Python tarball directories are traversable.
                tar.addfile(info)
            info = tarfile.TarInfo('python/bin/python3')
            info.size = 6
            tar.addfile(info, io.BytesIO(b'python'))
            info = tarfile.TarInfo('python/bin/python')
            info.type = tarfile.LNKTYPE
            info.linkname = 'python/bin/python3'
            tar.addfile(info)
        destination = self.scratch / 'runtime'
        _extract_portable(archive, destination)
        self.assertEqual((destination / 'bin/python').read_bytes(), b'python')
        self.assertFalse(any(path.is_symlink() for path in destination.rglob('*')))
    def test_nonoptional_archive_collision_is_rejected(self):
        from host.portable_python import _extract_portable
        archive = self.scratch / 'python-collision.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name in ('python', 'python/bin'):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755  # Real Python tarball directories are traversable.
                tar.addfile(info)
            info = tarfile.TarInfo('python/bin/python3')
            info.size = 6
            tar.addfile(info, io.BytesIO(b'python'))
            alias = tarfile.TarInfo('python/bin/./python3')
            alias.type = tarfile.SYMTYPE
            alias.linkname = 'python3'
            tar.addfile(alias)
        with self.assertRaisesRegex(RuntimeError, 'Conflicting portable Python archive link'):
            _extract_portable(archive, self.scratch / 'runtime')
    def test_member_nested_under_archive_link_is_rejected(self):
        from host.portable_python import _extract_portable
        archive = self.scratch / 'nested.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo('python')
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
            info = tarfile.TarInfo('python/a')
            info.type = tarfile.SYMTYPE
            info.linkname = '.'
            tar.addfile(info)
            info = tarfile.TarInfo('python/a/data')
            info.size = 4
            tar.addfile(info, io.BytesIO(b'data'))
        with self.assertRaisesRegex(RuntimeError, 'nested under archive link'):
            _extract_portable(archive, self.scratch / 'runtime')
    def test_external_link_rejected_before_extract(self):
        from host.portable_python import _extract_portable
        archive = self.archive({'bin/escape': '../../../outside'})
        with self.assertRaisesRegex(RuntimeError, 'Escaping portable Python link'):
            _extract_portable(archive, self.scratch / 'runtime')


# Migrated from tests/test_windows_packaged_bash.py.
"""Windows release must contain a runnable MSYS2 Bash binary closure."""
from pathlib import Path
import tempfile
import unittest
import zipfile
import json
import hashlib
import stat
from host import universal_package as package


class WindowsPackagedBashTests(unittest.TestCase):
    def test_missing_bash_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, 'missing packaged MSYS2 Bash'):
            package._require_windows_shell({})

    def test_rejects_non_pe_bash_even_if_present(self):
        files = {package.WINDOWS_SHELL_PREFIX + n: b'MZ-fixture'
                 for n in package.WINDOWS_SHELL_REQUIRED}
        package._require_windows_shell(files)
        files[package.WINDOWS_SHELL_PREFIX + 'bash.exe'] = b'not-an-exe'
        with self.assertRaisesRegex(RuntimeError, 'not PE'):
            package._require_windows_shell(files)

    def test_release_zip_rejects_missing_bash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'windows.zip'
            content = b'MZ-fixture'
            name = 'CinterForge Terminal/CinterForge Terminal.exe'
            record = {'sha256': hashlib.sha256(content).hexdigest(),
                      'bytes': len(content), 'mode': 0o644}
            manifest = {'schema': package.FOUR_SCHEMA, 'target': 'win',
                        'release': 'test', 'root': 'CinterForge Terminal',
                        'files': {name: record}}
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr(name, content)
                z.writestr('release-manifest.json', json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, 'missing packaged MSYS2 Bash'):
                package._verify_release_zip(path, target='win', release='test')



