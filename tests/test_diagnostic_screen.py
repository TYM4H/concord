from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from concord.diagnostic_screen import diagnosis_page
from concord.review_store import load_run
from concord.rules import Rules
from concord.run_snapshot import open_snapshot
from concord.runs import export_run


class DiagnosticScreenTests(unittest.TestCase):
    def setUp(self):
        self.review = sqlite3.connect(":memory:")
        self.addCleanup(self.review.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "run.jsonl"
        rules = Rules.parse({"left_id": "id", "right_id": "key", "block_limit": 1, "fields": [
            {"left": "name", "right": "label", "mode": "text", "weight": 80, "block_on_conflict": False},
            {"left": "tag", "right": "code", "mode": "exact", "weight": 15},
            {"left": "region", "right": "area", "mode": "exact", "weight": 5},
        ]})
        left = [{"id": "001", "name": "<Леонид>", "tag": "XYZ", "region": "РФ"}]
        right = [{"key": 1, "label": "Лев", "code": "XYZ", "area": "РФ"},
                 {"key": "1", "label": "Лев Второй", "area": "РФ"}]
        export_run(path, left, right, rules, {})
        self.rules, _ = load_run(path, self.review)
        with path.open(encoding="utf-8") as stream:
            header = json.loads(stream.readline())
        self.snapshot = open_snapshot(path, header)
        self.addCleanup(self.snapshot.close)

    def page(self, params):
        return diagnosis_page(self.review, self.rules, 1, params, self.snapshot)

    def test_search_and_back_preserve_context_and_typed_id(self):
        search = self.page({"q": ["ЛеВ"]})
        self.assertIn('/compare?n=1', search)
        from concord.review_screen import comparison_page
        alternatives = comparison_page(self.review, self.rules, 1, "csrf", self.snapshot)
        self.assertIn("Вариантов: 2", alternatives)
        self.assertIn("18.54%", alternatives)
        self.assertNotIn('action="/decision"', alternatives)
        self.assertIn("&lt;Леонид&gt;", search)
        self.assertIn("right=1", search)
        self.assertIn("right=%221%22", search)
        self.assertIn("Лев Второй", search)
        below = self.page({"q": ["ЛеВ"], "page": ["2"], "right": ["1"]})
        self.assertIn("q=%D0%9B%D0%B5%D0%92&amp;page=2", below)
        self.assertIn("18.54%", below)
        self.assertIn("46.46 п. п.", below)
        self.assertIn("Расхождение · допускается правилами", below)
        self.assertIn("прошло отбор", below)
        self.assertNotIn("превышен предел частоты", below)

    def test_common_value_shows_scored_comparison(self):
        page = self.page({"right": ['"1"']})
        self.assertIn("Пара прошла отбор", page)
        self.assertIn("3.54%", page)
        self.assertIn("слева 1, справа 2", page)
        self.assertIn("Пропуск значения", page)
        self.assertIsNone(self.page({"right": ["404"]}))

    def test_legacy_and_right_orphan(self):
        legacy = diagnosis_page(self.review, self.rules, 1, {}, None, "Снимок недоступен: <файл>")
        self.assertIn("Снимок недоступен: &lt;файл&gt;", legacy)
        self.assertIn("Балл не достиг нижнего порога кандидата", legacy)
        self.assertIn('href="/pair?n=1"', legacy)
        self.assertIsNone(diagnosis_page(self.review, self.rules, 2, {}, self.snapshot))
