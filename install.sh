#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -eq 0 ]; then
    exec "$project_dir/packaging/install-root.sh" "$project_dir"
fi

if ! command -v pkexec >/dev/null 2>&1; then
    echo "Graphical administrator authorization (pkexec) is not installed." >&2
    exit 1
fi

exec pkexec "$project_dir/packaging/install-root.sh" "$project_dir"

