"""Candidate URL heuristics and conservative release identity checks."""
import html
import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit, urlunsplit


def ascii_url_candidate(url):
    """Transliterate Latin URL characters without dropping other scripts."""
    if not url:
        return None
    parts = urlsplit(url)
    path = unicodedata.normalize('NFC', unquote(parts.path))
    table = str.maketrans({'ø': 'o', 'Ø': 'O', 'ł': 'l', 'Ł': 'L',
                          'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE',
                          'ß': 'ss', 'ð': 'd', 'Ð': 'D', 'þ': 'th', 'Þ': 'Th'})
    converted = []
    for char in path:
        if 'LATIN' in unicodedata.name(char, ''):
            char = ''.join(c for c in unicodedata.normalize('NFKD', char.translate(table))
                           if not unicodedata.combining(c))
        converted.append(char)
    path_ascii = unicodedata.normalize('NFC', ''.join(converted))
    if path_ascii == unicodedata.normalize('NFC', path):
        return None
    return urlunsplit(parts._replace(path=quote(path_ascii, safe='/-')))


def retry_job_url(db, job):
    """Retry missing albums as EPs, or transliterate unverified candidates."""
    error = (job['error'] or '').lower()
    if (job['url_status'] not in ('candidate', 'candidate_ascii') or not job['attempts']
            or job['status'] == 'blocked'
            or any(s in error for s in ('blocked', 'challenge', 'captcha', '401', '403', '429', 'identity:'))):
        return job
    alternative = None
    url_status = 'candidate_ascii'
    if re.search(r'\b(?:404|410)\b|page not found', error):
        parts = urlsplit(job['url'] or '')
        if parts.path.startswith('/release/album/'):
            alternative = urlunsplit(parts._replace(path=parts.path.replace('/release/album/', '/release/ep/', 1)))
            url_status = 'candidate_ep'
    if alternative is None and job['url_status'] == 'candidate':
        alternative = ascii_url_candidate(job['url'])
    if alternative:
        with db:
            db.execute('UPDATE jobs SET url=?,url_status=? WHERE id=?',
                       (alternative, url_status, job['id']))
        print(f'[{job["id"]}] 重试候选地址：{alternative}', flush=True)
        return db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
    return job


def artist_name(source, localized=False):
    source = {key.strip(): value for key, value in source.items()}
    suffix = ' localized' if localized else ''
    return html.unescape(' '.join(filter(None, [source.get('First Name' + suffix), source.get('Last Name' + suffix)])))


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', html.unescape(value)).casefold() if c.isalnum())


def candidate_url(source):
    def slug(value):
        value = unicodedata.normalize('NFKC', html.unescape(value)).lower()
        # Apostrophes in contractions disappear instead of separating words.
        value = value.translate(str.maketrans('', '', "'\u2019"))
        # RYM represents a slash in a release/artist name with an underscore.
        # Clean each side separately so literal underscores keep their existing
        # punctuation behaviour while slash-derived underscores are preserved.
        parts = [re.sub(r'[^\w]+', '-', part.replace('_', '-'), flags=re.UNICODE).strip('-')
                 for part in value.split('/')]
        return quote('_'.join(parts).strip('_'), safe='-_')
    name = artist_name(source)
    # RYM artist slugs can differ from the generic punctuation rules.
    artist_overrides = {'fontaines d.c.': 'fontaines-d_c'}
    artist = artist_overrides.get(name.strip().casefold()) or slug(name)
    title = slug(source.get('Title', ''))
    if not artist or not title:
        return None
    # Album is only a candidate category: the export does not provide type.
    url = f'https://rateyourmusic.com/release/album/{artist}/{title}/'
    return ascii_url_candidate(url) or url


def identity(source, rid, result):
    issues = []
    expected_title = source['Title']
    actual_title = result['title']
    if normalized(expected_title) != normalized(actual_title):
        issues.append(f'identity:title_mismatch_manual_review (expected={expected_title!r}, actual={actual_title!r})')
    expected = {normalized(artist_name(source)), normalized(artist_name(source, True))} - {''}
    actual = {normalized(result.get('artist_credit_raw', '')),
              normalized(' '.join(a['name'] for a in result['artists']))} - {''}
    # RYM displays a single artist's translated name in trailing brackets.
    # Only expand a single artist, so collaborations still require full credit.
    if len(result['artists']) == 1:
        bilingual = re.fullmatch(r'([^\[\]]+?)\s+\[([^\[\]]+)\]',
                                 result['artists'][0]['name'].strip())
        if bilingual:
            actual.update(normalized(name) for name in bilingual.groups())
    if not expected.intersection(actual):
        issues.append('identity:artist_mismatch_manual_review')
    # Genre/descriptor voting links identify the active release entry. Cover
    # classes can refer to related artwork/version IDs and are diagnostic only
    # whenever a voting ID is available.
    page_ids = result.get('vote_album_ids') or result.get('page_release_ids', [])
    # Conflicting cover/version and voting IDs are deliberately retained for
    # manual review; do not silently choose a preferred edition here.
    if page_ids and page_ids != [str(rid)]:
        issues.append(f'identity:release_id_mismatch_manual_review (expected={str(rid)!r}, actual={page_ids!r})')
    return {'matched': not issues, 'basis': 'id_title_artist' if page_ids else 'title_artist_only',
            'expected_id': str(rid), 'page_ids': page_ids, 'issues': issues}


def is_legacy_url(url):
    return urlsplit(url or '').path.rstrip('/') == '/release/ac'
