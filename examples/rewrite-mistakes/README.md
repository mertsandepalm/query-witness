# Realistic rewrite mistakes

These are **constructed examples**, not customer incidents or a benchmark sampled
from production queries. They isolate five plausible rewrite mistakes using the
supported integer-only SQL subset. Each directory contains a schema and the
original `query-a.sql` and rewritten `query-b.sql`. `query-witness mutate` on
each `query-a.sql` rediscovers that rewrite as a named operator and searches for
a witness.

The independent examples below establish a discrepancy before running the search.
`tests/test_workflows.py` executes these hand-written tables directly in DuckDB
and asserts the stated results without using Query Witness's generator, validator,
or comparator. It then checks the installed CLI separately.

## The five supported cases

### Counting tickets with an optional assignee

In [assigned-count](assigned-count), a developer substitutes
`COUNT(assignee_id)` for `COUNT(*)` while simplifying a ticket report. An unassigned
ticket still exists, but its assignee is NULL. `COUNT(*)` counts the row;
`COUNT(assignee_id)` counts only non-NULL assignees.

Independent example: `tickets(ticket_id, assignee_id) = [(101, NULL)]`.
Query A returns `(1)` and B returns `(0)`. The ticket ID is a primary key;
the assignee is intentionally nullable. Counting the primary key instead would
preserve the count, but that is not this rewrite.

### Removing DISTINCT from a customer list

In [customer-distinct](customer-distinct), the report should list each positive
customer ID once. A developer removes DISTINCT, assuming that the source already
has one row per customer. The source actually represents purchases, so the same
customer may appear repeatedly.

Independent example: `purchases(customer_id) = [(7), (7)]`.
Query A returns `(7)` once; B returns it twice. NOT NULL does not imply uniqueness.
The comparison must preserve duplicate counts to detect this mistake.

### Changing an inventory boundary

In [stock-boundary](stock-boundary), `quantity >= 1` becomes `quantity > 1`
while rewriting an in-stock filter. The rewrite excludes an item with exactly
one unit remaining.

Independent example: `stock(item_id, quantity) = [(101, 1)]`.
Query A returns item `(101)`; B returns an empty bag. The item ID is unique and
quantity is NOT NULL.

### Moving an aggregate filter before grouping

In [aggregate-pushdown](aggregate-pushdown), the report selects customers whose
total payments exceed 10000 cents. A developer adds `WHERE amount_cents > 10000`
to reduce the work before grouping, while retaining the HAVING condition.
That removes small payments that could together exceed the threshold.

Independent example: `payments(customer_id, amount_cents) = [(7, 6000), (7, 6000)]`.
Query A returns `(7, 12000)`; B returns an empty bag because neither individual
payment passes its WHERE filter. Repeated rows represent distinct payments;
no uniqueness constraint was declared on this projection.

### Joining on a customer ID that is not unique

In [join-fanout](join-fanout), a developer joins a purchase report back to the
purchase relation on customer ID, assuming the join preserves the report's row
count. Every purchase instead matches every purchase from the same customer.
The self-join isolates this common join-key mistake within the supported subset.

Independent example: `purchases(customer_id, product_id) = [(7, 101), (7, 102)]`.
Query A returns the two purchases once each. B returns each purchase twice,
for four rows. Neither customer ID nor the whole row is declared unique.

## Controls and the SQL boundary

The equivalent control compares `stock-boundary/query-a.sql` with
[stock-boundary/equivalent.sql](stock-boundary/equivalent.sql): `quantity >= 1`
versus `quantity > 0`. These predicates select the same integers: there is no
integer strictly between 0 and 1. Direct DuckDB checks include zero, one, and
the signed 32-bit extremes. The search reports **no counterexample within budget**;
that search result is not a proof of equivalence. The reasoning about integers
establishes the equivalence independently.

The unsupported control compares [join-fanout/left-join.sql](join-fanout/left-join.sql)
with [join-fanout/inner-matching.sql](join-fanout/inner-matching.sql). The report
lists purchases of product 1 and looks for a purchase of product 2 by the same
customer. Replacing LEFT JOIN with INNER JOIN drops customers without that match.
On the valid table `[(7, 1)]`, direct DuckDB execution gives `(7, 1)` for the left
join and an empty bag for the inner join. Query Witness rejects LEFT JOIN before
search, with exit 2 and `Unsupported option on Join`; it exports no witness.

This is a useful problem that the current release cannot investigate. Outer joins
introduce unmatched rows and NULL extension and deserve a separate supported-SQL
change with rejection and validity tests. They are outside this narrow release.
Joins between different tables, more than two columns, strings, dates, and other
constraints also remain unsupported. Do not remove constraints from a real schema
and treat a finding on the weakened schema as valid for the original one.

