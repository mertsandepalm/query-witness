# How to ship Query Witness

Users install a **versioned package**, not whatever is on `main`.
`pip install -U query-witness` only moves them when you cut a tagged release
that published that version to PyPI.

This is a Python CLI. There is no npm/pnpm/bun package. Those installers
cannot run DuckDB/SQLGlot for this tool.

## What a release is

1. Version in `query_witness/__init__.py`
2. Notes in `CHANGELOG.md`
3. Annotated tag `vX.Y.Z` on `main` matching that version
4. GitHub Actions `Release` workflow:
   - builds the wheel and sdist
   - runs `scripts/verify-release.sh` on CPython 3.11–3.14
   - attaches the archives to a GitHub Release
   - uploads the same archives to PyPI

Do not upload from a laptop. Do not retag. Do not change files after the tag.

## Compatibility (so upgrades work)

Replay trusts `witness.json`. It does **not** require the CLI version in that
file to match the installed CLI. It **does** require:

- `format_version` 1
- a known `comparison_policy`
- the recorded **DuckDB** and **SQLGlot** versions

| Change | Version | Old `replay` folders |
| --- | --- | --- |
| Bugfix, docs, extra examples; same DuckDB/SQLGlot | patch (`0.1.1`) | Keep working |
| More SQL in the current format; same engines | minor (`0.2.0`) | Keep working |
| New witness format or comparison policy | major (`1.0.0`) | Need a CLI that understands that policy |
| DuckDB or SQLGlot pin change | minor or major | Replay fails until the user re-runs `check` or keeps the old pin |

Recorded `query_witness` versions stay in the file as provenance.

The portfolio demo vendors a wheel. A CLI release does **not** update
sandepalm.com until that wheel is replaced and Railway is redeployed.

## Cut a release

On a clean `main` that already passed CI:

```sh
# 1. Set query_witness/__init__.py to X.Y.Z
# 2. Add a ## X.Y.Z section to CHANGELOG.md
# 3. Open a PR. Wait for CI.
# 4. Merge, then from the merge commit:

git checkout main
git pull
git tag -a vX.Y.Z -m "Query Witness X.Y.Z"
git push origin vX.Y.Z
```

The tag must equal `__version__` (with a `v` prefix). The workflow fails if they
differ.

## First PyPI upload (one-time)

PyPI trusted publishing has to exist before the `pypi` job can succeed.

1. Create a pypi.org account.
2. Open [pending publishers](https://pypi.org/manage/account/publishing/).
3. Add GitHub:
   - PyPI project name: `query-witness`
   - Owner: `mertsandepalm`
   - Repository: `query-witness`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
4. Push the version tag. The first successful upload creates the project.

Until that publisher exists, GitHub Releases still work:

```sh
python -m pip install \
  https://github.com/mertsandepalm/query-witness/releases/download/vX.Y.Z/query_witness-X.Y.Z-py3-none-any.whl
```

If the GitHub Release succeeded and PyPI failed, fix the publisher and re-run
the failed `pypi` job on that workflow run. Do not mint a new version for a
failed upload of the same artifacts.

## After users have PyPI

```sh
pip install query-witness
pip install -U query-witness
pipx install query-witness
uv tool install query-witness
uv tool upgrade query-witness
```

## 0.1.0 evidence

[RELEASE.md](RELEASE.md) and `release-validation.json` are the recorded 0.1.0
build. Keep them. New versions use this file and the `Release` workflow.
