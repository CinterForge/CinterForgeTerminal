"""Focused games regression contracts. Native target acceptance is separate."""
from tests._support import *

class LagomorphClassicHFSTests(unittest.TestCase):
    """Never publish a modern HFS+ image to the System 7.6 Quadra."""

    def setUp(self):
        from host import lagomorph_setup
        self.lag = lagomorph_setup
        root = BOOTSTRAP_BUNDLE_BUILDS / 'Intermediates'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=root, prefix='lag-hfs-'))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_darwin_python_without_os_xattr_uses_native_metadata_fallback(self):
        """HOPE-DEV's pyenv 3.11 lacks os.getxattr and os.setxattr."""
        game = self.tmp / 'Lagomorph'
        game.write_bytes(b'')
        attributes = {}
        def read(path, name):
            return attributes[(str(path), name)]
        def write(path, name, value):
            attributes[(str(path), name)] = value
        with mock.patch.object(self.lag.platform, 'system', return_value='Darwin'), \
             mock.patch.object(self.lag.os, 'getxattr', None, create=True), \
             mock.patch.object(self.lag.os, 'setxattr', None, create=True), \
             mock.patch.object(self.lag, '_macos_xattr_get', side_effect=read) as native_get, \
             mock.patch.object(self.lag, '_macos_xattr_set', side_effect=write) as native_set:
            self.lag._setxattr(game, 'com.apple.FinderInfo', b'APPLKILL' + bytes(24))
            self.lag._setxattr(game, 'com.apple.ResourceFork', b'GAME-RESOURCE-FORK')
            finder, fork = self.lag._classic_application_metadata(game)
            self.assertEqual(finder[:8], b'APPLKILL')
            self.assertEqual(fork, b'GAME-RESOURCE-FORK')
            self.assertEqual(native_set.call_count, 2)
            self.assertEqual(native_get.call_count, 2)

    def test_battles_inspection_metadata_and_static_hints(self):
        from host import lagomorph_setup as lag
        game = self.tmp / 'Lagomorph'
        battle = self.tmp / 'Battle 1'
        game.write_bytes(b'Find Battles/Battle 1 next to application')
        battle.write_bytes(b'GAME-BATTLE')
        attributes = {
            (str(game), 'com.apple.FinderInfo'): b'APPLKILL' + bytes(24),
            (str(game), 'com.apple.ResourceFork'): b'RESOURCE Battles Battle 2',
            (str(battle), 'com.apple.FinderInfo'): b'DATAKILL' + bytes(24),
            (str(battle), 'com.apple.ResourceFork'): b'BATTLE-RESOURCE',
        }
        with mock.patch.object(lag, '_getxattr', side_effect=lambda p, k: attributes[(str(p), k)]):
            item = lag._finder_metadata_for_inspection(battle)
            hints = lag._game_path_hints(game)
        self.assertEqual(item['type'], 'DATA')
        self.assertEqual(item['creator'], 'KILL')
        self.assertEqual(item['resource_bytes'], len(b'BATTLE-RESOURCE'))
        self.assertTrue(any('Battle' in candidate['context'] for candidate in hints['candidates']))
        self.assertIn('not evidence', hints['limitation'])


    def test_standard_hfs_mdb_is_required(self):
        disk = self.tmp / 'standard.iso'
        disk.write_bytes(b'\0' * 1024 + b'BD' + b'\0' * 4096)
        self.lag._classic_hfs_signature(disk)
        disk.write_bytes(b'\0' * 1024 + b'H+' + b'\0' * 4096)
        with self.assertRaisesRegex(RuntimeError, 'not classic HFS'):
            self.lag._classic_hfs_signature(disk)

    def test_macbinary_preserves_data_and_resource_fork_lengths(self):
        source = self.tmp / 'Lagomorph 1'
        source.write_bytes(b'GAME-DATA')
        output = self.tmp / 'game.bin'
        with mock.patch.object(self.lag.platform, 'system', return_value='Darwin'), \
             mock.patch.object(self.lag.os, 'getxattr', create=True, side_effect=lambda p, key:
                               b'APPL????' + b'\x00'*24 if key == 'com.apple.FinderInfo'
                               else b'GAME-RESOURCE'):
            self.lag._macbinary(source, output)
        data = output.read_bytes()
        self.assertEqual(data[0], 0)
        self.assertEqual(data[1], len(source.name))
        self.assertEqual(data[65:69], b'APPL')
        self.assertEqual(int.from_bytes(data[83:87], 'big'), len(b'GAME-DATA'))
        self.assertEqual(int.from_bytes(data[87:91], 'big'), len(b'GAME-RESOURCE'))
        self.assertEqual(data[128:137], b'GAME-DATA')
        self.assertEqual(data[256:269], b'GAME-RESOURCE')

    def test_appledouble_original_game_application_metadata(self):
        import struct
        payload = bytearray(50 + 32 + 17)
        struct.pack_into('>II', payload, 0, 0x00051607, 0x00020000)
        struct.pack_into('>H', payload, 24, 2)
        struct.pack_into('>III', payload, 26, 9, 50, 32)
        struct.pack_into('>III', payload, 38, 2, 82, 17)
        payload[50:82] = b'APPLKILL' + bytes(24)
        payload[82:99] = b'ORIGINAL-GAME-RSC'
        finder, fork = self.lag._appledouble_metadata(bytes(payload), '._Lagomorph')
        self.assertEqual(finder[:8], b'APPLKILL')
        self.assertEqual(fork, b'ORIGINAL-GAME-RSC')
        with self.assertRaisesRegex(RuntimeError, 'Truncated'):
            self.lag._appledouble_metadata(bytes(payload[:18]), '._Lagomorph')

    def test_mac_original_game_zip_restores_resource_and_finder_metadata(self):
        import struct
        archive = self.tmp / 'Lagomorph.zip'
        payload = bytearray(50 + 32 + 17)
        struct.pack_into('>II', payload, 0, 0x00051607, 0x00020000)
        struct.pack_into('>H', payload, 24, 2)
        struct.pack_into('>III', payload, 26, 9, 50, 32)
        struct.pack_into('>III', payload, 38, 2, 82, 17)
        payload[50:82] = b'APPLKILL' + bytes(24)
        payload[82:99] = b'ORIGINAL-GAME-RSC'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('Lagomorph', '')
            output.writestr('__MACOSX/._Lagomorph', payload)
        output = self.tmp / 'Extracted'
        output.mkdir()
        (output / 'Lagomorph').write_bytes(b'')
        xattrs = {}
        def write_attr(target, name, value):
            xattrs[(str(target), name)] = value
        def read_attr(target, name):
            return xattrs[(str(target), name)]
        with mock.patch.object(self.lag.os, 'setxattr', create=True, side_effect=write_attr), \
             mock.patch.object(self.lag.os, 'getxattr', create=True, side_effect=read_attr):
            self.lag._recover_zip_forks(archive, output)
            finder, fork = self.lag._classic_application_metadata(output / 'Lagomorph')
            self.assertEqual(finder[:8], b'APPLKILL')
            self.assertEqual(fork, b'ORIGINAL-GAME-RSC')

    def test_existing_canonical_builds_game_tree_is_selected_without_input_copy(self):
        builds = self.tmp / 'Builds'
        game = builds / 'Lagomorph/Lagomorph 1.0/Lagomorph'
        game.mkdir(parents=True)
        (game / 'Lagomorph.zip').write_bytes(b'original-archive-fixture')
        (builds / 'CinterForgeTerminal/Input/Lagomorph').mkdir(parents=True)
        self.assertEqual(self.lag._source(builds, None), game)
        with mock.patch.object(self.lag, '_validate_zip'), \
             mock.patch.object(self.lag, '_unpack') as unpack, \
             mock.patch.object(self.lag, '_files', return_value=[self.tmp / 'Original Game Archive/Lagomorph']), \
             mock.patch.object(self.lag, '_classic_application_metadata', return_value=(b'APPLKILL', b'fork')):
            result = self.lag._original_game_override(game, self.tmp)
        self.assertEqual(result.name, 'Lagomorph')
        self.assertEqual(unpack.call_args.args[0], game / 'Lagomorph.zip')

    def test_original_game_resource_fork_is_required(self):
        game = self.tmp / 'Lagomorph 1'
        game.write_bytes(b'')
        with mock.patch.object(self.lag.os, 'getxattr', create=True, side_effect=lambda p, key:
                               b'APPLKILL' + bytes(24) if key == 'com.apple.FinderInfo' else b''):
            with self.assertRaisesRegex(RuntimeError, 'resource fork'):
                self.lag._classic_application_metadata(game)

    def test_hfs_media_uses_existing_linux_guest_not_homebrew(self):
        # Mac only creates source MacBinary files; the Linux t guest builds HFS.
        from unittest import mock
        from host import lagomorph_setup as lag
        import os
        builds = self.tmp / 'Builds'
        sdk = builds / 'CinterForgeTerminal/Toolchains/mac/env/bin'
        sdk.mkdir(parents=True)
        (sdk / 'docker').write_bytes(b'docker-cli')
        package = builds / 'CinterForgeTerminal/Toolchains/Linux/hfsutils/3.2.6-15/hfsutils.deb'
        package.parent.mkdir(parents=True)
        package.write_bytes(b'pinned-debian-package-fixture')
        (builds / 'Logs/CinterForgeTerminal').mkdir(parents=True)
        scratch = builds / 'Intermediates/CinterForgeTerminal/Lagomorph/test'
        scratch.mkdir(parents=True)
        content = scratch / 'content'
        content.mkdir()
        (content / 'Lagomorph 1').write_bytes(b'game data')
        image = scratch / 'Lagomorph-Application-1440K.img'
        called = []

        def fake_runner(command, **kwargs):
            called.append(list(command))
            self.assertIn('--network', command)
            self.assertEqual(command[command.index('--network') + 1], 'none')
            self.assertIn('host.lagomorph_hfs_linux', command)
            job_file = scratch / 'hfs-linux-job.json'
            data = json.loads(job_file.read_text())
            self.assertEqual(len(data['images']), 1)
            self.assertEqual(data['images'][0]['image'], image.relative_to(builds).as_posix())
            with image.open('wb') as writer:
                writer.truncate(lag.FLOPPY_BYTES)
            with image.open('r+b') as writer:
                writer.seek(1024)
                writer.write(b'BD')
            return mock.Mock(returncode=0)

        def fake_macbinary(source, target):
            target.write_bytes(b'encoded')
        with mock.patch.dict(os.environ, {'DOCKER_CONTEXT': 'colima-t'}), \
             mock.patch.object(lag, '_hfs_package', return_value=package), \
             mock.patch.object(lag, '_macbinary', side_effect=fake_macbinary), \
             mock.patch('host.three_samples._docker_image', return_value='pinned-debian-image'), \
             mock.patch('host.windows_environment._run', side_effect=fake_runner), \
             mock.patch('host.amd64_backend.docker_bind_source', lambda path: '/host/mac' + str(path)):
            lag._stage_hfs_linux(builds, scratch, [(content, image, 'Lagomorph 1',
                                                    lag.FLOPPY_BYTES, None)])
        self.assertEqual(len(called), 1)
        self.assertIn('pinned-debian-image', called[0])
        self.assertNotIn('brew', ' '.join(called[0]))

    def test_linux_hfs_worker_rejects_untrusted_package(self):
        from host import lagomorph_hfs_linux as worker
        builds = self.tmp / 'Builds'
        builds.mkdir(exist_ok=True)
        package = builds / 'hfsutils.deb'
        package.write_bytes(b'tampered')
        job = builds / 'job.json'
        job.write_text(json.dumps({'schema': 'cinterforge.lagomorph.hfs-linux.v1',
                                   'package': 'hfsutils.deb', 'package_sha256': '0'*64,
                                   'images': [{'image': 'output.img'}]}))
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            worker.assemble(builds, job)

    def test_linux_hfs_extracts_package_on_container_local_filesystem(self):
        from host import lagomorph_hfs_linux as worker
        builds = self.tmp / 'Builds'
        builds.mkdir(exist_ok=True)
        package = builds / 'hfsutils.deb'
        package.write_bytes(b'pinned-fixture')
        job = builds / 'job.json'
        job.write_text(json.dumps({
            'schema': 'cinterforge.lagomorph.hfs-linux.v1',
            'package': 'hfsutils.deb', 'package_sha256': worker._sha(package),
            'working': 'scratch', 'images': [{'image': 'output.img'}],
        }))
        observed = []

        def extract(*args, home, **kwargs):
            observed.append((args, home))
            self.assertEqual(args[:2], ('dpkg-deb', '-x'))
            destination = Path(args[3])
            self.assertEqual(destination.parent, Path('/tmp'))
            self.assertFalse(destination.is_relative_to(builds))
            for name in worker.TOOLS:
                binary = destination / 'usr/bin' / name
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text('fixture')
                binary.chmod(0o755)

        with mock.patch.object(worker, '_run', side_effect=extract), \
             mock.patch.object(worker, '_assemble_images') as images:
            worker.assemble(builds, job)
            images.assert_called_once()
            self.assertEqual(set(images.call_args.args[2]), set(worker.TOOLS))
        self.assertEqual(len(observed), 1)
        self.assertFalse(Path(observed[0][0][3]).exists())

    def test_public_publish_rejects_old_hfs_plus_hybrid_even_with_matching_hash(self):
        from host import universal_package
        builds = self.tmp / 'Builds'
        folder = builds / self.lag.RELATIVE_STAGE
        folder.mkdir(parents=True)
        disk = folder / self.lag.IMAGE_NAME
        disk.write_bytes(b'\0' * 1024 + b'H+' + b'\0' * 40000)
        manifest = {'schema': self.lag.SCHEMA, 'image': disk.name,
                    'image_size': disk.stat().st_size,
                    'image_sha256': self.lag.sha256(disk)}
        (folder / self.lag.MANIFEST_NAME).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, r'older incompatible HFS\+'):
            universal_package._lagomorph_public_disk(builds)
        manifest.update(filesystem='HFS Standard', image_format='raw-HFS-volume')
        (folder / self.lag.MANIFEST_NAME).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, 'not classic HFS'):
            universal_package._lagomorph_public_disk(builds)

