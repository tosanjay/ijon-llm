#!/usr/bin/env python3
"""CC-as-analyst driver for IJON-Reloaded Mode 1 (the `ijon-reloaded` skill).

run_target.py runs the WHOLE loop autonomously, calling an external LLM
(deepseek) to propose each annotation. In Mode 1, *Claude Code itself* is the
analyst: it reads the localized source, writes the IJON_* annotation directly
into the library .c (or harness) with its own Edit tool, and only needs the
MECHANICAL steps done reliably and identically to run_target.py. This CLI exposes
exactly those steps as subcommands, reusing run_target.py's helpers verbatim so
the reward is computed the same way:

    context   print the localized library source + harness + localization hint
              (what the analyst reads to decide the annotation)
    plateau   build plain (+ reward tool), fuzz the control to a plateau, record
              the baseline reward in <ws>/.analyst_state.json
    eval      (after CC has applied its annotation to disk) build the agent IJON
              binary, fuzz the eval window, measure the reward vs the baseline,
              and print KEEP or REVERT. Does NOT edit sources -- CC owns the edit
              and reverts it on REVERT (the tree is CC's to manage / git checkout).

Typical Mode-1 loop:
    analyst_cli.py plateau  --workspace workspace/libxyz [--manifest target_diversity.json]
    analyst_cli.py context  --workspace workspace/libxyz [...]      # read, then Edit a .c
    analyst_cli.py eval     --workspace workspace/libxyz [...]      # KEEP -> leave edit; REVERT -> undo edit
    # repeat context/eval for the next roadblock.

No external API key is used anywhere in this file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import run_target as rt          # reuse Manifest, fuzz, diversity, sites, _build_err
from harness import AflConfig


STATE_NAME = ".analyst_state.json"


def _state_path(ws: Path) -> Path:
    return ws / STATE_NAME


def _load_state(ws: Path) -> dict:
    p = _state_path(ws)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(ws: Path, st: dict) -> None:
    _state_path(ws).write_text(json.dumps(st, indent=2))


def _site(m: "rt.Manifest"):
    return rt.LibrarySite(m) if m.annotate == "library" else rt.HarnessSite(m)


def _agent_env(m: "rt.Manifest") -> dict:
    """Env for `build.sh agent`. Library mode recompiles the (CC-edited) library
    from disk; harness mode compiles the (CC-edited) harness in place."""
    env = {"IJON_OUT": str(m.path(m.d["targets"]["agent"]))}
    if m.annotate == "harness":
        env["IJON_HARNESS"] = str(m.path(m.d["harness"]))
    return env


# --------------------------------------------------------------------------- #
def cmd_context(m: "rt.Manifest", args) -> int:
    """Print what the analyst reads to decide an annotation."""
    site = _site(m)
    model_src, hint = site.model_source()
    if hint:
        print("=" * 72)
        print("LOCALIZATION (where the fuzzer is stuck — annotate in/near these):")
        print("=" * 72)
        print(hint)
        print()
    print("=" * 72)
    print(f"SOURCE SHOWN TO ANALYST ({site.render_lines()} lines) — place the "
          f"IJON_* annotation on an executable line here:")
    print("=" * 72)
    print(model_src)
    return 0


def cmd_plateau(m: "rt.Manifest", args) -> int:
    """Build the plain control + reward tool, fuzz to plateau, record baseline."""
    cfg = AflConfig(); cfg.check()
    ws = m.ws
    seeds = m.path(m.d["seeds"])
    reward_kind = m.d.get("reward", "diversity")

    rt.banner("BUILD + FUZZ the plain control to plateau")
    m.build("plain")
    st = {"reward": reward_kind, "manifest": args.manifest}

    if reward_kind == "diversity":
        m.build("describe")
        plain_bin = m.path(m.d["targets"]["plain"])
        snap, plain_q = rt.fuzz(plain_bin, seeds, ws / "out" / "analyst_plain",
                                cfg, ws, args.plateau_timeout, stop_on_crash=False)
        base, nfiles = rt.diversity(m.path(m.d["describe"]), plain_q)
        print(f"    plateau: corpus={nfiles} files, distinct sequences={base}, "
              f"edges={snap.edges_found if snap else '?'}")
        st["base_reward"] = base
        st["best_reward"] = base
    else:
        from harness.coverage import CoverageProbe
        LLVM = Path(os.environ.get("LLVM_BIN", "/usr/lib/llvm/bin"))
        m.build("cov")
        plain_bin = m.path(m.d["targets"]["plain"])
        snap, plain_q = rt.fuzz(plain_bin, seeds, ws / "out" / "analyst_plain",
                                cfg, ws, args.plateau_timeout, stop_on_crash=False)
        probe = CoverageProbe(m.path(m.d["targets"]["cov"]), LLVM,
                              Path(os.environ.get("TMPDIR", "/tmp")) / "analyst_cov")
        base_cov = probe.measure(plain_q, tag="base")
        print(f"    plateau: {base_cov.n_functions} functions covered")
        st["base_reward"] = base_cov.n_functions
        st["best_reward"] = base_cov.n_functions
        st["base_covered"] = sorted(base_cov.covered)

    _save_state(ws, st)
    print(f"\n[baseline recorded in {STATE_NAME}] reward={reward_kind} "
          f"base={st['base_reward']}")
    print("Next: run `context`, Edit one IJON_* annotation into a library .c "
          "(or the harness), then run `eval`.")
    return 0


def cmd_eval(m: "rt.Manifest", args) -> int:
    """CC has applied its annotation on disk. Build the IJON agent, fuzz the eval
    window, measure the reward vs the baseline, and print KEEP / REVERT."""
    cfg = AflConfig(); cfg.check()
    ws = m.ws
    seeds = m.path(m.d["seeds"])
    st = _load_state(ws)
    if not st:
        print(f"error: no {STATE_NAME}; run `plateau` first", file=sys.stderr)
        return 2
    reward_kind = st["reward"]
    base_reward = st["base_reward"]
    best_reward = st.get("best_reward", base_reward)

    rt.banner("BUILD the IJON agent (your annotation, applied on disk)")
    try:
        m.build("agent", extra_env=_agent_env(m))
    except RuntimeError as e:
        print(f"    [BUILD FAILED] {rt._build_err(e)}")
        print("    Fix the annotation (placement/syntax) and re-run `eval`. "
              "Anchor on an executable line; keep the macro + state.")
        return 1
    agent_bin = m.path(m.d["targets"]["agent"])

    rt.banner("FUZZ the agent build (eval window)")
    snap, q = rt.fuzz(agent_bin, seeds, ws / "out" / "analyst_eval", cfg, ws,
                      args.eval_timeout, stop_on_crash=True)
    if snap and snap.solved:
        print(f"    [SOLVED] crash found ({snap.saved_crashes}) — annotation reached a goal")
        return 0

    if reward_kind == "diversity":
        r, nf = rt.diversity(m.path(m.d["describe"]), q)
        keep = r > best_reward
        print(f"    distinct sequences: base={base_reward} best={best_reward} "
              f"now={r} ({nf} files) -> {'KEEP' if keep else 'REVERT'}")
    else:
        from harness.coverage import CoverageProbe
        LLVM = Path(os.environ.get("LLVM_BIN", "/usr/lib/llvm/bin"))
        probe = CoverageProbe(m.path(m.d["targets"]["cov"]), LLVM,
                              Path(os.environ.get("TMPDIR", "/tmp")) / "analyst_cov")
        after = probe.measure(q, tag="eval")
        base_covered = set(st.get("base_covered", []))
        new = sorted(after.covered - base_covered)
        keep = bool(new)
        print(f"    functions: base={base_reward} now={after.n_functions} -> "
              f"{'KEEP' if keep else 'REVERT'}")
        if new:
            print(f"    new functions reached: {', '.join(new[:12])}"
                  f"{' …' if len(new) > 12 else ''}")
        r = after.n_functions

    if keep:
        st["best_reward"] = r if reward_kind == "diversity" else max(best_reward, r)
        if reward_kind == "coverage":
            # accumulate: the kept annotation's corpus coverage becomes the new base
            st["base_covered"] = sorted(set(st.get("base_covered", [])) |
                                        set(after.covered))
        st.setdefault("kept", []).append(args.note or "(annotation applied on disk)")
        _save_state(ws, st)
        print("\n[KEEP] leave your annotation in place; run `context` again for the "
              "NEXT roadblock (it will now show your kept annotation).")
    else:
        print("\n[REVERT] undo your last annotation edit (Edit it back out or "
              "`git checkout` the file), then try a different state/primitive.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CC-as-analyst driver (Mode 1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("context", "plateau", "eval"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", required=True)
        s.add_argument("--manifest", default="target.json")
        s.add_argument("--plateau-timeout", type=float, default=100)
        s.add_argument("--eval-timeout", type=float, default=200)
        s.add_argument("--note", default=None, help="(eval) label for a kept annotation")
    args = ap.parse_args()

    ws = (REPO / args.workspace).resolve()
    m = rt.Manifest(ws, args.manifest)
    return {"context": cmd_context, "plateau": cmd_plateau,
            "eval": cmd_eval}[args.cmd](m, args)


if __name__ == "__main__":
    raise SystemExit(main())
