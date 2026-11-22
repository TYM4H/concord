"""Объяснимое ранжирование пар полей по наблюдениям двух независимых выборок."""
from collections import Counter
from dataclasses import dataclass
import re

from ..rules import normalize
from .sample import SampleField

# Небольшой словарь помогает с распространёнными обозначениями. Для неизвестных
# имён остаются общие части названий и свидетельства из самих данных.
ALIASES = {
    "почта": "email", "mail": "email", "телефон": "phone", "tel": "phone",
    "telephone": "phone", "mobile": "phone", "название": "name", "имя": "name",
    "address": "адрес", "дата": "date", "код": "code",
}
FORMATS = {
    "email": re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),
    "uuid": re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"),
    "date": re.compile(r"\d{4}-\d{2}-\d{2}"),
    "phone": re.compile(r"\+(?:[\s().-]*\d){8,15}"),
    "number": re.compile(r"[+-]?\d+(?:[.,]\d+)?"),
}


def name_tokens(name):
    spaced = re.sub(r"(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])", " ", name)
    tokens = re.findall(r"[^\W_]+", normalize(spaced, "text").replace("e-mail", "email"))
    return {ALIASES.get(token, token) for token in tokens}


@dataclass
class Summary:
    field: SampleField
    tokens: set
    shape: str | None
    coverage: float
    diversity: float
    preview: dict


def summarize(field, size):
    if not field.values:
        return None
    shapes = Counter()
    for value in field.values:
        for shape, regex in FORMATS.items():
            if regex.fullmatch(value):
                shapes[shape] += 1
                break
    shape = next((s for s, count in shapes.items() if count >= .8 * len(field.values)), None)
    populated = len(field.entities)
    diversity = len(field.values) / sum(field.frequency.values())
    examples = [field.values[v] for v in sorted(field.values)[:3]]
    preview = dict(path=field.path, column=field.name, type=field.sql_type, via=field.via,
                   examples=examples, sampled_entities=size, populated_entities=populated,
                   distinct_values=len(field.values), max_value_frequency=max(field.frequency.values()),
                   partial=field.clipped)
    return Summary(field, name_tokens(field.name), shape, populated / size, diversity, preview)


def rank_pair(left, right):
    a, b = left.field, right.field
    same_shape = left.shape and left.shape == right.shape
    if a.family != b.family and not (same_shape and "text" in (a.family, b.family)):
        return None
    if left.shape and right.shape and left.shape != right.shape:
        return None
    if any(len(f.values) == 1 and len(f.entities) > 1 for f in (a, b)):
        return None

    tokens = left.tokens | right.tokens
    name = len(left.tokens & right.tokens) / len(tokens) if tokens else 0
    shared = len(a.values.keys() & b.values.keys())
    overlap = shared / min(len(a.values), len(b.values))
    distinctive_format = same_shape and left.shape in ("email", "uuid", "phone")
    diversity = min(left.diversity, right.diversity)
    # Числовые диапазоны и небольшие справочники часто пересекаются случайно.
    # Для полей с произвольными названиями требуется более одного общего текста.
    data_support = shared >= 2 and overlap >= .2 and diversity >= .25 and (
        a.family == b.family == "text" and left.shape not in ("number", "date")
        and right.shape not in ("number", "date"))
    if name < .45 and not distinctive_format and not data_support:
        return None

    reasons = []
    if name:
        reasons.append("Сходные названия полей: " + ", ".join(sorted(left.tokens & right.tokens)) + ".")
    if same_shape:
        reasons.append(f"В обеих выборках преобладает формат {left.shape}.")
    reasons.append(f"Общих различных значений после нормализации: {shared}.")
    if diversity < .5:
        reasons.append("Значения часто повторяются у разных ID.")
    if min(left.coverage, right.coverage) < .5:
        reasons.append("Менее половины выбранных ID представлены значениями хотя бы с одной стороны.")
    if left.field.clipped or right.field.clipped:
        reasons.append("Часть строк или длинных значений не вошла в статистику.")
    support = overlap * min(1, shared / 3)
    score = round(40 * name + 29 * support + 18 * bool(distinctive_format)
                  + 8 * diversity + 5 * min(left.coverage, right.coverage), 1)
    return dict(left=left.preview, right=right.preview, rank=score, shared_values=shared,
                mode="text" if a.family == b.family == "text" else "exact", reasons=reasons)


def suggest_fields(left_sample, right_sample):
    left = [s for f in left_sample.fields if (s := summarize(f, left_sample.size))]
    right = [s for f in right_sample.fields if (s := summarize(f, right_sample.size))]
    pairs = []
    for a in left:
        options = [pair for b in right if (pair := rank_pair(a, b)) is not None]
        options.sort(key=lambda p: (-p["rank"], p["right"]["path"]))
        pairs.extend(options[:3])
    pairs.sort(key=lambda p: (-p["rank"], p["left"]["path"], p["right"]["path"]))

    def source(sample):
        return dict(id=sample.id_path, sampled_entities=sample.size, examined_fields=len(sample.fields),
                    warnings=sample.warnings)

    return {"left": source(left_sample), "right": source(right_sample), "suggestions": pairs}
