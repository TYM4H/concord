"""Предложить поля по двум выбранным таблицам и идентификаторам."""
import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from ..sources.adapters import connect
from ..sources.profile import Profile, password_for
from .ranking import suggest_fields
from .review import choose_fields
from .sample import sample_fields
from .selection import make_draft


def display(report, *, numbered=False):
    for key, label in (("left", "Слева"), ("right", "Справа")):
        side = report[key]
        print(f"{label}: {side['id']}, ID в выборке: {side['sampled_entities']}, "
              f"обследовано полей: {side['examined_fields']}.")
        for message in side["warnings"]:
            print(f"  {message}")
    if not report["suggestions"]:
        print("Подходящих пар полей в выборках не найдено.")
    for number, pair in enumerate(report["suggestions"], start=1):
        prefix = f"{number}. " if numbered else ""
        print(f"\n{prefix}{pair['left']['path']} ↔ {pair['right']['path']}")
        for key, label in (("left", "Слева"), ("right", "Справа")):
            side = pair[key]
            examples = json.dumps(side["examples"], ensure_ascii=False)
            print(f"  {label} ({side['type']}): {examples}. "
                  f"ID со значениями: {side['populated_entities']}/{side['sampled_entities']}, "
                  f"различных значений: {side['distinct_values']}.")
            for hop in side["via"]:
                print(f"    {', '.join(hop['from'])} → {hop['table']} ({', '.join(hop['to'])})")
        print("  " + " ".join(pair["reasons"]))


def save_draft(destination, draft):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent) as stream:
        json.dump(draft, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        try:
            os.link(stream.name, destination)
        except FileExistsError:
            raise FileExistsError(f"Файл уже существует: {destination}") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ("left", "right"):
        parser.add_argument(f"--{side}-source", type=Path, required=True, help="JSON-профиль с выбранной таблицей")
        parser.add_argument(f"--{side}-id", required=True, help="Колонка ID в выбранной таблице")
    parser.add_argument("--sample-size", type=int, default=100, help="Число ID на сторону, 1–500")
    parser.add_argument("--max-hops", type=int, default=2, help="Глубина обхода внешних ключей, 0–3")
    parser.add_argument("--json", action="store_true", help="Вывести структурированный отчёт")
    parser.add_argument("--draft", type=Path, help="Создать новый JSON-файл для выбора пар полей")
    parser.add_argument("--choose", action="store_true", help="Выбрать номера предложений для сохранения в --draft")
    args = parser.parse_args()
    if args.choose and (args.draft is None or args.json):
        parser.error("--choose требует --draft и текстовый вывод (без --json)")
    try:
        if args.draft is not None and args.draft.exists():
            raise FileExistsError(f"Файл уже существует: {args.draft}")
        samples = []
        for side in ("left", "right"):
            profile = Profile.load(getattr(args, side + "_source"))
            with connect(profile, password_for(profile, side.upper())) as conn:
                samples.append(sample_fields(conn, profile, getattr(args, side + "_id"),
                                             sample_size=args.sample_size, max_hops=args.max_hops))
        report = suggest_fields(*samples)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            display(report, numbered=args.choose)
        if args.draft is not None:
            draft = make_draft(report)
            if args.choose:
                if not report["suggestions"]:
                    raise ValueError("Нечего выбрать для запуска")
                answer = input("Введите номера нужных пар через запятую: ")
                draft = choose_fields(draft, answer)
            save_draft(args.draft, draft)
            if args.choose:
                print(f"Выбор сохранён в {args.draft}.", file=sys.stderr)
            else:
                print(f"Черновик сохранён в {args.draft}. Для нужных пар установите enabled в true.",
                      file=sys.stderr)
    except EOFError:
        parser.exit(1, "Ошибка: ввод завершён до получения ответа.\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
