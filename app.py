"""
app.py -- Streamlit UI.   Run:  streamlit run app.py

Thin presentation layer only: all logic (safety, prompting, repair, clarify/refuse) lives in t2s/assistant.py,
so the UI and the CLI behave identically. Secrets come from .env via the t2s package; they are never
shown or stored in the UI (the sidebar shows only the model/deployment NAME).

Flow per question:  chat box -> Assistant.ask() -> show answer, assumption, SQL, result table, optional trace.
Clarifying questions: options appear as buttons; clicking one (or typing a reply) is merged into the
original question and re-asked.
"""
import pandas as pd
import streamlit as st

from t2s import llm as llm_module            # attribute access (not `from ... import`) so tests can patch it
from t2s.assistant import Assistant, Config

st.set_page_config(page_title="Chinook Data Assistant", page_icon="🎵", layout="wide")

EXAMPLES = [
    "Which 5 customers spent the most in 2012, and which country are they in?",
    "What share of revenue came from each genre last year?",
    "Who are our top customers?",
    "Which genres have never been bought by a customer from the USA?",
    "Which customers from Japan spent more than 100 dollars in total?",
    "What's the weather today?",
]


# ------------------------------------------------------------------ setup
@st.cache_resource(show_spinner=False)
def get_assistant(schema_mode: str, repair: bool, empty_retry: bool, summarize: bool):
    """One Assistant per settings combination (builds schema + prompt once, reuses the LLM client)."""
    cfg = Config(schema_mode=schema_mode, repair=repair, empty_retry=empty_retry, summarize=summarize)
    return Assistant(llm_module.make_llm(), cfg)


def init_state():
    st.session_state.setdefault("turns", [])        # list of {"question", "resp"}
    st.session_state.setdefault("pending", None)    # original question awaiting a clarification


def submit(text: str, settings: dict):
    """Run one user input. If we asked a clarifying question last time, merge the reply into it."""
    text = text.strip()
    if not text:
        return
    st.session_state.pop("config_error", None)
    shown, question = text, text
    if st.session_state.pending:
        question = f"{st.session_state.pending}\n(Clarification from the user: {text})"
    try:
        asst = get_assistant(**settings)
        with st.spinner("Thinking..."):
            resp = asst.ask(question)
    except SystemExit as e:                          # make_llm() raises this when .env is incomplete
        st.session_state.config_error = str(e)
        return
    st.session_state.pending = question if resp.status == "clarify" else None
    st.session_state.turns.append({"question": shown, "resp": resp})


# ------------------------------------------------------------------ rendering
def render_response(resp, is_last: bool, settings: dict, idx: int):
    if resp.status == "answered":
        if resp.answer:
            st.success(resp.answer)
    elif resp.status == "empty":
        st.info(resp.answer)
    elif resp.status == "clarify":
        st.warning(f"I need one detail first: **{resp.answer}**")
        if is_last and resp.options:
            cols = st.columns(len(resp.options))
            for c, opt in zip(cols, resp.options):
                c.button(opt, key=f"opt_{idx}_{opt}", on_click=submit, args=(opt, settings))
            st.caption("Click an option or type your answer below.")
    elif resp.status == "refused":
        st.error(resp.answer)
    else:
        st.error(resp.answer or "Something went wrong.")

    if resp.assumptions and resp.status in ("answered", "empty"):
        st.caption(f"Assumption: {resp.assumptions}")

    if resp.sql and resp.status in ("answered", "empty"):
        with st.expander("Generated SQL", expanded=True):
            st.code(resp.sql.strip(), language="sql")
        if resp.rows:
            df = pd.DataFrame(resp.rows, columns=resp.columns)
            st.dataframe(df, use_container_width=True, hide_index=True)
            note = f"{len(df)}{'+' if resp.truncated else ''} row(s)"
            if resp.truncated:
                note += " (capped at the safety limit)"
            st.caption(note)

    with st.expander("Details (attempts, tokens, latency)"):
        st.write(f"status: `{resp.status}` | model calls: {resp.llm_calls} | SQL attempts: {len(resp.attempts)} | "
                 f"tokens: {resp.input_tokens} in / {resp.output_tokens} out | {resp.latency_s:.1f}s")
        st.write(f"tables in prompt: {', '.join(resp.tables_in_context)}")
        for i, a in enumerate(resp.attempts, 1):
            st.markdown(f"**Attempt {i}**" + (f" failed: `{a['error']}`" if a.get("error") else " succeeded"))
            st.code(a["sql"], language="sql")


def main():
    init_state()
    st.title("🎵 Chinook Data Assistant")
    st.caption("Ask questions about the music-store database in plain English. Read-only: the assistant can "
               "look at data but never change it.")

    with st.sidebar:
        st.header("Settings")
        schema_mode = st.radio("Schema given to the model", ["full", "retrieved"],
                               help="full = all 11 tables. retrieved = keyword-selected tables only (fewer tokens).")
        repair = st.toggle("Automatic error correction", value=True)
        empty_retry = st.toggle("Re-check empty results", value=False)
        summarize = st.toggle("One-line answer (extra model call)", value=True,
                              help="Turn off to halve model calls if you hit rate limits.")
        st.divider()
        st.subheader("Try an example")
        for i, ex in enumerate(EXAMPLES):
            if st.button(ex, key=f"ex_{i}", use_container_width=True):
                st.session_state.pending = None      # an example is a fresh question
                st.session_state.queued = ex
        if st.button("Clear conversation", use_container_width=True):
            st.session_state.turns, st.session_state.pending = [], None
            st.rerun()

    settings = dict(schema_mode=schema_mode, repair=repair, empty_retry=empty_retry, summarize=summarize)

    # Typed input or a clicked example
    typed = st.chat_input("Ask a question, e.g. 'How many customers are from Canada?'")
    queued = st.session_state.pop("queued", None)
    if typed or queued:
        submit(typed or queued, settings)

    if st.session_state.get("config_error"):
        st.error(st.session_state.config_error)
        st.stop()

    # History
    turns = st.session_state.turns
    if not turns:
        st.info("Type a question below or pick an example from the sidebar.")
    for i, t in enumerate(turns):
        with st.chat_message("user"):
            st.write(t["question"])
        with st.chat_message("assistant"):
            render_response(t["resp"], i == len(turns) - 1, settings, i)


main()
