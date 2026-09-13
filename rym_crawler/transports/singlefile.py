"""Normal Edge + SingleFile downloads; only local files are inspected."""
import hashlib
import json
import math
from pathlib import Path
import random
import time
import re
import shutil

from ..tab_bridge import TabBridge, close_completed
from urllib.parse import urlsplit
from ..release_urls import identity, retry_job_url
from ..cli import atomic, export, identity_mismatch, ingest, parse_page


def countdown(seconds, sleep=time.sleep):
    """Visible rate-limit wait without emitting one log line per second."""
    remaining = max(0, int(math.ceil(seconds)))
    while remaining:
        width = 20
        elapsed = int(width * (seconds - remaining) / seconds) if seconds else width
        bar = '#' * elapsed + '-' * (width - elapsed)
        print(f'\r访问间隔 [{bar}] {remaining:3d} 秒', end='', flush=True)
        sleep(min(1, remaining))
        remaining -= 1
    if seconds > 0:
        print('\r访问间隔 [####################] 完成   ', flush=True)


def saved_source(raw):
    match = re.search(r'(?mi)^\s*url:\s*(https?://\S+)', raw[:4096])
    return match.group(1) if match else None


class BridgeDownload(ValueError):
    """A saved handshake page from the old companion protocol, not album data."""


class UnrelatedDownload(ValueError):
    """A valid release page that cannot be bound to the current job."""


def archive_download(root, path):
    """Move a consumed SingleFile download into the project for later audit."""
    archive = root / 'inbox'
    archive.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() == archive.resolve():
        return path
    target = archive / path.name
    if target.exists():
        target = archive / f'{path.stem}-{time.time_ns()}{path.suffix}'
    try:
        return Path(shutil.move(str(path), str(target)))
    except OSError:
        return path


class Inbox:
    def __init__(self, folder):
        self.folder = folder
        self.samples = {}
        self.seen = set()

    def ready(self):
        """Require unchanged size/mtime across two polls, ignore partial files."""
        for path in sorted(self.folder.glob('*')):
            if path.suffix.lower() not in ('.html', '.htm') or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            sig = (stat.st_size, stat.st_mtime_ns)
            if self.samples.get(path) == sig and stat.st_size:
                key = (str(path), *sig)
                if key not in self.seen:
                    yield path, key
            self.samples[path] = sig


def accept_file(db, root, job, path, allow_redirect=False):
    """Return accepted state or a rejection message; never import wrong IDs."""
    raw = path.read_text(encoding='utf-8-sig')
    if re.fullmatch(r'http://127\.0\.0\.1:\d+/[A-Za-z0-9_-]{43}/start/?', saved_source(raw) or ''):
        raise BridgeDownload('忽略旧版插件握手页')
    try:
        parsed = parse_page(raw, job['url'])
    except ValueError as exc:
        source = saved_source(raw)
        if (str(exc).startswith('not_found:') and source
                and urlsplit(source)._replace(fragment='') == urlsplit(job['url'])._replace(fragment='')):
            return ingest(db, root, job['id'], raw, job['url'], source_path=path)
        raise
    check = identity(json.loads(job['source']), job['id'], parsed)
    source = saved_source(raw)
    expected_id_present = str(job['id']) in parsed.get('page_release_ids', [])
    if (not allow_redirect and not check['matched'] and not expected_id_present and
            (not source or urlsplit(source)._replace(fragment='') !=
             urlsplit(job['url'])._replace(fragment=''))):
        # Only a file proven to come from the current job URL may authorize
        # marking an identity mismatch. Presence of the expected release ID
        # also binds a related/mixed-edition page to the current job.
        raise UnrelatedDownload('; '.join(check['issues']))
    # Save receipt before ingest so an interrupted export can be recovered.
    atomic(root / 'html' / f'{job["id"]}.singlefile.json', json.dumps({
        'file': str(path.resolve()), 'sha256': hashlib.sha256(raw.encode()).hexdigest(),
        'imported_at': time.time(), 'transport': 'singlefile'}, ensure_ascii=False))
    return ingest(db, root, job['id'], raw, job['url'], source_path=path)


