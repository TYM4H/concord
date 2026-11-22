"""Дисковый индекс одного запуска. На входе — строки проекции каждого источника."""
import json
import sqlite3
from contextlib import closing
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from tempfile import TemporaryDirectory

from .rules import compare, normalize


SCHEMA = """
CREATE TABLE entity (
    side INTEGER, id TEXT, PRIMARY KEY(side, id)
) WITHOUT ROWID;
CREATE TABLE observation (
    side INTEGER, id TEXT, field INTEGER, norm TEXT, raw TEXT,
    PRIMARY KEY(side, id, field, norm, raw)
) WITHOUT ROWID;
CREATE TEMP TABLE paired_right (id TEXT PRIMARY KEY) WITHOUT ROWID;
"""

INDEX = """
CREATE TEMP TABLE token (
    side INTEGER, field INTEGER, norm TEXT, id TEXT,
    PRIMARY KEY(field, norm, side, id)
) WITHOUT ROWID;
INSERT OR IGNORE INTO token SELECT side, field, norm, id FROM observation;
CREATE TABLE frequency (
    field INTEGER, norm TEXT, nl INTEGER, nr INTEGER,
    PRIMARY KEY(field, norm)
) WITHOUT ROWID;
INSERT INTO frequency
    SELECT field, norm, SUM(side = 0), SUM(side = 1) FROM token GROUP BY field, norm;
"""


def ingest(db, side, rows, rules):
    id_path = rules.right_id if side else rules.left_id
    paths = [field.right if side else field.left for field in rules.fields]
    for line, row in enumerate(rows, 1):
        context = f"{'Правый' if side else 'Левый'} источник, запись {line}"
        if not isinstance(row, dict):
            raise ValueError(f"{context}: ожидался объект")
        key = row.get(id_path)
        if type(key) not in (int, str) or (isinstance(key, str) and not key.strip()):
            raise ValueError(f"{context}: некорректный ID в поле {id_path}")
        entity_id = json.dumps(key, ensure_ascii=False)
        db.execute("INSERT OR IGNORE INTO entity VALUES (?, ?)", (side, entity_id))
        for index, (path, field) in enumerate(zip(paths, rules.fields)):
            # Отсутствующее поле и NULL представляют отсутствие значения.
            values = row.get(path)
            if not isinstance(values, list):
                values = [values]
            for raw in values:
                if raw is None:
                    continue
                if not isinstance(raw, str):
                    raise ValueError(f"{context}: поле {path} ожидает текст или список строк")
                norm = normalize(raw, field.mode)
                if norm:
                    db.execute("INSERT OR IGNORE INTO observation VALUES (?, ?, ?, ?, ?)",
                               (side, entity_id, index, norm, raw))


def _field_values(observations):
    fields = {}
    for _, index, norm, raw, nl, nr in observations:
        if index is None:
            continue
        item = fields.setdefault(index, {}).setdefault(norm, {"originals": [], "counts": [nl, nr]})
        item["originals"].append(raw)
    return fields


def values(db, side, key):
    return _field_values(db.execute("""
        SELECT o.id, o.field, o.norm, o.raw, f.nl, f.nr
        FROM observation o JOIN frequency f USING (field, norm)
        WHERE o.side = ? AND o.id = ? ORDER BY o.field, o.norm, o.raw
    """, (side, key)))


def _left_records(db):
    # LEFT JOIN оставляет в потоке и ID, у которых нет заполненных признаков.
    observations = db.execute("""
        SELECT e.id, o.field, o.norm, o.raw, f.nl, f.nr
        FROM entity e LEFT JOIN observation o ON o.side=e.side AND o.id=e.id
        LEFT JOIN frequency f ON f.field=o.field AND f.norm=o.norm
        WHERE e.side=0 ORDER BY e.id, o.field, o.norm, o.raw
    """)
    for left_id, group in groupby(observations, itemgetter(0)):
        yield left_id, _field_values(group)


def candidates(db, fields):
    ids = set()
    for index, variants in fields.items():
        for norm in variants:
            matches = db.execute("SELECT id FROM token WHERE side=1 AND field=? AND norm=?",
                                 (index, norm))
            ids.update(row[0] for row in matches)
    return sorted(ids)


def results(db, rules, record_misses=False):
    for left_id, left in _left_records(db):
        found = False
        best = None
        for right_id in candidates(db, left):
            result = compare(rules, left, values(db, 1, right_id))
            result.update(left_id=json.loads(left_id), right_id=json.loads(right_id))
            if result["status"] == "below_threshold":
                if record_misses:
                    db.execute("INSERT INTO missed VALUES (?, ?, ?)", (left_id, right_id, result["score"]))
                if best is None or result["score"] > best["score"]:
                    best = result
                continue
            db.execute("INSERT OR IGNORE INTO paired_right VALUES (?)", (right_id,))
            found = True
            yield result
        if not found:
            yield dict(left_id=json.loads(left_id), right_id=None, status="unmatched",
                       reason="below_review_threshold" if best else "no_block_candidate",
                       best_comparison=best)
    for (key,) in db.execute("""
        SELECT id FROM entity WHERE side=1 AND id NOT IN (SELECT id FROM paired_right)
        ORDER BY id
    """):
        yield dict(left_id=None, right_id=json.loads(key), status="unmatched",
                   reason="no_retained_pair")


def match_in_database(db, left_rows, right_rows, rules, *, record_misses=False):
    db.executescript(SCHEMA)
    ingest(db, 0, left_rows, rules)
    ingest(db, 1, right_rows, rules)
    db.executescript(INDEX)
    if record_misses:
        db.execute("""CREATE TABLE missed (
            left_id TEXT, right_id TEXT, score REAL NOT NULL,
            PRIMARY KEY(left_id, right_id)
        ) WITHOUT ROWID""")
    yield from results(db, rules, record_misses)


def match(left_rows, right_rows, rules):
    """Возвращает пары и непарные ID. Временная база удаляется при закрытии итератора."""
    with TemporaryDirectory(prefix="concord-") as folder:
        with closing(sqlite3.connect(Path(folder) / "index.sqlite")) as db:
            yield from match_in_database(db, left_rows, right_rows, rules)
