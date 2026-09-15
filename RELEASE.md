# Query Witness 0.1.0 release preparation

Current shipping process: [RELEASING.md](RELEASING.md). This file is the
recorded 0.1.0 validation. Keep it; do not treat it as the live runbook.

This file, [release-validation.json](release-validation.json), the
[five constructed scenarios](examples/rewrite-mistakes/README.md), and their tests
are the durable validation record. They belong in the repository and source
distribution. Build outputs live in `dist/`; they can be recreated from these
sources. Temporary test environments are disposable.

## Release contract

The release target is CPython 3.11–3.14 on Linux x86_64. `Requires-Python` is
`>=3.11,<3.15`; future Python minors and other platforms are unverified.
DuckDB 1.5.5 and SQLGlot 30.18.0 are the only runtime dependencies. The supported
SQL scope and cooperative resource limits are prominent in the README.

The existing [LICENSE](LICENSE) is MIT, copyright 2026 Query Witness contributors.
Both archives must carry that file, and package metadata must declare MIT.
There is no outstanding license selection. Project URLs and individual author
contact details are omitted because they are not established in this workspace.
Do not invent a repository URL for package metadata.

## Validation status

Local validation completed on 2026-09-11. No external publication has been performed.

| Interpreter | Installed-package suite | Platform |
| --- | ---: | --- |
| CPython 3.11.16 | 570 passed | Linux x86_64 |
| CPython 3.12.14 | 570 passed | Linux x86_64 |
| CPython 3.13.15 | 570 passed | Linux x86_64 |
| CPython 3.14.7 | 570 passed | Linux x86_64 |

All five preserved witness payloads also replayed on each interpreter. Both
archives passed `twine check --strict`; their metadata, license, UTF-8 README,
runtime pins, entry point, and included files were checked. The README's first
command block was executed verbatim in a clean wheel-only directory and produced
the stated one-row finding and successful replay.

Two builds of the same sources produced byte-identical wheel and source archives.
Rebuilding both archives from the extracted source distribution also matched.
This establishes reproducibility with the recorded Linux/CPython 3.11 build
toolchain, not across untested build environments. Final checksums are in
`dist/SHA256SUMS`, outside the archives to avoid self-referential checksums.
The built packages contain the same application code and tests used in the matrix;
the completed records are included in the final source archive, and the final
archives are checked again on all four interpreters. The external receipt
`dist/final-validation.json` identifies their hashes and verification logs.

This candidate includes all four correctness-review fixes: sequence mode requires
both queries to be ordered; DuckDB-wrapped cancellation preserves Ctrl+C status;
JSON nesting exhaustion is invalid input; and failed watchdog startup closes its
connection. The original candidate and review/reproduction evidence are preserved
separately in the local review workspace. The source revision is identified in
`release-validation.json`; the unpublished package version remains 0.1.0 so valid
existing artifacts retain their version compatibility. Conservative bag comparison
for ordered queries, SQL scope, runtime pins, and module structure are unchanged.

Both review follow-ups are also corrected. The owned DuckDB connection parses all
three input SQL statements before any execution; native syntax rejections such as
reserved identifiers return unsupported input, while generated SQL and other
engine failures retain failure status. Search, reduction, and replay check the
deadline after final Python comparison. Completed export remains outside that
budget. Thirty additional regressions cover these boundaries, including ordinary
completion controls and cleanup; real-watchdog probes independently confirm the
resource-limit outcomes. The original R1–R4 candidate remains preserved separately.

There are no known local release blockers. Actual Git history/remotes and PyPI
account/name ownership remain unverified; those are publishing prerequisites.

The five supported scenarios found schema-valid discrepancies within the
default 64-candidate budget, exported both observed results, and replayed after
the original input directory was deleted. Independent DuckDB execution agreed
with the hand-written expectations and with both exported result bags.
The integer-filter control reported no counterexample within budget, which
is not an equivalence proof. The LEFT JOIN case returned unsupported input.

## Rebuild and verify

Run from the project root or an extracted source distribution. These instructions
use Bash and require the named CPython interpreters on PATH. Package installation
needs network access or a previously populated package cache; no API key is
needed for local builds or checks.

