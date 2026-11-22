"""Постраничный просмотр локального результата в браузере."""
import argparse
from contextlib import closing
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
from urllib.parse import parse_qs, urlencode, urlsplit

from .diagnostic_screen import diagnosis_page
from .diagnostics import exact_id_keys
from .workspace import FirstRun, Workspace, run_key
from .dashboard import history_page, overview_page
from .new_screen import create_run, new_page
from .source_screen import sources_page
from .wizard_state import restore_draft, start_connection
from .rules_screen import edited_rules, rules_page
from .threshold_screen import threshold_page
from .scores_screen import scores_page, missed_pair_page
from .rules import Rules
from .review_store import ReviewConflict, load_run, set_decision
from .review_export import csv_chunks
from .review_screen import MANUAL_LABELS, comparison_page
from .view_html import LABELS, REASONS, badge, html, identifier, layout, outcome_label, variants

PAGE_SIZE = 20


def record_cell(item, side, snapshot):
    key = item[side + "_id"]
    if key is None:
        return "<td>—</td>"
    text = None
    if snapshot is not None:
        row = snapshot.execute("SELECT raw FROM observation WHERE side=? AND id=? ORDER BY field,norm,raw LIMIT 1",
                               (int(side == "right"), json.dumps(key, ensure_ascii=False))).fetchone()
        if row:
            text = row[0]
    elif item.get("evidence"):
        for field in item["evidence"]:
            values = field.get(side + "_values", {})
            if values:
                text = next(iter(values.values()))["originals"][0]
                break
    if text is None:
        return f"<td>{identifier(key)}</td>"
    preview = str(text)
    if len(preview) > 90:
        preview = preview[:89] + "…"
    return f'<td class="record-cell"><strong>{html(preview)}</strong><small>ID · <code>{identifier(key)}</code></small></td>'


