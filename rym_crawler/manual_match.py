"""Safely match manually saved RYM pages to crawler jobs, then import them.

Scanning is read-only. Applying requires an explicit decisions JSON file; the
script never guesses whether an old RYM ID should be retained or replaced.
"""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import shutil

from .release_urls import artist_name, identity, normalized
from .cli import atomic, database, export, ingest, parse_page, safe_url


def page_artist(parsed):
    return parsed.get("artist_credit_raw") or " ".join(a["name"] for a in parsed.get("artists", []))


def scan(data, inbox, output):
    db = database(data)
    jobs = []
    for row in db.execute("SELECT * FROM jobs WHERE status != 'done'"):
        source = json.loads(row["source"])
        jobs.append((row, source, normalized(source.get("Title", "")), {
            normalized(artist_name(source)), normalized(artist_name(source, True))
        } - {""}))
    report = {"generated_at": datetime.now().astimezone().isoformat(), "inbox": str(inbox.resolve()), "files": []}
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in (".html", ".htm")):
        item = {"file": str(path.resolve())}
        try:
            raw = path.read_text(encoding="utf-8-sig")
            parsed = parse_page(raw, "https://rateyourmusic.com/release/album/unknown/unknown/")
            actual_artists = {normalized(page_artist(parsed)),
                              normalized(" ".join(a["name"] for a in parsed.get("artists", [])))} - {""}
            candidates = []
            for row, source, title, artists in jobs:
                title_ok = title == normalized(parsed.get("title", ""))
                artist_ok = bool(artists & actual_artists)
                identity_ids = parsed.get("vote_album_ids") or parsed.get("page_release_ids", [])
                id_ok = row["id"] in identity_ids
                if id_ok or (title_ok and artist_ok):
                    candidates.append({"old_id": row["id"], "source_title": source.get("Title"),
                                       "source_artist": artist_name(source), "title_match": title_ok,
                                       "artist_match": artist_ok, "old_id_on_page": id_ok})
            item.update(title=parsed.get("title"), artist=page_artist(parsed),
                        page_ids=parsed.get("page_release_ids", []), vote_ids=parsed.get("vote_album_ids", []),
                        cover_ids=parsed.get("cover_release_ids", []), canonical_url=parsed.get("canonical_url"),
                        candidates=candidates,
                        decision={"old_id": candidates[0]["old_id"] if len(candidates) == 1 else None,
                                  "action": "keep_id", "new_id": None})
        except Exception as exc:
            item["error"] = str(exc)
        report["files"].append(item)
    atomic(output, json.dumps(report, ensure_ascii=False, indent=2))
    db.close()
    print(json.dumps({"report": str(output), "files": len(report["files"]),
                      "unique_matches": sum(len(x.get("candidates", [])) == 1 for x in report["files"]),
                      "errors": sum("error" in x for x in report["files"])}, ensure_ascii=False))


