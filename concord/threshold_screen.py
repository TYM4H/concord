"""Предпросмотр порога автосовпадения на баллах сохранённого запуска."""
import json

from .dashboard import number
from .view_html import badge, html, identifier, layout


def _percent(value):
    return str(value).removesuffix(".0")


def _record(item, side):
    values = []
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    for field in evidence[:3]:
        if not isinstance(field, dict):
            continue
        path = field.get(side + "_path", "")
        variants = field.get(side + "_values") or {}
        if not isinstance(variants, dict) or not isinstance(path, str):
            continue
        original = next((raw for variant in variants.values() if isinstance(variant, dict)
                         for raw in (variant.get("originals") or ())), None)
        if original is not None:
            text = str(original)
            preview = text if len(text) <= 80 else text[:79] + "…"
            values.append(f'<small>{html(path.rsplit(".", 1)[-1])} · {html(preview)}</small>')
    return f'<td><code>{identifier(item[side + "_id"])}</code>{"".join(values)}</td>'


def _examples(db, accept, above, only_eligible):
    comparison, order = (">=", "s.score ASC,s.n DESC") if above else ("<", "s.score DESC,s.n ASC")
    eligible_filter = ""
    if only_eligible:
        eligible_filter = " AND s.eligible=1"
        order = "s.score ASC,s.n ASC" if above else "s.score DESC,s.n DESC"
    records = db.execute(f"""
        SELECT r.n,r.body,s.score,s.eligible FROM pair_score s JOIN result r ON r.n=s.n
        WHERE s.score {comparison} ?{eligible_filter} ORDER BY {order} LIMIT 5
    """, (accept,))
    rows = []
    for n, body, score, eligible in records:
        item = json.loads(body)
        projected = badge("accepted" if above and eligible == 1 else "review")
        if eligible == -1 and above:
            projected = "Недостаточно данных для прогноза"
        elif eligible == 0:
            projected += '<small>Расхождение полей с включённой блокировкой</small>'
        rows.append(f'<tr>{_record(item, "left")}{_record(item, "right")}'
                    f'<td>{_percent(score)}%</td><td>{badge(item["status"])}</td>'
                    f'<td>{projected}</td><td><a href="/pair?n={n}">Основания</a></td></tr>')
    if not rows:
        rows.append('<tr><td colspan="6">В этом диапазоне сохранённых пар нет.</td></tr>')
    title = "На пороге и выше" if above else "Ниже порога"
    return f"""<section><div class="rule-panel-heading"><h2>{title}</h2></div>
        <div class="scroll"><table><thead><tr><th>Запись слева</th><th>Запись справа</th><th>Балл</th>
        <th>В этом запуске</th><th>При пороге {_percent(accept)}%</th><th></th></tr></thead>
        <tbody>{''.join(rows)}</tbody></table></div></section>"""


def threshold_page(db, rules, params, snapshot=None):
    current, lower = _percent(rules.accept), _percent(rules.review)
    values = params.get("accept", [current])
    raw = values[0] if len(values) == 1 else ""
    only_eligible = params.get("examples") == ["eligible"]
    try:
        accept = float(raw)
        valid = rules.review < accept <= 100
    except ValueError:
        valid = False
    form = f"""<a href="/">← К результатам</a><h1>Подбор порога автосовпадения</h1>
        <section class="form-group"><form action="/threshold" method="get" class="match-filter">
        <label for="accept">Порог автосовпадения, %</label><input id="accept" name="accept"
        type="number" step="any" max="100" required value="{html(raw)}">
        <label><input type="checkbox" name="examples" value="eligible"{" checked" if only_eligible else ""}>
        Примеры без блокирующих расхождений</label>
        <button>Показать прогноз</button></form>
        <p>Порог этого запуска — {current}%. Нижний порог кандидата — {lower}%.
        <a href="/rules">Изменить оба порога в правилах</a></p></section>"""
    if not valid:
        return layout(form + f'<p role="alert">Введите порог больше {lower}% и не выше 100%.</p>')

    matches, = db.execute("SELECT COUNT(*) FROM pair_score WHERE eligible=1 AND score>=?", (accept,)).fetchone()
    old, = db.execute("SELECT COUNT(*) FROM result WHERE status='accepted'").fetchone()
    unknown, = db.execute("SELECT COUNT(*) FROM pair_score WHERE eligible=-1 AND (score IS NULL OR score>=?)",
                          (accept,)).fetchone()
    delta = matches - old
    change = f"{delta:+,}".replace(",", " ") if delta else "0"
    summary = (f"При выбранном пороге ожидается минимум {number(matches)}. Для {number(unknown)} пар в результате "
               f"недостаточно данных для точного прогноза." if unknown else
               f"При выбранном пороге ожидается <b>{number(matches)}</b> (изменение {change}).")
    apply = f"""<form action="/new" method="get"><input type="hidden" name="from_threshold" value="1">
        <input type="hidden" name="review" value="{lower}"><input type="hidden" name="accept" value="{_percent(accept)}">
        <button class="primary-action">В мастер с порогом {_percent(accept)}%</button></form>"""
    source_hint = "" if snapshot is not None else '<p>Для нового расчёта выберите исходные данные в мастере.</p>'
    return layout(form + f"""<section class="form-group"><h2>Прогноз</h2>
        <p>Автосовпадений в этом запуске — {number(old)}. {summary}</p>
        <small>Автосовпадение требует достижения порога и отсутствия расхождений по полям с включённой блокировкой.
        Учитывается решение алгоритма по сохранённым признакам.</small>{source_hint}{apply}</section>
        <h2>Примеры у границы</h2><p>С каждой стороны показано до пяти ближайших пар
        с баллом от {lower}%.
        <a href="/scores?group=below">Сравнения ниже нижнего порога</a></p>"""
        + _examples(db, accept, False, only_eligible) + _examples(db, accept, True, only_eligible))
