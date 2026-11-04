"""The system variants that are compared. Each is just a Config -> identical code path, one switch changed."""
from t2s.assistant import Config

SYSTEMS = {
    # A: the simplest sensible system. Full schema, no automatic error correction.
    "A_full_norepair": Config(schema_mode="full", repair=False, summarize=False, label="A_full_norepair"),
    # B: A + automatic error correction (feed DB error back, up to 2 retries).  A-vs-B isolates the repair loop.
    "B_full_repair": Config(schema_mode="full", repair=True, max_repairs=2, summarize=False, label="B_full_repair"),
    # C: B but with BM25-retrieved tables (+ FK path expansion). B-vs-C isolates the schema-context choice.
    "C_retrieved_repair": Config(schema_mode="retrieved", top_k_tables=3, repair=True, max_repairs=2,
                                 summarize=False, label="C_retrieved_repair"),
    # D (optional): B + one empty-result sanity retry.
    "D_full_repair_emptyretry": Config(schema_mode="full", repair=True, max_repairs=2, empty_retry=True,
                                       summarize=False, label="D_full_repair_emptyretry"),
}
