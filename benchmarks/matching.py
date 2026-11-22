"""Проверка дискового индекса на редком и частом признаке. Запуск из корня проекта."""
from argparse import ArgumentParser
from time import perf_counter

from concord.matching import match
from concord.rules import Rules


def main():
    parser = ArgumentParser(description="Синтетическая проверка ядра Concord")
    parser.add_argument("--rows", type=int, default=100, help="Число записей с каждой стороны")
    count = parser.parse_args().rows
    if count < 1:
        parser.error("--rows должен быть положительным")
    rules = Rules.parse(dict(left_id="id", right_id="key", fields=[
        dict(left="code", right="code", mode="exact", weight=90),
        dict(left="city", right="city", mode="text", weight=10),
    ]))
    left = ({"id": i, "code": f"C{i}", "city": "Город"} for i in range(count))
    right = ({"key": count + i, "code": f"C{i}", "city": "Город"} for i in range(count))
    started = perf_counter()
    accepted = 0
    for item in match(left, right, rules):
        if item["status"] != "accepted" or item["right_id"] != count + item["left_id"]:
            raise RuntimeError("Получена неожиданная пара")
        accepted += 1
    if accepted != count:
        raise RuntimeError("Количество пар не совпало с ожидаемым")
    print(f"Записей с каждой стороны: {count}. Автоматически принято пар: {accepted}. "
          f"Время ядра: {perf_counter() - started:.2f} с.")


if __name__ == "__main__":
    main()
