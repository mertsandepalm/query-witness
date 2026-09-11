# Query Witness

Find a small valid DuckDB database on which two supplied queries return different
results, then export the data and both queries so the difference can be replayed.

## Scope and limits

- One table with one or two **INTEGER columns**. NOT NULL and one column-level
  PRIMARY KEY are supported; other constraints are rejected.
- Restricted filters, projections, DISTINCT, ORDER BY, COUNT/SUM/MIN/MAX,
  GROUP BY/HAVING, and one inner self-join. See the [exact subset](#exact-current-subset).
  Strings, dates, outer joins, joins between different tables, and wider schemas
  are outside this release.
- Search is bounded and samples a sparse schedule of small tables. **No
  counterexample within budget does not prove equivalence.** A witness establishes
  a difference under its recorded DuckDB conditions. Reduction is not globally minimal.
- Trusted local SQL and witness files only. Timeouts are cooperative; the memory
  limit applies to DuckDB, not the whole process. This is not a sandbox.

The verified release target is **CPython 3.11–3.14 on Linux x86_64**. Package
metadata excludes Python 3.15 and later until verified. Other operating systems
and Python implementations are unverified. Runtime dependencies are pinned to
DuckDB 1.5.5 and SQLGlot 30.18.0. [MIT licensed](LICENSE).

## Install and try one example

This constructed example counts tickets. An unassigned ticket has a NULL assignee.
`COUNT(*)` counts every ticket; changing it to `COUNT(assignee_id)` silently counts
only assigned tickets.

Use the local wheel from [the release build](RELEASE.md#rebuild-and-verify).
The following Bash commands work in an empty directory containing that wheel;
no checkout or external service is needed to run the example. Installation may
download the two pinned dependencies from PyPI.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install ./query_witness-0.1.0-py3-none-any.whl

mkdir ticket-example
cat > ticket-example/schema.sql <<'SQL'
CREATE TABLE tickets (ticket_id INTEGER PRIMARY KEY, assignee_id INTEGER);
SQL
cat > ticket-example/query-a.sql <<'SQL'
SELECT COUNT(*) AS ticket_count FROM tickets;
SQL
cat > ticket-example/query-b.sql <<'SQL'
SELECT COUNT(assignee_id) AS ticket_count FROM tickets;
SQL

.venv/bin/query-witness check \
  --schema ticket-example/schema.sql \
  --query-a ticket-example/query-a.sql \
  --query-b ticket-example/query-b.sql \
  --out ticket-witness
.venv/bin/query-witness replay ticket-witness
```

Search checks two candidates and exports one row: `(-1, NULL)`. The primary key
is unique and non-NULL; -1 is a legal INTEGER because the schema does not require
positive IDs. Query A returns 1 and B returns 0. Both commands exit 0.
`ticket-witness` contains `witness.json`, `witness.txt`, and `reproduce.sql`.
Move that directory anywhere and run `query-witness replay /path/to/ticket-witness`;
the input SQL files and original working directory are not needed. Replay requires
the recorded Query Witness, DuckDB, and SQLGlot versions. Existing output directories
are never overwritten, so choose a new `--out` when repeating a check.

See all five [constructed rewrite examples and their validation](examples/rewrite-mistakes/README.md),
the [release report and publishing steps](RELEASE.md), and [release notes](CHANGELOG.md).

## More examples and development

From a checkout or the extracted source distribution, using CPython 3.11–3.14:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/query-witness check \
  --schema examples/nullable/schema.sql \
  --query-a examples/nullable/query-a.sql \
  --query-b examples/nullable/query-b.sql \
  --out witness
.venv/bin/query-witness replay witness
.venv/bin/python -m pytest
```

With uv, use `uv venv .venv` and `uv pip install --python .venv/bin/python -e '.[test]'`.
`python -m query_witness` is also an entry point.

The example declares `CREATE TABLE t (x INTEGER)` with a **nullable** column.
It checks `SELECT x FROM t WHERE x = x` against `SELECT x FROM t`.
The reduced database contains one NULL row. Query A returns an empty bag; query B
returns `(NULL)` once. The CLI prints the schema, inserts, original queries, both
result bags, and the comparison policy. There is no semantic explanation engine.

The null-comparison example checks `WHERE x IS NULL` against `WHERE x = NULL`:

```sh
.venv/bin/query-witness check \
  --schema examples/null-comparison/schema.sql \
  --query-a examples/null-comparison/query-a.sql \
  --query-b examples/null-comparison/query-b.sql \
  --out null-witness
.venv/bin/query-witness replay null-witness
```

It also finds a one-row NULL witness in two candidates. Query A returns `(NULL)`
once; query B returns an empty bag.

The NOT NULL example uses the original query pair on a constrained column:

```sh
.venv/bin/query-witness check \
  --schema examples/not-null/schema.sql \
  --query-a examples/not-null/query-a.sql \
  --query-b examples/not-null/query-b.sql
```

This returns exit code 1, no counterexample within budget: generated tables cannot
contain NULL. A literal comparison still produces a finding on the same schema:

```sh
.venv/bin/query-witness check \
  --schema examples/not-null/schema.sql \
  --query-a examples/not-null/literal-a.sql \
  --query-b examples/not-null/literal-b.sql \
  --out not-null-witness
.venv/bin/query-witness replay not-null-witness
```

`x >= 1` versus `x > 1` finds the one-row witness `x = 1`.

The DISTINCT example uses the unconstrained nullable schema:

```sh
.venv/bin/query-witness check \
  --schema examples/nullable/schema.sql \
  --query-a examples/distinct/query-a.sql \
  --query-b examples/distinct/query-b.sql \
  --out distinct-witness
.venv/bin/query-witness replay distinct-witness
```

`SELECT DISTINCT x` versus `SELECT x` first differs on two NULL rows, at candidate
6: DISTINCT returns `(NULL)` once and the other query returns it twice. Reduction
keeps both rows because deleting one removes the discrepancy. With `NOT NULL`,
the first witness contains two `-1` rows. A one-column primary-key schema cannot witness this
DISTINCT-versus-non-DISTINCT difference: its column is unique and non-null,
so valid data cannot produce duplicate projected rows. Searches still report no
counterexample within budget, not an equivalence proof.

The two-column example compares `SELECT x` with `SELECT y`:

```sh
.venv/bin/query-witness check \
  --schema examples/two-columns/schema.sql \
  --query-a examples/two-columns/query-a.sql \
  --query-b examples/two-columns/query-b.sql \
  --out two-column-witness
.venv/bin/query-witness replay two-column-witness
```

The first finding is one row `(NULL, -1)`, at candidate 3. Query A returns `(NULL)`;
query B returns `(-1)`. Reduction keeps that row. Comparing `SELECT x, y` with
`SELECT y, x` also detects the positional difference. `WHERE x = y` versus an
unfiltered query differs earlier, at candidate 2 on `(NULL, NULL)`: the equality
does not evaluate to true.

Add `NOT NULL` to x while leaving y nullable:

```sh
.venv/bin/query-witness check \
  --schema examples/two-columns/not-null-schema.sql \
  --query-a examples/two-columns/query-a.sql \
  --query-b examples/two-columns/query-b.sql \
  --out constrained-two-column-witness
.venv/bin/query-witness replay constrained-two-column-witness
```

The legal prefix is empty, `[(-1, NULL)]`, then `[(-1, -1)]`. The first nonempty
table is the finding at checked candidate 2: x returns `(-1)` and y returns `(NULL)`. Tables
containing NULL in x are skipped before execution and do not spend the budget.

All interesting singleton pairs are searched, including non-adjacent values:

```sh
.venv/bin/query-witness check \
  --schema examples/two-columns/schema.sql \
  --query-a examples/two-columns/literal-a.sql \
  --query-b examples/two-columns/literal-b.sql \
  --out literal-pair-witness
.venv/bin/query-witness replay literal-pair-witness
```

The queries filter for `x = 0 AND y = 42` versus `x = 1 AND y = 42`.
Search finds `(0, 42)` at candidate 23, before `(1, 42)`. The first query returns
that row and the second returns an empty bag.

The ORDER BY example compares ascending and descending results:

```sh
.venv/bin/query-witness check \
  --schema examples/nullable/schema.sql \
  --query-a examples/order-by/query-a.sql \
  --query-b examples/order-by/query-b.sql \
  --out ordered-witness
.venv/bin/query-witness replay ordered-witness
```

It finds rows `(-1)` and `(0)` at candidate 9, with sequences `[-1, 0]` versus
`[0, -1]`. The earlier table containing NULL and -1 is **not** a witness: DuckDB
1.5.5 puts NULL last for both default ASC and DESC, so both return `[-1, NULL]`.

The whole-table aggregate example compares `COUNT(*)` with `COUNT(x)`:

```sh
.venv/bin/query-witness check \
  --schema examples/nullable/schema.sql \
  --query-a examples/aggregates/query-a.sql \
  --query-b examples/aggregates/query-b.sql \
  --out aggregate-witness
.venv/bin/query-witness replay aggregate-witness
```

The empty table gives 0 for both queries. One NULL row, at candidate 2, gives 1
for `COUNT(*)` and 0 for `COUNT(x)`. On a NOT NULL schema this pair produces no
finding across the 22 legal tables in the default schedule. `COUNT(x)` versus
`COUNT(DISTINCT x)` first differs on two duplicate non-null rows; two NULL rows
give 0 for both. On empty input, `SUM`, `MIN`, and `MAX` return NULL.

The grouped example compares the two counts within each x group:

```sh
.venv/bin/query-witness check \
  --schema examples/nullable/schema.sql \
  --query-a examples/group-by/query-a.sql \
  --query-b examples/group-by/query-b.sql \
  --out grouped-witness
.venv/bin/query-witness replay grouped-witness
```

One NULL row, at candidate 2, produces `(NULL, 1)` versus `(NULL, 0)`.
On the NOT NULL schema the pair agrees across 22 legal tables. Grouped queries
return one result row per group, compared as unordered bags. Empty input has no
groups and returns an empty bag; whole-table aggregates instead return one row
(for example, `COUNT(*)` returns `(0)`).

HAVING filters groups after aggregation, whereas WHERE filters input rows before
groups are formed. For example:

```sh
.venv/bin/query-witness check \
  --schema examples/nullable/schema.sql \
  --query-a examples/having/query-a.sql \
  --query-b examples/having/query-b.sql \
  --out having-witness
.venv/bin/query-witness replay having-witness
```

The second query adds `HAVING COUNT(*) > 1`. One NULL row at candidate 2 produces
`(NULL, 1)` in the first query and an empty bag in the second. Two duplicate -1
rows pass the filter and both queries return `(-1, 2)`.
`HAVING COUNT(*) >= 1` agrees with no HAVING. Its literal 1 adds neighbor 2 to the
domain, so the default unchanged schedule checks 36 tables rather than 29.

The inner self-join example compares a table scan with a join back to the same table:

```sh
.venv/bin/query-witness check \
  --schema examples/nullable/schema.sql \
  --query-a examples/nullable/query-b.sql \
  --query-b examples/self-join/query.sql \
  --out self-join-witness
.venv/bin/query-witness replay self-join-witness
```

The join is `SELECT a.x FROM t AS a JOIN t AS b ON a.x = b.x`. One NULL row at
candidate 2 is a witness: the scan returns `(NULL)` and the join returns no rows,
because NULL equality in ON is not true. One non-null row matches itself. Two
duplicate `1` rows produce two scan rows and four join rows; search still reports
the earlier NULL witness.

## Exact current subset

- One `CREATE TABLE` with one or two `INTEGER`/`INT` columns with distinct names.
  Either column may have `NOT NULL`; at most one may have a column-level `PRIMARY KEY`.
- Simple unquoted ASCII identifiers; no schema qualification. Non-join queries
  use unqualified columns and no FROM alias. Self-join aliases are described below.
- Without GROUP BY, `SELECT` with 1–16 projections, either all declared-column references or all
  whole-table aggregates, optionally with output aliases. Supported aggregates:
  `COUNT(*)`, `COUNT(column)`, `COUNT(DISTINCT column)`, `SUM(column)`, `MIN(column)`,
  and `MAX(column)`. WHERE is allowed and filters rows before aggregation.
  Mixing columns with aggregates, `AVG`, `COUNT()`, `COUNT(1)`, aggregate
  arithmetic and `FILTER` are unsupported. HAVING without GROUP BY is unsupported.
- Optional `GROUP BY` one or two unique declared columns. Projected columns must
  be group keys and may mix with the supported aggregates; keys need not appear
  in the projection. Examples include `SELECT x, SUM(y) FROM t GROUP BY x` and
  `SELECT x, y, COUNT(*) FROM t GROUP BY x, y`. Ordinals, expressions, duplicate
  or unknown keys, grouping sets, ROLLUP, and CUBE are unsupported.
  Query-level DISTINCT and ORDER BY are rejected on grouped queries.
- Optional HAVING on grouped queries uses the same Boolean operators and
  comparisons as WHERE, with the supported aggregates also allowed as operands.
  Bare column operands must be group keys; aggregate arguments may reference any
  declared column. `IS NULL` / `IS NOT NULL` apply to group-key columns and the
  supported aggregates. WHERE and JOIN ON null checks remain column-only.
  Literals are harvested from accepted HAVING ASTs just as for WHERE.
  AVG, FILTER, COUNT(1), arithmetic, and subqueries remain unsupported.

For example, `HAVING SUM(y) IS NULL` keeps a group whose y values are all NULL.
`HAVING SUM(y) = NULL` is still accepted, but it is a different test: the equality
is unknown and keeps no groups. On one NULL row, `SELECT x, SUM(x) FROM t GROUP BY x`
with those respective HAVING conditions returns `(NULL, NULL)` versus an empty
bag, at candidate 2. COUNT returns a non-null integer even for an all-NULL group,
so `HAVING COUNT(*) IS NULL` keeps none and `IS NOT NULL` keeps the groups.
- Optional plain `SELECT DISTINCT`, including with the supported WHERE predicates.
  `DISTINCT ON` and `DISTINCT *` are unsupported. Query-level DISTINCT and ORDER BY
  are rejected on aggregate queries; `COUNT(DISTINCT column)` is the exception
  inside COUNT, with exactly one declared column.
- Optional `ORDER BY` one declared column, with `ASC` (default) or `DESC`.
  Multiple sort keys, ordinal `ORDER BY 1`, sort expressions, `NULLS FIRST`, and
  `LIMIT` are unsupported. `NULLS LAST` matches the accepted default in the AST.
- A required `FROM` of the declared table and optional `WHERE` using the grammar below.
- One statement per file, at most 16384 characters each. Source SQL is executed
  unchanged. SQLGlot parses in the DuckDB dialect and gates AST structure.
  The gate also tokenizes the source and SQLGlot's DuckDB rendering, ignoring
  COMMENT, SEMICOLON, and BREAK. For each other token type, the source count must
  not exceed the rendered count. This rejects dropped source tokens such as
  unary PLUS in `SELECT +x` or `WHERE x = +1`, and dropped ISNULL/NOTNULL/IS UNKNOWN
  spellings. Counts allow retained unary minus, reordered IS NOT NULL, INTEGER/INT,
  and inserted AS; exact token sequence equality is not required.
  The rendering is only a gate check: execution, witness text, and reproduce.sql
  still use the original source. This check does not establish execution safety.
  Before execution, the owned DuckDB connection also parses the schema and both
  queries. Native syntax rejection, including reserved-name disagreements with
  SQLGlot, reports unsupported input without executing any of the source.

Exactly one inner self-join is supported (`JOIN` or `INNER JOIN`). Both targets
must be the declared table and each must have a distinct, simple alias. Projections,
ON, and WHERE must qualify every column with one of those aliases. ON is required
and uses the existing WHERE predicate grammar, including null checks and Boolean
operators. For two columns, `ON a.x = b.y` and `SELECT a.x, b.y` are supported.
Literals in ON and WHERE are harvested from their accepted ASTs.

Join queries cannot use DISTINCT, ORDER BY, GROUP BY, HAVING, or aggregates.
A second table, a second join, LEFT/RIGHT/FULL/CROSS joins, NATURAL, and USING are
unsupported. Non-join `FROM t AS a` and `SELECT t.x FROM t` remain unsupported.

There are three supported one-column schema behaviors:

| Schema | NULL allowed | Duplicate values allowed |
| --- | --- | --- |
| `CREATE TABLE t (x INTEGER)` | Yes | Yes |
| `CREATE TABLE t (x INTEGER NOT NULL)` | No | Yes |
| `CREATE TABLE t (x INTEGER PRIMARY KEY)` | No | No |

`PRIMARY KEY` implies `NOT NULL`; spelling both as `NOT NULL PRIMARY KEY` is also
accepted. Constraints must be unnamed and column-level. Explicit `NULL`, `UNIQUE`,
`DEFAULT`, `CHECK`, `REFERENCES` / foreign keys, named constraints, and table-level
`PRIMARY KEY (x)` are unsupported. The empty table is valid for every supported schema.
On two-column tables, nullability is per-column: each column allows NULL unless
it declares `NOT NULL` or is the primary key. Both columns may declare `NOT NULL`.
The one permitted primary key may be on either column; it implies NOT NULL and
unique values in that column, not merely unique whole rows. The other column may
still contain duplicates. `DISTINCT key` versus `key` cannot differ on valid key
values; projecting only the other column can still produce duplicate results.
Composite `PRIMARY KEY (x, y)`, primary keys on both columns, three or more columns,
and duplicate column names (including case variants) are unsupported.

WHERE allows `column IS NULL` / `column IS NOT NULL`; comparisons `=`, `<>`, `!=`,
`==`, `<`, `<=`, `>`, `>=`; and `AND`, `OR`, `NOT`, with parentheses for grouping.
Each comparison operand must be a declared column, a signed 32-bit integer
literal, or NULL, and at least one operand must be a column. `WHERE x = y` is
supported when both columns are declared. Literal-vs-literal
comparisons such as `1 = 1` are unsupported. Integer literals must be in
[-2147483648, 2147483647]. Negative literals are accepted; arithmetic is not.

Everything else is explicitly unsupported, including boolean literals (`TRUE` /
`FALSE`), strings, floats, DECIMAL, `IN`, `BETWEEN`, `IS DISTINCT FROM`, `CASE`,
qualified columns outside self-joins, `*` outside `COUNT(*)`, `DISTINCT ON`,
other integer widths in schemas, other joins, other aggregates and functions, casts, CTEs,
subqueries, and other ordering forms. Those are outside this release.
Integer-width normalization already exists in the result
comparator, independently of the schema gate.

## Search and comparison contract

Search starts with the empty table, then singleton tables over an interesting-value
domain. The domain begins with NULL, -1, 0, and 1, then adds each integer literal
harvested from the **accepted ASTs of both queries**, plus literal−1 and literal+1
when they fit signed 32-bit INTEGER. Literals are processed in ascending order,
adding the literal before its neighbors and removing duplicate domain values.
Source-text regexes are not used for harvesting: numbers in comments or aliases
cannot inject domain values.

One-column generation uses this domain unchanged for unconstrained schemas and drops NULL
from the used domain for `NOT NULL` or `PRIMARY KEY`. For each larger row count,
candidates repeat each domain value or cycle through the domain from each starting
position. With a primary key, uniform tables of size greater than one and cycles
that repeat a value are skipped. Sizes with no legal table are skipped too.
Illegal entries are never yielded or executed and do not increment the checked
candidate count. There are at most twice the used domain size in candidates per
row count, not a Cartesian product or an enumeration of all unique key tuples.

Two-column generation shares the same interesting-value domain across columns.
After the empty table, it tries every legal one-row table `(v, w)`, with v as the
outer loop and w as the inner loop in domain order. Without constraints this
yields `|domain|²` singleton tables. The prefix is empty, `[(NULL, NULL)]`,
`[(NULL, -1)]`, `[(NULL, 0)]`, `[(NULL, 1)]`, `[(-1, NULL)]`, and so on.
These singleton pairs come before all larger tables. If they exceed the candidate
budget, the run can stop before finishing the singleton pairs or reaching larger
tables; the default budget remains 64, including the empty table.

For size two and larger, the schedule remains an equal-column row `(value, value)`
repeated to that size, then a mixed table cycling adjacent domain values from each
offset. This yields at most `2 * |domain|` legal tables per larger size, not a full
product across rows.
Each constructed table is skipped if any cell violates its column's nullability
or if values repeat in the primary-key column. Skipped tables do not increment
checked. The one-column schedule is unchanged.

Before insertion, data is validated for column count, signed 32-bit values, row
budget, nullability, and primary-key uniqueness, then executed against the declared
schema. Replay performs the same validation using the exported schema. Row deletion
preserves these constraints without repair or a solver; every reduction trial is
still validated and checked for the discrepancy.

Defaults: `--max-rows 4`, `--max-candidates 64`, `--timeout-seconds 5`,
`--memory-mb 64`. A candidate is one database checked against both queries.
Reduction uses additional comparisons under the same elapsed-time and memory
budgets. Row-deletion reduction rechecks validity and discrepancy after each
deletion. It does not claim global minimality. A reduction failure or timeout
aborts the run with the appropriate failure outcome, without exporting a finding.
The deadline is checked after final Python result comparison in search, reduction,
and replay, before reporting a completed outcome. Export may finish after the
deadline once search and reduction have completed within budget.

Columns compare by position, ignoring aliases. Unequal column counts constitute
a difference, including on empty results. **Order is compared only when both
queries have ORDER BY**: rows compare as sequences, preserving positions and
duplicates. If zero or one query has ORDER BY, rows compare as unordered bags
preserving duplicate counts; scan order alone cannot produce a finding.
DuckDB scan order is not a SQL ordering contract. When exactly one query has
ORDER BY, every check/replay and witness.txt explicitly says:
`Order was not compared; bags only (exactly one query has ORDER BY).`
Corresponding SQL NULLs compare equal. Integer widths normalize to integer values;
exact DuckDB type names are recorded but not compared for identity. This policy
prints after arguments successfully select a check or replay command. Help and
version print only their argparse output; argument errors have no policy paragraph.
Ordered findings print numbered result sequences;
unordered findings print bags with counts.

| Exit code | Outcome | Meaning |
| --- | --- | --- |
| 0 | counterexample found | A difference was found and exported, or verified by replay. |
| 1 | no counterexample within budget | Candidate budget or bounded schedule exhausted. |
| 2 | unsupported input | SQL is outside this slice, or cannot be parsed. |
| 3 | execution failure | Engine, configuration, file, export, or replay validation failure. |
| 4 | resource limit reached | Elapsed time or memory limit interrupted the work. |

Outcomes are mutually exclusive. **None proves equivalence.** A counterexample
demonstrates a difference under the recorded DuckDB conditions only.

Execution failure (3) covers user arguments, configuration, files, engine errors,
and invalid or mismatched witness input. Missing-file diagnostics include the path.
DuckDB parser rejection of the three input SQL statements is unsupported input (2).
Errors in generated engine SQL and other execution errors retain failure status (3).
The loader explicitly validates required witness fields; missing or malformed
fields report ValueError failures, including JSON decoder nesting exhaustion.
Unexpected TypeError, KeyError, RuntimeError, and RecursionError
exceptions propagate as internal crashes instead of being relabeled execution
failure. Such crashes are bugs, not check outcomes or findings.
KeyboardInterrupt, including cancellation wrapped by DuckDB, propagates as ordinary
Ctrl+C termination (shell status 130), rather than the negative-search status 1.
Signal delivery remains cooperative. Failure to create the watchdog thread because
thread resources are exhausted reports the resource-limit outcome and closes the connection.

## Export and replay

A new output directory contains `witness.txt` (readable finding), `reproduce.sql`
(schema, exact inserts, settings, and queries for a fresh DuckDB database), and
`witness.json` (authoritative replay input, observed rows/types, policy ID, versions,
and configuration). Existing output paths are never overwritten. Export writes
all three files into a sibling `<output-name>.exporting` directory, then renames
it to the requested output path only after the writes succeed. On a write failure,
it attempts to remove its staging directory so the export can be retried.
A crash or failed cleanup can leave staging behind; a later attempt reports that
path clearly and leaves it untouched. Inspect and remove that stale staging
directory before retrying. The destination is never removed during cleanup.

Replay requires matching recorded tool, DuckDB, and SQLGlot versions. It validates
and loads the exported rows, executes both original queries, and verifies both
recorded results and their discrepancy. New exports use policy identifier
`integer-position-v2` and record `comparison_order` as `bags` or `sequences`.
Replay uses that recorded mode for verification and result display; it does not
re-infer the mode from SQL. Missing or invalid modes on v2 witnesses are failures.
Sequence mode requires ORDER BY in both queries; contradictory artifacts are rejected
before execution. Explicit bag mode remains supported even when both queries are ordered.
For legacy `integer-bags-v1` witnesses without `comparison_order`, replay retains
the old inference: both queries ORDER BY means sequences, otherwise bags.
Replay does not need original input paths,
generation, a seed, or the original machine. It uses the recorded limits with a
fresh elapsed-time budget. The SQL file retains original source and labels the
queries with `-- Query A` and `-- Query B`. DuckDB CLI prints each statement's
result. Python's `connection.execute(whole_file).fetchall()` returns only the last
statement's result; it does not verify both queries. Use `query-witness replay`
for the programmatic check of both results and its timeout, or extract statements,
execute setup first, and execute A and B separately. Reproduction tests verify
both recorded bags or sequences this way, including ASC versus DESC.

## Trust and limits

**Local, trusted SQL and witness files only.** SQL executes in-process in an
ephemeral DuckDB database. A watchdog interrupts DuckDB at the elapsed deadline;
Python work checks the deadline between operations. This is a cooperative timeout,
not a hard process-kill deadline. Parsing is bounded by input size. File I/O and
export are not forcibly interrupted by the watchdog.
`query-witness check --help` describes this cooperative timeout and the DuckDB-only memory budget.

`--memory-mb` sets DuckDB's memory budget; it does **not** cap total Python/native
process memory. DuckDB documents that its [memory limit applies to the buffer
manager](https://duckdb.org/docs/current/configuration/pragmas#memory-limit).
Threads are limited to one, external access is disabled, and disk spill is disabled.
These settings and the subset gate are not a sandbox. Process isolation is deferred
as agreed; do not expose this CLI as a service accepting untrusted SQL.

The tests cover both null witnesses, integer comparisons, Boolean grouping,
literal harvesting and boundaries, singleton-pair coverage, bounded larger-table generation,
agreeing pairs, actual integer-width/bag behavior,
invalid data, subset rejection, reduction, export/replay without generation,
changed artifacts, engine failure, and real DuckDB timeout/memory-limit errors.
Constraint tests cover generated tables against both validation and DuckDB,
constrained reduction/replay, and invalid NULL/duplicate rejection before execution.
DISTINCT tests cover duplicate NULL/non-null witnesses, row reduction, replay
without generation, agreeing aliases, and primary-key no-find cases. Other schema
constraints remain unsupported.
Two-column tests cover positional comparison, column equality, DISTINCT,
singleton pairs and linear larger-table generation, invalid widths/cells, schema rejection, and exported replay.
They also cover per-column NOT NULL, keys in either position, skipped-table
counts, and constraint validation before execution and replay.
ORDER BY tests cover DuckDB 1.5.5 NULL placement, sequence-only differences,
one-sided ordering with bag comparison, and sequence validation during replay.
Aggregate tests cover empty/NULL input, COUNT DISTINCT duplicates, NOT NULL
agreement, aliases, WHERE, wide integer SUM results, and rejection of mixed or
unsupported projections. Aggregate support does not change the generator.
GROUP BY tests cover the NULL-count witness, NOT NULL agreement, grouping versus
DISTINCT, empty grouped versus scalar results, two-column keys, and rejected
group expressions and clauses. Grouping does not change generation or bag comparison.
HAVING tests cover group filtering, shared predicate/aggregate validation, AST
literal harvesting, replay without generation, and rejection of non-group keys.
Self-join tests cover NULL equality, duplicate multiplication, two-column joins,
qualified ON/WHERE scope, unsupported join forms, and replay without generation.

## Prior art

SQL equivalence checking and counterexample generation are established areas,
including Cosette and SpotIt+. Query Witness does not claim to invent them. This
project explores a focused developer workflow for readable, executable findings.

## Verify a release

Follow [RELEASE.md](RELEASE.md#rebuild-and-verify) for the pinned build tools,
exact archive checks, Python matrix, and later publishing steps. The verifier
installs the wheel outside the checkout and runs tests and examples from the
matching source archive. Rebuild and verify whenever release inputs change.
