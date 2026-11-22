"""Подбор полей двух профилей и подтверждение маршрутов в мастере запуска."""
import json
import os
from pathlib import Path

from .conflict_form import conflict_select, conflict_value, parse_conflict
from .sources.adapters import connect
from .sources.profile import Profile
from .suggestions.ranking import suggest_fields
from .suggestions.sample import sample_fields
from .suggestions.selection import compile_selection, make_draft
from .view_html import html, layout


def _single(form, name):
    values = form.get(name, [])
    if len(values) != 1:
        raise ValueError("Проверьте параметры выбора полей")
    return values[0]


def selected_suggestion_form(form):
    """Подготовить общую форму запуска и точные маршруты выбранных полей."""
    header = json.loads(_single(form, "suggestion_draft"))
    if not isinstance(header, dict) or set(header) != {"version", "left_id", "right_id"}:
        raise ValueError("Некорректный выбор полей")
    if any(_single(form, side + "_kind") != "profile" for side in ("left", "right")):
        raise ValueError("Для подбора полей нужны два профиля баз данных")
    fields = []
    for entry in form.get("choice", []):
        option = json.loads(entry)
        if not isinstance(option, list) or len(option) != 2:
            raise ValueError("Некорректное предложение полей")
        index, field = option
        if type(index) is not int or index < 1 or not isinstance(field, dict):
            raise ValueError("Некорректное предложение полей")
        try:
            weight = float(_single(form, f"weight_{index}"))
        except ValueError:
            raise ValueError("У выбранной пары должен быть числовой вес") from None
        key = f"block_on_conflict_{index}"
        if key in form:
            field["block_on_conflict"] = parse_conflict(_single(form, key))
        fields.append({**field, "enabled": True, "weight": weight, "mode": _single(form, f"mode_{index}")})
    rules, left_routes, right_routes = compile_selection({**header, "fields": fields})
    normalized = {**form, "left_id": [rules.left_id], "right_id": [rules.right_id]}
    for key in ("left", "right", "mode", "weight"):
        normalized["field_" + key] = [str(getattr(field, key)) for field in rules.fields]
    normalized["field_block_on_conflict"] = [conflict_value(f.block_on_conflict) for f in rules.fields]
    return normalized, {"left": left_routes, "right": right_routes}


def _preview(field):
    examples = "<br>".join(html(value) for value in field["examples"])
    route = "".join(f'<small>{html(", ".join(hop["from"]))} → {html(hop["table"])}'
                    f' ({html(", ".join(hop["to"]))})</small>' for hop in field["via"])
    return (f'<code>{html(field["path"])}</code><small>{html(field["type"])}</small>{route}'
            f'<div>{examples}</div><small>Заполнено ID: {field["populated_entities"]}'
            f' из {field["sampled_entities"]}</small>')


def suggest_page(run, form, token):
    """Считать выборки как в CLI и показать варианты без выполнения сопоставления."""
    samples = []
    for side in ("left", "right"):
        if _single(form, side + "_kind") != "profile":
            raise ValueError("Для автоматического подбора с обеих сторон выберите профиль базы данных")
        path = Path(_single(form, side + "_path").strip()).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Укажите полный путь к существующему файлу профиля")
        profile = Profile.load(path)
        passwords = form.get(side + "_password", [""])
        if len(passwords) != 1:
            raise ValueError("Проверьте параметры авторизации источника")
        with connect(profile, passwords[0] or os.environ.get("CONCORD_" + side.upper() + "_PASSWORD")) as conn:
            samples.append(sample_fields(conn, profile, _single(form, side + "_id").strip()))
    report = suggest_fields(*samples)
    draft = make_draft(report)
    summary = []
    for side, label in (("left", "Слева"), ("right", "Справа")):
        sample = report[side]
        warnings = "".join(f"<small>{html(message)}</small>" for message in sample["warnings"])
        summary.append(f'<div><small>{label}</small><code>{html(sample["id"])}</code>'
                       f'<small>В выборке {sample["sampled_entities"]} ID, обследовано полей: '
                       f'{sample["examined_fields"]}.</small>{warnings}</div>')
    heading = ('<a href="/new">← Новое сопоставление</a><h1>Подбор полей</h1>'
               '<div class="sources">' + "".join(summary) + '</div>')
    if not draft["fields"]:
        from .new_screen import new_page
        message = "Подходящих пар полей в выборках не найдено. Можно выбрать признаки вручную."
        warnings = [message for side in ("left", "right") for message in report[side]["warnings"]]
        return new_page(run, run.rules, token, " ".join([message, *warnings]), form)

    rows = []
    policies = {(left.strip(), right.strip()): policy for left, right, policy in
                zip(form.get("field_left", []), form.get("field_right", []), form.get("field_block_on_conflict", []))}
    for index, (field, suggestion) in enumerate(zip(draft["fields"], report["suggestions"]), 1):
        policy = policies.get((field["left"], field["right"]), "block")
        field["block_on_conflict"] = parse_conflict(policy)
        control = conflict_select(f"block_on_conflict_{index}", policy, f"При расхождении {field['left']} и {field['right']}")
        # Браузер отправит контекст только тех маршрутов, которые выбрал аналитик.
        choice = html(json.dumps([index, field], ensure_ascii=False, separators=(",", ":")))
        options = "".join(f'<option value="{mode}"{" selected" if mode == field["mode"] else ""}>{label}</option>'
                          for mode, label in (("text", "Нормализованный текст"), ("exact", "Точное совпадение")))
        reasons = " ".join(html(reason) for reason in suggestion["reasons"])
        rows.append(f"""<tr><td><input type="checkbox" name="choice" value="{choice}"
            aria-label="Сравнивать {html(field['left'])} с {html(field['right'])}"></td>
            <td>{_preview(suggestion['left'])}</td><td>{_preview(suggestion['right'])}</td>
            <td><label>Режим<select name="mode_{index}">{options}</select></label>
            <label>Вес<input type="number" name="weight_{index}" value="1" min="0" max="100" step="any"></label>
            <label>При расхождении{control}</label>
            <details><summary>Основания подбора</summary><small>{reasons}</small></details></td></tr>""")
    saved = {k: _single(form, k) for k in ("left_kind", "right_kind", "left_path", "right_path",
                                            "review", "accept")}
    saved.update(left_id=draft["left_id"], right_id=draft["right_id"],
                 token=token, suggestion_draft=json.dumps({k: v for k, v in draft.items() if k != "fields"},
                                                         ensure_ascii=False))
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html(v)}">' for k, v in saved.items())
    passwords = "".join(f'<label>Пароль · {label}<input type="password" name="{side}_password"'
                        f' autocomplete="off"></label>' for side, label in (("left", "слева"), ("right", "справа"))
                        if form.get(side + "_password", [""])[0])
    if passwords:
        passwords = ('<section class="form-group"><h2>Доступ к источникам</h2>'
                     '<p>Введите пароль повторно для чтения при запуске.</p>' + passwords + '</section>')
    return layout(heading + f"""<form class="new-form suggest-form" method="post" action="/new">{hidden}
        <section class="form-group"><h2>Предложенные пары полей</h2>
        <p>Отметьте признаки для расчёта. Каждое поле можно выбрать один раз с каждой стороны.</p>
        <div class="scroll"><table><thead><tr><th>Выбор</th><th>Слева · примеры</th>
        <th>Справа · примеры</th><th>Сравнение</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
        {passwords}<button class="primary-action" type="submit">Запустить с выбранными полями</button></form>""")