def fetch_singlefile(db, root, limit, minimum, maximum, inbox, wait_timeout=300,
                     opener=None, sleep=time.sleep, now=time.monotonic):
    if not all(math.isfinite(v) for v in (minimum, maximum, wait_timeout)) or minimum < 30 or maximum < minimum or limit < 1 or wait_timeout <= 0:
        raise ValueError('Require valid interval >=30s, positive limit and timeout')
    if opener is None:
        with TabBridge() as bridge:
            return fetch_singlefile(db, root, limit, minimum, maximum, inbox,
                                    wait_timeout, bridge.open, sleep, now)
    inbox = Path(inbox).resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    print(f'SingleFile 收件目录：{inbox}\n等待扩展自动保存，无需输入路径；Ctrl+C 保存退出。', flush=True)
    watcher = Inbox(inbox)
    jobs = list(db.execute('''SELECT * FROM jobs WHERE status IN ('blocked','awaiting_user')
        OR (status IN ('pending','retry') AND next_try<=?)
        ORDER BY CASE WHEN status IN ('blocked','awaiting_user') THEN 0 ELSE 1 END,id LIMIT ?''', (time.time(), limit)))
    for job in jobs:
        print(f'[{job["id"]}] {json.loads(job["source"])["Title"]}', flush=True)
        cached = root / 'html' / f'{job["id"]}.html'
        if cached.exists() and job['url_status'] != 'manual_unverified':
            try:
                state = accept_file(db, root, job, cached)
            except (OSError, ValueError):
                pass
            else:
                if state in ('done', 'not_found') or identity_mismatch(db, job['id']):
                    continue
                print('缓存字段不全，请检查后 reparse。', flush=True)
                return
        # Check already saved files first. This recovers interrupted runs
        # without revisiting RYM. Identity, not filenames, binds the task.
        list(watcher.ready())
        sleep(1)
        for path, key in list(watcher.ready()):
            try:
                state = accept_file(db, root, job, path)
            except BridgeDownload:
                watcher.seen.add(key)
                archived = archive_download(root, path)
                continue
            except (OSError, ValueError):
                continue
            archive_download(root, path)
            if state not in ('done', 'not_found') and not identity_mismatch(db, job['id']):
                print('已导入但字段不全，请检查缓存后 reparse。', flush=True)
                return
            break
        else:
            state = None
        if state in ('done', 'not_found') or identity_mismatch(db, job['id']):
            continue
        job = retry_job_url(db, job)
        # Baseline excludes old unrelated or rejected files from live alerts.
        baseline = {key for _, key in watcher.ready()}
        with db:
            db.execute('UPDATE jobs SET status="awaiting_user",error="等待 SingleFile 自动保存" WHERE id=?', (job['id'],))
        export(db, root)
        tab = None
        if job['url']:
            last = db.execute('SELECT value FROM settings WHERE key="last_request"').fetchone()
            if last:
                countdown(max(0, float(last[0]) + random.uniform(minimum, maximum) - time.time()), sleep)
            try:
                tab = opener(job['url'])
            except (OSError, RuntimeError) as exc:
                print(str(exc), flush=True)
                return
            else:
                with db:
                    db.execute('INSERT OR REPLACE INTO settings VALUES("last_request",?)', (str(time.time()),))
                    db.execute('UPDATE jobs SET attempts=attempts+1 WHERE id=?', (job['id'],))
        else:
            print('没有候选地址，请在 Edge 手动定位专辑。', flush=True)
        deadline = now() + wait_timeout
        accepted = False
        while now() < deadline:
            for path, key in list(watcher.ready()):
                if key in baseline or key in watcher.seen:
                    continue
                try:
                    # Files created after this owned tab was opened are allowed
                    # to reveal that a candidate URL redirected to another
                    # release. Pre-existing/cache files remain strictly bound.
                    state = accept_file(db, root, job, path, allow_redirect=True)
                except (OSError, ValueError) as exc:
                    watcher.seen.add(key)
                    archived = archive_download(root, path)
                    if isinstance(exc, BridgeDownload):
                        continue
                    # Only accept_file can authorize skipping a missing page,
                    # after checking its saved source against the current URL.
                    # Unknown pages may be unrecognized verification screens.
                    with db:
                        db.execute('UPDATE jobs SET error=? WHERE id=?', (str(exc), job['id']))
                    export(db, root)
                    print(f'[{job["id"]}] {exc}; queue stopped, progress saved.', flush=True)
                    return
                watcher.seen.add(key)
                archived = archive_download(root, path)
                if state not in ('done', 'not_found') and not identity_mismatch(db, job['id']):
                    print('字段缺失，已保存现场；修复后 reparse。', flush=True)
                    return
                accepted = True
                if state == 'not_found':
                    print(f'[{job["id"]}] NOT FOUND; skipped.', flush=True)
                    if tab is not None:
                        tab.close(job['url'])
                elif state == 'done':
                    close_completed(tab, db, job['id'])
                else:
                    print(f'[{job["id"]}] IDENTITY MISMATCH; marked partial and skipped.', flush=True)
                    if tab is not None:
                        # The extension is now on the redirect destination, not
                        # necessarily the candidate URL that opened the tab.
                        raw = archived.read_text(encoding='utf-8-sig')
                        tab.close(saved_source(raw) or job['url'])
                break
            if accepted:
                break
            sleep(1)
        if not accepted:
            print('等待超时，进度保留，未打开下一条。检查自动保存设置后重跑同一命令。', flush=True)
            return
        # Do not immediately open the next page after lengthy verification.
        with db:
            db.execute('INSERT OR REPLACE INTO settings VALUES("last_request",?)', (str(time.time()),))
    export(db, root)
