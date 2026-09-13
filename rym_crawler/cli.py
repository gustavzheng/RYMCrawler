"""Offline-first RYM enrichment. Network is available only through explicit fetch."""
import argparse
from contextlib import closing
import csv
import html
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit, parse_qs
from .release_urls import artist_name, candidate_url, identity, is_legacy_url, retry_job_url

from bs4 import BeautifulSoup

BASE = 'https://rateyourmusic.com'
VERSION = 10


def safe_url(url):
    u = urlsplit(urljoin(BASE, url))
    if u.scheme != 'https' or u.netloc != 'rateyourmusic.com' or not re.fullmatch(r'/release/[^/]+/[^/]+/[^/]+/', u.path):
        raise ValueError('Unsupported RYM release URL: ' + url)
    return u.geturl()


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def database(root):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'state.sqlite3')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT, url TEXT, status TEXT DEFAULT "pending", attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, error TEXT, result TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
    if 'url_status' not in columns:
        db.commit()
        with closing(sqlite3.connect(root / 'state.before-url-migration.sqlite3')) as backup:
            db.backup(backup)
        with db:
            db.execute('ALTER TABLE jobs ADD COLUMN url_status TEXT DEFAULT "candidate"')
            for row in list(db.execute('SELECT * FROM jobs')):
                if is_legacy_url(row['url']):
                    db.execute('UPDATE jobs SET url=?,url_status="candidate" WHERE id=?',
                               (candidate_url(json.loads(row['source'])), row['id']))
                else:
                    db.execute('UPDATE jobs SET url_status="manual_unverified" WHERE id=?', (row['id'],))
    with db:
        for row in list(db.execute('SELECT id,source FROM jobs WHERE url_status="candidate"')):
            db.execute('UPDATE jobs SET url=? WHERE id=?', (candidate_url(json.loads(row['source'])), row['id']))
    return db


def prepare(db, path):
    total = rated = 0
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not {'RYM Album', 'Rating', 'Title'} <= set(reader.fieldnames or []):
            raise ValueError('Missing required CSV columns')
        with db:
            for row in reader:
                total += 1
                rating = float(row['Rating'] or 0)
                if not math.isfinite(rating) or not 0 <= rating <= 10:
                    raise ValueError(f'Invalid rating at record {total}')
                if rating == 0:
                    continue
                rid = row['RYM Album'].strip()
                if not rid.isdigit():
                    raise ValueError(f'Invalid album ID at record {total}')
                rated += 1
                db.execute('INSERT INTO jobs(id,source,url) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET source=excluded.source',
                           (rid, json.dumps(row, ensure_ascii=False), candidate_url(row)))
    return {'csv_rows': total, 'nonzero_rows': rated, 'unique_jobs': db.execute('SELECT count(*) FROM jobs').fetchone()[0]}


def duration(value):
    value = value or ''
    m = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', value.strip())
    if m and any(m.groups()):
        return sum(int(x or 0) * n for x, n in zip(m.groups(), [3600, 60, 1]))
    if re.fullmatch(r'\d+:\d{2}(?::\d{2})?', value.strip()):
        result = 0
        for part in value.strip().split(':'):
            result = result * 60 + int(part)
        return result
    return None


