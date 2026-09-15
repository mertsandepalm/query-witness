# Engineering

This file is the contract for humans and coding agents. Follow it on every change.

## Product

A small CLI that searches for a valid DuckDB database where two queries disagree, then exports a replayable witness.

Finding a difference is not a proof of equivalence. Invalid data is not a finding. Unsupported SQL, engine errors, and resource limits must never look like success.

## Simple means fewer mechanisms, not fewer characters

Keep production code small by omitting features, files, helpers, flags, and dependencies.

Do not keep it small by packing. A boolean that needs a comment is not simpler than three clear lines. Nested tricks, one-letter names, and compressed expressions are not engineering.

Prefer deleting code over adding it. If a helper, class, or module has one call site, inline it.

## Shape

Keep these four modules unless a job truly does not fit them:

- `cli.py` — argv, outcomes, printing
- `subset.py` — fail-closed SQL gate; execute source verbatim; never translate
- `core.py` — generate, execute, compare, reduce
- `artifact.py` — export and replay from files, not from a seed

Do not add a fifth module, a package tree, a plugin system, or a config framework.

Dependencies stay DuckDB and SQLGlot. No Hypothesis, ORM, CLI framework, or extra package until a slice proves it removes code.

## Do not build

Anything outside the current slice. No “while we’re here.”

No website, AI, dialect translation, process isolation, equivalence prover, sandbox, logging framework, or retry layer.

No claims of global minimality, cross-engine behavior, or production-ready scale without evidence.

## Tests

Tests may be longer than the code. That is expected.

They must catch illegal data reported as a finding, and failures reported as success. Hit real DuckDB for behavior. Parametrize cases in `tests/`. Do not add a test package, fixture framework, or mocks of the engine for core behavior.

## Comments

Only for non-obvious constraints. No narration of what the next line does.

## Before finishing a change

1. Can a file, function, flag, or dependency be deleted instead?
2. Did SQL surface grow without a reject test?
3. Could this look like a finding on illegal data, or like success on a failure?
4. Is the new code obvious on one reading?

Do not rewrite working code only to satisfy this file. Apply it to new work, and to edits you are already making.

## Releasing

Ship from tags, not from `main`. Follow [RELEASING.md](RELEASING.md).

Replay may accept a different Query Witness version when `format_version` and
`comparison_policy` are known. DuckDB and SQLGlot versions in the witness must
still match the install. Changing those pins, the format, or the policy is what
breaks old `replay` folders — not a patch bump of this CLI.

Do not add an npm/pnpm/bun package. Users install with pip, pipx, or uv.
