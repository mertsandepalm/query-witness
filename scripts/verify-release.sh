#!/usr/bin/env bash
set -euo pipefail

wheel=$(realpath "${1:?Usage: bash scripts/verify-release.sh path/to/package.whl [python-executable]}")
release_python=${2:-python}
sdist=${wheel%-py3-none-any.whl}.tar.gz
test -f "$sdist"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

unset PYTHONPATH PYTHONHOME
"$release_python" -m venv "$work/venv"
"$work/venv/bin/python" -m pip install "$wheel[test]"
mkdir "$work/source"
tar -xzf "$sdist" -C "$work/source" --strip-components=1
cp -R "$work/source/tests" "$work/source/examples" "$work/"
cp "$work/source/release-validation.json" "$work/"
cd "$work"
"$work/venv/bin/python" - <<'PY'
from pathlib import Path
import importlib.metadata
import platform
import sys
import query_witness

assert Path(query_witness.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert importlib.metadata.version("query-witness") == query_witness.__version__
print("Installed package:", query_witness.__file__)
print("Python:", platform.python_version())
PY
"$work/venv/bin/python" -m pytest -q tests
cli="$work/venv/bin/query-witness"
"$cli" --help
"$cli" check --help
"$cli" mutate --help
"$cli" replay --help
version=$("$work/venv/bin/python" -c 'from query_witness import __version__; print(__version__)')
test "$("$cli" --version)" = "$version"
"$cli" check --schema examples/rewrite-mistakes/assigned-count/schema.sql \
  --query-a examples/rewrite-mistakes/assigned-count/query-a.sql \
  --query-b examples/rewrite-mistakes/assigned-count/query-b.sql --out witness
"$cli" replay witness
