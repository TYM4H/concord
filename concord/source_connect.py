"""Проверка подключений из браузера и создание локальных профилей."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from uuid import UUID, uuid4

from .sources.adapters import ADAPTERS, connect
from .sources.profile import Profile
from .sources.reader import catalog
from .view_html import html, layout
from .wizard_state import carried_inputs


SIDES = {"left": "Левый источник", "right": "Правый источник"}
DRIVERS = {
    "postgresql": ("PostgreSQL", "PG"),
    "mysql": ("MySQL", "MY"),
    "sqlite": ("SQLite", "SQ"),
    "jsonl": ("JSONL", "JL"),
}


def live_tables(tables, focus):
    blocks = []
    for index, table in enumerate(tables):
        name = table["table"]
        rows = []
        for col in table["columns"]:
            key = col["name"]
            constraints = []
            if key in table["primary_key"]:
                constraints.append("PRIMARY KEY")
            for fk in table["foreign_keys"]:
                for column, target in zip(fk["columns"], fk["target_columns"]):
                    if key == column:
                        target_name = ".".join(str(v) for v in (fk["target_schema"], fk["target_table"], target) if v)
                        constraints.append(f'FK → {html(target_name)}')
            if not col["nullable"]:
                constraints.append("NOT NULL")
            rows.append(f"<tr><td><code>{html(key)}</code></td><td>{html(col['type'])}</td>"
                        f"<td>{'<br>'.join(constraints) or '—'}</td></tr>")
        expanded = name == focus if focus else index == 0
        blocks.append(f"""<details class="src-table" {'open' if expanded else ''}><summary>{html(name)}
            <span>Полей: {len(rows)}</span></summary><table><thead><tr><th>Колонка</th><th>Тип данных</th>
            <th>Ограничения</th></tr></thead><tbody>{''.join(rows)}</tbody></table></details>""")
    return "".join(blocks) or '<p class="src-empty">В этой схеме нет доступных таблиц.</p>'


def val(form, key, default=""):
    values = form.get(key, [default])
    return values[0] if len(values) == 1 else default


def options(values, current):
    return "".join(f'<option value="{html(k)}" {"selected" if k == current else ""}>{html(v)}</option>'
                   for k, v in values)


def connection_form(form, token, schemas=(), tables=(), *, workspace=False):
    mode = val(form, "mode", "profile" if "profile" in form else "direct")
    side = val(form, "side", "left")
    draft = carried_inputs(form)
    csrf = f'<input type="hidden" name="token" value="{html(token)}">'
    links = []
    for key, label in (("direct", "Параметры подключения"), ("profile", "Готовый профиль")):
        current = ' class="selected" aria-current="page"' if key == mode else ""
        if draft:
            links.append(f'<form method="post" action="/sources/connect">{csrf}{draft}'
                         f'<input type="hidden" name="side" value="{html(side)}">'
                         f'<input type="hidden" name="mode" value="{key}">'
                         f'<button name="intent" value="mode"{current}>{label}</button></form>')
        else:
            links.append(f'<a href="/sources/connect?{html(urlencode(dict(side=side, mode=key)))}"{current}>{label}</a>')
    if mode == "profile":
        fields = f"""<label>Путь к профилю подключения (.json)<input name="profile" required
            value="{html(val(form, 'profile'))}" placeholder="/путь/к/профилю.json"></label>"""
    else:
        driver = val(form, "driver", "sqlite")
        drivers = ((k, DRIVERS.get(k, (k, ""))[0]) for k in ADAPTERS)
        fields = f"""<label>Тип базы<select name="driver">{options(drivers, driver)}</select></label>
            <label>База данных<input name="database" required value="{html(val(form, 'database'))}">
            <small class="sqlite-hint">Для SQLite укажите полный путь к файлу.</small></label>
            <fieldset class="src-server"><legend>Адрес сервера и пользователь</legend>
            <div><label>Хост или Unix-сокет<input name="host" value="{html(val(form, 'host'))}"></label>
            <label>Порт<input type="number" min="1" max="65535" name="port" value="{html(val(form, 'port'))}"></label>
            <label>Пользователь<input name="user" value="{html(val(form, 'user'))}"></label></div></fieldset>"""
    schema = html(val(form, "schema"))
    choices = "".join(f'<option value="{html(name)}"></option>' for name in schemas)
    if tables:
        items = [(t["table"], t["table"]) for t in tables]
        table_input = f'<select name="table">{options(items, val(form, "table"))}</select>'
    else:
        table_input = f'<input name="table" value="{html(val(form, "table"))}" placeholder="Выбрать после проверки">'
    save = ""
    if workspace and tables:
        save = f"""<input type="hidden" name="ticket" value="{uuid4().hex}">
            <button class="primary-action" type="submit" name="intent" value="save">Сохранить профиль для сопоставления</button>"""
    side_control = (f'<strong>{html(SIDES.get(side, ""))}</strong><input type="hidden" name="side" value="{html(side)}">'
                    if draft else f'<label>Сторона сопоставления<select name="side">{options(SIDES.items(), side)}</select></label>')
    return f"""<div class="tabs">{''.join(links)}</div><form class="src-form" method="post" action="/sources/connect">
        {csrf}{draft}<input type="hidden" name="mode" value="{html(mode)}">{side_control}
        {fields}<label>Схема<input name="schema" list="src-schemas" value="{schema}" placeholder="По умолчанию"></label>
        <datalist id="src-schemas">{choices}</datalist><label>Таблица для сопоставления{table_input}</label>
        <label>Пароль базы данных<input type="password" name="password" autocomplete="off"></label>
        <small>Пароль действует на один запрос. Для SQLite оставьте поле пустым.</small>
        <button class="src-check" type="submit" name="intent" value="inspect">Проверить подключение</button>{save}</form>"""


def page(form, token, result="", schemas=(), tables=(), *, workspace=False):
    fields = connection_form(form, token, schemas, tables, workspace=workspace)
    draft = carried_inputs(form)
    back = (f'<form action="/new/return" method="post"><input type="hidden" name="token" value="{html(token)}">'
            f'{draft}<button class="src-check">← Вернуться в мастер</button></form>'
            if draft else '<a href="/sources">← Источники данных</a>')
    return layout(f"""{back}<div class="page-heading"><div>
        <h1>Подключить источник</h1><p>Проверка доступа и просмотр структуры базы.</p></div></div>
        <div class="src-grid"><section class="src-config">{fields}</section>{result}</div>""", active="sources")


def connect_page(params, token):
    return page(params, token)


def read_profile(form):
    mode = val(form, "mode", "profile" if "profile" in form else "direct")
    if mode == "profile":
        path = Path(val(form, "profile").strip()).expanduser()
        if not path.is_file():
            raise ValueError("По указанному пути нет файла профиля подключения")
        path = path.resolve()
        return Profile.load(path), path
    if mode != "direct":
        raise ValueError("Выберите способ подключения")
    data = {k: val(form, k).strip() for k in ("driver", "database", "host", "user", "port") if val(form, k).strip()}
    if data.get("driver") == "sqlite":
        path = Path(data.get("database", "")).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Укажите полный путь к существующей базе SQLite")
        data = dict(driver="sqlite", database=str(path))
    elif "port" in data:
        if not data["port"].isascii() or not data["port"].isdecimal():
            raise ValueError("Некорректный порт базы данных")
        data["port"] = int(data["port"])
    return Profile.parse(data, Path.cwd()), None


def persist_profile(profile, workspace_folder, ticket):
    if workspace_folder is None:
        raise ValueError("Чтобы сохранить профиль, откройте рабочее пространство")
    try:
        if UUID(ticket).hex != ticket:
            raise ValueError
    except ValueError:
        raise ValueError("Повторите проверку подключения перед сохранением профиля") from None
    folder = Path(workspace_folder) / ".concord-profiles"
    if folder.is_symlink():
        raise ValueError("Для сохранения профилей нужен локальный каталог")
    folder.mkdir(mode=0o700, exist_ok=True)
    dest = folder / f"{ticket}.json"
    data = {key: v for key, v in asdict(profile).items() if v is not None}
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # Один и тот же submit можно безопасно повторить после сетевого сбоя.
    with NamedTemporaryFile(mode="w", dir=folder, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp.flush()
        try:
            os.link(tmp.name, dest)
        except FileExistsError:
            if dest.is_symlink() or not dest.is_file() or dest.read_text(encoding="utf-8") != content:
                raise ValueError("Параметры изменились. Проверьте подключение перед сохранением") from None
    return dest


def inspect_page(form, token, workspace_folder=None):
    schemas = []
    tables = []
    form = dict(form)
    if val(form, "intent") == "mode":
        return connect_page(form, token)
    try:
        side = val(form, "side", "left")
        intent = val(form, "intent", "inspect")
        if side not in SIDES or intent not in ("inspect", "save"):
            raise ValueError("Проверьте параметры подключения")
        profile, path = read_profile(form)
        password = val(form, "password") or os.environ.get(f"CONCORD_{side.upper()}_PASSWORD")
        with connect(profile, password) as conn:
            schemas = catalog(conn, None)["schemas"]
            schema = val(form, "schema").strip() or profile.schema or conn.dialect.default_schema_name
            tables = catalog(conn, schema)["tables"] if schema else []
        form["schema"] = [schema or ""]
        selected = val(form, "table") or (profile.table if schema == profile.schema else None)
        names = [t["table"] for t in tables]
        if selected and selected not in names or (intent == "save" and not selected):
            raise ValueError("Выберите таблицу из проверенной схемы")
        if not selected:
            selected = names[0] if names else ""
        form["table"] = [selected]
        saved = False
        if intent == "save":
            same = (schema, selected) == (profile.schema, profile.table)
            profile = replace(profile, schema=schema, table=selected, routes=profile.routes if same else {})
            path = persist_profile(profile, workspace_folder, val(form, "ticket"))
            saved = True
    except (ValueError, OSError, RuntimeError) as exc:
        result = f'<div class="src-error" role="alert"><strong>Не удалось завершить действие</strong><p>{html(exc)}</p></div>'
    else:
        action = ""
        if path and selected and profile.table == selected and profile.schema == schema:
            table = next(t for t in tables if t["table"] == selected)
            prefix = f"{schema}.{selected}."
            ids = [(prefix + col["name"], col["name"]) for col in table["columns"]]
            primary = table["primary_key"]
            default = prefix + primary[0] if len(primary) == 1 else ""
            draft = carried_inputs(form)
            route = "/new/return" if draft else "/new"
            method = "post" if draft else "get"
            carry = f'<input type="hidden" name="token" value="{html(token)}">{draft}' if draft else ""
            action = f"""<form class="src-form" action="{route}" method="{method}">{carry}
                <input type="hidden" name="{side}_profile" value="{html(path)}">
                <label>Идентификатор записи<select name="{side}_id" required>
                <option value="">Выберите поле ID</option>{options(ids, default)}</select></label>
                <button class="primary-action" type="submit">Использовать в сопоставлении</button></form>"""
        status = "Профиль сохранён" if saved else "Подключение проверено"
        kind = DRIVERS.get(profile.driver, (profile.driver, ""))[0]
        name = Path(profile.database).name if profile.driver == "sqlite" else profile.database
        result = f"""<section class="src-inspect"><div class="src-title"><div><small>{status}</small>
            <h2>{html(name)}</h2></div></div><dl class="src-facts"><div><dt>Тип</dt><dd>{html(kind)}</dd></div>
            <div><dt>Схема</dt><dd>{html(schema or '—')}</dd></div><div><dt>Доступ</dt><dd>Только чтение</dd></div></dl>
            <div class="src-content src-live">{live_tables(tables, selected)}{action}</div></section>"""
    return page(form, token, result, schemas, tables, workspace=workspace_folder is not None)
