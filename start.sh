#!/usr/bin/env sh
# One-command start for macOS / Linux: creates the virtual environment on first run.
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
exec .venv/bin/python -m server.main "$@"