class LagomorphFloppyAndEmbedTests(unittest.TestCase):
    def test_floppy_manifest_requires_exact_hfs_images_and_hashes(self):
        from host import lagomorph_setup as lag
        from host import universal_package as release
        with tempfile.TemporaryDirectory() as root:
            builds = Path(root) / 'Builds'
            stage = builds / lag.RELATIVE_STAGE
            stage.mkdir(parents=True)
            images = {}
            for name in (lag.APPLICATION_FLOPPY, lag.BATTLES_FLOPPY):
                image = stage / name
                with image.open('wb') as stream:
                    stream.truncate(lag.FLOPPY_BYTES)
                with image.open('r+b') as stream:
                    stream.seek(1024)
                    stream.write(b'BD')
                images[name] = {'bytes': image.stat().st_size, 'sha256': lag.sha256(image)}
            (stage / lag.MANIFEST_NAME).write_text(json.dumps({'floppies': images}))
            files = release._lagomorph_public_floppies(builds)
            self.assertEqual(set(files), set(images))
            with (stage / lag.BATTLES_FLOPPY).open('r+b') as stream:
                stream.seek(5000)
                stream.write(b'X')
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                release._lagomorph_public_floppies(builds)

    def test_floppies_only_used_after_public_hfs_range_verification(self):
        from webapp import CinterForgeTerminal as server
        from unittest.mock import Mock
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 206
        response.read.return_value = b'BD'
        response.headers = {'Access-Control-Allow-Origin': '*'}
        with mock.patch('urllib.request.urlopen', return_value=response):
            self.assertTrue(server._verified_public_hfs_floppy(
                'https://cinterforge.ucreg.io/release/Lagomorph-Application-1440K.img'))
        self.assertFalse(server._verified_public_hfs_floppy('http://127.0.0.1/private.img'))


