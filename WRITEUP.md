# Reliable Text-to-SQL Assistant: approach, evaluation, limitations

**Stack:** Python, SQLite (Chinook v1.4.5, 11 tables), Azure OpenAI API ('gpt-4.1-mini', temperature 0)/Gemini free API with gemini-flash-lite-latest, sqlglot. Model name is config (`T2SQL_MODEL`).
**Azure quota problems pushed me to Gemini’s free tier**

## 1. Approach and reasoning

`question -> schema context -> one LLM decision {answer | clarify | refuse} -> SafeDB (4 layers) -> on error: repair loop (max 2) -> table + one-line answer`

| Choice | What I did and why | Alternatives considered |
|---|---|---|
| **Schema context** | Full schema (~1.1k tokens) generated from `PRAGMA` (never stale) plus **exact value lists** for low-cardinality columns (so `'Hip Hop/Rap'`, not `'Hip-Hop'`), a short **glossary** of what DDL can't say (revenue = `UnitPrice*Quantity`, dates are text, no cost/payment/streaming data, join paths) and 5 fixed few-shots, none in the test set. 11 tables fit easily, so retrieval would only add a way to be wrong. | **Retrieved schema** (BM25, top-3 tables + FK shortest-path bridge tables) is built and measured as system C; it matters for 100+ table schemas. Dynamic few-shot: needs a labelled corpus. |
| **Read-only, bounded** | The prompt is not a control. `safe_db.py`: (1) sqlglot AST: one SELECT/CTE statement, table allow-list, no write nodes even inside CTEs; (2) connection opened `mode=ro` + `query_only`; (3) SQLite **authorizer** allow-listing operations, tables and functions (denies ATTACH, PRAGMA, `load_extension`, `zeroblob`, DDL/DML); (4) wall-clock timeout (kills cross joins / endless recursive CTEs), size limits, 200-row cap. Tests confirm layers 2+3 block writes even with layer 1 bypassed. A model-written write is refused, never "repaired". | Keyword blocklist (bypassable); read-only DB user (SQLite has none); container sandbox (stronger, more infra; listed under next steps). |
| **Failure handling** | *Invalid SQL*: DB error fed back, max 2 retries, then a plain-language failure. *Empty result*: reported honestly; optional one-shot filter re-check (system D). *Ambiguity*: ONE clarifying question with 2-4 options when a key parameter is missing and readings differ materially; prompt conventions (spend = revenue, "last year") prevent over-asking. *Out of scope / data not present / writes / injection*: refuse, no SQL runs. API outage returns a message, not a crash. | Separate router model: more latency/cost and can't use schema knowledge to judge answerability. |
| **Dates** | Data ends 2013-12-22, so "last year" = 2012 (computed from the DB, echoed in assumptions). | Wall-clock "last year" returns nothing. |

## 2. Evaluation

**Test set (55, appendix):** 12 easy, 15 medium, 14 hard (4-5 table joins, shares, `LAG`, nth-per-group, anti-joins with NULL traps, set logic, self-join), 6 to **clarify**, 8 to **refuse** (weather, trivia, missing data such as profit/unpaid/streams, destructive requests, injection). Every reference query was run and eyeballed; top-N cut-offs were checked for ties. An *oracle* (reference SQL through the pipeline) must score 100% and a junk model <10%: both are unit tests, so the harness can pass and fail.

**Correctness = result comparison, not SQL text.** Column names/order ignored; extra columns allowed, missing fail; row order ignored unless the question is explicitly ranked (tied rows may permute); row count exact; numbers within 0.011, with a *lenient* 1-dp band reported separately (my first, relative band forgave a wrong total; a unit test caught it); text case-sensitive; NULL = NULL only. Clarify/refuse pass only if the system took that action and ran no SQL.

**Failure taxonomy** (auto-labelled by structural SQL diff, spot-checked in `failure_review.csv`): wrong table / join / filter / aggregation / ordering-limit / output columns, rounding, no valid SQL, over-clarification, over-refusal, failed-to-clarify/refuse.

