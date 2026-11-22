"""Поиск путей и ограниченная выборка значений от выбранных ID."""
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from uuid import UUID

from ..rules import normalize
from ..sources.adapters import sqlalchemy
from ..sources.reader import catalog, feature
from ..sources.relations import declared_edges, projection_queries, resolve_route

MAX_ROUTES = 24
MAX_FIELDS = 128
MAX_LENGTH = 256


@dataclass
class SampleField:
    path: str
    name: str
    sql_type: str
    family: str
    via: list
    values: dict = field(default_factory=dict)  # Нормализованное значение → представительный оригинал.
    frequency: Counter = field(default_factory=Counter)
    entities: set = field(default_factory=set)
    clipped: bool = False


@dataclass
class Sample:
    id_path: str
    size: int
    fields: list
    warnings: list


def walk(root, edges, depth, warnings):
    pending = deque([(root, (), {root})])
    visited = 0
    while pending:
        table, route, used = pending.popleft()
        visited += 1
        yield table, route
        if len(route) < depth:
            for hop in sorted(edges[table], key=lambda h: (h.table, h.source_columns, h.target_columns)):
                if hop.table not in used:
                    if visited + len(pending) >= MAX_ROUTES:
                        message = f"Обзор ограничен {MAX_ROUTES} путями к таблицам."
                        if message not in warnings:
                            warnings.append(message)
                        break
                    pending.append((hop.table, (*route, hop), used | {hop.table}))


def discover(profile, tables, key, depth, warnings):
    edges = declared_edges(profile.schema, tables)
    prefix = f"{profile.schema}.{profile.table}."
    known = {resolve_route(profile.table, spec, tables, edges): name
             for name, spec in profile.routes.items()}
    # Совпадающие названия таблиц в разных ролях (например, buyer и seller)
    # должны по-прежнему обозначать разные маршруты в настройках запуска.
    reserved = {prefix + c["name"] for c in tables[profile.table]["columns"]} | set(profile.routes)
    fields = []
    routes = {}
    for table, hops in walk(profile.table, edges, depth, warnings):
        spec = tables[table]
        keys = set(spec["primary_key"])
        for fk in spec["foreign_keys"]:
            keys.update(fk["columns"])
        for hop in edges[table]:
            keys.update(hop.source_columns)
        if not hops:
            keys.add(key)
        via = [{"table": h.table, "from": list(h.source_columns), "to": list(h.target_columns)} for h in hops]
        for column in spec["columns"]:
            name = column["name"]
            if name in keys or not column["family"]:
                continue
            if len(fields) == MAX_FIELDS:
                warnings.append(f"Обзор ограничен {MAX_FIELDS} полями.")
                return fields, routes
            path = prefix + ".".join([*(h.table for h in hops), name])
            if hops:
                if (hops, name) in known:
                    path = known[hops, name]
                else:
                    base = path
                    index = 1
                    while path in reserved:
                        index += 1
                        path = f"{base} [{index}]"
                reserved.add(path)
                routes[path] = {"column": name, "via": via}
            fields.append(SampleField(path, name, column["type"], column["family"], via))
    return fields, routes


def sample_fields(conn, profile, id_column, *, sample_size=100, max_hops=2):
    if not 1 <= sample_size <= 500 or not 0 <= max_hops <= 3:
        raise ValueError("Размер выборки должен быть от 1 до 500 ID, глубина — от 0 до 3")
    if not profile.schema or not profile.table:
        raise ValueError("Выберите схему и таблицу в профиле источника")
    schema = catalog(conn, profile.schema)
    tables = {t["table"]: t for t in schema["tables"]}
    if profile.table not in tables:
        raise ValueError(f"Таблица не найдена: {profile.table}")
    prefix = f"{profile.schema}.{profile.table}."
    names = {c["name"] for c in tables[profile.table]["columns"]}
    key_name = id_column if id_column in names else id_column.removeprefix(prefix)
    if key_name not in names:
        raise ValueError(f"Не найден идентификатор {id_column} в таблице {profile.table}")
    id_path = prefix + key_name
    _, base_query = next(projection_queries(profile, tables, id_path, []))
    key = base_query.selected_columns[0]
    ids = conn.execute(base_query.distinct().order_by(key).limit(sample_size)).scalars().all()
    if any(type(value) not in (str, int, UUID) or (isinstance(value, str) and not value.strip())
           for value in ids):
        raise ValueError(f"Выберите непустой строковый, целочисленный или UUID идентификатор: {id_path}")
    if not ids:
        return Sample(id_path, 0, [], ["Выбранная таблица пуста."])
    warnings = []
    fields, routes = discover(profile, tables, key_name, max_hops, warnings)
    by_path = {f.path: f for f in fields}
    query_profile = replace(profile, routes=routes)
    sa = sqlalchemy()
    for paths, query in projection_queries(query_profile, tables, id_path, list(by_path)):
        if not paths:
            continue
        key, *columns = query.selected_columns
        group = [by_path[p] for p in paths]
        # Читаем ограниченный префикс длинного текста и пропускаем усечённые
        # значения целиком, чтобы общий префикс не давал ложных совпадений.
        selection = []
        for column, f in zip(columns, group):
            if f.family == "text":
                # В PostgreSQL enum требует приведения к строке для substr.
                # В SQLite сохраняем исходный тип BLOB при динамической типизации.
                if conn.dialect.name == "postgresql":
                    column = sa.cast(column, sa.String)
                column = sa.func.substr(column, 1, MAX_LENGTH + 1)
            selection.append(column)
        limit = len(ids) * 8
        statement = query.with_only_columns(key, *selection).where(key.in_(ids)).limit(limit + 1)
        observed = [defaultdict(set) for _ in group]
        with conn.execute(statement) as result:
            for i, (entity_id, *values) in enumerate(result):
                if i == limit:
                    for f in group:
                        f.clipped = True
                    warnings.append(f"Достигнут предел {limit} строк при чтении пути к {paths[0]}.")
                    break
                for f, owners, value in zip(group, observed, values):
                    if value is None or isinstance(value, (bytes, memoryview)):
                        continue
                    raw = feature(value, f.path)
                    if len(raw) > MAX_LENGTH:
                        f.clipped = True
                        continue
                    norm = normalize(raw, "text")
                    if norm:
                        owners[norm].add(entity_id)
                        f.values.setdefault(norm, raw)
        for f, owners in zip(group, observed):
            for value, entities in owners.items():
                f.frequency[value] = len(entities)
                f.entities.update(entities)
    if not any(f.values for f in fields):
        warnings.append("В обследованных полях нет пригодных для сравнения значений.")
    return Sample(id_path, len(ids), fields, warnings)
