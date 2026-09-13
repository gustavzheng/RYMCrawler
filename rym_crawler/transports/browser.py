"""Visible browser transport with explicit human verification handoff."""
import json
import math
import random
import time

from ..cli import atomic, export, identity_mismatch, ingest, parse_page, safe_url
from ..release_urls import identity, retry_job_url


def collect_page(db, root, job, page, status=None, prompt=input):
    """Inspect DOM only while paused; never refresh or navigate on Enter."""
    initial_status = status
    while True:
        raw = page.content()
        reason = None
        try:
            safe_url(page.url)
            parsed = parse_page(raw, page.url)
            checked = identity(json.loads(job['source']), job['id'], parsed)
            if not checked['matched']:
                atomic(root / 'html' / f'{job["id"]}.verification.html', raw)
                return ingest(db, root, job['id'], raw, page.url)
        except ValueError as exc:
            reason = str(exc)
        blocked = (reason or '').startswith('blocked:')
        if isinstance(status, int) and status >= 400 and not blocked:
            reason = f'HTTP {status}: manual inspection required'
        if (status in (404, 410) or (reason or '').startswith('not_found:')) and not blocked and page.url.rstrip('/') == (job['url'] or '').rstrip('/'):
            atomic(root / 'html' / f'{job["id"]}.verification.html', raw)
            with db:
                db.execute('UPDATE jobs SET status="not_found",error=?,result=NULL WHERE id=?', ('not_found: page not found', job['id']))
            export(db, root)
            return 'not_found'
        if reason is None:
            atomic(root / 'html' / f'{job["id"]}.response.json', json.dumps({
                'url': page.url, 'captured_at': time.time(), 'transport': 'browser',
                'initial_http_status': initial_status, 'source': 'rendered_dom'}))
            return ingest(db, root, job['id'], raw, page.url)
        atomic(root / 'html' / f'{job["id"]}.verification.html', raw)
        with db:
            db.execute('UPDATE jobs SET status="awaiting_user",error=? WHERE id=?', (reason, job['id']))
        export(db, root)
        print(f'\n[{job["id"]}] {json.loads(job["source"])["Title"]}: {reason}', flush=True)
        return 'awaiting_user'



def run_jobs(db, root, page, jobs, minimum, maximum, prompt=input):
    from playwright.sync_api import Error, TimeoutError as BrowserTimeout
    for job in jobs:
        cached = root / 'html' / f'{job["id"]}.html'
        if cached.exists() and job['status'] not in ('blocked', 'awaiting_user') and job['url_status'] != 'manual_unverified':
            state = ingest(db, root, job['id'], cached.read_text(encoding='utf-8'), job['url'])
            if state in ('done', 'not_found'):
                continue
            if state != 'blocked' and not (state == 'partial' and any(w.startswith('identity:') for w in json.loads(db.execute('SELECT result FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])['warnings'])):
                break
        job = retry_job_url(db, job)
        last = db.execute('SELECT value FROM settings WHERE key="last_request"').fetchone()
        if last:
            time.sleep(max(0, float(last[0]) + random.uniform(minimum, maximum) - time.time()))
        with db:
            db.execute('INSERT OR REPLACE INTO settings VALUES("last_request",?)', (str(time.time()),))
            db.execute('UPDATE jobs SET attempts=attempts+1 WHERE id=?', (job['id'],))
        from ..cover_cache import CoverResponses, attach_cover
        covers = CoverResponses(page)
        try:
            print(f'打开 [{job["id"]}] {json.loads(job["source"])["Title"]}', flush=True)
            response = None
            try:
                if job['url']:
                    response = page.goto(safe_url(job['url']), wait_until='domcontentloaded', timeout=45000)
                else:
                    page.goto('about:blank')
                page.wait_for_selector('.release_page, [itemtype$="/MusicAlbum"]', state='attached', timeout=10000)
            except BrowserTimeout:
                # A verification/loading page must stay open for the user.
                pass
            state = collect_page(db, root, job, page, response.status if response else None, prompt)
            if state in ('done', 'partial'):
                details = json.loads(db.execute('SELECT result FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
                if not any(w.startswith('identity:') for w in details['warnings']):
                    attach_cover(db, root, job['id'], covers.save(root, job['id']))
            # Start the next interval after human interaction, not before it.
            with db:
                db.execute('INSERT OR REPLACE INTO settings VALUES("last_request",?)', (str(time.time()),))
            if state not in ('done', 'not_found') and not identity_mismatch(db, job['id']):
                break
        except Error as exc:
            current = db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
            state = current if current == 'awaiting_user' else ('failed' if job['attempts'] >= 2 else 'retry')
            with db:
                db.execute('UPDATE jobs SET status=?,error=?,next_try=? WHERE id=?',
                           (state, str(exc), time.time() + 300 * 2 ** min(job['attempts'], 5), job['id']))
            export(db, root)
            print('浏览器已关闭或访问失败；进度已保存。', flush=True)
            break
        finally:
            covers.close()


def fetch_browser(db, root, limit, minimum, maximum, channel='msedge'):
    if not all(math.isfinite(v) for v in (minimum, maximum)) or minimum < 30 or maximum < minimum or limit < 1:
        raise ValueError('Require finite interval >= 30 seconds and positive limit')
    jobs = list(db.execute('''SELECT * FROM jobs WHERE
        status IN ('blocked','awaiting_user') OR
        (status IN ('pending','retry') AND attempts<3 AND next_try<=?)
        ORDER BY CASE WHEN status IN ('blocked','awaiting_user') THEN 0 ELSE 1 END, id LIMIT ?''', (time.time(), limit)))
    if not jobs:
        print('没有到期的待处理任务。')
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError('Install requirements.txt before browser fetch') from exc
    # Dedicated persistent profile; never attach to the user's personal profile.
    profile = root.resolve() / f'browser-profile-{channel}'
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile), channel=channel, headless=False, locale='en-US')
        try:
            page = context.pages[0] if context.pages else context.new_page()
            run_jobs(db, root, page, jobs, minimum, maximum)
        finally:
            context.close()
