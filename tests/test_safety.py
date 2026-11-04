"""Safety tests: every attack must be blocked, and blocked at MORE than one layer."""
import sqlite3
import pytest
from t2s.safe_db import SafeDB, UnsafeQueryError, QueryTimeout, ForbiddenOperation, static_validate

DB = "data/Chinook_Sqlite.sqlite"


@pytest.fixture(scope="module")
def db():
    return SafeDB(DB, timeout_s=2.0)


@pytest.mark.parametrize("sql", [
    "DROP TABLE Customer", "DELETE FROM Customer", "UPDATE Customer SET Country='X'",
    "INSERT INTO Genre(Name) VALUES('x')", "CREATE TABLE t(x)", "ALTER TABLE Genre ADD COLUMN z",
    "PRAGMA writable_schema=1", "ATTACH DATABASE '/tmp/x.db' AS x", "VACUUM",
    "WITH x AS (SELECT 1) DELETE FROM Customer", "REPLACE INTO Genre VALUES (99,'x')",
])
def test_write_and_admin_statements_blocked(db, sql):
    with pytest.raises(UnsafeQueryError):
        db.execute(sql)


def test_statement_chaining_blocked(db):
    with pytest.raises(UnsafeQueryError):
        db.execute("SELECT 1; DELETE FROM Customer")


@pytest.mark.parametrize("sql", [
    "SELECT load_extension('evil')", "SELECT zeroblob(2000000000)", "SELECT randomblob(10)",
    "SELECT * FROM sqlite_master", "SELECT * FROM nonexistent_table",
])
def test_dangerous_functions_and_tables_blocked(db, sql):
    with pytest.raises(UnsafeQueryError):
        db.execute(sql)


def test_runaway_query_times_out(db):
    with pytest.raises(QueryTimeout):
        db.execute("SELECT COUNT(*) FROM Track a, Track b, Track c, Track d")
    with pytest.raises(QueryTimeout):
        db.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c")


def test_row_cap(db):
    r = SafeDB(DB, max_rows=10).execute("SELECT * FROM Track")
    assert len(r.rows) == 10 and r.truncated


def test_layers_2_and_3_hold_even_if_static_layer_is_bypassed(db):
    """Defence in depth: call the raw connection (skipping sqlglot) -- SQLite itself must refuse."""
    conn = db._connect()
    for sql in ["DELETE FROM Genre", "CREATE TABLE t(x)", "INSERT INTO Genre(Name) VALUES('x')",
                "ATTACH DATABASE ':memory:' AS m", "PRAGMA query_only=OFF"]:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)


def test_data_unchanged_after_attacks(db):
    assert db.execute("SELECT COUNT(*) FROM Customer").rows == [(59,)]
    assert db.execute("SELECT COUNT(*) FROM Genre").rows == [(25,)]


def test_forbidden_is_distinct_from_repairable():
    with pytest.raises(ForbiddenOperation):
        static_validate("DELETE FROM Customer")
    with pytest.raises(UnsafeQueryError) as e:       # unknown table -> repairable, not "forbidden"
        static_validate("SELECT * FROM users")
    assert not isinstance(e.value, ForbiddenOperation)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM Customer",
    "WITH x AS (SELECT CustomerId, SUM(Total) t FROM Invoice GROUP BY 1) SELECT * FROM x ORDER BY t DESC LIMIT 2",
    "SELECT Name, RANK() OVER (ORDER BY Milliseconds DESC) FROM Track LIMIT 3",
    "SELECT strftime('%Y', InvoiceDate) y, ROUND(SUM(Total),2) FROM Invoice GROUP BY y",
    "SELECT * FROM (SELECT Name FROM Genre) LIMIT 2",
    "SELECT 1 UNION SELECT 2",
])
def test_legit_queries_still_work(db, sql):
    assert db.execute(sql).rows


@pytest.mark.parametrize("sql", [
    "SELECT Name FROM Genre INTERSECT SELECT Name FROM Genre WHERE Name='Rock'",
    "SELECT GenreId FROM Genre EXCEPT SELECT GenreId FROM Track",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT Name FROM Genre WHERE GenreId IN (SELECT GenreId FROM Track INTERSECT SELECT GenreId FROM Track)",
])
def test_regression_set_operations_are_allowed(db, sql):
    """Regression: INTERSECT/EXCEPT were wrongly rejected (and labelled as writes) by an earlier version."""
    assert db.execute(sql).rows is not None


def test_unrecognised_root_is_repairable_not_forbidden():
    with pytest.raises(UnsafeQueryError) as e:
        static_validate("VALUES (1)")
    assert not isinstance(e.value, ForbiddenOperation)
