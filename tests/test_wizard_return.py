from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
import tempfile
import unittest

from concord.new_screen import new_page
from concord.source_connect import connect_page, inspect_page
from concord.viewer import scoped_html
from concord.workspace import FirstRun
from concord.wizard_state import restore_draft, start_connection


class FormFields(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        value = dict(attrs)
        if tag == "form":
            self.current = {"action": value.get("action"), "method": value.get("method"), "inputs": {}}
            self.forms.append(self.current)
        if self.current is not None and tag == "input" and "name" in value:
            self.current["inputs"].setdefault(value["name"], []).append(value.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None

    def find(self, path, name=None):
        return next(f for f in self.forms if f["action"] == path and (not name or name in f["inputs"]))


class WizardReturnTests(unittest.TestCase):
    def test_existing_settings_survive_connection_catalog_and_return(self):
        try:
            import sqlalchemy
        except ImportError:
            self.skipTest("Нужны requirements-db.txt")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with closing(sqlite3.connect(root / "right.sqlite")) as db:
                db.execute("CREATE TABLE cases(k INTEGER PRIMARY KEY)")
            draft = dict(connect_side=["right"], left_path=["/profiles/left & one.json"], left_kind=["profile"],
                         left_id=["crm.people.identity"], right_kind=["snapshot"], right_id=["obsolete"],
                         field_left=["title", ""], field_right=["name", ""], field_weight=["100", "1"],
                         field_mode=["exact", "text"], field_block_on_conflict=["score_only", "block"],
                         review=["71"], left_password=["LEFT_SECRET"],
                         right_password=["RIGHT_SECRET"], token=["stale"])
            scope = "/runs/123"
            initial = start_connection(draft)
            body = scoped_html(connect_page(initial, "csrf"), scope)
            self.assertNotIn("SECRET", body)
            self.assertNotIn("stale", body)
            self.assertIn(scope + "/new/return", body)
            forms = FormFields(body)
            cancel = forms.find(scope + "/new/return")
            self.assertEqual(restore_draft(cancel["inputs"])["right_id"], ["obsolete"])
            source = forms.find(scope + "/sources/connect", "database")["inputs"]
            source.update(driver=["sqlite"], database=[str(root / "right.sqlite")], password=["DB_SECRET"])
            for stage in ("inspect", "save"):
                source["intent"] = [stage]
                body = scoped_html(inspect_page(source, "csrf", root), scope)
                self.assertNotIn("SECRET", body)
                if stage == "inspect":
                    source = FormFields(body).find(scope + "/sources/connect", "ticket")["inputs"]
                    source.update(driver=["sqlite"], table=["cases"])
            ready = FormFields(body).find(scope + "/new/return", "right_profile")
            self.assertEqual(ready["method"], "post")
            self.assertIn('name="right_id" required', body)
            carry = dict(ready["inputs"], right_id=["main.cases.k"])
            restored = restore_draft(carry)
            for key in ("left_path", "left_kind", "left_id", "review", "field_left", "field_right", "field_weight"):
                self.assertEqual(restored[key], draft[key])
            self.assertEqual(restored["right_id"], ["main.cases.k"])
            self.assertEqual(restored["right_kind"], ["profile"])
            self.assertEqual(restored["field_block_on_conflict"], ["score_only", "block"])
            self.assertTrue(Path(restored["right_path"][0]).is_file())
            result = scoped_html(new_page(FirstRun(), FirstRun.rules, "csrf", form=restored), scope)
            self.assertIn('value="crm.people.identity"', result)
            self.assertIn('value="main.cases.k"', result)
            self.assertIn('value="score_only" selected', result)
            self.assertIn('action="/runs/123/new/connect"', result)

    def test_independent_forms_do_not_mix(self):
        a = start_connection(dict(connect_side=["left"], right_id=["first"], right_password=["secret"]))
        b = start_connection(dict(connect_side=["left"], right_id=["second"], draft_password=["secret"]))
        self.assertEqual(restore_draft(a), dict(right_id=["first"]))
        self.assertEqual(restore_draft(b), dict(right_id=["second"]))
