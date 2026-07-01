"""Umbrella entry point: `ijon-reloaded <command> [args...]`.

Dispatches to each CLI module's `main()`. Every subcommand keeps its own argparse
interface unchanged — this only routes to it and fixes up the program name, so
`ijon-reloaded run --workspace ...` behaves exactly like the old
`python scripts/run_target.py --workspace ...`.
"""
from __future__ import annotations

import sys
from importlib import import_module

# subcommand -> module under this package (each exposes main())
COMMANDS = {
    "bringup":      "ijon_reloaded.bringup",
    "run":          "ijon_reloaded.run_target",
    "campaign":     "ijon_reloaded.campaign_supervisor",
    "cc":           "ijon_reloaded.campaign_cli",
    "build-doctor": "ijon_reloaded.build_doctor",
    "analyst":      "ijon_reloaded.analyst_cli",
    "triage":       "ijon_reloaded.triage_crashes",
}

_SUMMARY = {
    "bringup":      "draft build.sh + target.json + harness discovery for a new library",
    "run":          "one localize -> annotate -> rebuild -> keep/revert loop (Mode 2)",
    "campaign":     "adaptive long crash-hunting campaign, autonomous daemon (Mode 2)",
    "cc":           "campaign mechanics for Claude Code to drive (Mode 1: start-round/poll/stop-round)",
    "build-doctor": "fix build.sh from compiler/linker errors in a bounded loop",
    "analyst":      "single CC-as-analyst turn (localize/annotate/eval) on a workspace",
    "triage":       "bucket a campaign's crashes into distinct bugs",
}


def _usage() -> str:
    width = max(len(c) for c in COMMANDS)
    lines = [f"  {c.ljust(width)}  {_SUMMARY[c]}" for c in COMMANDS]
    return ("usage: ijon-reloaded <command> [args...]\n\n"
            "commands:\n" + "\n".join(lines) +
            "\n\nRun `ijon-reloaded <command> --help` for a command's options.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    cmd, rest = argv[0], argv[1:]
    mod_name = COMMANDS.get(cmd)
    if mod_name is None:
        print(f"ijon-reloaded: unknown command {cmd!r}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    # Hand the subcommand a clean argv so its own argparse (prog + args) is unchanged.
    sys.argv = [f"ijon-reloaded {cmd}", *rest]
    return import_module(mod_name).main() or 0


if __name__ == "__main__":
    raise SystemExit(main())
