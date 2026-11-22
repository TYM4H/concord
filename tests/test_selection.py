from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "Нужны requirements-db.txt")
class SelectionWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.draft = self.folder / "selection.json"
        self.result = self.folder / "result.jsonl"
        left = self.create_source("crm", "people", """
            CREATE TABLE people(id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE contacts(id INTEGER PRIMARY KEY, email TEXT);
            CREATE TABLE links(person TEXT REFERENCES people(id), contact INTEGER REFERENCES contacts(id));
            INSERT INTO people VALUES ('001', 'Ирина'), ('002', 'Марина');
            INSERT INTO contacts VALUES (10, 'One@example.test'), (11, 'Two@example.test');
            INSERT INTO links VALUES ('001',10), ('001',10), ('002',11);
        """)
        right = self.create_source("erp", "accounts", """
            CREATE TABLE accounts(pk INTEGER PRIMARY KEY, name TEXT, email TEXT);
            INSERT INTO accounts VALUES (91,'Марина','one@example.test'), (92,'Ирина','other@example.test');
        """)
        sources = ["--left-source", left, "--right-source", right]
        self.suggest = [sys.executable, "-m", "concord.suggestions", *sources,
                        "--left-id", "id", "--right-id", "pk", "--draft", self.draft]
        self.execute = [sys.executable, "-m", "concord", *sources, "--selection", self.draft,
                        "--output", self.result]

    def create_source(self, name, table, sql):
        database = self.folder / (name + ".db")
        with closing(sqlite3.connect(database)) as conn:
            conn.executescript(sql)
        profile = self.folder / (name + ".json")
        profile.write_text(json.dumps(dict(driver="sqlite", database=database.name, schema="main", table=table)))
        return profile

    def make_draft(self):
        run = subprocess.run(self.suggest, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("One@example.test", run.stdout)
        self.assertIn("one@example.test", run.stdout)
        return json.loads(self.draft.read_text())

    def test_confirm_related_field_and_run(self):
        source_files = {p: p.read_bytes() for p in self.folder.iterdir()}
        draft = self.make_draft()
        self.assertEqual((draft["version"], draft["left_id"], draft["right_id"]),
                         (1, "main.people.id", "main.accounts.pk"))
        fields = draft["fields"]
        self.assertTrue(all(f["enabled"] is False for f in fields))
        self.assertIn(("main.people.name", "main.accounts.name"), {(f["left"], f["right"]) for f in fields})
        chosen = next(f for f in fields if f["left"] == "main.people.links.contacts.email"
                      and f["right"] == "main.accounts.email")
        route = {"column": "email", "via": [
            {"table": "links", "from": ["id"], "to": ["person"]},
            {"table": "contacts", "from": ["contact"], "to": ["id"]},
        ]}
        self.assertEqual(chosen["left_route"], route)
        self.assertIsNone(chosen["right_route"])
        self.assertIs(chosen["block_on_conflict"], True)
        chosen.update(enabled=True, weight=37, block_on_conflict=False)
        self.draft.write_text(json.dumps(draft))
        run = subprocess.run(self.execute, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        header, *rows = map(json.loads, self.result.read_text().splitlines())
        rule, = header["rules"]["fields"]
        self.assertEqual(rule, {k: chosen[k] for k in ("left", "right", "weight", "mode", "block_on_conflict")})
        self.assertEqual(header["sources"]["left"]["routes"], {chosen["left"]: route})
        self.assertEqual(header["sources"]["right"]["routes"], {})
        self.assertCountEqual([(r["left_id"], r["right_id"], r["status"]) for r in rows],
                              [("001", 91, "accepted"), ("002", None, "unmatched"), (None, 92, "unmatched")])
        pair = next(r for r in rows if r["status"] == "accepted")
        evidence, = pair["evidence"]
        self.assertEqual((evidence["left_path"], evidence["right_path"]), (chosen["left"], chosen["right"]))
        self.assertEqual(evidence["left_values"]["one@example.test"]["originals"], ["One@example.test"])
        self.assertEqual(evidence["used_value"], "one@example.test")
        for path, contents in source_files.items():
            self.assertEqual(path.read_bytes(), contents, path.name)

    def test_reject_empty_or_reused_fields(self):
        draft = self.make_draft()
        for reused in (None, "left", "right"):
            with self.subTest(reused=reused):
                fields = []
                if reused:
                    email = next(f.copy() for f in draft["fields"] if f["right"] == "main.accounts.email")
                    name = next(f.copy() for f in draft["fields"] if f["right"] == "main.accounts.name")
                    name[reused] = email[reused]
                    name[reused + "_route"] = email[reused + "_route"]
                    fields = [dict(email, enabled=True), dict(name, enabled=True)]
                self.draft.write_text(json.dumps({**draft, "fields": fields or draft["fields"]}))
                run = subprocess.run(self.execute, capture_output=True, text=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertNotIn("Traceback", run.stderr)
                self.assertFalse(self.result.exists())

    def test_choose_by_number(self):
        listed = self.make_draft()
        fields = listed["fields"]
        self.assertGreaterEqual(len(fields), 2)
        target = next(i for i, f in enumerate(fields, 1) if f["right"] == "main.accounts.email")
        fresh = self.folder / "chosen.json"
        command = [*self.suggest[:-1], fresh, "--choose"]
        run = subprocess.run(command, input=f"{target}\n", capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        enabled = [f for f in json.loads(fresh.read_text())["fields"] if f["enabled"]]
        self.assertEqual([(f["left"], f["right"]) for f in enabled],
                         [(fields[target - 1]["left"], "main.accounts.email")])
        self.assertIn("One@example.test", run.stdout)

    def test_invalid_number_leaves_no_draft(self):
        run = subprocess.run([*self.suggest, "--choose"], input="99999\n",
                             capture_output=True, text=True, timeout=15)
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("Traceback", run.stderr)
        self.assertFalse(self.draft.exists())

    def test_existing_draft_preserved(self):
        self.draft.write_text("Ранее сохранённый выбор аналитика", encoding="utf-8")
        saved = self.draft.read_bytes()
        run = subprocess.run(self.suggest, capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(self.draft.read_bytes(), saved)
