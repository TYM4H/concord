"""Поиск записей справа и разбор отсева по снимку выбранного запуска."""
import json

from .matching import block_eligible, values
from .rules import compare, normalize


PAGE_SIZE = 20


def exact_id_keys(query):
    """Введённый ID может обозначать строку или целое число."""
    keys = [json.dumps(query, ensure_ascii=False)]
    try:
        number = int(query)
    except ValueError:
        return keys
    if str(number) == query:
        keys.append(str(number))
    return keys


def _page_keys(db, query, offset):
    if not query:
        return db.execute("SELECT id FROM entity WHERE side=1 ORDER BY id LIMIT ? OFFSET ?",
                          (PAGE_SIZE + 1, offset)).fetchall()

    # Точный ID идёт первым, даже если его текст содержится в тысячах значений.
    keys = exact_id_keys(query)
    placeholders = ", ".join("?" for _ in keys)
    exact = db.execute(f"SELECT id FROM entity WHERE side=1 AND id IN ({placeholders}) ORDER BY id",
                       keys).fetchall()
    front = exact[offset:]
    offset = max(0, offset - len(exact))

    # Штатный lower() SQLite ограничен ASCII. Функции сохраняют тот же Unicode-
    # регистр и пробелы, что и текстовая нормализация ядра, включая строковые ID.
    db.create_function("concord_search_text", 1, lambda raw: normalize(raw, "text"), deterministic=True)
    db.create_function("concord_search_id", 1, lambda key: normalize(str(json.loads(key)), "text"),
                       deterministic=True)
    needle = normalize(query, "text")
    rows = db.execute(f"""
        SELECT e.id FROM entity e
        WHERE e.side=1 AND e.id NOT IN ({placeholders})
        AND (instr(concord_search_id(e.id), ?) > 0 OR EXISTS (
            SELECT 1 FROM observation o WHERE o.side=e.side AND o.id=e.id
            AND instr(concord_search_text(o.raw), ?) > 0
        ))
        ORDER BY e.id LIMIT ? OFFSET ?
    """, (*keys, needle, needle, PAGE_SIZE + 1 - len(front), offset))
    return front + rows.fetchall()


def search_right(db, rules, query, page=1):
    """Выдать страницу правых записей, включая уже сопоставленные в этом запуске."""
    if type(page) is not int or not 1 <= page <= (2**63 - 1) // PAGE_SIZE:
        raise ValueError("Неверный номер страницы поиска")
    if not isinstance(query, str) or len(query) > 1024:
        raise ValueError("Поисковая строка должна содержать не более 1024 символов")
    keys = _page_keys(db, query.strip(), (page - 1) * PAGE_SIZE)
    items = []
    for (key,) in keys[:PAGE_SIZE]:
        preview = []
        for field_index, raw in db.execute("""
            SELECT field, substr(raw, 1, 161) FROM observation
            WHERE side=1 AND id=? ORDER BY field, norm, raw LIMIT 3
        """, (key,)):
            visible = raw if len(raw) <= 160 else raw[:160] + "…"
            preview.append(f"{rules.fields[field_index].right}: {visible}")
        items.append({"key": key, "id": json.loads(key), "preview": preview})
    return items, len(keys) > PAGE_SIZE


def scored_miss(rules, left, right, stored_score):
    comparison = compare(rules, left, right)
    if comparison["score"] != stored_score or comparison["status"] != "below_threshold":
        raise ValueError("Балл в снимке расходится с расчётом по сохранённым правилам")
    return comparison


def explain_pair(db, rules, left_key, right_key):
    """Диагностика одной пары для непарной левой записи. Ключи — типизированные JSON-ID."""
    count, = db.execute("""
        SELECT COUNT(*) FROM entity WHERE (side=0 AND id=?) OR (side=1 AND id=?)
    """, (left_key, right_key)).fetchone()
    if count != 2:
        return None
    left = values(db, 0, left_key)
    right = values(db, 1, right_key)
    attempt = db.execute("SELECT score FROM missed WHERE left_id=? AND right_id=?",
                         (left_key, right_key)).fetchone()
    score = None
    if attempt is not None:
        score, = attempt
        fields = scored_miss(rules, left, right, score)["evidence"]
    else:
        fields = []
        for index, rule in enumerate(rules.fields):
            a, b = left.get(index, {}), right.get(index, {})
            fields.append(dict(left_path=rule.left, right_path=rule.right, mode=rule.mode,
                               weight=rule.weight, left_values=a, right_values=b,
                               shared=sorted(a.keys() & b.keys())))

    for field in fields:
        field["eligible"] = [norm for norm in field["shared"]
                             if block_eligible(field["left_values"][norm]["counts"], rules.block_limit)]
    if any(field["eligible"] for field in fields) != (attempt is not None):
        raise ValueError("Журнал сравнений расходится с результатом отбора по сохранённым частотам")
    return dict(left_id=json.loads(left_key), right_id=json.loads(right_key),
                stage="below_threshold" if attempt is not None else "not_candidate",
                score=score, fields=fields)
