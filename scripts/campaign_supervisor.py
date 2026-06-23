#!/usr/bin/env python3
"""Adaptive long-running IJON campaign (Mode 2, standalone).

Where run_target.py is a short DISCOVERY loop (find an annotation, then it cleans
up), this is a long bug-hunting CAMPAIGN that keeps an analyst on call: it fuzzes
in the background, watches progress, and whenever the fuzzer STALLS it intervenes
exactly the way a human IJON analyst would —

    fuzz (background) -> stall? -> re-localize the new blocker -> [analyst: annotate]
      -> (under map pressure: retire a mined-out annotation) -> ONE recompile
      -> RESUME (re-seed from the accumulated queue) -> repeat

Saturation policy (three levers, applied in the single intervention transaction):
  * keep/revert  -- if the last-added annotation didn't grow coverage, drop it.
  * bound active -- cap how many annotations are live at once (--max-active).
  * lazy retire  -- only UNDER real map pressure (bitmap_cvg high) evict the
                    oldest (LRU) active annotation; its gains are already BANKED in
                    the corpus, so removing it frees map budget without losing
                    territory (the inputs persist + still cover their code).

Resume = re-seed from the accumulated queue (robust under recompiled instrumentation):
each round is a fresh -o with -i <prev round's queue>. Crashes do NOT live in the
queue and old round dirs are pruned, so every round we COPY crashes into a central,
deduped campaign/crashes/ (authoritative; a later triage script consumes it).

Requires an API key (Mode 2). For the no-key path, drive this from the
`ijon-reloaded` skill (Mode 1) where Claude Code plays the analyst.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))

import run_target as rt
from harness import AflConfig, apply_annotation
from harness.build import Annotation
from harness.fuzzer import FuzzerController
from harness.agent import propose_annotation
from harness.model import AnalystModel
from harness.localize import load_fi, load_cov, build_localization_context


@dataclass
class ActiveAnn:
    """One annotation currently live on disk (part of the active working set)."""
    ann: Annotation
    file: Path
    dim: str                 # the state it exposes (relevant_state) — for logging/LRU
    round_added: int
    edges_at_add: int        # snapshot.edges_found when it was added (for keep/revert)


# --------------------------------------------------------------------------- #
#  Pure policy helpers (no AFL / no LLM — unit-testable)                        #
# --------------------------------------------------------------------------- #
def needs_pressure_relief(bitmap_cvg: float, n_active: int,
                          map_pressure: float, max_active: int) -> bool:
    """Retire only under REAL pressure: the map is filling, or we're at the active
    cap. Lazy by design — don't evict a still-productive annotation early."""
    return bitmap_cvg >= map_pressure or n_active >= max_active


def choose_retire(active: list) -> "ActiveAnn | None":
    """LRU: the oldest active annotation (most likely mined-out; its gains are
    banked in the corpus). v1 heuristic — pluggable."""
    return min(active, key=lambda a: a.round_added) if active else None


def materialize(pristine: dict, active: list) -> dict:
    """Compute the on-disk text for every touched file = its pristine source with
    all of THAT file's active annotations applied in add-order. Returns
    {file: text}. A file whose annotations were all retired reverts to pristine."""
    out = {f: txt for f, txt in pristine.items()}            # start from pristine
    by_file: dict = {}
    for a in sorted(active, key=lambda a: a.round_added):
        by_file.setdefault(a.file, []).append(a)
    for f, anns in by_file.items():
        txt = pristine[f]
        for a in anns:
            txt = apply_annotation(txt, a.ann)
        out[f] = txt
    return out


