"""Focused bedrock regression contracts. Native target acceptance is separate."""
from tests._support import *

class BedrockWorldDiscoveryCommandTests(unittest.TestCase):
    """Real directory fixtures; no live download or Minecraft world mutation."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.builds = self.base / 'Builds'
        self.builds.mkdir()
        self.root = self.base / 'minecraftWorlds'
        self.root.mkdir()
        self.original = self.root / 'worldID123'
        (self.original / 'db').mkdir(parents=True)
        (self.original / 'level.dat').write_bytes(b'BEDROCK TEST DATA')
        (self.original / 'levelname.txt').write_text('My Education World\n', encoding='utf-8')
        self.manager = BedrockManager(self.builds, 'mac')

    def test_configured_root_discovers_converter_style_world(self):
        listed = self.manager.set_world_path(str(self.root))
        self.assertEqual([x['name'] for x in listed['worlds']], ['My Education World'])
        self.assertEqual(listed['worlds'][0]['path'], str(self.original.resolve()))
        self.assertEqual(self.manager._world_settings()['root'], str(self.root.resolve()))

    def test_path_change_rejects_nonexistent_and_running(self):
        with self.assertRaises(ValueError):
            self.manager.set_world_path(str(self.base / 'missing'))
        class Live:
            pid = 4242
            def poll(self): return None
        self.manager._process = Live()
        with self.assertRaises(RuntimeError):
            self.manager.set_world_path(str(self.root))

    def test_run_uses_readonly_source_copy_and_reports_port(self):
        from unittest.mock import patch
        self.manager.set_world_path(str(self.root))
        package = self.manager.package
        package.mkdir(parents=True)
        (package / 'receipt.json').write_text('{}')
        (package / 'bedrock_server').write_bytes(b'test')
        self.manager.accept(True)
        (package / 'server.properties').write_text('server-name=Test\nlevel-name=OLD\nserver-port=19135\n')
        with patch.object(self.manager, 'start', side_effect=lambda: self.manager.status()):
            result = self.manager.run_world('My Education World')
        self.assertEqual(result['source_world'], str(self.original))
        active = Path(result['active_world'])
        self.assertTrue((active / 'db').is_dir())
        self.assertEqual((active / 'level.dat').read_bytes(), b'BEDROCK TEST DATA')
        self.assertEqual((self.original / 'levelname.txt').read_text(), 'My Education World\n')
        self.assertEqual(result['server_configuration']['server-port'], '19135')
        self.assertEqual(result['server_configuration']['level-name'], active.name)
        with patch.object(self.manager, 'start', side_effect=lambda: self.manager.status()):
            self.assertEqual(self.manager.run_world('worldID123')['active_world'], str(active))

    def test_duplicate_display_name_is_not_selected_arbitrarily(self):
        other = self.root / 'otherID'
        (other / 'db').mkdir(parents=True)
        (other / 'level.dat').write_bytes(b'other')
        (other / 'levelname.txt').write_text('My Education World')
        self.manager.set_world_path(str(self.root))
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.manager.run_world('My Education World')

    def test_unsafe_symlinked_source_is_rejected(self):
        if not hasattr(os, 'symlink'):
            self.skipTest('no symlink support')
        (self.original / 'db' / 'outside').symlink_to(self.base)
        self.manager.set_world_path(str(self.root))
        self.manager.package.mkdir(parents=True)
        (self.manager.package / 'receipt.json').write_text('{}')
        (self.manager.package / 'bedrock_server').write_bytes(b'test')
        self.manager.accept(True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.manager.run_world('worldID123')

    def test_default_roots_cover_macos_windows_and_linux(self):
        from unittest.mock import patch
        home = self.base / 'login'
        mac = home / 'Library/Application Support/minecraftpe/games/com.mojang/minecraftWorlds'
        mac.mkdir(parents=True)
        with patch.object(self.manager, '_login_home', return_value=home):
            self.assertIn(mac.resolve(), self.manager._default_world_roots())
        linux = BedrockManager(self.builds, 'linux')
        linux_path = home / '.local/share/minecraftpe/games/com.mojang/minecraftWorlds'
        linux_path.mkdir(parents=True)
        with patch.object(linux, '_login_home', return_value=home):
            self.assertIn(linux_path.resolve(), linux._default_world_roots())
        windows = BedrockManager(self.builds, 'win')
        win_path = home / 'AppData/Roaming/Minecraft Education Edition/games/com.mojang/minecraftWorlds'
        win_path.mkdir(parents=True)
        with patch.object(windows, '_login_home', return_value=home), patch.dict(os.environ, {'APPDATA': str(home / 'AppData/Roaming'), 'LOCALAPPDATA': str(home / 'AppData/Local')}):
            self.assertIn(win_path.resolve(), windows._default_world_roots())

    def test_command_start_only_lists_run_calls_api(self):
        from unittest.mock import patch
        from webapp import CinterForgeTerminal as command_cli
        answers = []
        def fake_api(route, *, body=None):
            answers.append((route, body))
            if route == '/api/bedrock/worlds':
                return {'roots':[str(self.root)],'worlds':[{'name':'My Education World','folder':'worldID123','path':str(self.original)}]}
            return {'world_name':'My Education World','source_world':str(self.original),
                    'active_world':str(self.base / 'worlds/test'),
                    'server_configuration':{'server-port':'19132'}}
        with patch.object(command_cli, '_api', side_effect=fake_api), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_cli.execute(['bedrock','start']), 0)
            self.assertEqual(command_cli.execute(['bedrock','run','My Education World']), 0)
        self.assertEqual([v[0] for v in answers], ['/api/bedrock/worlds', '/api/bedrock/lan', '/api/bedrock/run'])
        self.assertIn('Configured UDP IPv4: 19132', out.getvalue())

class BedrockUnrestrictedNetworkingContractTests(unittest.TestCase):
    def test_only_unrestricted_guest_nic_and_entitlement(self):
        root=Path(__file__).resolve().parents[1]/'linuxos'
        swift=(root/'CinterBDSVM.swift').read_text()
        entitlement=(root/'CinterBDSVM.entitlements').read_text()
        self.assertIn('VZNATNetworkDeviceAttachment()', swift)
        self.assertNotIn('VZBridgedNetworkDeviceAttachment', swift)
        self.assertNotIn('CinterRawEthernetLink', swift)
        self.assertNotIn('VZFileHandleNetworkDeviceAttachment', swift)
        self.assertNotIn('<key>com.apple.vm.networking</key>', entitlement)
        self.assertIn('<key>com.apple.security.virtualization</key>', entitlement)

    def test_no_transient_settings_or_port_mappings(self):
        from linuxos.craftmaster.craftmaster_mac_bootstrap import RUN_SH
        self.assertIn('CINTER_BDS_NETWORK host-relay', RUN_SH)
        self.assertNotIn('server-udp-ports',RUN_SH)
        self.assertNotIn('PROPS_BACKUP',RUN_SH)
        self.assertNotIn('set_prop allow-list false',RUN_SH)

class BedrockLanRelayProvisioningTests(unittest.TestCase):
    def test_no_root_daemon_required_by_runtime(self):
        from linuxos import launcher_signing
        source=(Path(__file__).resolve().parents[1]/'linuxos/lifecycle.py').read_text()
        self.assertNotIn('from vmnet_service import ensure', source)
        self.assertNotIn(b'com.apple.vm.networking', launcher_signing.ENTITLEMENTS.read_bytes())
        mac=(Path(__file__).resolve().parents[1]/'host/mac_app.py').read_text()
        self.assertIn('sign_launcher(vm)', mac)
        self.assertIn('if not has_virtualization_entitlement(vm)', mac)
        self.assertIn("'--preserve-metadata=entitlements'",mac)

    def test_lan_relay_lifecycle_and_no_settings_restore(self):
        from linuxos.craftmaster.craftmaster_mac_bootstrap import RUN_SH
        for forbidden in ('server-udp-ports', 'PROPS_BACKUP', 'set_prop', 'CINTER_VMNET_SOCKET'):
            self.assertNotIn(forbidden, RUN_SH)
        self.assertIn('CinterBDSNetRelay', RUN_SH)
        self.assertIn('trap cleanup EXIT INT TERM', RUN_SH)
        swift=(Path(__file__).resolve().parents[1]/'linuxos/CinterBDSVM.swift').read_text()
        self.assertIn('VZNATNetworkDeviceAttachment',swift)
        self.assertNotIn('VZBridgedNetworkDeviceAttachment',swift)

class BedrockRelayDistributionContractTests(unittest.TestCase):
    def test_setup_builds_relay_and_distribution_preserves_it(self):
        root=Path(__file__).resolve().parents[1]
        lifecycle=(root/'linuxos/lifecycle.py').read_text()
        packaging=(root/'host/mac_app.py').read_text()
        relay_source=(root/'linuxos/craftmaster/craftmaster_mac_bootstrap.py').read_text()
        self.assertIn('staged Bedrock LAN relay',lifecycle)
        self.assertIn('CinterBDSNetRelay',lifecycle)
        self.assertIn('LAN_RELAY_C',relay_source)
        self.assertIn('19132,19133,19140',relay_source)
        self.assertNotIn("'CinterBDSNetRelay').unlink",packaging)
        self.assertNotIn('VZBridgedNetworkDeviceAttachment',(root/'linuxos/CinterBDSVM.swift').read_text())

class BedrockNetworkGuestFingerprintRegressionTests(unittest.TestCase):
    def test_host_swift_edit_does_not_invalidate_existing_linuxkit_guest(self):
        """Legacy receipt includes Swift; bridging changes only the Mac launcher."""
        import hashlib
        import importlib.util
        import tempfile
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        path = root / 'linuxos' / 'build_linux.py'
        import sys
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location('bridge_guest_fingerprint_regression', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch)
            authored = base / 'source'
            staged = base / 'stage'
            authored.mkdir()
            staged.mkdir()
            (authored / 'linuxkit.yml').write_text('kernel: unchanged\n')
            (authored / 'CinterBDSVM.swift').write_text('new bridge launcher')
            (staged / 'linuxkit.yml').write_text('kernel: unchanged\n')
            (staged / 'CinterBDSVM.swift').write_text('old NAT launcher')
            digest = hashlib.sha256()
            for name in module.SOURCES:
                digest.update(name.encode())
                digest.update((staged / name).read_bytes())
            digest.update((staged / 'linuxkit.yml').read_bytes())
            digest.update(b'linuxkit-bedrock-rosetta-appliance-v1')
            (staged / 'built-source-fingerprint.txt').write_text(digest.hexdigest() + '\n')
            (staged / 'cinter-bds-kernel').write_bytes(b'\0' * 0x38 + b'ARM\x64')
            (staged / 'cinter-bds-initrd.img').write_bytes(b'initrd')
            (staged / 'cinter-bds-cmdline').write_text('console=ttyS0')
            old_here = module.HERE
            old_root = module.linuxkit_bootstrap_root
            try:
                module.HERE = authored
                module.linuxkit_bootstrap_root = lambda *_args, **_kw: staged
                self.assertEqual(module.verify_shared_bootstrap(base, require_launcher=False), staged)
                (authored / 'linuxkit.yml').write_text('kernel: changed\n')
                with self.assertRaisesRegex(RuntimeError, 'guest is stale'):
                    module.verify_shared_bootstrap(base, require_launcher=False)
            finally:
                module.HERE = old_here
                module.linuxkit_bootstrap_root = old_root

class MinecraftConsoleInteractiveModeRegressionTests(unittest.TestCase):
    def test_console_runs_only_if_server_running(self):
        from webapp import CinterForgeTerminal as cli
        with mock.patch.object(cli, '_api', return_value={'running': False}) as api:
            self.assertEqual(cli.minecraft_console(input_fn=lambda _: 'list'), 1)
            api.assert_called_once_with('/api/bedrock/status')

    def test_console_forwards_commands_until_exit_without_stopping_server(self):
        from webapp import CinterForgeTerminal as cli
        iterator = iter(['list', '/say hello world', 'exit'])
        calls = []
        def api(route, *, body=None):
            calls.append((route, body))
            return {'running': True} if route == '/api/bedrock/status' else {'sent': True}
        with mock.patch.object(cli, '_api', side_effect=api):
            self.assertEqual(cli.minecraft_console(input_fn=lambda _: next(iterator)), 0)
        self.assertEqual(calls, [('/api/bedrock/status', None),
                                 ('/api/bedrock/command', {'command': 'list'}),
                                 ('/api/bedrock/command', {'command': 'say hello world'})])

    def test_minecraft_command_installed_in_existing_shell(self):
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        server=(root/'webapp/CinterForgeTerminal.py').read_text()
        self.assertIn("'bedrock', 'minecraft', 'lagomorph'", server)

    def test_ansi_erase_in_line_respects_cursor(self):
        from pathlib import Path
        js=(Path(__file__).resolve().parents[1]/'webapp/terminal.js').read_text()
        self.assertIn("else this.lines[index]=line.slice(0,this.current)",js)
        self.assertIn("if(mode===1) this.lines[index]",js)
        self.assertIn("else if(mode===2) this.lines[index]=''",js)

class BedrockLanRelayLaunchRecoveryTests(unittest.TestCase):
    def test_host_relay_preserves_player_settings(self):
        from linuxos.craftmaster.craftmaster_mac_bootstrap import RUN_SH
        self.assertIn('CINTER_BDS_NETWORK host-relay',RUN_SH)
        self.assertNotIn('selected=nat',RUN_SH)
        self.assertNotIn('PROPS_BACKUP',RUN_SH)
        self.assertNotIn('set_prop',RUN_SH)

    def test_waits_for_server_readiness_not_vm_pid(self):
        source=(Path(__file__).resolve().parents[1]/'webapp/CinterForgeTerminal.py').read_text()
        self.assertIn("b'Server started.' in output",source)
        self.assertIn('self._launch_offset',source)
        self.assertIn('def console_output(',source)
        self.assertIn('def _refresh_readiness_locked(',source)
        self.assertNotIn('deadline = time.monotonic() + 70.0',source)

    def test_nonblocking_start_reports_ready_only_after_new_log_marker(self):
        from webapp.CinterForgeTerminal import BedrockManager
        from unittest.mock import patch
        import io as memory_io
        with tempfile.TemporaryDirectory() as tmp:
            builds=Path(tmp)
            manager=BedrockManager(builds,'linux')
            manager.accept(True)
            manager.package.mkdir(parents=True)
            (manager.package/'receipt.json').write_text('{}')
            (manager.package/'bedrock_server').write_bytes(b'executable')
            (manager.package/'server.properties').write_text('server-name=Test\n')
            log=builds/'Logs/Bedrock/CinterForgeTerminal.log'
            log.parent.mkdir(parents=True)
            log.write_bytes(b'old boot: Server started.\n')
            mockprocess=mock.Mock()
            mockprocess.pid=4096
            mockprocess.poll.return_value=None
            mockprocess.wait.side_effect=subprocess.TimeoutExpired('fixture', 0.8)
            mockprocess.stdin=memory_io.BytesIO()
            with patch.object(manager,'_run_command',return_value=(['fixture'],{})), \
                 patch('webapp.CinterForgeTerminal.subprocess.Popen',return_value=mockprocess):
                first=manager.start()
                self.assertTrue(first['starting'])
                self.assertTrue(first['running'])  # VM process is alive; readiness is separate.
                self.assertFalse(first['server_ready'])
                self.assertEqual(first['pid'],4096)
                self.assertTrue(manager.console_command('list')['sent'])
                with log.open('ab') as stream:
                    stream.write(b'CINTER_VM_STARTED\nServer started.\n')
                second=manager.status()
                self.assertTrue(second['running'])
                self.assertFalse(second['starting'])
                self.assertTrue(manager.console_command('list')['sent'])
                self.assertEqual(mockprocess.stdin.getvalue(),b'list\nlist\n')
                mockprocess.poll.return_value=1
                mockprocess.wait.side_effect=None
                mockprocess.wait.return_value=1
                manager.stop()

    def test_console_command_persists_on_active_process(self):
        from webapp.CinterForgeTerminal import BedrockManager
        with tempfile.TemporaryDirectory() as tmp:
            manager=BedrockManager(Path(tmp),'mac')
            fake=subprocess.Popen(['/bin/cat'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            try:
                manager._process=fake
                manager._ready=True
                self.assertTrue(manager.status()['console_available'])
                self.assertTrue(manager.console_command('op TestPlayer')['sent'])
                with self.assertRaisesRegex(ValueError,'Minecraft console command'):
                    manager.console_command('op A\nstop')
            finally:
                fake.terminate();fake.wait()
                manager._process=None

class BedrockCurrentStageRegressionTests(unittest.TestCase):
    """Prevent the stage failures observed on HOPE-DEV with managed Colima HOME."""

    def test_lifecycle_uses_no_vmnet_stage_import(self):
        source=Path(__file__).resolve().parents[1]
        script=(source / 'linuxos/lifecycle.py').read_text(encoding='utf-8')
        self.assertNotIn('from vmnet_service import ensure',script)
        self.assertIn('CinterBDSNetRelay',script)

    def test_mac_steam_staging_uses_login_home_not_managed_colima_home(self):
        import types
        from unittest import mock
        from host.universal_package import steamapps_root
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam = root / 'login/Library/Application Support/Steam/steamapps'
            steam.mkdir(parents=True)
            (root / 'Builds/CFTVM/home').mkdir(parents=True)
            with mock.patch.dict(os.environ, {'HOME': str(root / 'Builds/CFTVM/home')}, clear=True), \
                 mock.patch('platform.system', return_value='Darwin'), \
                 mock.patch('pwd.getpwuid', return_value=types.SimpleNamespace(pw_dir=str(root / 'login'))):
                self.assertEqual(steamapps_root(), steam.resolve())

    def test_bedrock_refuses_stale_relay_before_vm_start(self):
        from webapp.CinterForgeTerminal import BedrockManager
        with tempfile.TemporaryDirectory() as tmp:
            builds = Path(tmp)
            manager = BedrockManager(builds, 'mac')
            guest = builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'
            required = ('run-mac-bds.sh', 'CinterBDSVM', 'CinterBDSNetRelay', 'cinter-bds-kernel',
                        'cinter-bds-initrd.img', 'cinter-bds-cmdline',
                        'GuestRuntime/glibc-x86_64/ld-linux-x86-64.so.2',
                        'GuestRuntime/cinter-nethernet-probe')
            for filename in required:
                path = guest / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#!/bin/sh\necho old-bridged-launcher\n')
            with self.assertRaisesRegex(RuntimeError, 'stale.*missing LAN relay'):
                manager._run_command()
            self.assertIsNone(manager._process)

    def test_bedrock_lan_relay_preflight_does_not_launch_or_edit_world(self):
        from webapp import CinterForgeTerminal as bedrock
        with tempfile.TemporaryDirectory() as tmp:
            builds=Path(tmp)
            manager=bedrock.BedrockManager(builds,'mac')
            guest=builds/'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'
            for filename in ('CinterBDSVM','cinter-bds-kernel','cinter-bds-initrd.img',
                             'cinter-bds-cmdline','GuestRuntime/glibc-x86_64/ld-linux-x86-64.so.2',
                             'GuestRuntime/cinter-nethernet-probe'):
                item=guest/filename;item.parent.mkdir(parents=True,exist_ok=True);item.write_text('payload')
            from linuxos.craftmaster.craftmaster_mac_bootstrap import RUN_SH
            (guest/'run-mac-bds.sh').write_text(RUN_SH)
            (guest/'CinterBDSNetRelay').write_text('relay')
            manager.run_root.mkdir(parents=True)
            (manager.run_root/'bedrock_server').write_text('binary')
            properties=manager.run_root/'server.properties'
            original=b'allow-list=true\nserver-name=My Server\n';properties.write_bytes(original)
            command,env=manager._run_command()
            self.assertEqual(command,['/bin/sh',str(guest/'run-mac-bds.sh'),str(manager.run_root)])
            self.assertEqual(env,{})
            self.assertEqual(properties.read_bytes(),original)
            self.assertIsNone(manager._process)


# Migrated from tests/test_bedrock_reattach.py.
"""Focused regression: attach to a live Bedrock VM after the API restarts.

