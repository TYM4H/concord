import argparse
import json
from pathlib import Path

from .adapters import connect
from .profile import Profile, password_for
from .reader import catalog


def main():
    parser = argparse.ArgumentParser(description="Посмотреть схему, таблицы и связи источника Concord")
    parser.add_argument("profile", type=Path, help="Профиль подключения в JSON")
    try:
        profile = Profile.load(parser.parse_args().profile)
        with connect(profile, password_for(profile, "SOURCE")) as conn:
            print(json.dumps(catalog(conn, profile.schema), ensure_ascii=False, indent=2))
    except (ValueError, OSError, EOFError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
