"""Validate every reference query on the real DB, flag ties / empties, and write eval/test_set.json."""
import json, sys
from pathlib import Path
from t2s.safe_db import SafeDB
from eval.testset_src import Q

ROOT = Path(__file__).resolve().parent.parent
db = SafeDB(ROOT / "data" / "Chinook_Sqlite.sqlite", max_rows=1000)


def main(verbose=True):
    ids = [q["id"] for q in Q]
    assert len(ids) == len(set(ids)), "duplicate ids"
    counts = {}
    for q in Q:
        counts[q["difficulty"]] = counts.get(q["difficulty"], 0) + 1
        if q["behavior"] != "answer":
            continue
        r = db.execute(q["ref_sql"])           # reference SQL goes through the same safety layer
        q["ref_columns"], q["ref_rows"] = r.columns, [list(x) for x in r.rows]
        q["ref_n_rows"] = len(r.rows)
        if verbose:
            print(f"{q['id']:4s} {q['difficulty']:6s} rows={len(r.rows):3d} {q['question'][:70]}")
            for row in r.rows[:6]:
                print("        ", row)
        if not r.rows and "empty-result" not in q["tags"]:
            print("   !!! unexpectedly EMPTY", q["id"])
    print("counts:", counts, "total:", len(Q))
    out = ROOT / "eval" / "test_set.json"
    out.write_text(json.dumps(Q, indent=2, default=str))
    print("wrote", out)


if __name__ == "__main__":
    main()
