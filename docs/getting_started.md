# Getting started — running IJON-Reloaded on a new library

Point the agent at a library it has never seen. **Part A** is the recipe — the
commands to fuzz your own `libxyz`, generic and copy-pasteable. **Part B** is the
optional A/B harness we use to *measure* the agent (skip it unless you're reproducing
our numbers). **Part C** explains *why* each step is shaped the way it is, and walks
two real targets — libpng (library mode) and libarchive (harness mode) — end to end.

> **The one thing to internalize.** You do **not** hand-write the IJON annotation —
> that is the agent's whole job. "Running on a new target" is three things *you* set
> up once: (1) a harness that links the library and decodes one input, (2) a
> `build.sh` so the loop can rebuild it, (3) a reward that matches your roadblock.
> Then the loop does the diagnose → annotate → keep/revert part. By default
> (`--mode library`) the agent is shown the relevant **library** code *and* your
> harness, and may annotate **either** — the library is where the interesting state
> lives for most targets, but a loop/counter/mode in the harness is fair game too.

> **Two ways to run it.** **Mode 2 (standalone)** — the Python agents drive everything
> via the DeepSeek API; that's the **Part A** recipe below. **Mode 1 (inside Claude
> Code)** — Claude Code itself is the build-doctor *and* the analyst (**no DeepSeek key
> needed**), running those same Part A steps for you, interactively. If you want Mode 1,
> start with the next section, then let Part A serve as the reference for what CC is doing.

---

# Mode 1 — run it inside Claude Code (no DeepSeek key)

In Mode 1 **Claude Code is the agent**: you don't run the Python loop yourself — CC
brings up the build, picks the harness *with you*, and acts as the analyst, executing
the Part A steps below on your behalf. The whole entry flow:

**1. Toolchain (same as A0, minus the API key).** The build and fuzzer still run
locally, so you still need these — Mode 1 only removes the DeepSeek key:

| Need | Export |
|---|---|
| AFL++ **with IJON** | `AFL_ROOT=/path/to/AFLplusplus` |
| LLVM (clang + llvm-cov) | `LLVM_BIN=/path/to/llvm/bin` |
| Scratch space (not `/tmp`) | `TMPDIR=/your/space` |
| Python venv + LiteLLM | `.venv` in the repo |
| ~~DeepSeek API key~~ | **not needed in Mode 1** |

