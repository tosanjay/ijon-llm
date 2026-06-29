# IJON Reloaded — an LLM as the IJON fuzzing analyst

> Using an LLM to *usher fuzzers through the maze.*

**[IJON](https://github.com/RUB-SysSec/ijon)** (Aschermann et al., IEEE S&P 2020)
lets a **human analyst** break fuzzing roadblocks by adding a tiny source
annotation that reshapes AFL's feedback function — exposing program **state** that
edge coverage is blind to. With one line, AFL solves mazes, plays Super Mario,
cracks checksums, and beats CGC challenges that defeat every automated tool. The
catch: it needs the human.

**This project replaces the human analyst with an LLM agent.** An autonomous loop
runs an AFL++ campaign, detects when it plateaus, asks DeepSeek *why* it is stuck
and *what* annotation to add, applies it, rebuilds, re-runs, and keeps or reverts
on the measured result — accumulating annotations until the goal is reached. The
LLM is used **only at the two judgment points** (diagnose why-stuck, synthesize
the annotation); everything mechanical — running the fuzzer, plateau detection,
patching, rebuilding, evaluation — is deterministic Python.

<p align="center">
  <img src="experiments/mario/mario_playthrough.gif" width="520"
       alt="AFL+IJON playing Super Mario, driven by the agent's blind IJON_MAX(world_pos) annotation; the world_pos reward climbs as Mario advances"/><br>
  <sub><b>The fuzzer is playing.</b> A fuzzer-found Super Mario playthrough driven by an annotation
  the agent proposed <i>blind</i> — rendered ROM-free, the live <code>world_pos</code> is the reward it maximizes.</sub>
</p>

📖 **Full write-up:** [`docs/writeup/`](docs/writeup/) — a single-file
[standalone HTML page](docs/writeup/ijon-llm-writeup.standalone.html) (host on
GitHub Pages for the live version).

## Results

The agent always sees only the **answer-stripped** source (a fairness gate removes
the ground-truth annotation and every `ijon` mention) plus plateau telemetry, and
re-derives a working annotation by reasoning.

| Target | Roadblock class (agent's diagnosis) | What happened |
|---|---|---|
| **maze** | known relevant state values · `IJON_SET` | blind, **exact match** to the paper's own annotation; plain AFL stuck @16 edges, agent solves in 8 s |
| **checksum / two-gate** | missing intermediate state · `IJON_CMP` | plain: 16.3M execs, 0 solves → agent solves; two-gate solved by the loop **accumulating two annotations** |
| **maxclimb** | known relevant state values · `IJON_MAX` | plain: 0 crashes / 16.8M execs → solved |
| **libpng** (real lib) | known state changes · `IJON_STATE` | **41×** more distinct chunk-type *sequences* explored than plain AFL (over wall-clock) |
| **libpng — autonomy** | known state changes · `IJON_STATE` | the loop, rewarded on state-diversity, **autonomously reaches** `IJON_STATE(hash(mode,chunk_name))` — **30.9×** |
| **Super Mario** *(demo)* | known relevant state values · `IJON_MAX` | blind, reproduces the paper's flagship `IJON_MAX(world_pos)`; that annotation drives a real playthrough (GIF above) — shown as a demo, not an effectiveness benchmark |
| **libtpms (vTPM)** | known state changes · `IJON_STATE` | 12 h real-target deployment, two autonomous annotations → **11.5×*** more command sequences than plain |
| **libarchive** (real lib) | known state changes · `IJON_STATE` | the [getting-started](docs/getting_started.md) target: agent blind-diagnoses class 2 and re-derives the entry-type state annotation; reference A/B → **up to 233×** more distinct format/entry *sequences* (budget-dependent) |
| **libcoap** (real lib) | known state changes · `IJON_STATE` | 5th real target + the Mode-1 bring-up worked example; the *reward-aware* analyst blind-picks class-2 `IJON_STATE` on the CoAP option sequence (a coverage reward kept nothing — wrong axis), in-loop **145 → 189** distinct option sequences (full A/B pending) |

<sub>\* **A conservative number with known headroom.** The class-3 annotation
landed semantically in the library (`IJON_SET(command.index)` in `ExecCommand.c`),
but the class-2 *sequence* annotation fell back to a raw-byte prefix hash in the
harness rather than the command-index sequence at the library's dispatch loop —
because v1's localizer is coverage-*frontier* oriented and has no class-2
loop-localization yet (see the [v2.0 roadmap](docs/architecture-design.md#roadmap--what-v20-will-add)).
A sharper, semantic class-2 site should only raise this gap.</sub>

Reproducible records for the newer experiments live in
[`experiments/`](experiments/); design rationale and dead-ends in
[`docs/architecture-design.md`](docs/architecture-design.md).

### The unifying finding

**IJON helps when the relevant state is *invisible* to edge coverage** — and the
gain scales with *how* invisible it is. Where state is genuinely hidden — chunk
*sequences*, a maximization score, command *orderings* — the agent's annotation
wins, and the win *compounds* with time (on libtpms the gap grew from 6× to 11.5×
as plain AFL flat-lined). The two real targets even trace the gradient: libpng's
chunk sequences run on shared code (nearly invisible → **41×**), while libtpms's
command *type* partly leaks into coverage via per-command handlers (partly visible
→ **11.5×**). So the analyst's real skill isn't picking the primitive; it's
choosing a state edge-coverage doesn't already capture — which the loop learns to
do.

## How it works

A deterministic loop with the model at two judgment points; the agent is a **single
reasoning call** on pre-localized source — it does not explore the codebase or
generate inputs. Its only lever is the **feedback function**.

```
fuzz → plateau → [localize] → [LLM: diagnose + annotate] → patch+rebuild → keep/revert
```

- **Fairness gate** — each benchmark ships the ground-truth annotation behind
  `#ifdef`; before the agent sees the source we strip it, redact every `ijon`
  line, and hard-assert nothing leaks. The model knows the IJON *API/taxonomy*
  (from its prompt), never the specific answer.
- **Source-coverage keep/revert** — decisions use *real* source coverage (replay
  through an llvm-cov build), never raw `edges_found` (IJON map entries inflate it
  and would fake "progress").
- **Localizer** — fuzz-introspector static call-graph × llvm-cov coverage →
  the frontier the fuzzer is stuck at, to point the model at the right code.

## Layout

```
harness/        deterministic harness + the LLM roles (stdlib + LiteLLM)
  config.py · fuzzer.py · plateau.py · build.py · model.py · agent.py (analyst + repair) ·
  build_doctor.py (standalone build-doctor) · loop.py · coverage.py · localize.py
scripts/        reproduce_m1, solve_target_llm, autonomous, run_target (generic real-lib loop, Mode 2),
                analyst_cli (CC-as-analyst loop, Mode 1), build_doctor (standalone build-doctor, Mode 2),
                bringup (draft build.sh + target.json + harness discovery for a new lib),
                campaign_supervisor (adaptive long campaign, Mode 2) + campaign_cli (CC-driven, Mode 1),
                triage_crashes (bucket campaign crashes into distinct bugs),
                annotation_comparison, libpng_{loop,convergence,autonomy}, mario_{annotation,video}
.claude/skills/ijon-reloaded/   the Claude Code skill (Mode 1 — CC as build-doctor + analyst)
experiments/    reproducible records: human_vs_llm, libpng_convergence, libpng_autonomy, mario, libtpms, libarchive
workspace/<t>/  per-target: src/ (harness), seeds, build.sh, target.json (libcoap, libpng, libarchive, …)
docs/           getting_started.md + architecture-design.md + the HTML write-up (writeup/)
tests/          unittest suite for the deterministic logic
```

## Setup

- **AFL++ with IJON** (`AFL_LLVM_IJON=1` support) — point `AFL_ROOT` at it (or edit
  `harness/config.py`).
- **LLVM** with `llvm-cov`/`clang` for source-coverage builds — `LLVM_BIN`.
- A **DeepSeek API key** in `DEEPSEEK_API_KEY` (env or `.env`; see `.env.example`) —
  for **Mode 2 / standalone** runs. **Mode 1 (the Claude Code skill) needs no extra
  key** — it uses Claude Code's own model.
- Python venv with LiteLLM: `python3 -m venv .venv && .venv/bin/pip install litellm`.

```bash
export AFL_ROOT=/path/to/AFLplusplus          # built with IJON
export LLVM_BIN=/path/to/llvm/bin             # clang, llvm-cov (for coverage builds)
export TMPDIR=/path/with/space                # scratch for builds/corpora (optional)
```

## Run

**The bundled benchmarks** — self-contained single-file targets (compiled with
`-fsanitize=fuzzer`); the turnkey path:

```bash
.venv/bin/python scripts/reproduce_m1.py                                   # deterministic A/B (no LLM)
.venv/bin/python scripts/solve_target_llm.py --workspace workspace/checksum --src checksum-guard.c
.venv/bin/python scripts/autonomous.py --workspace workspace/twogate --src twogate.c --max-iters 5
.venv/bin/python scripts/annotation_comparison.py                          # blind human-vs-LLM on IJON's benchmarks
.venv/bin/python -m unittest discover -s tests -v                          # tests
```

Model defaults to `deepseek/deepseek-v4-pro`; override with `--model` or `IJON_LLM_MODEL`.

### Run on your own library

You cloned `libxyz` — now what? You need a harness that links it, a `build.sh`, and
a class-matched reward. There are **two ways to drive it**:

- **Mode 1 — inside Claude Code (no extra API key).** The
  [`ijon-reloaded` skill](.claude/skills/ijon-reloaded/SKILL.md) turns Claude Code
  *itself* into the agent — it brings up the build, picks the reward, and acts as the
  analyst, using its own tools. Open the repo in Claude Code and ask it to fuzz your
  library (or run `/ijon-reloaded`) — start through the skill rather than scanning the
  tree; the skill + the three committed `workspace/` examples are the whole map. Uses
  CC's model; needs **no DeepSeek key**.
- **Mode 2 — standalone (autonomous, headless).** The Python agents run the whole
  thing via the DeepSeek API — the research artifact and the CI / no-CC path.

Both ride the same deterministic core:

1. **Bring-up.** [`scripts/bringup.py`](scripts/bringup.py) probes the source (build
   system, harnesses, `configure`/cmake flags, `*.pc.in` deps) and emits a draft
   `build.sh` + `target.json` with the AFL/IJON/ASAN scaffolding already correct.
   You fill the `TODO(verify)` slots — or, in Mode 2,
   [`scripts/build_doctor.py`](scripts/build_doctor.py) (a third *build-doctor* agent)
   reads the compiler/linker errors and fixes `build.sh` in a bounded loop.
   The **harness** can be an OSS-Fuzz/libFuzzer harness *or* a utility the library
   ships (`bsdtar`, `xmllint`): `bringup.py --list-harnesses` lists candidates,
   `--harness <path-or-substring>` picks one, and its *kind* (libFuzzer / argv-`@@` /
   stdin) sets the build and how AFL feeds input.
2. **Loop.** [`scripts/run_target.py`](scripts/run_target.py) (Mode 2) or
   [`scripts/analyst_cli.py`](scripts/analyst_cli.py) (Mode 1, CC-as-analyst) drives:
   fuzz to plateau → localize → annotate → rebuild → keep/revert.

The annotation goes **inside the library by default** (`"annotate":"library"`; the
harness is shown too and is fair game) — localized via the FI ∩ llvm-cov frontier.
Match the **reward** to the roadblock: `"reward":"coverage"` (reach new code) or
`"reward":"diversity"` (reorder/sequence already-covered code, via a small `describe`
tool); a `--manifest` flag lets a diversity variant share one workspace.

```bash
.venv/bin/python scripts/run_target.py --workspace workspace/libcoap --iters 3   # Mode 2
```

Because the agent edits real C, a **two-role** design keeps it honest: the *analyst*
chooses the annotation; a separate *repair* agent fixes compiler/placement errors
(preserving the analyst's macro + state) so only buildable annotations reach
keep/revert. The fairness gate (hide a planted answer) is **opt-in**
(`"fairness_gate":true`) — off by default so the agent sees your real source.

📘 **[`docs/getting_started.md`](docs/getting_started.md)** is the concrete,
command-by-command walkthrough. **You never hand-write an IJON annotation — the agent
does that.** Worked templates: [`workspace/libcoap`](workspace/libcoap) (CMake,
library mode, coverage + diversity), [`workspace/libpng`](workspace/libpng)
(autotools, coverage), [`workspace/libarchive`](workspace/libarchive) (harness mode,
diversity). The harness + `build.sh` are the per-library jobs; the
localize/diagnose/annotate/repair/keep-revert loop is the reusable part.

**Beyond discovery — bug-hunting.** A long **campaign** runs the kept annotation for
hours and, on each stall, re-annotates (retiring a mined-out annotation under map
pressure, since its gains are banked in the corpus) — autonomous via
[`campaign_supervisor.py`](scripts/campaign_supervisor.py) (Mode 2) or driven by Claude
Code (Mode 1). [`triage_crashes.py`](scripts/triage_crashes.py) then buckets the
campaign's crashes into the few distinct bugs. Both work the same in either mode — see
getting_started **Part D**.

## Honest negatives (reported, not hidden)

- **libpng bug-hunt** — clean miss; recent libpng CVEs are *format-gated*, not
  CRC-gated (coverage data refuted our CRC hypothesis).
- **libtpms** — 12 h, 0 crashes (no new bug on an OSS-Fuzz-hardened target); the
  win is the deployment + the 11.5× state expansion.
- **Class-2 crash auto-solve** — no 1-D synthetic target both defeats plain AFL
  *and* is IJON-climbable; the paper measures class 2 by sequence diversity, which
  we demonstrate instead.

## Acknowledgments & provenance

This project automates the analyst role of **IJON** (Cornelius Aschermann, Sergej
Schumilo, Ali Abbasi, Thorsten Holz — *IJON: Exploring Deep State Spaces via
Fuzzing*, [IEEE S&P 2020](https://nyx-fuzz.com/papers/ijon.pdf); reference
implementation: [RUB-SysSec/ijon](https://github.com/RUB-SysSec/ijon)) with an
LLM. It **builds directly on the IJON technique** and would not exist without it.

- **The fuzzing engine is external [AFL++](https://github.com/AFLplusplus/AFLplusplus)**,
  which has upstreamed IJON's annotation macros + runtime (`afl-ijon-min.h`, the
  IJON instrumentation pass, `AFL_LLVM_IJON`). We do **not** vendor any AFL/IJON
  engine source — the agent writes annotations and builds targets against your
  AFL++ install at `$AFL_ROOT`. (Earlier revisions carried the original IJON
  AFL-2.x tree from the fork; it was unused at runtime and has been removed.)
- **IJON-derived benchmark targets we reuse, with credit:** the maze
  (`workspace/maze/`) and Super Mario (`workspace/mario/`) are ported from the
  authors' [RUB-SysSec/ijon-data](https://github.com/RUB-SysSec/ijon-data) — ©
  the IJON authors, Apache-2.0 (see file headers + [`LICENSE`](LICENSE) and the
  per-target `experiments/*/REPORT.md`).
- **Original to this project (the LLM-analyst agent):** `harness/`, `scripts/`,
  `experiments/`, the harnesses in `workspace/<target>/src`, `tests/`,
  `docs/architecture-design.md` and `docs/writeup/`, and this README.

Also built on [fuzz-introspector](https://github.com/ossf/fuzz-introspector) and
[libtpms](https://github.com/stefanberger/libtpms).

## Citation

If you use this work, please **cite this repository** (below). Because it builds
directly on IJON, please **also cite the original IJON paper**.

**This repository** — BibTeX:

```bibtex
@software{rawat2026ijonreloaded,
  author = {Rawat, Sanjay},
  title  = {{IJON Reloaded}: Using an {LLM} to Usher Fuzzers Through the Maze},
  year   = {2026},
  url    = {https://github.com/tosanjay/ijon-llm},
  note   = {Automates the IJON analyst role with an LLM; builds on IJON
            (Aschermann et al., IEEE S\&P 2020)}
}
```

ACM Reference Format:

> Sanjay Rawat. 2026. IJON Reloaded: Using an LLM to Usher Fuzzers Through the Maze. Retrieved June 17, 2026 from https://github.com/tosanjay/ijon-llm

**The IJON paper it builds on** — BibTeX:

```bibtex
@inproceedings{aschermann2020ijon,
  author    = {Aschermann, Cornelius and Schumilo, Sergej and Abbasi, Ali and Holz, Thorsten},
  title     = {{IJON}: Exploring Deep State Spaces via Fuzzing},
  booktitle = {2020 IEEE Symposium on Security and Privacy (SP)},
  year      = {2020},
  pages     = {1597--1612},
  doi       = {10.1109/SP40000.2020.00117}
}
```

ACM Reference Format:

> Cornelius Aschermann, Sergej Schumilo, Ali Abbasi, and Thorsten Holz. 2020. IJON: Exploring Deep State Spaces via Fuzzing. In 2020 IEEE Symposium on Security and Privacy (SP). IEEE, 1597–1612. https://doi.org/10.1109/SP40000.2020.00117

— *Sanjay Rawat*
