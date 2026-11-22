"""Командная строка для запуска ядра на локальных проекциях."""
import argparse
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

from .runs import export_run
from .rules import Rules
from .suggestions.selection import compile_selection


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line, text in enumerate(stream, 1):
            if text.strip():
                try:
                    yield json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name}, строка {line}: неверный JSON") from exc


def source_input(args, side, rules, selected_routes=None):
    profile_path = getattr(args, side + "_source")
    if profile_path is None:
        path = getattr(args, side)
        info = {"format": "jsonl", "file": path.name}
        if selected_routes:
            info["routes"] = selected_routes
        return rows(path), info

    from .sources.adapters import connect
    from .sources.profile import Profile, password_for
    from .sources.reader import projection

    profile = Profile.load(profile_path)
    id_path = getattr(rules, side + "_id")
    paths = [getattr(f, side) for f in rules.fields]
    if selected_routes is not None:
        for path in paths:
            if path in profile.routes and profile.routes[path] != selected_routes.get(path):
                raise ValueError(f"Маршрут поля {path} в выборе отличается от маршрута в профиле {profile_path.name}")
        profile = replace(profile, routes=selected_routes)

    def read():
        with connect(profile, password_for(profile, side.upper())) as conn:
            yield from projection(conn, profile, id_path, paths)

    return read(), {"driver": profile.driver, "database": profile.database,
                    "schema": profile.schema, "table": profile.table,
                    "routes": {p: profile.routes[p] for p in paths if p in profile.routes}}


def run(args):
    if args.selection is not None:
        draft = json.loads(args.selection.read_text(encoding="utf-8"))
        rules, left_routes, right_routes = compile_selection(draft)
    else:
        rules = Rules.parse(json.loads(args.rules.read_text(encoding="utf-8")))
        left_routes = right_routes = None
    left, left_info = source_input(args, "left", rules, left_routes)
    right, right_info = source_input(args, "right", rules, right_routes)
    with closing(left), closing(right):
        counts = export_run(args.output, left, right, rules,
                            {"left": left_info, "right": right_info})
    print(f"Автоматически принято пар: {counts['accepted']}. "
          f"Возможных совпадений: {counts['review']}. Записей без пары: {counts['unmatched']}.")


def main():
    parser = argparse.ArgumentParser(description="Concord — локальное сопоставление сущностей")
    config = parser.add_mutually_exclusive_group(required=True)
    config.add_argument("--rules", type=Path, help="Настройки в JSON")
    config.add_argument("--selection", type=Path, help="Файл выбора с подтверждёнными парами полей")
    for side, label in (("left", "Левый"), ("right", "Правый")):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(f"--{side}", type=Path, help=f"{label} источник в JSONL")
        group.add_argument(f"--{side}-source", type=Path, help=f"{label} профиль базы в JSON")
    parser.add_argument("--output", required=True, type=Path, help="Новый файл результата в JSONL")
    try:
        run(parser.parse_args())
    except (ValueError, OSError, EOFError, sqlite3.Error) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