**2. Get this repo and launch Claude Code inside it.** ([Claude Code](https://claude.com/claude-code)
must be installed.) The skill lives in `.claude/skills/ijon-reloaded/` and registers
automatically when CC starts in the repo. The venv still needs LiteLLM installed —
`analyst_cli.py` imports it even in Mode 1 (it just never calls the API). Export the
env **before** launching `claude` so CC's shell inherits it:

```bash
git clone <ijon-llm-url> && cd ijon-llm
python3 -m venv .venv && .venv/bin/pip install litellm
export AFL_ROOT=/path/to/AFLplusplus            # built with IJON
export LLVM_BIN=/path/to/llvm/bin
export TMPDIR=/your/space                        # not /tmp — see C1
export PATH="$AFL_ROOT:$PATH" AFL_PATH="$AFL_ROOT/include"   # AFL_PATH must be …/include
claude                                           # start Claude Code IN this directory
```

**3. Have your target cloned, and tell CC what you want.** For example:

> "Use the ijon-reloaded skill to bring up and fuzz **/path/to/my/cloned/libxyz** with
> IJON. List the harnesses and let me pick." *(or name one: "use the
> `tests/oss-fuzz/foo_target.c` harness.")*

(If you give CC the repo URL instead, it can clone the target for you.)

**4. CC drives; you steer at the real decisions.** It will: list/confirm the **harness**
(it asks you — A2), bring up the build (the **build-doctor**, fixing compile errors as
they arise — A3/A4), set up the **reward** + localization (A4–A6), then run the
diagnose → annotate → keep/revert **loop** via `scripts/analyst_cli.py` (the Mode-1
equivalent of A6's `run_target.py`). You approve the harness and the coverage-vs-
diversity choice; CC writes the `IJON_*` annotations.

Everything from here on (Part A) is the detailed recipe **CC follows** — read it to
understand what CC is doing and to check its work. It's written as the standalone
Mode-2 commands; in Mode 1 CC runs them for you.

---

# Part A — Run it on your library

The whole of Part A produces and then runs **one directory**, `workspace/libxyz/`.
`bringup.py` creates it (A2); you finish filling it (A3–A5); `run_target.py` runs it
(A6). Wherever you see `libxyz`, substitute your target's short name.

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

## A1. What you bring

Two things, before any of our tooling runs:

- **The library source**, cloned to a directory —
  `git clone --depth 1 -b <tag> <url> /path/to/libxyz`.
- **A harness** — a `.c`/`.cc` that turns one input buffer into one decode. It can be
  either:
  - an **OSS-Fuzz / libFuzzer harness** (`LLVMFuzzerTestOneInput`), shipped under
    `contrib/oss-fuzz/`, `tests/oss-fuzz/`, `tests/fuzz/`; `bringup.py` discovers these
    (A2), or
  - a **utility the library already ships** (`bsdtar`, `xmllint`, `djpeg`, libarchive's
    `untar.c`) — a program with its own `main` that reads a file/stdin. You point
    `bringup.py` at it directly (A2).

  If neither fits, write a small persistent-mode harness calling the library's decode
  entry point. (`bringup.py` handles the build differences automatically — see A2.)

## A2. Generate the workspace

```bash
.venv/bin/python scripts/bringup.py --lib /path/to/libxyz --name libxyz \
    --mode library --reward coverage
```

This **creates `workspace/libxyz/`** and writes a draft `build.sh.draft` +
`target.json.draft` into it, filling the library-specific slots by probing the source
(build system, the OSS-Fuzz harness, link deps, `./configure` flags). *That directory
is your workspace* — the `--workspace workspace/libxyz` you pass to the runner in A6.
Everything for this target lives under it; you don't create the directory yourself.

The two choices, with their defaults:

| Option | Default | Alternative | Pick by |
|---|---|---|---|
| `--mode` | **`library`** — the runner localizes to the few library functions the fuzzer is stuck behind *and* also shows your harness; the agent annotates whichever holds the state (library `.c` or harness), and the library is rebuilt each iter | `harness` — shows + annotates the harness *only*; no library localization, no library rebuild (the library stays prebuilt). Use it when the decode state is fully reachable at the harness level (libarchive) and you want to skip the `localize` setup (→ C4) | most targets are `library` |
| `--reward` | **`coverage`** — keep an annotation if it **reaches new code** (class 3) | `diversity` — if it **reorders** exploration of already-covered code (class 2) | what your fuzzer is stuck on (→ C2) |

**Choosing the harness.** Don't trust the auto-pick — it's a weak name heuristic.
List what's there and choose:

```bash
.venv/bin/python scripts/bringup.py --lib /path/to/libxyz --name libxyz --list-harnesses
#   -> numbered list of every file defining LLVMFuzzerTestOneInput
.venv/bin/python scripts/bringup.py --lib … --name libxyz --mode library --reward coverage \
    --harness pdu_parse_udp          # pick by path OR a unique substring; copied into src/
```

`--harness` also takes a path to a **utility** that isn't a libFuzzer harness (e.g.
`--harness examples/untar.c`). `bringup.py` detects the **kind** and builds it right
(override with `--harness-kind`):

| kind | built with | AFL feeds it |
|---|---|---|
| `libfuzzer` (`LLVMFuzzerTestOneInput`) | `-fsanitize=fuzzer` + ASAN | persistent (no file arg) |
| `argv` (utility, own `main`, reads a file) | ASAN only (**no** fuzzer flag) | a file via `@@` (`"input":"argv"`) |
| `stdin` (utility reading stdin) | ASAN only | stdin |

ASAN stays on for **every** kind (and must match the library — all-or-nothing, → C1).
For an `argv` tool that needs a flag, edit the manifest's `"target_args"` (e.g.
`["-f","@@"]` for `archivetest -f @@`).

## A3. Finish the workspace

`bringup.py` gets the invariant AFL/IJON/ASAN wiring right; you fill the
library-specific slots it marked `# TODO(verify)` and add the two files it can't
generate. The finished layout:

