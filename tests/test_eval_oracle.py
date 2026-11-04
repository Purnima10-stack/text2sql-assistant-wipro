"""Oracle test: a perfect 'model' (returns the reference SQL / correct behaviour) must score 100%.
This validates reference SQL + comparator + scoring + pipeline together. It also checks that a
deliberately broken model scores LOW, i.e. the evaluation can actually fail."""
import json, re
from pathlib import Path
from t2s.assistant import Assistant, Config
from t2s.llm import ScriptedLLM
from eval.run_eval import score

TS = json.loads((Path(__file__).parent.parent / "eval" / "test_set.json").read_text())
BY_Q = {q["question"]: q for q in TS}


def oracle(i, user, tool):
    q = BY_Q[re.search(r"Question: (.*?)(?:\n|$)", user).group(1).strip()]
    if q["behavior"] == "clarify":
        return {"action": "clarify", "clarifying_question": "?", "options": ["a", "b"]}
    if q["behavior"] == "refuse":
        return {"action": "refuse", "refusal_reason": "no"}
    return {"action": "answer", "sql": q["ref_sql"]}


def test_oracle_scores_100_percent():
    a = Assistant(ScriptedLLM(oracle), Config(summarize=False))
    results = [score(q, a.ask(q["question"])) for q in TS]
    bad = [(q["id"], r) for q, r in zip(TS, results) if not r["correct"]]
    assert not bad, bad


def test_broken_model_scores_low():
    def always_count(i, user, tool):
        return {"action": "answer", "sql": "SELECT COUNT(*) FROM Customer"}
    a = Assistant(ScriptedLLM(always_count), Config(summarize=False))
    acc = sum(score(q, a.ask(q["question"]))["correct"] for q in TS) / len(TS)
    assert acc < 0.10      # only E01 ("How many customers") can pass by luck
