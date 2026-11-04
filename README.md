# Reliable Text-to-SQL Assistant (Chinook / SQLite)

Plain-English questions -> **generated SQL**, **result table**, **one-line answer**. Read-only by construction,
handles errors / empty results / ambiguity / out-of-scope, and ships with a result-based evaluation harness.

## Quick start
```bash
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your keys (Azure or Anthropic)
python cli.py --trace                          # interactive demo (terminal)
streamlit run app.py                           # web UI at http://localhost:8501
python -m pytest -q tests                      # 55 tests: safety, pipeline, comparator, oracle, UI (no API key needed)
python -m eval.run_eval --repeats 3            # real evaluation: systems A, B, C x 3 repeats (~495 LLM calls + repairs)
python -m eval.analyze                         # tables, failure breakdown, significance -> results/report.md
```
The Chinook DB (`data/Chinook_Sqlite.sqlite`, v1.4.5 from github.com/lerocha/chinook-database) is included.

## Layout
| Path | Role |
|---|---|
| `app.py` | Streamlit web UI (same assistant as the CLI) |
| `cli.py` | Terminal demo |
| `t2s/safe_db.py` | **Safety layer**: 4 independent controls (AST check, `mode=ro`, SQLite authorizer, timeout/row caps) |
| `t2s/schema.py` | Schema context: introspected DDL + exact value hints + glossary; BM25 + FK-path retrieval variant |
| `t2s/prompts.py` | Every prompt and the structured-output schemas (answer / clarify / refuse) |
| `t2s/assistant.py` | Pipeline: context -> LLM decision -> safe execution -> repair / empty-retry -> one-line answer |
| `t2s/llm.py` | Provider wrapper (Anthropic, forced tool call -> validated JSON) + scripted fake for tests |
| `eval/testset_src.py` -> `eval/test_set.json` | 55 questions + reference SQL (+ expected behaviour) |
| `eval/compare.py` | Result comparison (order / rounding / extra columns) + heuristic failure classifier |
| `eval/run_eval.py`, `eval/systems.py` | Runs system variants x repeats, scores them |
| `eval/analyze.py` | Accuracy tables, failure taxonomy, McNemar + bootstrap + run-to-run noise |
| `eval/simulated_llm.py` | Fake model used ONLY to self-test the harness (`--simulated`); never a result |

## Rebuilding the test set
`python -m eval.build_testset` re-runs every reference query against the DB (through the same safe executor), prints
the results for eyeballing and rewrites `eval/test_set.json`; `python -m eval.make_appendix` regenerates the appendix.