```
workspace/libxyz/
├── build.sh          ← rename from .draft; fill TODO(verify): repo url/tag, -I/-L/-l, configure pins
├── target.json       ← rename from .draft; fill source paths (+ library-mode localize block, A4)
├── src/
│   └── libxyz_fuzzer.c   ← your harness (copy + adapt the OSS-Fuzz one bringup pointed at)
└── in/               ← a few valid seed inputs (one per format/path you care about)
```

`bringup.py` prints the exact deps/flags it found for each `TODO(verify)` line — fill
those, drop both `.draft` suffixes, then sanity-check the control build:

```bash
cd workspace/libxyz
bash build.sh plain          # clones + builds the library under build/libxyz/, links the control
```

(The three gotchas that bite here — the silent `AFL_PATH` failure, pinning deps for a
deterministic link, and ASAN being all-or-nothing — are in **C1**; the template
already handles them, so this is just a checkpoint.)

## A4. (Library mode only) point the localizer at the code

Library mode shows the agent only the functions on the **coverage frontier**, so it
needs two inputs, named in `target.json`'s `localize` block:

- `fi_out/*.data.yaml` — a fuzz-introspector static call graph of the library.
- `build/cov/coverage.json` — an llvm-cov run of the plain corpus.

How to produce both is in **C3**. (Harness mode skips this entirely — it uses the
`focus` list `bringup.py` already wrote into the manifest.)

## A5. The reward tool

The reward you chose in A2 needs one small build variant:

- **`coverage`** (default) → a `cov` build variant (an llvm-cov driver). `bringup.py`
  left a `cov` slot in `build.sh`; point it at libxyz's coverage build.
- **`diversity`** → a tiny `describe` tool that decodes each corpus input the way the
  harness does and prints its token sequence; the loop counts distinct sequences.

→ C2 for which one fits, and the per-mode templates under `workspace/` for both.

## A6. Run

```bash
.venv/bin/python scripts/run_target.py --workspace workspace/libxyz --iters 3
```

The loop fuzzes the plain control to a plateau, then each iteration: shows the agent
the localized code, takes its proposed annotation, **builds it** (with a repair
sub-loop that fixes non-compiling edits — C5), fuzzes, and keeps or reverts on the
reward. A successful iteration reads like:

```
[0] localized library source shown to model
1) plateau: N functions covered
2.1) ANALYST  missing_intermediate_state   <diagnosis, derived blind>
     IJON_CMP(...)   placed in <some>.c
     functions: base=N -> KEEP        ← reached a new function
VERDICT  kept 1: IJON_CMP(...)
```

If the runner reaches the `VERDICT` banner and the kept annotation moved the reward,
it worked. The source tree is restored pristine afterward — the annotation lived only
inside the loop.

## A7. Checklist for *your* `libxyz`

1. **Inputs** — cloned library source + a harness that links it (A1).
2. **`bringup.py`** — `--mode library` (default) or `harness`; `--reward coverage`
   (default) or `diversity`. This **creates `workspace/libxyz/`** (A2).
3. **Finish the workspace** — rename the two `.draft`s, fill every `TODO(verify)`, add
   `src/<harness>` and a few `in/` seeds (A3).
4. **(library mode) localize** — drop in `fi_out/` + `build/cov/` and name them in
   `target.json` (A4; how in C3).
5. **Reward tool** — a `cov` build (coverage) or a `describe` tool (diversity) (A5).
6. **Run** — `run_target.py --workspace workspace/libxyz` (A6). **You never write an
   `IJON_*` call — the agent does.**

---

# Part B — Measuring the agent (optional)

You do **not** need any of this to fuzz your library — Part A is complete on its own.
Part B is what *we* add to **grade** the agent: a fixed, hand-written "answer key"
annotation so we can (1) prove the agent re-derived the insight blind, and (2) put a
number on how much an ideal annotation is worth, before spending any LLM budget. The
example here is libarchive (harness mode); the same shape applies in library mode.

## B1. Add a reference annotation behind `#ifdef _USE_IJON`

