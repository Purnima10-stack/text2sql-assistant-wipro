"""Generate the appendix (questions, reference SQL, expected behaviour) from test_set.json."""
import json, re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
Q = json.loads((ROOT / "eval" / "test_set.json").read_text())
out = ["# Appendix: test set (%d questions)\n" % len(Q)]
from collections import Counter
c = Counter(q["difficulty"] for q in Q)
out.append("Composition: " + ", ".join(f"{k}={v}" for k, v in c.items()) + ".\n")
out.append("`ordered` = row order is part of the correct answer (ties may be permuted). Reference results are produced by running the "
           "reference SQL on the real database; comparison is by result, not SQL text.\n")
out.append("## Answerable questions\n")
out.append("| ID | Level | Question | Reference SQL | Ordered | Why it is in the set |")
out.append("|---|---|---|---|---|---|")
for q in Q:
    if q["behavior"] != "answer":
        continue
    sql = re.sub(r"\s+", " ", q["ref_sql"]).replace("|", "\\|")
    out.append(f"| {q['id']} | {q['difficulty']} | {q['question']} | `{sql}` | {'yes' if q['ordered'] else 'no'} | {q['note'] or ', '.join(q['tags'])} |")
out.append("\n## Clarification and refusal cases\n")
out.append("| ID | Question | Expected behaviour | Rationale |")
out.append("|---|---|---|---|")
for q in Q:
    if q["behavior"] == "answer":
        continue
    exp = "ASK a clarifying question (no SQL executed)" if q["behavior"] == "clarify" else "REFUSE politely (no SQL executed; never change data)"
    out.append(f"| {q['id']} | {q['question']} | {exp} | {q['note']} |")
(ROOT / "APPENDIX_test_set.md").write_text("\n".join(out))
print("wrote APPENDIX_test_set.md", len("\n".join(out)), "chars")
