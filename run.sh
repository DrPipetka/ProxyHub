#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || {
    echo 'The Python venv module is required. On Ubuntu: sudo apt install python3.11-venv' >&2
    exit 1
  }
fi
if [ ! -f .venv/proxyhub-installed ]; then
  .venv/bin/python -m pip install -r requirements-lock.txt
  touch .venv/proxyhub-installed
fi
exec .venv/bin/python -m proxyhub
