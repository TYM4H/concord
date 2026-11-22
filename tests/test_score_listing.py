import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from concord.review_store import load_run
from concord.rules import Rules
from concord.run_snapshot import open_snapshot
from concord.runs import export_run
from concord.scores_screen import missed_pair_page, scores_page
from concord.viewer import index_page


class ScoreListingTests(unittest.TestCase):
    def test_order_and_paging_across_lower_threshold(self):
        rules = Rules.parse(dict(left_id="id", right_id="id", fields=[
            dict(left="name", right="name", mode="text", weight=70),
            dict(left="email", right="email", mode="exact", weight=30)]))
        left = [dict(id=f"{i:03}", name=f"name{i}", email=f"mail{i}") for i in range(25)]
        right = [dict(id=i, name=f"name{i}" if i % 3 != 2 else f"other{i}",
                      email=f"mail{i}" if i % 3 != 1 else f"other{i}") for i in range(25)]
        with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(":memory:")) as db:
            path = Path(tmp) / "result.jsonl"
            export_run(path, left, right, rules, {})
            _, counts = load_run(path, db)
            with path.open() as stream:
                header = json.loads(next(stream))
            with closing(open_snapshot(path, header)) as snapshot:
                first = scores_page(db, snapshot, {})
                second = scores_page(db, snapshot, {"page": ["2"]})
                scores = re.findall(r"<td>(\d+)%</td>", first + second)
                self.assertEqual(scores, ["100"] * 9 + ["70"] * 8 + ["30"] * 8)
                self.assertIn("21–25 из 25", second)
                url = re.search(r'href="(/scored-pair\?[^"]+)"', first).group(1)
                details = missed_pair_page(snapshot, rules, parse_qs(urlsplit(unescape(url)).query))
                self.assertIn("&quot;002&quot;", details)
                self.assertIn("30%", details)
                ranked = index_page(db, rules, counts, {"sort": ["score"]}, snapshot)
                displayed = re.findall(r'aria-hidden="true" class="match-score" max="100" value="(\d+)"', ranked)
                self.assertEqual(displayed, ["100"] * 9 + ["70"] * 8)
            old = scores_page(db, None, {})
            self.assertIn("Снимок сравнений недоступен", old)
            self.assertIn("из 17", old)
