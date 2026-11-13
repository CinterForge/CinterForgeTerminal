"""Regression tests for persistent CodeOSS and platform Launcher contracts.

Only development-time tests; no guest VM, platform SDK or image is installed.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from webapp import CinterForgeTerminal as terminal

ROOT = Path(__file__).resolve().parents[1]


class LauncherIntegrationTests(unittest.TestCase):
    def test_launchers_have_one_app_and_no_platform_selector_or_mode_badge(self):
        page = (ROOT / 'webapp/launcher/index.html').read_text()
        logic = (ROOT / 'webapp/launcher/app.js').read_text()
        ui = (ROOT / 'webapp/terminal.js').read_text()
        self.assertEqual(page.count('data-app="CinterForgeTerminal"'), 1)
        self.assertNotIn('data-app="Files"', page)
        self.assertNotIn('data-app="Terminal"', page)
        self.assertNotIn('desktop-mode', page + logic)
        self.assertNotIn('class="topbar-mode"', page)
        self.assertIn("new Set(['CinterForgeTerminal'])", ui)
        for kind in ('mac', 'linux', 'windows'):
            self.assertTrue((ROOT / f'webapp/launcher/assets/backgrounds/{kind}.png').is_file())
        self.assertTrue((ROOT / 'webapp/launcher/assets/cinterforge-logo.png').is_file())

    def test_staged_app_lookup_requires_matching_receipt_and_real_entrypoint(self):
        with tempfile.TemporaryDirectory() as temp:
            steam = Path(temp) / 'steamapps'
            steam.mkdir()
            with mock.patch.object(terminal, '_steam_library_for_installed_root', return_value=steam), \
                 mock.patch.object(terminal, '_devtools_release', return_value='2026.11.23'):
                for kind, osfolder, filename in (
                        ('mac', 'macOS', 'CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal'),
                        ('linux', 'Linux', 'CinterForge Terminal/Terminal.sh'),
                        ('win', 'Windows', 'CinterForge Terminal/Terminal.cmd')):
                    root = steam / 'common/CinterForge/Applications' / osfolder / 'CinterForge Terminal/2026.11.23'
                    exe = root / filename
                    with self.assertRaisesRegex(RuntimeError, 'Missing staged'):
                        terminal._launcher_staged_app(kind)
                    exe.parent.mkdir(parents=True, exist_ok=True)
                    exe.write_bytes(b'fixture')
                    receipt = root / 'stage-receipt.json'
                    receipt.write_text(json.dumps({'schema': terminal.FOUR_SCHEMA,
                                                   'target':kind, 'release':'2026.11.23'}))
                    self.assertEqual(terminal._launcher_staged_app(kind), exe)
                    receipt.write_text(json.dumps({'schema': terminal.FOUR_SCHEMA,
                                                   'target':kind, 'release':'bad-version'}))
                    with self.assertRaisesRegex(RuntimeError, 'receipt does not match'):
                        terminal._launcher_staged_app(kind)
                    receipt.write_text(json.dumps({'schema': terminal.FOUR_SCHEMA,
                                                   'target':kind, 'release':'2026.11.23'}))

    def test_linux_launcher_finds_unpacked_stage_receipt_and_nested_legacy_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            steam = Path(temp) / 'steamapps'
            root = (steam / 'common/CinterForge/Applications/Linux/'
                    'CinterForge Terminal/2026.11.23')
            exe = root / 'CinterForge Terminal/Terminal.sh'
            exe.parent.mkdir(parents=True)
            exe.write_text('#!/bin/sh\nexit 0\n')
            current = {'schema': 'cinterforge.terminal.unpacked-build.v1',
                       'target': 'linux', 'release': '2026.11.23',
                       'build_fingerprint': 'b'*64, 'installed_state': 'a'*64}
            with mock.patch.object(terminal, '_steam_library_for_installed_root', return_value=steam), \
                 mock.patch.object(terminal, '_devtools_release', return_value='2026.11.23'):
                (root / 'stage-receipt.json').write_text(json.dumps(current))
                self.assertEqual(terminal._launcher_staged_app('linux'), exe)
                (root / 'stage-receipt.json').unlink()
                (exe.parent / 'stage-receipt.json').write_text(json.dumps(current))
                self.assertEqual(terminal._launcher_staged_app('linux'), exe)
                current['release'] = 'stale'
                (exe.parent / 'stage-receipt.json').write_text(json.dumps(current))
                with self.assertRaisesRegex(RuntimeError, 'receipt does not match'):
                    terminal._launcher_staged_app('linux')

    def test_staged_launch_routes_mac_to_host_and_guests_to_guest_shell(self):
        root = Path('/tmp/CinterForge Test/steamapps')
        def staged(kind):
            return (root / 'common/CinterForge/Applications' / {'mac':'macOS', 'linux':'Linux','win':'Windows'}[kind]
                    / 'CinterForge Terminal/2026.11.23' /
                    ('CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal' if kind=='mac'
                     else 'CinterForge Terminal/' + ('Terminal.sh' if kind=='linux' else 'Terminal.cmd')))
        process = SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(terminal, '_launcher_staged_app', side_effect=staged), \
             mock.patch.object(terminal.platform, 'system', return_value='Darwin'), \
             mock.patch.object(terminal.subprocess, 'run', return_value=process) as open_process:
            result = terminal._launcher_launch('mac', 'CinterForgeTerminal')
        self.assertTrue(result['external'])
        self.assertEqual(open_process.call_args.args[0],
                         ['/usr/bin/open', '-a', str(staged('mac').parents[2])])
        with mock.patch.object(terminal, '_launcher_staged_app', side_effect=staged), \
             mock.patch.object(terminal, '_launcher_start'), \
             mock.patch.object(terminal, '_launcher_container', return_value=('docker', {}, 'owned-guest')), \
             mock.patch.object(terminal, '_docker_call', return_value=process) as docker:
            terminal._launcher_launch('linux', 'CinterForgeTerminal')
            linux = docker.call_args.args
            self.assertIn('Terminal.sh', ' '.join(str(arg) for arg in linux))
            self.assertIn('/bin/sh', linux)
            terminal._launcher_launch('win', 'CinterForgeTerminal')
            windows = docker.call_args.args
            self.assertIn('wine cmd.exe', ' '.join(str(arg) for arg in windows))
            self.assertIn('Terminal.cmd', ' '.join(str(arg) for arg in windows))
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            terminal._launcher_launch('win', 'Files')

    def test_codeoss_is_single_persistent_frame_and_menu_stays_stable(self):
        ui = (ROOT / 'webapp/terminal.js').read_text()
        css = (ROOT / 'webapp/terminal.css').read_text()
        native = (ROOT / 'src/CinterApplicationShell.cpp').read_text()
        self.assertIn("codeFrame.id = 'codeoss-frame'", ui)
        self.assertIn("if (!codeFrame.dataset.loaded)", ui)
        self.assertIn("const current = tabs.find(tab => !tab.closed && tab.profileId === 'codeoss')", ui)
        self.assertIn("codeFrame.hidden = true;", ui)
        self.assertIn(".computer-view iframe[hidden]", css)
        self.assertIn('codeServiceMenus.get(name)', ui)
        self.assertIn('enabled: active && item.enabled !== false', ui)
        self.assertIn('terminalItems().filter(', ui)
        self.assertIn('All modes share the same menu skeleton', native)
        self.assertIn('Unsupported leaves are disabled by code-menu-model', native)
        self.assertIn('Mac Launcher', ui)
        self.assertIn('Windows Launcher', ui)
        self.assertIn('Linux Launcher', ui)

    def test_codeoss_real_terminal_menu_contributes_three_launcher_commands(self):
        pkg = json.loads((ROOT / 'webapp/codeoss/extension/package.json').read_text())
        extension = (ROOT / 'webapp/codeoss/extension/extension.js').read_text()
        producer = (ROOT / 'webapp/codeoss/producer.py').read_text()
        for target in ('Mac', 'Linux', 'Windows'):
            name = 'cinterforge.open' + target + 'Launcher'
            self.assertIn(name, {item['command'] for item in pkg['contributes']['commands']})
            self.assertIn(name, extension)
            self.assertIn(name, producer)
        self.assertNotIn('menubar/terminal', pkg['contributes']['menus'])

    def test_codeoss_menu_setup_upgrades_existing_v1_source_without_duplicates(self):
        from webapp.codeoss.producer import _patch_codeoss_terminal_menus
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            target = source / 'src/vs/workbench/contrib/terminal/browser/terminalMenus.ts'
            target.parent.mkdir(parents=True)
            target.write_text("""import { MenuId, MenuRegistry } from 'actions';
