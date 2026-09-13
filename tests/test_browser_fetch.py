import json
import unittest
from unittest.mock import Mock, patch

import test_crawler
from rym_crawler.transports.browser import collect_page, run_jobs, fetch_browser


PAGE = test_crawler.PAGE


class BrowserTests(unittest.TestCase):
    setUp = test_crawler.Tests.setUp
    tearDown = test_crawler.Tests.tearDown
    def job(self):
        return self.db.execute('SELECT * FROM jobs').fetchone()

    def page(self, contents):
        page = Mock()
        page.url = 'https://rateyourmusic.com/release/album/example/example/'
        page.locator.return_value.first.evaluate.return_value = 'https://cdn.sonemic.net/cover.jpg'
        page.content.side_effect = contents
        return page

    def test_block_stops_immediately_and_later_run_can_resume(self):
        page = self.page(['<title>Just a moment...</title>', PAGE])
        prompt = Mock(side_effect=AssertionError('Must stop immediately'))
        self.assertEqual(collect_page(self.db, self.root, self.job(), page, 403, prompt), 'awaiting_user')
        self.assertTrue((self.root / 'html/1.verification.html').exists())
        self.assertEqual(collect_page(self.db, self.root, self.job(), page, prompt=prompt), 'done')
        prompt.assert_not_called()

    def test_quit_keeps_pending_handoff(self):
        page = self.page(['<title>Just a moment...</title>'])
        self.assertEqual(collect_page(self.db, self.root, self.job(), page, 429, lambda _: 'q'), 'awaiting_user')
        self.assertIsNone(self.job()['result'])
        self.assertFalse((self.root / 'html/1.html').exists())

    def test_challenge_with_404_status_is_not_skipped(self):
        page = self.page(['<title>请稍候…</title>'])
        page.url = self.job()['url']
        self.assertEqual(collect_page(self.db, self.root, self.job(), page, 404), 'awaiting_user')
        self.assertIn('blocked:', self.job()['error'])

    def test_enter_on_wrong_album_does_not_mark_done(self):
        page = self.page(['<title>Just a moment...</title>', PAGE.replace('Mere Mortals', 'Wrong Album')])
        self.assertEqual(collect_page(self.db, self.root, self.job(), page, prompt=Mock(side_effect=['', 'q'])), 'awaiting_user')

    def test_wrong_album_is_marked_partial_without_handoff(self):
        page = self.page([PAGE.replace('Mere Mortals', 'Wrong Album')])
        self.assertEqual(collect_page(self.db, self.root, self.job(), page), 'partial')
        row = self.job()
        self.assertEqual(row['status'], 'partial')
        self.assertTrue(any(w.startswith('identity:title_mismatch')
                            for w in json.loads(row['result'])['warnings']))

    def test_interrupt_preserves_handoff(self):
        page = self.page(['<title>Just a moment...</title>'])
        prompt = Mock(side_effect=KeyboardInterrupt)
        collect_page(self.db, self.root, self.job(), page, prompt=prompt)
        prompt.assert_not_called()
        self.assertEqual(self.job()['status'], 'awaiting_user')

    def test_browser_closed_preserves_handoff(self):
        from playwright.sync_api import Error
        page = self.page(['<title>Just a moment...</title>', Error('closed')])
        page.goto.return_value.status = 403
        run_jobs(self.db, self.root, page, [self.job()], 60, 120, lambda _: '')
        self.assertEqual(self.job()['status'], 'awaiting_user')

    def test_timeout_stops_queue(self):
        from playwright.sync_api import TimeoutError
        page = self.page(['<title>Just a moment...</title>', PAGE])
        page.goto.side_effect = TimeoutError('loading challenge')
        run_jobs(self.db, self.root, page, [self.job()], 60, 120, lambda _: '')
        self.assertEqual(self.job()['status'], 'awaiting_user')
        self.assertEqual(page.goto.call_count, 1)

    def test_browser_cache_avoids_navigation(self):
        from rym_crawler.cli import atomic
        atomic(self.root / 'html/1.html', PAGE)
        page = Mock()
        run_jobs(self.db, self.root, page, [self.job()], 60, 120)
        page.goto.assert_not_called()

    def test_pending_handoff_selected_and_profile_persistent(self):
        with self.db:
            self.db.execute('UPDATE jobs SET status="awaiting_user",attempts=9')
        with patch('playwright.sync_api.sync_playwright') as factory, patch('rym_crawler.transports.browser.run_jobs') as run:
            fetch_browser(self.db, self.root, 1, 60, 120)
            launch = factory.return_value.__enter__.return_value.chromium.launch_persistent_context
            self.assertFalse(launch.call_args.kwargs['headless'])
            self.assertEqual(launch.call_args.kwargs['channel'], 'msedge')
            self.assertIn('browser-profile-msedge', launch.call_args.args[0])
            self.assertEqual(run.call_args.args[3][0]['status'], 'awaiting_user')
