"""Измерения штатного просмотрщика в отдельном процессе на локальных SQL-источниках."""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
from functools import wraps
import json
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from concord import matching, runs, workspace
from concord.sources import reader
from concord.viewer import serve


def measured_server(folder, port):
    metrics = Counter()

    def emit(stage, seconds):
        metrics[stage] += seconds
        print(f"{stage}: {seconds:.3f} s", flush=True)
        (folder / "stages.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    def timer(fn, stage):
        @wraps(fn)
        def call(*args, **kwargs):
            start = perf_counter()
            result = fn(*args, **kwargs)
            emit(stage, perf_counter() - start)
            return result
        return call

    def next_times(iterator, key):
        # Время между yield исключает работу потребителя строки.
        total = 0.0
        count = 0
        while True:
            start = perf_counter()
            try:
                row = next(iterator)
            except StopIteration:
                total += perf_counter() - start
                break
            total += perf_counter() - start
            count += 1
            yield row
        metrics[key + "_rows"] = count
        emit(key, total)

    ingest, projection, results = matching.ingest, reader.projection, matching.results
    last_ingest = None

    def timed_ingest(db, side, rows, rules):
        nonlocal last_ingest
        source = "mysql" if side else "postgresql"
        start = perf_counter()
        ingest(db, side, rows, rules)
        emit("local_ingest_" + source, perf_counter() - start - metrics["read_" + source])
        last_ingest = perf_counter()

    def timed_projection(conn, profile, *args, **kwargs):
        yield from next_times(projection(conn, profile, *args, **kwargs), "read_" + profile.driver)

    def timed_results(*args, **kwargs):
        emit("blocking_index", perf_counter() - last_ingest)
        yield from next_times(results(*args, **kwargs), "comparison")

    def timed_export(fn):
        def call(*args, **kwargs):
            start = perf_counter()
            counts = fn(*args, **kwargs)
            total = perf_counter() - start
            known = sum(metrics[k] for k in ("read_postgresql","read_mysql","local_ingest_postgresql",
                                             "local_ingest_mysql","blocking_index","comparison"))
            emit("serialization_commit_other", total - known)
            emit("export_run_total", total)
            return counts
        return call

    from concord import new_screen
    folder.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for module, attr, wrapper in (
            (reader, "projection", timed_projection), (matching, "ingest", timed_ingest),
            (matching, "results", timed_results), (new_screen, "export_run", timed_export(runs.export_run)),
            (workspace, "load_run", timer(workspace.load_run, "load_review_store")),
        ):
            stack.enter_context(patch.object(module, attr, wrapper))
        ws = stack.enter_context(closing(workspace.Workspace(None, folder)))
        serve(ws, port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="Отдельный пустой каталог для одного замера")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.workspace.exists() and any(args.workspace.iterdir()):
        parser.error("Для воспроизводимого замера нужен пустой каталог")
    measured_server(args.workspace, args.port)
