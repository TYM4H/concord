"""Потоковая CSV-выгрузка принятых связей одного запуска."""
from contextlib import closing
import csv
from io import StringIO
import json


CSV_COLUMNS = (
    "left_id", "right_id", "left_id_type", "right_id_type",
    "auto_status", "auto_reason", "score", "acceptance_basis", "analyst_note",
    "decided_at", "evidence_json", "left_id_path", "right_id_path",
)


def csv_chunks(db, rules):
    """Выдать CSV в UTF-8. Один SELECT фиксирует решения на время выгрузки."""
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    columns = """
        json_extract(r.body, '$.left_id', '$.right_id', '$.reason', '$.score'),
        json_extract(r.body, '$.evidence'), r.status
    """
    # Оба индекса отдают номера по порядку, SQLite сливает два потока.
    query = f"""
        SELECT r.n AS n, {columns}, NULL, NULL FROM result r WHERE r.status = 'accepted'
        UNION ALL
        SELECT d.n AS n, {columns}, d.note, d.updated_at
        FROM decision d CROSS JOIN result r ON r.n = d.n
        WHERE d.action = 'confirmed' AND r.status = 'review'
        ORDER BY n
    """
    with closing(db.execute(query)) as records:
        for _, metadata, evidence, status, note, decided_at in records:
            # Короткий JSON-массив сохраняет типы и точность ID за пределами int64.
            left, right, reason, score = json.loads(metadata)
            basis = "automatic" if status == "accepted" else "analyst_confirmed"
            writer.writerow((
                left, right, "string" if isinstance(left, str) else "integer",
                "string" if isinstance(right, str) else "integer",
                status, reason, score, basis, note, decided_at, evidence,
                rules.left_id, rules.right_id,
            ))
            if output.tell() >= 65536:
                yield output.getvalue().encode("utf-8")
                output.seek(0)
                output.truncate()
    if output.tell():
        yield output.getvalue().encode("utf-8")