def parse_page(raw, url):
    # SingleFile omits optional HTML end tags. Use browser-style HTML5
    # tree construction so table cells and microdata keep their scope.
    soup = BeautifulSoup(raw, 'html5lib')
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    if any(x in title for x in ('just a moment', 'captcha', 'access denied', 'security check', '请稍候', '请稍等', '安全验证')) or soup.select_one('#challenge-form, .g-recaptcha, #cf-challenge-running, #challenge-running, .cf-turnstile'):
        raise ValueError('blocked: challenge page')
    release = soup.select_one('.release_page, [itemtype$="/MusicAlbum"]')
    if release is None:
        heading = ' '.join(node.get_text(' ', strip=True) for node in soup.select('title, h1, h2'))
        if re.search(r'\b404\b|\bnot\s+found\b', heading, re.I):
            raise ValueError('not_found: page not found')
        raise ValueError('schema: missing release container')
    def text(node):
        return node.get_text(' ', strip=True) if node else ''
    def prop(name):
        for node in release.select(f'[itemprop="{name}"]'):
            # A recording, artist or review has its own properties. Do not
            # mistake its name/duration/url for an album-level property.
            ancestors = []
            for parent in node.parents:
                if parent is release:
                    break
                if parent.has_attr('itemscope'):
                    ancestors.append(parent)
            if any(not str(p.get('itemtype', '')).endswith('/AggregateRating') for p in ancestors):
                continue
            return node.get('content') or text(node)
        # SingleFile can relocate metadata into head; accept only a unique
        # value and never treat a recording duration as the album duration.
        if soup.head and name in ('name', 'url', 'ratingValue', 'ratingCount', 'numTracks', 'datePublished', 'image'):
            values = {n.get('content') for n in soup.head.select(f'meta[itemprop="{name}"]') if n.get('content')}
            if len(values) == 1:
                return values.pop()
        return None
    rows = {}
    album_info = []
    for row in soup.select('.album_info tr'):
        th, td = row.find('th'), row.find('td')
        if th and td:
            rows[text(th).rstrip(':').lower()] = td
            cells = row.find_all('td', recursive=False)
            album_info.append({'label': text(th).rstrip(':'),
                               'values': [text(cell) for cell in cells],
                               'links': [{'text': text(a), 'url': urljoin(BASE, a['href'])}
                                         for cell in cells for a in cell.select('a[href]')]})
    artist = rows.get('artist') or rows.get('artists') or release.select_one('[itemprop="byArtist"]')
    artists = []
    for a in artist.select('a[href*="/artist/"]') if artist else []:
        name = text(a)
        # User's preferred credit for Ahmed; retain the full page credit below.
        if text(a.select_one('.subtext')) == '[Ahmed]':
            name = '[Ahmed]'
        elif name == '许嵩 [Xu Song]':
            name = '许嵩'
        item = {'name': name, 'url': urljoin(BASE, a['href'])}
        if item not in artists:
            artists.append(item)
    if not artists and artist:
        for credit in artist.select('.credited_name [itemprop="name"]'):
            name = text(credit)
            item = {'name': name, 'url': None}
            if name and item not in artists:
                artists.append(item)
    def genres(selector):
        return list(dict.fromkeys(text(n) for n in soup.select(selector) if text(n)))
    def number(value, integer=False):
        if value is None:
            return None
        cleaned = re.sub(r'[,\s]', '', value)
        try:
            n = float(cleaned)
            if not math.isfinite(n) or n < 0 or (integer and not n.is_integer()):
                return None
            return int(n) if integer else n
        except ValueError:
            return None
    tracks = []
    tracklist_entries = []
    for li in soup.select('#tracks li'):
        number_node = li.select_one('.tracklist_num')
        track_number = text(number_node)
        container = li.select_one('.tracklist_title')
        name = (container.select_one('[itemprop="name"], .rendered_text') or container) if container else None
        if not name or name.find_parent('li') is not li:
            continue
        length = li.select_one('.tracklist_duration, [itemprop="duration"]')
        visible_length = text(length) if length else ''
        track_duration = duration(length.get('content') or visible_length) if length else None
        # RYM uses PT0M00S as a placeholder when no standalone duration exists.
        if track_duration == 0 and not visible_length:
            track_duration = None
        clean_name = BeautifulSoup(str(name), 'html.parser')
        for noise in clean_name.select('.tracks_lyric_link, .tracklist_duration, meta'):
            noise.decompose()
        entry = {'number': track_number or None, 'title': text(clean_name),
                 'duration_seconds': track_duration}
        tracklist_entries.append(entry)
        # Preserve blank-number sections/subtracks above without inflating track_count.
        if number_node is None or track_number:
            tracks.append({'title': entry['title'], 'duration_seconds': track_duration})
    total_raw = text(soup.select_one('.tracklist_total_length, .tracklist_total'))
    total_match = re.search(r'\b\d+:\d{2}(?::\d{2})?\b', total_raw)
    seconds = next((v for v in (duration(prop('duration')), duration(text(rows.get('length'))), duration(total_match[0]) if total_match else None) if v is not None), None)
    duration_source = 'page' if seconds is not None else None
    if seconds is None and tracks and all(t['duration_seconds'] is not None for t in tracks):
        seconds = sum(t['duration_seconds'] for t in tracks)
        duration_source = 'track_sum'
    canonical = soup.select_one('link[rel="canonical"]')
    og_url = soup.find('meta', property='og:url')
    page_url = canonical.get('href') if canonical else None
    page_url = page_url or prop('url') or (og_url.get('content') if og_url else None) or url
    ranked_raw = text(rows.get('ranked')) or None
    cover_release_ids = set()
    for node in release.select('[class]'):
        for name in node.get('class', []):
            match = re.fullmatch(r'coverart_(\d+)', name)
            if match:
                cover_release_ids.add(match[1])
    vote_album_ids = set()
    current_link_ids = set()
    for link in release.select('a[href]'):
        target = urlsplit(urljoin(BASE, link['href']))
        if target.netloc == 'rateyourmusic.com' and target.path in ('/releases/ac', '/admin/corq/'):
            current_link_ids.update(x for x in parse_qs(target.query).get('album_id', []) if x.isdigit())
        if target.netloc == 'rateyourmusic.com' and target.path in ('/rgenre/set', '/rdescriptor/set'):
            vote_album_ids.update(x for x in parse_qs(target.query).get('album_id', []) if x.isdigit())
    page_ids = cover_release_ids | vote_album_ids | current_link_ids
    ranks = []
    for match in re.finditer(r'#\s*([\d,]+)\s+(?:for\s+(\d{4})\b|(overall)\b)', ranked_raw or '', re.I):
        ranks.append({'position': int(match[1].replace(',', '')),
                      'scope': 'year' if match[2] else 'overall',
                      'year': int(match[2]) if match[2] else None})
    data = {'title': prop('name') or text(soup.select_one('.album_title')), 'artists': artists,
            'page_release_ids': sorted(page_ids),
            'cover_release_ids': sorted(cover_release_ids | current_link_ids),
            'vote_album_ids': sorted(vote_album_ids),
            'cover_url': (soup.find('meta', property='og:image') or {}).get('content') or prop('image'),
            'artist_credit_raw': text(artist), 'release_date_raw': text(rows.get('released')) or prop('datePublished'),
            'release_type': text(rows.get('type')) or None,
            'recorded_raw': text(rows.get('recorded')) or None,
            'ranked_raw': ranked_raw, 'ranks': ranks,
            'language_raw': text(rows.get('language') or rows.get('languages')) or None,
            'album_info': album_info,
            'community_rating': number(prop('ratingValue')), 'rating_count': number(prop('ratingCount'), True),
            'primary_genres': genres('.release_pri_genres a.genre'), 'secondary_genres': genres('.release_sec_genres a.genre'),
            'descriptors': [x.strip() for x in text(soup.select_one('.release_pri_descriptors')).split(',') if x.strip()],
            'duration_seconds': seconds, 'duration_source': duration_source,
            'track_count': number(prop('numTracks'), True) or (len(tracks) if tracks else None),
            'tracks': tracks, 'tracklist_entries': tracklist_entries,
            'canonical_url': safe_url(page_url) if page_url != url else url,
            'parser_version': VERSION, 'parsed_at': time.time()}
    missing = [k for k in ('title', 'artists', 'release_date_raw', 'release_type', 'community_rating', 'rating_count', 'primary_genres', 'descriptors', 'duration_seconds', 'track_count') if data[k] is None or data[k] == '' or data[k] == []]
    if data['community_rating'] is not None and data['community_rating'] > 5:
        missing.append('community_rating_out_of_range')
    required = {'title', 'artists', 'community_rating_out_of_range'}
    data['warnings'] = [('missing_or_invalid:' if k in required else 'missing_optional:') + k for k in missing]
    if tracks and data['track_count'] != len(tracks):
        data['warnings'].append('inconsistent:track_count')
    if tracks and all(t['duration_seconds'] is not None for t in tracks):
        track_sum = sum(t['duration_seconds'] for t in tracks)
        unnumbered_sum = sum(e['duration_seconds'] or 0 for e in tracklist_entries
                             if e['number'] is None)
        if seconds not in (track_sum, track_sum + unnumbered_sum):
            data['warnings'].append('inconsistent:duration_vs_track_sum')
    return data


