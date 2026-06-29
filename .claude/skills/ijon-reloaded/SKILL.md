---
name: ijon-reloaded
description: >
  Bring up and run IJON-Reloaded (an LLM as the IJON fuzzing analyst) on a new
  C/C++ library. Use this skill when the user wants to fuzz a library with IJON
  state-annotations on a target it has never seen: standing up a workspace from a
  cloned repo + a fuzz harness, getting an instrumented build to compile (the
  "build-doctor"), wiring the coverage/diversity reward and localization, and
  driving the diagnose -> annotate -> keep/revert loop. Trigger on: "fuzz <lib>
  with IJON", "set up an IJON target", "bring up <library> for IJON-Reloaded",
  "the build won't compile for the IJON harness", or running scripts/run_target.py
  / scripts/bringup.py.
---

# IJON-Reloaded: bring up & run on a new library

## START HERE (read first, don't spelunk)
On a fresh checkout there is nothing to "discover" by scanning — **this skill plus
the three committed example workspaces are the orientation.** Do NOT go hunting for a
`MEMORY.md`, a hidden config, or prior runs; there aren't any. Just:
1. Read this skill top-to-bottom once, then `docs/getting_started.md` once.
2. Open the closest committed template and **imitate it** — `workspace/libcoap/`
   (CMake, library mode, coverage **and** diversity manifests) is the canonical one;
   `workspace/libpng/` (autotools, coverage) and `workspace/libarchive/` (harness
   mode, diversity) are the other two.
3. Everything else (the cloned lib under `build/`, AFL output, coverage data) is
   gitignored and gets created by the loop — its absence on a fresh clone is normal.

This skill is **Mode 1**: *you* (Claude Code) are the agent — the build-doctor AND
the annotation analyst — using your own tools (Bash/Read/Write/Edit/WebFetch) and
reasoning. **No external API key is required.** (Mode 2, the standalone autonomous
deepseek/any-API agent in `scripts/run_target.py`, is for headless/CI runs and is
the research artifact; this skill can delegate to it but does not need it.)

Repo root for all paths below: the `ijon-llm/` checkout this skill lives in.

## The one thing to internalize
The human sets up **three things once** — (1) a harness that links the library and
decodes one input, (2) a `build.sh` so the loop can rebuild it, (3) a reward that
matches the roadblock. **You write the `IJON_*` annotations** — that is the whole
job; never ask the user to hand-write one. By default the annotation goes **inside
the library** (`--mode library`); the harness is shown too and is fair game.

Read `docs/getting_started.md` once for the full picture. The worked, committed
templates to imitate: `workspace/libcoap/` (CMake, library mode, both coverage +
diversity manifests), `workspace/libpng/` (autotools, library mode, coverage),
`workspace/libarchive/` (harness mode, diversity).

---

# Phase A — Bring-up (the build-doctor)

Goal: from *"a cloned library repo + a harness .c"* to *a green `build.sh plain`,
a working IJON `agent` build, the reward tool, and the localization inputs.* This
is the judgment-heavy part `scripts/bringup.py` (deterministic probes) cannot fully
do — you fill the `# TODO(verify)` slots and run the build-repair loop.

## A0. Environment (confirm before anything)
```bash
export AFL_ROOT=/path/to/AFLplusplus            # built WITH IJON
export LLVM_BIN=/path/to/llvm/bin               # clang + llvm-cov
export TMPDIR=/path/with/space                  # NEVER /tmp (too small)
export PATH="$AFL_ROOT:$PATH" AFL_PATH="$AFL_ROOT/include"   # AFL_PATH MUST be .../include
```
Verify `afl-clang-fast` is on PATH and `$AFL_PATH/afl-ijon-min.h` exists. On this
research machine the concrete values are in the `real-target-run-setup` memory.

## A1. Gather inputs
- **Library source**, cloned to a dir. Pin a tag.
- **A harness** — a `.c`/`.cc` that links the library and decodes one input from a
  byte buffer. Most libraries ship one under `contrib/oss-fuzz/`, `tests/fuzz/`,
  `fuzz/`. Find it; do not write one unless none exists.

## A2. The six decisions (what makes bring-up non-mechanical)
Reason these out by **reading the repo** (tree, build files, the harness `#include`s,
`./configure --help` or `CMakeLists.txt`). This is exactly the libcoap session:

