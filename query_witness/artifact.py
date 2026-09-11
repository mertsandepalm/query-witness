"""Self-contained witness data; replay never calls the generator."""

from dataclasses import asdict
import json
from pathlib import Path
import shutil

import duckdb
import sqlglot
from sqlglot.tokens import Tokenizer, TokenType

from . import __version__
from .core import Config, ORDER_NOTICE, POLICY, POLICY_ID, Result, both_ordered, differs, validate_rows
from .subset import parse


def versions():
    return {"query_witness": __version__, "duckdb": duckdb.__version__,
            "sqlglot": sqlglot.__version__}


def sql_statement(source):
    source = source.rstrip()
    tokens = Tokenizer(dialect="duckdb").tokenize(source)
    if tokens and tokens[-1].token_type == TokenType.SEMICOLON:
        return source + "\n"
    # A newline prevents a trailing SQL line comment swallowing the terminator.
    return source + "\n;\n"


def inserts(inputs, rows):
    columns = ", ".join(f'"{column}"' for column in inputs.columns)
    statements = []
    for row in rows:
        values = ", ".join("NULL" if value is None else str(value) for value in row)
        statements.append(f'INSERT INTO "{inputs.table}" ({columns}) VALUES ({values});')
    return "\n".join(statements)


def result_text(result, sequential=False):
    lines = [f"  Columns: {len(result.types)}; observed types: {', '.join(result.types)}"]
    if sequential:
        if not result.rows:
            lines.append("  (empty sequence)")
        for position, row in enumerate(result.rows, start=1):
            values = ", ".join("NULL" if value is None else str(value) for value in row)
            lines.append(f"  {position}. ({values})")
        return "\n".join(lines)
    if not result.rows:
        lines.append("  (empty bag)")
    for row, count in sorted(result.bag().items(), key=lambda item: repr(item[0])):
        values = ", ".join("NULL" if value is None else str(value) for value in row)
        lines.append(f"  ({values}) × {count}")
    return "\n".join(lines)


def witness_text(inputs, rows, results, sequential):
    kind = "sequence" if sequential else "bag"
    return (f"Schema:\n{inputs.schema.strip()}\n\n"
            f"Data:\n{inserts(inputs, rows) or '-- empty table'}\n\n"
            f"Query A:\n{inputs.query_a.strip()}\nResult {kind} A:\n{result_text(results[0], sequential)}\n\n"
            f"Query B:\n{inputs.query_b.strip()}\nResult {kind} B:\n{result_text(results[1], sequential)}")