class MacGamesAndHiddenLagomorphTests(unittest.TestCase):
    """Catalog and launcher contracts live in the existing Terminal test file."""

    def test_uploaded_library_catalog_has_67_unique_commands(self):
        from pathlib import Path
        import json
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root/'webapp/mac_games.json').read_text())
        self.assertEqual(catalog['schema'], 'cinterforge.terminal.macgames.v1')
        self.assertEqual(len(catalog['games']), 67)
        commands = [x['command'] for x in catalog['games']]
        self.assertEqual(len(set(commands)), 67)
        for game in catalog['games']:
            self.assertRegex(game['command'], r'^[a-z0-9]+$')
            self.assertTrue(game['library_path'].startswith('Library/Games/'))
        self.assertNotIn('lagomorph', commands)



    def test_macgames_catalog_visible_and_authenticated_launch_queue(self):
        from pathlib import Path
        import importlib.util
        from unittest.mock import patch
        root=Path(__file__).resolve().parents[1]
        module_path=root/'webapp/CinterForgeTerminal.py'
        spec=importlib.util.spec_from_file_location('macgames_command_contract',module_path)
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(len(mod._mac_catalog()),67)
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mod.execute(['infinite-mac']),0)
        self.assertEqual(len(output.getvalue().strip().splitlines()),67)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mod.execute(['help','infinite-mac']),0)
        self.assertIn('https://infinitemac.org/',output.getvalue())
        with patch.object(mod,'_api',return_value={'requested':True}) as api:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(mod.execute(['simcity']),0)
            api.assert_called_once_with('/api/macgames/open',body={'command':'simcity'})
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mod.execute(['help']),0)
        self.assertIn('67 classic Macintosh',output.getvalue())
        self.assertIn('16 IF',output.getvalue())
        self.assertIn('help infinite-mac',output.getvalue())
        self.assertIn('https://infinitemac.org/',output.getvalue())

