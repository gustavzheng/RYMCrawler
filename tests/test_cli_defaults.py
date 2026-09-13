import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from rym_crawler import cli as rym_crawler


class CliDefaultTests(unittest.TestCase):
    def test_fetch_inbox_defaults_to_downloads(self):
        captured = {}
        with patch.object(sys, 'argv', ['rym-crawler', '--data', 'test-data', 'fetch', '--allow-network']), \
             patch('rym_crawler.cli.database') as database, \
             patch('rym_crawler.transports.singlefile.fetch_singlefile', side_effect=lambda *args: captured.update(inbox=args[5])), \
             patch('rym_crawler.cli.export'), patch('pathlib.Path.open', unittest.mock.mock_open()), \
             patch('msvcrt.locking'):
            database.return_value.close.return_value = None
            rym_crawler.main()
        self.assertEqual(captured['inbox'], Path.home() / 'Downloads')


if __name__ == '__main__':
    unittest.main()
