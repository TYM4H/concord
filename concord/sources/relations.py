"""Построение запросов по подтверждённым внешним ключам в одной схеме."""
from collections import defaultdict
from dataclasses import dataclass

from .adapters import sqlalchemy


@dataclass(frozen=True)
class Hop:
    table: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]


def declared_edges(schema, tables):
    edges = defaultdict(set)
    for child, table in tables.items():
        for fk in table["foreign_keys"]:
            parent = fk["target_table"]
            if fk["target_schema"] != schema or parent not in tables:
                continue
            source, target = tuple(fk["columns"]), tuple(fk["target_columns"])
            edges[child].add(Hop(parent, source, target))
            edges[parent].add(Hop(child, target, source))
    return edges


def resolve_route(root, spec, tables, edges):
    if not isinstance(spec, dict) or set(spec) != {"via", "column"}:
        raise ValueError("Связанному полю нужны via и column")
    if not isinstance(spec["via"], list) or not spec["via"]:
        raise ValueError("via должен содержать переходы между таблицами")
    hops = []
    current = root
    for step in spec["via"]:
        if not isinstance(step, dict) or set(step) != {"table", "from", "to"}:
            raise ValueError("Переходу нужны table, from и to")
        for key in ("from", "to"):
            if not isinstance(step[key], list) or not all(isinstance(c, str) for c in step[key]):
                raise ValueError(f"Колонки {key} нужно перечислить списком имён")
        if not isinstance(step["table"], str):
            raise ValueError("Имя таблицы должно быть строкой")
        hop = Hop(step["table"], tuple(step["from"]), tuple(step["to"]))
        if hop not in edges[current]:
            raise ValueError(f"Связь {current} → {hop.table} с указанными колонками не найдена")
        hops.append(hop)
        current = hop.table
    column = spec["column"]
    if column not in [c["name"] for c in tables[current]["columns"]]:
        raise ValueError(f"Колонка {column} не найдена в таблице {current}")
    return tuple(hops), column


def projection_queries(profile, tables, id_path, paths):
    sa = sqlalchemy()
    root = profile.table
    if root not in tables:
        raise ValueError(f"Таблица не найдена: {root}")

    def relation(name):
        fields = [sa.column(c["name"]) for c in tables[name]["columns"]]
        return sa.table(name, *fields, schema=profile.schema)

    base = relation(root).alias("root_entity")
    prefix = f"{profile.schema}.{root}."
    direct = {prefix + c.name: c.name for c in base.c}
    if id_path not in direct:
        raise ValueError(f"Не найден идентификатор {id_path} в таблице {root}")
    key = base.c[direct[id_path]]
    edges = declared_edges(profile.schema, tables)
    groups = {(): []}  # Базовый запрос сохраняет ID, даже если связей нет.
    seen = set()
    for path in paths:
        if path in direct and path not in profile.routes:
            route, column = (), direct[path]
        elif path in profile.routes and path not in direct:
            route, column = resolve_route(root, profile.routes[path], tables, edges)
        else:
            raise ValueError(f"Не найден путь или имя неоднозначно: {path}")
        # Внешний ключ может просто копировать выбранный ID в другую таблицу.
        id_copies = {key.name}
        for hop in route:
            id_copies = {b for a, b in zip(hop.source_columns, hop.target_columns) if a in id_copies}
        if column in id_copies:
            raise ValueError(f"Поле {path} повторяет выбранный идентификатор")
        if (route, column) in seen:
            raise ValueError(f"Признак {path} повторяет уже выбранное поле")
        seen.add((route, column))
        groups.setdefault(route, []).append((path, column))

    for route, fields in groups.items():
        current = joins = base
        for i, hop in enumerate(route):
            target = relation(hop.table).alias(f"rel_{i}")
            condition = sa.and_(*(current.c[a] == target.c[b]
                                  for a, b in zip(hop.source_columns, hop.target_columns)))
            joins = joins.join(target, condition)
            current = target
        selection = [current.c[col] for _, col in fields]
        yield [path for path, _ in fields], sa.select(key, *selection).select_from(joins)
