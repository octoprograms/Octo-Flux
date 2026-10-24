#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv "$SCRIPT_DIR/.venv"

    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "Failed to create the virtual environment." >&2
        exit 1
    fi

    # Activate the environment for shell tools while using its interpreter explicitly below.
    source "$SCRIPT_DIR/.venv/bin/activate"
    echo "Installing project dependencies..."
    "$VENV_PYTHON" -m pip install -e ".[dev]"
else
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

# --dev turns on auto-reload (an extra watcher process, ~20 MB); production runs without it.
RELOAD=()
if [[ "${1:-}" == "--dev" ]]; then
    RELOAD=(--reload)
fi

echo "Starting OctoFlux..."
exec "$VENV_PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log ${RELOAD[@]+"${RELOAD[@]}"}
