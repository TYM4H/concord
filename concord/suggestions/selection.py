"""Преобразование предложений в редактируемый выбор и настройки запуска."""
from copy import deepcopy

from ..rules import Rules


def make_draft(report):
    """Создать черновик для аналитика, сохранив маршрут каждого варианта."""
    fields = []
    for pair in report["suggestions"]:
        row = {"enabled": False, "mode": pair["mode"], "weight": 1, "block_on_conflict": True}
        for side in ("left", "right"):
            preview = pair[side]
            row[side] = preview["path"]
            row[side + "_route"] = None
            if preview["via"]:
                row[side + "_route"] = {"column": preview["column"], "via": deepcopy(preview["via"])}
        fields.append(row)
    return {"version": 1, "left_id": report["left"]["id"], "right_id": report["right"]["id"],
            "fields": fields}


def _name(value):
    return isinstance(value, str) and bool(value.strip())


def _check_route(route, context):
    if not isinstance(route, dict) or set(route) != {"column", "via"} or not _name(route["column"]):
        raise ValueError(f"{context}: нужны имя колонки column и список переходов via")
    if not isinstance(route["via"], list) or not route["via"]:
        raise ValueError(f"{context}: via должен содержать хотя бы один переход")
    for number, hop in enumerate(route["via"], 1):
        position = f"{context}, переход {number}"
        if not isinstance(hop, dict) or set(hop) != {"table", "from", "to"} or not _name(hop["table"]):
            raise ValueError(f"{position}: нужны имя таблицы table и колонки from и to")
        for key in ("from", "to"):
            cols = hop[key]
            if not isinstance(cols, list) or not cols or not all(_name(c) for c in cols):
                raise ValueError(f"{position}: {key} должен содержать непустой список имён колонок")
        if len(hop["from"]) != len(hop["to"]):
            raise ValueError(f"{position}: число колонок в from и to должно совпадать")


def compile_selection(draft):
    """Вернуть правила и маршруты двух сторон для явно подтверждённых полей."""
    if not isinstance(draft, dict) or type(draft.get("version")) is not int or draft["version"] != 1:
        raise ValueError("Ожидается черновик выбора полей с version = 1")
    if set(draft) != {"version", "left_id", "right_id", "fields"}:
        raise ValueError("Черновик выбора должен содержать version, left_id, right_id и fields")
    if not isinstance(draft["fields"], list):
        raise ValueError("fields должен быть списком пар полей")

    selected = []
    routes = {"left": {}, "right": {}}
    field_keys = ("left", "right", "mode", "weight")
    for number, row in enumerate(draft["fields"], 1):
        if not isinstance(row, dict) or type(row.get("enabled")) is not bool:
            raise ValueError(f"Пара {number}: укажите enabled равным true или false")
        if not row["enabled"]:
            continue
        if set(row) - {"block_on_conflict"} != {*field_keys, "enabled", "left_route", "right_route"}:
            raise ValueError(f"Пара {number}: ожидаются left, right, mode, weight, enabled, left_route и right_route")
        selected.append({**{key: row[key] for key in field_keys},
                         "block_on_conflict": row.get("block_on_conflict", True)})
        for side in routes:
            route = row[side + "_route"]
            if route is not None:
                if not _name(row[side]):
                    raise ValueError(f"Пара {number}: {side} должен содержать путь к полю")
                _check_route(route, f"Пара {number}, {side}_route")
                routes[side][row[side]] = deepcopy(route)

    rules = Rules.parse({"left_id": draft["left_id"], "right_id": draft["right_id"], "fields": selected})
    return rules, routes["left"], routes["right"]
