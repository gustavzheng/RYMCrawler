import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from rym_crawler import manual_match
from rym_crawler.cli import database


PAGE = '''<html><head><link rel="canonical" href="https://rateyourmusic.com/release/album/a/right/"></head>
<body><main class="release_page"><h1 class="album_title" itemprop="name">Right</h1>
<table class="album_info"><tr><th>Artist</th><td><a href="/artist/a">Artist</a></td></tr></table>
<div class="coverart_22"></div></main></body></html>'''


class ManualMatchTests(unittest.TestCase):
    def test_scan_and_explicit_id_migration(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / "data"; inbox = root / "inbox"; inbox.mkdir()
            db = database(data)
            source = {"RYM Album": "11", "Rating": "8", "Title": "Right", "First Name": "", "Last Name": "Artist"}
            with db: db.execute("INSERT INTO jobs(id,source,url) VALUES(?,?,?)", ("11", json.dumps(source), "https://rateyourmusic.com/release/album/a/right/"))
            db.close()
            page = inbox / "saved.html"; page.write_text(PAGE, encoding="utf-8")
            report = data / "match.json"; manual_match.scan(data, inbox, report)
            doc = json.loads(report.read_text(encoding="utf-8")); decision = doc["files"][0]["decision"]
            decision.update(action="adopt_page_id", new_id="22")
            report.write_text(json.dumps(doc), encoding="utf-8")
            manual_match.apply(data, report)
            db = database(data)
            self.assertIsNone(db.execute("SELECT 1 FROM jobs WHERE id='11'").fetchone())
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id='22'").fetchone()[0], "done")
            self.assertTrue((data / "html" / "22.html").exists())
            db.close()

    def test_name_mismatch_requires_explicit_confirmation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / "data"; inbox = root / "inbox"; inbox.mkdir()
            db = database(data)
            source = {"RYM Album": "11", "Rating": "8", "Title": "Right Extended", "First Name": "", "Last Name": "Artist"}
            with db: db.execute("INSERT INTO jobs(id,source,url) VALUES(?,?,?)", ("11", json.dumps(source), "https://rateyourmusic.com/release/album/a/right/"))
            db.close(); page = inbox / "saved.html"; page.write_text(PAGE, encoding="utf-8")
            decisions = data / "match.json"
            doc = {"files": [{"file": str(page), "decision": {"old_id": "11", "action": "adopt_page_id", "new_id": "22"}}]}
            decisions.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "title/artist does not match"):
                manual_match.apply(data, decisions)
            doc["files"][0]["decision"]["confirm_title_artist_mismatch"] = True
            decisions.write_text(json.dumps(doc), encoding="utf-8")
            manual_match.apply(data, decisions)
            db = database(data); self.assertEqual(db.execute("SELECT status FROM jobs WHERE id='22'").fetchone()[0], "done"); db.close()


if __name__ == "__main__": unittest.main()
