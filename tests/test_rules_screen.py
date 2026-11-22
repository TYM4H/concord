import unittest

from concord.rules import Rules
from concord.rules_screen import edited_rules, rules_page


class RulesScreenTests(unittest.TestCase):
    def setUp(self):
        self.rules = Rules.parse(dict(left_id="каталог.id", right_id="справочник.id", fields=[
            dict(left="название <старое>", right="имя", mode="text", weight=20),
            dict(left="код", right="номер", mode="exact", weight=30, block_on_conflict=False),
        ], review=52.5, accept=88, block_limit=8))
        self.form = dict(mode_1=["text"], weight_0=["17.5"], weight_1=["32.5"],
                         review=["51.25"], accept=["82.75"], block_limit=["5"])

    def test_edit_starts_from_saved_run(self):
        page = rules_page(self.rules, {})
        self.assertIn('action="/new"', page)
        self.assertIn('name="mode_0" value="text" checked', page)
        self.assertNotIn('name="mode_1" value="text" checked', page)
        for value in ("каталог.id", "справочник.id", 'value="52.5"', 'value="88"',
                      "название &lt;старое&gt;"):
            self.assertIn(value, page)

    def test_draft_keeps_original_fields_and_old_settings(self):
        draft = edited_rules(self.rules, self.form)
        self.assertEqual((draft.review, draft.accept), (51.25, 82.75))
        self.assertEqual(draft.fields[0].left, "название <старое>")
        self.assertEqual([(f.mode, f.weight) for f in draft.fields], [("exact", 17.5), ("text", 32.5)])
        self.assertEqual((self.rules.review, self.rules.fields[0].mode), (52.5, "text"))
        self.assertEqual([f.block_on_conflict for f in draft.fields], [True, False])

    def test_change_conflict_policy_per_field(self):
        draft = edited_rules(self.rules, dict(self.form, block_on_conflict_0=["score_only"], block_on_conflict_1=["block"]))
        self.assertEqual([f.block_on_conflict for f in draft.fields], [False, True])
        self.assertIn('value="score_only" selected', rules_page(draft, {}))

    def test_invalid_form_rejected(self):
        for changes in (dict(review=["90"]), dict(accept=["nan"]), dict(weight_1=["0"]),
                        dict(mode_0=["unknown"]), dict(weight_0=[]),
                        dict(block_on_conflict_0=["unknown"]), dict(block_on_conflict_0=["score_only", "block"])):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                edited_rules(self.rules, {**self.form, **changes})
