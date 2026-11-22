"""Запуски локального рабочего пространства и их изолированные хранилища проверки."""
from contextlib import ExitStack, closing
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from .review_store import STATUSES, indexed_counts, load_run, result_signature, review_path
from .rules import Rules
from .run_snapshot import SnapshotUnavailable, open_snapshot


def run_key(path):
    return sha256(os.fsencode(str(path))).hexdigest()[:24]


class OpenRun:
    def __init__(self, path, verified=None):
        self.path = path
        self.signature = result_signature(path.stat())
        storage = review_path(path)
        os.close(os.open(storage, os.O_CREAT | os.O_RDWR, 0o600))
        with ExitStack() as stack:
            self.db = stack.enter_context(closing(sqlite3.connect(storage, check_same_thread=False)))
            self.rules, self.counts = load_run(path, self.db, verified=verified)
            header, digest = self.db.execute("SELECT header,digest FROM run_metadata WHERE id=1").fetchone()
            self.header = json.loads(header)
            self.message = ""
            try:
                self.snapshot = stack.enter_context(closing(open_snapshot(path, self.header, result_digest=digest,
                                                                         check_same_thread=False)))
            except SnapshotUnavailable as exc:
                self.snapshot = None
                self.message = str(exc)
            self.resources = stack.pop_all()


class FirstRun:
    """Настройки формы до первого сохранённого результата."""
    path = snapshot = None
    rules = Rules("", "", ())


class Workspace:
    def __init__(self, initial, folder):
        self.initial = initial.resolve(strict=True) if initial else None
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder.resolve(strict=True)
        if not self.folder.is_dir():
            raise ValueError("Выберите каталог рабочего пространства")
        self.opened = {}
        self.files = {}
        self.entries = {}
        self.verified = {}
        if self.initial is None:
            recent = self.listing()
            if recent:
                self.initial = self.files[recent[0]["key"]]
        if self.initial:
            self.open(self.initial)

    def close(self):
        for run in self.opened.values():
            run.resources.close()

    def profiles(self):
        profiles = []
        for path in sorted((self.folder / ".concord-profiles").glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open(encoding="utf-8") as stream:
                    profile = json.loads(stream.read(1024 * 1024))
                label = " · ".join(str(profile[key]) for key in ("driver", "database", "schema", "table")
                                   if profile.get(key))
                profiles.append((path, label))
            except (AttributeError, OSError, ValueError):
                continue
        return profiles

    def open(self, path):
        if path not in self.opened:
            new = OpenRun(path, self.verified.pop(path, None))
            if len(self.opened) >= 3:
                old = next(p for p in self.opened if p != self.initial)
                self.opened.pop(old).resources.close()
            self.opened[path] = new
        return self.opened[path]

    def listing(self):
        paths = {self.initial} if self.initial else set()
        for pattern in ("*.jsonl", "*/*.jsonl"):
            for path in self.folder.glob(pattern):
                if path.parent.name.startswith(".") or path.is_symlink() or path.parent.is_symlink():
                    continue
                if path.is_file():
                    paths.add(path)
        items = []
        self.files = {}
        for path in paths:
            try:
                stat = path.stat()
                stamp = result_signature(stat)
                if self.entries.get(path, (None,))[0] != stamp:
                    with path.open("rb") as stream:
                        header = json.loads(stream.readline(4 * 1024 * 1024))
                    if not isinstance(header, dict) or header.get("type") != "run" or header.get("version") != 1:
                        continue
                    rules = Rules.parse(header["rules"])
                    labels = []
                    for side in ("left", "right"):
                        info = (header.get("sources") or {}).get(side, {})
                        label = info.get("file") or ".".join(str(v) for v in (info.get("schema"), info.get("table")) if v)
                        labels.append(label or getattr(rules, side + "_id"))
                    name = str(path.relative_to(self.folder)) if path.is_relative_to(self.folder) else path.name
                    self.entries[path] = stamp, dict(key=run_key(path), name=name,
                        time=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        sources=" → ".join(labels), source_labels=labels, counts=None)
                item = self.entries[path][1]
                if path in self.opened and stamp == self.opened[path].signature:
                    item["counts"] = self.opened[path].counts
                elif item["counts"] is None:
                    counts, verified = indexed_counts(path)
                    if verified is not None:
                        self.verified[path] = verified
                    if counts is None:
                        counts = dict.fromkeys(STATUSES, 0)
                        with path.open("rb") as stream:
                            next(stream)
                            for line in stream:
                                row = json.loads(line)
                                counts[row["status"]] += 1
                    item["counts"] = counts
                self.files[item["key"]] = path
                items.append(item)
            except (AttributeError, OSError, KeyError, ValueError, TypeError, sqlite3.DatabaseError):
                continue
        return sorted(items, key=lambda item: (item["time"], item["name"]), reverse=True)

    def by_key(self, key):
        if key not in self.files:
            self.listing()
        path = self.files.get(key)
        return self.open(path) if path is not None and path.is_file() else None
