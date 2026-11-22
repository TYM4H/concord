"""Сохранение объяснимого результата одного запуска."""
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from .matching import match_in_database
from .run_snapshot import VERSION, seal_snapshot, snapshot_name


def write_record(stream, data, checksum):
    line = (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    stream.write(line)
    checksum.update(line)


def export_run(destination, left, right, rules, sources):
    """Сохранить результат нового запуска и вернуть счётчики по статусам."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"Файл уже существует: {destination}")
    snapshot_id = uuid4().hex
    header = dict(type="run", version=1, rules=asdict(rules), sources=sources,
                  diagnostics={"version": VERSION, "id": snapshot_id})
    checksum = sha256()
    counts = Counter()
    with TemporaryDirectory(prefix=".concord-run-", dir=destination.parent) as tmp:
        index = Path(tmp) / snapshot_name(snapshot_id)
        report = Path(tmp) / "result.jsonl"
        index.touch(mode=0o600)
        report.touch(mode=0o600)
        with closing(sqlite3.connect(index)) as db, report.open("wb") as output:
            write_record(output, header, checksum)
            for row in match_in_database(db, left, right, rules, record_misses=True):
                counts[row["status"]] += 1
                write_record(output, {"type": "result", **row}, checksum)
            seal_snapshot(db, header, checksum.hexdigest())
        # Публикация JSONL последней гарантирует, что его снимок уже на месте.
        snapshot = destination.parent / index.name
        os.link(index, snapshot)
        try:
            os.link(report, destination)
        except BaseException:
            snapshot.unlink()
            raise
    return counts
