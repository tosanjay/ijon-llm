#!/usr/bin/env python3
"""Standalone build-doctor driver (Mode 2): autonomously iterate a draft build.sh
to a green build via the build-doctor LLM role (harness/build_doctor.py).

This is the headless counterpart to what Claude Code does interactively in Mode 1.
Pipeline: bringup.py emits a draft build.sh + target.json with `# TODO(verify)`
slots; this driver runs `build.sh <variant>`, captures the compiler/linker error,
asks the build-doctor to edit build.sh, writes it back (guarding the IJON wiring),
and retries -- bounded by --tries.

    python3 scripts/build_doctor.py --workspace workspace/libxyz [--variant plain]
        [--tries 5] [--model deepseek/deepseek-v4-pro]

Requires an API key (Mode 2). For the no-key path, use the `ijon-reloaded` skill
(Mode 1) where Claude Code is the build-doctor.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from harness.model import AnalystModel
from harness.build_doctor import repair_build
import bringup   # reuse detect_build_system for repo facts


_ERR_PAT = re.compile(r"(error:|undefined reference|cannot find -l|fatal error:|"
                      r"No such file|ld: )", re.I)


def extract_error(output: str, max_lines: int = 25) -> str:
    """Pull the salient compiler/linker error lines; fall back to the tail."""
    hits = [ln.rstrip() for ln in output.splitlines() if _ERR_PAT.search(ln)]
    if hits:
        return "\n".join(hits[:max_lines])
    tail = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    return "\n".join(tail[-max_lines:])


def repo_facts(ws: Path, manifest: dict) -> str:
    """Compact, factual context for the build-doctor: build system, the harness
    #includes, where headers/libs live. Mirrors what a human reads to fix a build."""
    lines = []
    src = None
    for cand in (ws / "build" / manifest.get("name", ""), ):
        if cand.is_dir():
            src = cand
    lines.append(f"build system: {bringup.detect_build_system(src) if src else 'unknown'}"
                 f"  (cloned src: {src.relative_to(ws) if src else 'not cloned yet'})")
    # harness includes
    h = manifest.get("harness")
    if h and (ws / h).exists():
        incs = [ln.strip() for ln in (ws / h).read_text(errors="replace").splitlines()
                if ln.strip().startswith("#include")][:12]
        lines.append(f"harness {h} #includes:\n  " + "\n  ".join(incs))
    if src:
        # candidate include dirs (where config/public headers live) + static libs
        hdrs = sorted({str(p.parent.relative_to(src)) for p in src.rglob("*.h")})[:14]
        lines.append("header dirs under src (for -I):\n  " + "\n  ".join(hdrs))
        libs = [str(p.relative_to(ws)) for p in src.rglob("lib*.a")][:8]
        lines.append("static libs built so far: " + (", ".join(libs) or "(none yet — "
                     "library may not have configured/compiled)"))
    return "\n".join(lines)


def run_build(ws: Path, variant: str) -> tuple[bool, str]:
    """Run `bash build.sh <variant>`; return (success, combined_output)."""
    r = subprocess.run(["bash", "build.sh", variant], cwd=str(ws),
                       capture_output=True, text=True, env=dict(os.environ))
    return r.returncode == 0, (r.stdout + "\n" + r.stderr)


def run_doctor(ws: Path, variant: str, tries: int, model,
               manifest: dict, verbose: bool = True) -> dict:
    """The bounded build-repair loop. `model` must answer repair_build (an
    AnalystModel, or a mock for tests). Returns a result dict."""
    build_sh = ws / "build.sh"
    if not build_sh.exists() and (ws / "build.sh.draft").exists():
        build_sh.write_text((ws / "build.sh.draft").read_text())
        build_sh.chmod(0o755)
        if verbose:
            print("[build-doctor] promoted build.sh.draft -> build.sh")

    history = []
    for attempt in range(tries + 1):       # attempt 0 = the initial build
        ok, output = run_build(ws, variant)
        if ok:
            return {"ok": True, "attempts": attempt, "history": history}
        err = extract_error(output)
        if verbose:
            print(f"\n[build-doctor] build.sh {variant} failed "
                  f"(attempt {attempt}/{tries}):\n  " + err.replace("\n", "\n  "))
        if attempt >= tries:
            return {"ok": False, "attempts": attempt, "error": err, "history": history}
        try:
            fix = repair_build(model, build_sh.read_text(), variant, err,
                               repo_facts(ws, manifest), history)
        except ValueError as e:
            if verbose:
                print(f"[build-doctor] rejected edit: {e}")
            history.append(f"(rejected) {e}")
            continue
        if verbose:
            print(f"[build-doctor] fix: {fix['diagnosis']}")
        build_sh.write_text(fix["fixed_build_sh"]); build_sh.chmod(0o755)
        history.append(fix["diagnosis"] or "(edited build.sh)")
    return {"ok": False, "attempts": tries, "error": "exhausted", "history": history}


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone build-doctor (Mode 2)")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--variant", default="plain")
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    ws = (REPO / args.workspace).resolve()
    import json
    mpath = ws / "target.json"
    if not mpath.exists() and (ws / "target.json.draft").exists():
        mpath = ws / "target.json.draft"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {}

    model = AnalystModel(args.model) if args.model else AnalystModel()
    print(f"[build-doctor] workspace={ws.name} variant={args.variant} "
          f"model={model.model} tries={args.tries}")
    res = run_doctor(ws, args.variant, args.tries, model, manifest)
    if res["ok"]:
        print(f"\n[build-doctor] GREEN after {res['attempts']} repair(s). "
              f"Run the sanity checks (md5 plain≠agent, IJON pass count), then "
              f"build cov/describe + localization, then run the loop.")
        return 0
    print(f"\n[build-doctor] could not fix in {args.tries} tries. Last error:\n"
          f"{res.get('error','')}\nHand off to a human (Mode 1) or raise --tries.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
