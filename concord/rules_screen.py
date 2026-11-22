"""Правила сохранённого запуска и подготовка настроек следующего."""
from .conflict_form import conflict_select, conflict_value, parse_conflict
from .rules import Rules
from .view_html import html, layout


def edited_rules(rules, params):
    """Прочитать HTML-форму и проверить полученный набор общими правилами ядра."""
    def value(key):
        values = params.get(key, [])
        if len(values) != 1:
            raise ValueError("Форма правил неполна. Откройте правила запуска повторно.")
        return values[0]

    def number(key, convert=float):
        raw = value(key)
        try:
            return convert(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Проверьте числовые значения весов, порогов и лимита записей.") from exc

    fields = []
    for i, field in enumerate(rules.fields):
        mode = params.get(f"mode_{i}", [])
        if mode not in ([], ["text"]):
            raise ValueError("Неизвестный режим нормализации")
        policy = f"block_on_conflict_{i}"
        fields.append(dict(left=field.left, right=field.right, mode="text" if mode else "exact",
                           weight=number(f"weight_{i}"),
                           block_on_conflict=parse_conflict(value(policy)) if policy in params else field.block_on_conflict))
    review = number("review")
    accept = number("accept")
    block_limit = number("block_limit", int)
    return Rules.parse(dict(left_id=rules.left_id, right_id=rules.right_id, fields=fields,
                            review=review, accept=accept, block_limit=block_limit))


def rules_page(rules, params):
    normalization = []
    weights = []
    for i, field in enumerate(rules.fields):
        left, right = html(field.left), html(field.right)
        checked = " checked" if field.mode == "text" else ""
        normalization.append(f"""<label class="rule-switch-row">
            <input type="checkbox" class="rule-switch" name="mode_{i}" value="text"{checked}>
            <span><b>{left}</b><small>Нормализовать для сравнения с {right}</small></span></label>""")
        policy = conflict_select(f"block_on_conflict_{i}", conflict_value(field.block_on_conflict),
                                 f"При расхождении {field.left} и {field.right}")
        weights.append(f"""<tr><td><code>{left}</code></td><td class="rule-arrow" aria-hidden="true">→</td>
            <td><code>{right}</code></td><td><input class="rule-number" type="number" name="weight_{i}"
            aria-label="Вес для {left} и {right}" required step="any" min="0" max="100" value="{field.weight}"></td><td>{policy}</td></tr>""")

    threshold_rows = []
    for key, label, status, note in (
        ("accept", "Автосовпадение", "accepted", "При достижении порога и отсутствии блокирующих расхождений."),
        ("review", "Возможное совпадение", "review", "Нижний порог кандидата. Блокирующее расхождение оставляет пару в этой категории даже при достижении порога автосовпадения."),
    ):
        threshold_rows.append(f"""<div class="rule-threshold"><label>
            <span class="badge {status}">{label}</span><span>от <input class="rule-number" type="number"
            aria-label="Порог: {label}" name="{key}" value="{getattr(rules, key)}" required step="any" min="0" max="100"> %</span>
            </label><small>{note}</small></div>""")

    return layout(f"""<form class="rules-form" action="/new" method="get">
        <input type="hidden" name="from_rules" value="1">
        <div class="page-heading"><div><h1>Правила</h1>
        <p>Нормализация, поля и пороги выбранного запуска.</p></div>
        <button class="primary-action" type="submit">Применить к новому запуску</button></div>
        <div class="rules-grid"><div class="rule-column">
        <section><div class="rule-panel-heading"><h2>Нормализация значений</h2>
        <small>Единый регистр, совместимая форма Unicode и выравнивание пробелов.</small></div>
        {''.join(normalization)}
        <details class="rule-explanation"><summary>Как сравниваются значения</summary>
        <p>Включённый переключатель приводит обе стороны к форме Unicode NFKC и одному регистру,
        убирает крайние пробелы и заменяет пробельные последовательности одним пробелом.
        При выключенном переключателе сравнивается точное значение. Пустые значения пропускаются в обоих режимах.</p></details>
        </section>
        <section><div class="rule-panel-heading"><h2>Сопоставление полей и веса</h2>
        <small>Вес задаёт максимальный вклад поля. Итоговый балл выражается в процентах от суммы весов.</small></div>
        <div class="scroll"><table class="rule-fields"><thead><tr><th scope="col">Слева</th><th></th><th scope="col">Справа</th>
        <th scope="col">Вес</th><th scope="col">При расхождении</th></tr></thead><tbody>{''.join(weights)}</tbody></table></div></section>
        </div><div class="rule-column">
        <section class="rule-identifiers"><span class="id-marker" aria-hidden="true">ID</span>
        <h2>Идентификаторы записей</h2><small>Сохраняются вместе с каждой парой для поиска исходных строк.
        Балл рассчитывается по выбранным полям.</small>
        <dl><dt>Слева</dt><dd><code>{html(rules.left_id)}</code></dd>
        <dt>Справа</dt><dd><code>{html(rules.right_id)}</code></dd></dl></section>
        <section><div class="rule-panel-heading"><h2>Пороги решения</h2><small>Границы категорий результата.</small></div>
        {''.join(threshold_rows)}
        <div class="rule-threshold"><span class="badge unmatched">Без пары</span>
        <small>Кандидатов нет или каждый из них набрал меньше нижнего порога кандидата.</small></div></section>
        <section><div class="rule-panel-heading"><h2>Отбор кандидатов</h2></div>
        <label class="rule-block-limit"><span>Максимум записей с общим значением в каждом источнике</span>
        <input class="rule-number" type="number" name="block_limit" required min="1" step="1" value="{rules.block_limit}"></label></section>
        </div></div></form>""", active="rules")
