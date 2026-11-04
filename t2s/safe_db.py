"""
safe_db.py -- the SAFETY LAYER. Nothing the LLM writes reaches SQLite except through here.

Design principle: the prompt is NOT a security boundary. We enforce "read-only, bounded"
with four independent layers, so that any single one failing does not compromise data:

  Layer 1  (static, friendly errors)  sqlglot AST check: exactly one statement, must be a
           SELECT / WITH...SELECT / UNION, only known tables. Gives the repair loop a clear
           message and stops obviously bad SQL before it touches the DB.
  Layer 2  (OS / SQLite file level)   connection opened with `file:...?mode=ro` URI plus
           `PRAGMA query_only=ON`. Even if layers 1 and 3 were bypassed, SQLite itself
           refuses to write.
  Layer 3  (SQLite authorizer)        `set_authorizer` callback runs at statement-PREPARE
           time for every operation SQLite is about to perform. We allow-list: SELECT,
           READ of whitelisted tables, a whitelist of functions. ATTACH, PRAGMA, INSERT,
           UPDATE, DELETE, DDL, load_extension, etc. are all denied.
  Layer 4  (resource limits)          progress-handler wall-clock timeout (kills runaway
           cross joins / recursive CTEs), SQLite length/depth limits, and a hard cap on
           rows fetched. Result: a bad query cannot hang the app or flood memory.

Alternatives considered: (a) regex/keyword blocklist -- trivially bypassed, rejected as the
only control; (b) a DB user with SELECT-only grants -- the right answer for Postgres/MySQL,
but SQLite has no users, so mode=ro + authorizer is the equivalent; (c) running in a
separate process/container -- stronger isolation, listed under "with more time".
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

# ----------------------------------------------------------------------------------------
# Policy constants
# ----------------------------------------------------------------------------------------
MAX_ROWS = 200            # rows returned to the user / evaluator (hard cap)
TIMEOUT_SECONDS = 5.0     # wall-clock budget per query
PROGRESS_STEP = 10_000    # SQLite VM instructions between timeout checks

# Tables the assistant may read. (Everything in Chinook; sqlite_master etc. are NOT allowed.)
ALLOWED_TABLES = {
    "album", "artist", "customer", "employee", "genre", "invoice",
    "invoiceline", "mediatype", "playlist", "playlisttrack", "track",
}

# Allow-list of SQL functions (lower-case). Anything else (load_extension, randomblob,
# zeroblob, writefile, readfile, ...) is rejected by the authorizer.
ALLOWED_FUNCTIONS = {
    # aggregates
    "count", "sum", "avg", "min", "max", "total", "group_concat",
    # scalar
    "abs", "round", "coalesce", "ifnull", "nullif", "iif", "length", "lower", "upper",
    "substr", "substring", "trim", "ltrim", "rtrim", "replace", "instr", "typeof",
    "printf", "format", "cast", "like", "glob", "unicode", "char", "hex", "quote",
    "likely", "unlikely", "sign",
    # date/time
    "date", "time", "datetime", "julianday", "strftime", "unixepoch",
    # window functions
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag",
    "lead", "first_value", "last_value", "nth_value",
    # math (present only if SQLite compiled with math functions; harmless if absent)
    "ceil", "ceiling", "floor", "sqrt", "pow", "power", "ln", "log", "exp", "mod",
}


class UnsafeQueryError(Exception):
    """Raised when a query is rejected by policy (not a normal SQL error)."""


class ForbiddenOperation(UnsafeQueryError):
    """A write / DDL / admin operation was attempted. NOT repairable: the assistant refuses."""


class QueryTimeout(Exception):
    """Raised when the wall-clock budget is exceeded."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False          # True if we hit MAX_ROWS
    elapsed_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------------------
