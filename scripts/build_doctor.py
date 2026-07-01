#!/usr/bin/env python3
"""Shim: the real module now lives in the installed `ijon_reloaded` package.
Kept so `python scripts/build_doctor.py ...` (Mode 1 / clone users) still works verbatim.
Prefer `ijon-reloaded <command>` when installed."""
from ijon_reloaded.build_doctor import main

if __name__ == "__main__":
    raise SystemExit(main())
