import csv
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from concord.review_export import csv_chunks
from concord.review_store import load_run, set_decision


class ReviewExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "run.jsonl"
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.config = dict(left_id="crm.id", right_id="erp.key", fields=[
            dict(left="crm.address", right="erp.mail", mode="exact", weight=1),
        ])
        self.original = 'Почта, "рабочая"\nВторая строка'
        self.evidence = [{
            "left_path": "crm.address", "right_path": "erp.mail",
            "mode": "exact", "weight": 1, "outcome": "different", "points": 0,
            "shared": [], "used_value": None,
            "left_values": {self.original: {"originals": [self.original], "counts": [1, 0]}},
            "right_values": {"другое": {"originals": ["другое"], "counts": [0, 1]}},
        }]

    def open_export(self, records):
        with self.path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(type="run", version=1, rules=self.config)) + "\n")
            for row in records:
                stream.write(json.dumps({"type": "result", **row}, ensure_ascii=False) + "\n")
        self.rules, _ = load_run(self.path, self.db)

    def read_csv(self):
        data = b"".join(csv_chunks(self.db, self.rules)).decode("utf-8")
        return csv.DictReader(StringIO(data, newline=""))

    def test_exports_automatic_and_all_confirmed_for_same_id(self):
        automatic = dict(status="accepted", reason="accept_threshold", score=95,
                         evidence=[], left_id=1, right_id="001")
        manual = dict(status="review", reason="conflict_blocks_acceptance", score=85,
                      evidence=self.evidence, left_id="001")
        rights = ['А, "Р"\n01', 2, 3, 4, 5]
        self.open_export([automatic, *(dict(manual, right_id=k) for k in rights),
                          dict(status="unmatched", reason="no_block_candidate", left_id="007", right_id=None)])
        note = 'Сверено по договору, запись "А".\r\nАрхив СПб.'
        set_decision(self.db, 2, "confirmed", note, 0)
        set_decision(self.db, 3, "confirmed", "", 0)
        set_decision(self.db, 4, "rejected", "Разные", 0)
        set_decision(self.db, 5, "deferred", "Уточнить", 0)

        auto, first, second = list(self.read_csv())
        self.assertEqual((auto["left_id"], auto["left_id_type"], auto["right_id"], auto["right_id_type"]),
                         ("1", "integer", "001", "string"))
        self.assertEqual((auto["acceptance_basis"], auto["auto_reason"], auto["analyst_note"],
                          auto["decided_at"]), ("automatic", "accept_threshold", "", ""))
        self.assertEqual([first["right_id"], second["right_id"]], [rights[0], "2"])
        for record in (first, second):
            self.assertEqual((record["left_id"], record["left_id_type"]), ("001", "string"))
            self.assertEqual((record["left_id_path"], record["right_id_path"]), ("crm.id", "erp.key"))
            self.assertEqual((record["auto_status"], record["auto_reason"], record["acceptance_basis"]),
                             ("review", "conflict_blocks_acceptance", "analyst_confirmed"))
            self.assertEqual(json.loads(record["evidence_json"]), self.evidence)
            self.assertTrue(record["decided_at"])
        self.assertEqual(first["analyst_note"], note)
        self.assertEqual(second["analyst_note"], "")

        set_decision(self.db, 2, "rejected", "Исправление", 1)
        self.assertEqual([record["right_id"] for record in self.read_csv()], ["001", "2"])

    def test_a_header_is_returned_for_empty_export(self):
        self.open_export([])
        reader = self.read_csv()
        self.assertIn("acceptance_basis", reader.fieldnames)
        self.assertEqual(list(reader), [])

    def test_export_preserves_unsigned_bigint_and_embedded_nul(self):
        row = dict(status="accepted", reason="accept_threshold", score=95,
                   evidence=self.evidence, left_id=2**64 - 1, right_id="0\x00Я")
        self.open_export([row])
        record, = self.read_csv()
        self.assertEqual((record["left_id"], record["left_id_type"]), (str(2**64 - 1), "integer"))
        self.assertEqual((record["right_id"], record["right_id_type"]), ("0\x00Я", "string"))
        self.assertEqual(json.loads(record["evidence_json"]), self.evidence)

    def test_large_csv_is_delivered_in_multiple_chunks(self):
        row = dict(status="accepted", reason="accept_threshold", score=90, evidence=[], left_id="002")
        self.open_export(dict(row, right_id=i) for i in range(2000))
        chunks = iter(csv_chunks(self.db, self.rules))
        first = next(chunks)
        self.assertTrue(first.startswith(b"left_id,right_id,"))
        self.assertGreater(len(next(chunks)), 0)
        chunks.close()
