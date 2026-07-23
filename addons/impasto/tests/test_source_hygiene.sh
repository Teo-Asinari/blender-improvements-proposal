#!/usr/bin/env bash
# Source/export hygiene checks that do not require Blender.

set -eu

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$REPO"

tracked_cache="$(git ls-files 'addons/impasto/**/__pycache__/**' \
    'addons/impasto/**/*.pyc' 'addons/impasto/**/*.pyo')"
if [ -n "$tracked_cache" ]; then
    echo "Tracked Python cache artifacts:" >&2
    echo "$tracked_cache" >&2
    exit 1
fi

git check-ignore -q addons/impasto/__pycache__/contract.pyc

unexpected_root_docs="$(find addons/impasto -maxdepth 1 -type f -name '*.md' \
    ! -name README.md ! -name PROGRESS.md ! -name ROADMAP.md \
    ! -name CHANGELOG.md -print)"
if [ -n "$unexpected_root_docs" ]; then
    echo "Impasto design/history documents belong under docs/:" >&2
    echo "$unexpected_root_docs" >&2
    exit 1
fi

attrs="$(git check-attr export-ignore -- \
    addons/impasto/__pycache__/contract.pyc)"
case "$attrs" in
    *": export-ignore: set") ;;
    *)
        echo "Missing export-ignore for Python cache artifacts: $attrs" >&2
        exit 1
        ;;
esac

echo "IMPASTO_SOURCE_HYGIENE_PASSED"