const TerminalMenuBarGroup = { Create: '1_create' };
export function setupTerminalMenus(): void {
\tMenuRegistry.appendMenuItems(
\t\t[
            // CINTERFORGE_REAL_CODEOSS_TERMINAL_COMMANDS_V1
            { id: MenuId.MenubarTerminalMenu, item: { command: {
                id: 'cinterforge.newMacTerminal' }, group: TerminalMenuBarGroup.Create } },
        ]);
}
""")
            _patch_codeoss_terminal_menus(source)
            text = target.read_text()
            self.assertIn('CINTERFORGE_REAL_CODEOSS_TERMINAL_COMMANDS_V3', text)
            self.assertEqual(text.count("id: 'cinterforge.newMacTerminal'"), 1)
            self.assertEqual(text.count("id: 'cinterforge.openMacLauncher'"), 1)
            self.assertEqual(text.count("id: 'cinterforge.openWindowsLauncher'"), 1)
            self.assertEqual(text.count("id: 'cinterforge.openLinuxLauncher'"), 1)
            _patch_codeoss_terminal_menus(source)
            self.assertEqual(text, target.read_text(), 'Setup must be idempotent')

    def test_native_platform_menu_entries_remain_visible_but_disabled(self):
        ui = (ROOT / 'webapp/terminal.js').read_text()
        server = (ROOT / 'webapp/CinterForgeTerminal.py').read_text()
        extension = (ROOT / 'webapp/codeoss/extension/extension.js').read_text()
        producer = (ROOT / 'webapp/codeoss/producer.py').read_text()
        self.assertIn('function terminalActionEnabled(kind)', ui)
        self.assertIn("platform === 'mac' && devtoolsInstalled", ui)
        self.assertIn("if (!terminalActionEnabled(kind)) return;", ui)
        self.assertIn("'devtools_installed':", server)
        self.assertIn('Cross-platform Launchers are only available on the Mac host', server)
        self.assertIn('cinterforge.devtoolsInstalled', extension)
        self.assertIn('precondition: {precondition}', producer)
        self.assertNotIn('f"when: {when}', producer)
        self.assertTrue(terminal._devtools_menu_ready('linux') is False)
        self.assertTrue(terminal._devtools_menu_ready('win') is False)
        with mock.patch.object(terminal, 'sdk_root', return_value=None):
            self.assertFalse(terminal._devtools_menu_ready('mac'))
        with mock.patch.object(terminal, 'sdk_root', return_value=Path('/installed')):
            self.assertTrue(terminal._devtools_menu_ready('mac'))
        with mock.patch.object(terminal, 'sdk_root', side_effect=RuntimeError('broken')):
            self.assertFalse(terminal._devtools_menu_ready('mac'))

    def test_authenticated_handler_uses_guest_readiness_without_shell_only_api(self):
        server = SimpleNamespace(guest_start_lock=threading.RLock(),
            guest_start_state='ready', guest_start_error='', _stopping=False)
        with mock.patch.object(terminal.platform, 'system', return_value='Darwin'), \
             mock.patch.object(terminal, 'sdk_root', return_value=Path('/sdk')), \
             mock.patch.object(terminal, '_env', return_value={'DEBUG':'1'}), \
             mock.patch.object(terminal, '_api', side_effect=AssertionError('Shell-only token must not be needed')):
            docker, env, name = terminal._launcher_container('linux', server)
        self.assertEqual(docker, '/sdk/mac/env/bin/docker')
        self.assertEqual(env, {'DEBUG':'1'})
        self.assertEqual(name, terminal._guest_name('linux'))
        server.guest_start_state = 'failed'
        server.guest_start_error = 'Guest image is missing'
        with mock.patch.object(terminal.platform, 'system', return_value='Darwin'), \
             mock.patch.object(terminal, 'sdk_root', return_value=Path('/sdk')):
            with self.assertRaisesRegex(RuntimeError, 'Guest image is missing'):
                terminal._launcher_container('win', server)

    def test_closing_one_codeoss_workbench_releases_only_its_owned_sessions(self):
        class DummyPTY(terminal.TerminalSession):
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True

        owned = [DummyPTY(), DummyPTY()]
        other = DummyPTY()
        fake_server = SimpleNamespace(code_sessions_lock=threading.Lock(),code_closed_owners=set(),code_sessions={
            'terminal-alpha-1': owned[0], 'terminal-alpha-2': owned[1],
            'terminal-beta-1': other})
        handler = object.__new__(terminal.Handler)
        handler.server = fake_server
        result = handler._code_shell_action('/host/code-shell-close-owner', {'owner':'alpha'})
        self.assertEqual(result, {'closed':2})
        self.assertTrue(all(pty.closed for pty in owned))
        self.assertFalse(other.closed)
        self.assertIn('alpha',fake_server.code_closed_owners)
        self.assertEqual(list(fake_server.code_sessions), ['terminal-beta-1'])
        # Closing CodeOSS must also reject late PTY opens from its old worker,
        # not merely remove the sessions that existed at close time.
        with self.assertRaisesRegex(RuntimeError, 'workbench is closed'):
            handler._code_shell_action('/host/code-shell-open',
                                       {'id': 'terminal-alpha-3', 'backend': 'server'})
        with self.assertRaisesRegex(ValueError, 'Invalid Code-OSS owner'):
            handler._code_shell_action('/host/code-shell-close-owner', {'owner':'alpha/../beta'})


if __name__ == '__main__':
    unittest.main()
