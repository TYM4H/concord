from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from concord.viewer import detail_page, index_page, load_run


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "examples/rules.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp) / "run.jsonl"
            items = [{"type": "run", "version": 1, "rules": config}]
            for i in range(21):
                items.append(dict(type="result", left_id=f"<{i}>", right_id=None,
                                  status="unmatched", reason="no_block_candidate"))
            export.write_text("\n".join(map(json.dumps, items)))
            self.rules, self.counts = load_run(export, self.db)

    def test_paging_filter_and_escaping(self):
        page = index_page(self.db, self.rules, self.counts, {"page": ["2"]})
        self.assertIn("21–21 из 21", page)
        self.assertIn("&lt;20&gt;", page)
        self.assertNotIn("&lt;19&gt;", page)
        page = index_page(self.db, self.rules, self.counts, {"status": ["review"]})
        self.assertIn("В этой выборке нет записей", page)
        with self.assertRaises(ValueError):
            index_page(self.db, self.rules, self.counts, {"status": ["wrong"]})

    def test_detail_and_missing_record(self):
        page = detail_page(self.db, self.rules, 1)
        self.assertIn("отбор по частоте", page)
        self.assertIn("&lt;0&gt;", page)
        self.assertIsNone(detail_page(self.db, self.rules, 100))

    def test_select_unmatched_left_by_typed_id(self):
        params = {"status": ["unmatched"], "side": ["left"], "left_id": ["001"]}
        for key in ("001", "1", 1, None):
            item = dict(left_id=key, right_id=1 if key is None else None, status="unmatched")
            sql_key = json.dumps(key) if key is not None else None
            self.db.execute("INSERT INTO result(status,left_key,body) VALUES ('unmatched',?,?)",
                            (sql_key, json.dumps(item)))
        page = index_page(self.db, self.rules, self.counts, params)
        self.assertEqual(page.count(">Разобрать</a>"), 1)
        self.assertIn("&quot;001&quot;", page)
        params["left_id"] = ["1"]
        page = index_page(self.db, self.rules, self.counts, params)
        self.assertEqual(page.count(">Разобрать</a>"), 2)
        right = index_page(self.db, self.rules, self.counts, {"status": ["unmatched"], "side": ["right"]})
        self.assertNotIn("/diagnose?", right)
        self.assertIn(">Открыть</a>", right)


class ReviewPageTests(unittest.TestCase):
    def setUp(self):
        from concord.review_store import set_decision
        from concord.runs import export_run
        from concord.rules import Rules

        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        rules = Rules.parse(dict(left_id="left.key", right_id="right.key", fields=[
            dict(left="tags", right="code", mode="exact", weight=70),
            dict(left="location", right="location", mode="exact", weight=30),
        ]))
        left = [{"left.key": "001", "tags": ["A", "B", "C"], "location": "<L>"}]
        right = [{"right.key": i, "code": tag, "location": "<R>"}
                 for i, tag in enumerate(("A", "B", "C"), 1)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.jsonl"
            export_run(path, left, right, rules, {})
            self.rules, self.counts = load_run(path, self.db)
        set_decision(self.db, 2, "confirmed", "Проверен <документ>", 0)
        set_decision(self.db, 3, "deferred", "", 0)

    def test_all_candidates_keep_auto_reason_and_separate_decisions(self):
        from concord.review_screen import comparison_page

        page = comparison_page(self.db, self.rules, 3, "local-token")
        self.assertEqual(page.count('action="/decision"'), 3)
        self.assertEqual(page.count('name="token" value="local-token"'), 3)
        self.assertEqual(page.count('href="/pair?n='), 3)
        for text in ("Подтверждено", "Отложено", "Без решения", "Расхождение",
                     "&lt;L&gt;", "&lt;R&gt;", "Проверен &lt;документ&gt;"):
            self.assertIn(text, page)
        self.assertIsNone(comparison_page(self.db, self.rules, 10, "local-token"))

    def test_return_to_deferred_queue(self):
        page = index_page(self.db, self.rules, self.counts, {"status": ["review"], "state": ["deferred"]})
        self.assertIn('href="/compare?n=3"', page)
        self.assertNotIn('href="/compare?n=2"', page)
        self.assertIn('href="/accepted.csv"', page)
        self.assertIn('Показано 1–1 из 1', page)

    def test_all_scored_candidates_across_pages_with_actions_only_for_review(self):
        from contextlib import closing
        from concord.review_screen import comparison_page
        from concord.run_snapshot import open_snapshot
        from concord.runs import export_run
        from concord.rules import Rules

        rules = Rules.parse(dict(left_id="id", right_id="key", fields=[
            dict(left="name", right="name", mode="text", weight=70),
            dict(left="tag", right="tag", mode="exact", weight=30)]))
        names = [f"Имя {i}" for i in range(10)]
        left = [dict(id="Л", name=names, tag="общий")]
        right = [dict(key=i, name=name) for i, name in enumerate(names)]
        right += [dict(key="001", tag="общий"), dict(key="z", tag="общий")]
        with tempfile.TemporaryDirectory() as folder, closing(sqlite3.connect(":memory:")) as review:
            path = Path(folder) / "variants.jsonl"
            export_run(path, left, right, rules, {})
            load_run(path, review)
            header = json.loads(path.read_bytes().splitlines()[0])
            with closing(open_snapshot(path, header)) as snapshot:
                first = comparison_page(review, rules, 1, "t", snapshot)
                self.assertIn("Вариантов: 12", first)
                self.assertIn('/compare?n=1&amp;page=2', first)
                self.assertEqual(first.count('action="/decision"'), 9)
                second = comparison_page(review, rules, 1, "t", snapshot, page=2)
                self.assertEqual(second.count('action="/decision"'), 1)
                self.assertEqual(second.count('Ниже порога'), 2)
                for right_id in ('&quot;001&quot;', '&quot;z&quot;', '9'):
                    self.assertIn(f'<code>{right_id}</code></th>', second)
                self.assertEqual(second.count('21.21%'), 2)
                # Переход на десятую пару открывает её страницу после ручного решения.
                self.assertIn("10–12 из 12", comparison_page(review, rules, 10, "t", snapshot))
                self.assertIsNone(comparison_page(review, rules, 1, "t", snapshot, page=3))
