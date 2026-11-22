from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from .adapters import sqlalchemy
from .relations import projection_queries


def type_family(sql_type):
    try:
        native = sql_type.python_type
    except (AttributeError, NotImplementedError):
        return None
    families = {str: "text", int: "number", float: "number", Decimal: "number",
                bool: "boolean", date: "date", time: "time", datetime: "datetime", UUID: "uuid"}
    return families.get(native)


def catalog(conn, schema, table_names=None):
    inspector = sqlalchemy().inspect(conn)
    schemas = inspector.get_schema_names()
    if schema is None:
        return {"schemas": schemas}
    if schema not in schemas:
        raise ValueError(f"Схема не найдена: {schema}")
    columns = inspector.get_multi_columns(schema=schema, filter_names=table_names)
    primary = inspector.get_multi_pk_constraint(schema=schema, filter_names=table_names)
    options = {"postgresql_ignore_search_path": True} if conn.dialect.name == "postgresql" else {}
    foreign = inspector.get_multi_foreign_keys(schema=schema, filter_names=table_names, **options)
    tables = []
    for key, fields in sorted(columns.items()):
        tables.append({
            "table": key[1],
            "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"],
                         "family": type_family(c["type"])}
                        for c in fields],
            "primary_key": primary[key]["constrained_columns"],
            "foreign_keys": [
                dict(name=fk["name"], columns=fk["constrained_columns"],
                     target_schema=fk["referred_schema"] or schema, target_table=fk["referred_table"],
                     target_columns=fk["referred_columns"]) for fk in foreign[key]
            ],
        })
    return {"schema": schema, "tables": tables}


def projection(conn, profile, id_path, field_paths):
    if not profile.table or not profile.schema:
        raise ValueError("Для запуска укажите schema и table в профиле источника")
    names = None if profile.routes else [profile.table]
    schema = catalog(conn, profile.schema, table_names=names)
    tables = {t["table"]: t for t in schema["tables"]}
    for paths, query in projection_queries(profile, tables, id_path, field_paths):
        with conn.execute(query.execution_options(yield_per=1000)) as result:
            for row in result:
                key = row[0]
                if isinstance(key, UUID):
                    key = str(key)
                if type(key) not in (str, int):
                    raise ValueError(f"Ожидался целочисленный, строковый или UUID идентификатор в {id_path}")
                yield {id_path: key, **{path: feature(v, path) for path, v in zip(paths, row[1:])}}


def feature(value, path):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal, UUID, timedelta)):
        return str(value)
    raise ValueError(f"Для {path} требуется скалярный текстовый, числовой или календарный признак")
