"""Обзор и история локальных результатов сопоставления."""
from datetime import date, timedelta
from urllib.parse import quote, urlencode

from .view_html import html, layout


def number(value):
    return f"{value:,}".replace(",", " ")


def run_link(run):
    return f'/runs/{quote(run["key"], safe="")}/'


def page_heading(title, subtitle, action):
    return f"""<div class="page-heading"><div><h1>{title}</h1><p>{subtitle}</p></div>
        <a class="primary-action" href="/new">{action}</a></div>"""


def results_chart(metrics):
    """Четыре столбца с общей нулевой отметкой и линейным масштабом."""
    maximum = max(value for _, _, value, _ in metrics) or 1
    columns = []
    for index, (key, label, value, _) in enumerate(metrics):
        x = 62 + index * 125
        bar_height = round(166 * value / maximum, 2)
        top = 196 - bar_height
        columns.append(f"""<g class="chart-{key}">
            <rect x="{x-42}" y="{top}" width="84" height="{bar_height}" rx="3"/>
            <text class="chart-number" x="{x}" y="{top-8}">{number(value)}</text>
            <text x="{x}" y="216">{label}</text></g>""")
    return f"""<svg class="results-chart" role="img" aria-labelledby="overview-chart-title"
        viewBox="0 0 500 230" xmlns="http://www.w3.org/2000/svg">
        <title id="overview-chart-title">Распределение строк результата. {'. '.join(f'{label} — {value}' for _, label, value, _ in metrics)}.</title>
        <path class="chart-grid" d="M0 30H500 M0 113H500 M0 196H500"/>
        {''.join(columns)}</svg>"""


def overview_page(runs, current, counts, pending):
    selected = next((run for run in runs if run["key"] == current), None)
    title = page_heading("Обзор", "Источники и результаты выбранного запуска.", "Новое сопоставление")
    if selected is None:
        return layout(title + '<section class="dash-empty">Выберите запуск в <a href="/history">истории запусков</a>.</section>',
                      active="overview")
    link = run_link(selected)
    metrics = [
        ("total", "Всего строк", sum(counts.values()), ""),
        ("accepted", "Совпадения", counts["accepted"], "?status=accepted"),
        ("review", "Возможные совпадения", counts["review"], "?status=review"),
        ("unmatched", "Без пары", counts["unmatched"], "?status=unmatched"),
    ]
    cards = "".join(f'<a class="card {key}" href="{link}{query}"><b>{number(value)}</b>{label}</a>'
                    for key, label, value, query in metrics)
    labels = selected.get("source_labels") or []
    if len(labels) == 2:
        source_rows = "".join(f'<div class="dash-source"><div><small>{side}</small><strong>{html(name)}</strong></div></div>'
                              for side, name in zip(("Левый источник", "Правый источник"), labels))
    else:
        source_rows = f'<div class="dash-source">{html(selected["sources"])}</div>'
    latest = selected["key"] == max(runs, key=lambda run: (run["time"], run["name"], run["key"]))["key"]
    run_heading = "Последний запуск завершён" if latest else "Выбранный запуск завершён"
    body = f"""{title}<div class="dashboard">
        <section class="dash-run"><div><small>{html(selected['name'])}</small><h2>{run_heading}</h2>
        <small>{html(selected['sources'])} · Файл от {html(selected['time'])}</small></div>
        <a class="dash-secondary" href="{link}">Открыть результаты</a></section>
        <div class="cards dash-counts">{cards}</div>
        <div class="dash-columns"><section class="dash-figure"><div class="dash-panel-head"><h2>Результаты запуска</h2>
        <small>Совпадения, кандидаты и записи без пары</small></div>
        {results_chart(metrics)}</section>
        <div class="dash-side"><section><div class="dash-panel-head"><h2>Источники запуска</h2>
        <a href="/sources">Все источники</a></div>{source_rows}</section>
        <section class="dash-queue"><div><strong>Порог автосовпадения</strong>
        <small>Возможных совпадений без решения · {number(pending)}</small></div>
        <a class="primary-action" href="{link}threshold">Посмотреть границу</a></section></div></div></div>"""
    return layout(body, active="overview")