def index_page(db, rules, counts, params, snapshot=None):
    status = params.get("status", [""])[0]
    if status and status not in LABELS:
        raise ValueError
    state = params.get("state", [""])[0]
    if state and (status != "review" or state not in MANUAL_LABELS):
        raise ValueError("Неизвестный фильтр решений")
    side = params.get("side", [""])[0]
    left_id = params.get("left_id", [""])[0]
    query_id = params.get("id", [""])[0]
    minimum = params.get("min_score", [""])[0]
    sort = params.get("sort", ["score" if status == "review" else "original"])[0]
    if sort not in ("original", "score"):
        raise ValueError("Неизвестный порядок сортировки")
    if minimum not in ("", "50", "75", "90"):
        raise ValueError("Неизвестный фильтр сходства")
    if side and (status != "unmatched" or side not in ("left", "right")):
        raise ValueError("Неизвестная сторона источника")
    if left_id and side != "left":
        raise ValueError("Для поиска ID выберите записи слева")
    decisions = dict(db.execute("SELECT action, COUNT(*) FROM decision GROUP BY action"))
    decisions["pending"] = counts["review"] - sum(decisions.values())
    total = decisions.get(state, 0) if state else (counts[status] if status else sum(counts.values()))
    conditions, args = [], []
    if status:
        conditions.append("s.status=?" if sort == "score" else "r.status=?")
        args.append(status)
    if state == "pending":
        conditions.append("d.n IS NULL")
    elif state:
        conditions.append("d.action=?")
        args.append(state)
    if side:
        conditions.append("r.left_key IS NOT NULL" if side == "left" else "r.left_key IS NULL")
    if query_id:
        keys = exact_id_keys(query_id)
        marks = ",".join("?" for _ in keys)
        conditions.append(f"(r.left_key IN ({marks}) OR json_quote(json_extract(r.body,'$.right_id')) IN ({marks}))")
        args.extend(keys + keys)
    if minimum:
        conditions.append("s.score >= ?")
        args.append(float(minimum))
    if left_id:
        keys = exact_id_keys(left_id)
        conditions.append("r.left_key IN (" + ",".join("?" for _ in keys) + ")")
        args.extend(keys)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    source = "result r"
    if sort == "score":
        # Ведущий индекс по баллу сразу отдаёт одну страницу в нужном порядке.
        source = "pair_score s CROSS JOIN result r ON r.n=s.n"
    elif minimum:
        source += " JOIN pair_score s ON s.n=r.n"
    source += " LEFT JOIN decision d ON d.n=r.n"
    if side or query_id or minimum:
        total = db.execute("SELECT COUNT(*) FROM " + source + where, args).fetchone()[0]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(pages, max(1, int(params.get("page", ["1"])[0])))
    offset = (page - 1) * PAGE_SIZE
    order = "s.score DESC, s.n ASC" if sort == "score" else "r.n"
    records = db.execute("SELECT r.n, r.body, d.action FROM " + source + where
                         + f" ORDER BY {order} LIMIT ? OFFSET ?", [*args, PAGE_SIZE, offset])
    cards = "".join(f'<div class="card {s}"><b>{counts[s]:,}</b>{label}</div>'.replace(",", " ")
                    for s, label in zip(LABELS, ("Автосовпадений", "Возможных совпадений", "Без пары")))
    sort_arg = "&amp;sort=" + sort if "sort" in params else ""
    tabs = "".join(f'<a class="{"selected" if key == status else ""}" href="/?status={key}{sort_arg}">{label}</a>'
                   for key, label in {"": "Все", **LABELS}.items())
    filters = ""
    if status == "review":
        filters = '<div class="tabs" aria-label="Решение аналитика">' + "".join(
            f'<a class="{"selected" if key == state else ""}" href="/?status=review&amp;state={key}{sort_arg}">'
            f'{label} ({decisions.get(key, 0) if key else counts["review"]})</a>'
            for key, label in {"": "Все решения", **MANUAL_LABELS}.items()) + "</div>"
    elif status == "unmatched":
        filters = '<div class="tabs" aria-label="Источник">' + "".join(
            f'<a class="{"selected" if key == side else ""}" href="/?status=unmatched&amp;side={key}{sort_arg}">{label}</a>'
            for key, label in (("", "Оба источника"), ("left", "Записи слева"), ("right", "Записи справа"))
        ) + "</div>"
        if side == "left":
            filters += f"""<form class="diagnostic-search" action="/" method="get">
                <input type="hidden" name="status" value="unmatched"><input type="hidden" name="side" value="left">
                <label for="left_id">Точный ID слева</label>
                <div><input type="search" id="left_id" name="left_id" value="{html(left_id)}">
                <button type="submit">Найти</button></div></form>"""
    rows = []
    for n, body, action in records:
        item = json.loads(body)
        score = f'{item["score"]:g}%' if "score" in item else "—"
        if "score" in item:
            score += f' <progress aria-hidden="true" class="match-score" max="100" value="{float(item["score"]):g}"></progress>'
        manual = html(MANUAL_LABELS[action or "pending"]) if item["status"] == "review" else "—"
        if item["status"] in ("review", "accepted"):
            link = f'<a href="/compare?n={n}">Сравнить</a>'
        elif item["left_id"] is not None:
            link = f'<a href="/diagnose?n={n}">Разобрать</a>'
        else:
            link = f'<a href="/pair?n={n}">Открыть</a>'
        rows.append(f'<tr>{record_cell(item, "left", snapshot)}{record_cell(item, "right", snapshot)}'
                    f'<td>{score}</td><td>{badge(item["status"])}</td>'
                    f'<td>{manual}</td><td>{link}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="6">В этой выборке нет записей.</td></tr>')
    links = []
    for p, label in ((page - 1, "Назад"), (page + 1, "Далее")):
        if 1 <= p <= pages:
            query = urlencode(dict(status=status, state=state, side=side, left_id=left_id, id=query_id, min_score=minimum, sort=sort, page=p))
            links.append(f'<a href="/?{html(query)}">{label}</a>')
    options = "".join(f'<option value="{v}" {"selected" if minimum == v else ""}>{label}</option>'
                      for v,label in (("", "Сходство: любое"), ("50", "От 50%"), ("75", "От 75%"), ("90", "От 90%")))
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html(v)}">' for k,v in
                     (("status",status),("state",state),("side",side),("left_id",left_id)) if v)
    return layout(f"""<div class="page-heading"><div><h1>Сопоставление</h1><p>Результаты локального запуска</p></div>
        <a class="primary-action" href="/new">Новое сопоставление</a></div>
        <div class="sources"><div><small>Левый источник</small><code>{html(rules.left_id)}</code></div>
        <b>→</b><div><small>Правый источник</small><code>{html(rules.right_id)}</code></div></div>
        <div class="cards">{cards}</div>
        <div class="review-tools"><a href="/threshold">Настроить порог автосовпадения</a>
        <a href="/scores">Все оценённые пары по убыванию балла</a>
        <a href="/accepted.csv" download="concord-accepted.csv">Скачать принятые пары · CSV</a></div><section>
        <div class="match-toolbar"><div class="tabs">{tabs}</div><form class="match-filter" action="/">{hidden}
        <select aria-label="Минимальное сходство" name="min_score">{options}</select>
        <select aria-label="Сортировка" name="sort">
        <option value="original" {"selected" if sort == "original" else ""}>По порядку запуска</option>
        <option value="score" {"selected" if sort == "score" else ""}>По убыванию балла</option></select>
        <input type="search" aria-label="Точный ID записи" name="id" value="{html(query_id)}" placeholder="Точный ID записи">
        <button>Показать</button></form></div>{filters}<div class="scroll">
        <table><thead><tr><th>Запись слева</th><th>Запись справа</th><th>Сходство</th><th>Статус алгоритма</th><th>Решение аналитика</th><th></th>
        </tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        <footer>Показано {min(total, offset + 1)}–{min(total, offset + PAGE_SIZE)} из {total}
        <div>{' &nbsp; '.join(links)}</div></footer></section>""")


