# Getting started — running IJON-Reloaded on a new library

This is the concrete recipe for pointing the agent at a library it has never seen.
**Part A** is the path you follow to fuzz your own `libxyz` — commands first, prose
out of the way. **Part B** is the optional A/B harness we use to *measure* the agent
(skip it unless you're reproducing our numbers). **Part C** explains *why* each step
is shaped the way it is.

Two worked examples ship with the repo and are used throughout: **libarchive 3.8.6**
in *harness mode* ([`workspace/libarchive/`](../workspace/libarchive)) and **libpng**
in *library mode* ([`workspace/libpng/`](../workspace/libpng)) — the more common case,
where the annotation goes *inside* the library. Follow Part A verbatim to reproduce
either, or use them as templates for your own target.

> **The one thing to internalize.** The agent does not explore your codebase or
> write inputs, and **you do not hand-write the IJON annotation** — that is the
> agent's whole job. "Running on a new target" is three things *you* set up once —
> (1) an instrumented harness exposing a clear loop/decision/state site (in the
> library code for most targets, or in the harness when it drives the decode loop —
> see A2), (2) a `build.sh` so
> the loop can rebuild it, (3) a reward that matches the roadblock — and then the
> loop does the diagnose → annotate → keep/revert part.

---

# Part A — Run it on your library

## A0. Prerequisites

| Need | What we used | Export |
|---|---|---|
| AFL++ **with IJON** | AFL++ 4.41a (IJON macros + pass compiled in) | `AFL_ROOT=/path/to/AFLplusplus` |
| LLVM (clang + llvm-cov) | clang 18 | `LLVM_BIN=/path/to/llvm/bin` |
| Python venv + LiteLLM | `.venv` | — |
| DeepSeek API key | `deepseek-v4-pro` | `DEEPSEEK_API_KEY=…` (or `.env`) |
| Scratch space | not `/tmp` (too small) | `TMPDIR=/your/space` |

```bash
export AFL_ROOT=/home/you/AFLplusplus        # built with IJON
export LLVM_BIN=/home/you/llvm/bin
export TMPDIR=/home/you/scratch
export PATH="$AFL_ROOT:$PATH" AFL_PATH="$AFL_ROOT/include"   # AFL_PATH must be …/include — see C1
```

## A1. Get the target source

```bash
git clone --depth 1 -b v3.8.6 https://github.com/libarchive/libarchive.git
```

`build.sh` can clone this itself. If your host lacks autotools (`libtool` etc.) and
the library ships a generated `./configure`, point the build at a pre-`configure`'d
tree instead:

```bash
export LIBARCHIVE_SRC=/path/to/an/already-configured/libarchive-3.8.6
```

## A2. Provide a harness with a clear loop/decision site

Two ways to get an instrumented target — pick whichever is less work:

**(a) Instrument an existing utility / fuzz target.** If `libxyz` ships a CLI
(`bsdtar`, `xmllint`, `djpeg`) or an OSS-Fuzz harness, compile *that* with
`afl-clang-fast`. You get instrumentation for free.

**(b) Write a small persistent-mode harness that links the library.** This is what
we did — a clean, single decode path that's easy to annotate. Template:
[`workspace/libarchive/src/archive_fuzzer.c`](../workspace/libarchive/src/archive_fuzzer.c).

Either way, the agent needs a **clear loop / decision / state site to annotate**, and
you do **not** write any `IJON_*` call yourself. `run_target.py` supports two modes,
selected by `"annotate"` in the manifest (A6). *Where* the site lives decides which:

- **`"annotate": "library"`** *(the common case)* — the meaningful loop or decision is
  inside `libxyz` (e.g. libpng's per-chunk processing). The runner **localizes** to
  the few library functions the fuzzer is stuck behind (frontier mode, C4), shows the
  agent only those, places the annotation **in the matching library `.c`**, and
  rebuilds the library. Worked example:
  [`workspace/libpng/`](../workspace/libpng) (manifest + `build.sh agent` below).
  Annotating only the harness would be useless here — the state you care about never
  surfaces at the harness level.
- **`"annotate": "harness"`** *(the libarchive case)* — valid only when the harness
  itself drives the decode loop **and** the interesting state is reachable through the
  library's public API. libarchive's per-entry header loop runs in the harness, and
  `archive_format()` / `archive_entry_filetype()` hand the decoded state back across
  the API, so a harness-level annotation captures real library state.

For libarchive (harness mode) the site is that per-entry header loop:

```c
struct archive_entry *entry;
for (;;) {
    int r = archive_read_next_header(a, &entry);
    if (r == ARCHIVE_EOF || r == ARCHIVE_FATAL) break;
    if (r == ARCHIVE_RETRY) continue;

    /* one decoded entry header = one state transition.
       The agent inserts its IJON feedback annotation here. */

    ... /* consume the entry */
}
```

## A3. Write `build.sh` (or generate a draft)

The loop never hard-codes how to build your target; it shells out to your
workspace's `build.sh <variant>`. **Don't write it from scratch** — generate a draft
and fill in the slots:

```bash
.venv/bin/python scripts/bringup.py --lib /path/to/cloned/libxyz --name libxyz \
    --mode library --reward coverage          # → workspace/libxyz/build.sh.draft + target.json.draft
```

`bringup.py` (deterministic, no LLM) emits the **invariant** AFL/IJON/ASAN
scaffolding — the variant layout, the `AFL_PATH` gotcha, the ASAN-match rule — and
fills the library-specific slots by probing the source: build system, the OSS-Fuzz
harness (the best starting point), `./configure --help` `--without-*` options to pin,
and `-l` deps from `*.pc.in`. The scaffolding is correct by construction; every
uncertain slot is marked `# TODO(verify)`. Review it, drop the `.draft` suffix, done.
(For now `bringup.py` covers autotools well; cmake/meson get a skeleton + TODOs.)

For a normal run the `build.sh` has **three** variants
([`workspace/libarchive/build.sh`](../workspace/libarchive/build.sh)):

| `build.sh <v>` | Produces | Used for |
|---|---|---|
| `plain` | `targets/archive_plain` (afl-clang-fast + ASAN, **no** IJON) | the control arm the loop fuzzes to plateau |
| `agent` | the IJON target the loop rebuilds each iteration | evaluating each proposal |
| `describe` *or* `cov` | the reward tool (see A4) | keep/revert metric |

The `agent` variant differs by mode — this is the one place library vs harness mode
shows up in `build.sh`:

- **harness mode** — the runner writes the patched harness to `$IJON_HARNESS`; your
  `agent` compiles *that* (under `AFL_LLVM_IJON=1`) against the prebuilt library →
  `$IJON_OUT`. (libarchive's `build.sh agent`.)
- **library mode** — the runner has already written the annotation into a library
  `.c` on disk; your `agent` just **recompiles the library under `AFL_LLVM_IJON=1`
  and relinks the (fixed) harness** → `$IJON_OUT`. The runner reverts the file
  afterward. (See [`workspace/libpng/build.sh`](../workspace/libpng/build.sh)'s
  `build_agent`: `make -j4 -C $LIBPNG libpng16.la` then relink.)

```bash
cd workspace/libarchive
bash build.sh plain
md5sum targets/archive_plain   # sanity: a later IJON build must differ from this (C1)
```

Three gotchas that will bite you — details in **C1**: build the library *once* and
relink per variant (in library mode, `make` only recompiles the one annotated file);
pin optional deps so the link is deterministic; and if the library is built with
ASAN, *every* binary linking it must also pass `-fsanitize=address`.

## A4. Give the loop a class-matched reward

Pick the reward that matches your roadblock (the *why* is in **C2**):

- **Reach new code (class 3)** → `"reward": "coverage"`. Add a `cov` build variant
  (an llvm-cov driver); keep/revert counts new source functions. This is the
  [libpng loop](../scripts/libpng_loop.py)'s mode.
- **Reorder exploration of already-covered code (class 2)** → `"reward": "diversity"`.
  Coverage won't move (no new functions), so you supply a tiny `describe` tool that
  decodes each corpus input the way the harness does and prints its token sequence;
  the loop counts distinct sequences. Template:
  [`src/archive_describe.c`](../workspace/libarchive/src/archive_describe.c).

```bash
bash build.sh describe
./targets/archive_describe in/archive.tar.gz   # → "1.196612:32768 1.196612:32768"
#                                                  filter.format:filetype  per entry
```

## A5. Seeds

A handful of valid inputs in `in/`, one per format/filter you care about:

```bash
tar cf a.tar f; tar czf a.tar.gz f; tar cJf a.tar.xz f   # → workspace/libarchive/in/
```

Four seeds is plenty; the fuzzer expands from there.

## A6. The manifest — `target.json`

One small file ties it together so `run_target.py` stays target-agnostic
([`workspace/libarchive/target.json`](../workspace/libarchive/target.json)):

```json
{
  "name": "libarchive",
  "source_name": "libarchive read path (multi-format entry loop)",
  "harness": "src/archive_fuzzer.c",
  "focus":   ["src/archive_fuzzer.c"],
  "seeds":   "in",
  "reward":  "diversity",
  "describe":"targets/archive_describe",
  "build":   { "plain": ["bash","build.sh","plain"],
               "describe": ["bash","build.sh","describe"],
               "agent": ["bash","build.sh","agent"] },
  "targets": { "plain": "targets/archive_plain", "agent": "targets/archive_agent" }
}
```

`focus` is what the agent is shown; `reward` selects the keep/revert metric;
`build.agent` is how the loop rebuilds the agent's annotation. The default
`"annotate"` is `"harness"`, so libarchive omits it.

For **library mode** the manifest instead names where the library source lives and
how to localize ([`workspace/libpng/target.json`](../workspace/libpng/target.json)):

```json
{
  "name": "libpng",
  "source_name": "libpng read path (coverage frontier)",
  "annotate": "library",
  "library_src": "build/libpng",
  "localize": { "fi": "fi_out/…data.yaml", "cov": "build/cov/coverage.json" },
  "harness": "src/libpng_crc_fuzzer.cc",
  "seeds":  "in_single",
  "reward": "coverage",
  "build":  { "plain": ["bash","build.sh","plain"],
              "cov":   ["bash","cov-build.sh"],
              "agent": ["bash","build.sh","agent"] },
  "targets": { "plain": "targets/libpng_crc_plain",
               "cov":   "targets/libpng_crc_cov",
               "agent": "targets/libpng_agent" }
}
```

Here there is no `focus` list — `localize` (the FI static graph ∩ llvm-cov frontier,
C4) picks the functions to show the agent, and the annotation is placed in whichever
`library_src/*.c` holds the agent's chosen anchor line.

## A7. Run

```bash
.venv/bin/python scripts/run_target.py \
    --workspace workspace/libarchive --iters 2 \
    --plateau-timeout 75 --eval-timeout 90
```

Recorded transcript (abridged):

```
[0] fairness gate: 54 lines shown to model; no 'ijon' token present (verified)

1) BUILD + FUZZ the plain control to plateau
    plateau: corpus=1074 files, distinct sequences=45, edges=1623

2.1) ANALYST proposes
    why_stuck     : Edge coverage treats every entry loop iteration identically;
                    the fuzzer cannot distinguish per-entry file types or order.
    failure_class : known_state_changes               ← class 2, correct, blind
    IJON_STATE: IJON_STATE(archive_entry_filetype(entry));
    distinct sequences: base=45 now=160 -> KEEP        ← 3.6x in a 90s window

2.2) ANALYST proposes
    why_stuck     : The existing IJON_STATE exposes the current file type but not
                    the order or history… different sequences look identical.
    IJON_STATE: { static int seq=0; seq=ijon_hashint(seq, …filetype(entry));
                  IJON_STATE(seq); }                   ← the richer sequence idiom
    distinct sequences: base=45 now=106 -> revert      ← did not beat iter1 in-window
```

If `run_target.py` reaches the `VERDICT` banner and the kept annotation moved the
reward, you've reproduced the libarchive result on your own target. What the run
shows: the agent **diagnosed class 2 blind** (it saw the same loop code for every
header, no gradient) and reached for `IJON_STATE`; on iteration 2 it **re-derived
the rolling-state-hash idiom** after seeing its own iteration-1 annotation; and
keep/revert **stayed honest** — the richer annotation was conceptually better but
didn't measurably beat the simpler one in a 90 s window, so the reward didn't credit
it.

### Library mode (libpng) and the build-repair loop

Library mode is the same command on the libpng manifest:

```bash
.venv/bin/python scripts/run_target.py --workspace workspace/libpng \
    --iters 3 --plateau-timeout 100 --eval-timeout 200
```

```
[0] 654 lines of real library source (localized) shown to model
1) plateau: 131 functions covered
2.1) ANALYST  missing_intermediate_state   chunk_name is a 4-byte magic value, no gradient
     IJON_CMP(chunk_name, png_iCCP);   after `png_uint_32 chunk_name = png_ptr->chunk_name;`
     functions: base=131 -> KEEP        ← reaches png_handle_iCCP, a new function
VERDICT  kept 1: IJON_CMP(chunk_name, png_iCCP)
```

The agent, shown only the localized **library** functions, blind-diagnoses the
per-chunk magic-value gate and places `IJON_CMP` *inside `pngrutil.c`* — the same
annotation the bespoke libpng script keeps.

**The build-repair loop.** The agent is editing real C, so it can produce code that
doesn't compile (a classic miss: anchoring on a `#if defined(...)` preprocessor
line). The runner runs a tight inner **correctness** loop, separate from the outer
keep/revert **effectiveness** loop: it captures the compiler/placement error and
hands it to a second *repair* agent whose only job is to make it build — preserving
the analyst's macro and target state, fixing only placement/syntax — then recompiles,
up to `--build-repair-tries` (default 3). A real rescue:

```
[repair 1/3] anchor is a preprocessor line (#if/#define/defined(...)); an IJON
             statement must go on an executable code line ...
[repair 1] -> IJON_CMP(png_ptr->transformations & PNG_COMPOSE, PNG_COMPOSE);
             after 'png_debug(1, "in png_do_read_transformations");'
[repair] fixed after 1 attempt(s)        ← same macro + state, legal placement, builds
```

Only buildable annotations reach the fuzz/reward stage; if repair can't fix it in N
tries, the iteration reverts and the *analyst* re-strategizes next round. (Set
`--repair-model` to use a cheaper model for this mechanical step.)

## A8. The checklist for *your* `libxyz`

1. **Harness + mode** — instrument a utility or write a persistent-mode harness that
   links `libxyz`. Pick the mode: `"annotate":"library"` for most targets (the state
   lives inside `libxyz`; add a `localize` block — frontier mode, C4) or
   `"annotate":"harness"` when the harness drives the decode loop and the state is
   API-visible (libarchive). **No annotation — the agent writes it.**
2. **`build.sh`** — generate a draft with `scripts/bringup.py` and fill the
   `TODO(verify)` slots, or write `plain` + `agent` (library mode: recompile the
   library + relink; harness mode: compile `$IJON_HARNESS`), plus `describe` (class 2)
   or `cov` (class 3). The template keeps `AFL_PATH`/ASAN correct for you.
3. **Reward** — class 2 → a `describe` sequence extractor + `"reward":"diversity"`;
   class 3 → a `cov` build + `"reward":"coverage"`.
4. **Seeds** — a few valid inputs in `in/` (or `in_single`).
5. **`target.json`** — fill in the paths above (library mode also needs
   `library_src` + `localize`; templates: `workspace/libpng` and `workspace/libarchive`).
6. **Run** — `.venv/bin/python scripts/run_target.py --workspace workspace/libxyz --iters 3`.
   The build-repair loop handles compile errors; bump `--build-repair-tries` if needed.

---

# Part B — Measuring the agent (optional)

You do **not** need any of this to fuzz your library — Part A is complete on its own.
Part B is what *we* add to **grade** the agent: a fixed, hand-written "answer key"
annotation so we can (1) prove the agent re-derived the insight blind, and (2) put a
number on how much an ideal annotation is worth, before spending any LLM budget.

## B1. Add a reference annotation behind `#ifdef _USE_IJON`

This is the answer key — the one place a human writes an `IJON_*` call, and only for
measurement. Drop it at the same loop site from A2:

```c
    /* IJON-ANCHOR: one decoded entry header is one state transition. */
#ifdef _USE_IJON
    IJON_STATE(ijon_hashint(
        ijon_hashint((uint32_t)archive_filter_code(a, 0),
                     (uint32_t)archive_format(a)),
        (uint32_t)archive_entry_filetype(entry)));
#endif
```

It's gated so it only compiles into the deterministic-control build (`-D_USE_IJON`),
never into what the agent sees.

## B2. The fairness gate — why the agent never sees the answer

The fairness gate is **opt-in**: set `"fairness_gate": true` in the manifest (our
bundled libarchive workspace does). When on, `harness/build.py: make_clean_source`
removes the `#ifdef _USE_IJON` block **and every line mentioning `ijon`**, then
hard-asserts no `ijon` token survives, and the run prints
`no 'ijon' token present (verified)`. The model knows the IJON *API* from its own
prompt; it never sees this target's planted answer.

**Default is OFF** — and that is correct for real use. With the gate off the agent
sees the **real source, including any IJON annotations you already added**, and
*builds on them* (the loop accumulates; the agent looks for the next roadblock).
Stripping in that case would be wrong — it would hide working code from the agent and
the hard-assert would mangle it. So turn the gate on only to *grade* the agent
against a planted reference; leave it off to actually fuzz your library.

## B3. Deterministic A/B — does the annotation even matter?

Add an `ijon` build variant (`-D_USE_IJON AFL_LLVM_IJON=1`) and fuzz plain vs IJON
under the same seeds and wall-clock, then count distinct sequences:

```
[A/B] 120s per arm from 4 seeds
  plain : corpus= 975  distinct sequences=43
  ijon  : corpus=10667 distinct sequences=10033
  => sequence-diversity ratio: 233.3x
```

Plain AFL retains 43 distinct format/entry sequences (4.4% of its corpus); IJON
retains 10,033 (94%). This both validates your wiring before involving the LLM and
sets the ceiling the agent is reaching for. (You could equally A/B the agent's
*kept* output instead of a hand-written reference — the reference just gives a fixed,
known-good control.)

**Honest scope:** the *magnitude* is budget-dependent and amplified by IJON's corpus
retention — every new state sequence is saved as "new coverage." The durable point is
the **mechanism**: to plain AFL these inputs are coverage-equivalent, so it discards
them; IJON keeps them and keeps mutating them. (Same story, different domain, as
libpng's 41× chunk sequences and libtpms's 11.5× command sequences.)

---

# Part C — Why it works (detailed notes)

## C1. The three build gotchas (and the silent `AFL_PATH` failure)

- **The single most common silent failure.** AFL's compiler force-includes
  `afl-ijon-min.h` only from `$AFL_PATH`. If `AFL_PATH` does not point at
  `$AFL_ROOT/include`, the `IJON_*` macros stay undefined, the annotation *silently
  compiles to nothing*, and the "IJON" binary is byte-identical to plain. Always set
  `AFL_PATH=$AFL_ROOT/include`, and check `md5sum` of plain vs IJON differ.
- **Build the library once, relink the harness per variant.** libarchive's `.a` is
  built a single time (`afl-clang-fast + -fsanitize=address`); only the tiny harness
  is recompiled for plain/agent/ijon. Much faster than a full rebuild per iteration.
- **Pin optional deps explicitly.** A stray host library (we hit `lz4`) gets
  auto-detected by `./configure` and then fails to link. We pass
  `--without-bz2lib --without-lz4 …` so the link is deterministic on any host.
- **ASAN is all-or-nothing.** If the library is built with `-fsanitize=address`,
  every binary that links it (including `describe`) must also pass
  `-fsanitize=address`, or you get `undefined reference to __asan_report_*`.

## C2. Why the reward must match the failure class

IJON failures fall into classes, and the keep/revert reward has to measure the thing
the annotation actually changes:

- **Class 3 — reach new code.** The annotation unlocks a previously-unreachable
  branch. Reward = **new source functions** (llvm-cov replay). This is what a naive
  coverage loop measures, and it's correct *here*.
- **Class 2 — reorder exploration of already-covered code.** A class-2 annotation
  adds **no new functions** — it makes the fuzzer distinguish *orders/sequences* of
  code it already runs. A coverage-based reward would see "0 new functions" and
  wrongly revert the best annotation. Reward must instead be **distinct state
  sequences** (the `describe` tool). This is the step people skip and then wonder why
  a good annotation gets thrown away.

## C3. Why `(filter, format, entry-filetype)` is the right state for libarchive

That triple is *invisible to edge coverage*: a tar with `[file, dir, symlink]` and
one with `[symlink, file, file]` run the **same** loop code in a different order, so
AFL's edge map cannot tell them apart. `archive_format()` alone also can't tell
`a.tar` from `a.tar.gz` (it ignores the compression filter), which is why the filter
code is folded in. That coverage-blind, order-sensitive signal is exactly the gap
IJON fills — and the gap you look for when choosing an annotation site in your own
library.

## C4. Localization — how the agent is pointed at the right code

- **Focus mode (used here).** For a compact harness, hand the agent the whole
  (stripped) harness via `focus`. The annotation site is obvious; no extra setup.
- **Frontier mode (`"annotate":"library"`).** For a big library where the question is
  *which of hundreds of functions* is the wall, the localizer intersects a
  fuzz-introspector static call graph with llvm-cov runtime coverage to find the
  coverage frontier and feeds only those functions to the agent — and the annotation
  is placed **in that library source**, not the harness. `run_target.py` does this
  directly from the manifest's `localize` block; you supply a `fi_out/*.data.yaml`
  (fuzz-introspector) and a `build/cov/coverage.json` (llvm-cov of the plain corpus).
  Worked example: [`workspace/libpng`](../workspace/libpng). It is *not* required for
  small targets — libarchive ran fine in focus mode because its decode state is
  reachable from the harness.

  *Open edge (v2):* the localizer points the agent at the right *functions*; pinning
  the exact loop/state *site* within them — and branch-level dataflow localization —
  is the next step on the roadmap.

## C5. Two agents: analyst vs repair

The loop uses **two roles**, not one, because annotating and fixing-to-compile are
opposite jobs. The **analyst** is creative/strategic — it decides *what state is
invisible to coverage* and *which primitive exposes it*. The **repair** agent is
conservative/mechanical — given a compiler/placement error it makes the patch build
while **preserving** the analyst's macro and target state, changing only placement or
syntax. Merging them risks the analyst "re-thinking" a build error into a different
annotation, discarding the strategy we were trying to compile. So:

- inner **correctness** loop (repair): signal = compiler stderr; bounded by
  `--build-repair-tries`; if it can't fix it, escalate back out.
- outer **effectiveness** loop (analyst): signal = reward (keep/revert); only ever
  sees *buildable* annotations.

They share one JSON schema, so the analyst's `failure_class`/`why_stuck`/state are
carried over verbatim on a repair (the repair agent cannot change strategy even if it
tries). `--repair-model` can point the mechanical step at a cheaper model.

## C6. Honest notes

- **No new bug here.** libarchive is OSS-Fuzz-hardened; this round demonstrates the
  *workflow* and a class-2 state win, not a crash. (As on libtpms.)
- **Numbers are budget-dependent.** The 233× (full A/B) vs 3.6× (90 s loop window)
  gap is mostly eval-window length and annotation richness, not a contradiction.
  Treat them as evidence of the mechanism, not a fixed score.
- **Library-mode coverage reward is coarse + noisy in short windows.** Keep/revert at
  function granularity (`new_vs`) can flip on a function or two between short fuzz
  runs. The durable signal in the libpng run is *which annotation the agent reaches
  blind* — `IJON_CMP(chunk_name, png_iCCP)`, the same one the bespoke script keeps —
  not the exact function delta. For class-2 targets prefer the diversity reward.
- **One generic runner, target-specific plumbing.** `run_target.py` now drives both
  harness- and library-mode targets (with localization + the build-repair loop); the
  harness + `build.sh` are inherently per-library. No tool removes the harness step —
  every fuzzer needs one.
