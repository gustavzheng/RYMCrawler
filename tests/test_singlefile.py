import base64
import json
import unittest
from unittest.mock import Mock, patch

import test_crawler
from rym_crawler.transports.singlefile import BridgeDownload, Inbox, accept_file, countdown, fetch_singlefile, saved_source
from rym_crawler.cli import identity_mismatch


class SingleFileTests(unittest.TestCase):
    setUp = test_crawler.Tests.setUp
    tearDown = test_crawler.Tests.tearDown

    def page(self):
        data = base64.b64encode(b'\xff\xd8\xffcover').decode()
        return test_crawler.PAGE.replace('</table>', '</table><div class="coverart_1"><img src="data:image/jpeg;base64,' + data + '"></div>')

    def clock(self):
        self.tick = 0
        def sleep(n): self.tick += max(n, 1)
        return sleep, lambda: self.tick

    def test_stable_files_only_and_partial_ignored(self):
        path = self.root / 'a.html'
        path.write_text('first')
        (self.root / 'download.crdownload').write_text('incomplete')
        watcher = Inbox(self.root)
        self.assertEqual(list(watcher.ready()), [])
        path.write_text('second version')
        self.assertEqual(list(watcher.ready()), [])
        self.assertEqual(len(list(watcher.ready())), 1)

    def test_auto_import_and_embedded_cover(self):
        inbox = self.root / 'downloads'
        inbox.mkdir()
        tab = Mock()
        def opened(_):
            (inbox / 'auto.html').write_text(self.page())
            return tab
        opener = Mock(side_effect=opened)
        def closed(_):
            # Verify durable state through another connection before closing.
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(self.root / 'state.sqlite3')) as other:
                self.assertEqual(other.execute('SELECT status FROM jobs').fetchone()[0], 'done')
            self.assertTrue((self.root / 'html' / '1.html').exists())
            self.assertTrue((self.root / 'enriched.csv').exists())
        tab.close.side_effect = closed
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 60, 120, inbox, 10, opener, sleep, now)
        row = self.db.execute('SELECT status,result FROM jobs').fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertFalse((inbox / 'auto.html').exists())
        self.assertTrue((self.root / 'inbox' / 'auto.html').exists())
        cover = json.loads(row['result'])['cover']
        self.assertEqual(cover['method'], 'singlefile_embedded')
        self.assertEqual((self.root / cover['path']).read_bytes(), b'\xff\xd8\xffcover')
        opener.assert_called_once()
        tab.close.assert_called_once()

    def test_resume_uses_file_without_opening(self):
        inbox = self.root / 'inbox'
        inbox.mkdir()
        (inbox / 'existing.html').write_text(self.page())
        opener = Mock(side_effect=AssertionError('Must not open browser'))
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 60, 120, inbox, 10, opener, sleep, now)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'done')

    def test_retry_opens_transliterated_url_and_verifies_saved_page(self):
        self.db.execute('UPDATE jobs SET url=?,attempts=1,status="awaiting_user"',
                        ('https://rateyourmusic.com/release/album/fl%C3%B8ating-points/mere-mortals/',))
        inbox = self.root / 'downloads'
        inbox.mkdir()
        tab = Mock()
        def opened(url):
            self.assertEqual(url, 'https://rateyourmusic.com/release/album/floating-points/mere-mortals/')
            (inbox / 'auto.html').write_text(self.page())
            return tab
        opener = Mock(side_effect=opened)
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 60, 120, inbox, 10, opener, sleep, now)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'done')
        opener.assert_called_once()
        tab.close.assert_called_once()

    def test_challenge_keeps_queue_paused(self):
        inbox = self.root / 'inbox'
        inbox.mkdir()
        opener = Mock(side_effect=lambda _: (inbox / 'challenge.html').write_text('<title>Just a moment...</title>'))
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 60, 120, inbox, 5, opener, sleep, now)
        row = self.db.execute('SELECT status,result FROM jobs').fetchone()
        self.assertEqual(row['status'], 'awaiting_user')
        self.assertIsNone(row['result'])
        opener.assert_called_once()

    def test_missing_album_is_skipped(self):
        inbox = self.root / 'downloads'
        inbox.mkdir()
        original = self.db.execute('SELECT url FROM jobs').fetchone()[0]
        urls = []
        def opened(url):
            urls.append(url)
            raw = ('<!--\n url: ' + url + '\n--><title>Page not found - Rate Your Music</title>') if len(urls) == 1 else self.page()
            (inbox / f'page-{len(urls)}.html').write_text(raw, encoding='utf-8')
            return Mock()
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 30, 30, inbox, 5, opened, sleep, now)
        self.assertEqual(urls, [original])
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'not_found')

    def test_missing_ep_does_not_retry_forever(self):
        inbox = self.root / 'downloads'
        inbox.mkdir()
        urls = []
        def opened(url):
            urls.append(url)
            (inbox / f'page-{len(urls)}.html').write_text('<!--\n url: ' + url + '\n--><h1>404 Not Found</h1>', encoding='utf-8')
            return Mock()
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 30, 30, inbox, 5, opened, sleep, now)
        self.assertEqual(len(urls), 1)
        fetch_singlefile(self.db, self.root, 1, 30, 30, inbox, 5, opened, sleep, now)
        self.assertEqual(len(urls), 1)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'not_found')

    def test_unrelated_missing_page_does_not_retry(self):
        inbox = self.root / 'downloads'
        inbox.mkdir()
        def opened(url):
            (inbox / 'unrelated.html').write_text('<!--\n url: https://rateyourmusic.com/release/album/other/other/\n--><h1>404 Not Found</h1>', encoding='utf-8')
            return Mock()
        opener = Mock(side_effect=opened)
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 30, 30, inbox, 5, opener, sleep, now)
        opener.assert_called_once()

    def test_optional_duration_missing_closes_completed_tab(self):
        inbox = self.root / 'downloads'
        inbox.mkdir()
        tab = Mock()
        def opened(_):
            (inbox / 'partial.html').write_text(self.page().replace('4:03', '?'))
            return tab
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 60, 120, inbox, 5, opened, sleep, now)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'done')
        tab.close.assert_called_once()

    def test_resume_cached_html_after_interrupt_before_commit(self):
        from rym_crawler.cli import atomic
        atomic(self.root / 'html' / '1.html', self.page())
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 60, 120, self.root / 'downloads', 5,
                         Mock(side_effect=AssertionError('Must use cached HTML')), sleep, now)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'done')

    def test_close_failure_keeps_completed_record(self):
        inbox = self.root / 'downloads'
        inbox.mkdir()
        tab = Mock()
        tab.close.side_effect = RuntimeError('close timeout')
        def opened(_):
            (inbox / 'auto.html').write_text(self.page())
            return tab
        sleep, now = self.clock()
        with self.assertRaisesRegex(RuntimeError, 'close timeout'):
            fetch_singlefile(self.db, self.root, 1, 60, 120, inbox, 5, opened, sleep, now)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'done')

    def test_invalid_embedded_cover_recorded(self):
        from rym_crawler.cover_cache import import_local_cover
        with self.assertRaises(ValueError):
            import_local_cover(self.root, '1', self.root / 'page.html', '<img alt="Cover art for X" src="data:image/png;base64,INVALID!">', None)

    def test_countdown_and_saved_source(self):
        calls = []
        countdown(3, calls.append)
        self.assertEqual(calls, [1, 1, 1])
        self.assertEqual(saved_source('<!--\n url: https://rateyourmusic.com/collection/me/recent/\n-->'),
                         'https://rateyourmusic.com/collection/me/recent/')

    def test_old_handshake_download_never_reaches_album_parser(self):
        path = self.root / 'bridge.html'
        path.write_text('<!--\n url: http://127.0.0.1:12345/' + 'x' * 43 +
                        '/start\n--><title>RYM Tab Companion</title>', encoding='utf-8')
        job = self.db.execute('SELECT * FROM jobs').fetchone()
        with patch('rym_crawler.transports.singlefile.parse_page', side_effect=AssertionError('Must skip parser')):
            with self.assertRaises(BridgeDownload):
                accept_file(self.db, self.root, job, path)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'pending')
        self.assertFalse((self.root / 'html' / '1.singlefile.json').exists())

    def test_old_handshake_file_does_not_block_real_download(self):
        inbox = self.root / 'downloads'
        inbox.mkdir()
        tab = Mock()
        def opened(_):
            (inbox / 'a-bridge.html').write_text('<!--\n url: http://127.0.0.1:12345/' +
                                               'x' * 43 + '/start\n-->')
            (inbox / 'b-album.html').write_text(self.page())
            return tab
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 60, 120, inbox, 5, opened, sleep, now)
        row = self.db.execute('SELECT status,error FROM jobs').fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertIsNone(row['error'])
        tab.close.assert_called_once()

    def test_missing_page_continues_and_rerun_only_fetches_unfinished(self):
        self.db.execute("INSERT INTO jobs(id,source,url) SELECT '2',source,url FROM jobs WHERE id='1'")
        inbox = self.root / 'downloads'
        inbox.mkdir()
        urls = []
        def opened(url):
            urls.append(url)
            raw = ('<!--\n url: ' + url + '\n--><h1>NOT FOUND</h1>') if len(urls) == 1 else self.page().replace('coverart_1', 'coverart_2')
            (inbox / f'page-{len(urls)}.html').write_text(raw, encoding='utf-8')
            return Mock()
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 2, 30, 30, inbox, 5, opened, sleep, now)
        self.assertEqual([r[0] for r in self.db.execute('SELECT status FROM jobs ORDER BY id')], ['not_found', 'done'])
        fetch_singlefile(self.db, self.root, 2, 30, 30, inbox, 5, opened, sleep, now)
        self.assertEqual(len(urls), 2)

    def test_identity_mismatch_is_marked_and_queue_continues(self):
        self.db.execute("INSERT INTO jobs(id,source,url) SELECT '2',source,url FROM jobs WHERE id='1'")
        inbox = self.root / 'downloads'
        inbox.mkdir()
        opened_ids = []
        def opened(url):
            opened_ids.append(len(opened_ids) + 1)
            raw = self.page().replace('Mere Mortals', 'Wrong Album') if len(opened_ids) == 1 else self.page().replace('coverart_1', 'coverart_2')
            raw = '<!--\n url: ' + url + '\n-->' + raw
            (inbox / f'page-{len(opened_ids)}.html').write_text(raw, encoding='utf-8')
            return Mock()
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 2, 30, 30, inbox, 5, opened, sleep, now)
        rows = list(self.db.execute('SELECT status,result FROM jobs ORDER BY id'))
        self.assertEqual([row['status'] for row in rows], ['partial', 'done'])
        self.assertTrue(any(w.startswith('identity:title_mismatch')
                            for w in json.loads(rows[0]['result'])['warnings']))
        self.assertEqual(opened_ids, [1, 2])

    def test_unrelated_identity_mismatch_is_not_accepted_for_current_job(self):
        path = self.root / 'unrelated.html'
        path.write_text('<!--\n url: https://rateyourmusic.com/release/album/other/other/\n-->' +
                        self.page().replace('Mere Mortals', 'Wrong Album').replace('coverart_1', 'coverart_999'),
                        encoding='utf-8')
        job = self.db.execute('SELECT * FROM jobs').fetchone()
        with self.assertRaisesRegex(ValueError, 'identity:title_mismatch'):
            accept_file(self.db, self.root, job, path)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'pending')

    def test_related_extra_id_is_marked_partial_even_at_variant_url(self):
        path = self.root / 'related.html'
        raw = self.page().replace(
            'coverart_1', 'coverart_1 coverart_9338202')
        path.write_text('<!--\n url: https://rateyourmusic.com/release/album/example/variant/\n-->' + raw,
                        encoding='utf-8')
        job = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(accept_file(self.db, self.root, job, path), 'partial')
        self.assertTrue(identity_mismatch(self.db, job['id']))

    def test_live_redirect_is_recorded_as_mismatch_and_closed_at_final_url(self):
        inbox = self.root / 'unrelated-then-valid'
        inbox.mkdir()
        tab = Mock()

        def opened(url):
            unrelated = self.page().replace('Mere Mortals', 'Wrong Album').replace(
                'coverart_1', 'coverart_999')
            (inbox / 'a-unrelated.html').write_text(
                '<!--\n url: https://rateyourmusic.com/release/album/other/other/\n-->' + unrelated,
                encoding='utf-8')
            (inbox / 'b-valid.html').write_text(
                '<!--\n url: ' + url + '\n-->' + self.page(), encoding='utf-8')
            return tab

        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 1, 30, 30, inbox, 50, opened, sleep, now)
        row = self.db.execute('SELECT status,error,result FROM jobs').fetchone()
        self.assertEqual(row['status'], 'partial')
        self.assertIsNone(row['error'])
        self.assertTrue(any(w.startswith('identity:title_mismatch')
                            for w in json.loads(row['result'])['warnings']))
        tab.close.assert_called_once_with(
            'https://rateyourmusic.com/release/album/other/other/')

    def test_live_redirect_mismatch_continues_queue_without_retry(self):
        self.db.execute("INSERT INTO jobs(id,source,url) SELECT '2',source,url FROM jobs WHERE id='1'")
        inbox = self.root / 'only-unrelated'
        inbox.mkdir()
        opened_ids = []

        def opened(url):
            opened_ids.append(len(opened_ids) + 1)
            if len(opened_ids) == 1:
                raw = self.page().replace('Mere Mortals', 'Wrong Album').replace(
                    'coverart_1', 'coverart_999')
                source = 'https://rateyourmusic.com/release/album/other/other/'
            else:
                raw = self.page().replace('coverart_1', 'coverart_2')
                source = url
            (inbox / f'page-{len(opened_ids)}.html').write_text(
                '<!--\n url: ' + source + '\n-->' + raw, encoding='utf-8')
            return Mock()

        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 2, 30, 30, inbox, 50, opened, sleep, now)
        rows = list(self.db.execute('SELECT status,error FROM jobs ORDER BY id'))
        self.assertEqual(rows[0]['status'], 'partial')
        self.assertIsNone(rows[0]['error'])
        self.assertEqual(rows[1]['status'], 'done')
        self.assertEqual(opened_ids, [1, 2])

    def test_block_stops_before_next_job_without_waiting(self):
        self.db.execute("INSERT INTO jobs(id,source,url) SELECT '2',source,url FROM jobs WHERE id='1'")
        inbox = self.root / 'downloads'
        inbox.mkdir()
        opener = Mock(side_effect=lambda _: (inbox / 'challenge.html').write_text('<title>Access Denied</title>'))
        sleep, now = self.clock()
        fetch_singlefile(self.db, self.root, 2, 30, 30, inbox, 50, opener, sleep, now)
        self.assertLess(self.tick, 50)
        opener.assert_called_once()
        self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE id='2'").fetchone()[0], 'pending')

    def test_unknown_or_unrelated_error_stops_before_later_valid_download(self):
        for raw in ('<title>请稍候…</title>', '<html>Unknown verification screen</html>',
                    '<!--\n url: https://rateyourmusic.com/release/album/other/other/\n--><h1>404 Not Found</h1>'):
            with self.subTest(raw=raw):
                inbox = self.root / ('case-' + str(len(list(self.root.glob('case-*')))))
                inbox.mkdir()
                self.db.execute('UPDATE jobs SET status="pending",error=NULL')
                tab = Mock()
                def opened(_):
                    (inbox / 'a-error.html').write_text(raw, encoding='utf-8')
                    (inbox / 'b-valid.html').write_text(self.page(), encoding='utf-8')
                    return tab
                sleep, now = self.clock()
                fetch_singlefile(self.db, self.root, 1, 30, 30, inbox, 50, opened, sleep, now)
                row = self.db.execute('SELECT status,error FROM jobs').fetchone()
                self.assertEqual(row['status'], 'awaiting_user')
                self.assertIsNotNone(row['error'])
                self.assertLess(self.tick, 50)
                tab.close.assert_not_called()