RESULT_FILTERS = {"": "Все статусы", "current": "Открыт", "saved": "Сохранён"}
PERIODS = {"": "За всё время", "7": "Последние 7 дней", "30": "Последние 30 дней", "90": "Последние 90 дней"}


def select_filter(name, labels, value, accessible_name):
    options = "".join(f'<option value="{key}"{" selected" if key == value else ""}>{label}</option>'
                      for key, label in labels.items())
    return f'<select aria-label="{accessible_name}" name="{name}">{options}</select>'


def history_page(runs, current, params):
    query = params.get("q", [""])[0].strip()
    state = params.get("result", [""])[0]
    period = params.get("period", [""])[0]
    if state not in RESULT_FILTERS or period not in PERIODS:
        raise ValueError("Неизвестный фильтр истории")
    start = (date.today() - timedelta(days=int(period) - 1)).isoformat() if period else ""
    found = []
    for run in runs:
        if start and run["time"][:10] < start:
            continue
        if state == "current" and run["key"] != current:
            continue
        if state == "saved" and run["key"] == current:
            continue
        if query.casefold() not in f'{run["name"]} {run["sources"]}'.casefold():
            continue
        found.append(run)
    found.sort(key=lambda run: (run["time"], run["key"]), reverse=True)
    page_size = 20
    pages = max(1, (len(found) + page_size - 1) // page_size)
    page = min(pages, max(1, int(params.get("page", ["1"])[0])))
    offset = (page - 1) * page_size
    rows = []
    for run in found[offset:offset+page_size]:
        counts = run["counts"]
        totals = number(sum(counts.values())) if counts is not None else "—"
        categories = " / ".join(number(counts[k]) for k in ("accepted", "review", "unmatched")) if counts is not None else "—"
        status = ('<span class="badge accepted">Открыт</span>' if run["key"] == current
                  else '<span class="badge unmatched">Сохранён</span>')
        rows.append(f"""<tr><th scope="row">{html(run['name'])}</th><td>{html(run['sources'])}</td>
            <td><time>{html(run['time'])}</time></td><td>{totals}</td><td>{categories}</td>
            <td>{status}</td><td><a href="{run_link(run)}">Результаты</a></td></tr>""")
    if not rows:
        rows.append('<tr><td colspan="7">Запусков по этим условиям не найдено.</td></tr>')
    links = []
    for num, label in ((page-1, "Назад"), (page+1, "Далее")):
        if 1 <= num <= pages:
            qs = urlencode(dict(q=query, result=state, period=period, page=num))
            links.append(f'<a href="/history?{html(qs)}">{label}</a>')
    reset = '<a href="/history">Сбросить</a>' if query or period or state else ''
    body = page_heading("История запусков", "Сохранённые результаты сопоставления в рабочем пространстве.", "Новый запуск")
    body += f"""<section class="dash-history"><form class="history-filters" action="/history" method="get">
        <input type="search" name="q" aria-label="Название запуска или таблица" placeholder="Название запуска или таблица" value="{html(query)}">
        {select_filter("result", RESULT_FILTERS, state, "Статус запуска")}
        {select_filter("period", PERIODS, period, "Период")}
        <button class="dash-secondary" type="submit">Найти</button>{reset}</form>
        <div class="scroll" role="region" aria-label="История запусков" tabindex="0"><table><thead><tr>
        <th>Запуск</th><th>Источники и таблицы</th><th>Дата файла</th><th>Строк результата</th>
        <th>Совпадения / Возможные совпадения / Без пары</th><th>Статус</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        <footer>Показано {min(offset + 1, len(found))}–{min(offset + page_size, len(found))} из {len(found)}
        <div>{' &nbsp; '.join(links)}</div></footer></section>"""
    return layout(body, active="history")