def apply(data, decisions, csv_path=None):
    doc = json.loads(decisions.read_text(encoding="utf-8-sig"))
    selected = [x for x in doc.get("files", []) if x.get("decision", {}).get("old_id")]
    if not selected:
        raise ValueError("No decisions with old_id were supplied")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = data / f"state.before-manual-import-{stamp}.sqlite3"
    with closing(database(data)) as db:
        db.commit()
        with closing(__import__("sqlite3").connect(backup)) as target:
            db.backup(target)
        log = {"applied_at": datetime.now().astimezone().isoformat(), "database_backup": str(backup), "entries": []}
        migrations = {}
        for item in selected:
            decision = item["decision"]
            old_id, action = str(decision["old_id"]), decision.get("action", "keep_id")
            path = Path(item["file"])
            raw = path.read_text(encoding="utf-8-sig")
            parsed = parse_page(raw, "https://rateyourmusic.com/release/album/unknown/unknown/")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (old_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown old_id {old_id}")
            check = identity(json.loads(row["source"]), old_id, parsed)
            name_issues = [x for x in check["issues"]
                           if x.startswith(("identity:title_mismatch", "identity:artist_mismatch"))]
            if name_issues and not decision.get("confirm_title_artist_mismatch", False):
                raise ValueError(f"{path}: title/artist does not match old_id {old_id}: {check['issues']}")
            target_id = old_id
            if action == "adopt_page_id":
                target_id = str(decision.get("new_id") or "")
                identity_ids = parsed.get("vote_album_ids") or parsed.get("page_release_ids", [])
                if not target_id.isdigit() or target_id not in identity_ids:
                    raise ValueError(f"{path}: new_id must be a numeric voting ID (or page ID when no voting ID exists)")
                collision = db.execute("SELECT 1 FROM jobs WHERE id=?", (target_id,)).fetchone()
                if target_id != old_id and collision:
                    raise ValueError(f"Cannot migrate {old_id} to existing job {target_id}")
                if target_id != old_id:
                    source = json.loads(row["source"]); source["RYM Album"] = target_id
                    with db:
                        db.execute("UPDATE jobs SET id=?,source=?,status='pending',result=NULL,error=NULL WHERE id=?",
                                   (target_id, json.dumps(source, ensure_ascii=False), old_id))
                    migrations[old_id] = target_id
            elif action != "keep_id":
                raise ValueError(f"Unsupported action: {action}")
            state = ingest(db, data, target_id, raw, safe_url(parsed["canonical_url"]), path)
            if name_issues and decision.get("confirm_title_artist_mismatch", False):
                saved = db.execute("SELECT result FROM jobs WHERE id=?", (target_id,)).fetchone()
                result = json.loads(saved["result"])
                result["warnings"] = [w for w in result.get("warnings", []) if w not in name_issues]
                result["warnings"].append("manual_confirmation:title_artist_mismatch")
                result["identity"]["issues"] = [w for w in result["identity"].get("issues", []) if w not in name_issues]
                result["identity"]["matched"] = not result["identity"]["issues"]
                incomplete = any(w.startswith(("missing_or_invalid:", "identity:")) for w in result["warnings"])
                state = "partial" if incomplete else "done"
                with db:
                    db.execute("UPDATE jobs SET status=?,result=? WHERE id=?",
                               (state, json.dumps(result, ensure_ascii=False), target_id))
            log["entries"].append({"old_id": old_id, "final_id": target_id, "file": str(path),
                                   "action": action, "state": state, "page_ids": parsed.get("page_release_ids", [])})
        if csv_path and migrations:
            import csv, io
            original = csv_path.read_text(encoding="utf-8-sig")
            reader = csv.DictReader(io.StringIO(original)); rows = list(reader)
            changed = 0
            for row in rows:
                if row.get("RYM Album") in migrations:
                    row["RYM Album"] = migrations[row["RYM Album"]]; changed += 1
            csv_backup = csv_path.with_name(csv_path.name + f".before-id-migration-{stamp}.bak")
            shutil.copy2(csv_path, csv_backup)
            out = io.StringIO(newline=""); writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
            writer.writeheader(); writer.writerows(rows); atomic(csv_path, "\ufeff" + out.getvalue())
            log["csv"] = {"path": str(csv_path), "backup": str(csv_backup), "rows_changed": changed}
        export(db, data)
        log_path = data / f"manual-import-{stamp}.json"
        atomic(log_path, json.dumps(log, ensure_ascii=False, indent=2))
        print(json.dumps({"log": str(log_path), "database_backup": str(backup), "entries": log["entries"]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan"); scan_p.add_argument("inbox", type=Path); scan_p.add_argument("--output", type=Path, default=Path("data/manual-match.json"))
    apply_p = sub.add_parser("apply"); apply_p.add_argument("decisions", type=Path); apply_p.add_argument("--csv", type=Path)
    args = parser.parse_args()
    if args.command == "scan": scan(args.data, args.inbox, args.output)
    else: apply(args.data, args.decisions, args.csv)


if __name__ == "__main__":
    main()