This is the answer key — the one place a human writes an `IJON_*` call, and only for
measurement. Drop it at the decode/loop site (for libarchive, the per-entry header
loop — see C4):

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
- **Build the library once, relink the harness per variant.** The library archive is
  built a single time (`afl-clang-fast + -fsanitize=address`); only the tiny harness
  is recompiled for plain/agent/ijon. In library mode `make` then recompiles just the
  one annotated file. Much faster than a full rebuild per iteration.
- **Pin optional deps explicitly.** A stray host library (we hit `lz4`) gets
  auto-detected by `./configure` and then fails to link. Pass
  `--without-bz2lib --without-lz4 …` so the link is deterministic on any host.
  `bringup.py` lists the candidates it found.
- **ASAN is all-or-nothing.** If the library is built with `-fsanitize=address`,
  every binary that links it (including `describe`) must also pass
  `-fsanitize=address`, or you get `undefined reference to __asan_report_*`.

## C2. Why the reward must match the failure class

IJON failures fall into classes, and the keep/revert reward has to measure the thing
the annotation actually changes:

- **Class 3 — reach new code.** The annotation unlocks a previously-unreachable
  branch. Reward = **new source functions** (llvm-cov replay → `"reward":"coverage"`).
  This is what a naive coverage loop measures, and it's correct *here*.
- **Class 2 — reorder exploration of already-covered code.** A class-2 annotation
  adds **no new functions** — it makes the fuzzer distinguish *orders/sequences* of
  code it already runs. A coverage-based reward would see "0 new functions" and
  wrongly revert the best annotation. Reward must instead be **distinct state
  sequences** (a `describe` tool → `"reward":"diversity"`). This is the step people
  skip and then wonder why a good annotation gets thrown away.

## C3. Localization — pointing the agent at the right code (and generating its inputs)

