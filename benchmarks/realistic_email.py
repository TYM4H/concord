"""Сравнить два правила Concord с заранее сохранёнными правильными парами."""
import argparse
from array import array
from collections import Counter
from contextlib import closing
import csv
from dataclasses import asdict, replace
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from benchmarks.realistic_people import SIZE, OVERLAP, DUPLICATES, source_rows, write_truth
from concord.matching import SCHEMA, INDEX, ingest, results, values
from concord.rules import Field, Rules
from concord.sources.profile import Profile

STATES = ("no_candidate", "below_threshold", "review", "accepted")
EXPECTED_CSV = "32d8a8a3dc24b570da609d597cfdf757c18c951cba4e2fb83dc93d89610e2de5"


def read_truth(path):
    with path.open("rb") as raw:
        digest = hashlib.file_digest(raw, "sha256").hexdigest()
    with gzip.open(path, "rb") as raw_csv:
        csv_digest = hashlib.file_digest(raw_csv, "sha256").hexdigest()
    if csv_digest != EXPECTED_CSV:
        raise ValueError("Разметка отличается от зафиксированной для этого эксперимента")
    truth = array("I", [0]) * (SIZE + 1)
    with gzip.open(path, "rt") as stream:
        for row in csv.DictReader(stream):
            right = int(row["right_id"]) - SIZE
            left = int(row["left_id"])
            if not 1 <= right <= SIZE or not 1 <= left <= SIZE or truth[right]:
                raise ValueError("Некорректная или повторная связь в разметке")
            truth[right] = left
    if sum(map(bool, truth)) != OVERLAP + DUPLICATES:
        raise ValueError("Неполная разметка")
    return truth, digest


def sample(left, right, score):
    fields = []
    for i, label in enumerate(("full_name","email")):
        fields.append(dict(field=label,
            left=[s for item in left.get(i,{}).values() for s in item["originals"]],
            right=[s for item in right.get(i,{}).values() for s in item["originals"]]))
    return {"score":score, "fields":fields}


def evaluate(db, rules, truth):
    # Повторно используем один индекс признаков и частот. Меняется только
    # block_on_conflict; список кандидатов и все оценки вычисляет ядро Concord.
    db.execute("DELETE FROM paired_right")
    db.execute("DELETE FROM missed")
    states = bytearray(SIZE+1)
    wrong_accepted = {}
    incorrect = Counter()
    for row in results(db, rules, record_misses=True):
        if row["status"] == "unmatched":
            continue
        l, r = int(row["left_id"]), row["right_id"]-SIZE
        if truth[r] == l:
            if states[r]:
                raise ValueError("Повтор пары")
            states[r] = STATES.index(row["status"])
        else:
            incorrect[row["status"]] += 1
            if row["status"] == "accepted":
                wrong_accepted[l,r] = row["score"]
    for left, right in db.execute("SELECT left_id,right_id FROM missed"):
        l,r = int(left[1:-1]),int(right)-SIZE
        if truth[r] == l:
            if states[r]:
                raise ValueError("Пара одновременно сохранена и отсеяна")
            states[r] = 1
        else:
            incorrect["below_threshold"] += 1
    counts = Counter(STATES[states[r]] for r in range(1,SIZE+1) if truth[r])
    tp,fp = counts["accepted"],incorrect["accepted"]
    metrics = dict(true_by_status=counts, false_by_status=incorrect,
        true_accepted=tp, false_accepted=fp, true_not_automatically_accepted=OVERLAP+DUPLICATES-tp,
        true_not_shown=counts["below_threshold"]+counts["no_candidate"],
        precision=tp/(tp+fp),recall=tp/(OVERLAP+DUPLICATES))
    return metrics, states, wrong_accepted


