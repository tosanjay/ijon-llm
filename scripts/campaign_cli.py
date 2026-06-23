#!/usr/bin/env python3
"""CC-driven adaptive campaign helpers (Mode 1).

Same loop as campaign_supervisor.py (Mode 2), but here *Claude Code is the analyst
and owns the AFL process*: CC launches afl-fuzz in the background (its native
strength), polls fuzzer_stats, and when the fuzzer stalls it decides the next
annotation. This CLI provides only the MECHANICAL steps — identical to the
supervisor's, reused verbatim — so CC supplies the brain and never hand-rolls
llvm-cov replay, the retire/keep-revert transaction, or crash dedup:

    localize        re-localize the blocker on the CURRENT corpus (frontier + source)
    seed            add a starting annotation (e.g. a discovery-loop keep), build
    apply           the intervention TRANSACTION: keep/revert last + (under map
                    pressure) retire oldest + add the new one -> ONE recompile.
                    CC passes the new annotation and the edges/bitmap_cvg it read
                    from fuzzer_stats; the CLI does the rest and rebuilds.
    collect-crashes copy a round's crashes into the central, deduped campaign/crashes/
    status          show the active set + cumulative crashes
    finalize        restore the source tree to pristine + write campaign/summary.json

CC owns the process/polling loop (launch afl-fuzz background with -i <prev queue>
-o round_N, read fuzzer_stats, detect stall, call `apply`, start the next round).
State persists in campaign/cc_state.json across invocations. No API key is used.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))

import run_target as rt
import campaign_supervisor as cs
from harness.build import Annotation
from harness.localize import load_fi


def _camp(m): return m.ws / "campaign"
def _statef(m): return _camp(m) / "cc_state.json"


def _load(m) -> dict:
    p = _statef(m)
    if p.exists():
        return json.loads(p.read_text())
    return {"reward": m.d.get("reward", "coverage"), "pristine": {}, "active": [],
            "round": 0, "crash_hashes": []}


def _save(m, st: dict):
    _camp(m).mkdir(parents=True, exist_ok=True)
    _statef(m).write_text(json.dumps(st, indent=2))


def _active_objs(st: dict) -> list:
    """state dicts -> cs.ActiveAnn (for materialize/choose_retire)."""
    return [cs.ActiveAnn(Annotation(code=a["code"], after_substring=a["after"]),
                         Path(a["file"]), a.get("dim", ""), a["round_added"],
                         a.get("edges_at_add", 0)) for a in st["active"]]


def _write_and_build(m, st: dict) -> str:
    """Materialize pristine+active to disk and rebuild the agent. Returns '' or err."""
    pristine = {Path(f): t for f, t in st["pristine"].items()}
    for f, txt in cs.materialize(pristine, _active_objs(st)).items():
        f.write_text(txt)
    return cs.build_agent(m, m.path(m.d["targets"]["agent"]))


# --------------------------------------------------------------------------- #
def cmd_localize(m, args):
    fi = load_fi(m.path(m.d["localize"]["fi"]))
    site = rt.LibrarySite(m)
    queue = Path(args.queue).resolve()
    hint, src = cs.localize(fi, m.path(m.d["targets"]["cov"]), queue, site.lib_src,
                            Path(os.environ.get("LLVM_BIN", "/usr/lib/llvm/bin")),
                            _camp(m) / "coverage.json",
                            Path(os.environ.get("TMPDIR", "/tmp")),
                            per_file="@@" in m.target_args)
    print("=" * 72); print("BLOCKER (frontier on the current corpus):"); print("=" * 72)
    print(hint)
    print("\n" + "=" * 72); print("LOCALIZED SOURCE — place an IJON_* on a live line here:")
    print("=" * 72)
    print("\n\n".join(src.values()))
    return 0


def _add(m, st, code, after, edges, dim, do_keep_revert):
    """Shared add path for seed/apply. Returns (ok, message)."""
    site = rt.LibrarySite(m)
    # keep/revert the most-recently-added annotation (apply only)
    if do_keep_revert and st["active"]:
        last = max(st["active"], key=lambda a: a["round_added"])
        if edges <= last.get("edges_at_add", 0):
            st["active"].remove(last)
            print(f"    [revert] last annotation gave no coverage gain "
                  f"(edges {last.get('edges_at_add',0)} -> {edges}); dropping "
                  f"`{last['code']}`")
    if any("".join(a["code"].split()) == "".join(code.split()) for a in st["active"]):
        return False, "annotation already active"
    tgt, exact = site._find_file(after)
    if tgt is None:
        return False, f"anchor not found in library/harness source: {after!r}"
    st["pristine"].setdefault(str(tgt), tgt.read_text(errors="replace"))
    return True, (tgt, exact)


def cmd_seed(m, args):
    st = _load(m)
    ok, res = _add(m, st, args.code, args.after, 0, args.dim or "seed", False)
    if not ok:
        print(f"error: {res}"); return 2
    tgt, exact = res
    st["active"].append({"code": args.code, "after": exact, "file": str(tgt),
                         "dim": args.dim or "seed", "round_added": st["round"],
                         "edges_at_add": 0})
    err = _write_and_build(m, st)
    if err:
        st["active"].pop(); print(f"[build failed] {err}"); return 1
    _save(m, st)
    print(f"[seed] active set: {len(st['active'])}; agent built")
    return 0


def cmd_apply(m, args):
    """The intervention transaction (CC supplies the annotation + observed stats)."""
    st = _load(m); st["round"] += 1
    pre = list(st["active"])
    ok, res = _add(m, st, args.code, args.after, args.edges, args.dim, True)
    if not ok:
        print(f"[skip] {res}"); _save(m, st); return 1
    tgt, exact = res
    retired = None
    if cs.needs_pressure_relief(args.bitmap_cvg, len(st["active"]),
                                args.map_pressure, args.max_active) and st["active"]:
        retired = min(st["active"], key=lambda a: a["round_added"])
        st["active"].remove(retired)
        print(f"    [retire] map pressure (bitmap_cvg={args.bitmap_cvg:.1f}%, "
              f"active={len(st['active'])+1}); evicting oldest `{retired['code']}` "
              f"(gains are banked in the corpus)")
    st["active"].append({"code": args.code, "after": exact, "file": str(tgt),
                         "dim": args.dim, "round_added": st["round"],
                         "edges_at_add": args.edges})
    err = _write_and_build(m, st)
    if err:
        print(f"[build failed] {err} -> reverting this intervention")
        st["active"] = pre; _write_and_build(m, st); _save(m, st); return 1
    _save(m, st)
    print(f"[applied] active set: {len(st['active'])} annotation(s); agent rebuilt at "
          f"{m.d['targets']['agent']}. Resume AFL with -i <this round's queue>.")
    return 0


def cmd_collect_crashes(m, args):
    st = _load(m)
    central = _camp(m) / "crashes"; central.mkdir(parents=True, exist_ok=True)
    seen = set(st["crash_hashes"])
    n = cs.collect_crashes(Path(args.round).resolve(), central, seen)
    st["crash_hashes"] = sorted(seen); _save(m, st)
    print(f"[crashes] +{n} new (cumulative unique: {len(seen)}) -> {central}")
    return 0


def cmd_status(m, args):
    st = _load(m)
    print(f"campaign: round={st['round']}  active={len(st['active'])}  "
          f"unique_crashes={len(st['crash_hashes'])}")
    for a in sorted(st["active"], key=lambda a: a["round_added"]):
        print(f"  [r{a['round_added']}] {a['code']}   ({a.get('dim','')})")
    return 0


def cmd_finalize(m, args):
    st = _load(m)
    for f, txt in st["pristine"].items():          # restore the tree
        Path(f).write_text(txt)
    summary = {"rounds": st["round"], "unique_crashes": len(st["crash_hashes"]),
               "crashes_dir": str(_camp(m) / "crashes"),
               "final_active": [{"code": a["code"], "dim": a.get("dim", ""),
                                 "round_added": a["round_added"]} for a in st["active"]]}
    (_camp(m) / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[finalize] restored {len(st['pristine'])} file(s) to pristine; "
          f"summary -> {_camp(m) / 'summary.json'}")
    print(f"  rounds={st['round']}  unique crashes={len(st['crash_hashes'])}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CC-driven adaptive campaign helpers (Mode 1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("localize", "seed", "apply", "collect-crashes", "status", "finalize"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", required=True)
        s.add_argument("--manifest", default="target.json")
        if name == "localize":
            s.add_argument("--queue", required=True)
        if name in ("seed", "apply"):
            s.add_argument("--code", required=True, help="the IJON_* C statement")
            s.add_argument("--after", required=True, help="exact line to insert AFTER")
            s.add_argument("--dim", default="", help="state it exposes (for logging/LRU)")
        if name == "apply":
            s.add_argument("--edges", type=int, default=0,
                           help="current edges_found (for keep/revert of the last annotation)")
            s.add_argument("--bitmap-cvg", type=float, default=0.0,
                           help="current bitmap_cvg %% (for the retire-under-pressure gate)")
            s.add_argument("--map-pressure", type=float, default=70.0)
            s.add_argument("--max-active", type=int, default=6)
        if name == "collect-crashes":
            s.add_argument("--round", required=True, help="a round's afl output dir")
    args = ap.parse_args()
    m = rt.Manifest((REPO / args.workspace).resolve(), args.manifest)
    return {"localize": cmd_localize, "seed": cmd_seed, "apply": cmd_apply,
            "collect-crashes": cmd_collect_crashes, "status": cmd_status,
            "finalize": cmd_finalize}[args.cmd](m, args)


if __name__ == "__main__":
    raise SystemExit(main())
