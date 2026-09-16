from dataclasses import replace
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

import duckdb
import pytest

from query_witness import artifact, core
from query_witness.cli import main
from query_witness.core import Config, Engine, Limit, Result, differs, reduce_rows, search
from query_witness.subset import Unsupported, parse

SCHEMA = "CREATE TABLE t (x INTEGER);"
FILTERED = "SELECT x FROM t WHERE x = x;"
ALL = "SELECT x FROM t;"


def engine(a=FILTERED, b=ALL, config=None):
    config = config or Config()
    return Engine(parse(SCHEMA, a, b), config, time.monotonic() + config.timeout_seconds)


def check_args(tmp_path, a=FILTERED, b=ALL, schema=SCHEMA):
    args = ["check"]
    for flag, text in (("schema", schema), ("query-a", a), ("query-b", b)):
        path = tmp_path / f"{flag}.sql"
        path.write_text(text)
        args.extend([f"--{flag}", str(path)])
    return args + ["--out", str(tmp_path / "finding")]


def assert_sql_reproduction(directory):
    payload = json.loads((directory / "witness.json").read_text())
    script = (directory / "reproduce.sql").read_text()
    assert "-- Query A\n" + artifact.sql_statement(payload["query_a"]) in script
    assert "-- Query B\n" + artifact.sql_statement(payload["query_b"]) in script
    with duckdb.connect() as db:
        statements = db.extract_statements(script)
        for statement in statements[:-2]:
            assert statement.type != duckdb.StatementType.SELECT
            db.execute(statement)
        assert len(payload["results"]) == 2
        for statement, recorded in zip(statements[-2:], payload["results"]):
            assert statement.type == duckdb.StatementType.SELECT
            cursor = db.execute(statement)
            actual = Result(tuple(str(column[1]) for column in cursor.description), tuple(cursor.fetchall()))
            expected = Result(tuple(recorded["types"]), tuple(map(tuple, recorded["rows"])))
            assert not differs(actual, expected, sequential=payload["comparison_order"] == "sequences")


def test_null_finding_and_reduction():
    with engine() as db:
        found, checked, _ = search(db)
        rows, (a, b) = found
        assert checked == 2
        assert rows == ((None,),)
        assert a.rows == ()
        assert b.rows == ((None,),)
    with engine(config=Config(max_rows=5)) as db:
        larger = ((7,), (None,), (-8,), (None,), (7,))
        reduced, observed = reduce_rows(db, larger)
        assert reduced == ((None,),)
        core.validate_rows(reduced, db.config.max_rows, db.inputs)
        assert differs(*observed)
        assert not differs(*db.compare(()))


@pytest.mark.parametrize("a,b", [(ALL, "SELECT x AS renamed FROM t"),
                                  (FILTERED, "SELECT x AS n FROM t WHERE x=x")])
def test_agreeing_pairs(a, b):
    with engine(a, b) as db:
        found, checked, _ = search(db)
        assert found is None
        assert checked > 5


def test_bag_policy_against_real_duckdb():
    with duckdb.connect() as db:
        def result(sql):
            cursor = db.execute(sql)
            return Result(tuple(str(d[1]) for d in cursor.description), tuple(cursor.fetchall()))
        a = result("SELECT * FROM (VALUES (1::INTEGER), (NULL), (1)) t(x)")
        b = result("SELECT * FROM (VALUES (NULL::BIGINT), (1), (1)) t(renamed)")
        assert not differs(a, b)
        assert differs(a, result("SELECT * FROM (VALUES (1), (NULL)) t(x)"))
        assert differs(a, result("SELECT * FROM (VALUES (1), (NULL), (2)) t(x)"))
        assert differs(result("SELECT 1 WHERE false"), result("SELECT 1, 2 WHERE false"))


@pytest.mark.parametrize("schema", [
    "CREATE TABLE t (x INTEGER, y INTEGER, z INTEGER)", "CREATE TABLE t (x BIGINT)",
    "CREATE TABLE t (x INTEGER DEFAULT 1)", "CREATE TABLE t (x INTEGER CHECK (x>0))",
    "CREATE TEMP TABLE t (x INTEGER)", "CREATE TABLE IF NOT EXISTS t (x INTEGER)",
    "CREATE TABLE t AS SELECT 1 AS x", "CREATE TABLE t (x INTEGER); DROP TABLE t",
    'CREATE TABLE "t" (x INTEGER)', "CREATE TABLE main.t (x INTEGER)",
])
def test_reject_schema_outside_slice(schema):
    with pytest.raises(Unsupported):
        parse(schema, FILTERED, ALL)


@pytest.mark.parametrize("query", [
    "SELECT DISTINCT ON (x) x FROM t", "SELECT * FROM t", "SELECT x FROM t WHERE x = x OR TRUE",
    "SELECT COUNT(DISTINCT x, x) FROM t", "SELECT DISTINCT * FROM t",
    "SELECT x FROM t GROUP BY 1", "SELECT DISTINCT x FROM t GROUP BY x",
    "SELECT x FROM t ORDER BY 1", "SELECT x FROM t LIMIT 1", "SELECT AVG(x) FROM t",
    "SELECT x::BIGINT FROM t", "SELECT random() FROM t", "SELECT x FROM read_csv('x')",
    "SELECT x FROM t JOIN t u USING (x)", "SELECT x FROM t UNION ALL SELECT x FROM t",
    "WITH c AS (SELECT x FROM t) SELECT x FROM c", "SELECT x FROM (SELECT x FROM t)",
    "SELECT x FROM t; DELETE FROM t", "DELETE FROM t", "SELECT y FROM t",
    "SELECT x FROM u", "SELECT x FROM t AS u", "SELECT t.x FROM t", "SELECT x",
    "", "-- comment only",
])
def test_reject_queries_outside_slice(query):
    with pytest.raises(Unsupported):
        parse(SCHEMA, query, ALL)


def test_budget_first_schedule():
    config = Config(max_rows=1000, max_candidates=2)
    iterator = core.candidates(config, parse(SCHEMA, ALL, ALL))
    assert [next(iterator) for _ in range(5)] == [(), ((None,),), ((-1,),), ((0,),), ((1,),)]
    with engine(ALL, ALL, config) as db:
        assert search(db)[1:] == (2, "candidate budget exhausted")


@pytest.mark.parametrize("rows", [((True,),), ((2**31,),), (("1",),), ((1, 2),)])
def test_invalid_data_never_compared(rows):
    with engine() as db, pytest.raises(ValueError, match="Invalid data"):
        db.compare(rows)


@pytest.mark.parametrize("a,b,expected", [
    (FILTERED, ALL, [[], [(None,)]]),
    ("SELECT x FROM t WHERE x IS NULL", "SELECT x FROM t WHERE x = NULL", [[(None,)], []]),
])
def test_export_replay_without_generation(tmp_path, monkeypatch, capsys, a, b, expected):
    assert main(check_args(tmp_path, a, b)) == 0
    output = capsys.readouterr().out
    for text in ("Comparison policy:", "Schema:", "INSERT INTO", "NULL", "Result bag A:",
                 "Result bag B:", "(empty bag)", "× 1", "not globally minimal"):
        assert text in output
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [[None]]
    assert payload["versions"]["duckdb"] == duckdb.__version__
    assert set(payload["versions"]) == {"query_witness", "duckdb", "sqlglot"}
    assert "generator" not in payload["search"]
    assert payload["config"] == {"max_rows": 4, "max_candidates": 64,
                                 "timeout_seconds": 5.0, "memory_mb": 64}
    def no_generation(*_):
        pytest.fail("Replay must not generate data")
    monkeypatch.setattr(core, "candidates", no_generation)
    # Original input files are not needed to replay.
    for path in tmp_path.glob("*.sql"):
        path.unlink()
    assert main(["replay", str(directory)]) == 0
    assert "Replay verified" in capsys.readouterr().out
    with duckdb.connect() as db:
        script = (directory / "reproduce.sql").read_text()
        results = []
        for stmt in db.extract_statements(script):
            cursor = db.execute(stmt)
            if stmt.type == duckdb.StatementType.SELECT:
                results.append(cursor.fetchall())
        assert results == expected


@pytest.mark.parametrize("change", ["rows", "results", "duckdb", "sqlglot", "policy"])
def test_replay_detects_changed_artifact(tmp_path, capsys, change):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    if change == "rows":
        payload["rows"] = [[1]]
    elif change == "results":
        payload["results"][1]["rows"] = [[2]]
    elif change == "duckdb":
        payload["versions"]["duckdb"] = "0.0.0"
    elif change == "sqlglot":
        payload["versions"]["sqlglot"] = "0.0.0"
    else:
        payload["comparison_policy"] = "unknown"
    path.write_text(json.dumps(payload))
    capsys.readouterr()
    assert main(["replay", str(path.parent)]) == 3
    output = capsys.readouterr().out
    assert "Outcome: execution failure" in output
    assert "Outcome: counterexample found" not in output


def test_replay_accepts_different_query_witness_version(tmp_path, capsys):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["versions"]["query_witness"] = "0.0.0"
    path.write_text(json.dumps(payload))
    capsys.readouterr()
    assert main(["replay", str(path.parent)]) == 0
    assert "Replay verified" in capsys.readouterr().out


def test_cli_outcomes_and_policy(tmp_path, capsys):
    args = check_args(tmp_path, ALL, ALL)
    assert main(args + ["--max-candidates", "1"]) == 1
    assert "no counterexample within budget" in capsys.readouterr().out
    assert main(check_args(tmp_path, "SELECT DISTINCT ON (x) x FROM t")) == 2
    assert "unsupported input" in capsys.readouterr().out
    args = check_args(tmp_path, ALL, ALL)
    assert main(args + ["--timeout-seconds", "0.000000001"]) == 4
    assert "resource limit reached" in capsys.readouterr().out
    assert main(args + ["--memory-mb", "0"]) == 3
    assert "execution failure" in capsys.readouterr().out
    assert main(["unknown"]) == 3
    assert "Comparison policy:" not in capsys.readouterr().out


def test_engine_error_not_a_finding(tmp_path, monkeypatch, capsys):
    def broken(*_):
        raise duckdb.BinderException("injected engine failure")
    monkeypatch.setattr(Engine, "compare", broken)
    assert main(check_args(tmp_path)) == 3
    assert "injected engine failure" in capsys.readouterr().out
    assert not (tmp_path / "finding").exists()


def test_real_query_timeout():
    # Directly exercise the engine interrupt, without admitting this SQL to the gate.
    with engine(config=Config(timeout_seconds=0.2)) as db:
        with pytest.raises(Limit, match="time budget|Elapsed-time"):
            db.execute("SELECT sum(i) FROM range(1000000000000) t(i)")


def test_real_memory_limit():
    with engine(config=Config(memory_mb=1)) as db:
        with pytest.raises(Limit, match="memory budget"):
            db.execute("SELECT list(i) FROM range(1000000) t(i)")


def test_existing_export_is_not_overwritten(tmp_path, capsys):
    args = check_args(tmp_path)
    assert main(args) == 0
    path = tmp_path / "finding" / "witness.json"
    before = path.read_bytes()
    assert main(args) == 3
    assert path.read_bytes() == before


@pytest.mark.parametrize("suffix", [" -- comment", " -- comment;", " -- comment;  \n",
                                    " /* comment; */", " /* -- ; */ -- ;"])
def test_comment_terminated_source_exports_valid_sql(tmp_path, suffix):
    inputs = parse("CREATE TABLE t (x INT)" + suffix, "SELECT x FROM t WHERE x=x" + suffix,
                   "SELECT x FROM t" + suffix)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, checked, _ = search(db)
    artifact.export(tmp_path / "finding", inputs, Config(), *found, checked)
    assert_sql_reproduction(tmp_path / "finding")


