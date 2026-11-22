"""Выбор пронумерованных предложений из показанного отчёта."""
from .selection import compile_selection


def choose_fields(draft, answer):
    fields = draft["fields"]
    selected = set()
    for entry in answer.split(","):
        try:
            index = int(entry.strip())
        except ValueError:
            raise ValueError("Введите номера предложений через запятую, например 1, 3") from None
        if not 1 <= index <= len(fields):
            raise ValueError(f"Номер {index} вне списка из {len(fields)} предложений")
        selected.add(index)

    reviewed = {**draft, "fields": [
        {**field, "enabled": index in selected} for index, field in enumerate(fields, start=1)
    ]}
    compile_selection(reviewed)
    return reviewed