def detail_page(db, rules, n):
    record = db.execute("SELECT body FROM result WHERE n=?", (n,)).fetchone()
    if not record:
        return None
    item = json.loads(record[0])
    comparison = item.get("best_comparison") or item
    info = f"""<a href="/">← Сопоставление</a><h1>Основания решения</h1>
        <p>{html(rules.left_id)} = {identifier(item["left_id"])} &nbsp; ↔ &nbsp;
        {html(rules.right_id)} = {identifier(item["right_id"])}</p>{badge(item["status"])}
        <p>{html(REASONS[item["reason"]])}</p>"""
    if item["status"] in ("review", "accepted"):
        info += f'<p><a href="/compare?n={n}">Сравнить все варианты для этой записи</a></p>'
    elif item["left_id"] is not None:
        info += f'<p><a href="/diagnose?n={n}">Найти запись справа и разобрать пару</a></p>'
    if item["status"] == "review":
        decision = db.execute("SELECT action,note,updated_at FROM decision WHERE n=?", (n,)).fetchone()
        if decision:
            action, note, updated_at = decision
            info += f'<p>Решение аналитика — {html(MANUAL_LABELS[action])}. {html(updated_at)}<br>{html(note)}</p>'
        else:
            info += f'<p>Решение аналитика — {html(MANUAL_LABELS["pending"])}.</p>'
    if item.get("best_comparison"):
        info += f'<p>Ближайшая проверенная запись справа: {identifier(comparison["right_id"])}</p>'
    if "score" in comparison:
        info += f'<p>Сходство {comparison["score"]:g}%. Нижний порог кандидата — {rules.review:g}%, порог автосовпадения — {rules.accept:g}%.</p>'
    for field in comparison.get("evidence", []):
        info += f"""<section class="field"><strong>{html(field["left_path"])} ↔ {html(field["right_path"])}</strong>
            <div class="values"><div>{variants(field["left_values"])}</div><div>{variants(field["right_values"])}</div></div>
            <small>{outcome_label(field)} · {html(field["mode"])} ·
            Учтённое значение: {identifier(field["used_value"])} · Вклад {field["points"]:.2f} из {field["weight"]:g}</small></section>"""
    return layout(info)


def scoped_html(body, prefix):
    if not prefix:
        return body
    return re.sub(r'((?:href|action)="/)(?!runs/|style\.css")',
                  lambda match: match.group(1)[:-1] + prefix + "/", body)


