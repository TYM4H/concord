"""Сравнение кандидатов одной записи по фактическим результатам запуска."""
from datetime import datetime
from itertools import islice
import json

from .diagnostics import scored_miss
from .matching import values
from .view_html import REASONS, badge, html, identifier, layout, outcome_label

MANUAL_LABELS = {
    "pending": "Без решения",
    "confirmed": "Подтверждено",
    "rejected": "Отклонено",
    "deferred": "Отложено",
}
PAGE_SIZE = 9


def observed_values(values):
    if not values:
        return '<small>Нет значения</small>'
    parts = []
    for norm, value in values.items():
        originals = ", ".join(html(raw) for raw in value["originals"])
        parts.append(f'<div class="review-original">{originals}</div>'
                     f'<small class="review-normalized">→ <code>{html(norm)}</code></small>')
    return "".join(parts)


def decision_cell(candidate, token):
    n, item, action, note, updated_at, revision = candidate
    if item["status"] == "accepted":
        return '<td><small>Принято автоматически</small></td>'
    if item["status"] == "below_threshold":
        return '<td>—</td>'
    state = action or "pending"
    date = ""
    if updated_at:
        when = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).astimezone()
        date = (f'<time datetime="{html(updated_at)}">'
                f'{html(when.strftime("%d.%m.%Y %H:%M %z"))}</time>')
    return f"""<td><strong class="review-state {state}">{MANUAL_LABELS[state]}</strong>{date}
      <form method="post" action="/decision" class="review-form">
      <input type="hidden" name="n" value="{int(n)}">
      <input type="hidden" name="token" value="{html(token)}">
      <input type="hidden" name="revision" value="{int(revision or 0)}">
      <label for="note-{int(n)}">Основание решения <small>(необязательно)</small></label>
      <textarea id="note-{int(n)}" name="note" maxlength="2000" rows="2">{html(note or "")}</textarea>
      <div class="review-buttons"><button type="submit" name="action" value="confirmed">Подтвердить</button>
      <button type="submit" name="action" value="rejected">Отклонить</button>
      <button type="submit" name="action" value="deferred">Отложить</button></div></form></td>"""


def comparison_table(candidates, rules, token):
    # Из одного снимка запуска значения левого ID одинаковы для всех его вариантов.
    # Показываем их один раз напротив значений каждой предложенной правой записи.
    heads = []
    auto = []
    evidence_by_candidate = []
    for n, item, *_ in candidates:
        marker = f' id="candidate-{n}"' if n is not None else ""
        heads.append(f'<th scope="col"{marker}><small>{html(rules.right_id)}</small>'
                     f'<code>{identifier(item["right_id"])}</code></th>')
        if item["status"] == "below_threshold":
            status = '<span class="badge unmatched">● &nbsp;Ниже порога кандидата</span>'
            points = "".join(f'<div>{html(f["left_path"])}: {f["points"]:g} из {f["weight"]:g}</div>'
                             for f in item["evidence"])
            details = f'<details><summary>Основания расчёта</summary>{points}</details>'
        else:
            status = badge(item["status"])
            details = f'<a href="/pair?n={n}">Основания расчёта</a>'
        auto.append(f'<td>{status} <b>{html(item["score"])}%</b>'
                    f'<small>{html(REASONS[item["reason"]])}</small>{details}</td>')
        evidence_by_candidate.append({(f["left_path"], f["right_path"]): f
                                      for f in item.get("evidence", [])})
    rows = ['<tr><th scope="row">Решение алгоритма</th>' + "".join(auto) + '</tr>']
    for field in rules.fields:
        key = (field.left, field.right)
        comparisons = [evidence.get(key) for evidence in evidence_by_candidate]
        first = next((f for f in comparisons if f is not None), None)
        if first is None:
            continue
        rows.append(f'<tr><th scope="row"><small>{html(field.left)}</small>'
                    f'{observed_values(first["left_values"])}</th>')
        for f in comparisons:
            if f is None:
                rows.append('<td>Нет сравнения</td>')
                continue
            outcome = f["outcome"]
            rows.append(f'<td><small>{html(f["right_path"])}</small>{observed_values(f["right_values"])}'
                        f'<span class="review-outcome {html(outcome)}">{outcome_label(f)}</span></td>')
        rows.append('</tr>')
    decisions = "".join(decision_cell(c, token) for c in candidates)
    return f"""<section class="review-group"><div class="scroll">
      <table class="review-matrix"><thead><tr><th scope="col">Запись слева</th>{''.join(heads)}</tr></thead>
      <tbody>{''.join(rows)}<tr><th scope="row">Решение аналитика</th>{decisions}</tr></tbody></table>
      </div></section>"""