- **Focus mode** (harness mode's default). For a compact harness, hand the agent the
  whole harness via the manifest's `focus` list. The annotation site is obvious; no
  extra inputs needed. `bringup.py` writes this automatically for `--mode harness`.
- **Frontier mode** (`--mode library`, the default). For a big library where the
  question is *which of hundreds of functions* is the wall, the localizer intersects a
  fuzz-introspector static call graph with llvm-cov runtime coverage to find the
  coverage frontier, and feeds those functions to the agent — **plus the harness**, so
  a harness-level loop/counter is annotatable in the same run (the agent picks
  whichever file holds the state; the runner places the annotation there and rebuilds).
  Once the agent has engaged the library (any proposal that lands in a library `.c`),
  the runner **retires the harness to a comment-stripped stub** for later iterations —
  it stops re-sending the boilerplate but keeps *every executable line*, so no harness
  site is ever lost and the agent can still annotate there.
  `run_target.py` reads this from the manifest's `localize` block. You produce two
  files (A4):
  - **`fi_out/*.data.yaml`** — run fuzz-introspector's static analysis over the
    library to emit the call graph. (We use the standalone tree-sitter frontend; the
    `workspace/libpng/fi_out/` files are a worked reference.)
  - **`build/cov/coverage.json`** — build a coverage variant
    (`-fprofile-instr-generate -fcoverage-mapping`, see `workspace/libpng/cov-build.sh`),
    replay the plain corpus, and export with `llvm-cov export … > coverage.json`.

  *Open edge (v2):* the localizer points the agent at the right *functions*; pinning
  the exact loop/state *site* within them — and branch-level dataflow localization —
  is the next step on the roadmap.

## C4. Harness mode, worked: libarchive

Harness mode is the exception, valid only when the harness **itself** drives the
decode loop and the decoded state is reachable through the library's public API.
libarchive fits: its per-entry header loop runs in the harness, and
`archive_format()` / `archive_entry_filetype()` hand the decoded state back across the
API, so a harness-level annotation captures real library state. The site:

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

**Why `(filter, format, entry-filetype)` is the right state.** That triple is
*invisible to edge coverage*: a tar with `[file, dir, symlink]` and one with
`[symlink, file, file]` run the **same** loop code in a different order, so AFL's edge
map cannot tell them apart. `archive_format()` alone also can't tell `a.tar` from
`a.tar.gz` (it ignores the compression filter), which is why the filter code is folded
in. That coverage-blind, order-sensitive signal is exactly the gap IJON fills.

A real run (diversity reward, 90 s eval window) — the agent diagnoses this **blind**:

```
2.1) ANALYST proposes
    why_stuck     : Edge coverage treats every entry loop iteration identically;
                    the fuzzer cannot distinguish per-entry file types or order.
    failure_class : known_state_changes               ← class 2, correct, blind
    IJON_STATE: IJON_STATE(archive_entry_filetype(entry));
    distinct sequences: base=45 now=160 -> KEEP        ← 3.6x in a 90s window
2.2) ANALYST proposes
    IJON_STATE: { static int seq=0; seq=ijon_hashint(seq, …filetype(entry));
                  IJON_STATE(seq); }                   ← the richer sequence idiom
    distinct sequences: base=45 now=106 -> revert      ← did not beat iter1 in-window
```

It reached for `IJON_STATE` with no gradient to see, and on iteration 2 re-derived the
rolling-state-hash idiom from its own iteration-1 annotation — while keep/revert stayed
honest (the richer annotation didn't measurably beat the simpler one in-window).

## C5. Two agents: analyst vs repair (and the build-repair loop)

The loop uses **two roles**, not one, because annotating and fixing-to-compile are
opposite jobs. The **analyst** is creative/strategic — it decides *what state is
invisible to coverage* and *which primitive exposes it*. The **repair** agent is
conservative/mechanical — given a compiler/placement error it makes the patch build
while **preserving** the analyst's macro and target state, changing only placement or
syntax. Merging them risks the analyst "re-thinking" a build error into a different
annotation, discarding the strategy we were trying to compile. So:

- inner **correctness** loop (repair): signal = compiler stderr; bounded by
  `--build-repair-tries` (default 3); if it can't fix it, escalate back out.
- outer **effectiveness** loop (analyst): signal = reward (keep/revert); only ever
  sees *buildable* annotations.

They share one JSON schema, so the analyst's `failure_class`/`why_stuck`/state are
carried over verbatim on a repair (the repair agent cannot change strategy even if it
tries). `--repair-model` can point the mechanical step at a cheaper model. A real
rescue, where the analyst anchored on a preprocessor line that can't take a statement:

```
[repair 1/3] anchor is a preprocessor line (#if/#define/defined(...)); an IJON
             statement must go on an executable code line ...
[repair 1] -> IJON_CMP(png_ptr->transformations & PNG_COMPOSE, PNG_COMPOSE);
             after 'png_debug(1, "in png_do_read_transformations");'
[repair] fixed after 1 attempt(s)        ← same macro + state, legal placement, builds
```

Only buildable annotations reach the fuzz/reward stage; if repair can't fix it in N
tries, the iteration reverts and the *analyst* re-strategizes next round.

## C6. Library mode, worked: libpng

Library mode is the same `run_target.py` command on a library-mode manifest:

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

Shown only the localized **library** functions (frontier mode, C3), the agent
blind-diagnoses the per-chunk magic-value gate and places `IJON_CMP` *inside
`pngrutil.c`* — the same annotation the bespoke libpng script keeps. This is the
common case Part A defaults to.

## C7. Honest notes

- **No new bug in these runs.** libarchive and libpng are OSS-Fuzz-hardened; these
  rounds demonstrate the *workflow* and state wins, not crashes. (As on libtpms.)
- **Numbers are budget-dependent.** The 233× (full A/B) vs 3.6× (90 s loop window)
  gap is mostly eval-window length and annotation richness, not a contradiction.
  Treat them as evidence of the mechanism, not a fixed score.
- **Library-mode coverage reward is coarse + noisy in short windows.** Keep/revert at
  function granularity can flip on a function or two between short fuzz runs. The
  durable signal in the libpng run is *which annotation the agent reaches blind*, not
  the exact function delta. For class-2 targets prefer the diversity reward.
- **One generic runner, target-specific plumbing.** `run_target.py` drives both
  harness- and library-mode targets (with localization + the build-repair loop); the
  harness + `build.sh` are inherently per-library. No tool removes the harness step —
  every fuzzer needs one.
