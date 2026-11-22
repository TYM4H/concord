"""Прогон через HTTP-формы Concord на базах из benchmarks/fixtures."""
from argparse import ArgumentParser
import csv
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from time import perf_counter
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen


class Inputs(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.fields = {}
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and "name" in values:
            self.fields.setdefault(values["name"], []).append(values.get("value", ""))


def check_duplicate_views(page, run_path):
    """Проверить ID в заголовках таблиц сравнения и переход ко второй странице."""
    def right_ids(body):
        return [int(key) for key in re.findall(
            r'<th scope="col"[^>]*><small>[^<]*</small>\s*<code>(\d+)</code></th>', body)]

    body, _ = page(run_path + "compare?n=1")
    if (right_ids(body) != [1000001, 1000002, 1000003] or body.count("66.82%") != 2
            or "17.32%" not in body or "Ниже порога" not in body or body.count("Подтверждено</strong>") != 2):
        raise ValueError("На экране сравнения нет полного набора дублей и отсеянного кандидата")
    diagnosis, _ = page(run_path + "diagnose?n=3")
    if run_path + "compare?n=3" not in diagnosis:
        raise ValueError("Не найден переход от записи без пары ко всем её кандидатам")
    first, _ = page(run_path + "compare?n=3")
    if run_path + "compare?n=3&amp;page=2" not in first:
        raise ValueError("Нет перехода на следующую страницу вариантов")
    second, _ = page(run_path + "compare?n=3&page=2")
    expected = [1000001, 1000002, *range(1000004, 1000033, 4)]
    if right_ids(first) + right_ids(second) != expected:
        raise ValueError("При перелистывании страницы потеряны или повторены кандидаты")


def workflow(base, profiles, folder, *, duplicates=False):
    elapsed = {}

    def response(path, fields=None):
        data = urlencode(fields, doseq=True).encode() if fields is not None else None
        return urlopen(base + path, data, timeout=1800)

    def page(path, fields=None):
        with response(path, fields) as reply:
            return reply.read().decode(), urlparse(reply.url).path

    def finish(label, start):
        elapsed[label] = perf_counter() - start
        print(f"{label}: {elapsed[label]:.3f} s", flush=True)

    start = perf_counter()
    body, _ = page("/sources/connect")
    token = Inputs(body).fields["token"][0]
    params = dict(token=token, review=65, accept=85, block_limit=64)
    for side, key in (("left", "person_id"), ("right", "customer_id")):
        path = profiles[side].resolve(strict=True)
        info = json.loads(path.read_text())
        body, _ = page("/sources/connect", dict(mode="profile", side=side, profile=path, token=token))
        if "Подключение проверено" not in body:
            raise ValueError("Не удалось проверить " + side)
        field = ".".join((info["schema"], info["table"], key))
        params.update({side + "_path": path, side + "_kind": "profile", side + "_id": field})
    finish("connections", start)
    start = perf_counter()
    suggested, _ = page("/new/suggest", params)
    form = Inputs(suggested).fields
    choices = []
    for choice in form["choice"]:
        i, field = json.loads(choice)
        if field["left"].endswith(".contacts.email") and field["right"].endswith(".email"):
            mode, weight = "exact", 30
        elif field["left"].endswith(".full_name") and field["right"].endswith(".full_name"):
            mode, weight = "text", 70
        else:
            continue
        form["mode_" + str(i)] = mode
        form["weight_" + str(i)] = weight
        choices.append(choice)
    if len(choices) != 2:
        raise ValueError("Ожидались предложения для имени и почты")
    form["choice"] = choices
    finish("suggestions", start)
    start = perf_counter()
    _, run_path = page("/new", form)
    if not run_path.startswith("/runs/"):
        raise ValueError("Запуск не завершён")
    finish("submit_to_results", start)
    start = perf_counter()
    for number in ((1, 2) if duplicates else (2,)):
        review, _ = page(run_path + f"compare?n={number}")
        left_id = "0000001" if duplicates else "0000002"
        if left_id not in review or str(1_000_000 + number) not in review:
            raise ValueError("В очереди отсутствует контрольная пара")
        fields = Inputs(review).fields
        index = fields["n"].index(str(number))
        decision = dict(n=number, token=fields["token"][index], revision=fields["revision"][index],
                        action="confirmed", note="Подтверждено по контрольной разметке")
        page(run_path + "decision", decision)
    finish("review", start)
    csv_path = folder / "accepted.csv"
    start = perf_counter()
    with response(run_path + "accepted.csv") as source, csv_path.open("xb") as dest:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            dest.write(chunk)
    finish("csv_http_to_file", start)
    manifest = dict(times=elapsed, run=run_path, bytes=csv_path.stat().st_size)
    if duplicates:
        from benchmarks.duplicate_oracle import verify
        manifest["validation"] = verify(folder)
        check_duplicate_views(page, run_path)
    else:
        check_csv(csv_path)
        result_path, = folder.glob("run-*.jsonl")
        manifest["result_rows_sha256"] = check_results(result_path)
    (folder / "workflow.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    return manifest


def check_results(path):
    """Проверить все четыре группы контрольной разметки и обе стороны без пары."""
    left_seen, right_seen = bytearray(1_000_001), bytearray(1_000_001)
    digest = sha256()
    with path.open("rb") as stream:
        next(stream)  # Заголовок содержит индивидуальный идентификатор снимка.
        for line in stream:
            digest.update(line)
            row = json.loads(line)
            left, right = row["left_id"], row["right_id"]
            n = int(left) if left is not None else right - 1_000_000
            if not 1 <= n <= 1_000_000:
                raise ValueError(f"ID вне контрольного набора: {n}")
            if left is not None:
                if left != f"{n:07d}" or left_seen[n]:
                    raise ValueError(f"Неверный или повторный левый ID: {left}")
                left_seen[n] = 1
            if right is not None:
                if right != 1_000_000 + n or right_seen[n]:
                    raise ValueError(f"Неверный или повторный правый ID: {right}")
                right_seen[n] = 1
            group = n % 4
            if left is None:
                valid = group in (0, 3) and (row["status"], row["reason"]) == ("unmatched", "no_retained_pair")
            elif group in (1, 2):
                expected = ("accepted", 100) if group == 1 else ("review", 70)
                valid = right is not None and (row["status"], row.get("score")) == expected
            else:
                best = row.get("best_comparison")
                if group == 3:
                    valid = (row["reason"] == "below_review_threshold" and best and
                             (best["left_id"], best["right_id"], best["score"]) == (left, n + 1_000_000, 30))
                else:
                    valid = row["reason"] == "no_block_candidate" and best is None
                valid = valid and right is None and row["status"] == "unmatched"
            if not valid:
                raise ValueError(f"Неверный результат для {n}: {row['reason']}")
    if sum(left_seen) != 1_000_000 or sum(right_seen) != 1_000_000:
        raise ValueError("В результатах отсутствуют исходные ID")
    return digest.hexdigest()


def check_csv(path):
    # Проверяем каждую пару, типы ID, основание принятия и пути доказательств.
    numbers = set()
    with path.open(encoding="utf8", newline="") as stream:
        for row in csv.DictReader(stream):
            n = int(row["left_id"])
            status, basis, score = ("review", "analyst_confirmed", 70) if n == 2 else ("accepted", "automatic", 100)
            if (not 1 <= n <= 1_000_000 or (n % 4 != 1 and n != 2) or n in numbers or
                    row["left_id"] != f"{n:07d}" or int(row["right_id"]) != 1_000_000 + n or
                    (row["left_id_type"], row["right_id_type"]) != ("string", "integer") or
                    (row["auto_status"], row["acceptance_basis"], float(row["score"])) != (status, basis, score)):
                raise ValueError(f"Неожиданная связь в CSV: {n}")
            evidence = {f["left_path"]: f["outcome"] for f in json.loads(row["evidence_json"])}
            if evidence != {"registry.people.full_name": "equal",
                            "registry.people.contacts.email": "different" if n == 2 else "equal"}:
                raise ValueError(f"Неожиданное основание: {n}")
            numbers.add(n)
    if len(numbers) != 250_001:
        raise ValueError(f"Получено {len(numbers)} связей")


if __name__ == "__main__":
    ap = ArgumentParser(description=__doc__)
    ap.add_argument("url", help="Адрес измеряемого локального просмотрщика")
    ap.add_argument("left", type=Path)
    ap.add_argument("right", type=Path)
    ap.add_argument("folder", type=Path, help="Каталог артефактов этого прогона")
    ap.add_argument("--duplicates", action="store_true", help="Набор с однофамильцами и дублями")
    args = ap.parse_args()
    if urlparse(args.url).hostname not in ("localhost", "127.0.0.1"):
        ap.error("Для измерения нужен локальный просмотрщик")
    print(workflow(args.url.rstrip("/"), {"left": args.left, "right": args.right}, args.folder,
                   duplicates=args.duplicates))
