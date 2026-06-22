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
2. **Harness pick (if several).** Choose by *which exercises the stateful decode you
   care about*, NOT alphabetically. `bringup.py` falls back to the first file when no
   filename matches the lib name — that is usually wrong (libcoap has 18 harnesses,
   none named "coap"). Read candidates; pick the single-message/decode one to start
   (e.g. `pdu_parse_udp`, not `async`).
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