def export(db, root):
    records = []
    for row in db.execute('SELECT * FROM jobs ORDER BY CAST(id AS INTEGER)'):
        source = json.loads(row['source'])
        records.append({'rym_id': row['id'], 'source': source, 'source_artist_decoded': artist_name(source),
                        'status': row['status'], 'error': row['error'], 'url': row['url'], 'url_status': row['url_status'], 'details': json.loads(row['result']) if row['result'] else None})
    atomic(root / 'enriched.json', json.dumps(records, ensure_ascii=False, indent=2))
    import io
    out = io.StringIO(newline='')
    fields = ['rym_id', 'user_rating', 'title', 'status', 'error', 'url', 'artists', 'artist_credit_raw', 'release_date_raw', 'release_type', 'recorded_raw', 'ranked_raw', 'ranks', 'language_raw', 'community_rating', 'rating_count', 'primary_genres', 'secondary_genres', 'descriptors', 'duration_seconds', 'track_count', 'tracks', 'tracklist_entries', 'album_info', 'warnings']
    fields += ['cover_url', 'cover', 'url_status', 'identity']
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for r in records:
        d = r['details'] or {}
        flat = {k: d.get(k, r.get(k, '')) for k in fields}
        flat.update(user_rating=r['source']['Rating'], title=d.get('title') or r['source']['Title'])
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in flat.items()})
    atomic(root / 'enriched.csv', '\ufeff' + out.getvalue())


