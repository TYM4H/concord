from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from concord import review_store
from concord.new_screen import create_run
from concord.rules import Rules
from concord.runs import export_run
from concord.viewer import index_page, scoped_html
from concord.workspace import Workspace, run_key


class WorkspaceTests(unittest.TestCase):
    def write_result(self, path):
        rules = dict(left_id="id", right_id="key", fields=[
            dict(left="code", right="name", mode="text", weight=1)])
        header = dict(type="run", version=1, rules=rules)
        row = dict(type="result", left_id=1, right_id=7, status="review")
        path.write_text(json.dumps(header) + "\n" + json.dumps(row)[:-1] + "  }\n", encoding="utf-8")

    def test_history_checks_digest_once_when_reopening_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "a #1.jsonl"
            self.write_result(path)
            with closing(Workspace(path, root)):
                pass
            with patch.object(review_store, "_file_digest", wraps=review_store._file_digest) as digest:
                with closing(Workspace(None, root)) as ws:
                    self.assertEqual(ws.listing()[0]["counts"]["review"], 1)
                    digest.assert_called_once_with(path.resolve())

    def test_history_fallback_preserves_files_and_open_rejects_stale_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with closing(Workspace(None, root)) as ws:
                plain = root / "plain.jsonl"
                self.write_result(plain)
                self.assertEqual(ws.listing()[0]["counts"]["review"], 1)
                self.assertEqual(list(root.iterdir()), [plain])
                old = root / "old.jsonl"
                self.write_result(old)
                old_index = review_store.review_path(old)
                old_index.write_bytes(b"older or unreadable index")
                counts = {item["name"]: item["counts"] for item in ws.listing()}
                self.assertEqual(counts[old.name]["review"], 1)
                self.assertEqual(old_index.read_bytes(), b"older or unreadable index")
                path = root / "indexed.jsonl"
                self.write_result(path)
                with closing(sqlite3.connect(review_store.review_path(path))) as db:
                    review_store.load_run(path, db)
                counts = {item["name"]: item["counts"] for item in ws.listing()}
                self.assertEqual(counts[path.name]["review"], 1)
                stamp = path.stat()
                path.write_bytes(path.read_bytes().replace(b'"review"  ', b'"accepted"'))
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(path.stat().st_size, stamp.st_size)
                with self.assertRaisesRegex(ValueError, "отличается"):
                    ws.open(path)
                counts = {item["name"]: item["counts"] for item in ws.listing()}
                self.assertEqual(counts[path.name], dict(accepted=1, review=0, unmatched=0))

    def test_history_review_and_repeat_from_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = root / "первый.jsonl"
            rules = Rules.parse(dict(left_id="id", right_id="key", fields=[
                dict(left="code", right="name", mode="text", weight=70),
                dict(left="tag", right="tag", mode="exact", weight=30)]))
            export_run(initial, [dict(id="001", code="AB", tag="L"), dict(id=7)],
                       [dict(key="004", name="ab", tag="R")], rules, {})
            with closing(Workspace(initial, root)) as ws:
                current = ws.open(initial)
                form = dict(left_id=["id"], right_id=["key"], field_left=["code", "tag", ""],
                            field_right=["name", "tag", ""], field_mode=["text", "exact", "text"],
                            field_weight=["80", "20", "1"], left_kind=["snapshot"], right_kind=["snapshot"],
                            block_limit=["64"], review=["65"], accept=["85"])
                path = create_run(ws, current, form)
                self.assertTrue(path.is_file())
                self.assertEqual(len(ws.listing()), 2)
                new = ws.by_key(run_key(path))
                self.assertEqual(new.rules.fields[0].weight, 80)
                self.assertEqual(current.rules.fields[0].weight, 70)
                page = index_page(new.db, new.rules, new.counts, {"id": ["004"], "min_score": ["75"]}, new.snapshot)
                self.assertIn("80%", page)
                self.assertIn("AB", page)
                self.assertIn("1–1 из 1", page)
                empty = index_page(current.db,current.rules,current.counts,{"min_score":["75"]})
                self.assertIn("0–0 из 0", empty)
                self.assertIsNone(ws.by_key("../other"))
                relaxed_form = dict(form, field_block_on_conflict=["block", "score_only", "block"], accept=["75"])
                relaxed = ws.open(create_run(ws, current, relaxed_form))
                self.assertEqual(relaxed.counts["accepted"], 1)
                self.assertIs(relaxed.rules.fields[1].block_on_conflict, False)
                legacy_form = dict(form, accept=["75"])
                repeated = ws.open(create_run(ws, relaxed, legacy_form))
                self.assertEqual(repeated.counts["accepted"], 1)
                self.assertIs(repeated.rules.fields[1].block_on_conflict, False)
            with closing(Workspace(initial, root)) as ws:
                self.assertEqual(ws.by_key(run_key(path)).counts["review"], 1)

    def test_historical_forms_links_and_stylesheets(self):
        body = '<a href="/rules">R</a><form action="/decision"><button formaction="/new/suggest">S</button></form>'
        body += '<a href="/runs/other/">H</a><link href="/style.css">'
        scoped = scoped_html(body, "/runs/abc")
        for path in ("/runs/abc/rules", "/runs/abc/decision", "/runs/abc/new/suggest", "/runs/other/", "/style.css"):
            self.assertIn('="' + path + '"', scoped)
