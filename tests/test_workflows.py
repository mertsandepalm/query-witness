from collections import Counter
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import duckdb
import pytest


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "rewrite-mistakes"


def run_cli(arguments, directory):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return subprocess.run(
        [str(Path(sys.executable).with_name("query-witness")), *arguments],
        cwd=directory, env=environment, capture_output=True, text=True, timeout=30,
    )


def observe(schema, table, rows, queries):
    # DuckDB enforces the actual DDL, independently of Query Witness's validator.
    with duckdb.connect(config={"threads": "1"}) as connection:
        connection.execute(schema)
        width = len(connection.execute(f"DESCRIBE {table}").fetchall())
        for row in rows:
            assert len(row) == width
            assert all(value is None or type(value) is int and -(2**31) <= value < 2**31
                       for value in row)
            connection.execute(f"INSERT INTO {table} VALUES ({', '.join(['?'] * width)})", row)
        return [connection.execute(query).fetchall() for query in queries]


@pytest.mark.parametrize("case,table,query_files,manual_rows,expected,exit_code", [
    ("assigned-count", "tickets", ("query-a.sql", "query-b.sql"),
     [(101, None)], [[(1,)], [(0,)]], 0),
    ("customer-distinct", "purchases", ("query-a.sql", "query-b.sql"),
     [(7,), (7,)], [[(7,)], [(7,), (7,)]], 0),
    ("stock-boundary", "stock", ("query-a.sql", "query-b.sql"),
     [(101, 1)], [[(101,)], []], 0),
    ("aggregate-pushdown", "payments", ("query-a.sql", "query-b.sql"),
     [(7, 6000), (7, 6000)], [[(7, 12000)], []], 0),
    ("join-fanout", "purchases", ("query-a.sql", "query-b.sql"),
     [(7, 101), (7, 102)], [[(7, 101), (7, 102)], [(7, 101), (7, 101), (7, 102), (7, 102)]], 0),
    ("stock-boundary", "stock", ("query-a.sql", "equivalent.sql"),
     [(1, -(2**31)), (2, 0), (3, 1), (4, 2**31 - 1)], [[(3,), (4,)], [(3,), (4,)]], 1),
    ("join-fanout", "purchases", ("left-join.sql", "inner-matching.sql"),
     [(7, 1)], [[(7, 1)], []], 2),
], ids=["nullable-count", "duplicates", "filter-boundary", "aggregate-filter",
        "join-multiplicity", "equivalent-control", "unsupported-outer-join"])