## Try the examples

After installing Query Witness, run from this checkout or an extracted source
distribution. Replace `query-witness` with the absolute path to your environment's
command if it is not on PATH. Output directories must not already exist.

```sh
example_root="$PWD/examples/rewrite-mistakes"
output_root="$(mktemp -d)"
for example in assigned-count customer-distinct stock-boundary aggregate-pushdown join-fanout; do
  query-witness check \
    --schema "$example_root/$example/schema.sql" \
    --query-a "$example_root/$example/query-a.sql" \
    --query-b "$example_root/$example/query-b.sql" \
    --out "$output_root/$example"
  query-witness replay "$output_root/$example"
done
```

Each check and replay should exit 0. The controls use the same schemas:

```sh
query-witness check \
  --schema "$example_root/stock-boundary/schema.sql" \
  --query-a "$example_root/stock-boundary/query-a.sql" \
  --query-b "$example_root/stock-boundary/equivalent.sql" \
  --out "$output_root/equivalent"
# Expected exit 1: no counterexample within budget; no output directory.

query-witness check \
  --schema "$example_root/join-fanout/schema.sql" \
  --query-a "$example_root/join-fanout/left-join.sql" \
  --query-b "$example_root/join-fanout/inner-matching.sql" \
  --out "$output_root/unsupported"
# Expected exit 2: unsupported input; no output directory.
```

## Observed release validation

Validated on Linux x86_64 with CPython 3.11.16, 3.12.14, 3.13.15, and 3.14.7,
Query Witness 0.1.0, DuckDB 1.5.5, and SQLGlot 30.18.0. All **570 tests passed on
each interpreter**, including thirteen workflow tests. Other operating systems
and Python implementations remain unverified. The [release report](../../RELEASE.md)
and [saved validation data](../../release-validation.json) are preserved in the
repository and source archive. The saved data includes all five full witness
payloads, which are replayed by the tests without access to temporary audit files.

With the defaults (4 rows, 64 candidates, 5 seconds, 64 MB), the observed witnesses
were as follows. Parentheses denote rows; repetitions are significant.

| Case | Checked candidates | Exported table | Query A | Query B |
| --- | ---: | --- | --- | --- |
| Optional assignee count | 2 | `(-1, NULL)` | `(1)` | `(0)` |
| Removed DISTINCT | 9 | `(1)` twice | `(1)` once | `(1)` twice |
| Stock boundary | 4 | `(-1, 1)` | `(-1)` | empty |
| Aggregate filter | 44 | `(10000, 10000)` twice | `(10000, 20000)` | empty |
| Nonunique join key | 14 | `(1, 1)` twice | `(1, 1)` twice | `(1, 1)` four times |
| Equivalent integer filter | 20 | none | no counterexample within budget | |
| Unsupported outer join | no search | none | unsupported input | |

The generated IDs of -1 are valid under the supplied schemas: signed INTEGER
does not impose positivity. The examples describe exactly their declared
constraints; they do not claim to encode every business rule of a ticketing or
inventory system. The hand-written examples use positive IDs to illustrate the
same mistakes independently of the generator's chosen values.

The package validation builds the wheel from the source distribution, installs
it with the pinned dependencies in a fresh environment outside the repository,
and runs the copied tests and examples there. For every supported case the workflow
test runs the installed command with relative input paths, moves the exported
directory, deletes the entire original working directory and input files, and
replays from another working directory. Fresh DuckDB connections also insert
every exported row into the exact schema, check both original query results
against the recording, and execute both queries from `reproduce.sql` separately.
The tests cover the prior comment-termination, parser-depth, and enormous-timeout
failures through the installed CLI too.

Run `bash scripts/verify-release.sh /absolute/path/to/query_witness-<version>-py3-none-any.whl python3.11`
to repeat the installed-package suite with the matching `.tar.gz` beside the wheel, or run
`python -m pytest tests/test_workflows.py -q -rP` in an environment with the project
and its test extra installed. The latter prints the witness rows and counts.

These checks establish usefulness for the five illustrated mistakes. They do not
measure production workload coverage or prove that the search will find every
difference in supported SQL. Larger tables use a sparse deterministic schedule,
and singletons can consume the budget before larger tables are tried. A no-find
result always remains bounded and inconclusive.

Recommendation: the demonstrated workflows justify shipping the documented narrow
SQL scope. No SQL expansion is needed for these five cases. The MIT license,
advertised Python range, package metadata, instructions, and reproducible builds
have now been checked. Shipping tagged releases is documented in
[RELEASING.md](../../RELEASING.md). Outer joins remain an explicit limitation.
