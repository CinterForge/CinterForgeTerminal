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
        image = ROOT / 'CinterForgeTerminal.png'
        self.assertTrue(image.is_file(), str(image))
        with image.open('rb') as stream:
            header = stream.read(24)
        self.assertEqual(header[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(header[12:16], b'IHDR')
        width, height = struct.unpack('>II', header[16:24])
        self.assertGreaterEqual(width, 1200)
        self.assertGreaterEqual(height, 700)





if __name__ == '__main__':
    unittest.main()
