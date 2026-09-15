from pathlib import Path

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
