#!/bin/sh
# Linux: run this file (or choose Run in your file manager).
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$project_dir/.venv/bin/python" "$project_dir/gui.py"
