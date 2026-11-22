"""Создание отдельного запуска из снимка, файлов или read-only профилей."""
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
from uuid import uuid4

from .__main__ import rows
from .conflict_form import conflict_select, conflict_value, parse_conflict
from .rules import Rules
from .runs import export_run
from .view_html import html, layout


def new_page(run, rules, token, error="", form=None, profiles=(), *, add_row=False):
    form = form or {}

    def val(name, default=""):
        return html(form.get(name, [default])[0])

    inputs = []
    for side, label in (("left", "Левый источник"), ("right", "Правый источник")):
        kinds = ([("snapshot", "Снимок этого запуска")] if run.snapshot is not None else [])
        kinds += [("jsonl", "Файл записей JSONL"), ("profile", "Профиль базы данных")]
        selected = form.get(side + "_kind", [kinds[0][0]])[0]
        options = "".join(f'<option value="{k}" {"selected" if selected == k else ""}>{name}</option>'
                          for k, name in kinds)
        choices = "".join(f'<option value="{html(path)}">{html(name)}</option>' for path, name in profiles)
        inputs.append(f"""<section class="form-group"><h2>{label}</h2>
            <label>Источник<select name="{side}_kind">{options}</select></label>
            <label>Путь к файлу или профилю<input name="{side}_path" value="{val(side+'_path')}" list="profiles-{side}" placeholder="/путь/к/файлу">
            <datalist id="profiles-{side}">{choices}</datalist>
            <small>Для сохранённого снимка оставьте пустым.</small></label>
            <label>Поле ID<input required name="{side}_id" value="{val(side+'_id', getattr(rules, side+'_id'))}"></label>
            <button type="submit" formnovalidate formaction="/new/connect" name="connect_side" value="{side}">
            Подключить {'левый' if side == 'left' else 'правый'} источник</button>
            <details><summary>Пароль для профиля базы</summary>
            <input aria-label="Пароль · {label}" type="password" name="{side}_password" autocomplete="off"></details></section>""")
    field_rows = []
    row_count = len(form["field_left"]) if "field_left" in form else len(rules.fields) + 1
    for i in range(max(1, row_count + int(add_row))):
        f = rules.fields[i] if i < len(rules.fields) else None
        def entry(key, default):
            items = form.get("field_" + key, [])
            return items[i] if i < len(items) else default
        mode = entry("mode", f.mode if f else "text")
        policy = entry("block_on_conflict", conflict_value(f.block_on_conflict if f else True))
        control = conflict_select("field_block_on_conflict", policy, f"При расхождении · поле {i + 1}")
        opts = "".join(f'<option value="{m}" {"selected" if m == mode else ""}>{label}</option>'
                       for m, label in (("text", "Нормализованный текст"), ("exact", "Точное совпадение")))
        field_rows.append(f"""<tr><td><input aria-label="Поле слева" name="field_left" value="{html(entry('left', f.left if f else ''))}"></td>
            <td><input aria-label="Поле справа" name="field_right" value="{html(entry('right', f.right if f else ''))}"></td>
            <td><select aria-label="Сравнение" name="field_mode">{opts}</select></td>
            <td><input aria-label="Вес" type="number" min="0" max="100" step="any" name="field_weight" value="{html(entry('weight', f.weight if f else 1))}"></td>
            <td>{control}</td></tr>""")
    message = f'<p role="alert">{html(error)}</p>' if error else ""
    return layout(f"""<a href="/">← Сопоставление</a><h1>Новое сопоставление</h1>
        <p>Выберите источники, идентификаторы и признаки. Результат появится в истории отдельным запуском.</p>
        {message}<form class="new-form" method="post" action="/new"><input type="hidden" name="token" value="{html(token)}">
        <div class="new-sources">{''.join(inputs)}</div><section class="form-group"><h2>Поля и веса</h2>
        <button type="submit" formaction="/new/suggest">Подобрать поля по двум профилям баз</button>
        <p>Для добавления признака заполните пустую строку. Для исключения очистите оба поля.</p>
        <div class="scroll"><table><thead><tr><th>Поле слева</th><th>Поле справа</th><th>Сравнение</th><th>Вес</th><th>При расхождении</th></tr></thead>
        <tbody>{''.join(field_rows)}</tbody></table></div><button type="submit" formnovalidate formaction="/new/fields">Добавить строку</button></section><section class="form-group"><h2>Условия решения</h2>
        <div class="new-limits"><label>Нижний порог кандидата, %<input required name="review" type="number" step="any" value="{val('review', rules.review)}"></label>
        <label>Автосовпадение, %<input required name="accept" type="number" step="any" value="{val('accept', rules.accept)}"></label>
        <label>Предел частоты<input required name="block_limit" type="number" min="1" value="{val('block_limit', rules.block_limit)}"></label></div></section>
        <button class="primary-action" type="submit">Запустить сопоставление</button></form>""")