def run(folder, profiles):
    if (folder/"comparison.json").exists():
        raise ValueError("Для повторного расчёта выберите новый каталог")
    folder.mkdir(parents=True, exist_ok=True)
    oracle = folder/"truth.csv.gz"
    if not oracle.exists():
        write_truth(oracle)
    truth,digest = read_truth(oracle)
    strict = Rules("id","id",(Field("full_name","full_name","text",70),Field("email","email","exact",30)),
                   accept=66.82)
    relaxed = replace(strict,fields=(strict.fields[0],replace(strict.fields[1],block_on_conflict=False)))
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("PRAGMA temp_store=MEMORY")
        db.executescript(SCHEMA)
        for i,side in enumerate(("left","right")):
            source = Profile.load(profiles[i])
            ingest(db,i,source_rows(source,side,os.environ.get(f"CONCORD_{side.upper()}_PASSWORD")),strict)
            if db.execute("SELECT count(*) FROM entity WHERE side=?",(i,)).fetchone()[0] != SIZE:
                raise ValueError("Неожиданное количество записей источника")
            print(f"Прочитан {side}",flush=True)
        db.executescript(INDEX)
        db.execute("CREATE TEMP TABLE missed(left_id TEXT,right_id TEXT, score REAL,PRIMARY KEY(left_id,right_id)) WITHOUT ROWID")
        quality = {}
        for side in (0,1):
            filled,typos=db.execute("SELECT count(*),sum(raw LIKE '%.tset') FROM observation WHERE side=? AND field=1",(side,)).fetchone()
            quality[("left","right")[side]]=dict(rows=SIZE,missing_email=SIZE-filled,email_domain_typos=typos)
        repeated = db.execute("SELECT count(*),sum(nl) FROM frequency WHERE field=0 AND nl>1").fetchone()
        quality["left_names_shared_by_different_people"]=repeated[0]
        quality["left_people_with_shared_name"]=repeated[1]
        print("Построен общий индекс",flush=True)
        baseline,old,old_fp = evaluate(db,strict,truth)
        print("Завершён режим с блокировкой",flush=True)
        trial,new,new_fp = evaluate(db,relaxed,truth)
        print("Завершён режим без блокировки",flush=True)
        if old_fp.keys() - new_fp.keys():
            raise ValueError("После ослабления правила пропали принятые пары")
        transitions=Counter()
        examples={}
        def record(kind,l,r,score=None):
            if kind not in examples:
                a=values(db,0,json.dumps(f"{l:07d}"))
                b=values(db,1,str(r+SIZE))
                examples[kind]=dict(left_id=f"{l:07d}",right_id=r+SIZE,**sample(a,b,score))
        with gzip.open(folder/"pair-audit.csv.gz","wt",encoding="utf-8",newline="") as out:
            writer=csv.writer(out)
            writer.writerow(("left_id","right_id","ground_truth","email_blocks","email_score_only"))
            for r in range(1,SIZE+1):
                l=truth[r]
                if not l:
                    continue
                a,b=STATES[old[r]],STATES[new[r]]
                writer.writerow((f"{l:07d}",SIZE+r,"true",a,b))
                transitions[f"{a} -> {b}"]+=1
                if old[r]!=new[r]:
                    if (a,b)!=("review","accepted"):
                        raise ValueError("Неожиданный переход правильной пары")
                    record("gained_true",l,r,70.0)
                elif b=="no_candidate":
                    record("true_no_candidate",l,r)
                elif b=="below_threshold":
                    if "true_below_threshold" not in examples:
                        score=db.execute("SELECT score FROM missed WHERE left_id=? AND right_id=?",
                                        (json.dumps(f"{l:07d}"),str(r+SIZE))).fetchone()[0]
                        record("true_below_threshold",l,r,score)
            for (l,r),score in new_fp.items():
                previous="accepted" if (l,r) in old_fp else "review"
                writer.writerow((f"{l:07d}",SIZE+r,"false",previous,"accepted"))
                record("false_in_both" if previous=="accepted" else "gained_false",l,r,score)
    report=dict(ground_truth_sha256=digest, expected_pairs=OVERLAP+DUPLICATES,
        persons_in_both=OVERLAP,extra_right_duplicates=DUPLICATES, quality=quality,
        strict_rules=asdict(strict),relaxed_rules=asdict(relaxed),email_blocks=baseline,
        email_score_only=trial,true_transitions=transitions,new_false_accepted=len(new_fp)-len(old_fp),
        examples=examples)
    (folder/"comparison.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    parser.add_argument("postgres_profile",type=Path)
    parser.add_argument("mysql_profile",type=Path)
    args=parser.parse_args()
    run(args.folder,(args.postgres_profile,args.mysql_profile))
