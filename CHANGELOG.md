# Release notes

## 0.1.1 — installable upgrades

Replay no longer requires the installed Query Witness version to match the
version recorded in `witness.json`. DuckDB 1.5.5, SQLGlot 30.18.0, format
version 1, and a known comparison policy are still required. `pip install -U
query-witness` can therefore keep replaying folders exported by 0.1.0.

Install with pip, pipx, or uv. GitHub Actions builds, verifies, and publishes
tagged releases. There is no npm package.

## 0.1.0 — release candidate

Query Witness searches small, valid DuckDB databases, reduces a discrepancy by
row deletion, and exports the source SQL, data, configuration, and both results.

- One table with one or two INTEGER columns; per-column NOT NULL and at most
  one column-level PRIMARY KEY.
- Column projections, DISTINCT, integer/null predicates, single-key ORDER BY,
  COUNT/SUM/MIN/MAX, GROUP BY, and HAVING within the README's fail-closed subset.
- One inner self-join with aliases; aggregate, grouped, distinct, and ordered
  join queries remain unsupported.
- Positional integer/NULL comparison preserves duplicates. Sequences are compared
  only when both queries have ORDER BY; otherwise comparison uses bags.
- Replay reads exported data without generation. This release records Query
  Witness 0.1.0, DuckDB 1.5.5, and SQLGlot 30.18.0. Python's version is not a
  replay pin. 0.1.1 keeps the engine pins and stops requiring the CLI version
  to match.
- Legacy `integer-bags-v1` witnesses remain readable. New exports record
  `integer-position-v2` and an explicit comparison mode.

No finding within budget proves nothing about equivalence. Reduction is not a
claim of global minimality. Trusted local SQL only: time limits are cooperative
DuckDB interrupts, and DuckDB's memory budget does not cap the Python process.

Release fixes include comment-safe SQL termination and explicit diagnostics for
SQL parser nesting exhaustion and invalid, enormous timeout values.
The correctness review also fixed contradictory replay sequence modes, preserved
Ctrl+C status for DuckDB-wrapped cancellation, classified nested JSON as invalid
input, and closed the connection when watchdog construction or startup fails.
Follow-up corrections check DuckDB's parser on all original SQL before execution
and classify its syntax rejections as unsupported input. Final search, reduction,
and replay comparisons now check the deadline before reporting completion.
Conservative bag replay for two ordered queries remains supported. SQL scope,
runtime pins, package version, and module structure are unchanged.

Five constructed [rewrite examples](examples/rewrite-mistakes/README.md) demonstrate
NULL counts, duplicate customers, filter boundaries, aggregate filter movement,
and join multiplicity. Installed-command tests check independent expected results,
schema-valid witnesses, SQL reproduction, and replay after removing the original
working directory, plus equivalent and unsupported controls.

MIT licensed. Verified on Linux x86_64 with CPython 3.11–3.14: 570 tests passed
on each interpreter. Package metadata limits Python support to `>=3.11,<3.15`.
Release tools are pinned, archive contents are explicit, and the validation report
and five replayable witness payloads are preserved in the source distribution.
See [RELEASE.md](RELEASE.md) for reproducible builds and the exact publishing steps.
No external publication was performed during preparation.
