"""Cache existing image bytes only: no HTTP client or image re-downloads."""
import hashlib
import base64
import binascii
import json
import os
from pathlib import Path
import time
from urllib.parse import unquote, unquote_to_bytes, urlsplit

from bs4 import BeautifulSoup

COVER_SELECTOR = '[class^="coverart_"] img, img[itemprop="image"], img[alt^="Cover art for"]'


def save_cover(root, rid, body, source_url, method):
    from .cli import atomic
    if not str(rid).isdigit():
        raise ValueError('Invalid album ID')
    if body.startswith(b'\xff\xd8\xff'):
        extension = 'jpg'
    elif body.startswith(b'\x89PNG\r\n\x1a\n'):
        extension = 'png'
    elif body[:6] in (b'GIF87a', b'GIF89a'):
        extension = 'gif'
    elif body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        extension = 'webp'
    else:
        raise ValueError('Unrecognized image bytes; not saving as a cover')
    path = root / 'covers' / f'{rid}.{extension}'
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        tmp = path.with_suffix(path.suffix + '.tmp')
        with tmp.open('wb') as f:
            f.write(body); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    info = {'status': 'saved', 'path': path.relative_to(root).as_posix(),
            'source_url': source_url, 'bytes': len(body), 'sha256': digest,
            'method': method, 'saved_at': time.time()}
    atomic(root / 'covers' / f'{rid}.json', json.dumps(info, ensure_ascii=False, indent=2))
    return info


def cached_cover(root, rid):
    path = root / 'covers' / f'{rid}.json'
    if path.exists():
        info = json.loads(path.read_text(encoding='utf-8'))
        image_path = (root / info['path']).resolve()
        if image_path.is_relative_to((root / 'covers').resolve()) and image_path.is_file():
            if hashlib.sha256(image_path.read_bytes()).hexdigest() == info['sha256']:
                return info
    return {'status': 'not_captured', 'path': None}


def import_local_cover(root, rid, html_path, raw, source_url):
    image = BeautifulSoup(raw, 'html.parser').select_one(COVER_SELECTOR)
    if image is None:
        return cached_cover(root, rid)
    value = image.get('src', '')
    if value.startswith('data:'):
        try:
            header, encoded = value.split(',', 1)
            if not header.lower().startswith('data:image/') or len(encoded) > 28 * 1024 * 1024:
                raise ValueError('Invalid or oversized embedded cover')
            body = base64.b64decode(unquote_to_bytes(encoded), validate=True) if ';base64' in header.lower() else unquote_to_bytes(encoded)
            return save_cover(root, rid, body, None, 'singlefile_embedded')
        except (binascii.Error, ValueError) as exc:
            raise ValueError('Cannot decode embedded cover: ' + str(exc)) from exc
    src = urlsplit(image.get('src', ''))
    if src.scheme or src.netloc or not src.path:
        return cached_cover(root, rid)
    # Read only a relative file alongside the user-selected saved HTML.
    folder = Path(html_path).resolve().parent
    path = (folder / unquote(src.path)).resolve()
    if not path.is_relative_to(folder):
        raise ValueError('Saved cover path escapes HTML directory')
    if not path.is_file():
        return cached_cover(root, rid)
    # Saved HTML rewrites src to a local path; its original selected URL is
    # unknown. Do not mislabel these bytes as the larger og:image variant.
    info = save_cover(root, rid, path.read_bytes(), None, 'saved_html_resource')
    info.update(local_source=src.path, declared_url=source_url,
                srcset_candidates=image.get('srcset'))
    from .cli import atomic
    atomic(root / 'covers' / f'{rid}.json', json.dumps(info, ensure_ascii=False, indent=2))
    return info


class CoverResponses:
    def __init__(self, page):
        self.page = page
        self.responses = {}
        page.on('requestfinished', self.finished)

    def finished(self, request):
        if request.resource_type == 'image':
            response = request.response()
            if response and response.ok:
                self.responses[response.url] = response
                if len(self.responses) > 256:
                    self.responses.pop(next(iter(self.responses)))

    def save(self, root, rid):
        from playwright.sync_api import Error
        existing = cached_cover(root, rid)
        if existing['status'] == 'saved':
            return existing
        try:
            # Wait for an already requested cover; never scroll, change src,
            # fetch an og:image or request a higher resolution variant.
            self.page.wait_for_function('selector => { const i=document.querySelector(selector); return i && i.complete && i.naturalWidth>0; }', arg=COVER_SELECTOR, timeout=10000)
            src = self.page.locator(COVER_SELECTOR).first.evaluate('(i) => i.currentSrc || i.src')
            response = self.responses.get(src)
            if response is None:
                return {'status': 'not_captured', 'path': None, 'reason': 'No completed response for displayed cover', 'source_url': src}
            if int(response.headers.get('content-length', '0')) > 20 * 1024 * 1024:
                raise ValueError('Cover exceeds 20 MiB cache limit')
            body = response.body()
            if len(body) > 20 * 1024 * 1024:
                raise ValueError('Cover exceeds 20 MiB cache limit')
            return save_cover(root, rid, body, src, 'browser_response')
        except (Error, ValueError) as exc:
            return {'status': 'not_captured', 'path': None, 'reason': str(exc)}

    def close(self):
        self.page.remove_listener('requestfinished', self.finished)
        self.responses.clear()


def attach_cover(db, root, rid, info):
    from .cli import export
    row = db.execute('SELECT result FROM jobs WHERE id=?', (rid,)).fetchone()
    if row and row['result']:
        result = json.loads(row['result'])
        result['cover'] = info
        with db:
            db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(result, ensure_ascii=False), rid))
        export(db, root)
