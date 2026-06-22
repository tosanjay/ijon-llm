#!/usr/bin/env python3
"""Generic autonomous-IJON loop for a REAL library target.

Where solve_target_llm.py handles single-file synthetic targets (one
Builder.compile), this drives any prepared workspace that links an external
library. The per-target specifics live in <workspace>/target.json + build.sh;
this script is target-agnostic.

The loop is the same on every target, just delegated:
    build plain -> fuzz to plateau -> [LLM: diagnose + annotate]
      -> apply the annotation -> build.sh agent -> re-fuzz -> keep/revert

WHERE the annotation goes (target.json "annotate"):
  library  -- (default for real libs) the annotation belongs INSIDE the library
              code -- the parser/decoder functions where the stuck state lives.
              We localize with the FI static call graph intersected with llvm-cov
              runtime coverage (the coverage frontier), show the model only those
              functions, then place the annotation in the matching library .c and
              rebuild the library. This is how libpng/libtpms were annotated and
              is what produced their numbers.
  harness  -- the annotation belongs in the harness itself, valid only when the
              harness drives the decode loop and the state is reachable through
              the library's PUBLIC API (libarchive: per-entry format/filetype).

Keep/revert reward (target.json "reward"):
  diversity  -- distinct state SEQUENCES (class 2), via a "describe" tool.
  coverage   -- new source functions via llvm-cov replay (class 3).

Fairness (target.json "fairness_gate", default OFF):
  OFF (real use): the model sees the REAL source, including any IJON annotations
       already present -- it builds on them and finds the NEXT roadblock.
  ON  (benchmark): strip a planted #ifdef _USE_IJON reference + assert no 'ijon'
       token leaks, so we can measure whether the agent re-derives it BLIND. Our
       bundled eval workspaces (e.g. libarchive) set this; a real target does not.

Usage:
    python3 scripts/run_target.py --workspace workspace/libpng --iters 3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from harness import (AflConfig, FuzzerController, PlateauDetector,
                     apply_annotation, make_clean_source)
from harness.build import Annotation
from harness.agent import propose_annotation, repair_annotation
from harness.model import AnalystModel
from harness.fuzzer import Snapshot
from harness.localize import load_fi, load_cov, build_localization_context


def banner(m: str) -> None:
    print(f"\n{'='*72}\n{m}\n{'='*72}")


def _norm(s: str) -> str:
    return " ".join(s.split())


@dataclass
class BuildOutcome:
    """Result of trying to apply + build one annotation. `detail` is the full
    error (for the repair agent); `region` is the patched source around the
    insertion (so repair sees where its statement landed)."""
    ok: bool
    note: str = ""
    binary: Optional[Path] = None
    undo: Callable = lambda: None
    detail: str = ""
    region: str = ""
    target: Optional[Path] = None   # file the anchor landed in (library .c or harness)


def _strip_c_comments(code: str) -> str:
    """Drop /* ... */ block comments, // line comments, and blank lines, keeping
    every executable line verbatim. Used to compact the harness once the agent has
    moved on to the library: comments (license header, boilerplate) go, but no
    annotatable statement is ever removed -- so retiring the harness can never cost
    a future annotation. (A '/*' inside a string literal is mis-handled, which is
    harmless: it only affects what the model READS, never placement, which matches
    anchors as substrings against the on-disk source.)"""
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    out = []
    for line in code.splitlines():
        line = re.sub(r"//.*$", "", line).rstrip()
        if line.strip():
            out.append(line)
    return "\n".join(out)


def _region(text: str, needle: str, ctx: int = 6) -> str:
    """The lines around `needle` in `text` (context for the repair agent)."""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if needle and needle in l:
            return "\n".join(lines[max(0, i - ctx):i + ctx + 1])
    return ""


def _build_err(exc) -> str:
    """Pull the real compiler error(s) out of a build RuntimeError -- not the
    first 120 chars, which are often a pre-existing warning."""
    txt = str(exc)
    errs = [l.strip() for l in txt.splitlines() if "error:" in l.lower()]
    if errs:
        return "; ".join(errs[:3])[:300]
    tail = [l.strip() for l in txt.splitlines() if l.strip()][-3:]
    return " / ".join(tail)[:300]


def _anchor_problem(line: str) -> str | None:
    """Reject an anchor that is not an executable-statement line (the IJON C
    statement would not compile after it). Returns a feedback note or None."""
    s = line.strip()
    if not s:
        return "anchor resolves to a blank line; pick an executable statement line"
    if s.startswith("#") or "defined(" in s:
        return ("anchor is a preprocessor line (#if/#define/defined(...)); an IJON "
                "statement must go on an EXECUTABLE code line inside a function "
                "body where the state variable is live, not a preprocessor line")
    return None


class Manifest:
    def __init__(self, ws: Path, manifest: str = "target.json"):
        self.ws = ws
        self.d = json.loads((ws / manifest).read_text())
        # manifest env are DEFAULTS only -- the real environment always wins
        for k, v in self.d.get("env", {}).items():
            os.environ.setdefault(k, v)

    def path(self, rel: str) -> Path:
        return (self.ws / rel).resolve()

    @property
    def annotate(self) -> str:
        return self.d.get("annotate", "harness")

    @property
    def fairness(self) -> bool:
        return bool(self.d.get("fairness_gate", False))

    def build(self, variant: str, extra_env: dict | None = None) -> str:
        cmd = self.d["build"][variant]
        env = dict(os.environ); env.update(extra_env or {})
        r = subprocess.run(cmd, cwd=str(self.ws), env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"build {variant} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
        return r.stdout


# --------------------------------------------------------------------------- #
#  Reward metrics                                                             #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
#  Annotation site: where the model reads, and where the patch lands           #
# --------------------------------------------------------------------------- #
class HarnessSite:
    """annotate == 'harness': the annotation goes into the harness file. The
    library is prebuilt; only the (patched) harness is recompiled each iter."""

    def __init__(self, m: Manifest):
        self.m = m
        self.scratch = m.ws / "src" / (Path(m.d["harness"]).stem + "_agent.c")

    def model_source(self) -> tuple[str, str | None]:
        """Returns (source shown to model, localization hint)."""
        clean_parts, harness_clean = [], None
        for rel in self.m.d["focus"]:
            raw = self.m.path(rel).read_text()
            clean = make_clean_source(raw) if self.m.fairness else raw
            clean_parts.append(clean)
            if rel == self.m.d["harness"]:
                harness_clean = clean
        assert harness_clean is not None, "harness must be in focus[]"
        # the harness is the source the model annotates + accumulates on; other
        # focus files are shown as context only.
        self._render = "\n\n".join(clean_parts)
        return harness_clean, self.m.d.get("localization")

    def render_lines(self) -> int:
        return len(self._render.splitlines())

    def apply_and_build(self, model_src: str, ann: Annotation) -> BuildOutcome:
        """Apply to the harness, build."""
        try:
            patched = apply_annotation(model_src, ann)
        except ValueError as e:
            return BuildOutcome(False, note=f"could not place annotation: {e}")
        self.scratch.write_text(patched)
        try:
            self.m.build("agent", extra_env={
                "IJON_HARNESS": str(self.scratch),
                "IJON_OUT": str(self.m.path(self.m.d["targets"]["agent"]))})
        except RuntimeError as e:
            return BuildOutcome(False, note=f"failed to build: {_build_err(e)}",
                                detail=str(e), region=_region(patched, ann.code.split('(')[0]))
        return BuildOutcome(True, binary=self.m.path(self.m.d["targets"]["agent"]))

    def cleanup(self):
        pass


class LibrarySite:
    """annotate == 'library': the annotation goes INSIDE the library source AND,
    when the interesting state lives there, the harness too. We localize (FI
    static graph + llvm-cov frontier) to the few library functions the fuzzer is
    stuck behind, also show the harness, place the annotation in whichever file
    (library .c or harness) holds the chosen anchor, and rebuild. The library-mode
    build compiles the harness from disk under the IJON pass, so a harness-landed
    annotation needs no special build path. Kept annotations accumulate ON DISK;
    touched files are restored to pristine at the end."""

    _HBANNER = "/* ===== harness:"             # stable marker to locate the section

    def __init__(self, m: Manifest):
        self.m = m
        self.lib_src = m.path(m.d["library_src"])
        self.harness = m.path(m.d["harness"]) if m.d.get("harness") else None
        self._pristine: dict[Path, str] = {}   # file -> original content (for restore)

    @property
    def has_harness(self) -> bool:
        return bool(self.harness and self.harness.exists())

    def model_source(self) -> tuple[str, str | None]:
        loc = self.m.d["localize"]
        fi = load_fi(self.m.path(loc["fi"]))
        cov = load_cov(self.m.path(loc["cov"]))
        hint, src = build_localization_context(fi, cov, source_root=self.lib_src)
        parts = []
        for name, text in src.items():
            parts.append(make_clean_source(text) if self.m.fairness else text)
        self._n_funcs = len(src)
        # Also show the harness: an interesting loop/counter/mode there is fair
        # game to annotate, not only the library functions above.
        if self.has_harness:
            raw = self.harness.read_text(errors="replace")
            htext = make_clean_source(raw) if self.m.fairness else raw
            parts.append(
                f"{self._HBANNER} {self.m.d['harness']} ===== */\n"
                f"/* Everything above is localized LIBRARY source. You may also place\n"
                f"   the annotation here in the harness if the state you want to expose\n"
                f"   (a decode loop, an iteration counter, a mode flag) lives here. */\n"
                f"{htext}")
        self._render = "\n\n".join(parts)
        return self._render, hint

    def compact_harness(self, model_src: str) -> str:
        """Return model_src with the (full) harness section replaced by a
        comment-stripped one -- every executable line kept, comments/boilerplate
        dropped (see _strip_c_comments). Called once the agent has moved to the
        library, to stop re-sending the full harness each iteration. Safe even if
        called too early: no annotatable statement is ever removed."""
        idx = model_src.find(self._HBANNER)
        if idx < 0 or not self.has_harness:
            return model_src
        code = _strip_c_comments(model_src[idx:])         # also drops banner/intro comments
        note = (f"{self._HBANNER} {self.m.d['harness']} (comments stripped to save "
                f"context; every code line kept -- still annotatable) ===== */\n")
        return model_src[:idx] + note + code + "\n"

    def render_lines(self) -> int:
        return len(self._render.splitlines())

    def _find_file(self, anchor: str):
        """Library .c (or the harness) whose CURRENT (on-disk, accumulated) content
        holds `anchor`. Returns (file, exact_anchor_line) -- exact_anchor occurs
        verbatim so apply_annotation's match succeeds even if the model paraphrased
        spacing. The harness is searched too (it is annotatable in this mode)."""
        files = sorted(self.lib_src.glob("*.c"))
        if self.harness and self.harness.exists():
            files = files + [self.harness]                # harness is annotatable too
        for c in files:                                   # exact
            if anchor in c.read_text(errors="replace"):
                return c, anchor
        na = _norm(anchor)                                # whitespace-normalized
        for c in files:
            for line in c.read_text(errors="replace").splitlines():
                if na and na in _norm(line):
                    return c, line.strip()
        return None, None

    def apply_and_build(self, model_src: str, ann: Annotation) -> BuildOutcome:
        target_file, exact = self._find_file(ann.after_substring)
        if target_file is None:
            return BuildOutcome(False, note=(
                f"anchor not found in any library source file or the harness: "
                f"{ann.after_substring!r} -- copy an exact line from the shown source"))
        before = target_file.read_text(errors="replace")
        self._pristine.setdefault(target_file, before)    # snapshot first touch
        if "".join(ann.code.split()) in "".join(before.split()):
            return BuildOutcome(False, note="annotation already present in that file; find the NEXT roadblock")
        matched = next((ln for ln in before.splitlines() if exact in ln), exact)
        prob = _anchor_problem(matched)                   # cheap pre-build check
        if prob:
            return BuildOutcome(False, note=f"{prob} (anchored on `{matched.strip()[:80]}`)",
                                detail=prob, region=_region(before, exact))
        place = ann if exact == ann.after_substring else \
            Annotation(code=ann.code, after_substring=exact)
        try:
            patched = apply_annotation(before, place)     # build on current (accumulated) disk
        except ValueError as e:
            return BuildOutcome(False, note=f"could not place annotation: {e}")
        target_file.write_text(patched)
        undo = lambda: target_file.write_text(before)     # revert this file to pre-patch
        try:
            self.m.build("agent", extra_env={
                "IJON_OUT": str(self.m.path(self.m.d["targets"]["agent"]))})
        except RuntimeError as e:
            undo()
            return BuildOutcome(False, note=f"failed to build: {_build_err(e)}",
                                detail=str(e), region=_region(patched, ann.code.split('(')[0]),
                                target=target_file)
        return BuildOutcome(True, binary=self.m.path(self.m.d["targets"]["agent"]),
                            undo=undo, target=target_file)

    def cleanup(self):
        for f, original in self._pristine.items():        # leave the tree pristine
            f.write_text(original)


# --------------------------------------------------------------------------- #
#  Inner correctness loop: make the analyst's annotation BUILD                  #
# --------------------------------------------------------------------------- #
def build_with_repair(site, model, model_src: str, proposal, source_name: str,
                      localization, tries: int):
    """Try to apply+build the analyst's annotation; on failure, hand the error to
    the REPAIR agent (which preserves the macro/state) and retry, up to `tries`
    times. Returns (BuildOutcome, annotation_that_was_tried_last)."""
    cur = proposal
    out = site.apply_and_build(model_src, cur.annotation)
    attempt = 0
    while not out.ok and attempt < tries:
        attempt += 1
        print(f"      [repair {attempt}/{tries}] {out.note}")
        try:
            cur = repair_annotation(model, model_src, cur,
                                    out.detail or out.note, source_name=source_name,
                                    patched_region=out.region, localization=localization)
        except Exception as e:
            print(f"      [repair {attempt}] repair agent error: {str(e)[:120]}")
            break
        print(f"      [repair {attempt}] -> {cur.annotation.code}  "
              f"after {cur.annotation.after_substring!r}")
        out = site.apply_and_build(model_src, cur.annotation)
    if out.ok and attempt:
        print(f"      [repair] fixed after {attempt} attempt(s)")
    return out, cur.annotation


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--plateau-timeout", type=float, default=90)
    ap.add_argument("--eval-timeout", type=float, default=90)
    ap.add_argument("--model", default=None)
    ap.add_argument("--repair-model", default=None,
                    help="model for the build-repair agent (default: same as --model)")
    ap.add_argument("--build-repair-tries", type=int, default=3,
                    help="max compile-fix-recompile attempts per annotation (0 disables)")
    ap.add_argument("--manifest", default="target.json",
                    help="manifest filename within the workspace (default: target.json; "
                         "use e.g. target_diversity.json to run a variant that shares the "
                         "same clone/build/localize artifacts)")
    args = ap.parse_args()

    ws = (REPO / args.workspace).resolve()
    m = Manifest(ws, args.manifest)
    cfg = AflConfig(); cfg.check()
    seeds = m.path(m.d["seeds"])
    reward_kind = m.d.get("reward", "diversity")
    site = LibrarySite(m) if m.annotate == "library" else HarnessSite(m)

    # --- the only source the model sees -----------------------------------
    model_src, localization = site.model_source()
    where = "library source (localized)" if m.annotate == "library" else "harness"
    if m.fairness:
        gate = (f"fairness gate ON: {site.render_lines()} lines shown to model from "
                f"{where}; no 'ijon' token present (verified)")
    else:
        gate = (f"{site.render_lines()} lines of real {where} shown to model "
                f"(fairness gate OFF: existing annotations, if any, are kept visible)")
    print(f"[0] {gate}")

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

    # --- iterate ----------------------------------------------------------
    model = AnalystModel(args.model) if args.model else AnalystModel()
    repair_model = AnalystModel(args.repair_model) if args.repair_model else model
    print(f"    analyst={model.model}  repair={repair_model.model}  "
          f"build-repair-tries={args.build_repair_tries}")
    history, kept = [], []
    best_reward = base_reward
    harness_compacted = False     # retire (comment-strip) the harness once the agent moves to the library
    try:
        for it in range(1, args.iters + 1):
            banner(f"2.{it}) ANALYST proposes (prior failed: {len(history)})")
            print(f"    model: {model.model}")
            # the model sees the CURRENT (accumulated) source: its own kept
            # additions are fair to show -- any ground-truth answer was already
            # stripped if the fairness gate is on -- so it builds on prior
            # annotations and finds the NEXT roadblock.
            p = propose_annotation(model, model_src, snap,
                                   source_name=m.d["source_name"], history=history,
                                   localization=localization, reward_kind=reward_kind)
            print(f"    why_stuck      : {p.why_stuck}")
            print(f"    failure_class  : {p.failure_class}")
            print(f"    relevant_state : {p.relevant_state}")
            print(f"    {p.macro}: {p.annotation.code}")
            print(f"    after_substring: {p.annotation.after_substring!r}")

            if "".join(p.annotation.code.split()) in "".join(model_src.split()):
                note = "annotation already present; find the NEXT roadblock"
                print(f"    [REVERT] {note}"); history.append((p, note)); continue

            print(f"    placing annotation; building AFL+IJON ...")
            out, used_ann = build_with_repair(site, repair_model, model_src, p,
                                              m.d["source_name"], localization,
                                              args.build_repair_tries)
            if not out.ok:
                note = (f"could not build after {args.build_repair_tries} repair "
                        f"attempt(s): {out.note}")
                print(f"    [REVERT] {note}"); history.append((p, note)); continue
            agent_bin, undo = out.binary, out.undo

            snap2, q = fuzz(agent_bin, seeds, ws / "out" / f"rt_iter{it}", cfg, ws,
                            args.eval_timeout, stop_on_crash=True)
            if snap2 and snap2.solved:
                print(f"    [SOLVED] crash found ({snap2.saved_crashes}) — annotation reached the goal")
                kept.append((p.macro, used_ann.code)); break

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
                kept.append((p.macro, used_ann.code)); best_reward = r
                try:                          # show the kept annotation to the model
                    model_src = apply_annotation(model_src, used_ann)
                except ValueError:
                    pass  # paraphrased anchor not verbatim in the slice; history still guides
                if reward_kind == "coverage":
                    base_cov = after
            else:
                undo()
                note = (f"no reward gain (reward {reward_kind}: {best_reward} -> {r}); "
                        f"raw IJON edge growth does not count — try a different state/primitive")
                history.append((p, note))

            # Retire-after-resolved: once the agent has placed an annotation in a
            # LIBRARY file (kept or reverted), it has engaged the library, so stop
            # re-sending the full harness. The compacted harness keeps every code
            # line (only comments drop), so this is safe even if it fires early --
            # no future harness site is ever lost, and the agent can still annotate
            # the harness. Harness-targeted iterations leave it at full text.
            if (m.annotate == "library" and not harness_compacted
                    and site.has_harness and out.target is not None
                    and out.target != site.harness):
                model_src = site.compact_harness(model_src)
                harness_compacted = True
                print("    [harness retired: agent engaged the library — sending a "
                      "comment-stripped harness from now on (all code lines kept)]")
    finally:
        site.cleanup()

    banner("VERDICT")
    gain = (best_reward / base_reward) if base_reward else 0
    print(f"    reward ({reward_kind}): plain={base_reward} -> best={best_reward}  "
          f"({gain:.1f}x)")
    print(f"    kept {len(kept)} annotation(s): {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
