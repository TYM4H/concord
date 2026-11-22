from contextlib import closing
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from concord.matching import match
from concord.rules import Rules

from concord.sources.adapters import connect
from concord.sources.profile import Profile
from concord.sources.reader import catalog, projection


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "Нужны requirements-db.txt")
class SourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        with closing(sqlite3.connect(self.path / "base.db")) as db:
            db.executescript("""
                CREATE TABLE parent (first int, second text, primary key(first, second));
                CREATE TABLE people (
                    key text PRIMARY KEY, "full name" text, part int, code text,
                    FOREIGN KEY(part, code) REFERENCES parent(first, second));
                INSERT INTO people(key, "full name") VALUES ('001', 'Test person');
            """)
        self.config = {"driver": "sqlite", "database": "base.db", "schema": "main", "table": "people"}
        self.profile_path = self.path / "profile.json"
        self.profile_path.write_text(json.dumps(self.config))
        self.profile = Profile.load(self.profile_path)

    def test_read_schema_and_composite_key(self):
        with connect(self.profile) as conn:
            tables = catalog(conn, "main")["tables"]
            parent, people = tables
            self.assertEqual(parent["primary_key"], ["first", "second"])
            fk, = people["foreign_keys"]
            self.assertEqual((fk["columns"], fk["target_columns"]), (["part", "code"], ["first", "second"]))
            self.assertEqual(next(projection(conn, self.profile, "main.people.key",
                                             ["main.people.full name"]))["main.people.key"], "001")

    def test_read_only_and_missing_files(self):
        with self.assertRaises(ValueError):
            with connect(self.profile) as conn:
                conn.exec_driver_sql("UPDATE people SET key = 'bad'")
        self.profile_path.write_text(json.dumps({**self.config, "database": "missing.db"}))
        with self.assertRaises(ValueError):
            with connect(Profile.load(self.profile_path)):
                pass
        self.assertFalse((self.path / "missing.db").exists())

    def test_unknown_identifier(self):
        with connect(self.profile) as conn, self.assertRaisesRegex(ValueError, "Не найден идентификатор"):
            list(projection(conn, self.profile, "main.people.missing", []))

    def test_multihop_and_duplicate_membership(self):
        with closing(sqlite3.connect(self.path / "base.db")) as db:
            db.executescript("""
                CREATE TABLE tags (id int primary key, label text);
                CREATE TABLE membership (person text REFERENCES people(key), tag int REFERENCES tags(id));
                INSERT INTO people(key) VALUES ('002');
                INSERT INTO tags VALUES(3, 'A'), (4, 'B');
                INSERT INTO membership VALUES ('001',3), ('001',3), ('001',4);
            """)
        path = "main.people.membership.tags.label"
        route = [dict(table="membership", **{"from": ["key"], "to": ["person"]}),
                 dict(table="tags", **{"from": ["tag"], "to": ["id"]})]
        profile = replace(self.profile, routes={path: {"via": route, "column": "label"}})
        rules = Rules.parse(dict(left_id="main.people.key", right_id="k",
                                 fields=[dict(left=path, right="tag", mode="exact", weight=100)]))
        with connect(profile) as conn:
            left = projection(conn, profile, rules.left_id, [path])
            result, orphan = match(left, [{"k": 9, "tag": "A"}], rules)
        self.assertEqual((result["score"], orphan["left_id"]), (100, "002"))
        values = result["evidence"][0]["left_values"]
        self.assertEqual(set(values), {"A", "B"})
        self.assertEqual(values["A"]["counts"], [1, 1])
        copied_id = replace(profile, routes={path: {"via": route[:1], "column": "person"}})
        with connect(copied_id) as conn, self.assertRaisesRegex(ValueError, "повторяет выбранный"):
            list(projection(conn, copied_id, rules.left_id, [path]))

    def test_unverified_relation_rejected(self):
        route = {"via": [{"table": "parent", "from": ["part"], "to": ["first"]}], "column": "second"}
        profile = replace(self.profile, routes={"label": route})
        with connect(profile) as conn, self.assertRaisesRegex(ValueError, "Связь"):
            list(projection(conn, profile, "main.people.key", ["label"]))

    def test_source_to_export(self):
        rules = {"left_id": "main.people.key", "right_id": "pk",
                 "fields": [dict(left="main.people.full name", right="label", mode="text", weight=100)]}
        (self.path / "rules").write_text(json.dumps(rules))
        (self.path / "rows").write_text(json.dumps(dict(pk=9, label="TEST PERSON")))
        output = self.path / "export"
        run = subprocess.run([sys.executable, "-m", "concord", "--left-source", str(self.profile_path),
                              "--right", str(self.path / "rows"), "--rules", str(self.path / "rules"),
                              "--output", str(output)], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        header, row = map(json.loads, output.read_text().splitlines())
        self.assertEqual(header["sources"]["left"]["table"], "people")
        self.assertEqual((row["left_id"], row["right_id"], row["status"]), ("001", 9, "accepted"))
