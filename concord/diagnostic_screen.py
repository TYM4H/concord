"""Разбор результата «Без пары» для записи из левого источника."""
import json
from urllib.parse import urlencode

from .diagnostics import explain_pair, search_right
from .matching import values
from .view_html import REASONS, badge, html, identifier, layout, outcome_label, variants


def diagnostic_link(n, query, page, right=None):
    params = {"n": n, "q": query, "page": page}
    if right is not None:
        params["right"] = right
    return "/diagnose?" + html(urlencode(params))


def search_panel(snapshot, rules, n, left_key, query, page):
    left_values = values(snapshot, 0, left_key)
    left_fields = "".join(
        f'<div><small>{html(field.left)}</small>{variants(left_values.get(index, {}))}</div>'
        for index, field in enumerate(rules.fields)
    )
    records, has_next = search_right(snapshot, rules, query, page)
    rows = []
    for record in records:
        link = diagnostic_link(n, query, page, record["key"])
        preview = " · ".join(html(value) for value in record["preview"]) or "—"
        rows.append(f'<tr><td>{identifier(record["id"])}</td><td>{preview}</td>'
                    f'<td><a href="{link}">Разобрать пару</a></td></tr>')
    if not records:
        rows.append('<tr><td colspan="3">На этой странице записей не найдено.</td></tr>')
    links = []
    if page > 1:
        links.append(f'<a href="{diagnostic_link(n, query, page - 1)}">Назад</a>')
    if has_next:
        links.append(f'<a href="{diagnostic_link(n, query, page + 1)}">Далее</a>')
    return f"""<details class="diagnostic-left" open><summary>Значения выбранной записи слева</summary>
        <div class="diagnostic-sample">{left_fields}</div></details>
        <h2>Найти запись справа</h2><section><form class="diagnostic-search" method="get" action="/diagnose">
        <input type="hidden" name="n" value="{n}">
        <label for="q">ID или значение поля</label>
        <div><input id="q" type="search" name="q" maxlength="1024" value="{html(query)}"><button>Найти</button></div></form>
        <div class="scroll"><table><thead><tr><th>{html(rules.right_id)}</th><th>Значения справа</th><th></th>
        </tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        <footer>Страница {page}<div>{' &nbsp; '.join(links)}</div></footer></section>"""


def block_reason(field):
    descriptions = []
    eligible = set(field["eligible"])
    for norm in field["shared"]:
        counts = field["left_values"][norm]["counts"]
        status = "прошло отбор" if norm in eligible else "превышен предел частоты"
        descriptions.append(f'<div><code>{html(norm)}</code> — {status}'
                            f' (слева {int(counts[0])}, справа {int(counts[1])})</div>')
    if descriptions:
        return "".join(descriptions)
    if not field["left_values"] or not field["right_values"]:
        return "Пропуск значения"
    return "Общих нормализованных значений нет"


def explanation(result, rules, *, pair_only=False):
    evaluated = result["stage"] == "below_threshold"
    if evaluated:
        score = result["score"]
        heading = "Пара прошла отбор, но набрала меньше порога"
        reason = (f'При запуске пара набрала <strong>{score:g}%</strong>. '
                  f'Нижний порог кандидата — {rules.review:g}%. '
                  f'До порога не хватило {rules.review - score:g} п. п.')
    else:
        heading = "Пара не попала в кандидаты"
        if any(f["shared"] for f in result["fields"]):
            reason = "Все общие нормализованные значения превысили предел частоты. Подробное сравнение не запускалось."
        else:
            reason = "Ни по одному полю не нашлось общего нормализованного значения. Подробное сравнение не запускалось."
    rows = []
    for f in result["fields"]:
        scoring = ""
        if evaluated:
            used = f["used_value"]
            basis = f' · учтено <code>{html(used)}</code>' if used is not None else ""
            scoring = (f'<div class="field-result {f["outcome"]}"><b>{outcome_label(f)}</b>'
                       f' · вклад {f["points"]:g} из {f["weight"]:g}{basis}</div>')
        mode = {"text": "Нормализованный текст", "exact": "Точное значение"}[f["mode"]]
        rows.append(f"""<tr><th scope="row">{html(f['left_path'])} ↔ {html(f['right_path'])}<small>{mode}</small></th>
            <td>{variants(f['left_values'])}</td><td>{variants(f['right_values'])}</td>
            <td>{block_reason(f)}{scoring}</td></tr>""")
    progress = (f'<progress aria-label="Сходство" max="100" value="{score:g}">{score:g}%</progress>'
                if evaluated else "")
    return f"""<div class="sources diagnostic-pair">
        <div><small>Левая запись · {html(rules.left_id)}</small><code>{identifier(result['left_id'])}</code></div>
        <b aria-hidden="true">→</b>
        <div><small>Правая запись · {html(rules.right_id)}</small><code>{identifier(result['right_id'])}</code></div>
        {'<span class="badge unmatched">Ниже нижнего порога</span>' if pair_only else badge('unmatched')}</div>
        <section class="diagnostic-verdict"><h2>{heading}</h2><p>{reason}</p>{progress}</section>
        <section class="diagnostic-evidence"><div class="evidence-title"><h2>Сравнение полей</h2></div>
        <div class="scroll" tabindex="0" role="region" aria-label="Сравнение полей">
        <table><thead><tr><th>Признак</th><th>Слева</th><th>Справа</th><th>Основание</th></tr></thead>
        <tbody>{''.join(rows)}</tbody></table></div></section>"""


def diagnosis_page(review_db, rules, n, params, snapshot, unavailable_message="", *, legacy_limit=None):
    row = review_db.execute("""SELECT left_key, body FROM result
                               WHERE n=? AND status='unmatched' AND left_key IS NOT NULL""", (n,)).fetchone()
    if row is None:
        return None
    left_key, body = row
    info = '<a href="/?status=unmatched&amp;side=left">← Записи без пары</a>'
    left_record = f'<p>Запись слева · <code>{html(rules.left_id)} = {identifier(json.loads(left_key))}</code></p>'
    title = '<h1>Почему не найдена пара</h1>'
    if snapshot is None:
        message = unavailable_message or "Для этого запуска недоступен снимок значений для поиска справа."
        record = json.loads(body)
        reason = REASONS.get(record.get("reason"), "Причина не указана.")
        link = ("Открыть сохранённое сравнение с ближайшим кандидатом" if record.get("best_comparison")
                else "Открыть сохранённое основание")
        info += title + left_record + f'<p>{html(message)}</p><section class="field"><strong>Результат запуска</strong>'
        info += f'<p>{html(reason)}</p><a href="/pair?n={n}">{link}</a></section>'
        return layout(info)

    query = params.get("q", [""])[0]
    page = int(params.get("page", ["1"])[0])
    if page < 1:
        raise ValueError("Номер страницы должен быть положительным")
    if "right" in params:
        result = explain_pair(snapshot, rules, left_key, params["right"][0], legacy_limit=legacy_limit)
        if result is None:
            return None
        back = diagnostic_link(n, query, page)
        info += f' <span class="crumb-divider">/</span> <a href="{back}">Поиск правой записи</a>'
        info += title + explanation(result, rules)
    else:
        info += title + left_record + f'<p><a href="/compare?n={n}">Сравнить все варианты для этой записи</a></p>'
        info += search_panel(snapshot, rules, n, left_key, query, page)
    return layout(info)
