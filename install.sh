#!/bin/sh
set -eu
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Instale Python 3.11 ou superior antes de continuar.' >&2
    exit 78
fi
EDGE_INSTALL_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$EDGE_INSTALL_SOURCE/install.py" "$@"
