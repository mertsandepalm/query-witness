# Query Witness

Give it two SQL queries. It looks for a **small, valid DuckDB table** where they
disagree, then saves that example so you can replay it later.

That is the whole product: not a proof that two queries are equivalent, and not
a general SQL debugger. When it finds a difference, you get the data, both
queries, and both results in a folder you can move around.

```text
two queries  →  try small tables  →  shrink the data  →  save a witness
                                                          →  replay it later
```

Live demo: [sandepalm.com/projects/query-witness](https://sandepalm.com/projects/query-witness)

## A mistake it can catch

Someone rewrites a ticket count from `COUNT(*)` to `COUNT(assignee_id)`.
Unassigned tickets still exist; their assignee is NULL. The first query counts
every ticket. The second counts only assigned ones.

On one unassigned ticket:

| ticket_id | assignee_id | `COUNT(*)` | `COUNT(assignee_id)` |
| ---: | ---: | ---: | ---: |
| -1 | NULL | 1 | 0 |

Query Witness finds a table like that, reduces it, and exports it.

## Install

Verified on **CPython 3.11–3.14, Linux x86_64**. Other platforms are unverified.
Runtime pins: DuckDB 1.5.5 and SQLGlot 30.18.0. [MIT licensed](LICENSE).

This is a Python CLI. Install it with pip, pipx, or uv — not npm, pnpm, or bun.

```sh
python -m pip install query-witness
```

Isolated tools:

```sh
pipx install query-witness
# or
uv tool install query-witness
```

Upgrade later with `pip install -U query-witness`, `pipx upgrade query-witness`,
or `uv tool upgrade query-witness`. Releases live on
[PyPI](https://pypi.org/project/query-witness/) and
[GitHub Releases](https://github.com/mertsandepalm/query-witness/releases).
If PyPI is not available yet, install the wheel from the latest GitHub Release.

Develop from source:

```sh
git clone https://github.com/mertsandepalm/query-witness.git
cd query-witness
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

With uv: `uv venv .venv` and `uv pip install --python .venv/bin/python -e '.[test]'`.

## Try it

```sh
.venv/bin/query-witness check \
  --schema examples/rewrite-mistakes/assigned-count/schema.sql \
  --query-a examples/rewrite-mistakes/assigned-count/query-a.sql \
  --query-b examples/rewrite-mistakes/assigned-count/query-b.sql \
  --out ticket-witness
.venv/bin/query-witness replay ticket-witness
```

Both commands should exit 0. Search checks two candidates and exports one row:
`(-1, NULL)`. Query A returns 1 and B returns 0. The primary key is unique and
non-NULL; `-1` is a legal INTEGER because the schema does not require positive IDs.

The saved folder contains:

- `witness.txt` — a readable copy of the finding
- `reproduce.sql` — the same example for a fresh DuckDB session
- `witness.json` — what `replay` actually trusts

You can move that folder anywhere and run `query-witness replay` on it. The
original SQL files are not needed. Existing output directories are never
overwritten; pick a new `--out` if you run check again.

Five constructed rewrite mistakes (duplicate customers, a `>= 1` vs `> 1`
filter, a HAVING filter moved into WHERE, and a join that multiplies rows, plus
the ticket count above) live in
[examples/rewrite-mistakes](examples/rewrite-mistakes/README.md).
More subset examples (NULL comparison, DISTINCT, ORDER BY, aggregates, GROUP BY,
HAVING, self-join, two columns) live under [examples/](examples/).

## Commands

```text
query-witness check --schema SCHEMA --query-a A --query-b B [--out DIR]
query-witness replay DIR
```

Defaults for check: `--max-rows 4`, `--max-candidates 64`, `--timeout-seconds 5`,
`--memory-mb 64`. A candidate is one database checked against both queries.

## What it can look at

This release is intentionally narrow:

- One table, one or two **INTEGER** columns
- Optional `NOT NULL` and at most one column-level `PRIMARY KEY`
- Filters, projections, `DISTINCT`, `ORDER BY`, `COUNT` / `SUM` / `MIN` / `MAX`,
  `GROUP BY` / `HAVING`, and one inner self-join

Strings, dates, outer joins, joins between different tables, and wider schemas
are out of scope. Unsupported SQL is rejected; it is never reported as a
counterexample. The [exact subset](#exact-current-subset) is below.

Search tries a short list of small tables. **No counterexample within that budget
does not mean the queries are equivalent.** Reduction is not globally minimal.

## What a run means

| Exit | Outcome | Meaning |
| ---: | --- | --- |
| 0 | counterexample found | A difference was found and exported, or verified by replay |
| 1 | no counterexample within budget | Finished the budget without a difference (not a proof) |
| 2 | unsupported input | SQL is outside this slice, or cannot be parsed |
| 3 | execution failure | Engine, file, config, or witness problem |
| 4 | resource limit reached | Time or memory limit stopped the run |

Trusted local SQL only. Timeouts ask DuckDB to stop; they do not kill the
process. `--memory-mb` caps DuckDB’s buffer manager, not Python. This is not a
sandbox. Do not expose it as a service that accepts untrusted SQL.

Columns compare by position, ignoring aliases. **Order is compared only when both
queries have ORDER BY**; otherwise rows compare as unordered bags that preserve
duplicates. DuckDB scan order is not a SQL ordering contract.

Replay requires matching recorded DuckDB and SQLGlot versions, plus a comparison
policy this CLI still understands. The Query Witness version in `witness.json` is
provenance; upgrading the CLI does not invalidate an older witness. It does not
need the original input paths or machine. Use `query-witness replay` to check
both recorded results; running `reproduce.sql` as one script in Python only
returns the last statement.

## Develop

```sh
.venv/bin/python -m pytest
```

`python -m query_witness` is also an entry point. Engineering notes for this
slice are in [AGENTS.md](AGENTS.md). How to cut a release is in
[RELEASING.md](RELEASING.md). The 0.1.0 validation record is in
[RELEASE.md](RELEASE.md).

## Exact current subset

- One `CREATE TABLE` with one or two `INTEGER`/`INT` columns with distinct names.
  Either column may have `NOT NULL`; at most one may have a column-level `PRIMARY KEY`.
- Simple unquoted ASCII identifiers; no schema qualification. Non-join queries
  use unqualified columns and no FROM alias. Self-join aliases are described below.
- Without GROUP BY, `SELECT` with 1–16 projections, either all declared-column
  references or all whole-table aggregates, optionally with output aliases.
  Supported aggregates: `COUNT(*)`, `COUNT(column)`, `COUNT(DISTINCT column)`,
  `SUM(column)`, `MIN(column)`, and `MAX(column)`. WHERE filters rows before
  aggregation. Mixing columns with aggregates, `AVG`, `COUNT()`, `COUNT(1)`,
  aggregate arithmetic, and `FILTER` are unsupported. HAVING without GROUP BY
  is unsupported.
- Optional `GROUP BY` one or two unique declared columns. Projected columns must
  be group keys and may mix with the supported aggregates; keys need not appear
  in the projection. Ordinals, expressions, duplicate or unknown keys, grouping
  sets, ROLLUP, and CUBE are unsupported. Query-level DISTINCT and ORDER BY are
  rejected on grouped queries.
- Optional HAVING on grouped queries uses the same Boolean operators and
  comparisons as WHERE, with the supported aggregates also allowed as operands.
  Bare column operands must be group keys. `IS NULL` / `IS NOT NULL` apply to
  group-key columns and the supported aggregates.
- Optional plain `SELECT DISTINCT`, including with the supported WHERE
  predicates. `DISTINCT ON` and `DISTINCT *` are unsupported.
  `COUNT(DISTINCT column)` is the exception inside COUNT.
- Optional `ORDER BY` one declared column, with `ASC` (default) or `DESC`.
  Multiple sort keys, ordinal `ORDER BY 1`, sort expressions, `NULLS FIRST`, and
  `LIMIT` are unsupported.
- One statement per file, at most 16384 characters each. Source SQL is executed
  unchanged. SQLGlot parses in the DuckDB dialect and gates AST structure.
  Execution, witness text, and `reproduce.sql` still use the original source.

Exactly one inner self-join is supported (`JOIN` or `INNER JOIN`). Both targets
must be the declared table and each must have a distinct, simple alias.
Projections, ON, and WHERE must qualify every column with one of those aliases.
Join queries cannot use DISTINCT, ORDER BY, GROUP BY, HAVING, or aggregates.
A second table, a second join, LEFT/RIGHT/FULL/CROSS joins, NATURAL, and USING
are unsupported.

| Schema | NULL allowed | Duplicate values allowed |
| --- | --- | --- |
| `CREATE TABLE t (x INTEGER)` | Yes | Yes |
| `CREATE TABLE t (x INTEGER NOT NULL)` | No | Yes |
| `CREATE TABLE t (x INTEGER PRIMARY KEY)` | No | No |

`PRIMARY KEY` implies `NOT NULL`. Constraints must be unnamed and column-level.
Table-level `PRIMARY KEY (x)`, `UNIQUE`, `DEFAULT`, `CHECK`, and foreign keys
are unsupported.

WHERE allows `column IS NULL` / `column IS NOT NULL`; comparisons `=`, `<>`,
`!=`, `==`, `<`, `<=`, `>`, `>=`; and `AND`, `OR`, `NOT`, with parentheses.
Each comparison operand must be a declared column, a signed 32-bit integer
literal, or NULL, and at least one operand must be a column. Arithmetic, strings,
floats, `IN`, `BETWEEN`, `CASE`, CTEs, and subqueries are unsupported.

## Prior art

SQL equivalence checking and counterexample generation are established areas,
including Cosette and SpotIt+. Query Witness does not claim to invent them. This
project explores a focused developer workflow for readable, executable findings.