def test_nested_sql_subprocess_is_unsupported(tmp_path):
    query = "SELECT x FROM t WHERE " + "(" * 300 + "x = 1" + ")" * 300
    result = subprocess.run([sys.executable, "-m", "query_witness", *check_args(tmp_path, query)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "Outcome: unsupported input" in result.stdout
    assert "parser nesting capacity" in result.stdout
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "finding").exists()


def test_enormous_timeout_subprocess_is_failure(tmp_path):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["config"]["timeout_seconds"] = 10 ** 1000
    path.write_text(json.dumps(payload))
    result = subprocess.run([sys.executable, "-m", "query_witness", "replay", str(path.parent)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 3
    assert "Outcome: execution failure" in result.stdout
    assert "timeout_seconds must be" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("a,b,value,checked", [
    ("x IS NULL", "x = NULL", None, 2),
    ("x >= 1", "x > 1", 1, 5),
    ("x = 1 OR x IS NULL", "x = 1", None, 2),
    ("x >= 42", "x > 42", 42, 6),
    ("x >= -2147483648", "x > -2147483648", -2147483648, 6),
    ("x <= 2147483647", "x < 2147483647", 2147483647, 6),
])
def test_where_findings_export_and_replay(tmp_path, monkeypatch, a, b, value, checked):
    args = check_args(tmp_path, f"SELECT x FROM t WHERE {a}", f"SELECT x FROM t WHERE {b}")
    assert main(args) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    assert payload["rows"] == [[value]]
    assert payload["results"][0]["rows"] == [[value]]
    assert payload["results"][1]["rows"] == []
    assert payload["search"]["candidates_checked"] == checked
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(path.parent)]) == 0


@pytest.mark.parametrize("a,b", [
    ("x = x", "x IS NOT NULL"),
    ("x > 0", "x >= 1"),
    ("NOT (x = 1 OR x IS NULL)", "x != 1 AND x IS NOT NULL"),
    ("NOT (x < -4 AND x <> 0)", "x >= -4 OR x == 0"),
    ("(x) <= (-1)", "NOT (x > -1)"),
    ("NULL < x", "x > NULL"),
    ("-1 < x", "x > -1"),
    ("x = x AND x = x", "x IS NOT NULL"),
])
def test_where_agreeing_pairs(a, b):
    with engine(f"SELECT x FROM t WHERE {a}", f"SELECT x FROM t WHERE {b}") as db:
        found, checked, _ = search(db)
        assert found is None
        assert checked > 5


@pytest.mark.parametrize("predicate", [
    "x = 1.5", "x = '1'", "x = -1.0", "x = 1e0", "x = DECIMAL '1'",
    "TRUE", "FALSE", "NOT TRUE", "x = TRUE", "x IS TRUE", "x IS NOT FALSE",
    "1 = 1", "NULL <> 1", "NULL = NULL", "1 IS NULL", "NULL IS NULL",
    "x IN (1)", "x BETWEEN 0 AND 1", "abs(x) = 1", "x = coalesce(x, 1)",
    "x IS DISTINCT FROM NULL", "x IS NOT DISTINCT FROM x", "t.x = 1", "x = t.x",
    "x = y", "x + 1 = 2", "x = -x", "x = (1 + 1)", "x::BIGINT = 1",
    "CASE WHEN x = 1 THEN TRUE ELSE FALSE END", "x = (SELECT 1)",
    "x = 2147483648", "x = -2147483649", "2147483648 = x", "x = -99999999999999999",
    "(x = 1) = (x = 1)", "x", "NOT x", "x IS NULL OR (1 = 1)",
])
def test_where_gate_rejects_outside_grammar(predicate):
    with pytest.raises(Unsupported):
        parse(SCHEMA, f"SELECT x FROM t WHERE {predicate}", ALL)


@pytest.mark.parametrize("operator", ["=", "<>", "!=", "==", "<", "<=", ">", ">="])
def test_all_comparison_spellings_execute_verbatim(operator):
    source = f"SELECT x FROM t WHERE -7 {operator} x OR x {operator} NULL"
    with engine(source, source) as db:
        assert db.inputs.query_a == source
        assert db.inputs.literals == (-7,)
        assert not differs(*db.compare(((None,), (-7,), (0,))))


def test_ast_literal_harvesting_and_range():
    inputs = parse("CREATE TABLE t (x INTEGER) -- 123456",
                   "SELECT x AS output_999 FROM t WHERE x >= -2147483648 AND x < 42 /* x = 8765 */",
                   "SELECT x FROM t WHERE NOT (2147483647 < x OR x = 42) -- 4321")
    assert inputs.literals == (-2147483648, 42, 2147483647)
    assert core.interesting_values(inputs.literals) == (
        None, -1, 0, 1, -2147483648, -2147483647, 42, 41, 43, 2147483647, 2147483646)


@pytest.mark.parametrize("literals", [(), (42,), tuple(range(10, 110, 10))])
def test_candidate_schedule_linear_in_domain_per_row_count(literals):
    config = Config(max_rows=4)
    domain = core.interesting_values(literals)
    inputs = replace(parse(SCHEMA, ALL, ALL), literals=literals)
    schedule = list(core.candidates(config, inputs))
    assert schedule[0] == ()
    assert schedule[1:1 + len(domain)] == [((value,),) for value in domain]
    assert len(schedule) == 1 + len(domain) + 2 * len(domain) * (config.max_rows - 1)
    for size in range(2, config.max_rows + 1):
        assert sum(len(rows) == size for rows in schedule) == 2 * len(domain)
        assert 2 * len(domain) < len(domain) ** size
    for rows in schedule:
        core.validate_rows(rows, config.max_rows, inputs)


def test_where_reduction_preserves_valid_discrepancy():
    with engine("SELECT x FROM t WHERE x >= 42", "SELECT x FROM t WHERE x > 42") as db:
        rows, results = reduce_rows(db, ((None,), (43,), (42,), (42,)))
        assert rows == ((42,),)
        core.validate_rows(rows, db.config.max_rows, db.inputs)
        assert differs(*results)


@pytest.mark.parametrize("sql,options", [
    ("SELECT sum(i) FROM range(1000000000000) t(i)", ["--timeout-seconds", "0.2"]),
    ("SELECT list(i) FROM range(1000000) t(i)", ["--memory-mb", "1"]),
])
def test_real_resource_failure_cli_is_limit(tmp_path, monkeypatch, capsys, sql, options):
    # Inject work inside the engine solely to test CLI classification; the gate
    # still rejects these queries as user input.
    def resource_work(db, rows):
        db.execute(sql)
        pytest.fail("Expected a resource limit")
    monkeypatch.setattr(Engine, "compare", resource_work)
    assert main(check_args(tmp_path) + options) == 4
    output = capsys.readouterr().out
    assert "Outcome: resource limit reached" in output
    assert "Outcome: counterexample found" not in output
    assert not (tmp_path / "finding").exists()


@pytest.mark.parametrize("constraint,nullable,primary_key", [
    ("", (True,), None), ("NOT NULL", (False,), None),
    ("PRIMARY KEY", (False,), "x"), ("NOT NULL PRIMARY KEY", (False,), "x"),
])
def test_parsed_constraint_flags(constraint, nullable, primary_key):
    inputs = parse(f"CREATE TABLE t (x INTEGER {constraint})", FILTERED, ALL)
    assert inputs.nullable == nullable
    assert inputs.primary_key == primary_key
    core.validate_rows((), 4, inputs)


@pytest.mark.parametrize("definition", [
    "x INTEGER UNIQUE", "x INTEGER DEFAULT 1", "x INTEGER CHECK (x > 0)",
    "x INTEGER REFERENCES other_table(x)", "x INTEGER, FOREIGN KEY (x) REFERENCES other_table(x)",
    "x INTEGER, PRIMARY KEY (x)", "x INTEGER, y INTEGER UNIQUE", "x INTEGER NULL",
    "x INTEGER CONSTRAINT nn NOT NULL", "x INTEGER CONSTRAINT pk PRIMARY KEY",
    "x INTEGER NOT NULL UNIQUE", "x INTEGER PRIMARY KEY DEFAULT 1",
    "x INTEGER PRIMARY KEY DESC", "x INTEGER PRIMARY KEY ASC",
    "x INTEGER NOT NULL NOT NULL", "x INTEGER PRIMARY KEY PRIMARY KEY",
])
def test_reject_unsupported_constraints(definition):
    with pytest.raises(Unsupported):
        parse(f"CREATE TABLE t ({definition})", FILTERED, ALL)


@pytest.mark.parametrize("constraint,count", [
    ("NOT NULL", 22), ("PRIMARY KEY", 10), ("NOT NULL PRIMARY KEY", 10),
])
def test_constrained_no_find_only_checks_legal_tables(tmp_path, monkeypatch, capsys, constraint, count):
    checked_rows = []
    original_compare = Engine.compare
    def record(db, rows):
        core.validate_rows(rows, db.config.max_rows, db.inputs)
        assert all(row[0] is not None for row in rows)
        if db.inputs.primary_key:
            assert len(set(rows)) == len(rows)
        checked_rows.append(rows)
        return original_compare(db, rows)
    monkeypatch.setattr(Engine, "compare", record)
    schema = f"CREATE TABLE t (x INTEGER {constraint})"
    assert main(check_args(tmp_path, schema=schema)) == 1
    assert len(checked_rows) == count
    assert checked_rows[:4] == [(), ((-1,),), ((0,),), ((1,),)]
    output = capsys.readouterr().out
    assert f"Checked {count} candidates" in output
    assert "Outcome: no counterexample within budget" in output
    assert not (tmp_path / "finding").exists()


@pytest.mark.parametrize("constraint", ["NOT NULL", "PRIMARY KEY", "NOT NULL PRIMARY KEY"])
def test_constrained_finding_reduction_export_replay(tmp_path, monkeypatch, constraint):
    schema = f"CREATE TABLE t (x INTEGER {constraint})"
    a, b = "SELECT x FROM t WHERE x >= 1", "SELECT x FROM t WHERE x > 1"
    inputs = parse(schema, a, b)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        reduced, results = reduce_rows(db, ((-1,), (1,), (2,)))
        assert reduced == ((1,),)
        core.validate_rows(reduced, db.config.max_rows, inputs)
        assert results[0].rows == ((1,),)
        assert results[1].rows == ()
    assert main(check_args(tmp_path, a, b, schema)) == 0
    directory = tmp_path / "finding"
    loaded, _, rows, _, _ = artifact.load(directory)
    assert rows == ((1,),)
    assert loaded.nullable == (False,)
    assert loaded.primary_key == inputs.primary_key
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["schema"] == schema
    assert payload["search"]["candidates_checked"] == 4
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


@pytest.mark.parametrize("constraint,rows", [
    ("NOT NULL", ((None,),)), ("PRIMARY KEY", ((None,),)),
    ("PRIMARY KEY", ((1,), (1,))), ("NOT NULL PRIMARY KEY", ((0,), (0,))),
])
def test_invalid_constrained_rows_rejected_before_sql(monkeypatch, constraint, rows):
    inputs = parse(f"CREATE TABLE t (x INTEGER {constraint})", FILTERED, ALL)
    with pytest.raises(ValueError, match="Invalid data"):
        core.validate_rows(rows, 4, inputs)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        monkeypatch.setattr(db, "execute", lambda *_: pytest.fail("Invalid data reached SQL execution"))
        with pytest.raises(ValueError, match="Invalid data"):
            db.compare(rows)


@pytest.mark.parametrize("constraint,rows", [
    ("NOT NULL", [[None]]), ("PRIMARY KEY", [[None]]), ("PRIMARY KEY", [[1], [1]]),
])
def test_replay_validates_constraints_before_execution(tmp_path, monkeypatch, capsys, constraint, rows):
    schema = f"CREATE TABLE t (x INTEGER {constraint})"
    assert main(check_args(tmp_path, "SELECT x FROM t WHERE x >= 1",
                           "SELECT x FROM t WHERE x > 1", schema)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["rows"] = rows
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(Engine, "__enter__", lambda *_: pytest.fail("Invalid replay opened engine"))
    capsys.readouterr()
    assert main(["replay", str(path.parent)]) == 3
    output = capsys.readouterr().out
    assert "Invalid data" in output
    assert "Outcome: counterexample found" not in output


@pytest.mark.parametrize("constraint", ["", "NOT NULL", "PRIMARY KEY", "NOT NULL PRIMARY KEY"])
@pytest.mark.parametrize("literal", [None, 42])
def test_every_candidate_validates_and_executes(constraint, literal):
    query = ALL if literal is None else f"SELECT x FROM t WHERE x = {literal}"
    inputs = parse(f"CREATE TABLE t (x INTEGER {constraint})", query, ALL)
    config = Config(max_rows=8)
    domain = core.interesting_values(inputs.literals)
    assert domain[0] is None  # interesting_values itself is unchanged.
    domain_size = len(domain) - (not inputs.nullable[0])
    schedule = list(core.candidates(config, inputs))
    assert schedule[0] == ()
    with Engine(inputs, config, time.monotonic() + 5) as db:
        for rows in schedule:
            core.validate_rows(rows, config.max_rows, inputs)
            db.compare(rows)  # Independently exercise the declared constraints in DuckDB.
    for size in range(1, config.max_rows + 1):
        count = sum(len(rows) == size for rows in schedule)
        if inputs.primary_key:
            assert count == (domain_size if size <= domain_size else 0)
            if size > 1:
                assert count < domain_size ** size  # No enumeration of unique k-tuples.
        else:
            assert count == domain_size * (1 if size == 1 else 2)
    if not inputs.primary_key:
        assert ((1,), (1,)) in schedule


def test_pk_skips_do_not_spend_candidate_budget():
    inputs = parse("CREATE TABLE t (x INTEGER PRIMARY KEY)", FILTERED, ALL)
    config = Config(max_rows=20, max_candidates=10)
    with Engine(inputs, config, time.monotonic() + 5) as db:
        assert search(db) == (None, 10, "bounded candidate schedule exhausted")


def test_duckdb_constraint_error_is_failure(tmp_path, monkeypatch, capsys):
    def invalid_insert(db, rows):
        db.execute('INSERT INTO t VALUES (NULL)')
        pytest.fail("DuckDB should enforce NOT NULL")
    monkeypatch.setattr(Engine, "compare", invalid_insert)
    assert main(check_args(tmp_path, schema="CREATE TABLE t (x INTEGER NOT NULL)")) == 3
    output = capsys.readouterr().out
    assert "ConstraintException" in output
    assert "Outcome: execution failure" in output
    assert "Outcome: counterexample found" not in output
    assert not (tmp_path / "finding").exists()


@pytest.mark.parametrize("constraint,where,value,checked", [
    ("", "", None, 6),
    ("NOT NULL", "", -1, 5),
    ("", " WHERE x = 1", 1, 13),
])
def test_distinct_finding_reduction_export_replay(tmp_path, monkeypatch, capsys,
                                                constraint, where, value, checked):
    schema = f"CREATE TABLE t (x INTEGER {constraint})"
    query_a = f"SELECT DISTINCT x FROM t{where}"
    query_b = f"SELECT x FROM t{where}"
    inputs = parse(schema, query_a, query_b)
    assert inputs.query_a == query_a
    assert inputs.query_b == query_b
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, count, _ = search(db)
        rows, results = found
        assert count == checked
        assert rows == ((value,), (value,))
        assert results[0].rows == ((value,),)
        assert results[1].rows == rows
        assert reduce_rows(db, rows) == found
        assert not differs(*db.compare(rows[:1]))
        reduced, observed = reduce_rows(db, ((value,),) * 4)
        assert reduced == rows
        assert differs(*observed)
        core.validate_rows(reduced, db.config.max_rows, inputs)

    assert main(check_args(tmp_path, query_a, query_b, schema)) == 0
    output = capsys.readouterr().out
    printed_value = "NULL" if value is None else str(value)
    assert f"({printed_value}) × 1" in output
    assert f"({printed_value}) × 2" in output
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [[value], [value]]
    assert payload["search"]["candidates_checked"] == checked
    assert payload["results"][0]["rows"] == [[value]]
    assert payload["results"][1]["rows"] == [[value], [value]]
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


@pytest.mark.parametrize("constraint", ["PRIMARY KEY", "NOT NULL PRIMARY KEY"])
def test_primary_key_distinct_has_no_finding(constraint):
    inputs = parse(f"CREATE TABLE t (x INTEGER {constraint})", "SELECT DISTINCT x FROM t", ALL)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        assert search(db) == (None, 10, "bounded candidate schedule exhausted")
        for rows in core.candidates(db.config, inputs):
            assert all(row[0] is not None for row in rows)
            assert len(rows) == len(set(rows))
        with pytest.raises(ValueError, match="duplicate"):
            db.compare(((1,), (1,)))


@pytest.mark.parametrize("query_b", ["SELECT DISTINCT x FROM t", "SELECT DISTINCT x AS renamed FROM t"])
def test_distinct_queries_agree(query_b):
    with engine("SELECT DISTINCT x FROM t", query_b) as db:
        found, checked, _ = search(db)
        assert found is None
        assert checked == 29


@pytest.mark.parametrize("query_a,query_b,rows,checked,result_a,result_b", [
    ("SELECT x FROM t", "SELECT y FROM t", ((None, -1),), 3, ((None,),), ((-1,),)),
    ("SELECT x, y FROM t", "SELECT y, x FROM t", ((None, -1),), 3,
     ((None, -1),), ((-1, None),)),
    ("SELECT x, y FROM t WHERE x = y", "SELECT x, y FROM t", ((None, None),), 2,
     (), ((None, None),)),
    ("SELECT DISTINCT x FROM t", "SELECT x FROM t", ((None, None), (None, None)), 18,
     ((None,),), ((None,), (None,))),
])
def test_two_column_pipeline(tmp_path, monkeypatch, query_a, query_b, rows, checked, result_a, result_b):
    schema = "CREATE TABLE t (x INTEGER, y INT)"
    inputs = parse(schema, query_a, query_b)
    assert inputs.columns == ("x", "y")
    assert inputs.nullable == (True, True)
    assert inputs.primary_key is None
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, count, _ = search(db)
        assert count == checked
        assert found[0] == rows
        assert found[1][0].rows == result_a
        assert found[1][1].rows == result_b
        assert reduce_rows(db, rows)[0] == rows
        assert not differs(*db.compare(rows[:-1]))
    assert main(check_args(tmp_path, query_a, query_b, schema)) == 0
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [list(row) for row in rows]
    assert payload["search"]["candidates_checked"] == checked
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


def test_two_column_equality_filters_mixed_and_null_rows():
    inputs = parse("CREATE TABLE t (x INTEGER, y INTEGER)",
                   "SELECT x, y FROM t WHERE x = y", "SELECT x, y FROM t")
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(((None, -1),))
        assert results[0].rows == ()
        assert results[1].rows == ((None, -1),)
        assert not differs(*db.compare(((1, 1),)))


@pytest.mark.parametrize("literals", [(), (42,), tuple(range(10, 60, 10))])
def test_two_column_singleton_pairs_and_linear_larger_tables(literals):
    inputs = replace(parse("CREATE TABLE t (x INTEGER, y INTEGER)", ALL, ALL), literals=literals)
    config = Config(max_rows=3)
    domain = core.interesting_values(literals)
    schedule = list(core.candidates(config, inputs))
    assert schedule[:5] == [(), ((None, None),), ((None, -1),), ((None, 0),), ((None, 1),)]
    assert len(schedule) == 1 + len(domain) ** 2 + 2 * len(domain) * (config.max_rows - 1)
    singletons = [rows for rows in schedule if len(rows) == 1]
    assert singletons == [((first, second),) for first in domain for second in domain]
    assert len(singletons) == len(domain) ** 2
    for size in range(2, config.max_rows + 1):
        tables = [rows for rows in schedule if len(rows) == size]
        assert len(tables) == 2 * len(domain)
        assert len(tables) < len(domain) ** 2
        for offset, value in enumerate(domain):
            assert tables[2 * offset] == ((value, value),) * size
            expected_mixed = []
            for index in range(size):
                expected_mixed.append((domain[(offset + index) % len(domain)],
                                       domain[(offset + index + 1) % len(domain)]))
            assert tables[2 * offset + 1] == tuple(expected_mixed)
    with Engine(inputs, config, time.monotonic() + 5) as db:
        for rows in schedule:
            core.validate_rows(rows, config.max_rows, inputs)
            assert not differs(*db.compare(rows))


@pytest.mark.parametrize("definition", [
    "x INT, y INT, z INT", "x INT, x INT", "x INT, X INT",
    "x INT PRIMARY KEY, y INT PRIMARY KEY",
    "x INT, y INT, PRIMARY KEY (x, y)", "x INT UNIQUE, y INT",
    "x INT, y INT DEFAULT 1", "x INT, y INT NULL", "x INT, y BIGINT",
    'x INT, "y" INT',
])
def test_two_column_schema_rejections(definition):
    with pytest.raises(Unsupported):
        parse(f"CREATE TABLE t ({definition})", ALL, ALL)


@pytest.mark.parametrize("query", [
    "SELECT x FROM t JOIN t u ON x = y", "SELECT t.x FROM t", "SELECT z FROM t",
    "SELECT x FROM t WHERE t.x = y", "SELECT x FROM t WHERE x = z",
    "SELECT x FROM t WHERE x + y = 1", "SELECT * FROM t",
])
def test_two_column_query_rejections(query):
    with pytest.raises(Unsupported):
        parse("CREATE TABLE t (x INT, y INT)", query, ALL)


@pytest.mark.parametrize("row", [(1,), (1, 2, 3), (1, "2"), (None, True),
                                 (1.0, 2), (1, 2**31), (-2**31-1, 0)])
def test_invalid_two_column_data_before_execution(monkeypatch, row):
    inputs = parse("CREATE TABLE t (x INT, y INT)", ALL, ALL)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        monkeypatch.setattr(db, "execute", lambda *_: pytest.fail("Invalid data reached SQL"))
        with pytest.raises(ValueError, match="Invalid data"):
            db.compare((row,))


def test_two_column_replay_rejects_wrong_width(tmp_path, monkeypatch):
    assert main(check_args(tmp_path, ALL, "SELECT y FROM t", "CREATE TABLE t (x INT, y INT)")) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["rows"] = [[None]]
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(Engine, "__enter__", lambda *_: pytest.fail("Invalid replay opened engine"))
    assert main(["replay", str(path.parent)]) == 3


@pytest.mark.parametrize("constraints,nullable,key,count", [
    (("NOT NULL", ""), (False, True), None, 25),
    (("", "NOT NULL"), (True, False), None, 25),
    (("NOT NULL", "NOT NULL"), (False, False), None, 20),
    (("PRIMARY KEY", ""), (False, True), "x", 16),
    (("", "PRIMARY KEY"), (True, False), "y", 16),
    (("NOT NULL PRIMARY KEY", "NOT NULL"), (False, False), "x", 11),
    (("NOT NULL", "NOT NULL PRIMARY KEY"), (False, False), "y", 11),
])
def test_two_column_constraints_schedule(constraints, nullable, key, count):
    schema = f"CREATE TABLE t (x INT {constraints[0]}, y INT {constraints[1]})"
    inputs = parse(schema, "SELECT x, y FROM t", "SELECT x, y FROM t")
    assert inputs.nullable == nullable
    assert inputs.primary_key == key
    config = Config(max_candidates=count)
    schedule = list(core.candidates(config, inputs))
    assert len(schedule) == count
    assert schedule[0] == ()
    domain_size = len(core.interesting_values(inputs.literals))
    first_values = domain_size - (not nullable[0])
    second_values = domain_size - (not nullable[1])
    assert sum(len(rows) == 1 for rows in schedule) == first_values * second_values
    for size in range(2, config.max_rows + 1):
        assert sum(len(rows) == size for rows in schedule) <= 2 * domain_size
    with Engine(inputs, config, time.monotonic() + 5) as db:
        for rows in schedule:
            core.validate_rows(rows, config.max_rows, inputs)
            for row in rows:
                for index in range(2):
                    if not nullable[index]:
                        assert row[index] is not None
            if key is not None:
                key_index = inputs.columns.index(key)
                assert len({row[key_index] for row in rows}) == len(rows)
            assert not differs(*db.compare(rows))
        assert search(db) == (None, count, "bounded candidate schedule exhausted")


@pytest.mark.parametrize("constraints,rows,checked", [
    (("NOT NULL", ""), ((-1, None),), 2),
    (("NOT NULL", "NOT NULL"), ((-1, 0),), 3),
    (("PRIMARY KEY", ""), ((-1, None),), 2),
    (("", "PRIMARY KEY"), ((None, -1),), 2),
])
def test_constrained_two_column_pipeline(tmp_path, monkeypatch, constraints, rows, checked):
    schema = f"CREATE TABLE t (x INT {constraints[0]}, y INT {constraints[1]})"
    inputs = parse(schema, ALL, "SELECT y FROM t")
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, count, _ = search(db)
        assert found[0] == rows
        assert count == checked
        assert found[1][0].rows == ((rows[0][0],),)
        assert found[1][1].rows == ((rows[0][1],),)
        assert reduce_rows(db, rows)[0] == rows
        assert not differs(*db.compare(()))
    assert main(check_args(tmp_path, ALL, "SELECT y FROM t", schema)) == 0
    directory = tmp_path / "finding"
    loaded, _, exported_rows, _, _ = artifact.load(directory)
    assert exported_rows == rows
    assert loaded.nullable == inputs.nullable
    assert loaded.primary_key == inputs.primary_key
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


@pytest.mark.parametrize("key", ["x", "y"])
def test_two_column_primary_key_distinct_agrees(key):
    schema = f"CREATE TABLE t ({key} INT PRIMARY KEY, other INT)"
    # Put y in the second position to exercise indexed key validation.
    if key == "y":
        schema = "CREATE TABLE t (other INT, y INT PRIMARY KEY)"
    inputs = parse(schema, f"SELECT DISTINCT {key} FROM t", f"SELECT {key} FROM t")
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        assert search(db) == (None, 16, "bounded candidate schedule exhausted")
        if key == "x":
            rows = ((1, None), (2, None))
        else:
            rows = ((None, 1), (None, 2))
        assert not differs(*db.compare(rows))


@pytest.mark.parametrize("definition,rows", [
    ("x INT NOT NULL, y INT", ((None, 1),)),
    ("x INT, y INT NOT NULL", ((1, None),)),
    ("x INT PRIMARY KEY, y INT", ((None, 1),)),
    ("x INT PRIMARY KEY, y INT", ((1, 0), (1, 2))),
    ("x INT, y INT PRIMARY KEY", ((1, None),)),
    ("x INT, y INT PRIMARY KEY", ((0, 1), (2, 1))),
])
def test_two_column_constraints_reject_before_sql(monkeypatch, definition, rows):
    inputs = parse(f"CREATE TABLE t ({definition})", ALL, ALL)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        monkeypatch.setattr(db, "execute", lambda *_: pytest.fail("Illegal rows reached SQL"))
        with pytest.raises(ValueError, match="Invalid data"):
            db.compare(rows)


def test_not_null_two_column_equality_first_legal_difference():
    inputs = parse("CREATE TABLE t (x INT NOT NULL, y INT)",
                   "SELECT x, y FROM t WHERE x = y", "SELECT x, y FROM t")
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, count, _ = search(db)
        assert count == 2
        assert found[0] == ((-1, None),)
        assert found[1][0].rows == ()
        assert found[1][1].rows == ((-1, None),)


@pytest.mark.parametrize("rows", [[[None, 1]], [[0, 1], [2, 1]]])
def test_two_column_constraint_replay_rejects_invalid_data(tmp_path, monkeypatch, rows):
    schema = "CREATE TABLE t (x INT NOT NULL, y INT PRIMARY KEY)"
    assert main(check_args(tmp_path, ALL, "SELECT y FROM t", schema)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["rows"] = rows
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(Engine, "__enter__", lambda *_: pytest.fail("Invalid replay opened engine"))
    assert main(["replay", str(path.parent)]) == 3


@pytest.mark.parametrize("distinct", ["", "DISTINCT "])
def test_ordered_pipeline(tmp_path, monkeypatch, capsys, distinct):
    query_a = f"SELECT {distinct}x FROM t ORDER BY x"
    query_b = f"SELECT {distinct}x FROM t ORDER BY x DESC"
    inputs = parse(SCHEMA, query_a, query_b)
    assert inputs.ordered == (True, True)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, checked, _ = search(db)
        rows, results = found
        assert checked == 9
        assert rows == ((-1,), (0,))
        assert results[0].rows == ((-1,), (0,))
        assert results[1].rows == ((0,), (-1,))
        assert not differs(*results)
        assert differs(*results, sequential=True)
        assert reduce_rows(db, rows) == found
        assert not differs(*db.compare(rows[:1]), sequential=True)
    assert main(check_args(tmp_path, query_a, query_b)) == 0
    output = capsys.readouterr().out
    assert "only when both queries have ORDER BY" in output
    assert "Result sequence A:" in output
    assert "1. (-1)\n  2. (0)" in output
    assert "1. (0)\n  2. (-1)" in output
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["comparison_policy"] == "integer-position-v2"
    assert payload["comparison_order"] == "sequences"
    assert payload["rows"] == [[-1], [0]]
    assert payload["search"]["candidates_checked"] == 9
    assert payload["results"][0]["rows"] == [[-1], [0]]
    assert payload["results"][1]["rows"] == [[0], [-1]]
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert "Result sequence B:" in (directory / "witness.txt").read_text()
    assert_sql_reproduction(directory)


def test_duckdb_155_null_order_trap():
    assert duckdb.__version__ == "1.5.5"
    with engine("SELECT x FROM t ORDER BY x", "SELECT x FROM t ORDER BY x DESC") as db:
        results = db.compare(((None,), (-1,)))
        assert results[0].rows == results[1].rows == ((-1,), (None,))
        assert not differs(*results, sequential=True)
        results = db.compare(((-1,), (0,)))
        assert results[0].rows == ((-1,), (0,))
        assert results[1].rows == ((0,), (-1,))
        assert differs(*results, sequential=True)


@pytest.mark.parametrize("query_a,query_b,ordered", [
    ("SELECT x FROM t ORDER BY x", "SELECT x FROM t ORDER BY x ASC", (True, True)),
    ("SELECT x FROM t ORDER BY x", ALL, (True, False)),
    (ALL, "SELECT x FROM t ORDER BY x DESC", (False, True)),
])
def test_ordering_agreement_and_one_order_bags(query_a, query_b, ordered):
    with engine(query_a, query_b) as db:
        assert db.inputs.ordered == ordered
        for rows in (((-1,), (0,)), ((0,), (-1,)), ((None,), (-1,))):
            assert not differs(*db.compare(rows), sequential=all(ordered))
        found, count, _ = search(db)
        assert found is None
        assert count == 29


def test_two_column_order_keys_differ():
    inputs = parse("CREATE TABLE t (x INT, y INT)",
                   "SELECT x, y FROM t ORDER BY x", "SELECT x, y FROM t ORDER BY y")
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, count, _ = search(db)
        assert count == 19
        assert found[0] == ((None, -1), (-1, 0))
        assert found[1][0].rows == ((-1, 0), (None, -1))
        assert found[1][1].rows == ((None, -1), (-1, 0))
        assert not differs(*found[1])
        assert differs(*found[1], sequential=True)


@pytest.mark.parametrize("suffix", [
    "ORDER BY 1", "ORDER BY x, y", "ORDER BY x NULLS FIRST",
    "ORDER BY x DESC NULLS FIRST", "ORDER BY x + 1", "ORDER BY abs(x)",
    "ORDER BY t.x", "ORDER BY z", "ORDER BY x LIMIT 1", "ORDER BY x OFFSET 1",
])
def test_order_gate_rejections(suffix):
    with pytest.raises(Unsupported):
        parse("CREATE TABLE t (x INT, y INT)", f"SELECT x FROM t {suffix}", ALL)


def test_ordered_replay_checks_sequence_not_just_bags(tmp_path, capsys):
    assert main(check_args(tmp_path, "SELECT x FROM t ORDER BY x",
                           "SELECT x FROM t ORDER BY x DESC")) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    for result in payload["results"]:
        result["rows"].reverse()
    path.write_text(json.dumps(payload))
    capsys.readouterr()
    assert main(["replay", str(path.parent)]) == 3
    assert "Replay did not match the recorded results" in capsys.readouterr().out


def test_old_unordered_policy_still_replays_with_bag_semantics(tmp_path):
    # Reverse stored rows without changing the bag or legacy policy identifier.
    inputs = parse(SCHEMA, FILTERED, ALL)
    rows = ((None,), (1,))
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(rows)
    artifact.export(tmp_path / "finding", inputs, Config(), rows, results, 1)
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["comparison_policy"] = "integer-bags-v1"
    del payload["comparison_order"]
    payload["versions"]["python"] = "old unchecked version"
    payload["search"]["generator"] = "integer-constraints-v3"
    payload["results"][1]["rows"].reverse()
    path.write_text(json.dumps(payload))
    assert main(["replay", str(path.parent)]) == 0


def test_sequence_comparison_preserves_width_nulls_and_integer_normalization():
    a = Result(("INTEGER",), ((None,), (1,), (1,)))
    b = Result(("BIGINT",), ((None,), (1,), (1,)))
    assert not differs(a, b, sequential=True)
    assert differs(a, Result(("INTEGER",), ((None,), (1,))), sequential=True)
    assert differs(Result(("INTEGER",), ()), Result(("INTEGER", "INTEGER"), ()), sequential=True)


@pytest.mark.parametrize("query_a,query_b,rows,checked,expected_a,expected_b", [
    ("SELECT COUNT(*) FROM t", "SELECT COUNT(x) FROM t", ((None,),), 2, ((1,),), ((0,),)),
    ("SELECT COUNT(x) FROM t", "SELECT COUNT(DISTINCT x) FROM t",
     ((-1,), (-1,)), 8, ((2,),), ((1,),)),
])
def test_aggregate_pipeline(tmp_path, monkeypatch, capsys, query_a, query_b, rows,
                            checked, expected_a, expected_b):
    with engine(query_a, query_b) as db:
        assert db.inputs.ordered == (False, False)
        assert db.inputs.query_a == query_a
        found, count, _ = search(db)
        assert count == checked
        assert found[0] == rows
        assert found[1][0].rows == expected_a
        assert found[1][1].rows == expected_b
        assert found[1][0].types == ("BIGINT",)
        assert reduce_rows(db, rows) == found
        assert not differs(*db.compare(rows[:-1]))
    assert main(check_args(tmp_path, query_a, query_b)) == 0
    output = capsys.readouterr().out
    assert "Result bag A:" in output
    assert "Result bag B:" in output
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [list(row) for row in rows]
    assert payload["search"]["candidates_checked"] == checked
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


def test_not_null_count_star_and_column_agree(tmp_path, capsys):
    assert main(check_args(tmp_path, "SELECT COUNT(*) FROM t", "SELECT COUNT(x) FROM t",
                           "CREATE TABLE t (x INT NOT NULL)")) == 1
    output = capsys.readouterr().out
    assert "Checked 22 candidates" in output
    assert "Outcome: no counterexample within budget" in output


def test_count_distinct_nulls_are_not_a_witness():
    with engine("SELECT COUNT(x) FROM t", "SELECT COUNT(DISTINCT x) FROM t") as db:
        results = db.compare(((None,), (None,)))
        assert results[0].rows == results[1].rows == ((0,),)
        assert not differs(*results)


@pytest.mark.parametrize("rows,expected", [
    ((), ((0, 0, 0, None, None, None),)),
    (((None,), (None,)), ((2, 0, 0, None, None, None),)),
    (((None,), (-1,), (2,), (2,)), ((4, 3, 2, 3, -1, 2),)),
])
def test_all_aggregates_against_duckdb(rows, expected):
    query = "SELECT COUNT(*) AS n, COUNT(x), COUNT(DISTINCT x), SUM(x) AS total, MIN(x), MAX(x) FROM t"
    with engine(query, query) as db:
        results = db.compare(rows)
        assert results[0].rows == results[1].rows == expected
        assert not differs(*results)


def test_aggregate_where_and_two_columns():
    query = "SELECT COUNT(y), SUM(y), MIN(x), MAX(y) FROM t WHERE x >= 1"
    inputs = parse("CREATE TABLE t (x INT, y INT)", query, query)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(((None, 9), (0, 8), (1, 3), (2, None)))
        assert results[0].rows == ((1, 3, 1, 3),)
        assert not differs(*results)
        results = db.compare(((0, 8),))
        assert results[0].rows == ((0, None, None, None),)


def test_sum_accepts_result_larger_than_input_integer_width():
    with engine("SELECT SUM(x) FROM t", "SELECT SUM(x) AS total FROM t") as db:
        results = db.compare(((2147483647,), (2147483647,)))
        assert results[0].rows == ((4294967294,),)
        assert not differs(*results)


@pytest.mark.parametrize("query", [
    "SELECT AVG(x) FROM t", "SELECT x, COUNT(*) FROM t", "SELECT COUNT(*), x FROM t",
    "SELECT COUNT(1) FROM t", "SELECT COUNT() FROM t", "SELECT COUNT(*) + 1 FROM t",
    "SELECT COUNT(*) FILTER (WHERE x > 0) FROM t", "SELECT SUM(x) OVER () FROM t",
    "SELECT COUNT(*) FROM t GROUP BY 1", "SELECT COUNT(*) FROM t HAVING COUNT(*) > 0",
    "SELECT DISTINCT COUNT(*) FROM t", "SELECT COUNT(*) FROM t ORDER BY x",
    "SELECT COUNT(DISTINCT *) FROM t", "SELECT COUNT(DISTINCT 1) FROM t",
    "SELECT COUNT(DISTINCT x, y) FROM t", "SELECT COUNT(x, y) FROM t",
    "SELECT SUM(DISTINCT x) FROM t", "SELECT MIN(x, y) FROM t",
    "SELECT MAX(*) FROM t", "SELECT SUM(x + 1) FROM t", "SELECT COUNT(t.x) FROM t",
    "SELECT COUNT(z) FROM t", "SELECT SUM(COUNT(x)) FROM t",
])
def test_aggregate_gate_rejections(query):
    with pytest.raises(Unsupported):
        parse("CREATE TABLE t (x INT, y INT)", query, ALL)


def test_aggregate_projection_limit_and_unchanged_generator_prefix():
    query = "SELECT " + ", ".join(["COUNT(*)"] * 16) + " FROM t"
    inputs = parse(SCHEMA, query, query)
    iterator = core.candidates(Config(), inputs)
    assert [next(iterator) for _ in range(5)] == [(), ((None,),), ((-1,),), ((0,),), ((1,),)]
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        assert db.compare(())[0].rows == ((0,) * 16,)
        with pytest.raises(ValueError, match="Invalid data"):
            db.compare(((True,),))
    with pytest.raises(Unsupported):
        parse(SCHEMA, "SELECT " + ", ".join(["COUNT(*)"] * 17) + " FROM t", ALL)


@pytest.mark.parametrize("query_a,query_b,rows,count,expected_a,expected_b", [
    ("SELECT x, COUNT(*) FROM t GROUP BY x", "SELECT x, COUNT(x) FROM t GROUP BY x",
     ((None,),), 2, ((None, 1),), ((None, 0),)),
    ("SELECT COUNT(*) FROM t", "SELECT COUNT(*) FROM t GROUP BY x",
     (), 1, ((0,),), ()),
])
def test_grouped_pipeline(tmp_path, monkeypatch, query_a, query_b, rows, count, expected_a, expected_b):
    with engine(query_a, query_b) as db:
        found, checked, _ = search(db)
        assert checked == count
        assert found[0] == rows
        assert found[1][0].rows == expected_a
        assert found[1][1].rows == expected_b
        assert reduce_rows(db, rows) == found
        if rows:
            empty = db.compare(())
            assert empty[0].rows == empty[1].rows == ()
            assert empty[0].types == empty[1].types == ("INTEGER", "BIGINT")
    assert main(check_args(tmp_path, query_a, query_b)) == 0
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [list(row) for row in rows]
    assert payload["search"]["candidates_checked"] == count
    assert payload["results"][0]["rows"] == [list(row) for row in expected_a]
    assert payload["results"][1]["rows"] == [list(row) for row in expected_b]
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


@pytest.mark.parametrize("schema,query_a,query_b,count", [
    ("CREATE TABLE t (x INT NOT NULL)", "SELECT x, COUNT(*) FROM t GROUP BY x",
     "SELECT x, COUNT(x) FROM t GROUP BY x", 22),
    (SCHEMA, "SELECT x FROM t GROUP BY x", "SELECT DISTINCT x FROM t", 29),
])
def test_grouped_agreeing_pairs(tmp_path, capsys, schema, query_a, query_b, count):
    assert main(check_args(tmp_path, query_a, query_b, schema)) == 1
    output = capsys.readouterr().out
    assert f"Checked {count} candidates" in output
    assert "Outcome: no counterexample within budget" in output


@pytest.mark.parametrize("query,expected", [
    ("SELECT x, y, COUNT(*) FROM t GROUP BY x, y", ((None, 3, 1), (1, None, 1), (1, 2, 2))),
    ("SELECT x AS key, SUM(y) AS total FROM t GROUP BY x", ((None, 3), (1, 4))),
    ("SELECT COUNT(DISTINCT y), MIN(y), MAX(y) FROM t GROUP BY x", ((1, 3, 3), (1, 2, 2))),
    ("SELECT x, SUM(y) FROM t WHERE y = 2 GROUP BY x", ((1, 4),)),
    ("SELECT y FROM t GROUP BY x, y", ((3,), (None,), (2,))),
])
def test_two_column_grouped_results(query, expected):
    inputs = parse("CREATE TABLE t (x INT, y INT)", query, query)
    assert inputs.ordered == (False, False)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(((None, 3), (1, None), (1, 2), (1, 2)))
        assert results[0].bag() == Result(results[0].types, expected).bag()
        assert not differs(*results)
        assert db.compare(())[0].rows == ()


@pytest.mark.parametrize("query", [
    "SELECT x FROM t GROUP BY 1", "SELECT x FROM t GROUP BY x + 1",
    "SELECT COUNT(*) FROM t GROUP BY x IS NULL", "SELECT x FROM t GROUP BY x, x",
    "SELECT x FROM t GROUP BY x, X", "SELECT x FROM t GROUP BY x, y, x",
    "SELECT x FROM t GROUP BY z", "SELECT x FROM t GROUP BY t.x",
    "SELECT x, COUNT(*) FROM t GROUP BY y", "SELECT y FROM t GROUP BY x",
    "SELECT x FROM t GROUP BY x HAVING AVG(x) > 0", "SELECT x FROM t GROUP BY x ORDER BY x",
    "SELECT DISTINCT x FROM t GROUP BY x", "SELECT x, COUNT(*) FROM t",
    "SELECT COUNT(*) FROM t GROUP BY ()", "SELECT x FROM t GROUP BY ALL",
    "SELECT x FROM t GROUP BY ROLLUP(x)", "SELECT x FROM t GROUP BY CUBE(x)",
    "SELECT x FROM t GROUP BY GROUPING SETS ((x))", "SELECT x AS k FROM t GROUP BY k",
    "SELECT x FROM t GROUP BY x LIMIT 1",
])
def test_group_gate_rejections(query):
    with pytest.raises(Unsupported):
        parse("CREATE TABLE t (x INT, y INT)", query, ALL)


def test_grouped_generator_prefix_unchanged():
    inputs = parse(SCHEMA, "SELECT x, COUNT(*) FROM t GROUP BY x", ALL)
    iterator = core.candidates(Config(), inputs)
    assert [next(iterator) for _ in range(5)] == [(), ((None,),), ((-1,),), ((0,),), ((1,),)]


def test_having_pipeline(tmp_path, monkeypatch):
    query_a = "SELECT x, COUNT(*) FROM t GROUP BY x"
    query_b = query_a + " HAVING COUNT(*) > 1"
    with engine(query_a, query_b) as db:
        found, checked, _ = search(db)
        assert checked == 2
        assert found[0] == ((None,),)
        assert found[1][0].rows == ((None, 1),)
        assert found[1][1].rows == ()
        assert reduce_rows(db, found[0]) == found
        assert not differs(*db.compare(()))
        duplicates = db.compare(((-1,), (-1,)))
        assert duplicates[0].rows == duplicates[1].rows == ((-1, 2),)
    assert main(check_args(tmp_path, query_a, query_b)) == 0
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [[None]]
    assert payload["search"]["candidates_checked"] == 2
    assert payload["results"][0]["rows"] == [[None, 1]]
    assert payload["results"][1]["rows"] == []
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


def test_having_every_group_has_at_least_one_row():
    query = "SELECT x, COUNT(*) FROM t GROUP BY x"
    with engine(query, query + " HAVING COUNT(*) >= 1") as db:
        assert db.inputs.literals == (1,)
        assert core.interesting_values(db.inputs.literals) == (None, -1, 0, 1, 2)
        assert search(db) == (None, 36, "bounded candidate schedule exhausted")


@pytest.mark.parametrize("having,expected", [
    ("x > 0", ((1, 2),)),
    ("x IS NULL", ((None, 1),)),
    ("x IS NOT NULL AND NOT (COUNT(*) <= 1)", ((1, 2),)),
    ("COUNT(*) > 1 OR x IS NULL", ((None, 1), (1, 2))),
    ("COUNT(y) = COUNT(DISTINCT y)", ((None, 1), (-1, 1))),
    ("SUM(y) >= 4 AND MIN(y) = MAX(y)", ((1, 2),)),
    ("0 < COUNT(*)", ((None, 1), (-1, 1), (1, 2))),
    ("SUM(y) = NULL", ()),
])
def test_having_predicates_against_duckdb(having, expected):
    query = f"SELECT x, COUNT(*) FROM t GROUP BY x HAVING {having}"
    inputs = parse("CREATE TABLE t (x INT, y INT)", query, query)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(((None, 3), (-1, None), (1, 2), (1, 2)))
        assert results[0].bag() == Result(results[0].types, expected).bag()
        assert not differs(*results)


def test_having_ast_literals_and_where_separation():
    query = ("SELECT x FROM t WHERE x >= -7 GROUP BY x "
             "HAVING COUNT(*) > 42 AND x < 2147483647 /* 8765 */")
    inputs = parse(SCHEMA, query, ALL)
    assert inputs.literals == (-7, 42, 2147483647)
    domain = core.interesting_values(inputs.literals)
    assert 42 in domain and 41 in domain and 43 in domain
    assert 8765 not in domain and 2147483648 not in domain
    iterator = core.candidates(Config(), inputs)
    assert [next(iterator) for _ in range(5)] == [(), ((None,),), ((-1,),), ((0,),), ((1,),)]
    with pytest.raises(Unsupported):
        parse(SCHEMA, "SELECT x FROM t WHERE COUNT(*) > 1 GROUP BY x", ALL)


@pytest.mark.parametrize("having", [
    "AVG(x) IS NULL", "1 IS NULL", "NULL IS NULL",
    "AVG(x) > 0", "COUNT(1) > 0", "COUNT() > 0", "COUNT(DISTINCT x, y) > 0",
    "COUNT(*) FILTER (WHERE x > 0) > 1", "x + 1 > 0", "x > (SELECT 1)",
    "y > 0", "y IS NULL", "t.x > 0", "1 = 1", "TRUE",
    "COUNT(*) > 1.5", "COUNT(*) > '1'", "COUNT(*) > 2147483648",
    "COUNT(*) > -2147483649", "SUM(DISTINCT y) > 0",
])
def test_having_gate_rejections(having):
    with pytest.raises(Unsupported):
        parse("CREATE TABLE t (x INT, y INT)",
              f"SELECT x, COUNT(*) FROM t GROUP BY x HAVING {having}", ALL)


@pytest.mark.parametrize("query", [
    "SELECT COUNT(*) FROM t HAVING COUNT(*) > 0",
    "SELECT x FROM t HAVING x > 0",
    "SELECT DISTINCT x FROM t GROUP BY x HAVING COUNT(*) > 0",
    "SELECT x FROM t GROUP BY x HAVING COUNT(*) > 0 ORDER BY x",
])
def test_having_requires_group_and_keeps_group_restrictions(query):
    with pytest.raises(Unsupported):
        parse(SCHEMA, query, ALL)


@pytest.mark.parametrize("join_kind", ["JOIN", "INNER JOIN"])
def test_inner_self_join_pipeline(tmp_path, monkeypatch, join_kind):
    query = f"SELECT a.x FROM t AS a {join_kind} t AS b ON a.x = b.x"
    with engine(ALL, query) as db:
        assert db.inputs.query_b == query
        found, checked, _ = search(db)
        assert checked == 2
        assert found[0] == ((None,),)
        assert found[1][0].rows == ((None,),)
        assert found[1][1].rows == ()
        assert reduce_rows(db, found[0]) == found
        assert not differs(*db.compare(((1,),)))
        results = db.compare(((1,), (1,)))
        assert results[0].rows == ((1,), (1,))
        assert results[1].rows == ((1,),) * 4
    assert main(check_args(tmp_path, ALL, query)) == 0
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [[None]]
    assert payload["search"]["candidates_checked"] == 2
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


def test_self_join_agrees_with_itself_and_keeps_generator_prefix():
    query = "SELECT a.x FROM t AS a JOIN t AS b ON a.x = b.x"
    with engine(query, query) as db:
        assert search(db) == (None, 29, "bounded candidate schedule exhausted")
        iterator = core.candidates(Config(), db.inputs)
        assert [next(iterator) for _ in range(5)] == [(), ((None,),), ((-1,),), ((0,),), ((1,),)]


def test_two_column_self_join_results():
    query = "SELECT a.x AS first, b.y AS second FROM t AS a JOIN t AS b ON a.x = b.y"
    inputs = parse("CREATE TABLE t (x INT, y INT)", query, query)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(((1, 2), (2, 1), (None, 1)))
        expected = Result(("INTEGER", "INTEGER"), ((1, 1), (1, 1), (2, 2)))
        assert results[0].bag() == expected.bag()
        assert not differs(*results)


def test_join_on_where_scope_and_literal_harvest():
    query = ("SELECT a.x, b.y FROM t AS a JOIN t AS b "
             "ON (a.x = b.y OR a.x IS NULL) AND b.y >= -42 "
             "WHERE NOT (a.y IS NULL) AND b.x < 7 /* 8765 */")
    inputs = parse("CREATE TABLE t (x INT, y INT)", query, query)
    assert inputs.literals == (-42, 7)
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(((None, 1), (1, None), (2, 1)))
        assert results[0].rows == ((None, 1),)
        assert not differs(*results)


@pytest.mark.parametrize("query", [
    "SELECT a.x FROM t a LEFT JOIN t b ON a.x = b.x",
    "SELECT a.x FROM t a RIGHT JOIN t b ON a.x = b.x",
    "SELECT a.x FROM t a FULL JOIN t b ON a.x = b.x",
    "SELECT a.x FROM t a CROSS JOIN t b",
    "SELECT a.x FROM t a CROSS JOIN t b ON a.x = b.x",
    "SELECT a.x FROM t a NATURAL JOIN t b",
    "SELECT a.x FROM t a JOIN t b USING (x)",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x JOIN t c ON a.x=c.x",
    "SELECT a.x FROM t a JOIN t b", "SELECT a.x FROM t a, t b",
    "SELECT x FROM t a JOIN t b ON a.x=b.x",
    "SELECT a.x FROM t a JOIN t b ON x=b.x",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x WHERE x > 0",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x WHERE c.x IS NULL",
    "SELECT a.x FROM t a JOIN t b ON a.x=c.x",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.z",
    "SELECT t.x FROM t a JOIN t b ON a.x=b.x",
    "SELECT a.x FROM t JOIN t b ON a.x=b.x",
    "SELECT a.x FROM t a JOIN t ON a.x=b.x",
    "SELECT a.x FROM t a JOIN t A ON a.x=A.x",
    "SELECT a.x FROM t a JOIN other b ON a.x=b.x",
    "SELECT a.x FROM other a JOIN t b ON a.x=b.x",
    "SELECT a.x FROM t a JOIN main.t b ON a.x=b.x",
    "SELECT a.x FROM t a JOIN t b(x) ON a.x=b.x",
    "SELECT DISTINCT a.x FROM t a JOIN t b ON a.x=b.x",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x ORDER BY a.x",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x GROUP BY a.x",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x HAVING a.x > 0",
    "SELECT COUNT(*) FROM t a JOIN t b ON a.x=b.x",
    "SELECT a.x FROM t a JOIN t b ON COUNT(*) > 0",
    "SELECT a.x FROM t a JOIN t b ON 1=1",
    "SELECT a.x FROM t a JOIN t b ON a.x+1=b.x",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x LIMIT 1",
    "SELECT x FROM t AS a", "SELECT t.x FROM t",
])
def test_self_join_gate_rejections(query):
    with pytest.raises(Unsupported):
        parse(SCHEMA, query, ALL)


def test_two_column_distant_literals_find_and_replay(tmp_path, monkeypatch):
    schema = "CREATE TABLE t (x INTEGER, y INTEGER)"
    query_a = "SELECT x, y FROM t WHERE x = 0 AND y = 42"
    query_b = "SELECT x, y FROM t WHERE x = 1 AND y = 42"
    inputs = parse(schema, query_a, query_b)
    schedule = list(core.candidates(Config(), inputs))
    assert ((0, 42),) in schedule
    assert ((1, 42),) in schedule
    assert schedule.index(((0, 42),)) < schedule.index(((1, 42),))
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        found, checked, _ = search(db)
        assert checked == 23
        assert found[0] == ((0, 42),)
        assert found[1][0].rows == ((0, 42),)
        assert found[1][1].rows == ()
        assert reduce_rows(db, found[0]) == found
    assert main(check_args(tmp_path, query_a, query_b, schema)) == 0
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [[0, 42]]
    assert payload["search"]["candidates_checked"] == 23
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0


def test_singleton_pairs_precede_larger_tables_when_budget_runs_out():
    query = "SELECT x, y FROM t WHERE x = 0 OR x = 1 OR y = 42"
    inputs = parse("CREATE TABLE t (x INT, y INT)", query, query)
    config = Config()
    assert config.max_candidates == 64
    assert len(core.interesting_values(inputs.literals)) ** 2 == 64
    iterator = core.candidates(config, inputs)
    checked_prefix = [next(iterator) for _ in range(config.max_candidates)]
    assert checked_prefix[0] == ()
    assert all(len(rows) == 1 for rows in checked_prefix[1:])
    assert len(next(iterator)) == 1
    assert len(next(iterator)) == 2
    with Engine(inputs, config, time.monotonic() + 5) as db:
        assert search(db) == (None, 64, "candidate budget exhausted")


def test_having_sum_null_pipeline(tmp_path, monkeypatch):
    query_a = "SELECT x, SUM(x) FROM t GROUP BY x HAVING SUM(x) IS NULL"
    query_b = "SELECT x, SUM(x) FROM t GROUP BY x HAVING SUM(x) = NULL"
    with engine(query_a, query_b) as db:
        assert db.compare(())[0].rows == db.compare(())[1].rows == ()
        found, checked, _ = search(db)
        assert checked == 2
        assert found[0] == ((None,),)
        assert found[1][0].rows == ((None, None),)
        assert found[1][1].rows == ()
        assert reduce_rows(db, found[0]) == found
    assert main(check_args(tmp_path, query_a, query_b)) == 0
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["rows"] == [[None]]
    assert payload["search"]["candidates_checked"] == 2
    assert payload["results"][0]["rows"] == [[None, None]]
    assert payload["results"][1]["rows"] == []
    monkeypatch.setattr(core, "candidates", lambda *_: pytest.fail("Replay generated data"))
    assert main(["replay", str(directory)]) == 0
    assert_sql_reproduction(directory)


@pytest.mark.parametrize("rows,expected", [
    (((1, None),), ((1, None),)),
    (((None, None),), ((None, None),)),
    (((1, None), (1, 2), (0, None)), ((0, None),)),
])
def test_two_column_having_sum_missing(rows, expected):
    query = "SELECT x, SUM(y) FROM t GROUP BY x HAVING SUM(y)"
    inputs = parse("CREATE TABLE t (x INT, y INT)", query + " IS NULL", query + " = NULL")
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(rows)
        assert results[0].bag() == Result(results[0].types, expected).bag()
        assert results[1].rows == ()
        assert differs(*results)


@pytest.mark.parametrize("aggregate", ["COUNT(*)", "COUNT(y)", "COUNT(DISTINCT y)",
                                      "SUM(y)", "MIN(y)", "MAX(y)"])
def test_having_supported_aggregate_null_checks(aggregate):
    query = f"SELECT x FROM t GROUP BY x HAVING {aggregate}"
    inputs = parse("CREATE TABLE t (x INT, y INT)", query + " IS NULL", query + " IS NOT NULL")
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(((0, None), (1, 2)))
        if aggregate.startswith("COUNT"):
            expected_null, expected_not_null = (), ((0,), (1,))
        else:
            expected_null, expected_not_null = ((0,),), ((1,),)
        assert results[0].bag() == Result(("INTEGER",), expected_null).bag()
        assert results[1].bag() == Result(("INTEGER",), expected_not_null).bag()


@pytest.mark.parametrize("query", [
    "SELECT x FROM t WHERE SUM(x) IS NULL",
    "SELECT x FROM t WHERE COUNT(*) IS NOT NULL",
    "SELECT a.x FROM t a JOIN t b ON SUM(a.x) IS NULL",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x WHERE SUM(a.x) IS NULL",
    "SELECT x FROM t GROUP BY x HAVING AVG(x) IS NULL",
    "SELECT x FROM t GROUP BY x HAVING y IS NULL",
    "SELECT x FROM t GROUP BY x HAVING 1 IS NULL",
    "SELECT x FROM t GROUP BY x HAVING COUNT(1) IS NULL",
])
def test_having_null_check_boundaries(query):
    with pytest.raises(Unsupported):
        parse("CREATE TABLE t (x INT, y INT)", query, ALL)


@pytest.mark.parametrize("query", [
    "SELECT +x FROM t", "SELECT x FROM t WHERE x = +1",
    "SELECT x FROM t WHERE +x = 1", "SELECT +COUNT(*) FROM t",
    "SELECT +a.x FROM t a JOIN t b ON a.x=b.x",
    "SELECT a.x FROM t a JOIN t b ON +a.x=b.x",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x WHERE +b.x=1",
    "SELECT x FROM t GROUP BY x HAVING +COUNT(*) > 1",
])
def test_dropped_plus_is_unsupported(tmp_path, capsys, query):
    with pytest.raises(Unsupported, match="drops source token PLUS"):
        parse(SCHEMA, query, ALL)
    assert main(check_args(tmp_path, query)) == 2
    output = capsys.readouterr().out
    assert "Outcome: unsupported input" in output
    assert "drops source token PLUS" in output
    assert not (tmp_path / "finding").exists()


@pytest.mark.parametrize("predicate", ["x ISNULL", "x NOTNULL", "x IS UNKNOWN"])
def test_dropped_null_spellings_are_unsupported(predicate):
    with pytest.raises(Unsupported, match="drops source token"):
        parse(SCHEMA, f"SELECT x FROM t WHERE {predicate}", ALL)


@pytest.mark.parametrize("query", [
    "SELECT x FROM t", "SELECT x FROM t WHERE x = -1",
    "SELECT x FROM t WHERE x IS NOT NULL",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x",
    "SELECT x FROM t /* +COUNT(*) */ WHERE x = -1 -- +x",
])
def test_token_counts_allow_retained_syntax(query):
    schema = "CREATE TABLE t (x INTEGER);"
    inputs = parse(schema, query, query)
    assert inputs.schema == schema
    assert inputs.query_a == inputs.query_b == query
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        assert not differs(*db.compare(((None,), (-1,), (1,))))


@pytest.mark.parametrize("query", [
    "SELECT x FROM t WHERE x IS NOT NULL -- +x is only a comment",
    "SELECT a.x FROM t a JOIN t b ON a.x=b.x",
])
def test_original_source_export_after_token_check(tmp_path, query):
    schema = "CREATE TABLE t (x INTEGER);"
    assert main(check_args(tmp_path, query, ALL, schema)) == 0
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["schema"] == schema
    assert payload["query_a"] == query
    assert query in (directory / "reproduce.sql").read_text()
    assert query in (directory / "witness.txt").read_text()
    assert main(["replay", str(directory)]) == 0


@pytest.mark.parametrize("query_a,query_b", [
    ("SELECT x FROM t ORDER BY x", ALL),
    (ALL, "SELECT x FROM t ORDER BY x DESC"),
])
def test_one_sided_order_no_find_prints_notice(tmp_path, capsys, query_a, query_b):
    assert main(check_args(tmp_path, query_a, query_b)) == 1
    output = capsys.readouterr().out
    assert "Checked 29 candidates" in output
    assert "Order was not compared; bags only" in output


@pytest.mark.parametrize("reverse", [False, True])
def test_one_sided_order_finding_records_bags_and_notice(tmp_path, capsys, reverse):
    queries = ["SELECT x FROM t WHERE x = x ORDER BY x", ALL]
    if reverse:
        queries.reverse()
    assert main(check_args(tmp_path, *queries)) == 0
    assert "Order was not compared; bags only" in capsys.readouterr().out
    directory = tmp_path / "finding"
    payload = json.loads((directory / "witness.json").read_text())
    assert payload["comparison_policy"] == "integer-position-v2"
    assert payload["comparison_order"] == "bags"
    assert "Order was not compared; bags only" in (directory / "witness.txt").read_text()
    assert main(["replay", str(directory)]) == 0
    output = capsys.readouterr().out
    assert "Order was not compared; bags only" in output
    assert "Result bag A:" in output


def test_legacy_ordered_witness_infers_sequences(tmp_path, capsys):
    assert main(check_args(tmp_path, "SELECT x FROM t ORDER BY x",
                           "SELECT x FROM t ORDER BY x DESC")) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["comparison_policy"] = "integer-bags-v1"
    del payload["comparison_order"]
    path.write_text(json.dumps(payload))
    assert main(["replay", str(path.parent)]) == 0
    assert "Result sequence A:" in capsys.readouterr().out
    for result in payload["results"]:
        result["rows"].reverse()
    path.write_text(json.dumps(payload))
    assert main(["replay", str(path.parent)]) == 3


def test_replay_uses_recorded_bags_not_sql_inference(tmp_path, capsys):
    inputs = parse(SCHEMA, "SELECT x FROM t WHERE x = x ORDER BY x", "SELECT x FROM t ORDER BY x DESC")
    rows = ((None,), (-1,), (0,))
    with Engine(inputs, Config(), time.monotonic() + 5) as db:
        results = db.compare(rows)
    artifact.export(tmp_path / "finding", inputs, Config(), rows, results, 1)
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["comparison_order"] = "bags"
    for result in payload["results"]:
        result["rows"].reverse()
    path.write_text(json.dumps(payload))
    assert main(["replay", str(path.parent)]) == 0
    output = capsys.readouterr().out
    assert "Result bag A:" in output
    assert "Result sequence A:" not in output


@pytest.mark.parametrize("mode", [None, "invalid", "missing"])
def test_v2_replay_requires_valid_recorded_mode(tmp_path, mode):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    if mode == "missing":
        del payload["comparison_order"]
    else:
        payload["comparison_order"] = mode
    path.write_text(json.dumps(payload))
    assert main(["replay", str(path.parent)]) == 3


@pytest.mark.parametrize("args", [["--help"], ["--version"], ["check", "--help"], ["replay", "--help"]])
def test_help_version_exit_without_policy(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert output.strip()
    assert "Comparison policy:" not in output
    assert "Order was not compared" not in output
    assert "Outcome:" not in output


def test_budget_help_describes_cooperative_limits(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["check", "--help"])
    assert exc.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "Elapsed budget for DuckDB search/compare/replay" in output
    assert "cooperative interrupt, not a process kill" in output
    assert "Parsing and export are not cut off by the watchdog" in output
    assert "DuckDB buffer-manager budget only; does not cap the Python process" in output


@pytest.mark.parametrize("command", ["check", "replay"])
def test_spent_parse_budget_never_opens_engine(tmp_path, monkeypatch, capsys, command):
    from types import SimpleNamespace
    import query_witness.cli as cli

    args = check_args(tmp_path)
    operation = "parse"
    if command == "replay":
        assert main(args) == 0
        args = ["replay", str(tmp_path / "finding")]
        operation = "load"
    capsys.readouterr()
    clock = [100.0]
    original = getattr(cli, operation)

    def slow_parse(*args):
        result = original(*args)
        clock[0] += 6.0
        return result

    def unexpected_engine(*args):
        pytest.fail("Spent parsing budget must be rejected before Engine")

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cli, operation, slow_parse)
    monkeypatch.setattr(cli, "Engine", unexpected_engine)
    assert main(args) == 4
    output = capsys.readouterr().out
    assert "Outcome: resource limit reached" in output
    assert "Elapsed-time budget reached" in output
    assert "Outcome: counterexample found" not in output
    if command == "check":
        assert not (tmp_path / "finding").exists()


def test_export_finishes_after_deadline(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import query_witness.cli as cli

    original = cli.export
    clock = [time.monotonic()]

    def slow_export(*args):
        clock[0] += 6.0
        original(*args)

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cli, "export", slow_export)
    assert main(check_args(tmp_path)) == 0
    assert "Outcome: counterexample found" in capsys.readouterr().out
    assert main(["replay", str(tmp_path / "finding")]) == 0


@pytest.mark.parametrize("args", [[], ["unknown"], ["check"], ["replay"]])
def test_argument_errors_fail_without_policy(args, capsys):
    assert main(args) == 3
    output = capsys.readouterr().out
    assert "Outcome: execution failure" in output
    assert "Comparison policy:" not in output


def test_missing_schema_failure_includes_path(tmp_path, capsys):
    args = check_args(tmp_path)
    missing = tmp_path / "missing-schema.sql"
    args[args.index("--schema") + 1] = str(missing)
    assert main(args) == 3
    output = capsys.readouterr().out
    assert "Outcome: execution failure" in output
    assert str(missing) in output
    assert "Comparison policy:" in output
    assert "unsupported input" not in output


@pytest.mark.parametrize("error", [KeyError, TypeError, RecursionError, RuntimeError])
def test_internal_search_errors_propagate(tmp_path, monkeypatch, capsys, error):
    import query_witness.cli as cli
    def broken_search(*_):
        raise error("internal bug")
    monkeypatch.setattr(cli, "search", broken_search)
    with pytest.raises(error, match="internal bug"):
        main(check_args(tmp_path))
    assert "Outcome:" not in capsys.readouterr().out
    assert not (tmp_path / "finding").exists()


@pytest.mark.parametrize("field_path", [
    ("versions",), ("config",), ("schema",), ("query_a",), ("query_b",), ("rows",), ("results",),
    ("versions", "duckdb"), ("versions", "sqlglot"), ("versions", "query_witness"),
    ("config", "max_rows"), ("config", "timeout_seconds"),
    ("results", 0, "types"), ("results", 1, "rows"),
])
def test_witness_missing_fields_are_value_errors(tmp_path, capsys, field_path):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    parent = payload
    for field in field_path[:-1]:
        parent = parent[field]
    del parent[field_path[-1]]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Witness missing/invalid field"):
        artifact.load(path.parent)
    capsys.readouterr()
    assert main(["replay", str(path.parent)]) == 3
    output = capsys.readouterr().out
    assert "Outcome: execution failure" in output
    assert "Witness missing/invalid field" in output


@pytest.mark.parametrize("field_path,value", [
    (("versions",), []), (("versions", "duckdb"), None), (("config",), []),
    (("config", "extra"), 1), (("config", "max_rows"), "4"),
    (("schema",), None), (("query_a",), []), (("query_b",), 1),
    (("rows",), None), (("rows",), [1]),
    (("results",), {}), (("results",), []), (("results", 0), None),
    (("results", 0, "types"), "INTEGER"), (("results", 0, "types"), [[]]),
    (("results", 0, "rows"), None), (("results", 0, "rows"), [1]),
    (("results", 0, "rows"), [[{}]]),
])
def test_malformed_witness_fields_fail_without_internal_exceptions(tmp_path, field_path, value):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    parent = payload
    for field in field_path[:-1]:
        parent = parent[field]
    parent[field_path[-1]] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        artifact.load(path.parent)
    assert main(["replay", str(path.parent)]) == 3


@pytest.mark.parametrize("source,expected", [
    ("SELECT x FROM t;  \n", "SELECT x FROM t;\n"),
    ("SELECT x FROM t", "SELECT x FROM t\n;\n"),
    ("SELECT x FROM t -- trailing comment", "SELECT x FROM t -- trailing comment\n;\n"),
    ("SELECT x FROM t -- comment;", "SELECT x FROM t -- comment;\n;\n"),
    ("SELECT x FROM t; -- comment;", "SELECT x FROM t; -- comment;\n"),
])
def test_sql_statement_terminator(source, expected):
    assert artifact.sql_statement(source) == expected


def test_export_publishes_only_complete_directory(tmp_path, monkeypatch):
    destination = tmp_path / "finding"
    staging = tmp_path / "finding.exporting"
    original_write = artifact.Path.write_text
    original_rename = artifact.Path.rename
    written = []
    def observe_write(path, *args, **kwargs):
        if path.parent == staging:
            assert not destination.exists()
            written.append(path.name)
        return original_write(path, *args, **kwargs)
    def observe_rename(path, target):
        assert path == staging and target == destination
        assert not destination.exists()
        assert sorted(written) == ["reproduce.sql", "witness.json", "witness.txt"]
        assert sorted(p.name for p in staging.iterdir()) == sorted(written)
        return original_rename(path, target)
    monkeypatch.setattr(artifact.Path, "write_text", observe_write)
    monkeypatch.setattr(artifact.Path, "rename", observe_rename)
    assert main(check_args(tmp_path)) == 0
    assert not staging.exists()
    assert_sql_reproduction(destination)
    assert main(["replay", str(destination)]) == 0


@pytest.mark.parametrize("filename", ["witness.json", "reproduce.sql", "witness.txt"])
def test_export_write_failure_cleans_staging_and_allows_retry(tmp_path, monkeypatch, capsys, filename):
    args = check_args(tmp_path)
    original_write = artifact.Path.write_text
    def fail_write(path, *values, **kwargs):
        if path.parent.name == "finding.exporting" and path.name == filename:
            original_write(path, "partial", encoding="utf-8")
            raise OSError("injected export write failure")
        return original_write(path, *values, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(artifact.Path, "write_text", fail_write)
        assert main(args) == 3
    output = capsys.readouterr().out
    assert "injected export write failure" in output
    assert "Outcome: counterexample found" not in output
    assert not (tmp_path / "finding").exists()
    assert not (tmp_path / "finding.exporting").exists()
    assert main(args) == 0


def test_existing_staging_is_reported_and_left_untouched(tmp_path, capsys):
    args = check_args(tmp_path)
    staging = tmp_path / "finding.exporting"
    staging.mkdir()
    marker = staging / "partial"
    marker.write_text("leftover")
    assert main(args) == 3
    output = capsys.readouterr().out
    assert "Export staging path already exists" in output
    assert str(staging) in output
    assert marker.read_text() == "leftover"
    assert not (tmp_path / "finding").exists()


def test_destination_appearing_during_export_is_not_removed(tmp_path, monkeypatch):
    args = check_args(tmp_path)
    destination = tmp_path / "finding"
    original_write = artifact.Path.write_text
    def create_competing_destination(path, *values, **kwargs):
        result = original_write(path, *values, **kwargs)
        if path.parent.name == "finding.exporting" and path.name == "witness.txt":
            destination.mkdir()
            original_write(destination / "existing", "keep")
        return result
    monkeypatch.setattr(artifact.Path, "write_text", create_competing_destination)
    assert main(args) == 3
    assert (destination / "existing").read_text() == "keep"
    assert not (tmp_path / "finding.exporting").exists()


@pytest.mark.parametrize("policy", [core.POLICY_ID, "integer-bags-v1"])
@pytest.mark.parametrize("ordered_count", [0, 1, 2])
@pytest.mark.parametrize("mode", ["bags", "sequences"])
def test_replay_mode_compatible_with_ordering(tmp_path, monkeypatch, policy, ordered_count, mode):
    query_a = "SELECT x FROM t WHERE x=x" + (" ORDER BY x" if ordered_count else "")
    query_b = ALL.rstrip(";") + (" ORDER BY x DESC" if ordered_count == 2 else "")
    inputs = parse(SCHEMA, query_a, query_b)
    rows = ((None,), (1,), (0,))
    expected_a = ((0,), (1,)) if ordered_count else ((1,), (0,))
    expected_b = ((1,), (0,), (None,)) if ordered_count == 2 else rows
    results = (Result(("INTEGER",), expected_a), Result(("INTEGER",), expected_b))
    directory = tmp_path / "finding"
    artifact.export(directory, inputs, Config(), rows, results, 1)
    path = directory / "witness.json"
    payload = json.loads(path.read_text())
    payload["comparison_policy"] = policy
    payload["comparison_order"] = mode
    if mode == "bags":
        for result in payload["results"]:
            result["rows"].reverse()
    path.write_text(json.dumps(payload))
    if mode == "sequences" and ordered_count < 2:
        monkeypatch.setattr(Engine, "__enter__", lambda *_: pytest.fail("Invalid mode opened an engine"))
        with pytest.raises(ValueError, match="Sequence comparison requires ORDER BY in both queries"):
            artifact.load(directory)
        assert main(["replay", str(directory)]) == 3
    else:
        assert main(["replay", str(directory)]) == 0


@pytest.mark.parametrize("query_b", ["SELECT x FROM t GROUP BY x", "SELECT x FROM t ORDER BY x"])
def test_replay_rejects_unordered_sequence_only_difference(tmp_path, monkeypatch, query_b):
    inputs = parse(SCHEMA, ALL, query_b)
    rows = ((1,), (0,))
    with duckdb.connect(config={"threads": "1"}) as connection:
        connection.execute(SCHEMA)
        connection.execute("INSERT INTO t VALUES (1), (0)")
        assert connection.execute(ALL).fetchall() == [(1,), (0,)]
        assert connection.execute(query_b).fetchall() == [(0,), (1,)]
    results = (Result(("INTEGER",), rows), Result(("INTEGER",), tuple(reversed(rows))))
    directory = tmp_path / "finding"
    artifact.export(directory, inputs, Config(), rows, results, 1)
    path = directory / "witness.json"
    payload = json.loads(path.read_text())
    payload["comparison_order"] = "sequences"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(Engine, "__enter__", lambda *_: pytest.fail("Invalid mode opened an engine"))
    assert main(["replay", str(directory)]) == 3


@pytest.mark.parametrize("opening,closing", [("[", "]"), ('{"nested":', "}")])
def test_nested_witness_json_is_input_failure(tmp_path, opening, closing):
    (tmp_path / "witness.json").write_text(opening * 2000 + "0" + closing * 2000)
    result = subprocess.run([sys.executable, "-m", "query_witness", "replay", str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "Outcome: execution failure" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_replay_rejects_non_integer_format_version(tmp_path, version):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["format_version"] = version
    path.write_text(json.dumps(payload))
    assert main(["replay", str(path.parent)]) == 3


@pytest.mark.parametrize("dtype", ["INT4", "INT32", "int4"])
def test_int_width_aliases_are_unsupported(dtype):
    with pytest.raises(Unsupported, match="INTEGER"):
        parse(f"CREATE TABLE t (x {dtype})", ALL, FILTERED)


def test_int_and_integer_remain_supported():
    parse("CREATE TABLE t (x INT)", ALL, FILTERED)
    parse("CREATE TABLE t (x INTEGER)", ALL, FILTERED)


def test_int4_as_table_or_column_name_is_supported():
    parse(
        "CREATE TABLE int4 (x INTEGER)",
        "SELECT x FROM int4",
        "SELECT x FROM int4 WHERE x = x",
    )
    parse(
        "CREATE TABLE t (int4 INTEGER)",
        "SELECT int4 FROM t",
        "SELECT int4 FROM t WHERE int4 = int4",
    )


def test_replay_too_many_rows_is_invalid_data(tmp_path, capsys):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    payload = json.loads(path.read_text())
    payload["config"]["max_rows"] = 1
    payload["rows"] = [[-1], [0]]
    path.write_text(json.dumps(payload))
    capsys.readouterr()
    assert main(["replay", str(path.parent)]) == 3
    output = capsys.readouterr().out
    assert "Outcome: execution failure" in output
    assert "more rows than recorded max_rows" in output


def test_native_parse_interrupt_is_a_limit(tmp_path, monkeypatch, capsys):
    def boom(_self, _source):
        raise duckdb.InterruptException("INTERRUPT")
    monkeypatch.setattr(duckdb.DuckDBPyConnection, "extract_statements", boom)
    assert main(check_args(tmp_path)) == 4
    output = capsys.readouterr().out
    assert "interrupted" in output.lower() or "budget" in output.lower()


def test_witness_limit_is_bytes(tmp_path):
    assert main(check_args(tmp_path)) == 0
    path = tmp_path / "finding" / "witness.json"
    path.write_bytes(b"a" * 1_048_577)
    assert main(["replay", str(path.parent)]) == 3


def test_query_interrupted_is_cancellation(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("Query interrupted")
    monkeypatch.setattr(Engine, "compare", boom)
    with pytest.raises(KeyboardInterrupt):
        main(check_args(tmp_path))


@pytest.mark.parametrize("sql,params", [
    ("SELECT SUM(i) FROM range(1000000000000) t(i)", None),
    ("INSERT INTO t SELECT 0 FROM (SELECT SUM(i) AS s FROM range(?) t(i)) WHERE s > 0",
     [1000000000000]),
])
def test_sigint_during_duckdb_work_is_cancellation(tmp_path, sql, params):
    # Long real work selects the native cancellation boundary without extending the SQL gate.
    source = '''
import json, sys
sys.path.insert(0, sys.argv[1])
import duckdb
from query_witness import cli, core
opened = []
def compare(engine, rows):
    opened.append(engine)
    print("ready for interrupt", flush=True)
    engine.execute(sys.argv[2], json.loads(sys.argv[3]))
    raise AssertionError("Expected interruption")
core.Engine.compare = compare
try:
    raise SystemExit(cli.main(sys.argv[4:]))
finally:
    for engine in opened:
        assert not engine.timer.is_alive()
        try:
            engine.connection.execute("SELECT 1")
        except duckdb.ConnectionException:
            print("connection closed; watchdog stopped", flush=True)
        else:
            raise AssertionError("Connection still open")
'''
    command = [sys.executable, "-u", "-c", source, str(Path(core.__file__).resolve().parents[1]),
               sql, json.dumps(params), *check_args(tmp_path)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first = process.stdout.readline()
        assert first.startswith("Comparison policy:"), first
        assert process.stdout.readline() == "ready for interrupt\n"
        time.sleep(0.15)
        process.send_signal(signal.SIGINT)
        output, errors = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode in (-signal.SIGINT, 130), output + errors
    assert "connection closed; watchdog stopped" in output
    assert "Outcome:" not in output
    assert not (tmp_path / "finding").exists()
    assert not (tmp_path / "finding.exporting").exists()


@pytest.mark.parametrize("phase", ["construction", "start"])
@pytest.mark.parametrize("error", [MemoryError, KeyboardInterrupt, RuntimeError])
def test_watchdog_initialization_failure_closes_connection(monkeypatch, phase, error):
    failure = error("injected startup failure")
    def fail(*args, **kwargs):
        raise failure
    if phase == "construction":
        monkeypatch.setattr(core.threading, "Thread", fail)
    else:
        monkeypatch.setattr(core.threading.Thread, "start", fail)
    db = engine()
    with pytest.raises(error) as observed:
        with db:
            pytest.fail("Startup failure must prevent entry")
    assert observed.value is failure
    with pytest.raises(duckdb.ConnectionException):
        db.connection.execute("SELECT 1")
    assert db.timer is None or not db.timer.is_alive()


def test_watchdog_thread_exhaustion_is_limit_and_closes_connection(tmp_path, monkeypatch, capsys):
    connections = []
    original_connect = core.duckdb.connect
    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection
    def fail_start(*args):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(core.duckdb, "connect", connect)
    monkeypatch.setattr(core.threading.Thread, "start", fail_start)
    assert main(check_args(tmp_path)) == 4
    output = capsys.readouterr().out
    assert "Outcome: resource limit reached" in output
    assert "watchdog" in output
    assert len(connections) == 1
    with pytest.raises(duckdb.ConnectionException):
        connections[0].execute("SELECT 1")
    assert not (tmp_path / "finding").exists()
    assert not (tmp_path / "finding.exporting").exists()


@pytest.mark.parametrize("name,position", [
    ("asc", "table"), ("asc", "column"), ("asc", "query_a"), ("asc", "query_b"),
    ("aSc", "table"), ("pivot_wider", "column"),
])
@pytest.mark.parametrize("command", ["check", "replay"])
def test_duckdb_reserved_identifiers_are_unsupported(tmp_path, monkeypatch, capsys, name, position, command):
    schema, a, b = SCHEMA, FILTERED, ALL
    if position == "table":
        schema = f"CREATE TABLE {name} (x INTEGER)"
        a, b = f"SELECT x FROM {name} WHERE x=x", f"SELECT x FROM {name}"
    elif position == "column":
        schema = f"CREATE TABLE t ({name} INTEGER)"
        a, b = f"SELECT {name} FROM t WHERE {name}={name}", f"SELECT {name} FROM t"
    else:
        joined = f"SELECT {name}.x FROM t AS {name} JOIN t AS b ON {name}.x=b.x"
        if position == "query_a":
            a = joined
        else:
            b = joined
    inputs = parse(schema, a, b)
    invalid_source = schema if position in ("table", "column") else joined
    with duckdb.connect(config={"threads": "1"}) as connection:
        with pytest.raises(duckdb.ParserException):
            connection.extract_statements(invalid_source)
    args = check_args(tmp_path, a, b, schema)
    if command == "replay":
        artifact.export(tmp_path / "finding", inputs, Config(), ((None,),),
                        (Result(("INTEGER",), ()), Result(("INTEGER",), ((None,),))), 1)
        args = ["replay", str(tmp_path / "finding")]
    opened, executed = [], []
    original_enter, original_execute = Engine.__enter__, Engine.execute

    def enter(db):
        opened.append(db)
        return original_enter(db)

    def execute(db, sql, params=None):
        executed.append(sql)
        return original_execute(db, sql, params)

    monkeypatch.setattr(Engine, "__enter__", enter)
    monkeypatch.setattr(Engine, "execute", execute)
    assert main(args) == 2
    output = capsys.readouterr().out
    assert "Outcome: unsupported input" in output
    assert "DuckDB cannot parse source SQL" in output
    assert "Outcome: counterexample found" not in output
    assert executed == []
    assert len(opened) == 1
    assert not opened[0].timer.is_alive()
    with pytest.raises(duckdb.ConnectionException):
        opened[0].connection.execute("SELECT 1")
    if command == "check":
        assert not (tmp_path / "finding").exists()
    assert not (tmp_path / "finding.exporting").exists()


@pytest.mark.parametrize("sql,error", [("SELECT FROM", "ParserException"),
                                      ("SELECT missing FROM t", "BinderException")])
def test_internal_engine_sql_errors_remain_failures(tmp_path, monkeypatch, capsys, sql, error):
    import query_witness.cli as cli

    opened = []

    def broken_search(db):
        opened.append(db)
        db.execute(sql)
        pytest.fail("Expected real DuckDB error")

    monkeypatch.setattr(cli, "search", broken_search)
    assert main(check_args(tmp_path)) == 3
    output = capsys.readouterr().out
    assert "Outcome: execution failure" in output
    assert error in output
    assert "Outcome: unsupported input" not in output
    assert not opened[0].timer.is_alive()
    with pytest.raises(duckdb.ConnectionException):
        opened[0].connection.execute("SELECT 1")
    assert not (tmp_path / "finding").exists()


@pytest.mark.parametrize("phase", ["schedule", "reduction", "replay-first", "replay-last"])
@pytest.mark.parametrize("mode", ["bags", "sequences"])
@pytest.mark.parametrize("expired", [False, True])
def test_final_comparison_respects_deadline(tmp_path, monkeypatch, capsys, phase, mode, expired):
    from types import SimpleNamespace
    import query_witness.cli as cli

    a, b = (ALL, ALL) if phase == "schedule" else (FILTERED, ALL)
    if mode == "sequences" or phase.startswith("replay"):
        a, b = (source.rstrip(";") + " ORDER BY x" for source in (a, b))
    args = check_args(tmp_path, a, b)
    if phase == "schedule":
        args += ["--max-rows", "0"]
    if phase.startswith("replay"):
        assert main(args) == 0
        path = tmp_path / "finding" / "witness.json"
        payload = json.loads(path.read_text())
        payload["comparison_order"] = mode
        path.write_text(json.dumps(payload))
        args = ["replay", str(path.parent)]
    capsys.readouterr()
    clock = [100.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(core, "time", fake_time)
    monkeypatch.setattr(cli, "time", fake_time)
    opened = []
    reducing = False
    delayed = False
    replay_comparisons = 0
    original_reduce, original_differs = core.reduce_rows, core.differs

    def open_engine(*args):
        db = Engine(*args)
        opened.append(db)
        return db

    def reduce(db, rows):
        nonlocal reducing
        reducing = True
        return original_reduce(db, rows)

    def compare_results(a, b, **kwargs):
        nonlocal delayed, replay_comparisons
        result = original_differs(a, b, **kwargs)
        if phase.startswith("replay"):
            replay_comparisons += 1
        final_trial = reducing and a.rows == b.rows == ()
        selected = (phase == "schedule" or phase == "reduction" and final_trial
                    or phase == "replay-first" and replay_comparisons == 1
                    or phase == "replay-last" and replay_comparisons == 3)
        if selected and not delayed:
            assert result is (phase.startswith("replay") and replay_comparisons == 1)
            clock[0] = opened[0].deadline + (0.1 if expired else -0.1)
            delayed = True
        return result

    monkeypatch.setattr(cli, "Engine", open_engine)
    if phase.startswith("replay"):
        monkeypatch.setattr(cli, "differs", compare_results)
    else:
        monkeypatch.setattr(core, "differs", compare_results)
        monkeypatch.setattr(core, "reduce_rows", reduce)
    expected = 4 if expired else (1 if phase == "schedule" else 0)
    assert main(args) == expected
    assert delayed
    output = capsys.readouterr().out
    if expired:
        assert "Outcome: resource limit reached" in output
        assert "Outcome: counterexample found" not in output
        assert "Outcome: no counterexample within budget" not in output
    assert len(opened) == 1
    assert not opened[0].timer.is_alive()
    with pytest.raises(duckdb.ConnectionException):
        opened[0].connection.execute("SELECT 1")
    if not phase.startswith("replay") and (expired or phase == "schedule"):
        assert not (tmp_path / "finding").exists()
    assert not (tmp_path / "finding.exporting").exists()