class WarGamesLaunchTests(unittest.TestCase):
    """JOSHUA is a typed game, not a special terminal mode."""

    def test_first_game_of_exact_68_is_unchanged_joshua(self):
        from webapp import CinterForgeTerminal as cli
        catalog = cli._game_catalog()
        self.assertEqual(len(catalog), 68)
        self.assertEqual(next(iter(catalog)), 'wargames')
        self.assertEqual(catalog['wargames']['kind'], 'joshua')
        self.assertEqual(len(cli._cli_mac_catalog()), 67)
        self.assertEqual(len(cli._if_catalog()), 16)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.execute(['games']), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 68)
        self.assertIn('1. wargames', lines[0])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.execute(['stories']), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 16)

    def test_typed_wargames_requests_existing_session_not_mac_emulator(self):
        from webapp import CinterForgeTerminal as cli
        with mock.patch.object(cli, '_api', return_value={'requested': True}) as api:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.execute(['wargames']), 0)
            api.assert_called_once_with('/api/wargames/open', body={})
            self.assertIn('JOSHUA', output.getvalue())
        with mock.patch.object(cli, '_api') as api:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.execute(['wargames', '--mode']), 2)
            api.assert_not_called()

    def test_wargames_open_and_poll_are_authenticated_and_single_use(self):
        from webapp import CinterForgeTerminal as server
        from types import SimpleNamespace
        state = SimpleNamespace(server_port=17891, token='session-token',
                                origin='http://127.0.0.1:17891',
                                debug_lock=threading.Lock(), wargames_requests=0)
        handler = object.__new__(server.Handler)
        handler.server = state
        handler.headers = {'Host':'127.0.0.1:17891',
                           'X-CPH-Token':'session-token', 'Content-Length':'2'}
        handler.rfile = io.BytesIO(b'{}')
        handler._serve_artifact = lambda path: False
        sent = []
        handler._json = lambda status, body: sent.append((status, body))
        handler.path = '/api/wargames/open'
        handler.do_POST()
        self.assertEqual(sent.pop(), (200, {'requested': True}))
        self.assertEqual(state.wargames_requests, 1)
        handler.path = '/api/wargames/next'
        handler.do_GET()
        self.assertEqual(sent.pop(), (200, {'open': True}))
        handler.do_GET()
        self.assertEqual(sent.pop(), (200, {'open': False}))
        handler.headers['X-CPH-Token'] = 'wrong-token'
        handler.path = '/api/wargames/open'
        handler.rfile = io.BytesIO(b'{}')
        handler.do_POST()
        self.assertEqual(sent.pop()[0], 403)
        self.assertEqual(state.wargames_requests, 0)