def test_rewrite_workflow(tmp_path, case, table, query_files, manual_rows, expected, exit_code):
    example = EXAMPLES / case
    schema = (example / "schema.sql").read_text(encoding="utf-8")
    queries = [(example / filename).read_text(encoding="utf-8") for filename in query_files]
    manual_results = observe(schema, table, manual_rows, queries)
    assert [Counter(rows) for rows in manual_results] == [Counter(rows) for rows in expected]
    assert (Counter(expected[0]) == Counter(expected[1])) == (exit_code == 1)

    original = tmp_path / "original"
    original.mkdir()
    for filename, source in zip(("schema.sql", "a.sql", "b.sql"), (schema, *queries)):
        (original / filename).write_text(source, encoding="utf-8")
    checked = run_cli(["check", "--schema", "schema.sql", "--query-a", "a.sql",
                       "--query-b", "b.sql", "--out", "finding"], original)
    (tmp_path / "check.txt").write_text(checked.stdout + checked.stderr, encoding="utf-8")
    assert checked.returncode == exit_code, checked.stdout + checked.stderr
    assert not checked.stderr
    if exit_code:
        assert not (original / "finding").exists()
        assert not (original / "finding.exporting").exists()
        assert "Outcome: counterexample found" not in checked.stdout
        if exit_code == 1:
            assert "Outcome: no counterexample within budget" in checked.stdout
            assert "No outcome is an equivalence proof" in checked.stdout
        else:
            assert "Outcome: unsupported input" in checked.stdout
            assert "Unsupported option on Join" in checked.stdout
        print(case, query_files, checked.stdout.split("Outcome:")[-1].strip())
        return

    moved = tmp_path / "moved"
    moved.mkdir()
    witness = moved / "witness"
    (original / "finding").rename(witness)
    shutil.rmtree(original)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    replayed = run_cli(["replay", "../moved/witness"], elsewhere)
    (tmp_path / "replay.txt").write_text(replayed.stdout + replayed.stderr, encoding="utf-8")
    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    assert not replayed.stderr
    assert "Replay verified" in replayed.stdout
    assert not original.exists()
    assert {path.name for path in witness.iterdir()} == {"witness.json", "reproduce.sql", "witness.txt"}
    payload = json.loads((witness / "witness.json").read_text(encoding="utf-8"))
    assert payload["schema"] == schema
    assert [payload["query_a"], payload["query_b"]] == queries
    assert payload["comparison_order"] == "bags"
    assert len(payload["rows"]) <= payload["config"]["max_rows"] == 4
    assert 1 <= payload["search"]["candidates_checked"] <= 64
    observed = observe(schema, table, payload["rows"], queries)
    recorded = [[tuple(row) for row in result["rows"]] for result in payload["results"]]
    assert [Counter(rows) for rows in observed] == [Counter(rows) for rows in recorded]
    assert Counter(observed[0]) != Counter(observed[1])
    with duckdb.connect() as connection:
        script = (witness / "reproduce.sql").read_text(encoding="utf-8")
        assert all(query.strip() in script for query in queries)
        statements = connection.extract_statements(script)
        for statement in statements[:-2]:
            assert statement.type != duckdb.StatementType.SELECT
            connection.execute(statement)
        for statement, result in zip(statements[-2:], observed):
            assert statement.type == duckdb.StatementType.SELECT
            assert Counter(connection.execute(statement).fetchall()) == Counter(result)
    print(json.dumps({"case": case, "rows": payload["rows"], "results": observed,
                      "checked": payload["search"]["candidates_checked"]}))


def test_installed_input_failures_and_comment_export(tmp_path):
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (x INTEGER) -- schema;", encoding="utf-8")
    (tmp_path / "a.sql").write_text("SELECT COUNT(*) FROM t -- A;", encoding="utf-8")
    (tmp_path / "b.sql").write_text("SELECT COUNT(x) FROM t -- B;", encoding="utf-8")
    arguments = ["check", "--schema", "schema.sql", "--query-a", "a.sql", "--query-b", "b.sql",
                 "--out", "finding"]
    result = run_cli(arguments, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    with duckdb.connect() as connection:
        statements = connection.extract_statements((tmp_path / "finding/reproduce.sql").read_text())
        for statement in statements[:-2]:
            connection.execute(statement)
        assert [connection.execute(statement).fetchall() for statement in statements[-2:]] == [[(1,)], [(0,)]]
    witness = tmp_path / "finding/witness.json"
    payload = json.loads(witness.read_text())
    payload["config"]["timeout_seconds"] = 10**1000
    witness.write_text(json.dumps(payload))
    replayed = run_cli(["replay", "finding"], tmp_path)
    assert replayed.returncode == 3, replayed.stdout + replayed.stderr
    assert "Outcome: execution failure" in replayed.stdout
    assert "Traceback" not in replayed.stderr
    (tmp_path / "a.sql").write_text("SELECT x FROM t WHERE " + "(" * 300 + "x=1" + ")" * 300)
    rejected = run_cli(arguments[:-1] + ["rejected"], tmp_path)
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert "Outcome: unsupported input" in rejected.stdout
    assert "Traceback" not in rejected.stderr
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("case", ["assigned-count", "customer-distinct", "stock-boundary",
                                 "aggregate-pushdown", "join-fanout"])
def test_preserved_release_witness_replays(tmp_path, case):
    report = json.loads((EXAMPLES.parents[1] / "release-validation.json").read_text(encoding="utf-8"))
    record = next(record for record in report["cases"] if record["case"] == case)
    witness = tmp_path / "saved-witness"
    witness.mkdir()
    (witness / "witness.json").write_text(json.dumps(record["witness"]), encoding="utf-8")
    result = run_cli(["replay", "saved-witness"], tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Replay verified" in result.stdout
    assert not result.stderr
