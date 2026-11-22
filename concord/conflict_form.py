"""Представление правила расхождения поля в HTML-формах."""
from .view_html import html


def conflict_value(block_on_conflict):
    return "block" if block_on_conflict else "score_only"


def parse_conflict(value):
    if value not in ("block", "score_only"):
        raise ValueError("Выберите действие при расхождении поля")
    return value == "block"


def conflict_select(name, value, label):
    options = (("block", "Блокировать совпадение"), ("score_only", "Только 0 баллов"))
    choices = "".join(f'<option value="{key}"{" selected" if key == value else ""}>{title}</option>'
                      for key, title in options)
    return f'<select name="{html(name)}" aria-label="{html(label)}">{choices}</select>'
