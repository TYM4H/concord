"""Настройки одного запуска и сравнение значений."""
from dataclasses import dataclass
from math import isfinite, sqrt
import unicodedata


@dataclass(frozen=True)
class Field:
    left: str
    right: str
    mode: str
    weight: float
    block_on_conflict: bool = True


@dataclass(frozen=True)
class Rules:
    left_id: str
    right_id: str
    fields: tuple[Field, ...]
    review: float = 65
    accept: float = 85

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Настройки должны быть объектом JSON")
        # Старые профили содержат прежнюю настройку отбора. Новые расчёты её не используют.
        if "block_limit" in data:
            data = data.copy()
            legacy_limit = data.pop("block_limit")
            if type(legacy_limit) is not int or legacy_limit < 1:
                raise ValueError("Некорректный предел частоты в прежних настройках")
        try:
            fields = tuple(Field(**field) for field in data["fields"])
            rules = cls(**{**data, "fields": fields})
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Ошибка в описании настроек: {exc}") from exc
        if not fields:
            raise ValueError("Выберите хотя бы одну пару полей")
        for side in ("left", "right"):
            paths = [getattr(rules, side + "_id"), *(getattr(f, side) for f in fields)]
            if any(not isinstance(p, str) or not p.strip() for p in paths):
                raise ValueError("Путь к полю должен быть непустой строкой")
            if len(set(paths)) != len(paths):
                raise ValueError("ID и признаки должны указывать на разные поля")
        for field in fields:
            if type(field.block_on_conflict) is not bool:
                raise ValueError("Для блокировки при расхождении укажите true или false")
            if field.mode not in ("exact", "text"):
                raise ValueError("Доступные сравнения: exact и text")
            if not number(field.weight) or not 0 < field.weight <= 100:
                raise ValueError("Вес должен быть в диапазоне (0, 100]")
        if not all(number(t) for t in (rules.review, rules.accept)):
            raise ValueError("Пороги должны быть числами")
        if not 0 < rules.review < rules.accept <= 100:
            raise ValueError("Требуется 0 < review < accept <= 100")
        return rules


def number(value):
    return type(value) in (int, float) and -1e308 <= value <= 1e308 and isfinite(value)


def normalize(value, mode):
    if mode == "text":
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return value if value.strip() else ""


def compare(rules, left, right):
    evidence = []
    total = 0.0
    blocking_conflict = False
    for index, field in enumerate(rules.fields):
        a, b = left.get(index, {}), right.get(index, {})
        shared = sorted(a.keys() & b.keys())
        best = None
        points = 0.0
        if shared:
            # Один признак вносит вклад один раз, даже при нескольких совпадениях.
            best = min(shared, key=lambda key: max(a[key]["counts"]))
            points = field.weight / sqrt(max(a[best]["counts"]))
            result = "equal"
        elif a and b:
            blocking_conflict |= field.block_on_conflict
            result = "different"
        else:
            result = "missing"
        total += points
        evidence.append({
            "left_path": field.left, "right_path": field.right, "mode": field.mode,
            "weight": field.weight, "left_values": a, "right_values": b,
            "shared": shared, "used_value": best, "outcome": result, "points": points,
            **({} if field.block_on_conflict else {"block_on_conflict": False}),
        })
    score = round(100 * total / sum(f.weight for f in rules.fields), 2)
    if score >= rules.accept and not blocking_conflict:
        status, reason = "accepted", "accept_threshold"
    elif score >= rules.review:
        status = "review"
        reason = "conflict_blocks_acceptance" if score >= rules.accept else "review_threshold"
    else:
        status, reason = "below_threshold", "below_review_threshold"
    return dict(status=status, reason=reason, score=score, evidence=evidence)
