"""Public customer-documentation contract, run by the normal Cinter Test phase.

All checks are offline and read-only. They deliberately do not test public CDN
availability; publication/verification belongs to Cinter's explicit Publish.
"""
from pathlib import Path
import json
import re
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicDocumentationTests(unittest.TestCase):
    def test_poster_is_present_in_exact_named_png(self):
        image = ROOT / 'CinterForgeterminal.png'
        self.assertTrue(image.is_file(), str(image))
        with image.open('rb') as stream:
            header = stream.read(24)
        self.assertEqual(header[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(header[12:16], b'IHDR')
        width, height = struct.unpack('>II', header[16:24])
        self.assertGreaterEqual(width, 1200)
        self.assertGreaterEqual(height, 700)

    def test_catalog_counts_match_customer_claims(self):
        webapp = ROOT / 'webapp'
        styles = json.loads((webapp / 'terminal_styles.json').read_text(encoding='utf-8'))
        games = json.loads((webapp / 'mac_games.json').read_text(encoding='utf-8'))
        stories = json.loads((webapp / 'if_titles.json').read_text(encoding='utf-8'))
        self.assertEqual(len(styles['emulators']), 48)
        self.assertEqual(len(styles['profiles']), 19)
        self.assertEqual(len(games['games']) + 1, 68)  # WarGames plus classic Mac catalog.
        self.assertEqual(len(stories['titles']), 16)
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        manual = (ROOT / 'docs/User Manual.md').read_text(encoding='utf-8')
        for number in ('48', '19', '68', '16'):
            self.assertIn(number, readme)
            self.assertIn(number, manual)

    def test_readme_publisher_managed_links_use_stable_distribution_paths(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertTrue(readme.startswith('# CinterForge Terminal\n\n'
                          '<!-- CINTERFORGE:DOWNLOADS:START -->'))
        self.assertEqual(readme.count('<!-- CINTERFORGE:DOWNLOADS:START -->'),1)
        self.assertEqual(readme.count('<!-- CINTERFORGE:DOWNLOADS:END -->'),1)
        managed = readme.split('<!-- CINTERFORGE:DOWNLOADS:START -->',1)[1].split(
            '<!-- CINTERFORGE:DOWNLOADS:END -->',1)[0]
        for platform in ('mac', 'win', 'linux', 'devtools'):
            expected = (
                'https://cinterforge.ucreg.io/CinterForgeTerminal/Distribution/'
                f'CinterForgeTerminal-{platform}.zip'
            )
            self.assertIn(expected, managed)
        self.assertIn('CinterForgeTerminal/Distribution/four-artifacts.json', managed)
        self.assertNotIn('/Distribution/2026.11.23/', managed)
        self.assertIn('![CinterForge Terminal', readme)
        self.assertIn('](CinterForgeterminal.png)', readme)

    def test_manual_entrypoints_and_safe_bedrock_conventions(self):
        manual = (ROOT / 'docs/User Manual.md').read_text(encoding='utf-8')
        self.assertIn('bedrock run', manual)
        self.assertIn('minecraft', manual)
        self.assertIn('**attaches**', manual)
        self.assertNotIn('minecraft start', manual)
        self.assertIn('../CinterForgeterminal.png', manual)
        self.assertIn('Cloudflare', manual)
        self.assertIn('Build', manual)
        self.assertIn('Code-OSS', manual)


if __name__ == '__main__':
    unittest.main()
