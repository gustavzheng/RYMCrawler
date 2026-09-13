"""Loopback-only handshake with the unpacked RYM tab companion extension."""
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cli import safe_url


class ManagedTab:
    def __init__(self, url):
        self.url = safe_url(url)
        self.done_url = None
        self.registered = threading.Event()
        self.closed = threading.Event()

    def close(self, url, timeout=45):
        self.done_url = safe_url(url)
        if not self.closed.wait(timeout):
            raise RuntimeError('数据已保存，但未收到标签页关闭确认；停止队列，请检查 RYM Tab Companion 扩展。')


class TabBridge:
    def __init__(self, opener=webbrowser.open, timeout=30):
        self.tabs = {}
        self.opener = opener
        self.timeout = timeout

    def __enter__(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = self.path.strip('/').split('/')
                tab = bridge.tabs.get(parts[0]) if len(parts) == 2 else None
                if not tab or parts[1] != 'state':
                    self.send_error(404)
                    return
                body = json.dumps({'url': tab.url, 'done_url': tab.done_url}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # No CORS: ordinary websites cannot send these authenticated commands.
                parts = self.path.strip('/').split('/')
                tab = bridge.tabs.get(parts[0]) if len(parts) == 2 else None
                if (not tab or self.headers.get('X-RYM-Bridge') != '1'
                        or parts[1] not in ('registered', 'closed')
                        or (parts[1] == 'closed' and not tab.done_url)):
                    self.send_error(403)
                    return
                (tab.registered if parts[1] == 'registered' else tab.closed).set()
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def open(self, url):
        token = secrets.token_urlsafe(32)
        tab = self.tabs[token] = ManagedTab(url)
        # A fragment is not sent in the HTTP request. The extension consumes it
        # on the real release page; no intermediate HTML is exposed to SingleFile.
        from urllib.parse import urldefrag
        self.opener(f'{urldefrag(tab.url)[0]}#rym-crawler={self.server.server_port}.{token}')
        if not tab.registered.wait(self.timeout):
            del self.tabs[token]
            raise RuntimeError('未连接 RYM Tab Companion；请加载 edge-tab-companion 扩展后重跑，队列已停止。')
        return tab

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def close_completed(tab, db, rid):
    """Only a committed, complete record authorizes closing its owned tab."""
    if tab is None:
        return
    row = db.execute('SELECT status,url FROM jobs WHERE id=?', (rid,)).fetchone()
    if row['status'] == 'done':
        tab.close(row['url'])
        print(f'[{rid}] 已保存并关闭采集标签页。', flush=True)
