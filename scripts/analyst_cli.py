#!/usr/bin/env python3
"""Shim: the real module now lives in the installed `ijon_reloaded` package.
Kept so `python scripts/analyst_cli.py ...` (Mode 1 / clone users) still works verbatim.
Prefer `ijon-reloaded <command>` when installed."""
from ijon_reloaded.analyst_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
