"""UI smoke tests with Streamlit's headless AppTest and a scripted model (no key, no network)."""
from pathlib import Path
from streamlit.testing.v1 import AppTest
import t2s.llm as llm_module
from t2s.llm import ScriptedLLM


def script(i, user, tool):
    if tool == "summarize":
        return {"answer": "There are 59 customers."}
    if "top customers" in user.lower() and "Clarification" not in user:
        return {"action": "clarify", "clarifying_question": "Top by what?", "options": ["Total spend", "Orders"]}
    if "weather" in user.lower():
        return {"action": "refuse", "refusal_reason": "I only answer questions about the music store data."}
    return {"action": "answer", "sql": "SELECT COUNT(*) AS customers FROM Customer", "assumptions": "none needed"}


def make_app(monkeypatch):
    monkeypatch.setattr(llm_module, "make_llm", lambda *a, **k: ScriptedLLM(script))
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=30)
    return at.run()


def test_answer_renders_sql_table_and_sentence(monkeypatch):
    at = make_app(monkeypatch)
    at.chat_input[0].set_value("How many customers?").run()
    assert not at.exception
    assert any("59 customers" in s.value for s in at.success)
    assert any("SELECT COUNT" in c.value for c in at.code)
    assert len(at.dataframe) == 1


def test_refusal_shown_as_error_without_sql(monkeypatch):
    at = make_app(monkeypatch)
    at.chat_input[0].set_value("what's the weather").run()
    assert any("music store" in e.value for e in at.error)
    assert len(at.dataframe) == 0


def test_clarification_options_and_followup(monkeypatch):
    at = make_app(monkeypatch)
    at.chat_input[0].set_value("Who are our top customers?").run()
    assert any("Top by what?" in w.value for w in at.warning)
    labels = [b.label for b in at.button]
    assert "Total spend" in labels
    next(b for b in at.button if b.label == "Total spend").click().run()
    assert not at.exception and len(at.dataframe) == 1       # clarified -> answered


def test_missing_credentials_show_friendly_error(monkeypatch):
    import streamlit as st
    st.cache_resource.clear()      # earlier tests cached an Assistant built with the scripted model
    for k in ["ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("T2SQL_PROVIDER", "azure")
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=30).run()
    at.chat_input[0].set_value("How many customers?").run()
    assert any("Missing" in e.value and ".env" in e.value for e in at.error)   # names variables, no secret values