class ExternalIFTitleTests(unittest.TestCase):
    """User-initiated external downloads are not staged product assets."""

    def test_manifest_is_exact_16_and_zork_command_names(self):
        from webapp import CinterForgeTerminal as cli
        catalog = cli._if_catalog()
        self.assertEqual(len(catalog), 16)
        for name in ('Advent', 'HugeCave', '404-Life_not_found', 'zork1', 'zork2', 'zork3'):
            self.assertIn(name, catalog)
        self.assertEqual(catalog['zork1']['url'],
                         'https://raw.githubusercontent.com/historicalsource/zork1/master/COMPILED/zork1.z3')


    def test_library_override_is_used_without_home_fallback(self):
        from webapp import CinterForgeTerminal as cli
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            steam = Path(directory) / 'Other Disk' / 'steamapps'
            steam.mkdir(parents=True)
            with mock.patch.dict(os.environ, {'CINTER_STEAMAPPS': str(steam)}):
                self.assertEqual(cli._if_root(), steam / 'common/CinterForge/InteractiveFiction/Titles')

    def test_mocked_story_download_to_nonstandard_steamapps(self):
        from webapp import CinterForgeTerminal as cli
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as directory:
            steam = Path(directory) / 'ExternalLibrary' / 'steamapps'
            steam.mkdir(parents=True)
            class Response(io.BytesIO):
                status = 200
                def geturl(self): return 'https://ifarchive.org/if-archive/games/zcode/Advent.z5'
            data = b'\x05' + bytes(127)
            with mock.patch.dict(os.environ, {'CINTER_STEAMAPPS': str(steam)}), \
                 mock.patch('urllib.request.urlopen', return_value=Response(data)) as fetch:
                self.assertEqual(cli._if_command('Advent', 'install', cli._if_catalog()['Advent']), 0)
                self.assertEqual(cli._if_path('Advent', cli._if_catalog()['Advent']).read_bytes(), data)
                self.assertEqual(cli._if_command('Advent', 'install', cli._if_catalog()['Advent']), 0)
                fetch.assert_called_once()

    def test_missing_story_start_does_not_download(self):
        from webapp import CinterForgeTerminal as cli
        with mock.patch.object(cli, '_if_path', return_value=Path('/nonexistent/cinter-if-game.z3')):
            self.assertEqual(cli._if_command('zork1', 'start', cli._if_catalog()['zork1']), 1)