1. **Build system + can it actually build here?** autotools / cmake / meson. CRITICAL:
   if it's autotools and `libtool`/`libtoolize` is **missing on the host**, the
   `./autogen.sh` path fails — **pivot to CMake** if the repo ships `CMakeLists.txt`
   (libcoap did exactly this). Check: `command -v libtool libtoolize`.
2. **Harness pick — the USER's call, not yours to silently make.** A harness can be
   either (a) an **OSS-Fuzz/libFuzzer harness** (`LLVMFuzzerTestOneInput`) or (b) a
   **utility the library ships** (a `.c` with its own `main` that reads a file/stdin —
   e.g. libarchive's `bsdtar`/`untar.c`, `xmllint`, `djpeg`). List the libFuzzer ones:
   `bringup.py --lib <src> --name <n> --list-harnesses` (finds every file defining the
   entrypoint; libcoap has 18). **Present the list and ask which one** unless the user
   named it. For a **utility**, point at it directly: `--harness examples/untar.c` (a
   path relative to the lib src, cwd, or absolute) — utilities are NOT in the libFuzzer
   list. The auto-pick is a WEAK name heuristic (libcoap's harnesses contain no "coap",
   so it guesses `async_target.c`); prefer explicit. bringup copies the chosen file
   into the workspace `src/`.
   **Harness KIND** (bringup auto-detects; `--harness-kind` overrides) decides build +
   how AFL feeds input — both must be right or it silently won't fuzz:
   - `libfuzzer` → built `-fsanitize=fuzzer`; AFL persistent (no file arg).
   - `argv` (utility w/ `main` reading a file) → built WITHOUT `-fsanitize=fuzzer`; AFL
     feeds a file via `@@` (manifest `"input":"argv"`, `"target_args":["@@"]` — edit to
     e.g. `["-f","@@"]` if the tool needs a flag, like libarchive's `archivetest -f @@`).
   - `stdin` (utility reading stdin) → no `-fsanitize=fuzzer`; AFL feeds stdin.
   ASAN stays ON for EVERY kind (and must match the library — all-or-nothing).
3. **Mode.** `library` (default — annotation inside the lib; needs localization) vs
   `harness` (only when the harness itself drives the decode loop and state is
   reachable via the public API, e.g. libarchive).
4. **Reward = failure class.** `coverage` (class 3, reach new code) vs `diversity`
   (class 2, reorder/sequence already-covered code). HONEST CAVEAT: the coverage
   *frontier* localization biases toward class-3, but many real wins are class-2 and
   coverage reward is **blind** to them (it sees "0 new functions" and reverts a good
   annotation). If the target is a parser/state machine (options, records, commands),
   plan a `describe` tool + `diversity` reward (see B + the libcoap example).
5. **Dep pins for a deterministic link.** Disable ALL optional features so the link is
   dep-free and reproducible on any host: TLS backends, tests, examples, docs
   (libcoap: `-DENABLE_DTLS=OFF -DENABLE_OSCORE=OFF -DENABLE_TESTS=OFF
   -DENABLE_EXAMPLES=OFF -DENABLE_DOCS=OFF -DBUILD_SHARED_LIBS=OFF`; autotools:
   `--without-<dep> --disable-<feature>`).
6. **Include paths.** Generated config headers usually land in the *build* dir
   (`coap_config.h`, `coap_defines.h` via `configure_file`/autoheader) AND the harness
   often needs *internal* headers. Add `-I<builddir> -I<builddir>/include -I<src>/include`.

## A3. Generate the scaffold
```bash
.venv/bin/python scripts/bringup.py --lib /path/to/libxyz --name libxyz \
    --mode library --reward coverage --repo-url <url> --repo-tag <tag>
```
This **creates `workspace/libxyz/`** with `build.sh.draft` + `target.json.draft`.
`bringup.py` handles **autotools well**; for **CMake/meson** it emits a skeleton — in
that case hand-write `build.sh` by imitating `workspace/libcoap/build.sh` (the proven
CMake template: `cmake -G Ninja ... -DCMAKE_C_COMPILER=afl-clang-fast`, a `plain`,
`describe`, and `agent` variant; `static_lib()` finds `lib*.a`). Drop the `.draft`
suffixes once filled.

## A4. The build-repair loop (you are the build-doctor)
*(Mode 2 alternative: with an API key, `scripts/build_doctor.py --workspace
workspace/libxyz` runs this loop autonomously — build → read error → edit build.sh
→ rebuild, bounded, guarding the IJON wiring. In Mode 1 you do it yourself:)*
Iterate to a green build, bounded — do not thrash:
```bash
bash workspace/libxyz/build.sh plain      # read the FIRST real error, fix one slot, repeat
```
Fix the specific failing slot (a missing `-I`, a wrong dep, a configure flag). The
**invariant gotchas** (bake these in, they cause silent/confusing failures):
- **AFL_PATH silent failure.** If `AFL_PATH != $AFL_ROOT/include`, the `IJON_*`
  macros compile to *nothing* and the IJON binary is byte-identical to plain. Always
  check `md5sum plain agent` differ.
- **ASAN is all-or-nothing.** If the library is built `-fsanitize=address`, EVERY
  binary linking it (harness, describe, cov) must also pass `-fsanitize=address`, or
  you get `undefined reference to __asan_report_*`.
- **Build the library once, relink per variant** (library mode: `make`/`ninja`
  recompiles only the changed file).
- **AFL needs `ASAN_OPTIONS=...:abort_on_error=1`** or `afl-fuzz` aborts at startup
  ("Custom ASAN_OPTIONS set without abort_on_error=1").

## A5. Sanity checks (do all four before trusting a run)
1. **Functional:** `./targets/<x>_plain in/<seed>` decodes ("Execution successful").
2. **IJON really instruments:** temporarily insert one `IJON_STATE(...)` in a library
   `.c`, build `agent` non-quiet, expect `Instrumented N IJON calls for tracking`;
   `md5sum` of plain vs agent must differ. Then restore the file.
3. **AFL can drive it:** a ~100s `afl-fuzz` reaches a healthy `execs_per_sec` and grows
   the corpus (libcoap: ~57k/s). 
4. Leave the tree pristine afterward.

## A6. Localization inputs (library mode only)
The frontier localizer needs two files named in `target.json`'s `localize` block:
- **`build/cov/coverage.json`** — `bash workspace/libxyz/cov-build.sh` (clean clang
  `-fprofile-instr-generate -fcoverage-mapping`, libFuzzer driver), then replay the
  plain corpus: `llvm-cov export ... > coverage.json`. Imitate `workspace/libcoap/cov-build.sh`.
- **`fi_out/*.data.yaml`** — fuzz-introspector static graph. Use the standalone
  tree-sitter frontend (see the `fuzz-introspector-setup` memory). **GOTCHA:** FI
  skips any path containing `build` (its `EXCLUDE_DIRECTORIES`); stage sources into a
  `fi_proj/` dir with no "build" in the path.
Validate: load both via `harness/localize.py: build_localization_context` and confirm
the frontier slices *sensible* functions (libcoap → `coap_pdu_parse_opt`, the option
loop). If it slices junk, the localization is wrong — fix before running.

## A7. Wire & dry-run
Fill `target.json` (imitate `workspace/libcoap/target.json`). Confirm via a dry load
that `annotate`, `reward`, all `build`/`targets` paths, and `localize` resolve.

---

# Phase B — Run & analyze the loop

In Mode 1 **you are the analyst** (default — no key needed). Drive the loop with
`scripts/analyst_cli.py`, which does the mechanical steps **identically to
`run_target.py`** (same plateau, same reward) so you only supply the *brain* — the
annotation edit. You do NOT hand-roll llvm-cov replay or sequence counting; the CLI
reuses run_target's helpers.

```bash
# 1. baseline: build plain + reward tool, fuzz the control, record baseline
.venv/bin/python scripts/analyst_cli.py plateau --workspace workspace/libxyz \
    [--manifest target_diversity.json] --plateau-timeout 100
# 2. read what to annotate (localization frontier + the localized source + harness)
.venv/bin/python scripts/analyst_cli.py context --workspace workspace/libxyz [--manifest ...]
# 3. >>> YOU: Edit ONE IJON_* annotation into the matching library .c (or harness) <<<
# 4. evaluate: build the IJON agent, fuzz the eval window, KEEP/REVERT vs baseline
.venv/bin/python scripts/analyst_cli.py eval --workspace workspace/libxyz [--manifest ...] \
    --eval-timeout 200 --note "IJON_STATE(...)"
#    KEEP   -> leave your edit; go to step 2 for the NEXT roadblock (context now
#              shows your kept annotation, so you build on it).
#    REVERT -> undo your edit (Edit it back out / `git checkout` the file) and try a
#              different state/primitive. If the build FAILED, fix placement/syntax
#              (you are also the repair agent) and re-run `eval`.
```
You own the source edits, so you manage keep/revert by editing files (the CLI never
touches sources). Leave the tree pristine when done.

**Delegate to Mode 2** instead (only if the user provides a deepseek/API key and
wants the fully autonomous agent): `.venv/bin/python -u scripts/run_target.py
--workspace workspace/libxyz --iters 3 --plateau-timeout 100 --eval-timeout 200
[--manifest target_diversity.json] 2>&1 | tee run.log`.

## Analyst craft (when you write the annotation)
Read `harness/agent.py`'s `_SYSTEM` + `IJON_REFERENCE` for the primitives and the
failure taxonomy. The decisive judgments:

- **Match the primitive to the reward** (this is what the reward-aware prompt encodes):
  - reward = **coverage** → expose the *gating value* (magic tag, length) guarding an
    uncovered branch: `IJON_CMP(x, TARGET)` / `IJON_MAX(x)` for a gradient.
  - reward = **diversity** → expose *ordering/sequence/repetition* of already-covered
    code: `IJON_STATE(...)` on a running/rolling value, or `IJON_SET` per iteration.
    A pure reach-new-branch annotation will NOT move a diversity reward.
- **Beware map saturation (cardinality).** `IJON_STATE(x)` mixes `x` into the edge
  hash, so it can spray up to (#distinct x) × (#edges) entries into AFL's fixed 64KB
  bitmap. Too-high-cardinality state (raw byte offsets, pointers, `hash(wide,wide)`)
  **floods the map**: corpus explodes, hash collisions mask real coverage, fuzzing
  budget dilutes — and you get *less* diversity, not more (we saw 822→28014 files for
  *fewer* distinct sequences). Prefer **bounded** state: enums, small counters, option
  numbers, a sequence hash truncated to a few bits. If a kept annotation bloats the
  corpus with little reward gain, that is saturation — bound the state.
- **Anchor on an executable statement line** (ends in `;` or `{`), never a
  preprocessor/declaration/comment line, and where the state variable is live.
- **Honesty:** keep/revert is the truth. A conceptually-richer annotation that does
  not beat the simpler one in-window should revert. Report flat results as flat.

## Reward-axis honesty
If a coverage run keeps nothing and the target is stateful, it is very likely a
class-2 target measured on the wrong axis (this is the libcoap coverage→diversity
story). Switch to a `describe` tool (decode each input like the harness, print one
line = its state-token sequence; same contract as
`workspace/libarchive/src/archive_describe.c` and `workspace/libcoap/src/coap_pdu_describe.c`)
and `reward: diversity`. The loop-window multiplier UNDERSTATES the mechanism; the
honest magnitude comes from a deterministic full-budget A/B (Part B of getting_started).

---

# Phase C — long bug-hunting campaign (adaptive, optional)

Phase B *finds* good annotations; a **campaign** runs long to *hunt crashes*, and
re-annotates whenever the fuzzer stalls — automating the human analyst who watches a
campaign and adds an annotation when it gets stuck. Three ways, by what the user wants:

- **Static** (simplest): build the agent on the kept annotation, then one long
  `afl-fuzz -V <seconds>` (8 h = `-V 28800`). No re-annotation. Good when they just
  want to let the kept annotation run.
- **Adaptive, you drive it (Mode 1, no key)** — the loop below. `scripts/campaign_cli.py`
  owns the AFL process *and* the mechanics (`start-round`/`poll`/`stop-round` launch,
  observe, and stop the fuzzer for you); you only supply the analyst decision — when to
  stop and what one annotation to add. Don't hand-roll `afl-fuzz`.
- **Adaptive, autonomous (Mode 2, needs key)** — `scripts/campaign_supervisor.py`
  runs the identical loop unattended via the API. Prefer this for a *truly unattended*
  multi-hour run (a daemon survives better than a held-open session); launch it (or
  `nohup` it) and just report progress.

> **FUZZER DISCIPLINE — the running fuzzer is observe-only.** A live `afl-fuzz` must
> NOT be interrupted to "check on it." Never Ctrl-C it, never attach, never restart it
> to inspect — every interruption throws away in-flight progress and corrupts your
> read of the trend. **Observe only by reading files** (`fuzzer_stats`, `plot_data`)
> while it keeps running. The fuzzer is stopped **exactly once per round**, on purpose,
> for a re-annotation cycle (step 4) — and you stop it cleanly (SIGINT, let it flush
> `fuzzer_stats`), never SIGKILL. Between rounds, poll and wait; do not fidget.

**The Mode-1 adaptive loop you drive** (you decide *when/what*; the CLI owns the
process + mechanics via `start-round`/`poll`/`stop-round`):
1. **Seed + first round.** `campaign_cli.py seed --code … --after …` (the discovery
   keep) builds the agent. Then launch the round — **do not type `afl-fuzz` yourself**;
   `start-round` owns the env, the fresh `-o`, the reseed `-i`, and detaches it so it
   survives between your turns:
   ```bash
   campaign_cli.py start-round --workspace workspace/<t>
   ```
2. **Poll** (observe-only — never Ctrl-C the fuzzer to peek):
   ```bash
   campaign_cli.py poll --workspace workspace/<t>
   ```
   It prints `edges_found`, `time_wo_finds`, `bitmap_cvg`, `saved_crashes`, and flags a
   **STALL** for you. Poll between turns; let it run.
3. **Each round, collect crashes:** `campaign_cli.py collect-crashes --round
   workspace/<t>/campaign/round_N` (dedups into the central `campaign/crashes/`).
4. **On stall** (poll says so): `campaign_cli.py stop-round` (clean SIGINT + flush),
   then `campaign_cli.py localize --queue …/round_N/default/queue` → read the new blocker +
   localized source → **decide exactly ONE annotation** → apply the *transaction*:
   ```bash
   campaign_cli.py apply --code "IJON_…;" --after "<exact live line>" \
       --edges <current edges_found> --bitmap-cvg <current bitmap_cvg>
   ```
   This keeps/reverts the last annotation, **retires the oldest under map pressure**
   (your banked-corpus insight — gains persist in the queue), adds the new one, and
   **recompiles once**.
   > **The map has a budget — annotations are one-in/one-out, never additive.** AFL's
   > bitmap is a fixed 64 KB and `IJON_STATE` multiplies its footprint, so you do NOT
   > stack annotations. The rule is **one annotation per round**, applied through the
   > transaction above — which is why `apply` takes a single `--code`. The active set is
   > bounded (`--max-active`, default 6); when it's full or the map is hot
   > (`bitmap_cvg` high), the *same* `apply` evicts the oldest before adding the new one,
   > in a single recompile. So "the fuzzer is stuck, add more state" is answered by
   > **swapping** a fresh annotation in for a mined-out one — never by piling several on
   > at once (that is exactly what floods the map and makes diversity *worse*). If you
   > ever feel the urge to add two in one round, add one, run a round, then add the next.
5. **Resume** = `campaign_cli.py start-round` again — it auto-reseeds `round_{N+1}`
   from `round_N/default/queue` (robust under the recompiled binary) and assigns a
   fresh `-o`. Repeat from step 2.
6. **End:** `campaign_cli.py finalize` restores the source tree + writes
   `campaign/summary.json`. Crashes are in `campaign/crashes/`.
7. **Triage the crashes:** `scripts/triage_crashes.py --workspace workspace/<t>`
   replays each crash through the plain ASAN target and buckets them by
   (crash-type, top stack frames) into the few DISTINCT bugs — `campaign/crashes/`
   holds many inputs but usually far fewer real bugs. Writes
   `campaign/triage_report.md` (each bug: faulting `func@file:line`, count,
   representative input); add `--minimize` to afl-tmin each representative. Report
   the distinct bugs, not the raw crash count.

Caveat: a CC-driven campaign keeps the *session* alive across the run (you act in
turns, polling between them) — great for an **attended** run you watch/steer; for an
unattended 8 h+ run, prefer the Mode-2 supervisor. The map-saturation lever (retire
under pressure) is what makes a *multi-round* campaign sustainable — don't stack
annotations without it.

---

# Gotchas quick-reference
- `AFL_PATH=$AFL_ROOT/include` or IJON silently no-ops (check md5 plain≠agent).
- `AFL_LLVM_IJON=1` enables the pass; **unset it** for plain/cov/describe builds.
- ASAN all-or-nothing across every binary linking the lib.
- `ASAN_OPTIONS=...:abort_on_error=1` for `afl-fuzz`.
- Never write to `/tmp`; set `TMPDIR`.
- fuzz-introspector skips any path containing `build`; stage in `fi_proj/`.
- Pin/disable all optional deps for a deterministic link.
- Leave the library tree pristine (the loop snapshots+restores; if you edit by hand,
  restore or `git checkout`).
- A running `afl-fuzz` is observe-only: read `fuzzer_stats`/`plot_data`, never Ctrl-C
  to peek; stop it cleanly (SIGINT) exactly once per round for re-annotation.
- One annotation per round; the map is a fixed budget — swap (retire oldest + add),
  never stack several at once.
