import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rym_crawler import cli as c
from rym_crawler.release_urls import candidate_url, identity, ascii_url_candidate, retry_job_url
import test_crawler
from rym_crawler.transports.browser import collect_page


class UrlTests(unittest.TestCase):
    setUp = test_crawler.Tests.setUp
    tearDown = test_crawler.Tests.tearDown

    def test_latin_url_fallback(self):
        self.assertEqual(candidate_url({'Last Name': 'Lustmørd', 'Title': 'Heresy'}),
                         c.BASE + '/release/album/lustmord/heresy/')
        self.assertEqual(ascii_url_candidate(c.BASE + '/release/album/bj%C3%B6rk/æ-œ-ł-ß-é/'),
                         c.BASE + '/release/album/bjork/ae-oe-l-ss-e/')
        self.assertIsNone(ascii_url_candidate(c.BASE + '/release/album/中文/かな/'))
        self.assertIsNone(ascii_url_candidate(c.BASE + '/release/album/lustmord/heresy/'))
        self.assertEqual(ascii_url_candidate(c.BASE + '/release/album/cafe%CC%81/heresy/'),
                         c.BASE + '/release/album/cafe/heresy/')

    def test_fallback_only_on_retry_and_survives_reopen(self):
        original = c.BASE + '/release/album/lustm%C3%B8rd/heresy/'
        self.db.execute('UPDATE jobs SET url=?,attempts=0', (original,))
        job = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(retry_job_url(self.db, job)['url'], original)
        self.db.execute('UPDATE jobs SET attempts=1')
        job = retry_job_url(self.db, self.db.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(job['url'], c.BASE + '/release/album/lustmord/heresy/')
        self.assertEqual(job['url_status'], 'candidate_ascii')
        self.assertEqual(retry_job_url(self.db, job)['url'], job['url'])
        reopened = c.database(self.root)
        try:
            self.assertEqual(reopened.execute('SELECT url FROM jobs').fetchone()[0], job['url'])
        finally:
            reopened.close()

    def test_retry_preserves_manual_verified_and_blocked_urls(self):
        original = c.BASE + '/release/album/lustm%C3%B8rd/heresy/'
        for url_status, status, error in [('manual_unverified', 'awaiting_user', None),
                                          ('id_title_artist', 'retry', None),
                                          ('candidate', 'blocked', None),
                                          ('candidate', 'awaiting_user', 'blocked: challenge page'),
                                          ('candidate', 'retry', 'HTTP Error 429')]:
            with self.subTest(url_status=url_status, status=status, error=error):
                self.db.execute('UPDATE jobs SET url=?,url_status=?,status=?,error=?,attempts=1',
                                (original, url_status, status, error))
                job = self.db.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(retry_job_url(self.db, job)['url'], original)

    def test_candidate_rules(self):
        self.assertEqual(candidate_url({' First Name': 'The', 'Last Name': '1975', 'Title': 'Example'}),
                         c.BASE + '/release/album/the-1975/example/')
        self.assertEqual(candidate_url({'First Name': 'Iggy', 'Last Name': 'Pop', 'Title': 'Lust for Life'}),
                         c.BASE + '/release/album/iggy-pop/lust-for-life/')
        self.assertIn('floating-points-the-san-francisco-ballet-orchestra', candidate_url(json.loads(self.db.execute('SELECT source FROM jobs').fetchone()[0])))
        self.assertIsNone(candidate_url({'Title': 'Unknown'}))
        self.assertEqual(candidate_url({'Last Name': 'A/B', 'Title': 'What? #1'}),
                         c.BASE + '/release/album/a_b/what-1/')

    def test_slash_in_real_artist_name_uses_underscore(self):
        source = {'RYM Album': '16288311', 'Last Name': 'Model/Actriz',
                  'Title': 'Pirouette'}
        self.assertEqual(candidate_url(source), c.BASE +
                         '/release/album/model_actriz/pirouette/')

    def test_missing_album_ep_fallback_survives_reopen(self):
        with self.db:
            self.db.execute('UPDATE jobs SET url=?,attempts=1,status="awaiting_user",error="HTTP 404"',
                            (c.BASE + '/release/album/shygirl/alias/',))
        job = retry_job_url(self.db, self.db.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(job['url'], c.BASE + '/release/ep/shygirl/alias/')
        reopened = c.database(self.root)
        try:
            row = reopened.execute('SELECT * FROM jobs').fetchone()
            self.assertEqual(row['url'], job['url'])
            self.assertEqual(retry_job_url(reopened, row)['url'], job['url'])
        finally:
            reopened.close()

    def test_initial_candidate_transliterates_latin(self):
        for name in ('José González', 'Jose\u0301 Gonza\u0301lez'):
            with self.subTest(name=name):
                self.assertEqual(candidate_url({'Last Name': name, 'Title': 'Veneer'}),
                                 c.BASE + '/release/album/jose-gonzalez/veneer/')
        self.assertEqual(candidate_url({'Last Name': '中文', 'Title': 'かな'}),
                         c.BASE + '/release/album/%E4%B8%AD%E6%96%87/%E3%81%8B%E3%81%AA/')

    def test_reopen_refreshes_accented_candidate(self):
        source = {'Last Name': 'José González', 'Title': 'Veneer'}
        with self.db:
            self.db.execute('UPDATE jobs SET source=?,url=?,url_status="candidate"',
                            (json.dumps(source), c.BASE +
                             '/release/album/jos%C3%A9-gonz%C3%A1lez/veneer/'))
        reopened = c.database(self.root)
        try:
            self.assertEqual(reopened.execute('SELECT url FROM jobs').fetchone()[0],
                             c.BASE + '/release/album/jose-gonzalez/veneer/')
        finally:
            reopened.close()

    def test_fontaines_artist_slug(self):
        for title, expected in [("A Hero's Death", 'a-heros-death'),
                                ('Dogrel', 'dogrel')]:
            with self.subTest(title=title):
                self.assertEqual(candidate_url({'Last Name': 'Fontaines D.C.', 'Title': title}),
                                 c.BASE + '/release/album/fontaines-d_c/' + expected + '/')

    def test_reopen_refreshes_fontaines_candidate(self):
        source = {'RYM Album': '11460995', 'Last Name': 'Fontaines D.C.',
                  'Title': "A Hero's Death"}
        with self.db:
            self.db.execute('UPDATE jobs SET source=?,url=?,url_status="candidate"',
                            (json.dumps(source), c.BASE +
                             '/release/album/fontaines-d-c/a-heros-death/'))
        reopened = c.database(self.root)
        try:
            self.assertEqual(reopened.execute('SELECT url FROM jobs').fetchone()[0],
                             c.BASE + '/release/album/fontaines-d_c/a-heros-death/')
        finally:
            reopened.close()

    def test_reject_old_entry(self):
        with self.assertRaises(ValueError):
            c.safe_url(c.BASE + '/release/ac?album_id=45')

    def test_apostrophes_in_candidate_url(self):
        for apostrophe in ("'", '\u2019', '&#39;', '&rsquo;'):
            with self.subTest(apostrophe=apostrophe):
                source = {'First Name': 'Christoph de', 'Last Name': 'Babalon',
                          'Title': f'If You{apostrophe}re Into It, I{apostrophe}m Out of It'}
                self.assertEqual(candidate_url(source), c.BASE +
                                 '/release/album/christoph-de-babalon/if-youre-into-it-im-out-of-it/')

    def test_reopen_refreshes_apostrophe_candidate(self):
        source = {'First Name': 'Christoph de', 'Last Name': 'Babalon',
                  'Title': "If You're Into It, I'm Out of It"}
        with self.db:
            self.db.execute('UPDATE jobs SET source=?,url=?,url_status="candidate"',
                            (json.dumps(source), c.BASE +
                             '/release/album/christoph-de-babalon/if-you-re-into-it-i-m-out-of-it/'))
        reopened = c.database(self.root)
        try:
            self.assertEqual(reopened.execute('SELECT url FROM jobs').fetchone()[0],
                             candidate_url(source))
        finally:
            reopened.close()

    def test_identity_checks_artist_and_id(self):
        source = json.loads(self.db.execute('SELECT source FROM jobs').fetchone()[0])
        parsed = c.parse_page(test_crawler.PAGE, c.BASE)
        self.assertTrue(identity(source, '1', parsed)['matched'])
        parsed['page_release_ids'] = ['999']
        issues = identity(source, '1', parsed)['issues']
        self.assertTrue(any(x.startswith('identity:release_id_mismatch_manual_review') for x in issues))
        self.assertIn("expected='1'", issues[0])
        parsed['page_release_ids'] = []
        parsed['artist_credit_raw'] = 'Wrong Artist'
        parsed['artists'] = [{'name': 'Wrong Artist'}]
        self.assertFalse(identity(source, '1', parsed)['matched'])

    def test_identity_flags_expected_id_among_related_ids_for_review(self):
        source = {'Title': 'Mere Mortals', 'First Name': 'Floating Points',
                  'Last Name': '& The San Francisco Ballet Orchestra'}
        result = {'title': 'Mere Mortals',
                  'artist_credit_raw': 'Floating Points & The San Francisco Ballet Orchestra',
                  'artists': [{'name': 'Floating Points & The San Francisco Ballet Orchestra'}],
                  'page_release_ids': ['1', '9338202']}
        checked = identity(source, '1', result)
        self.assertFalse(checked['matched'])
        self.assertTrue(any(x.startswith('identity:release_id_mismatch_manual_review')
                            for x in checked['issues']))

    def test_voting_id_takes_precedence_over_cover_id(self):
        source = {'Title': 'Mere Mortals', 'First Name': 'Floating Points',
                  'Last Name': '& The San Francisco Ballet Orchestra'}
        result = {'title': 'Mere Mortals',
                  'artist_credit_raw': 'Floating Points & The San Francisco Ballet Orchestra',
                  'artists': [{'name': 'Floating Points & The San Francisco Ballet Orchestra'}],
                  'page_release_ids': ['1', '9338202'], 'cover_release_ids': ['1'],
                  'vote_album_ids': ['9338202']}
        self.assertTrue(identity(source, '9338202', result)['matched'])
        self.assertFalse(identity(source, '1', result)['matched'])

    def test_bilingual_artist_identity(self):
        credit = '坂本龍一 [Ryuichi Sakamoto]'
        parsed = {'title': '12', 'artists': [{'name': credit}],
                  'artist_credit_raw': credit, 'page_release_ids': ['15010489']}
        for name in ('坂本龍一', 'Ryuichi Sakamoto', credit):
            source = {'Title': '12', 'Last Name': name}
            self.assertTrue(identity(source, '15010489', parsed)['matched'])
        source = {'Title': '12', 'Last Name': 'Ryuichi Sakamoto'}
        self.assertFalse(identity(source, '999', parsed)['matched'])
        self.assertFalse(identity(dict(source, Title='Other'), '15010489', parsed)['matched'])
        self.assertFalse(identity(dict(source, **{'Last Name': 'Wrong Artist'}),
                                  '15010489', parsed)['matched'])
        parsed['artists'].append({'name': 'Other Artist'})
        parsed['artist_credit_raw'] += ' & Other Artist'
        self.assertFalse(identity(source, '15010489', parsed)['matched'])

    def test_manual_relocation_persists_mapping(self):
        page = Mock()
        page.url = c.BASE + '/release/album/example/example/'
        page.content.side_effect = [test_crawler.PAGE]
        job = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(collect_page(self.db, self.root, job, page, 200, lambda _: ''), 'done')
        row = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(row['url_status'], 'title_artist_only')
        self.assertTrue(row['url'].endswith('/mere-mortals/'))
        page.reload.assert_not_called()

    def test_prepare_keeps_confirmed_mapping(self):
        c.ingest(self.db, self.root, '1', test_crawler.PAGE, c.BASE)
        before = self.db.execute('SELECT url,url_status FROM jobs').fetchone()
        c.prepare(self.db, self.root / 'input.csv')
        self.assertEqual(tuple(before), tuple(self.db.execute('SELECT url,url_status FROM jobs').fetchone()))

    def test_real_saved_page_id(self):
        path = next(Path('.').glob('OK Computer*.html'))
        parsed = c.parse_page(path.read_text(encoding='utf-8'), c.BASE)
        self.assertEqual(parsed['page_release_ids'], ['45'])

    def test_migration_backup_and_replacement(self):
        folder = self.root / 'legacy'
        folder.mkdir()
        with sqlite3.connect(folder / 'state.sqlite3') as old:
            old.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,url TEXT,status TEXT,attempts INTEGER,next_try REAL,error TEXT,result TEXT)')
            old.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)', ('9', json.dumps({'Last Name': 'Iggy Pop', 'Title': 'Lust for Life'}), c.BASE + '/release/ac?album_id=9', 'pending', 0, 0, None, None))
        old.close()
        migrated = c.database(folder)
        try:
            row = migrated.execute('SELECT * FROM jobs').fetchone()
            self.assertEqual(row['url'], c.BASE + '/release/album/iggy-pop/lust-for-life/')
            self.assertEqual(row['url_status'], 'candidate')
            self.assertTrue((folder / 'state.before-url-migration.sqlite3').exists())
        finally:
            migrated.close()
