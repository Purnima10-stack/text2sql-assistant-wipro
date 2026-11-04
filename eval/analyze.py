"""
analyze.py -- turns raw run files into the numbers for the write-up, and answers
"is the difference between two systems real or noise?"

Three complementary checks (none is sufficient alone, with only ~55 questions):
  1. Paired exact McNemar test on per-question pass/fail (same questions, so use pairing).
       Only questions where the systems DISAGREE carry information. p < 0.05 needed.
  2. Paired bootstrap 95% CI on the accuracy difference (resample questions, keep pairing).
       If the CI contains 0 the difference could be sampling noise.
  3. Run-to-run noise floor: repeat each system k times. If two repeats of the SAME system differ by
       as much as A differs from B, the gap is not evidence of a real effect. Also reports 'flip rate'
       (share of questions whose outcome changes between repeats of one system).

  python -m eval.analyze                       # real results in results/
  python -m eval.analyze --dir results_simulated
"""
import argparse, json, random
from collections import Counter, defaultdict
from pathlib import Path

from scipy.stats import binomtest

ROOT = Path(__file__).resolve().parent.parent
GROUPS = ["easy", "medium", "hard", "clarify", "refuse"]


def load(d: Path):
    runs = defaultdict(list)            # system -> [ {qid: item} per repeat ]
    for sysdir in sorted(p for p in d.iterdir() if p.is_dir()):
        for f in sorted(sysdir.glob("run*.jsonl")):
            runs[sysdir.name].append({json.loads(l)["id"]: json.loads(l) for l in f.read_text().splitlines() if l})
    return runs


def acc(run, key="correct", group=None):
    items = [i for i in run.values() if group is None or i["difficulty"] == group]
    return sum(i[key] for i in items) / len(items) if items else float("nan")


def per_question_pass(runs_for_system, key="correct"):
    """Majority vote across repeats (k=1 -> the single run). Returns {qid: 0/1} and {qid: pass-rate}."""
    ids = runs_for_system[0].keys()
    rate = {q: sum(r[q][key] for r in runs_for_system) / len(runs_for_system) for q in ids}
    return {q: int(rate[q] > 0.5) for q in ids}, rate


def mcnemar(x: dict, y: dict):
    b = sum(1 for q in x if x[q] and not y[q])      # x passes, y fails
    c = sum(1 for q in x if not x[q] and y[q])
    p = 1.0 if b + c == 0 else binomtest(min(b, c), b + c, 0.5).pvalue
    return b, c, p


