import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from concord import matching
from concord.diagnostics import explain_pair, search_right
from concord.rules import Rules
from concord.run_snapshot import open_snapshot
from concord.runs import export_run


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.rules = Rules.parse(dict(left_id="id", right_id="key", review=65, block_limit=1, fields=[
            dict(left="name", right="label", mode="text", weight=60),
            dict(left="tags", right="tag", mode="exact", weight=30),
            dict(left="town", right="city", mode="text", weight=10),
        ]))
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(matching.SCHEMA)
        matching.ingest(self.db, 0, [
            dict(id="01", name="Иван", tags=["A", "A", "B"], town="Москва"),
            dict(id=1, name="Анна", tags=["X"], town="Москва"),
        ], self.rules)
        matching.ingest(self.db, 1, [
            dict(key=7, label="Иоанн", tag="A", city="МОСКВА"),
            dict(key="7", label="Ян", tag="B"),
            dict(key="003", label="Ян Иванов", city="МОСКВА"),
            dict(key="R", label="АННА", tag="X", city="МОСКВА"),
            dict(key="none"),
        ], self.rules)
        self.db.executescript(matching.INDEX)
        self.db.execute("""CREATE TABLE IF NOT EXISTS missed(left_id TEXT, right_id TEXT, score REAL,
                           PRIMARY KEY(left_id, right_id)) WITHOUT ROWID""")
        self.db.executemany("INSERT INTO missed VALUES (?, ?, ?)", [
            ('"01"', '7', 35.77), ('"01"', '"7"', 30),
        ])

    def test_search_cyrillic_substring_and_exact_typed_id(self):
        result, more = search_right(self.db, self.rules, "  иВанОВ  ")
        self.assertEqual([(item["key"], item["id"]) for item in result], [('"003"', "003")])
        self.assertTrue(any("Ян Иванов" in text for text in result[0]["preview"]))
        self.assertFalse(more)
        result, _ = search_right(self.db, self.rules, "7")
        self.assertEqual([item["id"] for item in result], ["7", 7])
        result, _ = search_right(self.db, self.rules, "анна")
        self.assertEqual(result[0]["id"], "R")  # Связана с другой левой записью.
        self.assertEqual(search_right(self.db, self.rules, "%_"), ([], False))

    def test_pagination_including_exact_id_first(self):
        # Совпавший ID сортируется после множества совпавших значений.
        extra = [dict(key=f"aaa-{i:02}", label="Искомая запись") for i in range(48)]
        matching.ingest(self.db, 1, extra + [dict(key="запись")], self.rules)
        page1, more1 = search_right(self.db, self.rules, "запись")
        page2, more2 = search_right(self.db, self.rules, "запись", 2)
        page3, more3 = search_right(self.db, self.rules, "запись", 3)
        self.assertEqual((len(page1), len(page2), len(page3)), (20, 20, 9))
        self.assertEqual((more1, more2, more3), (True, True, False))
        self.assertEqual(page1[0]["id"], "запись")
        self.assertEqual(len({i["key"] for i in page1 + page2 + page3}), 49)
        self.assertEqual(search_right(self.db, self.rules, "запись", 4), ([], False))
        for bad in (0, -1, 2**63, True):
            with self.subTest(page=bad), self.assertRaises(ValueError):
                search_right(self.db, self.rules, "", bad)

    def test_blocked_by_frequency_and_no_keys_in_common(self):
        data = explain_pair(self.db, self.rules, '"01"', '"003"', legacy_limit=1)
        self.assertEqual((data["stage"], data["score"]), ("not_candidate", None))
        self.assertEqual(data["fields"][2]["shared"], ["москва"])
        self.assertEqual(data["fields"][2]["eligible"], [])
        entry = data["fields"][2]["right_values"]["москва"]
        self.assertEqual(entry, dict(counts=[2, 3], originals=["МОСКВА"]))
        self.assertNotIn("points", data["fields"][2])
        empty = explain_pair(self.db, self.rules, '"01"', '"none"')
        self.assertEqual((empty["stage"], empty["score"]), ("not_candidate", None))
        self.assertTrue(all(not field["shared"] for field in empty["fields"]))
        self.assertIsNone(explain_pair(self.db, self.rules, '"01"', '"unknown"'))
        self.assertIsNone(explain_pair(self.db, self.rules, '"1"', '"003"'))

    def test_rejected_best_and_other_candidate_have_run_scores(self):
        calc = explain_pair(self.db, self.rules, '"01"', "7")
        self.assertEqual(calc["score"], 35.77)
        data = explain_pair(self.db, self.rules, '"01"', '"7"')
        self.assertEqual((data["stage"], data["score"]), ("below_threshold", 30))
        self.assertEqual([f["outcome"] for f in data["fields"]], ["different", "equal", "missing"])
        self.assertEqual(data["fields"][1]["eligible"], ["B"])
        self.assertEqual(data["fields"][1]["points"], 30)
        self.assertEqual(data["fields"][1]["used_value"], "B")

    def test_inconsistent_trace_is_reported(self):
        self.db.execute("UPDATE missed SET score=1 WHERE right_id='7'")
        with self.assertRaisesRegex(ValueError, "Балл"):
            explain_pair(self.db, self.rules, '"01"', "7")
        self.db.execute("DELETE FROM missed WHERE right_id='7'")
        with self.assertRaisesRegex(ValueError, "Журнал"):
            explain_pair(self.db, self.rules, '"01"', "7")

    def test_reopen_export_and_diagnose_specific_candidate(self):
        with TemporaryDirectory() as directory:
            result = Path(directory) / "result.jsonl"
            export_run(result, [dict(id="01", name="Иван", tags=["A", "B"])],
                       [dict(key=7, label="Иоанн", tag="A"), dict(key="7", tag="B"),
                        dict(key="07", label="Иванов")], self.rules, {})
            with result.open(encoding="utf-8") as stream:
                header = json.loads(next(stream))
            with closing(open_snapshot(result, header)) as reopened:
                items, _ = search_right(reopened, self.rules, "7")
                rows = [explain_pair(reopened, self.rules, '"01"', item["key"]) for item in items]
                self.assertEqual([(r["right_id"], r["stage"]) for r in rows],
                                 [("7", "below_threshold"), (7, "below_threshold"),
                                  ("07", "not_candidate")])
                self.assertGreater(rows[0]["score"], 0)
                self.assertIsNone(rows[2]["score"])