**Systems (one switch each):** A full schema, no repair; B = A + repair; C = B with retrieved schema.Run-to-run noise was not measured. A difference counts as real only if the paired McNemar p<0.05, the paired-bootstrap 95% CI excludes 0, **and** the gap exceeds the run-to-run spread of the same system.

### Results

> **From `python -m eval.run_eval && python -m eval.analyze`.**

Model: gemini-flash-lite-latest (an alias, so results may not reproduce exactly later), temperature 0, run on 4.11.2026.
Each system was run ONCE on the 55-question set, so run-to-run noise was not measured.

| System | Overall | Easy | Medium | Hard | Clarify | Refuse | Input tokens/q |
|---|---|---|---|---|---|---|---|
| A full schema, no repair | 100.0% (55/55) | 100% | 100% | 100% | 100% | 100% | 2,523 |
| B full schema + repair | 98.2% (54/55) | 100% | 100% | 92.9% | 100% | 100% | 2,574 |
| C retrieved schema + repair | 98.2% (54/55) | 100% | 93.3% | 100% | 100% | 100% | 1,858 |

**Failures.** B failed H08 and C failed M09. On manual review neither is clearly wrong. For H08 the model also used December 2012 so that January has a change value (0.0), where the reference gives NULL; every other month is identical.
For M09 every value was correct but the full name came back as two columns. The automatic label for M09 (wrong_ordering_or_limit) was wrong; I corrected it to an output-shape difference, which shows the labels are only heuristic. If both answers are accepted, all three systems score 100%; this is a post-hoc judgment and strict scores are the headline.

**Is the difference real?** No. A to B: -1.8 points, 95% bootstrap CI [-5.5, 0.0], McNemar p = 1.0 (A better on 1 question, B on 0). B to C: 0.0 points, CI [-5.5, +5.5], p = 1.0 (each better on 1). Differences of one question are well inside chance variation at n = 55, so I do not claim that error correction or schema retrieval changes accuracy.
What is measurable is cost: retrieval used about 26% fewer input tokens at similar accuracy.

**What this does and does not show.** Scores are near the ceiling, so this test set cannot separate the versions; a harder set is needed to do so. The repair loop was needed on only about one question (B made 1.02 model calls per question), so A versus B had almost no chance to differ. The sandbox was never triggered because the model refused the write requests itself; its protection is evidenced by the unit tests, not by this run.

**A defect found by the evaluation.** My first run scored A 96.4%, B 94.5%, C 94.5%. Reviewing its failures showed that my validator rejected valid INTERSECT/EXCEPT queries and mislabelled them as write attempts (H07, H10), that two C failures were API errors, and that M08 was ambiguous about the quarter label (models returned Q1-Q4, the reference returned 1-4). I fixed the validator and added regression tests, labelled API failures separately, and reworded M08.
Because the M08 change was made after seeing results, I would treat it as a disclosed adjustment. The first-run files are kept in results_run1_before_fix.

Top failure types: _. A->B: Δ=_ (CI _, p=_, run-to-run spread _) => real / noise. B->C: _.

## 3. Assumptions and limitations

- **Small single-author set**: n=55 gives wide intervals (one 80% run is about 68-88%); clarify (6) and refuse (8) are only a coarse signal. I wrote both system and tests, so bias is possible; held-out demo questions are the real check.
- Defaults for revenue and "last year = 2012" are my convention, stated in the prompt.
- Failure labels are heuristic; subtle logic errors fall into "other".
- The one-line answer is constrained to the rows (template fallback) but not evaluated.
- Safety tested against a fixed attack list, not fuzzed; in-process SQLite limits are not multi-tenant isolation.
- Clarification is single-turn (the reply is appended to the question).

## 4. With more time

1. A harder, larger test set, plus repeats to measure noise, like 150+ questions from several authors with paraphrase groups; more repeats.
2. Execution-guided self-consistency (sample k SQLs, vote on results) to attack *silent* errors.
3. Automatic faithfulness check for the answer sentence.
4. Open-weight local model via the same interface; cost/accuracy trade-off.
5. Container/subprocess execution, rate limits, audit logging.
6. RAG concept for schema based retrieval for a database with more number of tables.
