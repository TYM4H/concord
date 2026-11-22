from dataclasses import dataclass, field
from getpass import getpass
import json
import os
import sys

from .adapters import ADAPTERS


@dataclass(frozen=True)
class Profile:
    driver: str
    database: str
    schema: str | None = None
    table: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    routes: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path):
        return cls.parse(json.loads(path.read_text(encoding="utf-8")), path.parent)

    @classmethod
    def parse(cls, data, base_dir):
        if not isinstance(data, dict) or not isinstance(data.get("driver"), str):
            raise ValueError("Профилю нужен параметр driver")
        if data["driver"] not in ADAPTERS:
            raise ValueError("Доступные драйверы: " + ", ".join(ADAPTERS))
        if data.keys() - cls.__dataclass_fields__.keys():
            raise ValueError("Неизвестные параметры профиля. Пароль задаётся отдельно")
        if not isinstance(data.get("database"), str) or not data["database"].strip():
            raise ValueError("Профилю нужен параметр database")
        for key in ("host", "user", "table", "schema"):
            if key in data and (not isinstance(data[key], str) or not data[key].strip()):
                raise ValueError(f"Ожидалось непустое значение {key}")
        if "port" in data and (type(data["port"]) is not int or not 1 <= data["port"] <= 65535):
            raise ValueError("Некорректный порт базы данных")
        if not isinstance(data.get("routes", {}), dict):
            raise ValueError("routes должен описывать пути связанных полей")
        if data["driver"] == "sqlite":
            data = {**data, "database": str((base_dir / data["database"]).resolve())}
        return cls(**data)


def password_for(profile, side):
    if ADAPTERS[profile.driver].uses_password:
        env = f"CONCORD_{side}_PASSWORD"
        if env in os.environ:
            return os.environ[env]
        if sys.stdin.isatty():
            return getpass(f"Пароль {side} (пустой для входа без пароля): ") or None
    return None
