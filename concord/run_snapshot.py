"""Привязка диагностического снимка к содержимому завершённого запуска."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

# При изменении отбора или расчёта баллов прежнему снимку нужен свой обработчик.
# Во второй версии missed содержит все сравнения ниже порога, включая ID с принятой парой.
VERSION = 2
ALGORITHM = "blocking-frequency-score-v1"


def _algorithm(header):
    fields = header.get("rules", {}).get("fields", ())
    if any(field.get("block_on_conflict", True) is False for field in fields):
        return "blocking-frequency-score-field-conflicts-v1"
    return ALGORITHM


class SnapshotUnavailable(ValueError):
    """Диагностика по данным и правилам этого запуска недоступна."""


def snapshot_name(snapshot_id):
    if not isinstance(snapshot_id, str) or re.fullmatch(r"[0-9a-f]{32}", snapshot_id) is None:
        raise SnapshotUnavailable("Некорректный идентификатор диагностического снимка")
    return f"concord-diagnostics-{snapshot_id}.sqlite3"


def _header_digest(header):
    encoding = json.dumps(header, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256(encoding).hexdigest()


def seal_snapshot(db, header, digest):
    db.execute("CREATE INDEX missed_by_score ON missed(score DESC,left_id,right_id)")
    db.execute("""CREATE TABLE snapshot_metadata (
        id TEXT PRIMARY KEY, version INTEGER NOT NULL, algorithm TEXT NOT NULL,
        header_digest TEXT NOT NULL, result_digest TEXT NOT NULL
    ) WITHOUT ROWID""")
    db.execute("INSERT INTO snapshot_metadata VALUES (?, ?, ?, ?, ?)",
               (header["diagnostics"]["id"], VERSION, _algorithm(header), _header_digest(header), digest))
    db.commit()


def _result_digest(path):
    checksum = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def open_snapshot(result_path, header, *, result_digest=None, check_same_thread=True):
    """Открыть read-only снимок. Можно передать SHA-256, уже проверенный load_run."""
    ref = header.get("diagnostics")
    if ref is None:
        raise SnapshotUnavailable("В этом запуске диагностический снимок не сохранялся. "
                                  "Для полного разбора выполните новое сопоставление")
    if not isinstance(ref, dict) or type(ref.get("version")) is not int or ref["version"] not in (1, VERSION):
        raise SnapshotUnavailable("Эта версия диагностического снимка не поддерживается")
    snapshot_id = ref.get("id")
    result_path = Path(result_path)
    path = result_path.absolute().parent / snapshot_name(snapshot_id)
    if path.is_symlink() or not path.is_file():
        raise SnapshotUnavailable(f"Для разбора нужен исходный файл {path.name} рядом с результатом")
    try:
        digest = result_digest if result_digest is not None else _result_digest(result_path)
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=check_same_thread)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            with closing(db.execute("SELECT version, algorithm, header_digest, result_digest "
                                    "FROM snapshot_metadata WHERE id=?", (snapshot_id,))) as cursor:
                metadata = cursor.fetchone()
            if (metadata is None or metadata[0] != ref["version"]
                    or metadata[2:] != (_header_digest(header), digest)
                    or metadata[1] != _algorithm(header)):
                raise SnapshotUnavailable("Диагностический снимок не соответствует запуску или версии расчёта")
            return db
        except BaseException:
            db.close()
            raise
    except (sqlite3.Error, OSError) as exc:
        raise SnapshotUnavailable("Не удалось прочитать диагностический снимок запуска") from exc
