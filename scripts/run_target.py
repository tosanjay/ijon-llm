#!/usr/bin/env python3
"""Generic autonomous-IJON loop for a REAL library target.

Where solve_target_llm.py handles single-file synthetic targets (one
Builder.compile), this drives any prepared workspace that links an external
library. The per-target specifics live in <workspace>/target.json + build.sh;
this script is target-agnostic.

The loop is the same as on libpng/libtpms, just delegated:
    build plain -> fuzz to plateau -> [strip + LLM: diagnose + annotate]
      -> apply to the harness -> build.sh agent -> re-fuzz -> keep/revert
The model only ever sees the fairness-stripped source (make_clean_source).

Keep/revert reward (target.json "reward"):
  diversity  -- distinct state SEQUENCES (class 2). A "describe" tool emits each
                corpus input's sequence of tokens; we count distinct ones. This
                is the right metric when the annotation reorders exploration of
                already-covered code (libarchive entry/format sequences).
  coverage   -- new source functions via llvm-cov replay (class 3). Correct when
                the annotation should let the fuzzer reach NEW code (libpng_loop).

Usage:
    python3 scripts/run_target.py --workspace workspace/libarchive --iters 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from harness import (AflConfig, FuzzerController, PlateauDetector,
                     apply_annotation, make_clean_source)
from harness.agent import propose_annotation
from harness.model import AnalystModel
from harness.fuzzer import Snapshot


def banner(m: str) -> None:
    print(f"\n{'='*72}\n{m}\n{'='*72}")


class Manifest:
    def __init__(self, ws: Path):
        self.ws = ws
        self.d = json.loads((ws / "target.json").read_text())
        # manifest env are DEFAULTS only -- the real environment always wins
        for k, v in self.d.get("env", {}).items():
            os.environ.setdefault(k, v)

    def path(self, rel: str) -> Path:
        return (self.ws / rel).resolve()

    def build(self, variant: str, extra_env: dict | None = None) -> None:
        cmd = self.d["build"][variant]
        env = dict(os.environ); env.update(extra_env or {})
        r = subprocess.run(cmd, cwd=str(self.ws), env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"build {variant} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
        return r.stdout


def diversity(describe_bin: Path, queue: Path) -> tuple[int, int]:
    """(distinct sequences, files parsed) over a corpus dir via the describe tool."""
    files = sorted(p for p in queue.iterdir() if p.is_file()) if queue.exists() else []
    if not files:
        return 0, 0
    env = dict(os.environ); env["ASAN_OPTIONS"] = "detect_leaks=0"
    seqs = set()
    for f in files:
        r = subprocess.run([str(describe_bin), str(f)], capture_output=True,
                           text=True, env=env)
        line = r.stdout.strip()
        if line:
            seqs.add(line)
    return len(seqs), len(files)


def fuzz(target: Path, seeds: Path, out: Path, cfg: AflConfig, ws: Path,
         timeout: float, stop_on_crash: bool, min_stall: int = 30):
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    fc = FuzzerController(target, seeds, out, cfg, cwd=ws, stop_on_crash=stop_on_crash)
    det = PlateauDetector(min_stall_seconds=min_stall)
    fc.run_until(lambda s: s.solved or det.is_plateau(s), timeout=timeout, poll=3.0)
    return fc.snapshot(), out / "default" / "queue"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--plateau-timeout", type=float, default=90)
    ap.add_argument("--eval-timeout", type=float, default=90)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    ws = (REPO / args.workspace).resolve()
    m = Manifest(ws)
    cfg = AflConfig(); cfg.check()
    seeds = m.path(m.d["seeds"])
    reward_kind = m.d.get("reward", "diversity")

    # --- fairness: the only source the model sees, ijon-stripped + asserted ----
    clean_parts, harness_clean = [], None
    for rel in m.d["focus"]:
        clean = make_clean_source(m.path(rel).read_text())
        clean_parts.append(clean)
        if rel == m.d["harness"]:
            harness_clean = clean
    focused = "\n\n".join(clean_parts)
    assert harness_clean is not None, "harness must be in focus[]"
    print(f"[0] fairness gate: {len(focused.splitlines())} lines shown to model; "
          f"no 'ijon' token present (verified)")

    banner("1) BUILD + FUZZ the plain control to plateau")
    m.build("plain")
    if reward_kind == "diversity":
        m.build("describe")
    plain_bin = m.path(m.d["targets"]["plain"])
    snap, plain_q = fuzz(plain_bin, seeds, ws / "out" / "rt_plain", cfg, ws,
                         args.plateau_timeout, stop_on_crash=False)
    if reward_kind == "diversity":
        describe = m.path(m.d["describe"])
        base_reward, nfiles = diversity(describe, plain_q)
        print(f"    plateau: corpus={nfiles} files, distinct sequences={base_reward}, "
              f"edges={snap.edges_found if snap else '?'}")
    else:
        from harness.coverage import CoverageProbe
        LLVM = Path(os.environ.get("LLVM_BIN", "/usr/lib/llvm/bin"))
        m.build("cov")
        probe = CoverageProbe(m.path(m.d["targets"]["cov"]), LLVM,
                              Path(os.environ.get("TMPDIR", "/tmp")) / "rt_cov")
        base_cov = probe.measure(plain_q, tag="base")
        base_reward = base_cov.n_functions
        print(f"    plateau: {base_reward} functions covered")

    # --- iterate ---------------------------------------------------------------
    model = AnalystModel(args.model) if args.model else AnalystModel()
    scratch = ws / "src" / (Path(m.d["harness"]).stem + "_agent.c")
    history, kept = [], []
    best_reward = base_reward
    current_src = harness_clean   # accumulates KEPT annotations across iterations
    for it in range(1, args.iters + 1):
        banner(f"2.{it}) ANALYST proposes (prior failed: {len(history)})")
        print(f"    model: {model.model}")
        # the model sees the CURRENT annotated source (its own kept additions are
        # fair to show -- the ground-truth answer was already stripped), so it can
        # build on prior annotations and find the NEXT roadblock.
        p = propose_annotation(model, current_src, snap,
                               source_name=m.d["source_name"], history=history,
                               localization=m.d.get("localization"))
        print(f"    why_stuck      : {p.why_stuck}")
        print(f"    failure_class  : {p.failure_class}")
        print(f"    relevant_state : {p.relevant_state}")
        print(f"    {p.macro}: {p.annotation.code}")
        print(f"    after_substring: {p.annotation.after_substring!r}")

        if "".join(p.annotation.code.split()) in "".join(current_src.split()):
            note = "annotation already present; find the NEXT roadblock"
            print(f"    [REVERT] {note}"); history.append((p, note)); continue
        try:
            patched = apply_annotation(current_src, p.annotation)
        except ValueError as e:
            note = f"could not place annotation: {e}"
            print(f"    [REVERT] {note}"); history.append((p, note)); continue

        scratch.write_text(patched)
        print(f"    patched -> {scratch.name}; building AFL+IJON ...")
        try:
            m.build("agent", extra_env={"IJON_HARNESS": str(scratch),
                                        "IJON_OUT": str(m.path(m.d["targets"]["agent"]))})
        except RuntimeError as e:
            note = f"patched target failed to build ({str(e)[:120]})"
            print(f"    [REVERT] {note}"); history.append((p, note)); continue

        agent_bin = m.path(m.d["targets"]["agent"])
        snap2, q = fuzz(agent_bin, seeds, ws / "out" / f"rt_iter{it}", cfg, ws,
                        args.eval_timeout, stop_on_crash=True)
        if snap2 and snap2.solved:
            print(f"    [SOLVED] crash found ({snap2.saved_crashes}) — annotation reached the goal")
            kept.append((p.macro, p.annotation.code)); break

        if reward_kind == "diversity":
            r, nf = diversity(describe, q)
            verdict = r > best_reward
            print(f"    distinct sequences: base={base_reward} best={best_reward} now={r} "
                  f"({nf} files) -> {'KEEP' if verdict else 'revert'}")
        else:
            after = probe.measure(q, tag=f"iter{it}")
            r = after.n_functions; verdict = bool(after.new_vs(base_cov))
            print(f"    functions: base={base_reward} now={r} -> {'KEEP' if verdict else 'revert'}")

        if verdict:
            kept.append((p.macro, p.annotation.code)); best_reward = r
            current_src = patched           # commit: future proposals build on this
            if reward_kind == "coverage": base_cov = after
        else:
            note = (f"no reward gain (reward {reward_kind}: {best_reward} -> {r}); "
                    f"raw IJON edge growth does not count — try a different state/primitive")
            history.append((p, note))

    banner("VERDICT")
    gain = (best_reward / base_reward) if base_reward else 0
    print(f"    reward ({reward_kind}): plain={base_reward} -> best={best_reward}  "
          f"({gain:.1f}x)")
    print(f"    kept {len(kept)} annotation(s): {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
