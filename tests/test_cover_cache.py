import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rym_crawler.cover_cache import CoverResponses, cached_cover, import_local_cover, save_cover


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def test_exact_response_bytes_without_download(self):
        page = Mock()
        page.locator.return_value.first.evaluate.return_value = 'https://cdn.sonemic.net/600/cover'
        recorder = CoverResponses(page)
        request = Mock(resource_type='image')
        response = request.response.return_value
        response.url = 'https://cdn.sonemic.net/600/cover'
        response.ok = True
        response.headers = {'content-type': 'image/jpeg'}
        response.body.return_value = b'\xff\xd8\xfftest-cover'
        recorder.finished(request)
        info = recorder.save(self.root, '45')
        self.assertEqual((self.root / info['path']).read_bytes(), response.body.return_value)
        self.assertEqual(info['source_url'], response.url)
        self.assertEqual(info['method'], 'browser_response')
        recorder.save(self.root, '45')
        response.body.assert_called_once()
        page.request.get.assert_not_called()
        page.goto.assert_not_called()
        recorder.close()
        page.remove_listener.assert_called_once()
    def test_missing_response_no_fallback(self):
        page = Mock()
        page.locator.return_value.first.evaluate.return_value = 'https://cdn.sonemic.net/missing'
        info = CoverResponses(page).save(self.root, '45')
        self.assertEqual(info['status'], 'not_captured')
        page.request.get.assert_not_called()
    def test_local_cover_and_integrity(self):
        image = self.root / 'cover.jpg'
        image.write_bytes(b'\xff\xd8\xfflocal')
        raw = '<div class="coverart_45"><img src="cover.jpg"></div>'
        info = import_local_cover(self.root, '45', self.root / 'page.html', raw, 'https://example.test/cover')
        self.assertEqual(info['method'], 'saved_html_resource')
        self.assertEqual(cached_cover(self.root, '45')['status'], 'saved')
        (self.root / info['path']).write_bytes(b'corrupt')
        self.assertEqual(cached_cover(self.root, '45')['status'], 'not_captured')
    def test_reject_html_and_local_traversal(self):
        with self.assertRaises(ValueError):
            save_cover(self.root, '45', b'<html>blocked</html>', 'url', 'test')
        with self.assertRaises(ValueError):
            import_local_cover(self.root, '45', self.root / 'page.html', '<img alt="Cover art for X" src="../private.jpg">', 'url')
