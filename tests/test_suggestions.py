from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from concord.sources.adapters import connect
from concord.sources.profile import Profile
from concord.suggestions.ranking import suggest_fields
from concord.suggestions.sample import sample_fields


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "Нужны requirements-db.txt")
class SuggestionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.left = self.source("crm", "people", """
            CREATE TABLE people(pk integer primary key, chosen text, x7 text, town text, empty text, raw blob);
            CREATE TABLE contacts(key integer primary key, address text);
            CREATE TABLE links(person integer references people(pk), contact integer references contacts(key));
            INSERT INTO people VALUES(1,'01','Copper','Москва',null,null),
                (2,'02','Quartz','Москва',null,null),(3,'03','Onyx','Москва',null,null);
            INSERT INTO contacts VALUES(7,'A@example.test'),(8,'B@example.test'),(9,'C@example.test');
            INSERT INTO links VALUES(1,7),(1,7),(1,8),(2,9);
        """)
        self.right = self.source("erp", "accounts", """
            CREATE TABLE accounts(pk integer primary key, label text, login text, backup text, town text);
            INSERT INTO accounts VALUES(1,'quartz','B@example.test','z@example.test','Москва'),
                (2,'COPPER','a@example.test','y@example.test','Москва'),
                (3,'topaz','D@example.test','w@example.test','Москва');
        """)

    def source(self, file, table, sql):
        with closing(sqlite3.connect(self.root / file)) as db:
            db.executescript(sql)
        path = self.root / (file + ".json")
        path.write_text(json.dumps(dict(driver="sqlite", database=file, schema="main", table=table)))
        return Profile.load(path)

    def sample(self, profile, id_column, **options):
        with connect(profile) as db:
            return sample_fields(db, profile, id_column, **options)

    def test_autodiscovery_examples_and_duplicate_join(self):
        left = self.sample(self.left, "chosen")
        right = self.sample(self.right, "main.accounts.pk")
        report = suggest_fields(left, right)
        rows = {(p["left"]["path"], p["right"]["column"]): p for p in report["suggestions"]}
        direct = rows["main.people.x7", "label"]
        self.assertEqual(direct["shared_values"], 2)
        self.assertIn("Copper", direct["left"]["examples"])
        self.assertIn("COPPER", direct["right"]["examples"])
        pair = rows["main.people.links.contacts.address", "login"]
        self.assertEqual(pair["left"]["distinct_values"], 3)
        self.assertEqual(pair["left"]["populated_entities"], 2)
        self.assertEqual(pair["left"]["max_value_frequency"], 1)
        self.assertEqual([h["table"] for h in pair["left"]["via"]], ["links", "contacts"])
        self.assertIn(("main.people.links.contacts.address", "backup"), rows)
        self.assertGreater(pair["rank"], rows["main.people.links.contacts.address", "backup"]["rank"])
        self.assertEqual({f.name for f in left.fields}, {"x7", "town", "empty", "address"})
        self.assertFalse(any(p["left"]["column"] in ("town", "empty") for p in rows.values()))

    def test_depth_sample_and_validation(self):
        sample = self.sample(self.left, "chosen", sample_size=2, max_hops=0)
        self.assertEqual(sample.size, 2)
        self.assertEqual(next(f.values for f in sample.fields if f.name == "x7"),
                         {"copper": "Copper", "quartz": "Quartz"})
        self.assertFalse(any(f.via for f in sample.fields))
        with self.assertRaisesRegex(ValueError, "идентификатор"):
            self.sample(self.left, "missing")
        with self.assertRaises(ValueError):
            self.sample(self.left, "pk", sample_size=501)

    def test_ambiguous_links_remain_distinct(self):
        source = self.source("jobs", "jobs", """
            CREATE TABLE staff(id integer primary key, nickname text);
            CREATE TABLE jobs(id integer primary key, owner int references staff(id),
                              reviewer int references staff(id));
            INSERT INTO staff VALUES(1,'Alpha'),(2,'Beta');
            INSERT INTO jobs VALUES(1,1,2);
        """)
        sample = self.sample(source, "id")
        self.assertEqual(len({f.path for f in sample.fields}), 2)
        self.assertEqual({f.via[0]["from"][0]: list(f.values) for f in sample.fields},
                         {"owner": ["alpha"], "reviewer": ["beta"]})

    def test_incompatible_types_and_empty_source(self):
        source = self.source("s", "measure", """
            CREATE TABLE measure(id int primary key, label int);
            INSERT INTO measure VALUES(1,200),(2,400);
        """)
        result = suggest_fields(self.sample(source, "id"), self.sample(self.right, "pk"))
        self.assertFalse(result["suggestions"])
        empty = self.source("t", "measure", "CREATE TABLE measure(id int primary key, label text);")
        report = suggest_fields(self.sample(empty, "id"), self.sample(self.right, "pk"))
        self.assertEqual(report["left"]["sampled_entities"], 0)
        self.assertTrue(report["left"]["warnings"])
        self.assertEqual(report["suggestions"], [])

    def test_cli(self):
        command = [sys.executable, "-m", "concord.suggestions", "--left-source", str(self.root / "crm.json"),
                   "--left-id", "chosen", "--right-source", str(self.root / "erp.json"), "--right-id", "pk"]
        report = json.loads(subprocess.check_output(command + ["--json"], text=True))
        self.assertTrue(report["suggestions"])
        preview = subprocess.check_output(command, text=True)
        self.assertIn("A@example.test", preview)
        self.assertIn("a@example.test", preview)
        self.assertIn("main.people.links.contacts.address", preview)