def export(directory, inputs, config, rows, results, checked):
    sequential = both_ordered(inputs)
    payload = {
        "format_version": 1, "versions": versions(), "comparison_policy": POLICY_ID,
        "comparison_order": "sequences" if sequential else "bags",
        "config": asdict(config), "schema": inputs.schema,
        "query_a": inputs.query_a, "query_b": inputs.query_b,
        "rows": rows, "results": [asdict(result) for result in results],
        "search": {"candidates_checked": checked,
                   "reduction": "row deletion completed; not globally minimal"},
    }
    reproduction = (f"-- Query Witness: {POLICY}\n"
                    "-- Run in a fresh DuckDB database using the recorded engine version.\n"
                    f"SET memory_limit = '{config.memory_mb}MB';\nSET threads = 1;\n"
                    "SET enable_external_access = false;\nSET max_temp_directory_size = '0B';\n"
                    + sql_statement(inputs.schema) + inserts(inputs, rows) + "\n"
                    + "-- Query A\n" + sql_statement(inputs.query_a)
                    + "-- Query B\n" + sql_statement(inputs.query_b))
    directory = Path(directory)
    notice = ""
    if inputs.ordered[0] != inputs.ordered[1]:
        notice = ORDER_NOTICE + "\n\n"
    readable = (
        "counterexample found\nComparison policy: " + POLICY + "\n\n"
        + notice + witness_text(inputs, rows, results, sequential)
        + "\n\nReduced by row deletion; not claimed globally minimal.\n"
        + "Replay: query-witness replay <this-directory>\n"
        + "Configuration and versions: see witness.json.\n")
    if directory.exists() or directory.is_symlink():
        raise FileExistsError(f"Output already exists: {directory}")
    temporary = directory.with_name(directory.name + ".exporting")
    try:
        temporary.mkdir()
    except FileExistsError:
        raise FileExistsError(f"Export staging path already exists (possibly an interrupted export): {temporary}") from None
    try:
        (temporary / "witness.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        (temporary / "reproduce.sql").write_text(reproduction, encoding="utf-8")
        (temporary / "witness.txt").write_text(readable, encoding="utf-8")
        if directory.exists() or directory.is_symlink():
            raise FileExistsError(f"Output already exists: {directory}")
        temporary.rename(directory)
    except BaseException:
        try:
            shutil.rmtree(temporary)
        except OSError:
            pass
        raise


def load(directory):
    with (Path(directory) / "witness.json").open(encoding="utf-8") as stream:
        source = stream.read(1_048_577)
    if len(source) > 1_048_576:
        raise ValueError("Witness exceeds the 1 MiB input limit")
    try:
        payload = json.loads(source)
    except RecursionError as exc:
        raise ValueError("Witness JSON exceeds parser nesting capacity") from exc
    if type(payload) is not dict or payload.get("format_version") != 1:
        raise ValueError("Unknown witness format")
    for field, expected_type in (("versions", dict), ("config", dict), ("schema", str),
                                 ("query_a", str), ("query_b", str), ("rows", list), ("results", list)):
        if type(payload.get(field)) is not expected_type:
            raise ValueError(f"Witness missing/invalid field: {field}")
    policy = payload.get("comparison_policy")
    if policy not in (POLICY_ID, "integer-bags-v1"):
        raise ValueError("Unknown comparison policy")
    recorded = payload["versions"]
    for component in ("query_witness", "duckdb", "sqlglot"):
        if type(recorded.get(component)) is not str:
            raise ValueError(f"Witness missing/invalid field: versions.{component}")
        if recorded[component] != versions()[component]:
            raise ValueError(f"Replay requires recorded {component} version {recorded[component]}")
    config_fields = set(asdict(Config()))
    if set(payload["config"]) != config_fields:
        raise ValueError("Witness missing/invalid field: config (expected "
                         + ", ".join(sorted(config_fields)) + ")")
    config = Config(**payload["config"])
    inputs = parse(payload["schema"], payload["query_a"], payload["query_b"])
    if policy == "integer-bags-v1" and "comparison_order" not in payload:
        comparison_order = "sequences" if both_ordered(inputs) else "bags"
    else:
        comparison_order = payload.get("comparison_order")
        if comparison_order not in ("bags", "sequences"):
            raise ValueError("Missing or invalid comparison_order")
    if comparison_order == "sequences" and not both_ordered(inputs):
        raise ValueError("Sequence comparison requires ORDER BY in both queries")
    raw_rows = payload["rows"]
    validate_rows(raw_rows, config.max_rows, inputs)
    rows = tuple(map(tuple, raw_rows))
    if len(payload["results"]) != 2:
        raise ValueError("Witness missing/invalid field: results (expected two results)")
    results = []
    for index, result in enumerate(payload["results"]):
        if type(result) is not dict:
            raise ValueError(f"Witness missing/invalid field: results[{index}]")
        types = result.get("types")
        if type(types) is not list or any(type(value) is not str for value in types):
            raise ValueError(f"Witness missing/invalid field: results[{index}].types")
        result_rows = result.get("rows")
        if type(result_rows) is not list or any(type(row) is not list for row in result_rows):
            raise ValueError(f"Witness missing/invalid field: results[{index}].rows")
        results.append(Result(tuple(types), tuple(map(tuple, result_rows))))
    results = tuple(results)
    if not differs(*results, sequential=comparison_order == "sequences"):
        raise ValueError("Recorded results do not contain a discrepancy")
    return inputs, config, rows, results, comparison_order
