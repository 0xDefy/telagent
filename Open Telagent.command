#!/bin/sh
# macOS: double-click in Finder after the one-time setup in README.md.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$project_dir/.venv/bin/python" "$project_dir/gui.py"
