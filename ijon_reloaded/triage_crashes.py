#!/usr/bin/env python3
"""Triage a campaign's crashes into unique bugs + a report.

A long campaign saves many crash INPUTS, but most are the same BUG (different bytes,
same faulting location). This replays each crash through the (ASAN) target, parses
the sanitizer report, and BUCKETS by (crash-type, top stack frames) so you get the
small set of distinct bugs — with a representative input, location, and count each.

    python3 scripts/triage_crashes.py --workspace workspace/<t>          # auto-resolve
    python3 scripts/triage_crashes.py --crashes <dir> --target <bin> [--stdin]
        [--top-frames 3] [--minimize] [--timeout 10] [--out report.md]

With --workspace it reads the manifest for the crash dir (campaign/crashes), the
target (the plain ASAN build), and how to feed input (@@ / stdin). Standalone, it
needs --crashes + --target. --minimize runs afl-tmin on each representative (needs
AFL on PATH). No API key, no LLM — pure triage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_TYPE = re.compile(r"(?:ERROR|SUMMARY): \w*Sanitizer: (?P<type>[\w-]+)")
_FRAME = re.compile(r"^\s*#(?P<n>\d+) 0x[0-9a-fA-F]+ in (?P<func>\S+) (?P<loc>\S+)", re.M)
_SUMMARY = re.compile(r"SUMMARY: \w*Sanitizer: [\w-]+ (?P<loc>\S+?)(?: in (?P<func>\S+))?$",
                      re.M)


@dataclass
class Report:
    kind: str                       # crash type, e.g. heap-buffer-overflow / SIGSEGV / NO_REPRO
    frames: list = field(default_factory=list)   # [(func, file:line), ...] top-N
    summary: str = ""

    def key(self):
        return (self.kind, tuple(self.frames))


def _strip_col(loc: str) -> str:
    """file.c:123:5 -> file.c:123 (column varies, line is the dedup granularity)."""
    parts = loc.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else loc


def parse_report(stderr: str, returncode: int, top: int) -> Report:
    m = _TYPE.search(stderr)
    frames = []
    for fm in _FRAME.finditer(stderr):
        # skip sanitizer-internal frames; keep the first real ones
        func, loc = fm.group("func"), _strip_col(fm.group("loc"))
        if func.startswith(("__asan", "__sanitizer", "__interceptor")):
            continue
        frames.append((func, Path(loc).name if "/" in loc else loc))
        if len(frames) >= top:
            break
    summ = ""
    sm = _SUMMARY.search(stderr)
    if sm:
        summ = sm.group(0).replace("SUMMARY: ", "").strip()
    if m:
        return Report(m.group("type"), frames, summ)
    if returncode is not None and returncode < 0:        # killed by a signal, no ASAN text
        import signal as _sig
        try:
            name = _sig.Signals(-returncode).name
        except Exception:
            name = f"SIG{-returncode}"
        return Report(name, frames, summ or name)
    return Report("NO_REPRO", [], "")


def run_one(target: Path, crash: Path, args_tmpl: list, env: dict,
            timeout: float, top: int) -> Report:
    cmd = [str(target)] + [str(crash) if a == "@@" else a for a in args_tmpl]
    feed_stdin = "@@" not in args_tmpl and not args_tmpl  # no args at all -> try stdin
    try:
        r = subprocess.run(cmd, env=env, timeout=timeout,
                           stdin=open(crash, "rb") if feed_stdin else subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return parse_report(r.stderr.decode("utf-8", "replace"), r.returncode, top)
    except subprocess.TimeoutExpired:
        return Report("TIMEOUT/HANG", [], "timed out")


def minimize(target: Path, crash: Path, out: Path, args_tmpl: list,
             env: dict, timeout: float) -> "Path | None":
    afl_tmin = "afl-tmin"
    cmd = [afl_tmin, "-i", str(crash), "-o", str(out), "--",
           str(target)] + (args_tmpl or ["@@"])
    try:
        subprocess.run(cmd, env=env, timeout=max(timeout * 6, 60),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if out.exists() else None
    except Exception:
        return None


def resolve_from_workspace(ws: Path, manifest: str):
    """(crashes_dir, target, args_tmpl) from a workspace manifest."""
    m = json.loads((ws / manifest).read_text())
    crashes = ws / "campaign" / "crashes"
    # prefer the plain ASAN build for clean reproduction; fall back to agent
    tkey = "plain" if "plain" in m.get("targets", {}) else "agent"
    target = (ws / m["targets"][tkey]).resolve()
    # how to feed input: explicit target_args, else argv->["@@"], else libFuzzer (file arg)
    args = m.get("target_args") or (["@@"] if m.get("input") == "argv" else ["@@"])
    return crashes, target, args


def main() -> int:
    ap = argparse.ArgumentParser(description="Triage campaign crashes into unique bugs")
    ap.add_argument("--workspace", help="resolve crashes/target/args from its manifest")
    ap.add_argument("--manifest", default="target.json")
    ap.add_argument("--crashes", help="crash dir (default: <ws>/campaign/crashes)")
    ap.add_argument("--target", help="ASAN binary to reproduce on")
    ap.add_argument("--stdin", action="store_true", help="feed input via stdin (else as a file arg)")
    ap.add_argument("--top-frames", type=int, default=3, help="stack depth for the dedup key")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--minimize", action="store_true", help="afl-tmin each representative")
    ap.add_argument("--out", help="report path (default: <crashes>/../triage_report.md)")
    a = ap.parse_args()

    args_tmpl = []
    if a.workspace:
        ws = Path(a.workspace).resolve()
        crashes, target, args_tmpl = resolve_from_workspace(ws, a.manifest)
        if a.crashes: crashes = Path(a.crashes).resolve()
        if a.target: target = Path(a.target).resolve()
    else:
        if not (a.crashes and a.target):
            ap.error("standalone needs --crashes and --target")
        crashes, target = Path(a.crashes).resolve(), Path(a.target).resolve()
        args_tmpl = [] if a.stdin else ["@@"]
    if a.stdin:
        args_tmpl = []
    if not crashes.is_dir():
        print(f"no crash dir: {crashes}"); return 2
    if not target.exists():
        print(f"target not found: {target} (build the plain variant first)"); return 2

    env = dict(os.environ)
    env["ASAN_OPTIONS"] = "abort_on_error=1:detect_leaks=0:symbolize=1"
    llvm = os.environ.get("LLVM_BIN")
    if llvm and (Path(llvm) / "llvm-symbolizer").exists():
        env["ASAN_SYMBOLIZER_PATH"] = str(Path(llvm) / "llvm-symbolizer")

    files = sorted(p for p in crashes.glob("crash_*") if p.is_file()) \
        or sorted(p for p in crashes.iterdir() if p.is_file() and "README" not in p.name)
    if not files:
        print(f"no crash inputs in {crashes}"); return 0

    buckets = defaultdict(list)
    for c in files:
        rep = run_one(target, c, args_tmpl, env, a.timeout, a.top_frames)
        buckets[rep.key()].append((c, rep))
    ordered = sorted(buckets.items(), key=lambda kv: -len(kv[1]))

    mins_dir = crashes.parent / "minimized"
    lines = [f"# Crash triage — {target.name}", "",
             f"- inputs triaged: **{len(files)}**",
             f"- distinct bugs: **{sum(1 for k,_ in ordered if k[0] not in ('NO_REPRO',))}**"
             f" (+{sum(1 for k,_ in ordered if k[0]=='NO_REPRO')} non-reproducing group)",
             f"- crash dir: `{crashes}`", ""]
    for i, (key, items) in enumerate(ordered, 1):
        kind, frames = key
        rep_input, rep = items[0]
        loc = frames[0][1] if frames else "?"
        func = frames[0][0] if frames else "?"
        lines.append(f"## Bug {i}: {kind}  ({len(items)} input{'s' if len(items)>1 else ''})")
        lines.append(f"- faulting: `{func}`  @ `{loc}`")
        if rep.summary:
            lines.append(f"- summary: `{rep.summary}`")
        if len(frames) > 1:
            lines.append("- stack: " + " ← ".join(f"{f}@{l}" for f, l in frames))
        lines.append(f"- representative: `{rep_input}`")
        if a.minimize and kind not in ("NO_REPRO", "TIMEOUT/HANG"):
            mins_dir.mkdir(parents=True, exist_ok=True)
            mn = minimize(target, rep_input, mins_dir / f"bug{i}_{kind}.min",
                          args_tmpl, env, a.timeout)
            if mn:
                lines.append(f"- minimized: `{mn}` ({mn.stat().st_size} bytes)")
        lines.append("")

    report = "\n".join(lines)
    out = Path(a.out) if a.out else (crashes.parent / "triage_report.md")
    out.write_text(report)
    print(report)
    print(f"\n[triage] {len(files)} inputs -> {len(ordered)} group(s); report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
