"""
Interactive demo.   python cli.py            (needs ANTHROPIC_API_KEY)
Shows, for every question: the generated SQL, the result table, and a one-line plain-English answer.
Clarifying questions are answered in the next prompt; the reply is merged into the original question.
"""
import argparse, textwrap

import pandas as pd

from t2s.assistant import Assistant, Config
from t2s.llm import make_llm

pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 20)


def render(resp, show_trace=False):
    if resp.status in ("answered", "empty") and resp.sql:
        print("\n--- SQL " + "-" * 60)
        print(textwrap.dedent(resp.sql).strip())
        if resp.assumptions:
            print(f"\n(assumption: {resp.assumptions})")
        print("\n--- RESULT " + "-" * 57)
        if resp.rows:
            df = pd.DataFrame(resp.rows, columns=resp.columns)
            print(df.head(25).to_string(index=False))
            if len(df) > 25 or resp.truncated:
                print(f"... ({len(df)}{'+' if resp.truncated else ''} rows; showing 25)")
        else:
            print("(no rows)")
    print("\n>> " + (resp.answer or ""))
    if resp.status == "clarify" and resp.options:
        for i, o in enumerate(resp.options, 1):
            print(f"   {i}. {o}")
    if show_trace:
        print(f"\n[status={resp.status} llm_calls={resp.llm_calls} attempts={len(resp.attempts)} "
              f"tokens={resp.input_tokens}+{resp.output_tokens} {resp.latency_s:.1f}s tables={resp.tables_in_context}]")
        for a in resp.attempts:
            if a.get("error"):
                print(f"   attempt failed: {a['error']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema-mode", choices=["full", "retrieved"], default="full")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--empty-retry", action="store_true")
    ap.add_argument("--no-summary", action="store_true", help="skip the one-line answer (halves model calls / tokens)")
    ap.add_argument("--trace", action="store_true", help="show attempts, tokens, latency")
    args = ap.parse_args()
    asst = Assistant(make_llm(), Config(schema_mode=args.schema_mode, repair=not args.no_repair, summarize=not args.no_summary,
                                            empty_retry=args.empty_retry))
    print(f"Chinook text-to-SQL assistant  (model: {asst.llm.name}, schema: {args.schema_mode}). Ctrl-C to quit.")
    pending = None
    while True:
        try:
            q = input("\nask> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if pending:                      # the user is answering our clarifying question
            q = f"{pending}\n(Clarification from the user: {q})"
        resp = asst.ask(q)
        pending = q if resp.status == "clarify" else None
        render(resp, args.trace)


if __name__ == "__main__":
    main()
