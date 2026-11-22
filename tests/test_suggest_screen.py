from contextlib import closing
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from concord.new_screen import create_run
from concord.rules import Rules
from concord.suggest_screen import selected_suggestion_form, suggest_page


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = {}
        self.options = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden":
            self.hidden[attrs["name"]] = [attrs["value"]]
        if tag == "input" and attrs.get("name") == "choice":
            self.options.append(attrs["value"])


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "Нужны requirements-db.txt")
class SuggestScreenTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.form = dict(review=["50"], accept=["85"], block_limit=["64"], left_password=["test-password"])
        for side, ddl in (("left", """
            CREATE TABLE entities(id integer primary key);
            CREATE TABLE contacts(id int primary key, ent integer references entities(id), email text);
            INSERT INTO entities VALUES (1), (2);
            INSERT INTO contacts VALUES (1,1,'one@example.test'), (2,2,'two@example.test');
        """), ("right", """
            CREATE TABLE entities(id integer primary key, email text);
            INSERT INTO entities VALUES (12,'ONE@example.test'), (13,'two@example.test');
        """)):
            with closing(sqlite3.connect(self.folder / side)) as db:
                db.executescript(ddl)
            profile = self.folder / (side + ".json")
            profile.write_text(json.dumps(dict(driver="sqlite", database=side, schema="main", table="entities")))
            self.form.update({side + "_id": ["id"], side + "_path": [str(profile)], side + "_kind": ["profile"]})

    def test_choose_comparison_with_examples_and_route(self):
        path = "main.entities.contacts.email"
        page = suggest_page(None, dict(self.form, field_left=[path], field_right=["main.entities.email"],
                                      field_block_on_conflict=["score_only"]), "csrf")
        self.assertIn('value="score_only" selected', page)
        self.assertIn("one@example.test", page)
        self.assertIn("ONE@example.test", page)
        self.assertNotIn("test-password", page)
        self.assertIn('type="password" name="left_password"', page)
        inputs = Inputs()
        inputs.feed(page)
        self.assertEqual(len(inputs.options), 1)
        self.assertNotIn("example.test", inputs.options[0])
        i, _ = json.loads(inputs.options[0])
        chosen = {**inputs.hidden, "choice": inputs.options, f"mode_{i}": ["text"], f"weight_{i}": ["37"]}
        normalized, routes = selected_suggestion_form(chosen)
        self.assertEqual(normalized["field_block_on_conflict"], ["score_only"])
        switched, _ = selected_suggestion_form({**chosen, f"block_on_conflict_{i}": ["block"]})
        self.assertEqual(switched["field_block_on_conflict"], ["block"])
        self.assertEqual(normalized["field_left"], [path])
        self.assertEqual(normalized["field_right"], ["main.entities.email"])
        self.assertEqual(normalized["field_weight"], ["37.0"])
        self.assertEqual(normalized["left_id"], ["main.entities.id"])
        self.assertEqual(routes["left"][path]["via"], [dict(table="contacts", **{"from": ["id"], "to": ["ent"]})])
        result = create_run(SimpleNamespace(folder=self.folder), None, chosen)
        header, *records = map(json.loads, result.read_text().splitlines())
        self.assertIs(header["rules"]["fields"][0]["block_on_conflict"], False)
        self.assertEqual(header["sources"]["left"]["routes"], routes["left"])
        self.assertEqual(sorted((r["left_id"], r["right_id"]) for r in records if r["status"] == "accepted"),
                         [(1, 12), (2, 13)])
        with self.assertRaises(ValueError):
            selected_suggestion_form(inputs.hidden)

    def test_requires_two_profile_sources(self):
        with self.assertRaisesRegex(ValueError, "профиль"):
            suggest_page(None, {**self.form, "left_kind": ["snapshot"]}, "csrf")

    def test_no_suggestions_keeps_manual_form_and_source_ids(self):
        with closing(sqlite3.connect(self.folder / "right")) as db:
            db.execute("DELETE FROM entities")
            db.commit()
        rules = Rules.parse(dict(left_id="a", right_id="b", fields=[
            dict(left="first", right="second", mode="text", weight=1)]))
        page = suggest_page(SimpleNamespace(rules=rules, snapshot=None), self.form, "csrf")
        self.assertIn("Выбранная таблица пуста.", page)
        self.assertIn('name="left_id" value="id"', page)
        self.assertIn('name="right_path" value="' + self.form["right_path"][0] + '"', page)
        self.assertNotIn("test-password", page)
