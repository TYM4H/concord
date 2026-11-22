"""Рейтинг фактически оценённых пар, включая сравнения ниже нижнего порога."""
import json
from urllib.parse import urlencode

from .diagnostics import explain_pair
from .diagnostic_screen import explanation
from .view_html import badge, html, identifier, layout


PAGE_SIZE = 20


def scores_page(db, snapshot, params):
    # Сохранённые в результате пары всегда выше отсеянных: их диапазоны
    # разделены нижним порогом кандидата. Читаем только нужную страницу.
    groups = params.get("group", ["all"])
    if groups not in (["all"], ["below"]):
        raise ValueError("Неизвестная группа баллов")
    below_only = groups == ["below"]
    retained = 0 if below_only else db.execute(
        "SELECT count(*) FROM pair_score WHERE score IS NOT NULL").fetchone()[0]
    missed = 0
    notice = ""
    if snapshot is None:
        notice = "<p>Снимок сравнений недоступен. Показаны баллы из сохранённого результата.</p>"
    else:
        missed = snapshot.execute("SELECT count(*) FROM missed").fetchone()[0]
        version, = snapshot.execute("SELECT version FROM snapshot_metadata").fetchone()
        if version < 2:
            notice = "<p>В этой версии снимка часть сравнений ниже нижнего порога не сохранялась.</p>"
    total = retained + missed
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(pages, max(1, int(params.get("page", ["1"])[0])))
    offset = (page - 1) * PAGE_SIZE
    rows = []

    def row(left, right, score, status, url):
        return (f'<tr><td>{identifier(left)}</td><td>{identifier(right)}</td><td>{score:g}%</td>'
                f'<td>{status}</td><td><a href="{html(url)}">Основания расчёта</a></td></tr>')

    if offset < retained:
        for n, score in db.execute("""
            SELECT n,score FROM pair_score WHERE score IS NOT NULL
            ORDER BY score DESC,n LIMIT ? OFFSET ?
        """, (PAGE_SIZE, offset)):
            status, body = db.execute("SELECT status,body FROM result WHERE n=?", (n,)).fetchone()
            pair = json.loads(body)
            rows.append(row(pair["left_id"], pair["right_id"], score, badge(status), f"/pair?n={n}"))
    if len(rows) < PAGE_SIZE and missed:
        for left, right, score in snapshot.execute("""
            SELECT left_id,right_id,score FROM missed ORDER BY score DESC,left_id,right_id LIMIT ? OFFSET ?
        """, (PAGE_SIZE - len(rows), max(0, offset - retained))):
            link = "/scored-pair?" + urlencode(dict(left=left, right=right))
            rows.append(row(json.loads(left), json.loads(right), score, "Ниже нижнего порога", link))
    links = []
    for other, label in ((page - 1, "Назад"), (page + 1, "Далее")):
        if 1 <= other <= pages:
            query = urlencode(dict(group=groups[0], page=other))
            links.append(f'<a href="/scores?{html(query)}">{label}</a>')
    empty = '<tr><td colspan="5">Нет сохранённых оценок.</td></tr>'
    return layout(f"""<a href="/">← Результаты запуска</a><h1>Все оценённые пары</h1>
        <p>По убыванию балла · <a href="/threshold">Настроить порог автосовпадения</a></p>
        {notice}<section><div class="tabs"><a href="/scores">Все пары</a>
        <a href="/scores?group=below">Ниже нижнего порога</a></div><div class="scroll">
        <table><thead><tr><th>ID слева</th><th>ID справа</th><th>Балл ↓</th>
        <th>Статус алгоритма</th><th></th></tr></thead><tbody>{''.join(rows) or empty}</tbody></table></div>
        <footer>Показано {min(total, offset + 1)}–{min(total, offset + PAGE_SIZE)} из {total}
        <div>{' &nbsp; '.join(links)}</div></footer></section>""")


def missed_pair_page(snapshot, rules, params, *, legacy_limit=None):
    if snapshot is None:
        return None
    left, right = params.get("left", []), params.get("right", [])
    if len(left) != 1 or len(right) != 1:
        return None
    if not snapshot.execute("SELECT 1 FROM missed WHERE left_id=? AND right_id=?", (left[0], right[0])).fetchone():
        return None
    comparison = explain_pair(snapshot, rules, left[0], right[0], legacy_limit=legacy_limit)
    if comparison is None:
        return None
    return layout('<a href="/scores?group=below">← Сравнения ниже нижнего порога</a>'
                  '<h1>Основания расчёта</h1>' + explanation(comparison, rules, pair_only=True))
