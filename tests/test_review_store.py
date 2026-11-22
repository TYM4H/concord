from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from concord.review_store import ReviewConflict, load_run, review_path, set_decision


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "history.jsonl"
        rules = {"left_id": "person.id", "right_id": "doctor.id", "fields": [
            {"left": "person.name", "right": "doctor.name", "weight": 1, "mode": "text"}]}
        header = {"version": 1, "type": "run", "rules": rules}
        rows = [("01", 17, "review"), ("01", 31, "review"), (1, 44, "accepted"),
                ("1", None, "unmatched"), (None, "01", "unmatched")]
        items = [header] + [dict(type="result", left_id=l, right_id=r, status=s) for l, r, s in rows]
        self.path.write_text("\n".join(map(json.dumps, items)), encoding="utf-8")
        self.sidecar = review_path(self.path)

    def test_restart_preserves_multiple_confirmations_and_automatic_rows(self):
        snapshot = self.path.read_bytes()
        with closing(sqlite3.connect(self.sidecar)) as db:
            rules, counts = load_run(self.path, db)
            self.assertEqual(rules.right_id, "doctor.id")
            self.assertEqual(counts, {"accepted": 1, "review": 2, "unmatched": 2})
            for n in (1, 2):
                self.assertEqual(set_decision(db, n, "confirmed", "Сверено вручную", 0), 1)
        with closing(sqlite3.connect(self.sidecar)) as db:
            load_run(self.path, db)
            self.assertEqual(db.execute("SELECT action,revision FROM decision ORDER BY n").fetchall(),
                             [("confirmed", 1), ("confirmed", 1)])
            self.assertEqual(db.execute("SELECT left_key,status FROM result ORDER BY n").fetchall(), [
                ('"01"', "review"), ('"01"', "review"), ('1', "accepted"),
                ('"1"', "unmatched"), (None, "unmatched")])
            stamp = db.execute("SELECT updated_at FROM decision WHERE n=1").fetchone()[0]
            self.assertEqual(datetime.fromisoformat(stamp).utcoffset(), timedelta())
        self.assertEqual(self.path.read_bytes(), snapshot)

    def test_idempotent_retries_stale_updates_and_scope(self):
        with closing(sqlite3.connect(self.sidecar)) as db:
            load_run(self.path, db)
            self.assertEqual(set_decision(db, 1, "deferred", None, 0), 1)
            stamp = db.execute("SELECT updated_at FROM decision WHERE n=1").fetchone()
            self.assertEqual(set_decision(db, 1, "deferred", "", 0), 1)
            self.assertEqual(stamp, db.execute("SELECT updated_at FROM decision WHERE n=1").fetchone())
            with self.assertRaises(ReviewConflict):
                set_decision(db, 1, "rejected", "расходятся контакты", 0)
            self.assertEqual(set_decision(db, 1, "rejected", "расходятся контакты", 1), 2)
            self.assertEqual(set_decision(db, 1, "confirmed", "обновлены данные", 2), 3)
            for n, action, note, revision in [(3, "rejected", "", 0), (4, "confirmed", "", 0),
                                               (6, "confirmed", "", 0), (1, "bogus", "", 3),
                                               (1, "deferred", "x" * 2001, 3)]:
                with self.subTest(n=n, action=action), self.assertRaises(ValueError):
                    set_decision(db, n, action, note, revision)
            self.assertEqual(db.execute("SELECT action,note,revision FROM decision").fetchall(),
                             [("confirmed", "обновлены данные", 3)])

    def test_altered_source_refused_without_losing_decisions(self):
        original = self.path.read_bytes()
        with closing(sqlite3.connect(self.sidecar)) as db:
            load_run(self.path, db)
            set_decision(db, 2, "deferred", "Запросил уточнение", 0)
        with closing(sqlite3.connect(self.sidecar)) as db:
            self.path.write_bytes(original.replace(b'"01"', b'"02"'))
            with self.assertRaisesRegex(ValueError, "отличается"):
                load_run(self.path, db)
            self.path.write_bytes(original)
            load_run(self.path, db)
            self.assertEqual(db.execute("SELECT n,action FROM decision").fetchall(), [(2, "deferred")])

    def test_partial_failed_import_retries_cleanly(self):
        original = self.path.read_bytes()
        self.path.write_bytes(original + b'\n{')
        with closing(sqlite3.connect(self.sidecar)) as db:
            with self.assertRaises(ValueError):
                load_run(self.path, db)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
            self.path.write_bytes(original)
            _, counts = load_run(self.path, db)
            self.assertEqual(sum(counts.values()), 5)

    def test_score_index_migrates_without_changing_decisions(self):
        header, *rows = map(json.loads, self.path.read_text().splitlines())
        rows[0].update(score=70, reason="review_threshold", evidence=[{"outcome": "different"}])
        rows[1].update(score=70, reason="review_threshold", evidence=[{"outcome": "equal"}])
        rows[2].update(score=99, reason="accept_threshold")
        rows.extend([dict(type="result", left_id=2, right_id=3, status="review", score=69),
                     dict(type="result", left_id=4, right_id=5, status="review")])
        self.path.write_text("\n".join(map(json.dumps, [header, *rows])), encoding="utf-8")
        expected = [(1, 70, 0), (2, 70, 1), (3, 99, 1), (4, None, 0),
                    (5, None, 0), (6, 69, -1), (7, None, -1)]
        with closing(sqlite3.connect(self.sidecar)) as db:
            load_run(self.path, db)
            self.assertEqual(list(db.execute("SELECT n,score,eligible FROM pair_score ORDER BY n")), expected)
            set_decision(db, 1, "confirmed", "Сверено по другому полю", 0)
            decision = db.execute("SELECT * FROM decision").fetchone()
            db.execute("DROP TABLE pair_score")
            db.execute("CREATE TABLE pair_score (n INTEGER PRIMARY KEY, score REAL, eligible INTEGER)")
        with closing(sqlite3.connect(self.sidecar)) as db:
            db.set_authorizer(lambda op, name, *_: sqlite3.SQLITE_DENY
                              if op == sqlite3.SQLITE_CREATE_INDEX and name == "pair_score_status_order"
                              else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):
                load_run(self.path, db)
            db.set_authorizer(None)
            self.assertEqual([col[1] for col in db.execute("PRAGMA table_info(pair_score)")],
                             ["n", "score", "eligible"])
            load_run(self.path, db)
            self.assertEqual(list(db.execute("SELECT n,score,eligible FROM pair_score ORDER BY n")), expected)
            self.assertEqual(db.execute("SELECT * FROM decision").fetchone(), decision)
            self.assertEqual(list(db.execute("SELECT score,status FROM pair_score WHERE n IN (1,4) ORDER BY n")),
                             [(70, "review"), (None, "unmatched")])
            sorted_ids = list(db.execute("SELECT n FROM pair_score ORDER BY score DESC,n ASC"))
            self.assertEqual(sorted_ids, [(3,), (1,), (2,), (6,), (4,), (5,), (7,)])

    def test_concurrent_initialisation_and_updates(self):
        start = Barrier(2)
        save = Barrier(2)

        def user(action):
            with closing(sqlite3.connect(self.sidecar)) as db:
                start.wait(timeout=10)
                load_run(self.path, db)
                save.wait(timeout=10)
                try:
                    set_decision(db, 1, action, "", 0)
                    return action
                except ReviewConflict:
                    return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(user, ("deferred", "confirmed")))
        self.assertEqual(outcomes.count(None), 1)
        with closing(sqlite3.connect(self.sidecar)) as db:
            self.assertEqual(db.execute("SELECT action,revision FROM decision").fetchall(),
                             [(next(a for a in outcomes if a), 1)])
