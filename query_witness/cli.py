import argparse
from enum import IntEnum
from pathlib import Path
import time

import duckdb

from . import __version__
from .artifact import export, load, witness_text
from .core import Config, Engine, Limit, ORDER_NOTICE, POLICY, both_ordered, differs, search
from .mutate import proposals
from .subset import Unsupported, parse


class Outcome(IntEnum):
    FOUND = 0
    NO_COUNTEREXAMPLE = 1
    UNSUPPORTED = 2
    FAILURE = 3
    LIMIT = 4


LABELS = {Outcome.FOUND: "counterexample found",
          Outcome.NO_COUNTEREXAMPLE: "no counterexample within budget",
          Outcome.UNSUPPORTED: "unsupported input", Outcome.FAILURE: "execution failure",
          Outcome.LIMIT: "resource limit reached"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    root = Parser(description="Search for and replay DuckDB counterexamples (trusted local SQL only).")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Search for a counterexample")
    check.add_argument("--schema", required=True, type=Path)
    check.add_argument("--query-a", required=True, type=Path)
    check.add_argument("--query-b", required=True, type=Path)
    check.add_argument("--out", type=Path, default=Path("witness"))
    check.add_argument("--max-rows", type=int, default=4)
    check.add_argument("--max-candidates", type=int, default=64)
    check.add_argument("--timeout-seconds", type=float, default=5.0,
                       help="Elapsed budget for DuckDB search/compare/replay; cooperative interrupt, "
                            "not a process kill. Parsing and export are not cut off by the watchdog.")
    check.add_argument("--memory-mb", type=int, default=64,
                       help="DuckDB buffer-manager budget only; does not cap the Python process.")
    replay = commands.add_parser("replay", help="Replay exported data; no generation")
    replay.add_argument("directory", type=Path)
    mutate = commands.add_parser(
        "mutate",
        help="Apply named in-subset rewrite mistakes to query A and search each pair",
    )
    mutate.add_argument("--schema", required=True, type=Path)
    mutate.add_argument("--query-a", required=True, type=Path)
    mutate.add_argument("--out", type=Path, default=Path("mutations"))
    mutate.add_argument("--max-rows", type=int, default=4)
    mutate.add_argument("--max-candidates", type=int, default=64)
    mutate.add_argument("--timeout-seconds", type=float, default=5.0,
                        help="Elapsed budget per mutation; cooperative DuckDB interrupt.")
    mutate.add_argument("--memory-mb", type=int, default=64,
                        help="DuckDB buffer-manager budget only; does not cap the Python process.")
    return root


def read_sql(path):
    with path.open(encoding="utf-8") as stream:
        return stream.read(16_385)


def emit(outcome, diagnostic, detail=None):
    print(f"Outcome: {LABELS[outcome]}")
    print(diagnostic)
    if detail:
        print("\n" + detail)
    return int(outcome)


def check_pair(schema, query_a, query_b, config, out, start):
    inputs = parse(schema, query_a, query_b)
    if inputs.ordered[0] != inputs.ordered[1]:
        print(ORDER_NOTICE)
    if time.monotonic() >= start + config.timeout_seconds:
        raise Limit("Elapsed-time budget reached")
    with Engine(inputs, config, start + config.timeout_seconds) as engine:
        found, checked, reason = search(engine)
    if found is None:
        return emit(Outcome.NO_COUNTEREXAMPLE, f"Checked {checked} candidates; {reason}.")
    rows, results = found
    export(out, inputs, config, rows, results, checked)
    return emit(
        Outcome.FOUND,
        f"Checked {checked} candidates. Reduced by row deletion; not globally minimal.\n"
        f"Exported to {out}. Replay: query-witness replay {out}",
        witness_text(inputs, rows, results, both_ordered(inputs)),
    )


def mutate_command(args):
    schema = read_sql(args.schema)
    query_a = read_sql(args.query_a)
    config = Config(args.max_rows, args.max_candidates, args.timeout_seconds, args.memory_mb)
    applied = list(proposals(schema, query_a))
    if not applied:
        return emit(
            Outcome.NO_COUNTEREXAMPLE,
            "No in-subset rewrite mutations applied to this query.",
        )
    if args.out.exists() or args.out.is_symlink():
        raise FileExistsError(f"Output already exists: {args.out}")
    args.out.mkdir()
    found = 0
    limited = 0
    failed = 0
    print(f"Mutations: {len(applied)}")
    for name, query_b in applied:
        print(f"\nMutation: {name}")
        start = time.monotonic()
        try:
            code = check_pair(schema, query_a, query_b, config, args.out / name, start)
        except Unsupported as exc:
            code = emit(Outcome.UNSUPPORTED, str(exc))
        except (Limit, duckdb.OutOfMemoryException, MemoryError) as exc:
            code = emit(Outcome.LIMIT, str(exc) or "Memory exhausted")
        except (duckdb.Error, OSError, ValueError) as exc:
            code = emit(Outcome.FAILURE, f"{type(exc).__name__}: {exc}")
        if code == Outcome.FOUND:
            found += 1
        elif code == Outcome.LIMIT:
            limited += 1
        elif code == Outcome.FAILURE:
            failed += 1
    summary = f"Mutations with a witness: {found} of {len(applied)}."
    if found:
        return emit(Outcome.FOUND, summary)
    if failed:
        return emit(Outcome.FAILURE, summary)
    if limited:
        return emit(Outcome.LIMIT, summary)
    return emit(Outcome.NO_COUNTEREXAMPLE, summary)


def main(argv=None):
    start = time.monotonic()
    try:
        args = parser().parse_args(argv)
        print("Comparison policy: " + POLICY)
        if args.command == "mutate":
            return mutate_command(args)
        if args.command == "check":
            config = Config(args.max_rows, args.max_candidates, args.timeout_seconds, args.memory_mb)
            return check_pair(
                read_sql(args.schema), read_sql(args.query_a), read_sql(args.query_b),
                config, args.out, start,
            )
        inputs, config, rows, expected, comparison_order = load(args.directory)
        if inputs.ordered[0] != inputs.ordered[1]:
            print(ORDER_NOTICE)
        if time.monotonic() >= start + config.timeout_seconds:
            raise Limit("Elapsed-time budget reached")
        with Engine(inputs, config, start + config.timeout_seconds) as engine:
            observed = engine.compare(rows)
            sequential = comparison_order == "sequences"
            if not differs(*observed, sequential=sequential):
                raise ValueError("Replay did not reproduce the recorded discrepancy")
            for recorded, actual in zip(expected, observed):
                if differs(recorded, actual, sequential=sequential):
                    raise ValueError("Replay did not match the recorded results")
            engine.check_time()
        return emit(Outcome.FOUND, "Replay verified the exported data and both recorded results.",
                    witness_text(inputs, rows, observed, sequential))
    except Unsupported as exc:
        return emit(Outcome.UNSUPPORTED, str(exc))
    except (Limit, duckdb.OutOfMemoryException, MemoryError) as exc:
        return emit(Outcome.LIMIT, str(exc) or "Memory exhausted")
    except (duckdb.Error, OSError, ValueError) as exc:
        return emit(Outcome.FAILURE, f"{type(exc).__name__}: {exc}")
    except RuntimeError as exc:
        if isinstance(exc.__cause__, KeyboardInterrupt):
            raise exc.__cause__ from None
        raise


if __name__ == "__main__":
    raise SystemExit(main())
