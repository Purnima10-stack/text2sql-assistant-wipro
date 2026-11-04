"""
assistant.py -- the end-to-end pipeline.

  question
     |
     v
 [1] build schema context (full | retrieved)
     |
     v
 [2] LLM call -> {answer | clarify | refuse}           (clarify/refuse stop here: NO SQL runs)
     |  answer
     v
 [3] SafeDB.execute(sql)   <-- all four safety layers (see safe_db.py)
     |-- error --> [4] repair loop (optional, max N): feed the error back, ask for fixed SQL
     |-- 0 rows -> [5] one "empty-result sanity check" retry (optional)
     v
 [6] one-sentence plain-English answer (LLM, grounded in the returned rows only)
     v
 Response(sql, table, answer, assumptions, status, attempt log, token usage)

Every switch that matters for the evaluation lives in `Config`, so "system A vs system B" is just two
Config objects run through the same code path.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import prompts
from .llm import LLM
from .safe_db import ForbiddenOperation, QueryResult, QueryTimeout, SafeDB, UnsafeQueryError
from .schema import SchemaInfo

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "Chinook_Sqlite.sqlite"


@dataclass
class Config:
    schema_mode: str = "full"        # "full" | "retrieved"
    top_k_tables: int = 3            # only used when schema_mode == "retrieved"
    repair: bool = True              # automatic error correction loop
    max_repairs: int = 2
    empty_retry: bool = False        # one extra check when a query returns 0 rows
    summarize: bool = True           # LLM one-line answer (switched off in eval: it does not affect SQL)
    label: str = "default"           # name used in reports


@dataclass
class Response:
    question: str
    status: str                      # answered | empty | clarify | refused | error
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    answer: str = ""                 # one-line plain-English answer OR the clarifying question / refusal text
    assumptions: str = ""
    options: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)    # audit trail: each SQL tried + its error
    tables_in_context: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    llm_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Assistant:
    def __init__(self, llm: LLM, config: Config | None = None, db_path: str | Path = DEFAULT_DB):
        self.llm = llm
        self.cfg = config or Config()
        self.db = SafeDB(db_path)
        self.schema = SchemaInfo(db_path)
        # The system prompt (with FULL schema) is static -> build once, benefit from prompt caching.
        self._full_system = prompts.SYSTEM_TEMPLATE.format(
            schema=self.schema.render(), glossary=self.schema.glossary())

    # ------------------------------------------------------------------ helpers
    def _system_for(self, question: str) -> tuple[str, list[str]]:
        if self.cfg.schema_mode == "full":
            return self._full_system, list(self.schema.tables)
        text, names = self.schema.context_for(question, "retrieved", self.cfg.top_k_tables)
        return prompts.SYSTEM_TEMPLATE.format(schema=text, glossary=self.schema.glossary()), names

    def _call(self, resp: Response, system: str, user: str, tool: dict) -> dict:
        out = self.llm.complete(system, user, tool)
        resp.llm_calls += 1
        resp.input_tokens += out.input_tokens
        resp.output_tokens += out.output_tokens
        return out.data

    # ------------------------------------------------------------------ main entry
    def ask(self, question: str) -> Response:
        t0 = time.monotonic()
        resp = Response(question=question, status="error")
        question = question.strip()
        if not question:
            resp.answer = "Please type a question."
            return resp
        if len(question) > 1000:                       # cheap input bound (cost + injection surface)
            resp.answer = "Question is too long (limit 1000 characters)."
            return resp

        system, resp.tables_in_context = self._system_for(question)
        try:
            decision = self._call(resp, system, prompts.user_prompt(question), prompts.RESPOND_TOOL)
        except Exception as e:                         # network / API failure must not crash the app
            resp.answer = f"The language model call failed ({type(e).__name__}: {str(e)[:300]}). Please try again."
            resp.latency_s = time.monotonic() - t0
            return resp

        repairs_left = self.cfg.max_repairs if self.cfg.repair else 0
        empty_retry_left = 1 if self.cfg.empty_retry else 0

        while True:
            action = decision.get("action")
            resp.assumptions = decision.get("assumptions", "") or resp.assumptions

            # ---- clarify / refuse: stop, never touch the DB
            if action == "clarify":
                resp.status = "clarify"
                resp.sql = None
                resp.options = list(decision.get("options") or [])
                resp.answer = decision.get("clarifying_question") or "Could you clarify what you mean?"
                break
            if action == "refuse":
                resp.status = "refused"
                resp.sql = None
                resp.answer = decision.get("refusal_reason") or "Sorry, I can't help with that."
                break
            if action != "answer" or not decision.get("sql"):
                resp.status, resp.answer = "error", "The model returned an unusable response."
                break

            # ---- answer: run the SQL through the safety layer
            sql = decision["sql"]
            resp.sql = sql
            attempt: dict[str, Any] = {"sql": sql}
            resp.attempts.append(attempt)
            try:
                result: QueryResult = self.db.execute(sql)
                attempt["rows"] = len(result.rows)
            except ForbiddenOperation as e:
                # The model tried to write/alter: never "repair" this, refuse outright.
                attempt["error"] = str(e)
                resp.status, resp.sql, resp.rows, resp.columns = "refused", None, [], []
                resp.answer = ("I can only read data, not change it. "
                               "Ask me a question about the data and I'll look it up.")
                break
            except (UnsafeQueryError, QueryTimeout, sqlite3.Error) as e:
                attempt["error"] = f"{type(e).__name__}: {e}"
                if repairs_left > 0:
                    repairs_left -= 1
                    decision = self._safe_call(resp, system,
                                               prompts.repair_prompt(question, sql, attempt["error"]))
                    if decision is None:
                        break
                    continue
                resp.status = "error"
                resp.answer = ("I couldn't produce a valid query for that question "
                               f"({type(e).__name__}). Try rephrasing it.")
                break

            # ---- success path
            resp.columns, resp.rows, resp.truncated = result.columns, result.rows, result.truncated
            if not result.rows:
                if empty_retry_left > 0:
                    empty_retry_left -= 1
                    new = self._safe_call(resp, system, prompts.empty_retry_prompt(question, sql))
                    if new is None:
                        break
                    if new.get("action") == "answer" and (new.get("sql") or "").strip() != sql.strip():
                        decision = new
                        continue                       # re-run the corrected SQL
                resp.status = "empty"
                resp.answer = "The query ran correctly but no rows matched. Nothing in the data fits that question."
                break
            resp.status = "answered"
            resp.answer = self._summarize(resp, question) if self.cfg.summarize else ""
            break

        resp.latency_s = time.monotonic() - t0
        return resp

    # ------------------------------------------------------------------ small pieces
    def _safe_call(self, resp: Response, system: str, user: str) -> dict | None:
        try:
            return self._call(resp, system, user, prompts.RESPOND_TOOL)
        except Exception as e:
            resp.status, resp.answer = "error", f"The language model call failed ({type(e).__name__}: {str(e)[:300]})."
            return None

    def _summarize(self, resp: Response, question: str) -> str:
        """One-line answer grounded in the rows. Falls back to a deterministic sentence if the LLM fails."""
        try:
            data = self._call(resp, self._full_system,
                              prompts.summary_prompt(question, resp.columns, resp.rows, len(resp.rows),
                                                     resp.truncated, resp.assumptions),
                              prompts.SUMMARY_TOOL)
            return data.get("answer", "").strip() or self._fallback_answer(resp)
        except Exception:
            return self._fallback_answer(resp)

    @staticmethod
    def _fallback_answer(resp: Response) -> str:
        if len(resp.rows) == 1 and len(resp.columns) == 1:
            return f"{resp.columns[0]}: {resp.rows[0][0]}"
        return f"Returned {len(resp.rows)} row(s); see the table."
