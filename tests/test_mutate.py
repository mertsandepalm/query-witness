from pathlib import Path

from query_witness import cli
from query_witness.cli import main
from query_witness.mutate import proposals
from query_witness.subset import statement


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "rewrite-mistakes"


def normalized(sql):
    return statement(sql).sql(dialect="duckdb")


def example(name):
    folder = EXAMPLES / name
    return (
        (folder / "schema.sql").read_text(encoding="utf-8"),
        (folder / "query-a.sql").read_text(encoding="utf-8"),
        (folder / "query-b.sql").read_text(encoding="utf-8"),
    )


def test_mutations_rediscover_catalog_rewrites():
    schema, query_a, query_b = example("assigned-count")
    rendered = {normalized(sql) for _, sql in proposals(schema, query_a)}
    assert normalized(query_b) in rendered

    schema, query_a, query_b = example("customer-distinct")
    rendered = {normalized(sql) for _, sql in proposals(schema, query_a)}
    assert normalized(query_b) in rendered

    schema, query_a, query_b = example("stock-boundary")
    rendered = {normalized(sql) for _, sql in proposals(schema, query_a)}
    assert normalized(query_b) in rendered

    schema, query_a, query_b = example("aggregate-pushdown")
    rendered = {normalized(sql) for _, sql in proposals(schema, query_a)}
    assert normalized(query_b) in rendered

    schema, query_a, query_b = example("join-fanout")
    rendered = {normalized(sql) for _, sql in proposals(schema, query_a)}
    assert normalized(query_b) in rendered


def test_mutate_cli_finds_assigned_count_witness(tmp_path, capsys):
    schema, query_a, _ = example("assigned-count")
    (tmp_path / "schema.sql").write_text(schema, encoding="utf-8")
    (tmp_path / "query-a.sql").write_text(query_a, encoding="utf-8")
    out = tmp_path / "mutations"
    assert main([
        "mutate",
        "--schema", str(tmp_path / "schema.sql"),
        "--query-a", str(tmp_path / "query-a.sql"),
        "--out", str(out),
    ]) == 0
    output = capsys.readouterr().out
    assert "Mutation: count-star-to-column-assignee_id" in output
    assert "Mutations with a witness:" in output
    witness = out / "count-star-to-column-assignee_id"
    assert (witness / "witness.json").is_file()
    assert main(["replay", str(witness)]) == 0


def test_mutate_cli_finds_catalog_cases(tmp_path, capsys):
    expected = {
        "customer-distinct": "drop-distinct",
        "stock-boundary": "gte-to-gt-1",
        "aggregate-pushdown": "push-sum-filter-to-where-1",
        "join-fanout": "self-join-on-customer_id",
    }
    for case, mutation in expected.items():
        capsys.readouterr()
        schema, query_a, _ = example(case)
        work = tmp_path / case
        work.mkdir()
        (work / "schema.sql").write_text(schema, encoding="utf-8")
        (work / "query-a.sql").write_text(query_a, encoding="utf-8")
        out = work / "mutations"
        assert main([
            "mutate",
            "--schema", str(work / "schema.sql"),
            "--query-a", str(work / "query-a.sql"),
            "--out", str(out),
        ]) == 0, case
        output = capsys.readouterr().out
        assert f"Mutation: {mutation}" in output, output
        assert (out / mutation / "witness.json").is_file(), case


def test_push_sum_filter_keeps_literal_side():
    schema, query_a, _ = example("aggregate-pushdown")
    reversed_having = """
SELECT customer_id, SUM(amount_cents) AS total_cents
FROM payments
GROUP BY customer_id
HAVING 10000 < SUM(amount_cents);
"""
    rendered = {normalized(sql) for name, sql in proposals(schema, reversed_having)
                if name.startswith("push-sum-filter-to-where")}
    assert any("10000 < amount_cents" in sql or "10000 < payments.amount_cents" in sql
               for sql in rendered)
    assert all("amount_cents < 10000" not in sql for sql in rendered)
    catalog = {normalized(sql) for name, sql in proposals(schema, query_a)
               if name.startswith("push-sum-filter-to-where")}
    assert any("amount_cents > 10000" in sql for sql in catalog)


def test_count_star_mutates_each_star():
    schema = "CREATE TABLE tickets (ticket_id INTEGER PRIMARY KEY, assignee_id INTEGER);"
    query_a = "SELECT COUNT(*), COUNT(*) FROM tickets;"
    names = [name for name, _ in proposals(schema, query_a)]
    assert "count-star-to-column-assignee_id-1" in names
    assert "count-star-to-column-assignee_id-2" in names


def test_self_join_skips_primary_key():
    schema, query_a, _ = example("stock-boundary")
    names = [name for name, _ in proposals(schema, query_a)]
    assert "self-join-on-item_id" not in names
    assert "self-join-on-quantity" in names


def test_mutate_does_not_create_out_when_nothing_applies(tmp_path, capsys):
    schema = "CREATE TABLE tickets (ticket_id INTEGER PRIMARY KEY);"
    query_a = "SELECT COUNT(ticket_id) FROM tickets;"
    (tmp_path / "schema.sql").write_text(schema, encoding="utf-8")
    (tmp_path / "query-a.sql").write_text(query_a, encoding="utf-8")
    out = tmp_path / "mutations"
    assert main([
        "mutate",
        "--schema", str(tmp_path / "schema.sql"),
        "--query-a", str(tmp_path / "query-a.sql"),
        "--out", str(out),
    ]) == 1
    assert "No in-subset rewrite mutations applied" in capsys.readouterr().out
    assert not out.exists()


def test_mutate_failure_is_not_success(tmp_path, monkeypatch, capsys):
    schema, query_a, _ = example("assigned-count")
    (tmp_path / "schema.sql").write_text(schema, encoding="utf-8")
    (tmp_path / "query-a.sql").write_text(query_a, encoding="utf-8")
    original = cli.check_pair
    calls = {"n": 0}

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("injected search failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "check_pair", wrapped)
    assert main([
        "mutate",
        "--schema", str(tmp_path / "schema.sql"),
        "--query-a", str(tmp_path / "query-a.sql"),
        "--out", str(tmp_path / "mutations"),
    ]) == 3
    assert "injected search failure" in capsys.readouterr().out


def test_mutate_all_unsupported_exits_unsupported(tmp_path, capsys):
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE asc (x INTEGER);", encoding="utf-8",
    )
    (tmp_path / "query-a.sql").write_text(
        "SELECT x FROM asc WHERE x >= 0;", encoding="utf-8",
    )
    assert main([
        "mutate",
        "--schema", str(tmp_path / "schema.sql"),
        "--query-a", str(tmp_path / "query-a.sql"),
        "--out", str(tmp_path / "mutations"),
    ]) == 2
    output = capsys.readouterr().out
    assert "unsupported input" in output
    assert output.rstrip().endswith("Mutations with a witness: 0 of 1.") or (
        "Mutations with a witness: 0 of" in output and output.count("Outcome: execution failure") == 0
    )
