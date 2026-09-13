"""Open ordinary Edge; import a user-saved page without browser automation."""
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import time

from ..release_urls import retry_job_url
from ..cli import export, identity_mismatch, ingest, parse_page, safe_url


def open_edge(url):
    url = safe_url(url)
    if os.name == 'nt':
        # Ask Windows to open the URL in the user's default browser/session.
        # Launching msedge.exe directly can select another Edge profile or
        # create a background window that the user never sees.
        os.startfile(url)
        return
    candidates = [shutil.which('msedge')]
    for name in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
        if os.environ.get(name):
            candidates.append(str(Path(os.environ[name]) / 'Microsoft/Edge/Application/msedge.exe'))
    exe = next((p for p in candidates if p and Path(p).is_file()), None)
    if not exe:
        raise RuntimeError('未找到 Edge，请手动打开上方 URL。')
    # No user-data-dir, debugging port, or Playwright launch parameters.
    subprocess.Popen([exe, '--new-tab', url], shell=False)


def fetch_normal(db, root, limit, minimum, maximum, prompt=input, opener=None):
    if not all(math.isfinite(v) for v in (minimum, maximum)) or minimum < 30 or maximum < minimum or limit < 1:
        raise ValueError('Require finite interval >= 30 seconds and positive limit')
    from ..tab_bridge import TabBridge, close_completed
    if opener is None:
        with TabBridge() as bridge:
            return fetch_normal(db, root, limit, minimum, maximum, prompt, bridge.open)
    jobs = list(db.execute('''SELECT * FROM jobs WHERE status IN ('blocked','awaiting_user')
        OR (status IN ('pending','retry') AND next_try<=?)
        ORDER BY CASE WHEN status IN ('blocked','awaiting_user') THEN 0 ELSE 1 END,id LIMIT ?''', (time.time(), limit)))
    for job in jobs:
        job = retry_job_url(db, job)
        print(f'\n[{job["id"]}] {json.loads(job["source"])["Title"]}\n{job["url"] or "无候选 URL，请手动查找"}', flush=True)
        with db:
            db.execute('UPDATE jobs SET status="awaiting_user",error="等待保存的页面 HTML" WHERE id=?', (job['id'],))
        export(db, root)
        tab = None
        if job['url']:
            last = db.execute('SELECT value FROM settings WHERE key="last_request"').fetchone()
            if last:
                time.sleep(max(0, float(last[0]) + random.uniform(minimum, maximum) - time.time()))
            try:
                tab = opener(job['url'])
            except (OSError, RuntimeError) as exc:
                print(str(exc), flush=True)
                return
            else:
                with db:
                    db.execute('INSERT OR REPLACE INTO settings VALUES("last_request",?)', (str(time.time()),))
                    db.execute('UPDATE jobs SET attempts=attempts+1 WHERE id=?', (job['id'],))
        while True:
            try:
                answer = prompt('在日常 Edge 打开正确专辑，Ctrl+S 选择“网页，全部”保存；粘贴 HTML 完整路径后按 Enter（q 退出）：').strip()
            except EOFError:
                return
            if answer.lower() == 'q':
                return
            path = Path(answer.strip('"').strip("'"))
            try:
                raw = path.read_text(encoding='utf-8-sig')
                parse_page(raw, job['url'])
            except (OSError, ValueError) as exc:
                if str(exc).startswith('not_found:'):
                    ingest(db, root, job['id'], raw, job['url'], source_path=path)
                    if tab is not None:
                        tab.close(job['url'])
                    break
                with db:
                    db.execute('UPDATE jobs SET error=? WHERE id=?', (str(exc), job['id']))
                export(db, root)
                print(f'{exc}; queue stopped, progress saved.', flush=True)
                return
            state = ingest(db, root, job['id'], raw, job['url'], source_path=path)
            print(f'已保存：{state}', flush=True)
            if state != 'done' and not identity_mismatch(db, job['id']):
                return
            if state == 'done':
                close_completed(tab, db, job['id'])
            else:
                print(f'[{job["id"]}] IDENTITY MISMATCH; marked partial and skipped.', flush=True)
                if tab is not None:
                    tab.close(job['url'])
            break
    if not jobs:
        print('没有到期的待处理任务。')