class IFSteamAutosaveTests(unittest.TestCase):
    """Existing IFPlayer Quetzal/CONV data, persisted outside signed app."""

    def test_host_autosave_roundtrip_previous_and_story_identity(self):
        from webapp import CinterForgeTerminal as service
        with tempfile.TemporaryDirectory() as scratch:
            root=Path(scratch)
            steam=root/'Other Disk/steamapps'
            story=steam/'common/CinterForge/InteractiveFiction/Titles/zork1/zork1.z3'
            story.parent.mkdir(parents=True)
            story.write_bytes(b'\x03'+bytes(127))
            class Server: pass
            class Bedrock: pass
            server=Server();server.bedrock=Bedrock();server.bedrock.steamapps=steam
            checksum=service.hashlib.sha256(story.read_bytes()).hexdigest()
            self.assertFalse(service._if_load_save(server,'zork1')['found'])
            card={'schema':'cinterforge.conv.if.v1','card_type':'CONV','story':'zork1',
                  'story_sha256':checksum,'vm_state':{
                      'schema':'cinterforge.if.quetzal.v1','data_base64':'QUJD'},
                  'session_state':{'transcript':'West of House'}}
            service._if_store_save(server,'zork1',card)
            self.assertEqual(service._if_load_save(server,'zork1')['save']['session_state']['transcript'],
                             'West of House')
            next_card=dict(card,session_state={'transcript':'In the kitchen'})
            service._if_store_save(server,'zork1',next_card)
            path=service._if_save_path(server,'zork1')
            self.assertIn('In the kitchen',path.read_text())
            self.assertIn('West of House',path.with_name('autosave.previous.conv.json').read_text())
            story.write_bytes(b'\x03'+bytes(126)+b'!')
            with self.assertRaisesRegex(ValueError,'incompatible'):
                service._if_load_save(server,'zork1')
            with self.assertRaisesRegex(ValueError,'fingerprint'):
                service._if_store_save(server,'zork1',card)


    def test_live_authenticated_save_api(self):
        from webapp import CinterForgeTerminal as service
        from urllib.request import Request, urlopen
        with tempfile.TemporaryDirectory() as scratch:
            root=Path(scratch)
            builds=root/'Builds';builds.mkdir()
            steam=root/'AlternateSteam/steamapps';steam.mkdir(parents=True)
            story=steam/'common/CinterForge/InteractiveFiction/Titles/zork1/zork1.z3'
            story.parent.mkdir(parents=True);story.write_bytes(b'\x03'+bytes(127))
            with mock.patch.dict(os.environ,{'CINTER_STEAMAPPS':str(steam)}):
                instance=service.CinterShellServer(builds)
            worker=threading.Thread(target=instance.serve_forever,daemon=True);worker.start()
            def request(method='GET',payload=None,authorized=True):
                req=Request(instance.origin+'/api/if/save/zork1',
                    data=json.dumps(payload).encode() if payload else None,
                    headers={'X-CPH-Token':instance.token if authorized else '',
                             'Content-Type':'application/json'},method=method)
                with urlopen(req,timeout=4) as response:return json.loads(response.read())
            try:
                first=request()
                self.assertFalse(first['found'])
                card={'schema':'cinterforge.conv.if.v1','card_type':'CONV','story':'zork1',
                      'story_sha256':first['story_sha256'],
                      'vm_state':{'schema':'cinterforge.if.quetzal.v1','data_base64':'QUJD'}}
                self.assertTrue(request('POST',card)['saved'])
                self.assertTrue(request()['found'])
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as ctx:request(authorized=False)
                self.assertEqual(ctx.exception.code,403)
            finally:
                instance.stop();worker.join(timeout=3)

