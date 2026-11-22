from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3


def sqlalchemy():
    try:
        import sqlalchemy as sa
    except ImportError:
        raise ValueError("Установите зависимости из requirements-db.txt для работы с БД") from None
    return sa


def server_engine(profile, driver, password, connect_args):
    sa = sqlalchemy()
    url = sa.URL.create(driver, username=profile.user, password=password, host=profile.host,
                        port=profile.port, database=profile.database)
    return sa.create_engine(url, connect_args=connect_args, hide_parameters=True,
                            poolclass=sa.pool.NullPool)


def postgres_engine(profile, password):
    return server_engine(profile, "postgresql+psycopg", password, {"connect_timeout": 8})


def mysql_engine(profile, password):
    options = {"connect_timeout": 8, "charset": "utf8mb4"}
    if profile.host and profile.host.startswith("/"):
        options["unix_socket"] = profile.host
    return server_engine(profile, "mysql+pymysql", password, options)


def sqlite_engine(profile, password):
    sa = sqlalchemy()
    uri = Path(profile.database).as_uri() + "?mode=ro"
    return sa.create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True),
                            poolclass=sa.pool.NullPool)


@dataclass(frozen=True)
class Adapter:
    engine: Callable
    begin_read: tuple[str, ...]
    uses_password: bool = True


ADAPTERS = {
    "postgresql": Adapter(postgres_engine, ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",)),
    "mysql": Adapter(mysql_engine, ("START TRANSACTION READ ONLY",)),
    "sqlite": Adapter(sqlite_engine, ("PRAGMA query_only=ON", "BEGIN"), uses_password=False),
}


@contextmanager
def connect(profile, password=None):
    sa = sqlalchemy()
    adapter = ADAPTERS[profile.driver]
    try:
        engine = adapter.engine(profile, password)
    except ImportError:
        raise ValueError(f"Установите драйвер {profile.driver} из requirements-db.txt") from None
    try:
        with engine.connect() as conn:
            for statement in adapter.begin_read:
                conn.exec_driver_sql(statement)
            yield conn
    except sa.exc.SQLAlchemyError as exc:
        # Ошибки драйверов могут содержать значения из БД и параметры подключения.
        cause = getattr(exc, "orig", None)
        state = getattr(cause, "sqlstate", None)
        code = f" (SQLSTATE {state})" if state else ""
        raise ValueError(f"{profile.driver}: ошибка подключения или чтения{code}. "
                         "Проверьте адрес, доступ и выбранную схему") from None
    finally:
        engine.dispose()
