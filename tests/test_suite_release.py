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

    def test_native_host_launches_python_without_stage_bytecode_writes(self):
        # The actual native process launcher must use -B on both Windows and
        # POSIX. Testing only a Python mock would miss the original bug.
        cpp = (Path(manager.__file__).resolve().parents[1]
               / 'src/CinterPythonHost.cpp').read_text(encoding='utf-8')
        self.assertIn(' + " -B -u " + quote_windows(script)', cpp)
        self.assertIn('values {interpreter, "-B", "-u", script}', cpp)
        package = self.builds / 'Python/Host/mac/arm64/test-pycache'
        package.mkdir(parents=True)
        (package / 'child.py').write_text('VALUE = 1\n')
        entry = ('import importlib.util, pathlib; '
                 'p=pathlib.Path(__import__("sys").argv[1]); '
                 's=importlib.util.spec_from_file_location("child", p); '
                 'm=importlib.util.module_from_spec(s); s.loader.exec_module(m)')
        run = subprocess.run([sys.executable, '-B', '-u', '-c', entry,
                              str(package / 'child.py')], check=True,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0)
        self.assertFalse((package / '__pycache__').exists())

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
    """Four-ZIP Build and Steam install: no Bedrock before EULA acceptance."""
    def setUp(self):
        base = UNIVERSAL_PACKAGE_BUILDS / 'Intermediates/CinterForgeTerminalTests'
        base.mkdir(parents=True, exist_ok=True)
        self.scratch = Path(tempfile.mkdtemp(dir=base, prefix='four-steam-'))
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
            self.assets[target] = {f'{root}/{binary}': magic + b'fixture-binary',
                                   f'{root}/Builds/Python/Host/{target}/x64/{self.release}/webapp/CinterForgeTerminal.py': b'print("ready")',
                                   f'{root}/Builds/Python/Host/{target}/x64/{self.release}/webapp/CinterForgeTerminal.py': b'# EULA-gated installer only',
                                   f'{root}/Builds/Run/README.txt': b'No Bedrock server is provisioned here'}
        for name in universal.WINDOWS_SHELL_REQUIRED:
            self.assets['win'][universal.WINDOWS_SHELL_PREFIX + name] = b'MZmock Windows shell fixture'
        self.assets['devtools'] = {
            'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/bin/cmake': b'cmake',
            'DevTools/Builds/CinterForgeTerminal/Toolchains/LinuxSample/oci.tar': b'Linux OCI fixture',
            'DevTools/Builds/CinterForgeTerminal/Toolchains/WindowsCross/oci.tar': b'Windows OCI fixture',
        }
        self.patch_apps = mock.patch.object(universal, '_app_assets',
                                            side_effect=lambda b, t, r: self.assets[t])
        self.patch_tools = mock.patch.object(universal, '_devtools_assets',
                                             side_effect=lambda b: self.assets['devtools'])
        self.patch_apps.start(); self.patch_tools.start()
        # Fixture Mach-O headers do not constitute signed macOS app bundles.
        # Production Stage still calls the real codesign verifier.
        self.fixture_signature = mock.patch('host.mac_app._verify_signature')
        self.fixture_signature.start()
    def tearDown(self):
        self.fixture_signature.stop()
        self.patch_apps.stop(); self.patch_tools.stop()
        shutil.rmtree(self.scratch)
    def test_release_zip_preserves_runtime_python_and_devtools_executable_modes(self):
        runtime = self.scratch / 'python'
        runtime.write_bytes(b'\xcf\xfa\xed\xfe-mac-python-fixture')
        runtime.chmod(0o644)  # Upstream unpacker may lose executable metadata.
        app_python = ('CinterForge Terminal.app/Contents/Resources/Builds/Python/'
                      'Environments/mac/arm64/cinterterminalenv/bin/python')
        app_zip = self.scratch / 'mac-python-fixture.zip'
        universal._write_release_zip(app_zip, {app_python: runtime}, target='mac',
                                     release=self.release, root='CinterForge Terminal.app')
        with zipfile.ZipFile(app_zip) as archive:
            self.assertEqual((archive.getinfo(app_python).external_attr >> 16) & 0o777, 0o755)
        tool = self.scratch / 'tool'
        tool.write_text('#!/bin/sh\necho cmake\n')
        tool.chmod(0o755)
        tool_name = 'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/env/bin/cmake'
        data = self.scratch / 'data'
        data.write_bytes(b'plain data')
        data.chmod(0o644)
        data_name = 'DevTools/Builds/Toolchains/WebView/metadata.txt'
        dev_zip = self.scratch / 'devtool-modes-fixture.zip'
        universal._write_release_zip(dev_zip, {tool_name: tool, data_name: data},
                                     target='devtools', release=self.release, root='DevTools')
        with zipfile.ZipFile(dev_zip) as archive:
            self.assertEqual((archive.getinfo(tool_name).external_attr >> 16) & 0o777, 0o755)
            self.assertEqual((archive.getinfo(data_name).external_attr >> 16) & 0o777, 0o644)
    def test_mac_stage_preserves_existing_app_when_signature_rejected(self):
        universal.build_four(self.builds, release=self.release)
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        executable = root / 'CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal'
        old_bytes = executable.read_bytes()
        with mock.patch('platform.system', return_value='Darwin'), \
             mock.patch('host.mac_app._verify_signature',
                        side_effect=RuntimeError('Invalid extracted signature')):
            with self.assertRaisesRegex(RuntimeError, 'Invalid extracted signature'):
                universal.stage_four(self.builds, release=self.release,
                                     steamapps=self.steamapps, targets=('mac',))
        self.assertEqual(executable.read_bytes(), old_bytes)
    def test_mac_stage_recovers_backup_when_installed_app_was_moved_aside(self):
        """A crash between old-app rename and promotion must self-heal."""
        universal.build_four(self.builds, release=self.release)
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        old_app = root / 'CinterForge Terminal.app'
        backup = root / 'CinterForge Terminal.app.previous-stage'
        old_app.replace(backup)  # Simulate an interrupted previous Stage.
        self.assertFalse(old_app.exists())
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        self.assertTrue((old_app / 'Contents/MacOS/CinterForgeTerminal').is_file())
        self.assertFalse(backup.exists())

    def test_mac_stage_preserves_stale_backup_if_both_apps_exist(self):
        """A stale backup never blocks another Stage and is not discarded."""
        universal.build_four(self.builds, release=self.release)
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        old_app = root / 'CinterForge Terminal.app'
        backup = root / 'CinterForge Terminal.app.previous-stage'
        backup.mkdir()
        (backup / 'previous-version.txt').write_text('original prior user data')
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        self.assertTrue((old_app / 'Contents/MacOS/CinterForgeTerminal').is_file())
        self.assertFalse(backup.exists())
        recovered = list((self.builds / 'Intermediates/CinterForgeTerminal/StageRecovery/macOS')
                         .rglob('previous-version.txt'))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_text(), 'original prior user data')

    def test_mac_stage_recovers_stale_backup_across_volumes(self):
        """Recovery works even when Steam and Builds have different devices."""
        import errno
        import os
        from contextlib import redirect_stdout
        from io import StringIO

        universal.build_four(self.builds, release=self.release)
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        backup = root / 'CinterForge Terminal.app.previous-stage'
        backup.mkdir()
        (backup / 'preserve-this.txt').write_text('older app data')
        native_rename = os.rename

        def simulate_other_volume(source, destination):
            if str(source) == str(backup):
                raise OSError(errno.EXDEV, 'Cross-device link')
            return native_rename(source, destination)

        output = StringIO()
        with mock.patch('os.rename', side_effect=simulate_other_volume), redirect_stdout(output):
            universal.stage_four(self.builds, release=self.release,
                                 steamapps=self.steamapps, targets=('mac',))
        self.assertIn('macOS recovery protocol: restart-safe-v2', output.getvalue())
        self.assertIn('RECOVERED stale macOS Stage backup', output.getvalue())
        self.assertFalse(backup.exists())
        saved = list((self.builds / 'Intermediates/CinterForgeTerminal/StageRecovery/macOS')
                     .rglob('preserve-this.txt'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), 'older app data')
        self.assertTrue((root / 'CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal').is_file())

    def test_mac_stage_refuses_symlinked_recovery_backup_without_changes(self):
        universal.build_four(self.builds, release=self.release)
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        executable = root / 'CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal'
        old_bytes = executable.read_bytes()
        backup = root / 'CinterForge Terminal.app.previous-stage'
        backup.symlink_to(self.scratch, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe macOS Stage recovery backup'):
            universal.stage_four(self.builds, release=self.release,
                                 steamapps=self.steamapps, targets=('mac',))
        self.assertEqual(executable.read_bytes(), old_bytes)
        self.assertTrue(backup.is_symlink())

    def test_mac_stage_rolls_back_if_installed_signature_fails(self):
        universal.build_four(self.builds, release=self.release)
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps, targets=('mac',))
        root = universal._steam_release_path(self.steamapps, 'mac', self.release)
        executable = root / 'CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal'
        old_bytes = executable.read_bytes()
        def verify(path):
            # Candidate may be valid even if installed-path verification fails.
            if Path(path).parent == root:
                raise RuntimeError('Installed signature verification failed')
        with mock.patch('platform.system', return_value='Darwin'), \
             mock.patch('host.mac_app._verify_signature', side_effect=verify):
            with self.assertRaisesRegex(RuntimeError, 'Installed signature verification failed'):
                universal.stage_four(self.builds, release=self.release,
                                     steamapps=self.steamapps, targets=('mac',))
        self.assertEqual(executable.read_bytes(), old_bytes)
        self.assertFalse((root / 'CinterForge Terminal.app.previous-stage').exists())

    def test_build_four_separate_zips_stage_all_into_steamapps_without_bedrock(self):
        result = universal.build_four(self.builds, release=self.release)
        self.assertEqual(set(result['artifacts']), {'mac', 'linux', 'win', 'devtools'})
        output = self.builds / 'CinterForgeTerminal/Distribution'
        self.assertEqual(len(list(output.glob('*.zip'))), 4)
        for target, entry in result['artifacts'].items():
            manifest = universal._verify_release_zip(output / entry['filename'],
                                                      target=target, release=self.release)
            self.assertEqual(manifest['target'], target)
            root = {'mac': 'CinterForge Terminal.app', 'linux': 'CinterForge Terminal',
                    'win': 'CinterForge Terminal', 'devtools': 'DevTools'}[target]
            self.assertEqual(manifest['root'], root)
            self.assertTrue(all(name.startswith(root + '/') for name in manifest['files']))
            if target == 'linux':
                self.assertEqual(manifest['files'][root + '/CinterForge Terminal']['mode'], 0o755)
            if target == 'win':
                self.assertIn(root + '/CinterForge Terminal.exe', manifest['files'])
            self.assertNotIn('Servers/Bedrock', ' '.join(manifest['files']))
        installed = universal.stage_four(self.builds, release=self.release, steamapps=self.steamapps)
        for target in result['artifacts']:
            new_root = universal._steam_release_path(self.steamapps, target, self.release)
            self.assertTrue((new_root / universal.APP_ROOTS[target]).is_dir())
        self.assertFalse(any(p.name in {'bedrock_server', 'bedrock_server.exe'}
                             for p in installed.rglob('*')))
        self.assertFalse((universal._steam_release_path(self.steamapps, 'mac', self.release) /
                          'CinterForge Terminal.app/Contents/Resources/Builds/Servers/Bedrock').exists())
        self.assertEqual(universal.stage_four(self.builds, release=self.release,
                                              steamapps=self.steamapps), installed)
    def test_four_artifact_status_reports_build_and_steam_separately(self):
        missing = universal.status_four(self.builds, release=self.release,
                                        steamapps=self.steamapps)
        self.assertFalse(missing['complete_build_receipt'])
        self.assertFalse(missing['complete_stage_receipt'])
        self.assertEqual(set(missing['targets']), set(universal.FOUR_TARGETS))
        self.assertTrue(all(not data['archive_present'] for data in missing['targets'].values()))
        universal.build_four(self.builds, release=self.release)
        built = universal.status_four(self.builds, release=self.release,
                                      steamapps=self.steamapps)
        self.assertTrue(built['complete_build_receipt'])
        self.assertFalse(built['complete_stage_receipt'])
        self.assertTrue(all(data['archive_present'] and data['size_matches_receipt']
                            for data in built['targets'].values()))
        universal.stage_four(self.builds, release=self.release,
                             steamapps=self.steamapps)
        staged = universal.status_four(self.builds, release=self.release,
                                       steamapps=self.steamapps)
        self.assertTrue(staged['complete_stage_receipt'])
        self.assertTrue(all(data['steam_receipt_matches'] and data['steam_app_root_present']
                            for data in staged['targets'].values()))
    def test_public_publish_selects_distinct_bucket_and_roundtrip(self):
        from host.publishing import cloudflare_r2_sync as r2
        universal.build_four(self.builds, release=self.release)
        objects = {}
        cfg = {'bucket': 'cinterforge'}
        def put(config, key, path, **kwargs):
            self.assertEqual(config['bucket'], 'cinterforge')
            objects[key] = path.read_bytes()
        def get(config, key, path, expected_size=None):
            self.assertEqual(config['bucket'], 'cinterforge')
            self.assertEqual(expected_size, len(objects[key]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(objects[key])
        def listed(config, prefix):
            self.assertEqual(config['bucket'], 'cinterforge')
            return {key[len(prefix)+1:]: {'size': len(val), 'etag': hashlib.md5(val).hexdigest()}
                    for key, val in objects.items() if key.startswith(prefix + '/')}
        lagomorph_fixture = self.scratch / 'Lagomorph-Quadra650.iso'
        lagomorph_fixture.write_bytes(b'Classic-HFS-publish-fixture')
        with mock.patch.object(universal, '_lagomorph_public_disk', return_value=lagomorph_fixture), \
             mock.patch.object(universal, '_cloudflare_config', return_value=cfg) as config, \
             mock.patch.object(r2, '_list_remote', side_effect=listed), \
             mock.patch.object(r2, '_put_file', side_effect=put), \
             mock.patch.object(r2, '_get_file', side_effect=get), \
             mock.patch.object(universal, '_verify_public_urls') as publicly_reachable:
            receipt = universal.publish_four(self.builds, release=self.release, channel='public')
            publicly_reachable.assert_called_once()
            urls = receipt['public_urls']
            self.assertEqual(set(urls), {'manifest', 'mac', 'linux', 'win', 'devtools', 'lagomorph'})
            self.assertTrue(urls['devtools'].startswith('https://cinterforge.ucreg.io/'))
            self.assertTrue(urls['devtools'].endswith('/CinterForgeTerminal-devtools.zip'))
            prefix = 'CinterForgeTerminal/Distribution/'
            for target in universal.FOUR_TARGETS:
                self.assertIn(prefix + f'CinterForgeTerminal-{target}.zip', objects)
            self.assertIn(prefix + 'four-artifacts.json', objects)
            # Keep existing classic Macintosh media URLs and objects versioned.
            self.assertIn(prefix + self.release + '/Lagomorph-Quadra650.iso', objects)
            self.assertEqual(receipt['bucket'], 'cinterforge')
            self.assertEqual(receipt['channel'], 'public')
            self.assertEqual(len(objects), 6)
            config.assert_called_with(None, channel='public')
            downloaded = self.builds / 'CinterForgeTerminal/Publish/Downloaded/public' / self.release
            self.assertEqual(len(list(downloaded.glob('*.zip'))), 4)
            self.assertEqual(universal.fetch_four(self.builds, release=self.release, channel='public'),
                             json.loads((downloaded / 'four-artifacts.json').read_text()))
    def test_public_403_preserves_verified_receipt_and_skips_reupload(self):
        """CDN propagation must never restart a completed multi-GB R2 transfer."""
        from host.publishing import cloudflare_r2_sync as r2
        universal.build_four(self.builds, release=self.release)
        objects = {}
        cfg = {'bucket': 'cinterforge'}
        uploads = []
        downloads = []
        def put(config, key, path, **kwargs):
            uploads.append(key)
            objects[key] = path.read_bytes()
        def get(config, key, path, expected_size=None):
            downloads.append(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(objects[key])
        def listed(config, prefix):
            return {key[len(prefix)+1:]: {'size': len(data), 'etag': hashlib.md5(data).hexdigest()}
                    for key, data in objects.items() if key.startswith(prefix+'/')}
        disk = self.scratch / 'Lagomorph-Quadra650.iso'
        disk.write_bytes(b'Classic HFS fixture')
        receipt_path = (self.builds / 'CinterForgeTerminal/Publish/Receipts/public' /
                        self.release / 'cloudflare.json')
        with mock.patch.object(universal, '_lagomorph_public_disk', return_value=disk), \
             mock.patch.object(universal, '_cloudflare_config', return_value=cfg), \
             mock.patch.object(r2, '_list_remote', side_effect=listed), \
             mock.patch.object(r2, '_put_file', side_effect=put), \
             mock.patch.object(r2, '_get_file', side_effect=get), \
             mock.patch.object(universal, '_verify_public_urls',
                               side_effect=[RuntimeError('HTTP 403'), None]) as public_check:
            with self.assertRaisesRegex(RuntimeError, 'Public verification pending'):
                universal.publish_four(self.builds, release=self.release, channel='public')
            cached = json.loads(receipt_path.read_text())
            self.assertEqual(cached['roundtrip'], 'sha256-verified')
            self.assertEqual(cached['public_http'], 'pending')
            self.assertEqual(len(uploads), 6)
            first_downloads = len(downloads)
            final = universal.publish_four(self.builds, release=self.release, channel='public')
            self.assertEqual(final['public_http'], 'verified')
            self.assertEqual(len(uploads), 6)
            self.assertEqual(len(downloads), first_downloads)
            self.assertEqual(public_check.call_count, 2)

    def test_stage_rejects_tampered_zip_before_creating_install(self):
        result = universal.build_four(self.builds, release=self.release)
        path = (self.builds / 'CinterForgeTerminal/Distribution' /
                result['artifacts']['linux']['filename'])
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('CinterForge Terminal/injected.txt', 'not in manifest')
        with self.assertRaisesRegex(RuntimeError, 'size mismatch|checksum mismatch'):
            universal.stage_four(self.builds, release=self.release, steamapps=self.steamapps)
        self.assertFalse(universal._steam_release_path(self.steamapps, 'mac', self.release).exists())
    def test_build_rejects_server_binary_even_before_consent(self):
        self.assets['win']['CinterForge Terminal/Builds/Servers/Bedrock/bedrock_server.exe'] = b'MZbad'
        with self.assertRaisesRegex(RuntimeError, 'Bedrock-server paths'):
            universal.build_four(self.builds, release=self.release)
        self.assertFalse((self.builds / 'CinterForgeTerminal/Distribution/four-artifacts.json').exists())
    def test_old_versioned_zip_names_are_adopted_without_recompression(self):
        """First stable-name build reuses verified older archives, including DevTools."""
        first = universal.build_four(self.builds, release=self.release)
        directory = self.builds / 'CinterForgeTerminal/Distribution'
        legacy = directory / self.release
        legacy.mkdir()
        historical = dict(first)
        historical['artifacts'] = {name: dict(item) for name, item in first['artifacts'].items()}
        for target, item in historical['artifacts'].items():
            original = directory / item['filename']
            old_name = f'CinterForgeTerminal-{target}-{self.release}.zip'
            original.replace(legacy / old_name)
            item['filename'] = old_name
        (legacy / 'four-artifacts.json').write_text(json.dumps(historical))
        (directory / 'four-artifacts.json').unlink()
        with mock.patch.object(universal, '_write_release_zip',
                               side_effect=AssertionError('must reuse existing archive')):
            adopted = universal.build_four(self.builds, release=self.release)
        self.assertEqual(adopted['release'], self.release)
        for target, item in adopted['artifacts'].items():
            self.assertEqual(item['filename'], f'CinterForgeTerminal-{target}.zip')
            self.assertEqual(item['sha256'], historical['artifacts'][target]['sha256'])
            self.assertTrue((directory / item['filename']).is_file())
            self.assertTrue((legacy / historical['artifacts'][target]['filename']).is_file())

    def test_unchanged_devtools_uses_metadata_cache_without_rehashing(self):
        first = universal.build_four(self.builds, release=self.release)
        cache = universal._release_cache_file(self.builds, self.release, 'devtools')
        self.assertTrue(cache.is_file())
        self.assertTrue(cache.is_relative_to(self.builds))
        with mock.patch.object(universal, '_verify_release_zip',
                               side_effect=AssertionError('unexpected ZIP read')), \
             mock.patch.object(universal, '_hash',
                               side_effect=AssertionError('unexpected input rehash')):
            second = universal.build_four(self.builds, release=self.release)
        self.assertEqual(second['artifacts'], first['artifacts'])

    def test_changed_devtools_rebuilds_only_devtools(self):
        original = universal.build_four(self.builds, release=self.release)
        original_artifacts = original['artifacts'].copy()
        key = 'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/bin/cmake'
        self.assets['devtools'][key] = b'cmake-updated'
        rebuilt = universal.build_four(self.builds, release=self.release)
        self.assertNotEqual(original_artifacts['devtools']['sha256'],
                            rebuilt['artifacts']['devtools']['sha256'])
        for target in ('mac', 'linux', 'win'):
            self.assertEqual(original_artifacts[target], rebuilt['artifacts'][target])

    def test_same_size_rewrite_with_restored_mtime_invalidates_cache(self):
        source = self.scratch / 'devtools-binary'
        source.write_bytes(b'12345678')
        name = 'DevTools/Builds/CinterForgeTerminal/Toolchains/mac/bin/cmake'
        self.assets['devtools'][name] = source
        original = universal.build_four(self.builds, release=self.release)
        old_mtime = source.stat().st_mtime_ns
        source.write_bytes(b'ABCDEFGH')
        os.utime(source, ns=(old_mtime, old_mtime))
        rebuilt = universal.build_four(self.builds, release=self.release)
        self.assertNotEqual(original['artifacts']['devtools']['sha256'],
                            rebuilt['artifacts']['devtools']['sha256'])

    def test_corrupted_devtools_zip_invalidates_cached_archive(self):
        original = universal.build_four(self.builds, release=self.release)
        archive = (self.builds / 'CinterForgeTerminal/Distribution' /
                   original['artifacts']['devtools']['filename'])
        with archive.open('ab') as handle:
            handle.write(b'CORRUPT-APPENDED-DATA')
        rebuilt = universal.build_four(self.builds, release=self.release)
        self.assertEqual(rebuilt['artifacts']['devtools'],
                         original['artifacts']['devtools'])
        self.assertEqual(archive.stat().st_size,
                         original['artifacts']['devtools']['bytes'])

    def test_changed_input_rebuilds_same_four_zips_and_reinstalls_to_steam(self):
        original = universal.build_four(self.builds, release=self.release)
        installed = universal.stage_four(self.builds, release=self.release,
                                         steamapps=self.steamapps)
        note = universal._steam_release_path(self.steamapps, 'linux', self.release) / 'readme-user.txt'
        note.write_text('keep me')
        self.assets['linux']['CinterForge Terminal/CinterForge Terminal'] = b'\x7fELFchanged'
        rebuilt = universal.build_four(self.builds, release=self.release)
        self.assertEqual(set(rebuilt['artifacts']), set(universal.FOUR_TARGETS))
        self.assertNotEqual(original['artifacts']['linux']['sha256'],
                            rebuilt['artifacts']['linux']['sha256'])
        self.assertEqual(len(list((self.builds / 'CinterForgeTerminal/Distribution').glob('*.zip'))), 4)
        universal.stage_four(self.builds, release=self.release, steamapps=self.steamapps)
        self.assertEqual((universal._steam_release_path(self.steamapps, 'linux', self.release) /
                          'CinterForge Terminal/CinterForge Terminal').read_bytes(),
                         b'\x7fELFchanged')
        self.assertEqual(note.read_text(), 'keep me')

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



