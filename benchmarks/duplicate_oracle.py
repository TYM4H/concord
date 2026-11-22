"""Потоковая проверка запуска с однофамильцами из набора concord_multi_01."""
import argparse
import csv
import json
import sqlite3
from collections import Counter
from contextlib import closing
from itertools import zip_longest
from pathlib import Path


GROUPS = 250_000
OFFSET = GROUPS * 4
SHARED_GROUPS = 8
LEFT_ID = "registry.people.person_id"
RIGHT_ID = "concord_multi_01.customers.customer_id"
NAME = ("registry.people.full_name", "concord_multi_01.customers.full_name")
EMAIL = ("registry.people.contacts.email", "concord_multi_01.customers.email")


def left_number(key):
    if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
        raise ValueError(f"Недопустимый левый ID {key!r}")
    n = int(key)
    if not 1 <= n <= OFFSET or key != f"{n:07d}":
        raise ValueError(f"Левый ID вне разметки {key!r}")
    return n


def expected_link(n, right):
    """Вернуть ожидания для истинной связи. Каждому правому ID назначен ровно один левый."""
    if type(right) is not int or not OFFSET < right <= OFFSET * 2:
        raise ValueError(f"Правый ID вне разметки {right!r}")
    index = right - OFFSET
    group, slot = divmod(index - 1, 4)
    if n != (index - 1 if slot == 1 else index):
        raise ValueError(f"Лишняя связь {n:07d} → {right}")
    if slot < 2:
        return "review", 66.82, "equal"  # 70 / √2 + 30 / √3
    if slot == 2 or group < SHARED_GROUPS:
        return "review", 70.0, "different"
    return "accepted", 100.0, "equal"


def check_evidence(evidence, email_outcome, n, right):
    outcomes = {(f["left_path"], f["right_path"]): f["outcome"] for f in evidence}
    if len(evidence) != 2 or outcomes != {NAME: "equal", EMAIL: email_outcome}:
        raise ValueError(f"Неверные основания связи {n:07d} → {right}")


def read_results(path):
    linked = bytearray(OFFSET + 1)
    unmatched = bytearray(OFFSET + 1)
    statuses = Counter()
    with path.open("rb") as stream:
        header = json.loads(next(stream))
        rules = header["rules"]
        fields = {(f["left"], f["right"]): (f["mode"], f["weight"]) for f in rules["fields"]}
        if ((rules["left_id"], rules["right_id"]) != (LEFT_ID, RIGHT_ID) or
                (rules["review"], rules["accept"]) != (65, 85) or
                fields != {NAME: ("text", 70), EMAIL: ("exact", 30)}):
            raise ValueError("Правила запуска отличаются от контрольной конфигурации")
        for line in stream:
            row = json.loads(line)
            n = left_number(row["left_id"])
            right = row["right_id"]
            status = row["status"]
            if status == "unmatched":
                if n % 4 != 2 or right is not None or unmatched[n] or row["reason"] != "below_review_threshold":
                    raise ValueError(f"Неожиданная запись без пары {n:07d} → {right}")
                best = row["best_comparison"]
                if (best["left_id"] != row["left_id"] or best["right_id"] not in (OFFSET + n - 1, OFFSET + n)
                        or best["score"] != 49.5 or best["status"] != "below_threshold"):
                    raise ValueError(f"Неверный лучший кандидат для {n:07d}")
                unmatched[n] = 1
            else:
                expected_status, score, email_outcome = expected_link(n, right)
                if linked[right - OFFSET] or (status, row["score"]) != (expected_status, score):
                    raise ValueError(f"Повтор связи или неверная оценка {n:07d} → {right}")
                check_evidence(row["evidence"], email_outcome, n, right)
                linked[right - OFFSET] = 1
            statuses[status] += 1
    if sum(linked) != OFFSET or sum(unmatched) != GROUPS:
        raise ValueError(f"Неполное покрытие: связей {sum(linked)}, без пары {sum(unmatched)}")
    return header, statuses


def expected_misses():
    """Отсеянные сравнения в порядке составного ключа missed."""
    for group in range(GROUPS):
        base = 4 * group
        first = f'"{base + 1:07d}"'
        yield first, str(OFFSET + base + 3), 17.32  # Одна общая почта на трёх правых ID.
        second = f'"{base + 2:07d}"'
        namesakes = [(OFFSET + base + 1, 49.5), (OFFSET + base + 2, 49.5)]
        if group < SHARED_GROUPS:
            namesakes.extend((OFFSET + 4 * j + 4, 10.61) for j in range(SHARED_GROUPS))
            namesakes.sort()
        for right, score in namesakes:
            yield second, str(right), score


def read_misses(path):
    missed = 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT left_id, right_id, score FROM missed ORDER BY left_id, right_id")
        for actual, expected in zip_longest(rows, expected_misses()):
            if actual != expected:
                raise ValueError(f"Расхождение журнала сравнений: получено {actual}, ожидалось {expected}")
            missed += 1
    return missed


def read_csv(path):
    seen = bytearray(OFFSET + 1)
    bases = Counter()
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            n = left_number(row["left_id"])
            right = int(row["right_id"])
            status, score, email_outcome = expected_link(n, right)
            if row["right_id"] != str(right) or (row["left_id_type"], row["right_id_type"]) != ("string", "integer"):
                raise ValueError(f"Некорректный тип ID в CSV {n:07d} → {right}")
            if status == "review" and n != 1:
                raise ValueError(f"В CSV попала неподтверждённая связь {n:07d} → {right}")
            basis = "analyst_confirmed" if status == "review" else "automatic"
            if ((row["auto_status"], row["acceptance_basis"], float(row["score"])) != (status, basis, score)
                    or (row["left_id_path"], row["right_id_path"]) != (LEFT_ID, RIGHT_ID)
                    or bool(row["decided_at"]) != (basis == "analyst_confirmed") or seen[right - OFFSET]):
                raise ValueError(f"Ошибка статуса, путей или повтор в CSV {n:07d} → {right}")
            check_evidence(json.loads(row["evidence_json"]), email_outcome, n, right)
            bases[basis] += 1
            seen[right - OFFSET] = 1
    if bases != {"analyst_confirmed": 2, "automatic": GROUPS - SHARED_GROUPS}:
        raise ValueError(f"В CSV пропущены связи: {bases}")
    return bases


def verify(folder):
    folder = Path(folder)
    run, = folder.glob("run-*.jsonl")
    header, statuses = read_results(run)
    snapshot = folder / f"concord-diagnostics-{header['diagnostics']['id']}.sqlite3"
    rejected_by_threshold = read_misses(snapshot)
    csv_bases = read_csv(folder / "accepted.csv")
    retained = statuses["accepted"] + statuses["review"]
    return dict(left_records=GROUPS * 4, right_records=GROUPS * 4,
                comparisons=retained + rejected_by_threshold, true_links=retained,
                missing_true_links=0, extra_retained_links=0, below_threshold=rejected_by_threshold,
                statuses=dict(statuses), csv_rows=sum(csv_bases.values()), csv_bases=dict(csv_bases))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", type=Path)
    print(json.dumps(verify(ap.parse_args().folder), ensure_ascii=False, indent=2))
