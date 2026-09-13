import json
import unittest
import urllib.error
import urllib.request
from urllib.parse import urldefrag
from unittest.mock import Mock

from rym_crawler.tab_bridge import ManagedTab, TabBridge

URL = 'https://rateyourmusic.com/release/album/example/example/'


class BridgeTests(unittest.TestCase):
    def test_handshake_and_close_ack(self):
        def opened(url):
            target, fragment = urldefrag(url)
            self.assertEqual(target, URL)
            self.assertTrue(fragment.startswith('rym-crawler='))
            port, token = fragment.removeprefix('rym-crawler=').split('.')
            base = f'http://127.0.0.1:{port}/{token}/'
            with urllib.request.urlopen(base + 'state') as response:
                self.assertEqual(json.load(response), {'url': URL, 'done_url': None})
            req = urllib.request.Request(base + 'registered', data=b'', headers={'X-RYM-Bridge': '1'})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status, 204)
            self.base = base
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + 'start')
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        with TabBridge(opener=opened, timeout=0.1) as bridge:
            tab = bridge.open(URL)
            with self.assertRaisesRegex(RuntimeError, '数据已保存'):
                tab.close(URL, timeout=0)
            with urllib.request.urlopen(self.base + 'state') as response:
                self.assertEqual(json.load(response)['done_url'], URL)
            req = urllib.request.Request(self.base + 'closed', data=b'', headers={'X-RYM-Bridge': '1'})
            with urllib.request.urlopen(req):
                pass
            tab.close(URL, timeout=0)

    def test_missing_extension_stops(self):
        with TabBridge(opener=Mock(), timeout=0) as bridge:
            with self.assertRaisesRegex(RuntimeError, '未连接'):
                bridge.open(URL)
            self.assertEqual(bridge.tabs, {})

    def test_no_ack_without_authentication_or_completion(self):
        with TabBridge(opener=Mock()) as bridge:
            tab = bridge.tabs['test'] = ManagedTab(URL)
            base = f'http://127.0.0.1:{bridge.server.server_port}/test/'
            for action, headers in [('registered', {}), ('closed', {'X-RYM-Bridge': '1'})]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base + action, data=b'', headers=headers))
                error.exception.close()
            self.assertFalse(tab.registered.is_set())
            self.assertFalse(tab.closed.is_set())
