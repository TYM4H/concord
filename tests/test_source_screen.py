from contextlib import closing
import importlib.util
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from concord.rules import Rules
from concord.run_snapshot import open_snapshot
from concord.runs import export_run
from concord.source_screen import connect_page, inspect_page, sources_page


class SourceScreenTests(unittest.TestCase):
    def setUp(self):
        self.rules = Rules.parse(dict(left_id="main.people.id", right_id="key", fields=[
            dict(left="contacts.email", right="email", weight=80, mode="text"),
            dict(left="main.people.name", right="name", weight=20, mode="text"),
        ]))
        metadata = {
            "left": {"driver": "sqlite", "database": "/tmp/реестр.db", "schema": "main", "table": "people",
                     "routes": {"contacts.email": {"via": [
                         {"table": "contact", "from": ["id"], "to": ["person"]}], "column": "address"}}},
            "right": {"format": "jsonl", "file": "<архив>.jsonl"},
        }
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "result.jsonl"
        export_run(path, [{"main.people.id": 5, "main.people.name": "Мария", "contacts.email": ["<html>", "b"]}],
                   [{"key": "X", "email": "A"}], self.rules, metadata)
        with path.open(encoding="utf-8") as stream:
            self.header = json.loads(stream.readline())
        self.db = open_snapshot(path, self.header)
        self.addCleanup(self.db.close)

    def test_both_sources_retain_original_scope_and_routes(self):
        page = sources_page(self.rules, self.header, self.db, {})
        self.assertIn("реестр.db", page)
        self.assertIn("SQLite", page)
        self.assertIn("people (id) → contact (person)", page)
        self.assertIn(">address</code>", page)
        self.assertIn("&lt;архив&gt;.jsonl", page)
        self.assertNotIn("<архив>", page)
        second = sources_page(self.rules, self.header, self.db, {"side": ["right"]})
        self.assertIn(">JSONL</dd>", second)
        self.assertNotIn("contact (person)", second)

    def test_samples_legacy_and_unknown_side(self):
        page = sources_page(self.rules, self.header, self.db, {"view": ["sample"]})
        self.assertIn("Мария", page)
        self.assertIn("&lt;html&gt;", page)
        self.assertNotIn("<html><br>", page)
        legacy = sources_page(self.rules, {}, None, {"view": ["sample"]})
        self.assertIn("нет снимка исходных значений", legacy)
        self.assertNotIn("Подключено", legacy)
        with self.assertRaises(ValueError):
            sources_page(self.rules, self.header, self.db, {"side": ["else"]})


class SourceConnectionTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "Нужны requirements-db.txt")
    def test_real_catalog_in_profile_form(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / "people.sqlite")) as db:
                db.executescript("""CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                    CREATE TABLE contact(mail TEXT, person INTEGER REFERENCES people(id));""")
            profile = root / "profile.json"
            profile.write_text(json.dumps({"driver": "sqlite", "database": "people.sqlite", "schema": "main",
                                           "table": "people"}))
            page = inspect_page({"side": ["right"], "profile": [str(profile)], "password": ["SENSITIVE"]},
                                "test-token")
            for value in ("Подключение проверено", "Только чтение", "primary-action", "FK → main.people.id",
                          "PRIMARY KEY", "TEXT", "NOT NULL", 'name="right_profile"', 'value="main.people.id" selected', "people.sqlite", "test-token"):
                self.assertIn(value, page)
            self.assertNotIn("SENSITIVE", page)

    def test_profile_errors_keep_password_out_of_response(self):
        form = connect_page({}, "token")
        self.assertIn('method="post"', form)
        self.assertIn('type="password"', form)
        result = inspect_page({"profile": ["/нет/профиля.json"], "password": ["SENSITIVE"]}, "token")
        self.assertIn("По указанному пути нет файла", result)
        self.assertNotIn("SENSITIVE", result)

    @unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "Нужны requirements-db.txt")
    def test_direct_connection_save_and_retry(self):
        from html.parser import HTMLParser
        from concord.sources.profile import Profile

        class Fields(HTMLParser):
            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "input" and values.get("name") == "ticket":
                    self.ticket = values["value"]

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "события.db"
            with closing(sqlite3.connect(database)) as db:
                db.executescript("CREATE TABLE people(id INTEGER PRIMARY KEY); CREATE TABLE places(code TEXT);")
            form = dict(mode=["direct"], side=["left"], driver=["sqlite"], database=[str(database)],
                        password=["SENSITIVE"])
            check = inspect_page(form, "token", root)
            self.assertIn('name="table"', check)
            self.assertIn('value="places"', check)
            self.assertIn('value="save"', check)
            self.assertFalse((root / ".concord-profiles").exists())
            parser = Fields()
            parser.feed(check)
            save = dict(form, intent=["save"], table=["places"], ticket=[parser.ticket])
            for _ in range(2):
                saved = inspect_page(save, "token", root)
                self.assertIn("Профиль сохранён", saved)
                self.assertIn('action="/new"', saved)
                self.assertIn('name="left_profile"', saved)
                self.assertIn('value="main.places.code"', saved)
                self.assertNotIn("SENSITIVE", saved)
            directory = root / ".concord-profiles"
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            dest, = directory.glob("*")
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            profile = Profile.load(dest)
            self.assertEqual((profile.driver, profile.database, profile.schema, profile.table),
                             ("sqlite", str(database.resolve()), "main", "places"))
            self.assertNotIn("SENSITIVE", dest.read_text())
            self.assertNotIn("password", json.loads(dest.read_text()))
            invalid = inspect_page(dict(save, table=["missing"]), "token", root)
            self.assertIn("Выберите таблицу из проверенной схемы", invalid)
            self.assertEqual(list(directory.iterdir()), [dest])