def ingest(db, root, rid, raw, url, source_path=None):
    job = db.execute('SELECT source FROM jobs WHERE id=?', (rid,)).fetchone()
    if not job:
        raise ValueError('Unknown album ID')
    atomic(root / 'html' / f'{rid}.html', raw)
    try:
        result = parse_page(raw, url)
        result['identity'] = identity(json.loads(job['source']), rid, result)
        result['warnings'].extend(result['identity']['issues'])
        from .cover_cache import cached_cover, import_local_cover
        result['cover'] = cached_cover(root, rid)
        if source_path and not any(w.startswith('identity:') for w in result['warnings']):
            try:
                result['cover'] = import_local_cover(root, rid, source_path, raw, result['cover_url'])
            except ValueError as exc:
                result['cover'] = {'status': 'not_captured', 'path': None, 'reason': str(exc)}
        # Consistency notes are useful diagnostics, but only missing/invalid
        # required fields make a record incomplete.
        incomplete = any(w.startswith(('missing_or_invalid:', 'identity:')) for w in result['warnings'])
        state, error = ('partial' if incomplete else 'done'), None
    except ValueError as exc:
        result, error = None, str(exc)
        state = 'blocked' if error.startswith('blocked:') else ('not_found' if error.startswith('not_found:') else 'parse_error')
    with db:
        db.execute('UPDATE jobs SET status=?,error=?,result=? WHERE id=?', (state, error, json.dumps(result, ensure_ascii=False) if result else None, rid))
        if result and result['identity']['matched']:
            try:
                confirmed = safe_url(result['canonical_url'])
            except (ValueError, TypeError):
                confirmed = None
            if confirmed:
                db.execute('UPDATE jobs SET url=?,url_status=? WHERE id=?',
                           (confirmed, result['identity']['basis'], rid))
    export(db, root)
    return state


