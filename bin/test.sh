#!/usr/bin/env bash
# This script runs the main project test suite locally.
# Usage:
#   $ ./bin/test.sh

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$SCRIPT_DIR/.." || exit 1

exec uv run pytest --numprocesses auto tests/ci $1 $2 $3
