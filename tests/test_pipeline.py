"""Pipeline behaviour tests with a scripted fake LLM (no network, deterministic)."""
from t2s.assistant import Assistant, Config
from t2s.llm import ScriptedLLM


def make(fn, **cfg):
    return Assistant(ScriptedLLM(fn), Config(summarize=False, **cfg))


def test_answer_path():
    a = make(lambda i, u, t: {"action": "answer", "sql": "SELECT COUNT(*) AS n FROM Customer"})
    r = a.ask("How many customers?")
    assert r.status == "answered" and r.rows == [(59,)] and r.sql


def test_clarify_and_refuse_never_execute_sql():
    a = make(lambda i, u, t: {"action": "clarify", "clarifying_question": "By what?", "options": ["a", "b"]})
    r = a.ask("top customers")
    assert r.status == "clarify" and r.sql is None and r.options == ["a", "b"] and not r.attempts
    a = make(lambda i, u, t: {"action": "refuse", "refusal_reason": "Out of scope"})
    r = a.ask("weather?")
    assert r.status == "refused" and not r.attempts


def test_repair_loop_fixes_invalid_sql():
    def fn(i, u, t):
        return {"action": "answer", "sql": "SELECT COUNT(*) FROM Custmer" if i == 0 else "SELECT COUNT(*) FROM Customer"}
    r = make(fn, repair=True).ask("How many customers?")
    assert r.status == "answered" and len(r.attempts) == 2 and "error" in r.attempts[0]


def test_no_repair_returns_graceful_error():
    r = make(lambda i, u, t: {"action": "answer", "sql": "SELECT COUNT(*) FROM Custmer"}, repair=False).ask("x?")
    assert r.status == "error" and "rephras" in r.answer.lower()


def test_repair_is_bounded():
    r = make(lambda i, u, t: {"action": "answer", "sql": "SELECT bogus FROM Customer"}, max_repairs=2).ask("x?")
    assert r.status == "error" and len(r.attempts) == 3      # 1 original + 2 repairs, then stop


def test_write_attempt_is_refused_not_repaired():
    a = make(lambda i, u, t: {"action": "answer", "sql": "DELETE FROM Customer"})
    r = a.ask("delete all customers")
    assert r.status == "refused" and r.sql is None and a.llm.calls == 1   # no repair attempt
    assert a.db.execute("SELECT COUNT(*) FROM Customer").rows == [(59,)]


def test_empty_result_reported_honestly():
    r = make(lambda i, u, t: {"action": "answer", "sql": "SELECT * FROM Customer WHERE Country='Atlantis'"}).ask("x")
    assert r.status == "empty" and "no rows" in r.answer.lower()


def test_empty_retry_can_fix_wrong_literal():
    def fn(i, u, t):
        return {"action": "answer", "sql": "SELECT COUNT(*) FROM Genre WHERE Name='Hip-Hop' GROUP BY Name" if i == 0
                else "SELECT COUNT(*) FROM Genre WHERE Name='Hip Hop/Rap'"}
    r = make(fn, empty_retry=True).ask("x")
    assert r.status == "answered" and r.rows == [(1,)]


def test_llm_failure_does_not_crash():
    def boom(i, u, t): raise RuntimeError("network down")
    r = make(boom).ask("anything")
    assert r.status == "error" and "failed" in r.answer


def test_retrieved_mode_trims_context():
    a = make(lambda i, u, t: {"action": "refuse"}, schema_mode="retrieved")
    r = a.ask("revenue share by genre")
    assert set(r.tables_in_context) < set(a.schema.tables)


def test_intersect_query_is_answered_not_refused():
    sql = "SELECT Name FROM Genre INTERSECT SELECT Name FROM Genre WHERE Name = 'Rock'"
    r = make(lambda i, u, t: {"action": "answer", "sql": sql}).ask("x")
    assert r.status == "answered" and r.rows == [("Rock",)]