def comparison_page(db, rules, n, token, snapshot=None, page=None):
    row = db.execute("SELECT left_key,status FROM result WHERE n=?", (n,)).fetchone()
    if row is None or row[0] is None:
        return None
    left_key, status = row
    retained = db.execute("SELECT count(*) FROM result WHERE left_key=? AND status IN ('review','accepted')",
                          (left_key,)).fetchone()[0]
    complete = False
    misses = 0
    if snapshot is not None:
        version, = snapshot.execute("SELECT version FROM snapshot_metadata").fetchone()
        complete = version >= 2 or status == "unmatched"
        if complete:
            misses, = snapshot.execute("SELECT count(*) FROM missed WHERE left_id=?", (left_key,)).fetchone()
    count = retained + misses
    if page is None:
        # Открываем страницу с выбранной парой, в том числе после сохранения решения.
        previous, = db.execute("SELECT count(*) FROM result WHERE left_key=? AND n<?", (left_key, n)).fetchone()
        page = 1 + previous // PAGE_SIZE
    if page < 1 or page > max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE):
        return None
    offset = (page - 1) * PAGE_SIZE
    cursor = db.execute("""
        SELECT r.n, r.body, d.action, d.note, d.updated_at, d.revision
        FROM result r LEFT JOIN decision d ON r.n=d.n
        WHERE r.left_key=? AND r.status IN ('review', 'accepted') ORDER BY r.n
        LIMIT ? OFFSET ?
    """, (left_key, PAGE_SIZE, offset))
    records = [(key, json.loads(body), action, note, when, revision)
               for key, body, action, note, when, revision in cursor]
    if misses and len(records) < PAGE_SIZE:
        left = values(snapshot, 0, left_key)
        for right, score in snapshot.execute("""
            SELECT right_id,score FROM missed WHERE left_id=? ORDER BY right_id LIMIT ? OFFSET ?
        """, (left_key, PAGE_SIZE - len(records), max(0, offset - retained))):
            item = scored_miss(rules, left, values(snapshot, 1, right), score)
            item["right_id"] = json.loads(right)
            records.append((None, item, None, None, None, None))
    tables = []
    candidates = iter(records)
    while group := list(islice(candidates, 3)):
        tables.append(comparison_table(group, rules, token))
    links = []
    if page > 1:
        links.append(f'<a href="/compare?n={n}&amp;page={page - 1}">Назад</a>')
    if offset + PAGE_SIZE < count:
        links.append(f'<a href="/compare?n={n}&amp;page={page + 1}">Далее</a>')
    footer = (f'<footer>Показано {offset + 1}–{offset + len(records)} из {count}'
              f'<div>{" &nbsp; ".join(links)}</div></footer>') if links else ""
    if status == "unmatched":
        back = f'<a href="/diagnose?n={n}">← К разбору записи без пары</a>'
    elif status == "accepted":
        back = '<a href="/?status=accepted">← Совпадения</a>'
    else:
        back = '<a href="/?status=review">← Возможные совпадения</a>'
    notice = "" if complete else "<p>Доступны только сохранённые пары, достигшие нижнего порога кандидата.</p>"
    return layout(f"""{back}<h1>Сравнение кандидатов</h1>
        <p><code>{html(rules.left_id)} = {identifier(json.loads(left_key))}</code> · Вариантов: {count}</p>
        {notice}{''.join(tables)}{footer}""")
