"""The comparator is the foundation of the evaluation, so it gets its own tests."""
from eval.compare import compare_results, classify_failure

C = ["a", "b"]


def cmp(ref, pred, ordered=False, rc=C, pc=C):
    return compare_results(rc, ref, pc, pred, ordered)


def test_identical():
    assert cmp([("x", 1)], [("x", 1)]).match


def test_row_order_ignored_unless_ordered():
    ref, pred = [("x", 3), ("y", 2)], [("y", 2), ("x", 3)]
    assert cmp(ref, pred).match
    o = cmp(ref, pred, ordered=True)
    assert not o.match and o.reason == "order"


def test_ties_may_be_reordered_when_ordered():
    ref, pred = [("x", 3), ("y", 2), ("z", 2)], [("x", 3), ("z", 2), ("y", 2)]
    assert cmp(ref, pred, ordered=True).match            # y,z tie on the numeric column
    assert not cmp([("a", 3), ("y", 2)], [("y", 2), ("a", 3)], ordered=True).match   # no tie -> order matters


def test_column_names_and_order_ignored():
    assert compare_results(["n", "t"], [("x", 1.5)], ["total", "name"], [(1.5, "x")]).match


def test_extra_columns_allowed_missing_not():
    o = compare_results(["n"], [("x",)], ["n", "extra"], [("x", 9)])
    assert o.match and o.extra_cols == 1
    assert compare_results(["a", "b"], [("x", 1)], ["a"], [("x",)]).reason == "missing_columns"


def test_row_pairing_matters_not_just_columns():
    # same per-column values but rows paired differently => different answer
    assert not cmp([("x", 1), ("y", 2)], [("x", 2), ("y", 1)]).match


def test_rounding_tolerance_strict_vs_lenient():
    assert cmp([("x", 12.34)], [("x", 12.3401)]).match
    o = cmp([("x", 44.1)], [("x", 44.1176)])             # unrounded vs rounded: tiny but > strict tolerance
    assert not o.match and o.lenient and o.reason == "rounding_precision"
    assert not cmp([("x", 2328.6)], [("x", 2331.0)]).lenient   # a real discrepancy fails even leniently


def test_numeric_strings_equal_numbers():
    assert cmp([("2012", 1)], [(2012, 1)]).match


def test_case_sensitive_text_and_nulls():
    assert not cmp([("Rock", 1)], [("rock", 1)]).match
    assert cmp([("x", None)], [("x", None)]).match and not cmp([("x", None)], [("x", 0)]).match


def test_row_count_and_empty():
    assert cmp([("x", 1)], [("x", 1), ("y", 2)]).reason == "row_count"
    assert cmp([], []).match
    assert cmp([("x", 1)], []).reason == "empty_mismatch"


def test_failure_classifier_labels():
    ref = "SELECT SUM(il.UnitPrice*il.Quantity) FROM InvoiceLine il JOIN Track t ON t.TrackId=il.TrackId JOIN Genre g ON g.GenreId=t.GenreId WHERE g.Name='Rock'"
    cases = {
        "wrong_table": "SELECT SUM(Total) FROM Invoice",
        "wrong_filter": ref.replace("'Rock'", "'Jazz'"),
        "wrong_join": ref.replace("JOIN Genre g ON g.GenreId=t.GenreId", "LEFT JOIN Genre g ON g.GenreId=t.GenreId"),
        "wrong_aggregation": ref.replace("SUM(", "AVG("),
    }
    for label, sql in cases.items():
        assert classify_failure(sql, ref, "value_mismatch", "answered") == label, label
    assert classify_failure(None, ref, "", "clarify") == "over_clarification"
    assert classify_failure(None, ref, "", "refused") == "over_refusal"
    assert classify_failure(None, ref, "", "error") == "no_valid_sql"
