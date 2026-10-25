#!/usr/bin/env bash
# Publishes a dev build of the garageworks Python package to PyPI.
#
# PyPI (unlike Sonatype's mutable snapshot repo) rejects re-uploading the
# same version twice, and also rejects PEP 440 local version identifiers
# (e.g. "0.2.0+gabc1234") outright: "the Python Package Index ... MUST NOT
# allow the use of local version identifiers." So each dev build gets a
# distinct, PyPI-legal ".devN" version derived from the current commit: the
# short git hash (hex) converted to its decimal integer value. Same commit
# always yields the same dev version, which also means publishing twice from
# an unchanged HEAD will collide -- caught below before it reaches twine.
set -euo pipefail

cd "$(dirname "$0")/../python"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree has uncommitted changes; commit first so the dev" >&2
  echo "version (derived from HEAD) reflects what gets published." >&2
  exit 1
fi

BASE_VERSION=$(grep -oE '^version *= *"[^"]*"' pyproject.toml | sed -E 's/.*"([^"]*)"/\1/')
if [[ "$BASE_VERSION" == *dev* ]]; then
  echo "version in pyproject.toml is '$BASE_VERSION', already a dev version. Aborting." >&2
  exit 1
fi

HASH=$(git rev-parse --short=10 HEAD)
DEVN=$((16#$HASH))
DEV_VERSION="${BASE_VERSION}.dev${DEVN}"

echo "Base version:   $BASE_VERSION"
echo "Commit:         $HASH"
echo "Dev version:    $DEV_VERSION"

trap 'git checkout -- pyproject.toml' EXIT

sed -i -E "s/^version *= *\"[^\"]*\"/version = \"$DEV_VERSION\"/" pyproject.toml

rm -rf dist build garageworks.egg-info

echo "Building $DEV_VERSION ..."
python3 -m build

echo "Uploading to PyPI ..."
twine upload dist/*

echo "Done. Published garageworks==$DEV_VERSION (from commit $HASH) to PyPI."
