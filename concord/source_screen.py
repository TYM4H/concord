"""Источники и проекция полей сохранённого запуска."""
import json
from collections import defaultdict
from pathlib import PurePath

from .source_connect import DRIVERS, SIDES, connect_page, inspect_page
from .view_html import html, identifier, layout


def source_info(header, side):
    sources = header.get("sources")
    info = sources.get(side) if isinstance(sources, dict) else None
    return info if isinstance(info, dict) else {}


def source_labels(info, side):
    kind = info.get("driver") or info.get("format")
    driver, symbol = DRIVERS.get(str(kind), (str(kind or "Не указан"), "ID"))
    name = info.get("database") or info.get("file")
    if name and kind == "sqlite":
        name = PurePath(str(name)).name
    return name or SIDES[side], driver, symbol


def table_groups(rules, side, info):
    """Показываем только таблицы и связи, сохранённые с расчётом."""
    groups = defaultdict(list)
    root = info.get("table") or "Поля источника"
    schema = info.get("schema")
    prefix = f"{schema}.{root}." if schema else ""
    id_path = getattr(rules, side + "_id")
    groups[root].append((id_path.removeprefix(prefix), "Идентификатор", ""))
    routes = info.get("routes") or {}
    for field in rules.fields:
        path = getattr(field, side)
        spec = routes.get(path) if isinstance(routes, dict) else None
        table = root
        column = path.removeprefix(prefix)
        links = []
        if isinstance(spec, dict) and isinstance(spec.get("via"), list):
            for step in spec["via"]:
                if not isinstance(step, dict):
                    break
                target = step.get("table")
                keys, targets = step.get("from"), step.get("to")
                if not target or not isinstance(keys, list) or not isinstance(targets, list):
                    break
                links.append(f'{table} ({", ".join(map(str, keys))}) → {target} ({", ".join(map(str, targets))})')
                table = target
            column = spec.get("column") or column
        groups[table].append((column, "Признак", " → ".join(links)))
    return groups


def structure_table(rules, side, info):
    blocks = []
    for index, (table, fields) in enumerate(table_groups(rules, side, info).items()):
        rows = []
        for column, role, route in fields:
            path = f'<small class="src-route">{html(route)}</small>' if route else ""
            rows.append(f"<tr><td><code>{html(column)}</code>{path}</td><td>{role}</td></tr>")
        blocks.append(f"""<details class="src-table" {'open' if index == 0 else ''}>
            <summary>{html(table)} <span>Полей: {len(fields)}</span></summary>
            <table><thead><tr><th scope="col">Колонка</th><th scope="col">Роль в запуске</th></tr></thead>
            <tbody>{''.join(rows)}</tbody></table></details>""")
    return "".join(blocks)


def sample_table(snapshot, rules, side):
    if snapshot is None:
        return '<p class="src-empty">Для этого запуска нет снимка исходных значений.</p>'
    number = int(side == "right")
    ids = snapshot.execute("SELECT id FROM entity WHERE side=? ORDER BY id LIMIT 3", (number,)).fetchall()
    if not ids:
        return '<p class="src-empty">В выбранном источнике нет записей.</p>'
    titles = "".join(f"<th scope=\"col\">{identifier(json.loads(key))}</th>" for key, in ids)
    rows = []
    for index, field in enumerate(rules.fields):
        cells = []
        for key, in ids:
            examples = snapshot.execute("""
                SELECT substr(raw, 1, 161) FROM observation WHERE side=? AND id=? AND field=?
                ORDER BY norm, raw LIMIT 4
            """, (number, key, index)).fetchall()
            visible = [html(v[:160]) + ("…" if len(v) > 160 else "") for v, in examples[:3]]
            if len(examples) > 3:
                visible.append("…")
            cells.append("<td>" + ("<br>".join(visible) or "—") + "</td>")
        rows.append(f'<tr><td><code>{html(getattr(field, side))}</code></td>{"".join(cells)}</tr>')
    return f"""<div class="src-preview"><table><caption>Примеры записей по ID · {html(getattr(rules, side + '_id'))}</caption>
        <thead><tr><th scope="col">Поле</th>{titles}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def sources_page(rules, header, snapshot, params):
    side = params.get("side", ["left"])[0]
    view = params.get("view", ["structure"])[0]
    if side not in SIDES or view not in ("structure", "sample"):
        raise ValueError("Выберите источник и представление")
    cards = []
    for key, label in SIDES.items():
        info = source_info(header, key)
        name, _, symbol = source_labels(info, key)
        current = ' aria-current="true"' if key == side else ""
        path = ".".join(str(v) for v in (info.get("schema"), info.get("table")) if v)
        location = path or getattr(rules, key + "_id")
        cards.append(f"""<a class="src-choice" href="/sources?side={key}&amp;view={view}"{current}>
            <span class="src-icon" aria-hidden="true">{html(symbol)}</span>
            <span><strong>{html(name)}</strong><small>{html(location)}</small>
            <small>{label if name != label else 'ID и поля запуска'}</small></span></a>""")
    info = source_info(header, side)
    name, driver, _ = source_labels(info, side)
    check = (f'<a class="src-check" href="/sources/connect?side={side}">Проверить подключение</a>'
             if info.get("driver") else "")
    tabs = []
    for key, label in (("structure", "Структура"), ("sample", "Примеры записей")):
        current = ' aria-current="page" class="selected"' if view == key else ""
        tabs.append(f'<a href="/sources?side={side}&amp;view={key}"{current}>{label}</a>')
    if snapshot is not None:
        count, = snapshot.execute("SELECT count(*) FROM entity WHERE side=?", (int(side == "right"),)).fetchone()
        volume = f"{count:,}".replace(",", " ")
    else:
        volume = "Нет снимка"
    details = structure_table(rules, side, info) if view == "structure" else sample_table(snapshot, rules, side)
    return layout(f"""<div class="page-heading"><div><h1>Источники данных</h1>
        <p>Источники и поля, выбранные для этого запуска.</p></div>
        <a class="primary-action" href="/sources/connect">Подключить источник</a></div>
        <div class="src-grid"><section class="src-list"><h2>Источники · 2</h2>{''.join(cards)}</section>
        <section class="src-inspect"><div class="src-title"><div><small>{SIDES[side]}</small>
        <h2>{html(name)}</h2></div>{check}</div>
        <dl class="src-facts"><div><dt>Тип</dt><dd>{html(driver)}</dd></div>
        <div><dt>Ключ записи</dt><dd><code>{html(getattr(rules, side + '_id'))}</code></dd></div>
        <div><dt>Записей в снимке</dt><dd>{volume}</dd></div></dl>
        <div class="tabs" aria-label="Данные источника">{''.join(tabs)}</div>
        <div class="src-content">{details}</div></section></div>""", active="sources")
