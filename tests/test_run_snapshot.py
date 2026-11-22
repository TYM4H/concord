from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from concord.matching import match, values
from concord.rules import Rules
from concord.run_snapshot import open_snapshot, snapshot_name, SnapshotUnavailable
from concord.runs import export_run


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "данные #1.jsonl"
        self.rules = Rules.parse(dict(left_id="id", right_id="key", fields=[
            dict(left="code", right="code", mode="exact", weight=90),
            dict(left="tag", right="tag", mode="text", weight=10),
        ], block_limit=2))
        self.left = [{"id": "007", "tag": ["One", "Two"]}, {"id": 7, "code": "X", "tag": "One"}]
        self.right = [{"key": 1, "tag": "one"}, {"key": "1", "tag": " TWO "},
                      {"key": 2, "code": "X", "tag": "One"}, {"key": "007"}]

    def create(self, path=None):
        path = path or self.path
        export_run(path, self.left, self.right, self.rules, {})
        return list(map(json.loads, path.read_text().splitlines()))

    def test_snapshot_matches_real_comparisons(self):
        header, *rows = self.create()
        self.assertEqual([{k: v for k, v in r.items() if k != "type"} for r in rows],
                         list(match(self.left, self.right, self.rules)))
        with closing(open_snapshot(self.path, header)) as db:
            scores = db.execute("SELECT left_id,right_id,score FROM missed ORDER BY left_id,right_id").fetchall()
            self.assertEqual(scores, [('"007"', '"1"', 10.0), ('"007"', '1', 7.07), ('"007"', '2', 7.07),
                                      ('7', '1', 7.07)])
            self.assertEqual(values(db, 1, '"1"')[1]["two"], {"originals": [" TWO "], "counts": [1, 1]})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entity WHERE side=1").fetchone(), (4,))
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM missed")
        snapshot = self.root / snapshot_name(header["diagnostics"]["id"])
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)

    def test_common_value_and_empty_fields(self):
        self.left = [{"id": 1, "tag": "one"}, {"id": 2}]
        self.right = [{"key": i, "tag": "ONE"} for i in range(3)]
        header, *_ = self.create()
        with closing(open_snapshot(self.path, header)) as db:
            counts = values(db, 0, "1")[1]["one"]["counts"]
            self.assertEqual(counts, [1, 3])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM missed").fetchone(), (3,))
            self.assertEqual(values(db, 0, "2"), {})

    def test_v1_matched_record_discloses_partial_list_but_unmatched_list_is_complete(self):
        from concord.review_screen import comparison_page
        from concord.review_store import load_run

        with patch("concord.runs.VERSION", 1), patch("concord.run_snapshot.VERSION", 1):
            header, *_ = self.create()
        path = self.root / snapshot_name(header["diagnostics"]["id"])
        with closing(sqlite3.connect(path)) as old:
            old.execute("DELETE FROM missed WHERE left_id='7'")
            old.commit()
        with closing(open_snapshot(self.path, header)) as snapshot, closing(sqlite3.connect(":memory:")) as review:
            rules, _ = load_run(self.path, review)
            retained = comparison_page(review, rules, 2, "t", snapshot)
            self.assertIn("только сохранённые пары", retained)
            self.assertIn("Вариантов: 1", retained)
            orphan = comparison_page(review, rules, 1, "t", snapshot)
            self.assertIn("Вариантов: 3", orphan)
            self.assertEqual(orphan.count("Ниже порога"), 3)

    def test_missing_mixed_and_changed_result(self):
        header, *_ = self.create()
        other = self.root / "other.jsonl"
        other_header, *_ = self.create(other)
        snapshot = self.root / snapshot_name(header["diagnostics"]["id"])
        other_snapshot = self.root / snapshot_name(other_header["diagnostics"]["id"])
        with self.assertRaises(SnapshotUnavailable):
            open_snapshot(self.path, {})
        for ref in [dict(id="../../other", version=1), dict(id=header["diagnostics"]["id"], version=88)]:
            with self.assertRaises(SnapshotUnavailable):
                open_snapshot(self.path, {**header, "diagnostics": ref})
        with self.assertRaises(SnapshotUnavailable):
            open_snapshot(self.path, {**header, "rules": {}})
        with self.assertRaises(SnapshotUnavailable):
            open_snapshot(other, header)
        snapshot.unlink()
        with self.assertRaises(SnapshotUnavailable):
            open_snapshot(self.path, header)
        other_snapshot.rename(snapshot)
        with self.assertRaises(SnapshotUnavailable):
            open_snapshot(self.path, header)

    def test_late_failure_does_not_publish_files(self):
        def fail():
            yield self.right[0]
            raise ValueError("Оборван источник")
        with self.assertRaises(ValueError):
            export_run(self.path, self.left, fail(), self.rules, {})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_concurrent_export_only_publishes_winning_snapshot(self):
        started = Barrier(2)
        def run():
            def read_left():
                started.wait(timeout=5)
                yield from self.left
            try:
                export_run(self.path, read_left(), self.right, self.rules, {})
            except FileExistsError:
                return False
            return True

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            self.assertCountEqual([job.result() for job in futures], [True, False])
        header = json.loads(self.path.read_text().splitlines()[0])
        self.assertCountEqual([path.name for path in self.root.iterdir()],
                              [self.path.name, snapshot_name(header["diagnostics"]["id"])])
        with closing(open_snapshot(self.path, header)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM missed").fetchone(), (4,))
