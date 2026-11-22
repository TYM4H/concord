"""Локальный индекс автоматического запуска и отдельные решения аналитика."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from .rules import Rules


STATUSES = ("accepted", "review", "unmatched")
ACTIONS = ("confirmed", "rejected", "deferred")


class ReviewConflict(ValueError):
    """Другой запрос сохранил более новую версию решения."""


def review_path(path):
    path = Path(path)
    return path.with_name(path.name + ".review.sqlite3")


def _metadata(db):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_metadata'").fetchone()
    if table is None:
        return None
    row = db.execute("SELECT digest, header, counts FROM run_metadata WHERE id=1").fetchone()
    if row is None:
        raise ValueError("В хранилище проверки отсутствует запись запуска")
    return row


def result_signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _check_snapshot(stream, path, original):
    if original != result_signature(os.fstat(stream.fileno())) or original != result_signature(path.stat()):
        raise ValueError("Файл результата изменился во время открытия. Откройте нужный запуск повторно")


def _file_digest(path):
    checksum = sha256()
    with path.open("rb") as stream:
        original = result_signature(os.fstat(stream.fileno()))
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
        _check_snapshot(stream, path, original)
    return original, checksum.hexdigest()


def indexed_counts(path):
    """Статистика истории и проверенная подпись JSONL, если его индекс актуален."""
    storage = review_path(path)
    if storage.is_symlink() or not storage.is_file():
        return None, None
    try:
        with closing(sqlite3.connect(storage.absolute().as_uri() + "?mode=ro", uri=True)) as db:
            meta = _metadata(db)
        if meta is not None:
            digest, _, counts = meta
            counts = json.loads(counts)
            if (isinstance(counts, dict) and counts.keys() == set(STATUSES)
                    and all(type(n) is int and n >= 0 for n in counts.values())):
                verified = _file_digest(path)
                if verified[1] == digest:
                    return counts, verified
    except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
        pass  # История сможет прочитать сам JSONL без индекса.
    return None, None


def _load_existing(path, metadata, verified):
    digest, header, counts = metadata
    if verified is None or verified[0] != result_signature(path.stat()):
        verified = _file_digest(path)
    if verified[1] != digest:
        raise ValueError("Содержимое JSONL отличается от сохранённого запуска. Для прежних решений "
                         "восстановите исходный JSONL. Новый запуск откройте под отдельным именем")
    return Rules.parse(json.loads(header)["rules"]), json.loads(counts)


def _score_row(n, item, field_count):
    status = item["status"]
    if status == "unmatched":
        return n, None, 0, status
    score = item.get("score")
    if type(score) not in (int, float) or not 0 <= score <= 100:
        return n, None, -1, status
    if status == "accepted":
        return n, score, 1, status
    if item.get("reason") == "conflict_blocks_acceptance":
        return n, score, 0, status
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        complete = len(evidence) == field_count
        for field in evidence:
            outcome = field.get("outcome") if isinstance(field, dict) else None
            if outcome == "different":
                if field.get("block_on_conflict", True):
                    return n, score, 0, status
            elif outcome not in ("equal", "missing"):
                complete = False
        if complete:
            return n, score, 1, status
    # В старом неполном результате отсутствие расхождения может быть неизвестно.
    return n, score, -1, status


def _create_scores(db):
    db.execute("""CREATE TABLE pair_score (
        n INTEGER PRIMARY KEY REFERENCES result(n), score REAL, eligible INTEGER NOT NULL,
        status TEXT NOT NULL)""")


def _index_scores(db):
    db.execute("CREATE INDEX pair_score_order ON pair_score(score DESC,n ASC)")
    db.execute("CREATE INDEX pair_score_eligible ON pair_score(eligible,score)")
    db.execute("CREATE INDEX pair_score_status_order ON pair_score(status,score DESC,n ASC)")


def _upgrade_scores(db, field_count):
    exists = "SELECT 1 FROM sqlite_master WHERE type='index' AND name='pair_score_status_order'"
    if db.execute(exists).fetchone():
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(exists).fetchone():
            return
        # Таблица содержит только производные данные. Старый индекс без статуса
        # заменяется в той же транзакции, что и заполнение обновлённого индекса.
        db.execute("DROP TABLE IF EXISTS pair_score")
        _create_scores(db)
        records = db.execute("SELECT n,body FROM result ORDER BY n")
        db.executemany("INSERT INTO pair_score VALUES (?,?,?,?)",
                       (_score_row(n, json.loads(body), field_count) for n, body in records))
        _index_scores(db)


def _import(path, db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        raise ValueError("По пути хранилища проверки находится другая база данных")
    db.execute("""CREATE TABLE result (
        n INTEGER PRIMARY KEY, status TEXT NOT NULL, left_key TEXT, body TEXT NOT NULL)""")
    _create_scores(db)
    db.execute("""CREATE TABLE decision (
        n INTEGER PRIMARY KEY REFERENCES result(n), action TEXT NOT NULL
        CHECK(action IN ('confirmed','rejected','deferred')), note TEXT NOT NULL,
        updated_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0))""")
    counts = dict.fromkeys(STATUSES, 0)
    checksum = sha256()
    with path.open("rb") as stream:
        original = result_signature(os.fstat(stream.fileno()))
        first = stream.readline()
        header = json.loads(first.decode("utf-8"))
        if not isinstance(header, dict) or header.get("type") != "run" or header.get("version") != 1:
            raise ValueError("Неизвестный формат результата")
        rules = Rules.parse(header.get("rules"))
        checksum.update(first)
        for line_no, line in enumerate(stream, 2):
            checksum.update(line)
            body = line.decode("utf-8")
            item = json.loads(body)
            if not isinstance(item, dict) or item.get("type") != "result" or item.get("status") not in STATUSES:
                raise ValueError(f"Некорректный результат в строке {line_no}")
            status = item["status"]
            left, right = item["left_id"], item["right_id"]
            for key in (left, right):
                if key is not None and type(key) not in (int, str):
                    raise ValueError(f"Некорректный тип ID в строке {line_no}")
            if status == "unmatched":
                valid_ids = (left is None) != (right is None)
            else:
                valid_ids = left is not None and right is not None
            if not valid_ids:
                raise ValueError(f"Некорректные ID в строке {line_no}")
            left_key = json.dumps(left, ensure_ascii=False) if left is not None else None
            n = db.execute("INSERT INTO result(status, left_key, body) VALUES (?,?,?)",
                           (status, left_key, body)).lastrowid
            db.execute("INSERT INTO pair_score VALUES (?,?,?,?)",
                       _score_row(n, item, len(rules.fields)))
            counts[status] += 1
        _check_snapshot(stream, path, original)
    db.execute("CREATE INDEX by_status ON result(status, n)")
    db.execute("CREATE INDEX by_left_key ON result(left_key, n)")
    db.execute("CREATE INDEX by_action ON decision(action, n)")
    _index_scores(db)
    db.execute("""CREATE TABLE run_metadata (
        id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL, header TEXT NOT NULL, counts TEXT NOT NULL)""")
    db.execute("INSERT INTO run_metadata VALUES (1, ?, ?, ?)",
               (checksum.hexdigest(), first.decode("utf-8"), json.dumps(counts)))
    return rules, counts


def load_run(path, db: sqlite3.Connection, *, verified=None):
    """Открыть снимок или атомарно построить его индекс. Старые решения сохраняются."""
    path = Path(path)
    meta = _metadata(db)
    if meta is None:
        with db:
            db.execute("BEGIN IMMEDIATE")
            meta = _metadata(db)
            if meta is None:
                return _import(path, db)
    rules, counts = _load_existing(path, meta, verified)
    _upgrade_scores(db, len(rules.fields))
    return rules, counts


def set_decision(db: sqlite3.Connection, n, action, note, revision):
    """Сохранить решение для пары на проверке; revision=0 означает первый выбор."""
    if type(n) is not int or not 1 <= n < 2**63:
        raise ValueError("Некорректный номер пары")
    if action not in ACTIONS:
        raise ValueError("Неизвестное решение аналитика")
    if type(revision) is not int or revision < 0:
        raise ValueError("Некорректная версия решения")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("Примечание должно содержать до 2000 символов")

    with db:
        db.execute("BEGIN IMMEDIATE")
        pair = db.execute("""SELECT r.status, d.action, d.note, coalesce(d.revision, 0)
            FROM result r LEFT JOIN decision d USING(n) WHERE r.n=?""", (n,)).fetchone()
        if pair is None or pair[0] != "review":
            raise ValueError("Решение аналитика доступно для пар со статусом «Возможное совпадение»")
        _, current_action, current_note, current_revision = pair
        if action == current_action and note == current_note and revision <= current_revision:
            return current_revision
        if revision != current_revision:
            raise ReviewConflict("Решение уже изменилось. Обновите страницу и проверьте текущий выбор")
        updated = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO decision(n, action, note, updated_at, revision) VALUES (?,?,?,?,?)
            ON CONFLICT(n) DO UPDATE SET action=excluded.action, note=excluded.note,
            updated_at=excluded.updated_at, revision=excluded.revision""",
                   (n, action, note, updated, revision + 1))
    return revision + 1
