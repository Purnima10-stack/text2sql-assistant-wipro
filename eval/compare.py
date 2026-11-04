"""
compare.py -- decides "is the model's answer correct?" by comparing RESULTS, never SQL text.

Comparison rules (all deliberate, all documented in the write-up):
  1. Column NAMES are ignored (aliases are arbitrary).  Column ORDER is ignored: we search for a mapping
     from every reference column to a distinct predicted column whose values agree.
  2. EXTRA predicted columns are allowed (e.g. model also returns the country) -- they do not change the
     answer to the question. MISSING columns fail.
  3. ROW ORDER is ignored (multiset comparison) unless the question is marked `ordered` (explicit ranking
     / sorted output). For ordered questions, order must match, EXCEPT inside runs of reference rows that
     tie on every numeric column (a legitimate tie can be ordered either way).
  4. Row COUNT must match exactly: extra or missing rows fail (this catches missing LIMIT, wrong filters).
  5. NUMBERS: strict tolerance is |a-b| <= 0.011 (+1e-4 relative) -- absorbs 2-dp rounding but not a real
     off-by-one invoice. A second, LENIENT tolerance (|a-b| <= 0.051, i.e. 1-dp rounding) is also computed;
     results that pass only leniently are tagged `rounding_precision` so we can report strict AND lenient
     accuracy. (A relative lenient band was tried first and rejected: 0.5% would have forgiven a genuinely
     wrong total such as 2331.0 vs 2328.6 -- caught by a unit test.)
  6. Numeric-looking strings are treated as numbers ('2012' == 2012); text is compared exactly after
     trimming (case-sensitive: 'rock' != 'Rock' is a real error in a business setting).
  7. NULL matches NULL only.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

STRICT_ABS, STRICT_REL = 0.011, 1e-4
LENIENT_ABS = 0.051      # absorbs rounding to 1 dp (or unrounded vs rounded) -- nothing bigger

_num_re = re.compile(r"^-?\d+(\.\d+)?$")


# ---------------------------------------------------------------- cell normalisation / equality
def norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    s = str(v).strip()
    return float(s) if _num_re.match(s) else s


def cell_eq(a: Any, b: Any, lenient: bool = False) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float):
        if lenient:
            return abs(a - b) <= LENIENT_ABS + STRICT_REL * abs(b)
        return abs(a - b) <= STRICT_ABS + STRICT_REL * abs(b)
    if isinstance(a, float) != isinstance(b, float):
        return False
    return a == b


def row_eq(r1, r2, lenient=False) -> bool:
    return len(r1) == len(r2) and all(cell_eq(x, y, lenient) for x, y in zip(r1, r2))


def _sort_key(row):
    # type-safe key: (is_null, is_text, number-rounded-to-1dp, text)
    return tuple((v is None, isinstance(v, str), round(v, 1) if isinstance(v, float) else 0.0,
                  v if isinstance(v, str) else "") for v in row)


def multiset_eq(rows_a: list[tuple], rows_b: list[tuple], lenient=False) -> bool:
    """Tolerant multiset equality (greedy matching; n <= 200 so O(n^2) is fine)."""
    if len(rows_a) != len(rows_b):
        return False
    remaining = list(rows_b)
    for ra in sorted(rows_a, key=_sort_key):
        for i, rb in enumerate(remaining):
            if row_eq(ra, rb, lenient):
                remaining.pop(i)
                break
        else:
            return False
    return True


def _col(rows, j):
    return [(r[j],) for r in rows]


# ---------------------------------------------------------------- result comparison
@dataclass
class Outcome:
    match: bool                 # strict match
    lenient: bool               # match under lenient numeric tolerance
    reason: str                 # match | empty_mismatch | row_count | missing_columns | value_mismatch | order | rounding_precision
    extra_cols: int = 0


def _try_match(ref, pred, ref_cols, pred_cols, ordered, lenient) -> tuple[bool, str, int]:
    """Search for a column mapping that makes the projected predicted rows equal the reference rows."""
    nref = len(ref[0]) if ref else len(ref_cols)
    npred = len(pred[0]) if pred else len(pred_cols)
    if nref > npred:
        return False, "missing_columns", 0
    # candidate predicted columns for each reference column (same value multiset)
    cands = []
    for j in range(nref):
        c = [k for k in range(npred) if multiset_eq(_col(ref, j), _col(pred, k), lenient)]
        if not c:
            return False, "value_mismatch", 0
        cands.append(c)
    saw_order_fail = False
    for assign in itertools.product(*cands):
        if len(set(assign)) != len(assign):
            continue
        proj = [tuple(r[k] for k in assign) for r in pred]
        if not multiset_eq(ref, proj, lenient):
            continue
        if ordered and not _order_ok(ref, proj, lenient):
            saw_order_fail = True
            continue
        return True, "match", npred - nref
    return False, ("order" if saw_order_fail else "value_mismatch"), 0


def _order_ok(ref, proj, lenient) -> bool:
    """Positions must agree, except inside runs of reference rows that tie on all numeric columns."""
    num_cols = [j for j in range(len(ref[0])) if all(isinstance(r[j], float) for r in ref)]
    i = 0
    while i < len(ref):
        k = i + 1
        if num_cols:
            while k < len(ref) and all(cell_eq(ref[k][j], ref[i][j], lenient) for j in num_cols):
                k += 1
        if not multiset_eq(ref[i:k], proj[i:k], lenient):
            return False
        i = k
    return True


def compare_results(ref_cols, ref_rows, pred_cols, pred_rows, ordered=False) -> Outcome:
    ref = [tuple(norm(v) for v in r) for r in ref_rows]
    pred = [tuple(norm(v) for v in r) for r in pred_rows]
    if not ref and not pred:
        return Outcome(True, True, "match")
    if bool(ref) != bool(pred):
        return Outcome(False, False, "empty_mismatch")
    if len(ref) != len(pred):
        return Outcome(False, False, "row_count")
    ok, reason, extra = _try_match(ref, pred, ref_cols, pred_cols, ordered, lenient=False)
    if ok:
        return Outcome(True, True, "match", extra)
    ok2, reason2, extra2 = _try_match(ref, pred, ref_cols, pred_cols, ordered, lenient=True)
    if ok2:
        return Outcome(False, True, "rounding_precision", extra2)
    return Outcome(False, False, reason)


# ---------------------------------------------------------------- failure classification (heuristic)
def _parse(sql: str):
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return None


def _signature(sql: str) -> dict | None:
    """Structural fingerprint of a query, used to label *why* a wrong query is wrong."""
    t = _parse(sql)
    if t is None:
        return None
    ctes = {c.alias_or_name.lower() for c in t.find_all(exp.CTE)}
    tables = {x.name.lower() for x in t.find_all(exp.Table) if x.name and x.name.lower() not in ctes}

    # join signature: unordered pair of column names per ON equality + join kind
    joins = set()
    for j in t.find_all(exp.Join):
        kind = (j.side or "").upper() or "INNER"
        on = j.args.get("on")
        pairs = set()
        if on is not None:
            for eq in on.find_all(exp.EQ):
                l, r = eq.left, eq.right
                if isinstance(l, exp.Column) and isinstance(r, exp.Column):
                    pairs.add(frozenset({l.name.lower(), r.name.lower()}))
        for u in (j.args.get("using") or []):
            pairs.add(frozenset({u.name.lower()}))
        joins.add((kind, frozenset(pairs)))
    join_pairs = {p for _, ps in joins for p in ps}
    join_kinds = {k for k, _ in joins}

    # filter signature: literals and columns used inside WHERE / HAVING
    lits, fcols = set(), set()
    for clause in list(t.find_all(exp.Where)) + list(t.find_all(exp.Having)):
        for lit in clause.find_all(exp.Literal):
            lits.add(str(lit.this).lower().strip("%"))
        for c in clause.find_all(exp.Column):
            fcols.add(c.name.lower())
    # join predicates written in WHERE (comma joins) should not count as filters
    aggs = {type(a).__name__ for a in t.find_all(exp.AggFunc)}
    windows = bool(list(t.find_all(exp.Window)))
    group = {g.sql().lower() for gb in t.find_all(exp.Group) for g in gb.expressions}
    order = [(o.args.get("desc") is True) for ob in t.find_all(exp.Order) for o in ob.expressions]
    limit = {str(l.expression.this) for l in t.find_all(exp.Limit)}
    return dict(tables=tables, join_pairs=join_pairs, join_kinds=join_kinds, lits=lits, fcols=fcols,
                aggs=aggs, windows=windows, group=group, order=order, limit=limit)


def classify_failure(pred_sql: str | None, ref_sql: str, outcome_reason: str, status: str) -> str:
    """Label a failed ANSWERABLE question. Order of checks = specificity of the diagnosis."""
    if status in ("clarify", "refused"):
        return "over_clarification" if status == "clarify" else "over_refusal"
    if status == "error" or not pred_sql:
        return "no_valid_sql"
    if outcome_reason == "rounding_precision":
        return "rounding_precision"
    p, r = _signature(pred_sql), _signature(ref_sql)
    if p is None or r is None:
        return "other"
    if p["tables"] != r["tables"]:
        return "wrong_table"
    if p["join_pairs"] != r["join_pairs"] or (p["join_kinds"] != r["join_kinds"]):
        return "wrong_join"
    if p["lits"] != r["lits"] or p["fcols"] != r["fcols"]:
        return "wrong_filter"
    if p["aggs"] != r["aggs"] or p["windows"] != r["windows"] or p["group"] != r["group"]:
        return "wrong_aggregation"
    if outcome_reason in ("order", "row_count") or p["order"] != r["order"] or p["limit"] != r["limit"]:
        return "wrong_ordering_or_limit"
    if outcome_reason == "missing_columns":
        return "wrong_output_columns"
    return "other_logic"