class ZMachineModuleServingTests(unittest.TestCase):
    """Browser module imports must resolve against compiled upstream JS."""

    def test_live_extensionless_es_module_imports(self):
        from webapp import CinterForgeTerminal as service
        from urllib.request import urlopen
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as scratch:
            builds=Path(scratch)/'Builds'
            root=builds/'CinterForgeTerminal/Dependencies/ZMachine/0.3.2/dist'
            (root/'core').mkdir(parents=True)
            (root/'index.js').write_text("export { ZMachine } from './core/ZMachine';\n")
            (root/'core/ZMachine.js').write_text('export class ZMachine {}\n')
            (root/'core/index.js').write_text("export * from './ZMachine';\n")
            steam=Path(scratch)/'Custom/steamapps';steam.mkdir(parents=True)
            with mock.patch.dict(os.environ,{'CINTER_STEAMAPPS':str(steam)}):
                instance=service.CinterShellServer(builds)
            thread=threading.Thread(target=instance.serve_forever,daemon=True)
            thread.start()
            try:
                for route,expected in (
                    ('/zmachine/index.js',b"export { ZMachine }"),
                    ('/zmachine/core/ZMachine',b'export class ZMachine'),
                    ('/zmachine/core/ZMachine.js',b'export class ZMachine'),
                    ('/zmachine/core',b"export * from './ZMachine.js'"),
                ):
                    with urlopen(instance.origin+route,timeout=4) as response:
                        self.assertEqual(response.status,200,route)
                        self.assertIn(expected,response.read(),route)
                        self.assertIn('javascript',response.headers['Content-Type'])
                with self.assertRaises(HTTPError) as error:
                    urlopen(instance.origin+'/zmachine/core/Absent',timeout=4)
                self.assertEqual(error.exception.code,404)
                with self.assertRaises(HTTPError) as error:
                    urlopen(instance.origin+'/zmachine/%2e%2e/server.py',timeout=4)
                self.assertEqual(error.exception.code,400)
            finally:
                instance.stop();thread.join(timeout=3)

