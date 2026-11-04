"""
Run one or more system variants over the test set, score each answer by RESULT comparison, save JSONL.

  python -m eval.run_eval --systems A_full_norepair,B_full_repair,C_retrieved_repair --repeats 3
  python -m eval.run_eval --simulated ...      # harness self-test, no API key (output goes to results_simulated/)
"""
import argparse, json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from t2s.assistant import Assistant
from t2s.safe_db import SafeDB
from eval.compare import compare_results, classify_failure, norm
from eval.systems import SYSTEMS

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "Chinook_Sqlite.sqlite"


def score(q: dict, resp) -> dict:
    """Return {correct, lenient_correct, failure_type, reason, ...} for one question/response."""
    beh, status = q["behavior"], resp.status
    out = {"expected": beh, "status": status, "correct": False, "lenient_correct": False,
           "failure_type": None, "reason": None, "guard_caught": False, "extra_cols": 0}
    # did the DB-level guard (not the model) stop a write? -> report separately
    api_failed = status == "error" and resp.llm_calls == 0       # no successful model call at all
    out["guard_caught"] = any("Only SELECT" in (a.get("error") or "") or "Forbidden" in (a.get("error") or "")
                              for a in resp.attempts)
    if api_failed:
        out["failure_type"] = "llm_api_error"      # infrastructure failure (e.g. rate limit): rerun, do not blame the model
        return out
    if beh == "clarify":
        out["correct"] = out["lenient_correct"] = status == "clarify"
        if not out["correct"]:
            out["failure_type"] = "failed_to_clarify" if status in ("answered", "empty") else f"wrong_behavior_{status}"
    elif beh == "refuse":
        out["correct"] = out["lenient_correct"] = status == "refused"
        if not out["correct"]:
            out["failure_type"] = "failed_to_refuse" if status in ("answered", "empty") else f"wrong_behavior_{status}"
    else:   # answer
        if status in ("answered", "empty"):
            o = compare_results(q["ref_columns"], q["ref_rows"], resp.columns, resp.rows, q["ordered"])
            out.update(correct=o.match, lenient_correct=o.lenient, reason=o.reason, extra_cols=o.extra_cols)
            if not o.match:
                out["failure_type"] = classify_failure(resp.sql, q["ref_sql"], o.reason, status)
        else:
            out["failure_type"] = classify_failure(resp.sql, q["ref_sql"], "", status)
    return out


def run_one(system: str, run_idx: int, questions: list, llm, workers: int, out_dir: Path):
    cfg = SYSTEMS[system]
    asst = Assistant(llm, cfg, DB)

    def work(q):
        t0 = time.monotonic()
        resp = asst.ask(q["question"])
        sc = score(q, resp)
        return {"id": q["id"], "difficulty": q["difficulty"], "question": q["question"], "tags": q["tags"],
                "sql": resp.sql, "ref_sql": q.get("ref_sql"), "n_rows": len(resp.rows), "rows_head": resp.rows[:5],
                "n_attempts": len(resp.attempts), "attempt_errors": [a.get("error") for a in resp.attempts if a.get("error")],
                "tables_in_context": resp.tables_in_context, "assumptions": resp.assumptions, "answer_text": (resp.answer or "")[:300],
                "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
                "llm_calls": resp.llm_calls, "latency_s": round(resp.latency_s, 3), **sc}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        items = list(ex.map(work, questions))
    path = out_dir / system / f"run{run_idx}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(i, default=str) for i in items))
    acc = sum(i["correct"] for i in items) / len(items)
    print(f"  {system} run{run_idx}: accuracy {acc:.1%} ({sum(i['correct'] for i in items)}/{len(items)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", default="A_full_norepair,B_full_repair,C_retrieved_repair")
    ap.add_argument("--repeats", type=int, default=3, help="independent repeats per system (measures run-to-run noise)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None, help="only first N questions (smoke test)")
    ap.add_argument("--simulated", action="store_true", help="use the fake LLM (harness self-test only)")
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()

    questions = json.loads((ROOT / "eval" / "test_set.json").read_text())[: args.limit]
    out_dir = ROOT / ("results_simulated" if args.simulated else "results")
    if args.simulated:
        from eval.simulated_llm import SimulatedLLM
        print("*** SIMULATED LLM: harness self-test, numbers are meaningless for real accuracy ***")
    for system in args.systems.split(","):
        for k in range(args.repeats):
            if args.simulated:
                llm = SimulatedLLM(seed=k)
            else:
                from t2s.llm import make_llm
                llm = make_llm(temperature=args.temperature)
            run_one(system, k, questions, llm, args.workers, out_dir)
    (out_dir / "meta.json").write_text(json.dumps({"simulated": args.simulated, "model": llm.name,
                                                   "temperature": args.temperature, "n_questions": len(questions)}))


if __name__ == "__main__":
    main()
