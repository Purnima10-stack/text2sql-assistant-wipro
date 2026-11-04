"""
SIMULATED LLM -- ONLY for testing the evaluation harness end-to-end without an API key.

It "knows" the reference answers and injects deterministic, plausible mistakes (syntax slip that a repair
can fix, wrong filter, missing table when retrieval drops it, ...). Its numbers say NOTHING about a real
model; every report produced from it is stamped SIMULATED. Real runs use t2s.llm.AnthropicLLM.
"""
import hashlib, json, re
from pathlib import Path
from t2s.llm import LLMOutput

TESTSET = json.loads((Path(__file__).parent / "test_set.json").read_text())
BY_Q = {q["question"]: q for q in TESTSET}


def _h(*parts) -> float:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class SimulatedLLM:
    name = "SIMULATED"

    def __init__(self, seed: int = 0):
        self.seed = seed

    def complete(self, system: str, user: str, tool: dict) -> LLMOutput:
        if tool["name"] == "summarize":
            return LLMOutput({"answer": "Simulated summary."})
        m = re.search(r"Question: (.*?)(?:\n|$)", user)
        q = BY_Q[m.group(1).strip()]
        if q["behavior"] == "clarify":
            return LLMOutput({"action": "clarify", "clarifying_question": "Which metric?", "options": ["a", "b"]})
        if q["behavior"] == "refuse":
            # simulate one model-level miss on a write request so the guard layer gets exercised
            if q["id"] == "R07" and _h(self.seed, q["id"]) > 0.0:
                return LLMOutput({"action": "answer", "sql": "UPDATE Track SET UnitPrice = 1.99"})
            return LLMOutput({"action": "refuse", "refusal_reason": "Out of scope."})
        ref = q["ref_sql"]
        # retrieval dropped a table the reference needs -> simulated model cannot get it right
        needed = {t for t in re.findall(r"(?:FROM|JOIN)\s+(\w+)", ref, flags=re.I)}
        present = set(re.findall(r"^TABLE (\w+):", system, flags=re.M))
        if needed - present - {"s", "g", "t", "m", "r"}:
            return LLMOutput({"action": "answer", "sql": "SELECT COUNT(*) FROM Customer"})
        if "Your previous SQL failed" in user:                    # repair turn: fixes it 90% of the time
            return LLMOutput({"action": "answer", "sql": ref if _h(self.seed, q["id"], "fix") < 0.9
                              else "SELECT 1 FROM Nope"})
        r = _h(self.seed, q["id"])
        hard = q["difficulty"] == "hard"
        if r < 0.10:                                               # first-attempt SQL error (repairable)
            return LLMOutput({"action": "answer", "sql": ref.replace("SELECT", "SELEC", 1)})
        if r < (0.30 if hard else 0.16):                           # silently wrong answer (not repairable)
            return LLMOutput({"action": "answer", "sql": re.sub(r"LIMIT \d+", "LIMIT 1", ref) if "LIMIT" in ref
                              else ref.replace("SUM(", "AVG(", 1) if "SUM(" in ref else "SELECT COUNT(*) FROM Track"})
        return LLMOutput({"action": "answer", "sql": ref})