def snapshot_rows(run, side, rules):
    if run.snapshot is None:
        raise ValueError("Снимок запуска недоступен")
    id_name = getattr(run.rules, side + "_id")
    available = [getattr(f, side) for f in run.rules.fields]
    if getattr(rules, side + "_id") != id_name or any(getattr(f, side) not in available for f in rules.fields):
        raise ValueError("Для снимка выберите сохранённые поля и исходный ID")
    data = run.snapshot.execute("""SELECT e.id,o.field,o.raw FROM entity e LEFT JOIN observation o
        ON e.id=o.id AND e.side=o.side WHERE e.side=? ORDER BY e.id""", (int(side == "right"),))
    row = None
    previous = None
    for key, field, raw in data:
        if key != previous:
            if row is not None:
                yield row
            row = {id_name: json.loads(key)}
            previous = key
        if field is not None:
            row.setdefault(available[field], []).append(raw)
    if row is not None:
        yield row


def create_run(workspace, run, form):
    selected_routes = None
    if "suggestion_draft" in form:
        from .suggest_screen import selected_suggestion_form
        form, selected_routes = selected_suggestion_form(form)

    def get(key):
        vals = form.get(key, [])
        if len(vals) != 1:
            raise ValueError("Проверьте параметры запуска")
        return vals[0].strip()

    columns = [form.get("field_" + k, []) for k in ("left", "right", "mode", "weight")]
    policies = form.get("field_block_on_conflict")
    if policies is None:
        previous = {(f.left, f.right): conflict_value(f.block_on_conflict) for f in run.rules.fields}
        policies = [previous.get((a.strip(), b.strip()), "block") for a, b in zip(*columns[:2])]
    columns.append(policies)
    if len(set(map(len, columns))) != 1:
        raise ValueError("Проверьте выбранные поля")
    fields = [dict(left=a.strip(), right=b.strip(), mode=m, weight=float(w),
                   block_on_conflict=parse_conflict(policy))
              for a, b, m, w, policy in zip(*columns) if a.strip() or b.strip()]
    rules = Rules.parse(dict(left_id=get("left_id"), right_id=get("right_id"), fields=fields,
                             review=float(get("review")), accept=float(get("accept")), block_limit=int(get("block_limit"))))
    info = {}
    inputs = []
    with ExitStack() as stack:
        for side in ("left", "right"):
            kind = get(side + "_kind")
            if kind == "snapshot":
                if run.snapshot is None:
                    raise ValueError("Выберите файл или профиль для источника без снимка")
                stream = snapshot_rows(run, side, rules)
                info[side] = (run.header.get("sources") or {}).get(side, {})
            else:
                path = Path(get(side + "_path")).expanduser()
                if not path.is_absolute() or not path.is_file():
                    raise ValueError("Укажите полный путь к существующему файлу источника или профиля")
                if kind == "jsonl":
                    stream = rows(path)
                    info[side] = dict(format="jsonl", file=path.name)
                elif kind == "profile":
                    from .sources.adapters import connect
                    from .sources.profile import Profile
                    from .sources.reader import projection
                    profile = Profile.load(path)
                    paths = [getattr(f, side) for f in rules.fields]
                    if selected_routes is not None:
                        routes = selected_routes[side]
                        if any(p in profile.routes and profile.routes[p] != routes.get(p) for p in paths):
                            raise ValueError("Выбранный маршрут отличается от маршрута в профиле")
                        profile = replace(profile, routes=routes)
                    password = form.get(side + "_password", [""])[0] or os.environ.get("CONCORD_" + side.upper() + "_PASSWORD")
                    conn = stack.enter_context(connect(profile, password))
                    stream = projection(conn, profile, getattr(rules, side + "_id"), paths)
                    info[side] = dict(driver=profile.driver, database=profile.database, schema=profile.schema,
                                      table=profile.table, routes={p: profile.routes[p] for p in paths if p in profile.routes})
                else:
                    raise ValueError("Неизвестный тип источника")
            inputs.append(stack.enter_context(closing(stream)))
        name = datetime.now().strftime("run-%Y%m%d-%H%M%S-") + uuid4().hex[:10] + ".jsonl"
        destination = workspace.folder / name
        export_run(destination, *inputs, rules, info)
    return destination
