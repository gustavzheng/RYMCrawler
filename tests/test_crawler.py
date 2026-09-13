"""Synthetic fixtures only. All network operations are mocked or forbidden."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from rym_crawler import cli as c

PAGE = '''<html><title>Mere Mortals</title>
<link rel="canonical" href="/release/album/floating-points-the-san-francisco-ballet-orchestra/mere-mortals/">
<div class="release_page" itemtype="http://schema.org/MusicAlbum">
<meta itemprop="name" content="Mere Mortals">
<div itemprop="aggregateRating"><meta itemprop="ratingValue" content="3.75"><meta itemprop="ratingCount" content="1,234"></div>
<table class="album_info">
<tr><th>Artist</th><td><a href="/artist/floating-points">Floating Points</a> &amp; <a href="/artist/the-san-francisco-ballet-orchestra">The San Francisco Ballet Orchestra</a></td></tr>
<tr><th>Released</th><td>2026</td></tr><tr><th>Type</th><td>Album</td></tr>
</table><div class="release_pri_genres"><a class="genre">Electronic</a></div>
<div class="release_sec_genres"><a class="genre">Ambient</a></div>
<div class="release_pri_descriptors">instrumental, atmospheric</div>
<ul id="tracks"><li>Disc 1<ul><li><span class="tracklist_title"><span class="rendered_text">One</span></span><span class="tracklist_duration">3:02</span></li>
<li><span class="tracklist_title">Two</span><span class="tracklist_duration">4:03</span></li></ul></li></ul>
</div></html>'''


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = c.database(self.root)
        path = self.root / 'input.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['RYM Album', 'Rating', 'Title', 'Last Name', 'Review'])
            w.writeheader()
            for rid, rating in [('1', '7'), ('2', '0'), ('1', '7')]:
                w.writerow({'RYM Album': rid, 'Rating': rating, 'Title': 'Mere Mortals', 'Last Name': 'Floating Points &amp; The San Francisco Ballet Orchestra', 'Review': '中文, "quote"\nnext'})
        self.summary = c.prepare(self.db, path)
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def test_csv_dedupe_and_no_network(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
            self.assertEqual(self.summary, {'csv_rows': 3, 'nonzero_rows': 2, 'unique_jobs': 1})
            c.export(self.db, self.root)
            data = json.loads((self.root / 'enriched.json').read_text(encoding='utf-8'))
            self.assertEqual(data[0]['source_artist_decoded'], 'Floating Points & The San Francisco Ballet Orchestra')
            self.assertIn('\n', data[0]['source']['Review'])
    def test_collaboration_and_tracks(self):
        result = c.parse_page(PAGE, c.BASE + '/release/album/example/example/')
        self.assertEqual(len(result['artists']), 2)
        self.assertEqual(result['rating_count'], 1234)
        self.assertEqual(result['track_count'], 2)
        self.assertEqual(result['duration_seconds'], 425)
        self.assertEqual(result['warnings'], [])
    def test_band_ampersand_not_split(self):
        page = PAGE.replace('Floating Points</a>', 'Earth, Wind &amp; Fire</a>')
        self.assertEqual(c.parse_page(page, c.BASE)['artists'][0]['name'], 'Earth, Wind & Fire')
    def test_collaboration_credit_without_artist_links(self):
        start = PAGE.index('<tr><th>Artist</th>')
        end = PAGE.index('</tr>', start) + len('</tr>')
        credit = 'The Lord † Petra Haden'
        row = ('<tr><th>Artist</th><td><span itemprop="byArtist">'
               '<span class="credited_name"><span itemprop="name">'
               + credit + '</span><div class="credited_list sf-hidden"></div>'
               '</span></span></td></tr>')
        result = c.parse_page(PAGE[:start] + row + PAGE[end:], c.BASE)
        self.assertEqual(result['artists'], [{'name': credit, 'url': None}])
        self.assertEqual(result['artist_credit_raw'], credit)
        self.assertNotIn('missing_or_invalid:artists', result['warnings'])
    def test_unknown_duration_not_zero(self):
        result = c.parse_page(PAGE.replace('4:03', '?'), c.BASE)
        self.assertIsNone(result['duration_seconds'])
    def test_challenge_and_schema(self):
        for page, status in [('<title>Just a moment...</title>', 'blocked'), ('<html>changed</html>', 'parse_error')]:
            self.assertEqual(c.ingest(self.db, self.root, '1', page, c.BASE), status)
            self.assertTrue((self.root / 'html/1.html').exists())
    def test_missing_and_identity(self):
        self.assertEqual(c.ingest(self.db, self.root, '1', PAGE.replace('Mere Mortals', 'Other album'), c.BASE), 'partial')
    def test_resume_cached_without_network(self):
        c.atomic(self.root / 'html/1.html', PAGE)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
            c.fetch(self.db, self.root, 1, 60, 120)
            c.fetch(self.db, self.root, 1, 60, 120)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'done')

    def test_optional_metadata_missing_continues_queue(self):
        page = '''<div class="release_page"><h1 class="album_title">Mere Mortals</h1>
        <table class="album_info"><tr><th>Artist</th><td>
        <a href="/artist/example">Floating Points &amp; The San Francisco Ballet Orchestra</a>
        </td></tr></table></div>'''
        job = self.db.execute('SELECT source,url FROM jobs WHERE id="1"').fetchone()
        with self.db:
            self.db.execute('INSERT INTO jobs(id,source,url) VALUES(?,?,?)', ('3', job['source'], job['url']))
        c.atomic(self.root / 'html/1.html', page)
        c.atomic(self.root / 'html/3.html', PAGE)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
            c.fetch(self.db, self.root, 2, 60, 120)
        self.assertEqual([r[0] for r in self.db.execute('SELECT status FROM jobs ORDER BY id')], ['done', 'done'])
        result = json.loads(self.db.execute('SELECT result FROM jobs WHERE id="1"').fetchone()[0])
        self.assertIsNone(result['duration_seconds'])
        self.assertIsNone(result['track_count'])
        self.assertEqual(result['descriptors'], [])
        self.assertIn('missing_optional:duration_seconds', result['warnings'])

    def test_invalid_rating_still_partial(self):
        self.assertEqual(c.ingest(self.db, self.root, '1', PAGE.replace('3.75', '5.75'), c.BASE), 'partial')
    def test_http_block_and_persistent_stop(self):
        error = HTTPError(c.BASE, 429, 'Too many requests', {}, io.BytesIO(b'blocked'))
        with patch('urllib.request.OpenerDirector.open', side_effect=error) as request:
            c.fetch(self.db, self.root, 1, 60, 120)
            with self.assertRaises(ValueError): c.fetch(self.db, self.root, 1, 60, 120)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'blocked')
    def test_retry_persisted(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=TimeoutError('timeout')):
            c.fetch(self.db, self.root, 1, 60, 120)
        row = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(row['status'], 'retry')
        self.assertGreater(row['next_try'], c.time.time())
    def test_http_not_found_continues_but_timeout_stops(self):
        self.db.execute("INSERT INTO jobs(id,source,url) SELECT '2',source,url FROM jobs WHERE id='1'")
        self.db.execute("INSERT INTO jobs(id,source,url) SELECT '3',source,url FROM jobs WHERE id='1'")
        error = HTTPError(c.BASE, 404, 'Not Found', {}, io.BytesIO(b''))
        with patch('urllib.request.OpenerDirector.open', side_effect=[error, TimeoutError('timeout')]) as opened, patch('rym_crawler.cli.time.sleep'):
            c.fetch(self.db, self.root, 3, 30, 30)
        error.close()
        self.assertEqual(opened.call_count, 2)
        self.assertEqual([r[0] for r in self.db.execute('SELECT status FROM jobs ORDER BY id')], ['not_found', 'retry', 'pending'])

    def test_not_found_detection_and_block_priority(self):
        self.assertEqual(c.ingest(self.db, self.root, '1', '<h1>NOT FOUND</h1>', c.BASE), 'not_found')
        self.assertEqual(c.ingest(self.db, self.root, '1', '<title>Access Denied</title><h1>404 Not Found</h1>', c.BASE), 'blocked')
        self.assertEqual(c.ingest(self.db, self.root, '1', PAGE.replace('<title>Mere Mortals</title>', '<title>Not Found</title>'), c.BASE), 'done')

    def test_url_and_duration(self):
        for url in ['https://evil.test/release/a', 'http://rateyourmusic.com/release/a', 'https://rateyourmusic.com/artist/a']:
            with self.assertRaises(ValueError): c.safe_url(url)
        self.assertEqual(c.duration('PT1H2M3S'), 3723)

    def test_recording_duration_is_not_album_duration(self):
        page = PAGE.replace('<span class="tracklist_title"><span class="rendered_text">One</span></span><span class="tracklist_duration">3:02</span>', '<div itemscope itemtype="http://schema.org/MusicRecording"><span class="tracklist_title"><a itemprop="name">One</a><a class="tracks_lyric_link">lyrics</a><span class="tracklist_duration" itemprop="duration" content="PT3M2S">3:02</span></span></div>')
        result = c.parse_page(page, c.BASE)
        self.assertEqual(result['duration_seconds'], 425)
        self.assertEqual(result['tracks'][0]['title'], 'One')

    def test_saved_ok_computer(self):
        path = next(Path(__file__).resolve().parents[1].glob('OK Computer*.html'), None)
        if path is None:
            self.skipTest('User-saved HTML is not present')
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
            result = c.parse_page(path.read_text(encoding='utf-8'), c.BASE)
        self.assertEqual(result['title'], 'OK Computer')
        self.assertEqual(result['artists'], [{'name': 'Radiohead', 'url': c.BASE + '/artist/radiohead'}])
        self.assertEqual(result['release_date_raw'], '16 June 1997')
        self.assertEqual(result['release_type'], 'Album')
        self.assertEqual(result['recorded_raw'], '4 September 1995 - 6 March 1997')
        self.assertEqual(result['language_raw'], 'English')
        self.assertEqual(result['ranks'], [
            {'position': 1, 'scope': 'year', 'year': 1997},
            {'position': 2, 'scope': 'overall', 'year': None}])
        self.assertEqual([row['label'] for row in result['album_info']],
                         ['Artist', 'Type', 'Released', 'Recorded', 'RYM Rating', 'Ranked', 'Genres', 'Descriptors', 'Language'])
        self.assertEqual(result['community_rating'], 4.30)
        self.assertEqual(result['rating_count'], 139830)
        self.assertEqual(result['primary_genres'], ['Alternative Rock', 'Art Rock'])
        self.assertEqual(result['secondary_genres'], ['Post-Britpop', 'Space Rock Revival'])
        self.assertEqual(len(result['descriptors']), 34)
        self.assertEqual(result['duration_seconds'], 3201)
        self.assertEqual(sum(t['duration_seconds'] for t in result['tracks']), 3201)
        self.assertEqual(result['track_count'], 12)
        self.assertEqual(result['tracks'][0]['title'], 'Airbag')
        self.assertEqual(result['tracks'][-1]['title'], 'The Tourist')
        self.assertFalse(any('lyrics' in t['title'] for t in result['tracks']))
        self.assertEqual(result['canonical_url'], c.BASE + '/release/album/radiohead/ok-computer/')
        self.assertEqual(result['warnings'], [])

    def test_extra_metadata_preserved_and_exported(self):
        page = PAGE.replace('</table>', '<tr><th>Languages</th><td>English, French</td></tr><tr><th>Unknown future field</th><td><a href="/label/example">Example</a></td><td>Extra value</td></tr></table>')
        c.ingest(self.db, self.root, '1', page, c.BASE)
        result = json.loads(self.db.execute('SELECT result FROM jobs').fetchone()[0])
        self.assertEqual(result['language_raw'], 'English, French')
        self.assertIsNone(result['recorded_raw'])
        self.assertEqual(result['ranks'], [])
        extra = result['album_info'][-1]
        self.assertEqual(extra['values'], ['Example', 'Extra value'])
        self.assertEqual(extra['links'][0]['url'], c.BASE + '/label/example')
        with (self.root / 'enriched.csv').open(encoding='utf-8-sig', newline='') as f:
            exported = next(csv.DictReader(f))
        self.assertEqual(exported['language_raw'], 'English, French')
        self.assertEqual(json.loads(exported['album_info'])[-1], extra)

    def test_cover_and_voting_ids_are_preserved_separately(self):
        page = PAGE.replace(
            '</table>',
            '<tr><th>Genres</th><td><a href="/rgenre/set?album_id=1">vote</a></td></tr>'
            '</table><div class="coverart_9338202"></div>'
            '<a href="/releases/ac?album_id=9338202">Correct entry</a>')
        result = c.parse_page(page, c.BASE)
        self.assertEqual(result['page_release_ids'], ['1', '9338202'])
        self.assertEqual(result['cover_release_ids'], ['9338202'])
        self.assertEqual(result['vote_album_ids'], ['1'])

    def test_unnumbered_subtracks_are_preserved(self):
        old = '''<ul id="tracks"><li>Disc 1<ul><li><span class="tracklist_title"><span class="rendered_text">One</span></span><span class="tracklist_duration">3:02</span></li>
<li><span class="tracklist_title">Two</span><span class="tracklist_duration">4:03</span></li></ul></li></ul>'''
        new = '''<ul id="tracks">
        <li class="track"><span class="tracklist_num">1</span><span class="tracklist_title"><span itemprop="name">Main</span></span><span class="tracklist_duration" itemprop="duration" content="PT0M00S"></span></li>
        <li class="track"><span class="tracklist_num"></span><span class="tracklist_title"><span itemprop="name">- Part A</span></span><span class="tracklist_duration" itemprop="duration" content="PT3M17S">3:17</span></li>
        <li class="track"><span class="tracklist_num"></span><span class="tracklist_title"><span itemprop="name">- [silence]</span></span><span class="tracklist_duration" itemprop="duration" content="PT1M53S">1:53</span></li>
        </ul><div class="tracklist_total">Total length: 5:10</div>'''
        result = c.parse_page(PAGE.replace(old, new), c.BASE)
        self.assertEqual(result['track_count'], 1)
        self.assertEqual(result['tracks'], [{'title': 'Main', 'duration_seconds': None}])
        self.assertEqual(
            result['tracklist_entries'],
            [
                {'number': '1', 'title': 'Main', 'duration_seconds': None},
                {'number': None, 'title': '- Part A', 'duration_seconds': 197},
                {'number': None, 'title': '- [silence]', 'duration_seconds': 113},
            ],
        )
        self.assertNotIn('inconsistent:duration_vs_track_sum', result['warnings'])

if __name__ == '__main__': unittest.main()