def paired_bootstrap(rate_x: dict, rate_y: dict, n=10000, seed=0):
    rng = random.Random(seed)
    ids = list(rate_x)
    diffs = []
    for _ in range(n):
        s = [rng.choice(ids) for _ in ids]
        diffs.append(sum(rate_y[q] - rate_x[q] for q in s) / len(s))
    diffs.sort()
    return diffs[int(0.025 * n)], diffs[int(0.975 * n)]


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 2
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return c - h, c + h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="results")
    ap.add_argument("--pairs", default="A_full_norepair:B_full_repair,B_full_repair:C_retrieved_repair")
    args = ap.parse_args()
    d = ROOT / args.dir
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
    runs = load(d)
    out = []
    P = out.append
    if meta.get("simulated"):
        P("**SIMULATED RESULTS -- harness self-test only. Not evidence about any real model.**\n")
    P(f"Model: `{meta.get('model','?')}`, temperature {meta.get('temperature','?')}, "
      f"{meta.get('n_questions','?')} questions, repeats per system: {', '.join(f'{s}={len(r)}' for s, r in runs.items())}\n")

    # ---------------- 1. accuracy table (mean over repeats, with per-repeat range)
    P("## Accuracy (strict result match; mean over repeats [min-max])\n")
    P("| System | Overall | " + " | ".join(GROUPS) + " | Lenient overall |")
    P("|---|---|" + "---|" * len(GROUPS) + "---|")
    for s, rs in runs.items():
        cells = []
        for g in [None] + GROUPS:
            vals = [acc(r, group=g) for r in rs]
            cells.append(f"{sum(vals)/len(vals):.1%} [{min(vals):.0%}-{max(vals):.0%}]")
        len_vals = [acc(r, "lenient_correct") for r in rs]
        P(f"| {s} | " + " | ".join(cells) + f" | {sum(len_vals)/len(len_vals):.1%} |")
    n = len(next(iter(runs.values()))[0])
    P(f"\nn = {n} questions per run. 95% Wilson CI for a single run at 80% accuracy is about "
      f"{wilson(int(0.8*n), n)[0]:.0%}-{wilson(int(0.8*n), n)[1]:.0%}: differences smaller than this need the paired tests below.\n")

    # ---------------- 2. failures by type
    P("## Failure breakdown (pooled over repeats; heuristic auto-labels, see failure_review.csv)\n")
    types = Counter()
    per_sys = {}
    for s, rs in runs.items():
        c = Counter(i["failure_type"] for r in rs for i in r.values() if not i["correct"])
        per_sys[s] = c
        types.update(c)
    P("| Failure type | " + " | ".join(runs) + " |")
    P("|---|" + "---|" * len(runs))
    for t, _ in types.most_common():
        P(f"| {t} | " + " | ".join(str(round(per_sys[s][t] / len(runs[s]), 1)) for s in runs) + " |")
    P("\n(values = average number of failing questions per run)\n")

    # ---------------- 3. questions that fail in EVERY run of EVERY system (hard core)
    always = [q for q in next(iter(runs.values()))[0]
              if all(not r[q]["correct"] for rs in runs.values() for r in rs)]
    P(f"Questions failed by all systems in all repeats: {always or 'none'}\n")

    # ---------------- 4. paired comparisons
    P("## Is the difference real? (paired tests)\n")
    for pair in args.pairs.split(","):
        a, b = pair.split(":")
        if a not in runs or b not in runs:
            continue
        pa, ra = per_question_pass(runs[a])
        pb, rb = per_question_pass(runs[b])
        b_only, a_only, p = mcnemar(pa, pb)       # (A passes & B fails) , (A fails & B passes)
        lo, hi = paired_bootstrap(ra, rb)
        diff = sum(rb.values()) / len(rb) - sum(ra.values()) / len(ra)
        P(f"**{a} -> {b}**: mean accuracy change {diff:+.1%}; 95% bootstrap CI [{lo:+.1%}, {hi:+.1%}]; "
          f"McNemar discordant pairs: {b} better on {a_only}, {a} better on {b_only}; exact p = {p:.3f}.")
        # noise floor
        spread = []
        for s in (a, b):
            vals = [acc(r) for r in runs[s]]
            spread.append(max(vals) - min(vals))
        flips = []
        for s in (a, b):
            ids = runs[s][0].keys()
            flips.append(sum(1 for q in ids if len({r[q]["correct"] for r in runs[s]}) > 1) / len(ids))
        verdict = ("REAL (CI excludes 0, p<0.05, and larger than run-to-run spread)"
                   if (lo > 0 or hi < 0) and p < 0.05 and abs(diff) > max(spread)
                   else "NOT DISTINGUISHABLE FROM NOISE at this sample size")
        P(f"  run-to-run spread of overall accuracy: {a}={spread[0]:.1%}, {b}={spread[1]:.1%}; "
          f"flip rate (questions whose outcome changes between repeats): {a}={flips[0]:.0%}, {b}={flips[1]:.0%}.")
        P(f"  => Verdict: {verdict}\n")

    # ---------------- 5. cost / latency / safety
    P("## Cost, latency, safety\n")
    P("| System | LLM calls/q | input tok/q | output tok/q | mean latency s | p95 latency s | guard caught writes |")
    P("|---|---|---|---|---|---|---|")
    for s, rs in runs.items():
        items = [i for r in rs for i in r.values()]
        lat = sorted(i["latency_s"] for i in items)
        P(f"| {s} | {sum(i['llm_calls'] for i in items)/len(items):.2f} | "
          f"{sum(i['input_tokens'] for i in items)/len(items):.0f} | {sum(i['output_tokens'] for i in items)/len(items):.0f} | "
          f"{sum(lat)/len(lat):.2f} | {lat[int(0.95*len(lat))-1]:.2f} | {sum(i['guard_caught'] for i in items)} |")

    text = "\n".join(out)
    print(text)
    (d / "report.md").write_text(text)

    # ---------------- failure_review.csv : one row per failing (system, run, question) for manual labelling
    import csv
    with open(d / "failure_review.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["system", "run", "id", "difficulty", "question", "auto_failure_type", "manual_failure_type",
                    "predicted_sql", "reference_sql"])
        for s, rs in runs.items():
            for k, r in enumerate(rs):
                for i in r.values():
                    if not i["correct"]:
                        w.writerow([s, k, i["id"], i["difficulty"], i["question"], i["failure_type"], "",
                                    i["sql"], i["ref_sql"]])


if __name__ == "__main__":
    main()
