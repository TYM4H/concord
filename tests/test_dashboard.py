from datetime import date, timedelta
import unittest

from concord.dashboard import history_page, overview_page


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.counts = dict(accepted=2, review=3, unmatched=7)
        self.run = dict(key="first", name='Проверка <A>', time=f"{date.today()} 12:00",
                        sources="erp.users → crm.customers", counts=self.counts,
                        source_labels=["erp.users", "crm.customers"])
        self.old = dict(key="old", name="Архив", time=f"{date.today()-timedelta(days=40)} 12:00",
                        sources="sale → audit", counts=None)

    def test_overview_shows_possible_matches_and_links_to_threshold(self):
        body = overview_page([self.run, self.old], "first", self.counts, 2)
        self.assertIn("Проверка &lt;A&gt;", body)
        self.assertIn('<b>12</b>Всего строк', body)
        self.assertIn("Возможных совпадений без решения · 2", body)
        self.assertIn('/runs/first/threshold', body)
        self.assertIn('Левый источник', body)
        self.assertIn('<title id="overview-chart-title">', body)
        zero = {key: 0 for key in self.counts}
        body = overview_page([self.old, self.run], "old", zero, 0)
        self.assertIn("Выбранный запуск завершён", body)
        self.assertIn("Возможных совпадений без решения · 0", body)
        self.assertIn('/runs/old/threshold', body)

    def test_history_text_period_and_result_filters(self):
        runs = [self.old, self.run]
        body = history_page(runs, "old", {"q": ["CRM"], "period": ["30"], "result": ["saved"]})
        self.assertIn('href="/runs/first/"', body)
        self.assertNotIn('href="/runs/old/"', body)
        self.assertIn("2 / 3 / 7", body)
        body = history_page(runs, "old", {"result": ["current"]})
        self.assertIn("Архив", body)
        self.assertNotIn("Проверка &lt;A&gt;", body)
        self.assertIn("Открыт</span>", body)

    def test_history_pagination_and_empty_results(self):
        runs = [dict(self.run, key=f"run-{n:02d}") for n in range(21)]
        body = history_page(runs, "run-20", {"page": ["2"]})
        self.assertIn("21–21 из 21", body)
        self.assertIn('href="/runs/run-00/"', body)
        self.assertNotIn('href="/runs/run-01/"', body)
        body = history_page(runs, "run-20", {"q": ["<пусто>"]})
        self.assertIn("не найдено", body)
        self.assertIn("&lt;пусто&gt;", body)
        with self.assertRaises(ValueError):
            history_page(runs, "run-20", {"period": ["-1"]})