Only local processes and temporary worlds; no Minecraft download, networking,
VM start or EULA bypass. Execute: python -m unittest tests.test_bedrock_reattach -v
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from webapp.CinterForgeTerminal import BedrockManager


class ReconnectTests(unittest.TestCase):
    def test_existing_mac_vm_is_detected_without_relaunch(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = BedrockManager(Path(temp), 'mac')
            script = '/bin/sh /opt/guest/run-mac-bds.sh ' + str(manager.run_root)
            def ps(command, **_):
                if '-axo' in command:
                    return subprocess.CompletedProcess(command, 0, ' 4242 ' + script + '\n', '')
                if '-p' in command:
                    return subprocess.CompletedProcess(command, 0, script + '\n', '')
                return subprocess.CompletedProcess(command, 1, '', '')
            with mock.patch('webapp.CinterForgeTerminal.subprocess.run', side_effect=ps):
                state = manager.status()
                self.assertTrue(state['running'])
                self.assertTrue(state['externally_managed'])
                self.assertFalse(state['console_available'])
                with self.assertRaisesRegex(RuntimeError, 'no reconnectable console endpoint'):
                    manager.console_command('list')
                with mock.patch.object(manager, '_prepare_run_tree') as prep:
                    with mock.patch.object(manager, 'accepted', return_value=True), \
                         mock.patch.object(manager, 'installed', return_value=True):
                        self.assertEqual(manager.start()['pid'], 4242)
                        prep.assert_not_called()

    def test_exec_optimized_swift_vm_identified_by_verified_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = BedrockManager(Path(temp), 'mac')
            binary = str(manager.builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64/CinterBDSVM')
            def fake(command, **_):
                if command[0] == 'lsof':
                    return subprocess.CompletedProcess(command, 0, f'p4242\nfcwd\nn{manager.run_root}\n', '')
                if '-axo' in command:
                    return subprocess.CompletedProcess(command, 0, ' 4242 ' + binary + '\n', '')
                return subprocess.CompletedProcess(command, 0, binary + '\n', '')
            with mock.patch('webapp.CinterForgeTerminal.subprocess.run', side_effect=fake):
                state = manager.status()
                self.assertTrue(state['running'])
                self.assertFalse(state['console_available'])
                self.assertEqual(state['pid'], 4242)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'Host FIFO is POSIX-only')
    def test_reopened_manager_sends_commands_through_same_serial_input(self):
        with tempfile.TemporaryDirectory() as temp:
            builds = Path(temp)
            first = BedrockManager(builds, 'mac')
            first.accept(True)
            first.package.mkdir(parents=True)
            (first.package / 'receipt.json').write_text('{}')
            (first.package / 'bedrock_server').write_bytes(b'not executed')
            (first.package / 'server.properties').write_text('server-name=Test\n')
            fixture = ("import sys\n"
                       "print('Server started.',flush=True)\n"
                       "for line in sys.stdin:\n"
                       " print('GOT:'+line.rstrip('\\n'),flush=True)\n"
                       " if line.strip()=='stop': break\n")
            with mock.patch.object(first, '_run_command', return_value=([sys.executable, '-u', '-c', fixture], {})):
                first.start()
            pid = first._process.pid
            second = BedrockManager(builds, 'mac')
            # The fixture stands in for the actual run-mac-bds.sh VM process.
            def valid_pid(value):
                return value == pid and first._process.poll() is None
            with mock.patch.object(second, '_launcher_pid_matches', side_effect=valid_pid):
                state = second.status()
                self.assertTrue(state['running'])
                self.assertTrue(state['console_available'])
                self.assertTrue(state['externally_managed'])
                self.assertEqual(second.console_command('say hello from new API')['pid'], pid)
                log = first._log_path()
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and 'GOT:say hello' not in log.read_text(errors='replace'):
                    time.sleep(.05)
                self.assertIn('GOT:say hello from new API',log.read_text(errors='replace'))
                # Reject commands with embedded newline; never launch another VM.
                with self.assertRaises(ValueError):
                    second.console_command('list\nstop')
                # The public stop path, not console_command, sends the stop command.
                second.stop()
            first._process.wait(timeout=4)
            if first._log_stream:
                first._log_stream.close()
            self.assertIn('GOT:stop', log.read_text(errors='replace'))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'Host FIFO is POSIX-only')
    def test_explicit_restart_replaces_old_vm_and_attaches_live_console(self):
        """Reproduce the real report: world running, but old console lost."""
        with tempfile.TemporaryDirectory() as temp:
            builds = Path(temp)
            manager = BedrockManager(builds, 'mac')
            manager.accept(True)
            manager.package.mkdir(parents=True)
            (manager.package / 'receipt.json').write_text('{}')
            (manager.package / 'bedrock_server').write_bytes(b'fixture')
            (manager.package / 'server.properties').write_text('level-name=existing-test-world\n')
            manager.run_root.mkdir(parents=True, exist_ok=True)
            (manager.run_root / 'server.properties').write_text('level-name=existing-test-world\n')
            old_script = ("import pathlib,sys,time\n"
                          "request=pathlib.Path(sys.argv[1])\n"
                          "print('OLD_VM_RUNNING',flush=True)\n"
                          "while not request.is_file(): time.sleep(.03)\n"
                          "print('OLD_VM_STOP_REQUEST',flush=True)\n")
            old = subprocess.Popen([sys.executable, '-u', '-c', old_script,
                                    str(manager.run_root / '.cinter/vm-stop-request')],
                                   cwd=manager.run_root, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
            try:
                manager._record_launch(old.pid)
                # An old VM has no FIFO. The new manager knows the PID, not its stdin.
                def matching(pid):
                    if pid == old.pid:
                        return old.poll() is None
                    replacement = manager._process
                    return bool(replacement and replacement.pid == pid and replacement.poll() is None)
                fixture = ("import sys\n"
                           "print('Server started.',flush=True)\n"
                           "for line in sys.stdin:\n"
                           " print('NEW_VM_COMMAND:'+line.rstrip('\\n'),flush=True)\n"
                           " if line.strip()=='stop':break\n")
                with mock.patch.object(manager, '_launcher_pid_matches', side_effect=matching), \
                     mock.patch.object(manager, '_run_command', return_value=([sys.executable, '-u', '-c', fixture], {})):
                    self.assertTrue(manager.status()['running'])
                    self.assertFalse(manager.status()['console_available'])
                    state = manager.restart()
                    self.assertEqual(old.wait(timeout=4), 0)
                    self.assertTrue((manager.run_root / '.cinter/vm-stop-request').is_file())
                    self.assertEqual((manager.run_root / 'server.properties').read_text(),
                                     'level-name=existing-test-world\n')
                    self.assertTrue(state['running'])
                    self.assertTrue(state['console_available'])
                    self.assertNotEqual(state['pid'], old.pid)
                    self.assertTrue(manager.console_command('list')['sent'])
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline and ('NEW_VM_COMMAND:list' not in manager._log_path().read_text(errors='replace')):
                        time.sleep(.05)
                    self.assertIn('NEW_VM_COMMAND:list', manager._log_path().read_text(errors='replace'))
                    manager.stop()
            finally:
                if old.poll() is None:
                    old.terminate()
                    old.wait(timeout=5)
                if manager._process and manager._process.poll() is None:
                    manager._process.terminate()
                    manager._process.wait(timeout=5)
                if manager._log_stream:
                    manager._log_stream.close()

    def test_automatic_run_does_not_restart_or_power_off_old_vm(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = BedrockManager(Path(temp), 'mac')
            manager.accept(True)
            with mock.patch.object(manager, '_live_session_locked', return_value=(4422, False, False)):
                with mock.patch.object(manager, '_prepare_run_tree') as prep, \
                     mock.patch.object(manager, '_stop_mac_vm_without_console_locked') as stop:
                    with mock.patch.object(manager, 'installed', return_value=True):
                        self.assertEqual(manager.start()['pid'], 4422)
                    prep.assert_not_called()
                    stop.assert_not_called()

    def test_cli_restart_routes_only_to_explicit_restart_endpoint(self):
        import contextlib
        import io
        from webapp import CinterForgeTerminal as command_cli
        requests = []
        def api(path, *, body=None):
            requests.append((path, body))
            return {'running': True, 'console_available': True}
        with mock.patch.object(command_cli, '_api', side_effect=api):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(command_cli.execute(['bedrock', 'restart']), 0)
        self.assertEqual(requests, [('/api/bedrock/restart', {})])
        self.assertIn('live console', output.getvalue())

    def test_minecraft_refuses_readonly_console_with_recovery_command(self):
        import contextlib
        import io
        from webapp import CinterForgeTerminal as command_cli
        with mock.patch.object(command_cli, '_api', return_value={
                'running': True, 'starting': False, 'console_available': False}):
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(command_cli.minecraft_console(input_fn=lambda _: 'list'), 1)
        self.assertIn('bedrock restart', error.getvalue())

    def test_legacy_shutdown_refuses_unverified_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = BedrockManager(Path(temp), 'mac')
            with mock.patch.object(manager, '_launcher_pid_matches', return_value=False), \
                 mock.patch.object(manager, '_external_vm_pid', return_value=7711):
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    manager._stop_mac_vm_without_console_locked(6622)
                self.assertFalse((manager.run_root / '.cinter/vm-stop-request').exists())



class DedicatedBedrockSessionTests(unittest.TestCase):
    """The visible Session owns a real PTY; no native VM is mocked as passing."""

    def test_bedrock_session_is_two_way_and_logs_original_stream(self):
        if os.name == 'nt':
            self.skipTest('Unix PTY fixture; native Windows is a separate acceptance gate')
        from webapp.CinterForgeTerminal import BedrockTerminalSession
        builds = ROOT.parent / 'Builds'
        scratch = builds / 'Intermediates/CinterForgeTerminalTests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='bedrock-pty-', dir=scratch) as directory:
            root = Path(directory)
            world = root / 'world'
            world.mkdir()
            log = root / 'bedrock.log'
            code = ('import sys\n'
                    'print("BEDROCK SESSION STARTED", flush=True)\n'
                    'for line in sys.stdin:\n'
                    ' print("BEDROCK RECEIVED:"+line.strip(), flush=True)\n'
                    ' if line.strip()=="stop": break\n')
            class FakeManager:
                run_root = world
                _launch_offset = 0
                _process = None
                _terminal_session = None
                _ready = False
                _startup_error = ''
                def _run_command(self): return [sys.executable, '-u', '-c', code], {}
                def _log_path(self): return log
                def stop(self):
                    if self._terminal_session is not None:
                        self._terminal_session.write('stop\n')
                        self._terminal_session.process.wait(timeout=4)
                        self._terminal_session = None
                        self._process = None
            owner = FakeManager()
            terminal = BedrockTerminalSession(owner, builds, 'linux', 80, 24,
                url='http://127.0.0.1:1', token='test-secret')
            try:
                self.assertIs(owner._process, terminal.process)
                def output_until(text):
                    result = b''
                    for _ in range(30):
                        out, closed = terminal.poll(100)
                        result += out
                        if text.encode() in result:
                            return result
                        if closed:
                            break
                    self.fail(f'Missing PTY output {text!r}; saw {result!r}')
                output_until('BEDROCK SESSION STARTED')
                terminal.write('list\n')
                output_until('BEDROCK RECEIVED:list')
                self.assertIn('BEDROCK SESSION STARTED', log.read_text())
            finally:
                terminal.close()
            self.assertIsNotNone(terminal.process.poll())
            self.assertIn('BEDROCK RECEIVED:stop', log.read_text())

    def test_request_creates_new_session_then_reselects_same_running_session(self):
        from collections import deque
        from webapp import CinterForgeTerminal as shell_server
        class FakeTerminal:
            id = 'bedrock-pty-id'
            kind = 'bedrock'
            process = mock.Mock()
            process.poll.return_value = None
        manager = mock.Mock()
        manager.run_world.return_value = {'running': False, 'world_name': 'Test'}
        manager.status.return_value = {'running': True, 'console_available': True}
        manager._world_settings.return_value = {'selected': {'name': 'My World', 'folder': 'id1'}}
        server = SimpleNamespace(bedrock=manager, bedrock_error='',
            bedrock_terminal_lock=threading.Lock(), sessions_lock=threading.Lock(),
            sessions={}, bedrock_terminal_requests=deque(), system='mac',
            mutable_builds=Path('/dummy'), origin='http://127.0.0.1:5555', token='token')
        with mock.patch.object(shell_server, 'BedrockTerminalSession', return_value=FakeTerminal()) as session_class:
            first = shell_server.open_bedrock_terminal(server, 'My World')
            self.assertEqual(first['terminal_session'], FakeTerminal.id)
            self.assertEqual(set(server.sessions), {FakeTerminal.id})
            self.assertEqual(server.bedrock_terminal_requests.popleft()['kind'], 'bedrock')
            second = shell_server.open_bedrock_terminal(server, 'My World')
            self.assertTrue(second['reused'])
            self.assertEqual(second['terminal_session'], first['terminal_session'])
        manager.run_world.assert_called_once_with('My World', launch=False)
        session_class.assert_called_once()
