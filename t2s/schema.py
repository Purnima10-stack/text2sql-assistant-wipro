"""
schema.py -- decides WHAT the model sees about the database.

Three ingredients, all built automatically from the live DB (so nothing goes stale) plus a small
hand-written glossary for the things a schema cannot tell you:

  1. Compact DDL  : tables, columns, types, PK/FK (read via PRAGMA, not hand-typed).
  2. Value hints  : the actual values of low-cardinality columns (Genre.Name, MediaType.Name,
                    Customer.Country, Playlist.Name, Employee.Title). This prevents the most
                    common silent failure: filtering on 'Hip-Hop' when the data says 'Hip Hop/Rap'.
  3. Glossary     : business meaning that is NOT in the DDL ("revenue" = SUM(UnitPrice*Quantity)
                    on InvoiceLine; dates are 'YYYY-MM-DD HH:MM:SS' text; data ends 2013-12-22).

Two modes (compared in the evaluation):
  - "full"      : all 11 tables (~2.5k tokens). Chinook is tiny, so this is the default.
  - "retrieved" : BM25 over table docs -> top-k tables -> add the tables needed to JOIN them
                  (shortest FK path) -> render only those. Shown for when a schema has 100+ tables.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

# The dataset has no "now": we anchor relative dates ("last year") to the newest invoice.
# Computed from the DB at load time (see SchemaInfo.as_of_date), not hard-coded.

# ----------------------------------------------------------------------------------------
# Hand-written glossary (what the DDL cannot express). Kept short on purpose.
# ----------------------------------------------------------------------------------------
TABLE_DESCRIPTIONS = {
    "Customer": "Customers (buyers). Country/City are the customer's home location. SupportRepId -> the Employee who serves them.",
    "Invoice": "One row per purchase (order header). Total = amount billed. BillingCountry/BillingCity are where it was billed (can differ from Customer.Country).",
    "InvoiceLine": "One row per track bought on an invoice (order line). Line revenue = UnitPrice * Quantity. This is the ONLY link between purchases and tracks/genres/artists.",
    "Track": "A song/video that can be sold. Belongs to an Album (nullable), a Genre (nullable) and a MediaType. Milliseconds = duration. Composer is free text.",
    "Album": "A music album; each album has exactly one Artist.",
    "Artist": "A music artist/band (Name).",
    "Genre": "Music genre of a track (Rock, Jazz, Hip Hop/Rap, ...).",
    "MediaType": "File format of a track (e.g. 'MPEG audio file', 'AAC audio file').",
    "Playlist": "A named playlist. Some names repeat (e.g. 'Music' appears twice with different PlaylistIds).",
    "PlaylistTrack": "Bridge table: which tracks are in which playlists (many-to-many).",
    "Employee": "Company staff. ReportsTo -> manager (self-join). Employees with Title 'Sales Support Agent' are customers' support reps.",
}

GLOSSARY = """\
- Revenue / sales / spend = SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity), or SUM(Invoice.Total) when no track/genre/artist detail is needed. The two agree.
- Dates are TEXT 'YYYY-MM-DD HH:MM:SS'. Use strftime('%Y', InvoiceDate) for year, or range filters like InvoiceDate >= '2012-01-01' AND InvoiceDate < '2013-01-01'.
- The data covers 2009-01-01 to {as_of}. Treat {as_of_year} as 'this year' and {last_year} as 'last year' (the dataset has no live clock).
- 'Country of a customer' = Customer.Country. Use Invoice.BillingCountry only when the question is about where an invoice was billed.
- There is NO payment-status, cost/profit, stock, rating, listening/streaming, or customer-age data.
- Join paths: Customer-Invoice-InvoiceLine-Track-(Album-Artist | Genre | MediaType); Playlist-PlaylistTrack-Track; Customer.SupportRepId-Employee.EmployeeId; Employee.ReportsTo-Employee.EmployeeId.
- Use LEFT JOIN when the question asks about things with NONE of something (e.g. tracks never sold).
- SQLite dialect only: no ILIKE, no DATE_TRUNC, no EXTRACT, no FILTER on non-aggregates; integer division truncates (multiply by 1.0 for ratios).
"""

# Columns worth showing example values for (cardinality is small and stable).
VALUE_HINT_COLUMNS = [
    ("Genre", "Name"), ("MediaType", "Name"), ("Customer", "Country"),
    ("Employee", "Title"), ("Playlist", "Name"), ("Invoice", "BillingCountry"),
]
VALUE_HINT_MAX = 40


@dataclass
class Column:
    name: str
    type: str
    pk: bool
    notnull: bool


@dataclass
class Table:
    name: str
    columns: list[Column]
    fks: list[tuple[str, str, str]]     # (column, ref_table, ref_column)
    description: str = ""


class SchemaInfo:
    """Introspects the DB once and renders prompt text on demand."""

    def __init__(self, db_path: str | Path):
        conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        self.tables: dict[str, Table] = {}
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for n in names:
            cols = [Column(r[1], r[2], bool(r[5]), bool(r[3])) for r in conn.execute(f"PRAGMA table_info([{n}])")]
            fks = [(r[3], r[2], r[4]) for r in conn.execute(f"PRAGMA foreign_key_list([{n}])")]
            self.tables[n] = Table(n, cols, fks, TABLE_DESCRIPTIONS.get(n, ""))
        # value hints
        self.value_hints: dict[tuple[str, str], list[str]] = {}
        for t, c in VALUE_HINT_COLUMNS:
            vals = [str(r[0]) for r in conn.execute(
                f"SELECT DISTINCT [{c}] FROM [{t}] WHERE [{c}] IS NOT NULL ORDER BY [{c}]")]
            if len(vals) <= VALUE_HINT_MAX:
                self.value_hints[(t, c)] = vals
        # dataset 'now'
        self.as_of_date = conn.execute("SELECT substr(MAX(InvoiceDate),1,10) FROM Invoice").fetchone()[0]
        conn.close()
        self.as_of_year = int(self.as_of_date[:4])
        self._build_graph()
        self._build_bm25()

    # ---------------------------------------------------------------- rendering
    def glossary(self) -> str:
        return GLOSSARY.format(as_of=self.as_of_date, as_of_year=self.as_of_year, last_year=self.as_of_year - 1)

    def render(self, table_names: list[str] | None = None) -> str:
        """Compact DDL-like text for the chosen tables (all by default)."""
        names = table_names or list(self.tables)
        out = []
        for n in names:
            t = self.tables[n]
            cols = []
            for c in t.columns:
                flag = " PK" if c.pk else ""
                cols.append(f"{c.name} {c.type.split('(')[0].upper() or 'TEXT'}{flag}")
            fk = "; ".join(f"{c}->{rt}.{rc}" for c, rt, rc in t.fks)
            out.append(f"TABLE {n}: {t.description}\n  cols: {', '.join(cols)}" + (f"\n  FK: {fk}" if fk else ""))
        hints = []
        for (t, c), vals in self.value_hints.items():
            if t in names:
                hints.append(f"- {t}.{c} values: {' | '.join(vals)}")
        text = "\n".join(out)
        if hints:
            text += "\n\nEXACT VALUES (use these literals verbatim in filters):\n" + "\n".join(hints)
        return text

    # ---------------------------------------------------------------- retrieval
    def _build_graph(self) -> None:
        self.graph: dict[str, set[str]] = defaultdict(set)
        for t in self.tables.values():
            for _, rt, _ in t.fks:
                self.graph[t.name].add(rt)
                self.graph[rt].add(t.name)

    STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "to", "by", "and", "or", "is", "are", "was", "were",
                 "which", "what", "who", "whom", "how", "many", "much", "most", "least", "top", "each", "per",
                 "with", "has", "have", "had", "from", "that", "than", "all", "any", "show", "list", "give",
                 "me", "our", "we", "do", "does", "did", "name", "number", "total", "average", "year"}

    @classmethod
    def _tok(cls, text: str) -> list[str]:
        # split camelCase / punctuation, lower-case, drop stopwords, crude plural stripping
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        toks = re.findall(r"[a-z0-9]+", text.lower())
        toks = [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in toks]
        return [t for t in toks if t not in cls.STOPWORDS]

    def _build_bm25(self) -> None:
        # one "document" per table: name (x3 weight), description, column names, example values
        self.docs: dict[str, list[str]] = {}
        for n, t in self.tables.items():
            words = self._tok(n) * 3 + self._tok(t.description)
            for c in t.columns:
                words += self._tok(c.name)
            for (tn, cn), vals in self.value_hints.items():
                if tn == n:
                    for v in vals:
                        words += self._tok(v)
            self.docs[n] = words
        self.avgdl = sum(len(d) for d in self.docs.values()) / len(self.docs)
        df: dict[str, int] = defaultdict(int)
        for d in self.docs.values():
            for w in set(d):
                df[w] += 1
        N = len(self.docs)
        self.idf = {w: math.log(1 + (N - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def _bm25(self, q: list[str], doc: list[str], k1=1.5, b=0.75) -> float:
        score, dl = 0.0, len(doc)
        for w in set(q):
            f = doc.count(w)
            if f:
                score += self.idf.get(w, 0) * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / self.avgdl))
        return score

    def _join_path(self, a: str, b: str) -> list[str]:
        """Shortest FK path between two tables (BFS). Used so retrieval never drops a bridge table."""
        prev, dq, seen = {a: None}, deque([a]), {a}
        while dq:
            cur = dq.popleft()
            if cur == b:
                break
            for nb in self.graph[cur]:
                if nb not in seen:
                    seen.add(nb); prev[nb] = cur; dq.append(nb)
        path, cur = [], b
        while cur is not None:
            path.append(cur); cur = prev.get(cur)
        return path

    def retrieve(self, question: str, top_k: int = 3) -> list[str]:
        q = self._tok(question)
        scored = sorted(((self._bm25(q, d), n) for n, d in self.docs.items()), reverse=True)
        picked = [n for s, n in scored[:top_k] if s > 0] or [n for _, n in scored[:top_k]]
        # Connect the picked tables via FK paths (adds e.g. InvoiceLine between Invoice and Track)
        keep = set(picked)
        for i in range(len(picked)):
            for j in range(i + 1, len(picked)):
                keep.update(self._join_path(picked[i], picked[j]))
        return [n for n in self.tables if n in keep]   # stable order

    def context_for(self, question: str, mode: str = "full", top_k: int = 3) -> tuple[str, list[str]]:
        """Return (schema_text, table_names_used)."""
        if mode == "full":
            names = list(self.tables)
        elif mode == "retrieved":
            names = self.retrieve(question, top_k)
        else:
            raise ValueError(mode)
        return self.render(names), names


if __name__ == "__main__":      # quick manual check:  python -m t2s.schema
    s = SchemaInfo(Path(__file__).parent.parent / "data" / "Chinook_Sqlite.sqlite")
    print(s.glossary())
    for q in ["Which 5 customers spent the most in 2012?", "revenue share by genre", "Which artist has the most albums?",
              "How many tracks are in the Grunge playlist?", "Which employee supports the most customers?"]:
        print(q, "->", s.retrieve(q))
