"""Общие компоненты локального интерфейса Concord."""
from html import escape
import json


LABELS = {"accepted": "Совпадение", "review": "Возможное совпадение", "unmatched": "Без пары"}
REASONS = {
    "accept_threshold": "Достигнут порог автоматического совпадения.",
    "review_threshold": "Балл достиг нижнего порога кандидата и остаётся ниже порога автосовпадения.",
    "conflict_blocks_acceptance": "Балл достиг порога автосовпадения. Обнаружено расхождение по полю, для которого включена блокировка.",
    "below_review_threshold": "Балл не достиг нижнего порога кандидата.",
    "no_block_candidate": "Совместных значений, прошедших отбор по частоте, не найдено.",
    "no_retained_pair": "Для этой записи справа ни одна пара не попала в результат.",
}


def html(value):
    return escape(str(value))


def badge(status):
    return f'<span class="badge {status}">● &nbsp;{LABELS[status]}</span>'


def outcome_label(field):
    outcome = field.get("outcome")
    if outcome == "different":
        if field.get("block_on_conflict", True):
            return "Расхождение · блокирует автосовпадение"
        return "Расхождение · допускается правилами"
    return {"equal": "Совпало", "missing": "Пропуск"}.get(outcome, "Недостаточно данных")


SECTIONS = {
    "overview": ("/overview", "Обзор"), "matching": ("/", "Сопоставление"),
    "sources": ("/sources", "Источники данных"), "rules": ("/rules", "Правила"),
    "history": ("/history", "История запусков"),
}


def layout(content, active="matching"):
    links = []
    for key, (path, label) in SECTIONS.items():
        marker = ' aria-current="page"' if key == active else ""
        links.append(f'<a href="{path}"{marker}>{label}</a>')
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{SECTIONS[active][1]} · Concord</title><link rel="stylesheet" href="/style.css">
    <aside><div class="brand"><b>C</b><div><strong>Concord</strong>
    <small>Сопоставление записей</small></div></div><div class="section">Работа</div>
    <nav aria-label="Разделы Concord">{"".join(links)}</nav>
    <div class="workspace-mark"><b>C</b><div>Concord<small>Локальное приложение</small></div></div></aside>
    <main><header><i></i>Контур «Основной»<details class="desk-help"><summary>Справка</summary><div><b>Как работать с Concord</b><p>Откройте «Источники данных», проверьте поля в «Правилах» и запустите новое сопоставление. К результатам и решениям можно вернуться через историю.</p></div></details></header><article>{content}</article></main></html>"""


def identifier(value):
    return "—" if value is None else html(json.dumps(value, ensure_ascii=False))


def variants(values):
    entries = []
    for norm, detail in values.items():
        raw = ", ".join(html(v) for v in detail["originals"])
        nl, nr = detail["counts"]
        entries.append(f'<div>{raw} → <code>{html(norm)}</code>'
                       f'<small>Записей слева / справа: {int(nl)} / {int(nr)}</small></div>')
    return "".join(entries) or "—"