```sh
python3.11 -m venv .release-venv
.release-venv/bin/python -m pip install -r scripts/release-requirements.txt
export SOURCE_DATE_EPOCH=1789084800
.release-venv/bin/python -m build --no-isolation --outdir dist
.release-venv/bin/python -m twine check --strict \
  dist/query_witness-0.1.0-py3-none-any.whl dist/query_witness-0.1.0.tar.gz

for release_python in python3.11 python3.12 python3.13 python3.14; do
  bash scripts/verify-release.sh dist/query_witness-0.1.0-py3-none-any.whl "$release_python" || exit
done
```

`build` creates the source distribution first and builds the wheel from it.
The backend and its dependencies are pinned in `scripts/release-requirements.txt`;
`--no-isolation` uses those exact installed build inputs. The fixed epoch is
2026-09-11 00:00:00 UTC. Hatchling uses it for reproducible archive timestamps.
The verifier creates a fresh environment outside the repository, installs the
wheel, checks that imports resolve inside that environment, and runs the tests
and examples extracted from the adjacent source archive. It requires both
distribution files and does not use a developer's editable installation.

To check repeatability and regenerate checksums:

```sh
export SOURCE_DATE_EPOCH=1789084800
.release-venv/bin/python -m build --no-isolation --outdir dist/rebuild
cmp dist/query_witness-0.1.0-py3-none-any.whl dist/rebuild/query_witness-0.1.0-py3-none-any.whl
cmp dist/query_witness-0.1.0.tar.gz dist/rebuild/query_witness-0.1.0.tar.gz
(cd dist && sha256sum query_witness-0.1.0-py3-none-any.whl query_witness-0.1.0.tar.gz) > dist/SHA256SUMS
```

Source changes intentionally change the checksums. Test environments, caches,
distribution archives, credentials, and personal working files are excluded by
the explicit source-distribution file selection. The source archive includes
all six package files, the tests, examples, release requirements, verification
script, license, README, release notes, and these validation records. The wheel
includes only the package code and distribution metadata, including its license
and README description. The README's first example creates its own SQL files,
so wheel-only users can run it without downloading the source archive.

Build documentation: [PyPA packaging guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/),
[Hatch reproducible builds](https://hatch.pypa.io/1.13/config/build/#reproducible-builds),
and [Twine checks](https://twine.readthedocs.io/en/stable/#twine-check).

## Publishing later

Preparation is local. Git history/remotes are not available in this workspace,
and registry ownership and upload credentials have not been verified. A public
repository is optional for PyPI distribution; choose/configure it separately if
you want hosted source and a release page. If adding project URLs or changing
the version, rebuild and verify those final sources before uploading.

For PyPI, use this sequence only when you decide to publish:

1. Confirm that your PyPI account can publish `query-witness` and that version
   `0.1.0` is unused. A missing public project page alone does not guarantee that
   a name can be registered. If renaming is necessary, update the project name
   and release commands and repeat the package checks.
2. Run the rebuild and verification commands above. Confirm that
   `query_witness/__init__.py` and the built package report `0.1.0`.
3. In the actual Git checkout, commit the reviewed sources and create the local
   tag `git tag -a v0.1.0 -m "Query Witness 0.1.0"`. If that tag already exists,
   verify its commit instead of replacing it. Push the reviewed commit and tag
   only when you are ready to publish source.
4. Upload exactly the two tested archives. Twine prompts for the PyPI API token;
   do not put it in source files or shell command arguments:

   ```sh
   .release-venv/bin/python -m twine upload --repository pypi \
     dist/query_witness-0.1.0-py3-none-any.whl dist/query_witness-0.1.0.tar.gz
   ```

5. Verify the registry installation in a new environment:

   ```sh
   python3.11 -m venv dist/published-check
   dist/published-check/bin/python -m pip install --no-cache-dir \
     --index-url https://pypi.org/simple query-witness==0.1.0
   dist/published-check/bin/query-witness --version
   dist/published-check/bin/query-witness check \
     --schema examples/rewrite-mistakes/assigned-count/schema.sql \
     --query-a examples/rewrite-mistakes/assigned-count/query-a.sql \
     --query-b examples/rewrite-mistakes/assigned-count/query-b.sql \
     --out dist/published-ticket-witness
   dist/published-check/bin/query-witness replay dist/published-ticket-witness
   ```

The upload and registry verification follow the
[PyPA publishing procedure](https://packaging.python.org/en/latest/tutorials/packaging-projects/#uploading-the-distribution-archives).
Uploading to TestPyPI is also external publication and is not part of this
preparation run. No publishing command is included in the verification script.