# --------------------------------------------------------------------------- #
class CampaignSupervisor:
    def __init__(self, m: "rt.Manifest", model, opts):
        self.m = m
        self.model = model
        self.o = opts
        self.cfg = AflConfig(); self.cfg.check()
        self.ws = m.ws
        self.seeds = m.path(m.d["seeds"])
        self.reward = m.d.get("reward", "coverage")
        self.site = rt.LibrarySite(m)                 # reuse _find_file + lib_src + harness
        self.fi = load_fi(m.path(m.d["localize"]["fi"]))
        self.camp = self.ws / "campaign"
        self.crashes = self.camp / "crashes"
        self.crashes.mkdir(parents=True, exist_ok=True)
        self.pristine: dict = {}                      # file -> original text (restore at end)
        self.active: list = []                        # working set on disk
        self.round = 0
        self.barren = 0
        self.llm_on = True
        self.crash_hashes: set = set()
        self.cov_bin = m.path(m.d["targets"]["cov"])
        self.agent_bin = m.path(m.d["targets"]["agent"])
        self.LLVM = Path(os.environ.get("LLVM_BIN", "/usr/lib/llvm/bin"))

    # ---- source transaction -------------------------------------------------
    def _snapshot(self, f: Path):
        if f not in self.pristine:
            self.pristine[f] = f.read_text(errors="replace")

    def _write_active(self):
        for f, txt in materialize(self.pristine, self.active).items():
            f.write_text(txt)

    def _build_agent(self) -> str:
        env = {"IJON_OUT": str(self.agent_bin)}
        if self.m.annotate == "harness":
            env["IJON_HARNESS"] = str(self.m.path(self.m.d["harness"]))
        try:
            self.m.build("agent", extra_env=env)
            return ""
        except RuntimeError as e:
            return rt._build_err(e)

    # ---- localization (re-run each intervention on the CURRENT corpus) -------
    def _tool(self, name: str) -> str:
        p = self.LLVM / name
        return str(p) if p.exists() else name

    def _export_coverage(self, queue: Path):
        files = sorted(str(p) for p in queue.iterdir() if p.is_file()) \
            if queue.exists() else []
        if not files:
            return None
        tmp = Path(os.environ.get("TMPDIR", "/tmp"))
        praw, pdat = tmp / "camp.profraw", tmp / "camp.profdata"
        cj = self.camp / "coverage.json"
        env = dict(os.environ); env["LLVM_PROFILE_FILE"] = str(praw)
        if "@@" in self.m.target_args:                # utility cov bin: one file at a time
            for fp in files:
                subprocess.run([str(self.cov_bin), fp], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:                                         # libFuzzer cov bin: all files at once
            subprocess.run([str(self.cov_bin), *files], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not praw.exists():
            return None
        subprocess.run([self._tool("llvm-profdata"), "merge", "-sparse",
                        str(praw), "-o", str(pdat)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = subprocess.run([self._tool("llvm-cov"), "export",
                            f"-instr-profile={pdat}", str(self.cov_bin)],
                           capture_output=True, text=True)
        if not r.stdout:
            return None
        cj.write_text(r.stdout)
        return cj

    def _localize(self, queue: Path):
        cj = self._export_coverage(queue)
        cov = load_cov(cj) if cj else {}
        return build_localization_context(self.fi, cov, source_root=self.site.lib_src)

    # ---- crash aggregation + disk hygiene -----------------------------------
    def _collect_crashes(self, round_dir: Path) -> int:
        """Copy this round's crashes into the central, deduped store. Returns the
        number of NEW unique crashes added."""
        new = 0
        for sub in ("crashes", "hangs"):
            d = round_dir / "default" / sub
            if not d.is_dir():
                continue
            for c in d.glob("id:*"):
                try:
                    h = hashlib.sha1(c.read_bytes()).hexdigest()
                except Exception:
                    continue
                if h in self.crash_hashes:
                    continue
                self.crash_hashes.add(h)
                dest = self.crashes / f"{sub[:-1]}_{h[:12]}_{c.name.replace(':','_')}"
                shutil.copy2(c, dest)
                if sub == "crashes":
                    new += 1
        return new

    def _prune_queue(self, round_dir: Path):
        """Drop a superseded round's queue (crashes already collected centrally)."""
        q = round_dir / "default" / "queue"
        if q.is_dir():
            shutil.rmtree(q, ignore_errors=True)

    # ---- the single intervention transaction --------------------------------
    def _keep_revert_last(self, snap):
        """Before adding, drop the most-recently-added annotation if it produced no
        coverage growth since it was added (it didn't earn its map slots)."""
        if not self.active:
            return
        last = max(self.active, key=lambda a: a.round_added)
        if snap.edges_found <= last.edges_at_add:
            self.active.remove(last)
            print(f"    [revert] last annotation gave no coverage gain "
                  f"(edges {last.edges_at_add} -> {snap.edges_found}); dropping "
                  f"`{last.ann.code}`")

    def _intervene(self, snap, queue: Path) -> bool:
        """Re-localize, propose one annotation, (lazily retire under pressure),
        apply BOTH edits + rebuild ONCE. Returns True if a new buildable annotated
        binary is ready; False if nothing useful was applied (a barren round)."""
        self._keep_revert_last(snap)
        hint, src = self._localize(queue)
        try:
            p = propose_annotation(self.model, src, snap,
                                   source_name=self.m.d["source_name"],
                                   localization=hint, reward_kind=self.reward)
        except Exception as e:
            print(f"    [analyst error] {str(e)[:140]}"); return False
        print(f"    why_stuck : {p.why_stuck}")
        print(f"    {p.macro}: {p.annotation.code}   after {p.annotation.after_substring!r}")
        if any("".join(a.ann.code.split()) == "".join(p.annotation.code.split())
               for a in self.active):
            print("    [skip] analyst re-proposed an already-active annotation")
            return False
        tgt, exact = self.site._find_file(p.annotation.after_substring)
        if tgt is None:
            print("    [skip] anchor not found in library/harness source"); return False
        self._snapshot(tgt)
        place = p.annotation if exact == p.annotation.after_substring else \
            Annotation(code=p.annotation.code, after_substring=exact)
        # pressure? retire the oldest (banked) annotation in the SAME transaction
        retired = None
        if needs_pressure_relief(snap.bitmap_cvg, len(self.active),
                                 self.o.map_pressure, self.o.max_active):
            retired = choose_retire(self.active)
            if retired:
                self.active.remove(retired)
                print(f"    [retire] map pressure (bitmap_cvg={snap.bitmap_cvg:.1f}%, "
                      f"active={len(self.active)+1}); evicting oldest `{retired.ann.code}` "
                      f"(its gains are banked in the corpus)")
        new = ActiveAnn(place, tgt, p.relevant_state or p.why_stuck[:40],
                        self.round, snap.edges_found)
        self.active.append(new)
        self._write_active()
        err = self._build_agent()
        if err:
            print(f"    [build failed] {err} -> reverting this intervention")
            self.active.remove(new)
            if retired:
                self.active.append(retired)        # undo the eviction too
            self._write_active()
            return False
        print(f"    [applied] active set now {len(self.active)} annotation(s); rebuilt")
        return True

    # ---- the campaign loop --------------------------------------------------
    def run(self) -> int:
        # seed annotations from a prior discovery run, if given
        if self.o.seed_annotation and Path(self.o.seed_annotation).exists():
            for d in json.loads(Path(self.o.seed_annotation).read_text()):
                f, exact = self.site._find_file(d["after_substring"])
                if f:
                    self._snapshot(f)
                    self.active.append(ActiveAnn(
                        Annotation(code=d["code"], after_substring=exact),
                        f, d.get("dim", "seed"), 0, 0))
        rt.banner("CAMPAIGN: build cov + initial agent")
        self.m.build("cov")
        self._write_active();
        err = self._build_agent()
        if err:
            print(f"initial agent build failed: {err}"); return 1

        deadline = time.monotonic() + self.o.hours * 3600
        stall = self.o.stall_min * 60
        prev_queue = self.seeds
        total_new_crashes = 0
        try:
            while time.monotonic() < deadline:
                self.round += 1
                rdir = self.camp / f"round_{self.round}"
                rt.banner(f"ROUND {self.round}  (active={len(self.active)}, "
                          f"llm={'on' if self.llm_on else 'off'})")
                fc = FuzzerController(self.agent_bin, prev_queue, rdir, self.cfg,
                                     cwd=self.ws, stop_on_crash=False,
                                     target_args=self.m.target_args)
                fc.start()
                snap = None
                while time.monotonic() < deadline:
                    time.sleep(self.o.poll)
                    snap = fc.snapshot()
                    if snap is None:
                        if not fc.is_running():
                            break
                        continue
                    if self.llm_on and snap.time_wo_finds >= stall \
                            and snap.run_time >= stall:
                        print(f"    stall: {snap.time_wo_finds}s without a new find "
                              f"(edges={snap.edges_found}, bitmap_cvg={snap.bitmap_cvg:.1f}%)")
                        break
                fc.stop()
                got = self._collect_crashes(rdir)
                total_new_crashes += got
                if got:
                    print(f"    [crashes] +{got} new (total unique: {len(self.crash_hashes)}) "
                          f"-> {self.crashes}")
                cur_queue = rdir / "default" / "queue"
                if time.monotonic() >= deadline:
                    break
                if self.llm_on:
                    ok = self._intervene(snap, cur_queue if cur_queue.exists() else prev_queue)
                    self.barren = 0 if ok else self.barren + 1
                    if self.barren >= self.o.give_up:
                        self.llm_on = False
                        print(f"    [give up] {self.barren} barren interventions; "
                              f"fuzzing the rest of the budget on the current binary")
                # disk hygiene: the round BEFORE prev is fully superseded now
                if self.round >= 2:
                    self._prune_queue(self.camp / f"round_{self.round - 1}")
                prev_queue = cur_queue if cur_queue.exists() else prev_queue
        finally:
            for f, original in self.pristine.items():     # leave the tree pristine
                f.write_text(original)
            summary = {
                "rounds": self.round,
                "unique_crashes": len(self.crash_hashes),
                "crashes_dir": str(self.crashes),
                "final_active": [{"code": a.ann.code, "dim": a.dim,
                                  "round_added": a.round_added} for a in self.active],
            }
            (self.camp / "summary.json").write_text(json.dumps(summary, indent=2))
            rt.banner("CAMPAIGN DONE")
            print(f"    rounds={self.round}  unique crashes={len(self.crash_hashes)}  "
                  f"-> {self.crashes}")
            print(f"    summary: {self.camp / 'summary.json'}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Adaptive long-running IJON campaign")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--manifest", default="target.json")
    ap.add_argument("--hours", type=float, default=8.0, help="total campaign budget")
    ap.add_argument("--stall-min", type=float, default=20.0,
                    help="minutes without a new find before the analyst intervenes")
    ap.add_argument("--map-pressure", type=float, default=70.0,
                    help="bitmap_cvg %% at/above which a retirement is triggered")
    ap.add_argument("--max-active", type=int, default=6,
                    help="cap on simultaneously-active annotations")
    ap.add_argument("--give-up", type=int, default=3,
                    help="consecutive barren interventions before disabling the analyst")
    ap.add_argument("--poll", type=float, default=30.0, help="seconds between stat polls")
    ap.add_argument("--seed-annotation", default=None,
                    help="JSON list of {code, after_substring[, dim]} to start with "
                         "(e.g. the kept annotation from a discovery run)")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    ws = (REPO / args.workspace).resolve()
    m = rt.Manifest(ws, args.manifest)
    model = AnalystModel(args.model) if args.model else AnalystModel()
    print(f"[campaign] {ws.name} manifest={args.manifest} model={model.model} "
          f"hours={args.hours} stall={args.stall_min}min map_pressure={args.map_pressure}%")
    return CampaignSupervisor(m, model, args).run()


if __name__ == "__main__":
    raise SystemExit(main())
