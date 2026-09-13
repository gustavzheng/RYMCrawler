import json
import unittest
from unittest.mock import Mock, patch

import test_crawler
from rym_crawler.transports.edge import fetch_normal, open_edge


class NormalEdgeTests(unittest.TestCase):
    setUp = test_crawler.Tests.setUp
    tearDown = test_crawler.Tests.tearDown

    def test_open_without_automation_profile(self):
        with patch('rym_crawler.transports.edge.os.name', 'nt'), patch('rym_crawler.transports.edge.os.startfile') as start:
            open_edge('https://rateyourmusic.com/release/album/iggy-pop/lust-for-life/')
            start.assert_called_once_with('https://rateyourmusic.com/release/album/iggy-pop/lust-for-life/')

    def test_save_and_resume(self):
        path = self.root / 'saved.html'
        path.write_text(test_crawler.PAGE, encoding='utf-8')
        launch = Mock()
        fetch_normal(self.db, self.root, 1, 60, 120, Mock(return_value=str(path)), launch)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'done')
        fetch_normal(self.db, self.root, 1, 60, 120, Mock(side_effect=AssertionError), launch)
        launch.assert_called_once()

    def test_quit_preserves_handoff(self):
        fetch_normal(self.db, self.root, 1, 60, 120, lambda _: 'q', Mock())
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'awaiting_user')

    def test_wrong_page_is_marked_partial_and_skipped(self):
        path = self.root / 'wrong.html'
        path.write_text(test_crawler.PAGE.replace('Mere Mortals', 'Wrong Album'), encoding='utf-8')
        fetch_normal(self.db, self.root, 1, 60, 120, Mock(side_effect=[str(path), 'q']), Mock())
        row = self.db.execute('SELECT status,result FROM jobs').fetchone()
        self.assertEqual(row['status'], 'partial')
        self.assertTrue(any(w.startswith('identity:title_mismatch')
                            for w in json.loads(row['result'])['warnings']))