def identity_mismatch(db, rid):
    """Return whether a partial job is specifically an identity mismatch."""
    row = db.execute('SELECT status,result FROM jobs WHERE id=?', (rid,)).fetchone()
    if not row or row['status'] != 'partial' or not row['result']:
        return False
    return any(w.startswith('identity:') for w in json.loads(row['result']).get('warnings', []))


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(db, root, limit, minimum, maximum):
    if minimum < 30 or maximum < minimum or limit < 1:
        raise ValueError('Require interval >= 30 seconds and positive limit')
    if db.execute('SELECT 1 FROM jobs WHERE status="blocked"').fetchone():
        raise ValueError('Blocked job exists: inspect cached page before explicit reset')
    opener = urllib.request.build_opener(Redirects())
    jobs = list(db.execute('SELECT * FROM jobs WHERE status IN ("pending","retry") AND attempts<3 AND next_try<=? ORDER BY id LIMIT ?', (time.time(), limit)))
    for job in jobs:
        cached = root / 'html' / f'{job["id"]}.html'
        if cached.exists() and job['url_status'] != 'manual_unverified':
            status = ingest(db, root, job['id'], cached.read_text(encoding='utf-8'), job['url'])
            if status not in ('done', 'not_found'):
                break
            continue
        job = retry_job_url(db, job)
        last = db.execute('SELECT value FROM settings WHERE key="last_request"').fetchone()
        time.sleep(max(0, float(last[0]) + random.uniform(minimum, maximum) - time.time()) if last else 0)
        with db:
            db.execute('INSERT OR REPLACE INTO settings VALUES("last_request",?)', (str(time.time()),))
            db.execute('UPDATE jobs SET attempts=attempts+1 WHERE id=?', (job['id'],))
        try:
            request = urllib.request.Request(safe_url(job['url']), headers={'User-Agent': 'RYMPersonalEnrichment/0.1', 'Accept-Language': 'en-US,en;q=0.9'})
            with opener.open(request, timeout=45) as response:
                raw = response.read().decode('utf-8', errors='replace')
                atomic(root / 'html' / f'{job["id"]}.response.json', json.dumps({'url': response.url, 'fetched_at': time.time(), 'status': response.status}))
                status = ingest(db, root, job['id'], raw, response.url)
            if status in ('blocked', 'parse_error') or (status == 'partial' and not identity_mismatch(db, job['id'])):
                break  # inspect first evidence of schema drift before spending more requests
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            code = getattr(exc, 'code', None)
            status = 'blocked' if code in (401, 403, 429) else ('not_found' if code in (404, 410) else ('failed' if job['attempts'] >= 2 else 'retry'))
            with db:
                db.execute('UPDATE jobs SET status=?,error=?,next_try=? WHERE id=?',
                           (status, str(exc), time.time() + 300 * 2 ** min(job['attempts'], 5), job['id']))
            if status != 'not_found':
                break


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, default=Path('data'))
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('prepare'); s.add_argument('csv')
    sub.add_parser('export')
    s = sub.add_parser('import-html'); s.add_argument('id'); s.add_argument('file', type=Path)
    sub.add_parser('reparse')
    s = sub.add_parser('set-url'); s.add_argument('id'); s.add_argument('url')
    s = sub.add_parser('reset'); s.add_argument('id')
    s = sub.add_parser('fetch'); s.add_argument('--allow-network', action='store_true', required=True); s.add_argument('--limit', type=int, default=1); s.add_argument('--min-delay', type=float, default=60); s.add_argument('--max-delay', type=float, default=120)
    s.add_argument('--transport', choices=['singlefile', 'normal', 'browser', 'http'], default='singlefile')
    s.add_argument('--inbox', type=Path, default=Path.home() / 'Downloads',
                   help='SingleFile 保存目录（默认：当前用户 Downloads）')
    s.add_argument('--wait-timeout', type=float, default=300)
    s.add_argument('--browser', choices=['msedge', 'chrome', 'chromium'], default='msedge')
    args = p.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    # OS lock is released even on forced process termination; no stale lock removal.
    lock = (args.data / 'process.lock').open('a+b')
    lock.write(b'0'); lock.flush(); lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        p.error('Another process is using this data directory')
    db = database(args.data)
    try:
        if args.command == 'prepare':
            print(json.dumps(prepare(db, args.csv)))
        elif args.command == 'import-html':
            row = db.execute('SELECT url FROM jobs WHERE id=?', (args.id,)).fetchone()
            if not row: raise ValueError('Unknown album ID')
            print(ingest(db, args.data, args.id, args.file.read_text(encoding='utf-8-sig'), row['url'], args.file))
        elif args.command == 'reparse':
            for row in list(db.execute('SELECT id,url FROM jobs')):
                path = args.data / 'html' / f'{row["id"]}.html'
                if path.exists(): ingest(db, args.data, row['id'], path.read_text(encoding='utf-8'), row['url'])
        elif args.command in ('set-url', 'reset'):
            with db:
                if args.command == 'set-url':
                    if not db.execute('SELECT 1 FROM jobs WHERE id=?', (args.id,)).fetchone():
                        raise ValueError('Unknown album ID')
                    db.execute('UPDATE jobs SET url=?,url_status="manual_unverified",status="awaiting_user",attempts=0,next_try=0,error=NULL WHERE id=?', (safe_url(args.url), args.id))
                else:
                    db.execute('UPDATE jobs SET status="pending",attempts=0,next_try=0,error=NULL,result=NULL WHERE id=?', (args.id,))
        elif args.command == 'fetch':
            if args.transport == 'singlefile':
                if args.browser != 'msedge':
                    p.error('singlefile 模式当前使用日常 Edge')
                from .transports.singlefile import fetch_singlefile
                fetch_singlefile(db, args.data, args.limit, args.min_delay, args.max_delay, args.inbox, args.wait_timeout)
            elif args.transport == 'normal':
                if args.browser != 'msedge':
                    p.error('normal 模式使用日常 Edge；其他浏览器自动化请显式选择 --transport browser')
                from .transports.edge import fetch_normal
                fetch_normal(db, args.data, args.limit, args.min_delay, args.max_delay)
            elif args.transport == 'browser':
                from .transports.browser import fetch_browser
                fetch_browser(db, args.data, args.limit, args.min_delay, args.max_delay, args.browser)
            else:
                fetch(db, args.data, args.limit, args.min_delay, args.max_delay)
    except KeyboardInterrupt:
        print('Interrupted; committed records and cached HTML are retained.')
    finally:
        try:
            export(db, args.data)
        finally:
            db.close()
            lock.close()


if __name__ == '__main__':
    main()