# Layer 1: static AST validation (fast, friendly, fail-closed)
# ----------------------------------------------------------------------------------------
def static_validate(sql: str) -> str:
    """Return the cleaned SQL or raise UnsafeQueryError with an actionable message."""
    if not sql or not sql.strip():
        raise UnsafeQueryError("Empty SQL.")
    cleaned = sql.strip().rstrip(";").strip()
    try:
        statements = sqlglot.parse(cleaned, read="sqlite")
    except (ParseError, TokenError) as e:
        # Fail closed: if we cannot parse it we do not run it. The message goes to the repair loop.
        raise UnsafeQueryError(f"SQL could not be parsed: {str(e)[:200]}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeQueryError("Exactly one SQL statement is allowed (no ';' chaining).")
    tree = statements[0]

    # No write/DDL/admin nodes anywhere in the tree (catches `WITH x AS (...) DELETE ...`
    # and data-modifying statements hidden inside CTEs/subqueries). These are NOT repairable.
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter,
                 exp.Command, exp.Pragma, exp.Attach, exp.Detach, exp.Merge, exp.Copy,
                 exp.Transaction, exp.Commit, exp.Rollback, exp.Set)
    for node in tree.walk():
        if isinstance(node, forbidden):
            raise ForbiddenOperation(f"Forbidden operation: {type(node).__name__.upper()}.")

    # Root must be a read-only query. UNION / INTERSECT / EXCEPT share the SetOperation base class in current
    # sqlglot (older versions: Union only) -- accept all of them. BUG FIXED: an earlier version accepted only
    # Union, so valid INTERSECT/EXCEPT queries were rejected AND mislabelled as write attempts (found via
    # the evaluation: questions H07 and H10). An unrecognised root is a plain, repairable error.
    set_op = getattr(exp, "SetOperation", exp.Union)
    if not isinstance(tree, (exp.Select, set_op, exp.Subquery, exp.With)):
        raise UnsafeQueryError(f"Only SELECT queries are allowed (got {type(tree).__name__.upper()}).")

    # Table whitelist (CTE names are allowed because they are defined inside the query).
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if name and name not in ALLOWED_TABLES and name not in cte_names:
            raise UnsafeQueryError(
                f"Unknown or disallowed table '{t.name}'. "
                f"Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}."
            )
    return cleaned


# ----------------------------------------------------------------------------------------
# Layer 3: SQLite authorizer (runs inside SQLite at prepare time -- cannot be talked around)
# ----------------------------------------------------------------------------------------
def _authorizer(action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None):
    # Plain reads and SELECT statements
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:          # arg1 = table, arg2 = column, db = schema name
        if db is None:
            # Ephemeral objects (materialised CTEs / subqueries) have no schema; they can only
            # contain data already read through allow-listed tables, so this is safe.
            return sqlite3.SQLITE_OK
        if db == "main" and (arg1 or "").lower() in ALLOWED_TABLES:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:      # arg2 = function name
        if (arg2 or "").lower() in ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    # Everything else: INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/PRAGMA/ATTACH/TRANSACTION/...
    return sqlite3.SQLITE_DENY


# ----------------------------------------------------------------------------------------
# The executor
# ----------------------------------------------------------------------------------------
class SafeDB:
    def __init__(self, db_path: str | Path, max_rows: int = MAX_ROWS, timeout_s: float = TIMEOUT_SECONDS):
        self.db_path = Path(db_path).resolve()
        if not self.db_path.exists():
            raise FileNotFoundError(self.db_path)
        self.max_rows = max_rows
        self.timeout_s = timeout_s

    def _connect(self) -> sqlite3.Connection:
        # Layer 2: read-only at the file-open level (immutable=0 so WAL etc. still work for reads).
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")            # belt and braces
        # Layer 4: SQLite hard limits (bounds on SQL size, expression depth, blob size...)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 50)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)      # max string/blob bytes
        conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 10)
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        # Layer 3: install the authorizer AFTER our own PRAGMA (which it would deny).
        conn.set_authorizer(_authorizer)
        return conn

    def execute(self, sql: str) -> QueryResult:
        """Validate + run. Raises UnsafeQueryError, QueryTimeout, or sqlite3.Error."""
        cleaned = static_validate(sql)                    # Layer 1
        conn = self._connect()                            # Layers 2 + 3
        start = time.monotonic()

        def _progress() -> int:                           # Layer 4: timeout
            return 1 if (time.monotonic() - start) > self.timeout_s else 0

        conn.set_progress_handler(_progress, PROGRESS_STEP)
        try:
            cur = conn.execute(cleaned)                   # sqlite3 also refuses multi-statement
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(self.max_rows + 1)       # +1 so we can detect truncation
            truncated = len(rows) > self.max_rows
            return QueryResult(cols, [tuple(r) for r in rows[: self.max_rows]],
                               truncated, time.monotonic() - start)
        except sqlite3.DatabaseError as e:          # OperationalError is a subclass
            # progress handler abort surfaces as "interrupted"
            if "interrupt" in str(e).lower():
                raise QueryTimeout(f"Query exceeded {self.timeout_s}s budget.") from e
            if "not authorized" in str(e).lower():
                raise UnsafeQueryError(
                    "Query uses an operation, table or function that is not permitted "
                    f"(SQLite authorizer: {e})."
                ) from e
            raise
        finally:
            conn.close()
