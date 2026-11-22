import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from concord.matching import match
from concord.rules import Rules


def config(**overrides):
    return {
        "left_id": "a.id", "right_id": "b.id",
        "fields": [
            dict(left="a.label", right="b.name", mode="text", weight=90),
            dict(left="a.refs.code", right="b.code", mode="exact", weight=10),
        ],
        **overrides,
    }


def compare(left, right, **settings):
    return list(match(left, right, Rules.parse(config(**settings))))


class MatchTests(unittest.TestCase):
    def test_field_conflict_changes_status_only_when_total_reaches_threshold(self):
        left = [{"a.id": "L", "a.label": "ABC", "a.refs.code": "old"}]
        right = [{"b.id": 17, "b.name": "abc", "b.code": "new"}]
        fields = config()["fields"]
        strict, = compare(left, right, fields=fields, accept=90)
        fields[1]["block_on_conflict"] = False
        relaxed, = compare(left, right, fields=fields, accept=90)
        higher, = compare(left, right, fields=fields, accept=91)
        self.assertEqual([r["status"] for r in (strict, relaxed, higher)], ["review", "accepted", "review"])
        self.assertEqual([r["score"] for r in (strict, relaxed, higher)], [90, 90, 90])
        evidence = relaxed["evidence"][1]
        self.assertEqual((evidence["outcome"], evidence["points"], evidence["block_on_conflict"]),
                         ("different", 0, False))
        for invalid in (0, "false", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Rules.parse(config(fields=[dict(fields[0], block_on_conflict=invalid)]))

    def test_trace_multivalues_and_duplicate_join(self):
        row = {"a.id": "001", "a.label": "  КИПАРИС ", "a.refs.code": ["X", "Y"]}
        right = [{"b.id": 3, "b.name": "кипарис", "b.code": "Y"}]
        result, = compare([row, row], right)
        self.assertEqual(result, compare([row], right)[0])
        self.assertEqual((result["status"], result["left_id"], result["right_id"]),
                         ("accepted", "001", 3))
        label, code = result["evidence"]
        self.assertEqual(label["left_values"]["кипарис"]["originals"], ["  КИПАРИС "])
        self.assertEqual(code["shared"], ["Y"])
        self.assertEqual(code["points"], 10)
        self.assertEqual(code["left_path"], "a.refs.code")

    def test_equal_ids_and_missing_values(self):
        rows = compare([{"a.id": 1, "a.label": "   "}], [{"b.id": 1, "b.name": None}])
        self.assertEqual([(r["left_id"], r["right_id"], r["status"]) for r in rows],
                         [(1, None, "unmatched"), (None, 1, "unmatched")])
        with self.assertRaises(ValueError):
            Rules.parse(config(left_id="a.label"))

    def test_spelling_variants_count_once_per_id_for_blocking(self):
        left = [{"a.id": "001", "a.label": [" A", "a", " A"]}, {"a.id": "002"}]
        right = [{"b.id": 7, "b.name": ["A", "a"]}]
        pair, orphan = compare(left, right, block_limit=1)
        self.assertEqual((pair["left_id"], pair["right_id"], pair["status"]), ("001", 7, "accepted"))
        label = pair["evidence"][0]
        self.assertEqual(label["left_values"]["a"], {"originals": [" A", "a"], "counts": [1, 1]})
        self.assertEqual(label["right_values"]["a"], {"originals": ["A", "a"], "counts": [1, 1]})
        self.assertEqual((orphan["left_id"], orphan["reason"]), ("002", "no_block_candidate"))

    def test_conflict_veto_and_missing_evidence(self):
        left = [{"a.id": 1, "a.label": "x", "a.refs.code": "q"}]
        right = {"b.id": "15", "b.name": "X", "b.code": "Q"}
        row, = compare(left, [right])
        self.assertEqual((row["status"], row["reason"], row["score"]),
                         ("review", "conflict_blocks_acceptance", 90))
        del right["b.code"]
        row, = compare(left, [right])
        self.assertEqual(row["evidence"][1]["outcome"], "missing")
        self.assertEqual(row["score"], 90)

    def test_many_to_many_and_frequency(self):
        left = [{"a.id": i, "a.label": "AA"} for i in ("1", 1)]
        right = [{"b.id": i, "b.name": "Aa"} for i in (8, 9)]
        rows = compare(left, right, review=60)
        self.assertEqual({(r["left_id"], r["right_id"]) for r in rows},
                         {(i, j) for i in ("1", 1) for j in (8, 9)})
        self.assertTrue(all(r["status"] == "review" and r["score"] == 63.64 for r in rows))
        self.assertEqual(rows[0]["evidence"][0]["right_values"]["aa"]["counts"], [2, 2])
        self.assertEqual(compare(left, right, review=60, block_limit=1), rows)

    def test_below_threshold_explains_closest_candidate(self):
        row, orphan = compare([{"a.id": 5, "a.refs.code": "A"}],
                              [{"b.id": 7, "b.code": "A"}])
        self.assertEqual((row["right_id"], row["reason"]), (None, "below_review_threshold"))
        self.assertEqual(row["best_comparison"]["score"], 10)
        self.assertEqual(orphan["right_id"], 7)

    def test_empty_sources(self):
        self.assertEqual(compare([], []), [])
        row, = compare([], [{"b.id": 0}])
        self.assertEqual(row["right_id"], 0)

    def test_validation(self):
        for change in (dict(review=85), dict(accept=110), dict(block_limit=True),
                       dict(fields=[]), dict(fields=config()["fields"] * 2)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Rules.parse(config(**change))
        for row in ({"a.id": False}, {"a.id": 1, "a.label": {"nested": "x"}}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                compare([row], [])


class CliTests(unittest.TestCase):
    def test_run_export_and_error_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in [("rules", config()), ("left", {"a.id": 25, "a.label": "ABC"}),
                               ("right", {"b.id": 64, "b.name": "abc"})]:
                (root / name).write_text(json.dumps(data))
            output = root / "result"
            cmd = [sys.executable, "-m", "concord", "--rules", str(root / "rules"),
                   "--left", str(root / "left"), "--right", str(root / "right"),
                   "--output", str(output)]
            run = subprocess.run(cmd, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            header, result = map(json.loads, output.read_text().splitlines())
            self.assertEqual(header["rules"]["fields"],
                             [dict(field, block_on_conflict=True) for field in config()["fields"]])
            self.assertEqual((result["status"], result["score"]), ("accepted", 90))
            before = output.read_bytes()
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), before)
            (root / "right").write_text("{")
            cmd[-1] = str(root / "failed")
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertFalse((root / "failed").exists())