def serve(workspace, port):
    # Браузер может заранее открыть пустой сокет при переходе или скачивании.
    # Обрабатываем такие соединения независимо, сериализуя обращения к Workspace.
    workspace_lock = Lock()
    css = "\n".join(Path(__file__).with_name(name).read_text(encoding="utf-8") for name in
                    ("viewer.css", "dashboard.css", "source_screen.css", "rules_screen.css"))
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        timeout = 15
        prefix = ""

        def select_run(self, path):
            if path.startswith("/runs/"):
                match = re.fullmatch(r"/runs/([a-f0-9]{24})(/.*)", path)
                if not match:
                    return None, path
                self.prefix = "/runs/" + match[1]
                return workspace.by_key(match[1]), match[2]
            run = workspace.open(workspace.initial) if workspace.initial else FirstRun()
            return run, path

        def log_request(self, code="-", size="-"):
            # В параметрах поиска могут быть имена и контакты из источников.
            path = getattr(self, "path", "").partition("?")[0]
            self.log_message("%s %s %s %s", self.command, path, code, size)

        def local_request(self):
            if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return True
            self.send_error(HTTPStatus.FORBIDDEN)
            return False

        def headers_for(self, status, mime):
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")

        def page(self, body, status=HTTPStatus.OK, mime="text/html"):
            if mime == "text/html":
                body = scoped_html(body, self.prefix)
            data = body.encode()
            self.headers_for(status, mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            with workspace_lock:
                self.get_page()

        def get_page(self):
            if not self.local_request():
                return
            url = urlsplit(self.path)
            params = parse_qs(url.query)
            try:
                run, route = self.select_run(url.path)
                if run is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                rules = run.rules
                if run.path:
                    db, counts = run.db, run.counts
                if run.path is None and route in ("/", "/sources", "/rules", "/overview"):
                    active, title, description, action, link = {
                        "/": ("matching", "Сопоставление", "Создайте первый запуск для сравнения двух источников.", "Новое сопоставление", "/new"),
                        "/sources": ("sources", "Источники данных", "Подключите базу, чтобы посмотреть таблицы и выбрать источник.", "Подключить источник", "/sources/connect"),
                        "/rules": ("rules", "Правила", "Выберите источники и задайте признаки, веса и пороги первого запуска.", "Настроить сопоставление", "/new"),
                        "/overview": ("overview", "Обзор", "В этом рабочем пространстве пока нет запусков.", "Начать сопоставление", "/new"),
                    }[route]
                    body = layout(f'<h1>{title}</h1><p>{description}</p><a class="primary-action" href="{link}">{action}</a>', active)
                elif route == "/overview":
                    pending = counts["review"] - db.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
                    body = overview_page(workspace.listing(), run_key(run.path), counts, pending)
                elif route == "/history":
                    body = history_page(workspace.listing(), run_key(run.path) if run.path else None, params)
                elif route == "/sources":
                    body = sources_page(rules, run.header, run.snapshot, params)
                elif route == "/sources/connect":
                    from .source_screen import connect_page
                    body = connect_page(params, token)
                elif route == "/rules":
                    body = rules_page(rules, params)
                elif route == "/new":
                    draft = edited_rules(rules, params) if "from_rules" in params else rules
                    if "from_threshold" in params:
                        if not run.path:
                            raise ValueError("Откройте настройку порога этого запуска")
                        proposed = params.get("accept", [])
                        if len(proposed) != 1:
                            raise ValueError("Укажите один порог автосовпадения")
                        draft = Rules.parse({**asdict(rules), "accept": float(proposed[0])})
                    fields = {}
                    for side in ("left", "right"):
                        if side + "_profile" in params:
                            fields[side + "_kind"] = ["profile"]
                            fields[side + "_path"] = params[side + "_profile"]
                            if side + "_id" in params:
                                fields[side + "_id"] = params[side + "_id"]
                    body = new_page(run, draft, token, form=fields, profiles=workspace.profiles())
                elif run.path is None and route != "/style.css":
                    body = None
                elif route == "/":
                    body = index_page(db, rules, counts, params, run.snapshot)
                elif route == "/threshold":
                    body = threshold_page(db, rules, params, run.snapshot)
                elif route == "/scores":
                    body = scores_page(db, run.snapshot, params)
                elif route == "/scored-pair":
                    body = missed_pair_page(run.snapshot, rules, params, legacy_limit=run.header["rules"].get("block_limit"))
                elif route in ("/pair", "/compare"):
                    n = int(params.get("n", ["0"])[0])
                    body = (comparison_page(db, rules, n, token, run.snapshot,
                                            int(params["page"][0]) if "page" in params else None) if route == "/compare"
                            else detail_page(db, rules, n))
                elif route == "/diagnose":
                    n = int(params.get("n", ["0"])[0])
                    try:
                        body = diagnosis_page(db, rules, n, params, run.snapshot, run.message,
                                              legacy_limit=run.header["rules"].get("block_limit"))
                    except (ValueError, sqlite3.DatabaseError) as exc:
                        self.page(layout(f'<h1>Диагностика недоступна</h1><p>{html(exc)}</p>'
                                         '<a href="/?status=unmatched&amp;side=left">К записям слева</a>'),
                                  HTTPStatus.BAD_REQUEST)
                        return
                elif route == "/accepted.csv":
                    self.headers_for(HTTPStatus.OK, "text/csv")
                    self.send_header("Content-Disposition", 'attachment; filename="concord-accepted.csv"')
                    self.end_headers()
                    for chunk in csv_chunks(db, rules):
                        self.wfile.write(chunk)
                    return
                elif route == "/style.css":
                    self.page(css, mime="text/css")
                    return
                else:
                    body = None
            except (ValueError, OverflowError, OSError, sqlite3.DatabaseError) as exc:
                self.page(layout(f'<h1>Не удалось открыть страницу</h1><p>{html(exc)}</p>'
                                 '<a href="/history">К истории запусков</a>'), HTTPStatus.BAD_REQUEST)
                return
            except (BrokenPipeError, ConnectionResetError):
                return
            if body is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.page(body)

        def do_POST(self):
            with workspace_lock:
                self.submit_form()

        def submit_form(self):
            if not self.local_request():
                return
            if self.headers.get("Origin") not in (None, "http://" + self.headers["Host"]):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            n = 0
            run = None
            route = ""
            form = {}
            try:
                run, route = self.select_run(urlsplit(self.path).path)
                if run is None or route not in ("/decision", "/new", "/sources/connect", "/new/suggest", "/new/fields", "/new/connect", "/new/return") or (route == "/decision" and not run.path):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= (65536 if route == "/decision" else 1024 * 1024):
                    self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                form = parse_qs(self.rfile.read(size).decode(), keep_blank_values=True,
                                strict_parsing=True, max_num_fields=2000, errors="strict")
                if len(form.get("token", [])) != 1 or not secrets.compare_digest(form["token"][0].encode(), token.encode()):
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                if route == "/sources/connect":
                    from .source_screen import inspect_page
                    self.page(inspect_page(form, token, workspace.folder))
                    return
                if route == "/new/connect":
                    from .source_screen import connect_page
                    self.page(connect_page(start_connection(form), token))
                    return
                if route == "/new/return":
                    self.page(new_page(run, run.rules, token, form=restore_draft(form), profiles=workspace.profiles()))
                    return
                if route == "/new/fields":
                    self.page(new_page(run, run.rules, token, form=form, profiles=workspace.profiles(), add_row=True))
                    return
                if route == "/new/suggest":
                    from .suggest_screen import suggest_page
                    self.page(suggest_page(run, form, token))
                    return
                if route == "/new":
                    path = create_run(workspace, run, form)
                    workspace.open(path)
                    if workspace.initial is None:
                        workspace.initial = path
                    location = "/runs/" + run_key(path) + "/"
                else:
                    keys = {"n", "revision", "action", "note", "token"}
                    if set(form) != keys or any(len(v) != 1 for v in form.values()):
                        raise ValueError("Проверьте поля решения")
                    n = int(form["n"][0])
                    set_decision(run.db, n, form["action"][0], form["note"][0], int(form["revision"][0]))
                    location = self.prefix + f"/compare?n={n}"
            except (ValueError, OverflowError, OSError, sqlite3.DatabaseError) as exc:
                code = HTTPStatus.CONFLICT if isinstance(exc, ReviewConflict) else HTTPStatus.BAD_REQUEST
                if route.startswith("/new") and run is not None:
                    self.page(new_page(run, run.rules, token, str(exc), form, workspace.profiles()), code)
                    return
                back = f"/compare?n={n}" if n > 0 else "/"
                self.page(layout(f'<h1>Решение не сохранено</h1><p>{html(exc)}</p>'
                                 f'<a href="{back}">Вернуться к проверке</a>'), code)
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = False

    with LocalServer(("127.0.0.1", port), Handler) as server:
        port = server.server_port
        print(f"Concord: http://127.0.0.1:{port}/", flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Просмотр и анализ результатов Concord")
    parser.add_argument("result", type=Path, nargs="?")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, help="Каталог результатов для истории и новых запусков")
    args = parser.parse_args()
    try:
        path = args.result.resolve(strict=True) if args.result else None
        with closing(Workspace(path, args.workspace or (path.parent if path else Path("local-runs")))) as workspace:
            serve(workspace, args.port)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
