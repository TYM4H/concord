import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from concord.review_screen import comparison_page
from concord.review_store import load_run
from concord.rules import Field, Rules
from concord.runs import export_run
from concord.threshold_screen import threshold_page
from concord.viewer import detail_page


class ThresholdScreenTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "run.jsonl"
        rules = dict(left_id="people.id", right_id="contacts.id", review=65, accept=85,
                     fields=[dict(left="people.name", right="contacts.name", mode="text", weight=1)])
        rows = [dict(type="run", version=1, rules=rules)]
        for n, (score, outcome, status) in enumerate([
            (66.82, "equal", "review"), (70, "different", "review"),
            (85, "different", "review"), (85, "equal", "accepted"), (98, "equal", "accepted"),
        ], 1):
            field = dict(left_path="people.name", right_path="contacts.name", outcome=outcome,
                         left_values={"test": {"originals": ["<Имя>"]}}, right_values={})
            rows.append(dict(type="result", left_id=f"{n:04d}", right_id=n, score=score, status=status,
                             reason="conflict_blocks_acceptance" if n == 3 else
                                    "accept_threshold" if status == "accepted" else "review_threshold",
                             evidence=[field]))
        rows.append(dict(type="result", status="unmatched", left_id="0006", right_id=None))
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.rules, _ = load_run(path, self.db)

    def test_lower_threshold_preserves_conflicts_and_includes_equality(self):
        page = threshold_page(self.db, self.rules, {"accept": ["66.82"]})
        self.assertIn("ожидается <b>3</b> (изменение +1)", page)
        self.assertIn('name="from_threshold" value="1"', page)
        self.assertIn('name="accept" value="66.82"', page)
        self.assertIn('name="review" value="65"', page)
        above = page.split("На пороге и выше</h2>")[1]
        self.assertLess(above.index('/pair?n=1"'), above.index('/pair?n=2"'))
        self.assertIn("Расхождение полей", above)
        self.assertIn("&lt;Имя&gt;", above)
        self.assertNotIn('/pair?n=6"', page)
        self.assertEqual(self.rules.accept, 85)

    def test_raise_threshold_and_filter_boundary_examples(self):
        form = {"accept": ["98"], "examples": ["eligible"]}
        page = threshold_page(self.db, self.rules, form)
        self.assertIn("ожидается <b>1</b> (изменение -1)", page)
        self.assertIn('value="eligible" checked', page)
        below, above = page.split("На пороге и выше</h2>")
        self.assertLess(below.index('/pair?n=4"'), below.index('/pair?n=1"'))
        self.assertNotIn('/pair?n=2"', page)
        self.assertNotIn('/pair?n=3"', page)
        self.assertIn('/pair?n=5"', above)

    def test_invalid_cutoff_keeps_rules_accessible(self):
        for value in (["nan"], ["65"], ["-1"], ["100.1"], ["80", "90"]):
            with self.subTest(value=value):
                page = threshold_page(self.db, self.rules, {"accept": value})
                self.assertIn('role="alert"', page)
                self.assertIn('href="/rules"', page)
                self.assertNotIn('name="from_threshold"', page)

    def test_unknown_evidence_in_historical_pair_marks_forecast_as_minimum(self):
        self.db.execute("UPDATE result SET body=json_remove(body,'$.evidence','$.reason') WHERE n=3")
        self.db.execute("UPDATE pair_score SET eligible=-1 WHERE n=3")
        page = threshold_page(self.db, self.rules, {})
        self.assertIn("ожидается минимум 2", page)
        self.assertIn("Недостаточно данных для прогноза", page)

    def test_forecast_and_explanations_apply_each_fields_saved_policy(self):
        rules = Rules("id", "id", (Field("name", "name", "text", 70),
                                   Field("email", "email", "exact", 20, block_on_conflict=False),
                                   Field("region", "region", "exact", 10)))
        left = [dict(id=1, name="Олег", email="a"), dict(id=2, name="Анна", email="b", region="Юг")]
        right = [dict(id=11, name="Олег", email="c"), dict(id=22, name="Анна", email="d", region="Север")]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.jsonl"
            export_run(path, left, right, rules, {})
            db = sqlite3.connect(":memory:")
            self.addCleanup(db.close)
            saved_rules, _ = load_run(path, db)
        self.assertEqual(db.execute("SELECT score, eligible FROM pair_score ORDER BY n").fetchall(),
                         [(70, 1), (70, 0)])
        forecast = threshold_page(db, saved_rules, {"accept": ["70"], "examples": ["eligible"]})
        self.assertIn("ожидается <b>1</b> (изменение +1)", forecast)
        self.assertIn('/pair?n=1"', forecast)
        self.assertNotIn('/pair?n=2"', forecast)
        for page in (comparison_page(db, saved_rules, 2, "token"), detail_page(db, saved_rules, 2)):
            self.assertIn("Расхождение · допускается правилами", page)
            self.assertIn("Расхождение · блокирует автосовпадение", page)
