# Getting started — running IJON-Reloaded on a new library

This is the concrete, end-to-end recipe for pointing the agent at a library it has
never seen. It is **not** a sketch: every command and every number below was run
on **libarchive 3.8.6**, and the files it produces live in
[`workspace/libarchive/`](../workspace/libarchive). Follow it verbatim to
reproduce, or use it as the template for your own `libxyz`.

> **The one thing to internalize first.** The agent does not explore your
> codebase or write inputs. It reshapes the fuzzer's **feedback function** at one
> code site. So "running on a new target" is really three jobs *you* do once —
> (1) get an instrumented harness, (2) give the loop a way to build it, (3) give
> the loop a reward that matches the roadblock — and then the loop does the
> diagnose → annotate → keep/revert part. Everything below is those three jobs.

---

## 0. Prerequisites

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
export PATH="$AFL_ROOT:$PATH" AFL_PATH="$AFL_ROOT/include"
```

> **The single most common silent failure** (it cost us a whole experiment once):
> AFL's compiler force-includes `afl-ijon-min.h` only from `$AFL_PATH`. If
> `AFL_PATH` does not point at `$AFL_ROOT/include`, the `IJON_*` macros stay
> undefined, your annotation *silently compiles to nothing*, and the "IJON" binary
> comes out byte-identical to plain. Always set `AFL_PATH=$AFL_ROOT/include`.

---

## 1. Get the target source

```bash
git clone --depth 1 -b v3.8.6 https://github.com/libarchive/libarchive.git
```

Our `build.sh` will clone this itself. If your host is missing autotools
(`libtool` etc.) and the library ships a generated `./configure` (most release
tarballs do), point the build at a pre-`configure`'d tree instead and skip
`autogen.sh`:

```bash
export LIBARCHIVE_SRC=/path/to/an/already-configured/libarchive-3.8.6
```

---

## 2. Pick the harness path — **utility** or **write one**

This is the fork in the road for any new library.

**(a) Instrument an existing utility / fuzz target.** If `libxyz` ships a CLI
(`bsdtar`, `xmllint`, `djpeg`) or an OSS-Fuzz harness, the fastest path is to
compile *that* with `afl-clang-fast`. You get an instrumented target for free;
you just need a code site to annotate.

**(b) Write a small persistent-mode harness that links the library.** This is
what we did — it gives a clean, single decode path that's easy to annotate.
libarchive even ships an OSS-Fuzz harness (`contrib/oss-fuzz/libarchive_fuzzer.cc`)
we adapted into a self-contained one:
[`workspace/libarchive/src/archive_fuzzer.c`](../workspace/libarchive/src/archive_fuzzer.c).

The harness has three deliberate features:

```c
struct archive_entry *entry;
for (;;) {
    int r = archive_read_next_header(a, &entry);
    if (r == ARCHIVE_EOF || r == ARCHIVE_FATAL) break;
    if (r == ARCHIVE_RETRY) continue;

    /* IJON-ANCHOR: one decoded entry header is one state transition. */
#ifdef _USE_IJON
    IJON_STATE(ijon_hashint(
        ijon_hashint((uint32_t)archive_filter_code(a, 0),
                     (uint32_t)archive_format(a)),
        (uint32_t)archive_entry_filetype(entry)));
#endif
    ...
}
```

1. **A clear loop/decision site** — the per-entry header loop. Container formats
   are walked one entry at a time; that loop is where the *sequence* state lives.
2. **A `#ifdef _USE_IJON` reference annotation** — the "answer." The
   [fairness gate](#what-the-agent-actually-sees) strips this block (and every
   `ijon` mention) before the agent sees the file, so the agent must re-derive it.
   It also lets you build a deterministic control (§7).
3. **The state choice itself.** Why `(filter, format, entry-filetype)`? Because
   that triple is *invisible to edge coverage*: a tar with `[file, dir, symlink]`
   and one with `[symlink, file, file]` run the **same** loop code in a different
   order, so AFL's edge map cannot tell them apart. That is precisely the gap IJON
   fills — and the gap you're looking for when you choose an annotation site.

---

## 3. Write `build.sh` — the build contract

The loop never hard-codes how to build your target; it shells out to your
workspace's `build.sh <variant>`. See
[`workspace/libarchive/build.sh`](../workspace/libarchive/build.sh). The variants:

| `build.sh <v>` | Produces | Used for |
|---|---|---|
| `plain` | `targets/archive_plain` (afl-clang-fast + ASAN, **no** IJON) | the control arm |
| `ijon` | `targets/archive_ijon` (`-D_USE_IJON AFL_LLVM_IJON=1`) | deterministic A/B |
| `describe` | `targets/archive_describe` (plain libarchive) | the class-2 metric (§5) |
| `agent` | `targets/archive_agent` from `$IJON_HARNESS` under `AFL_LLVM_IJON=1` | the loop's rebuilds |
| `cov` | `targets/archive_cov` (llvm-cov driver) | coverage-reward keep/revert |

Three things that bit us and will bite you:

- **Build the library once, relink the harness per variant.** libarchive's `.a`
  is built a single time with `afl-clang-fast + -fsanitize=address`; only the tiny
  harness is recompiled for plain/ijon/agent. Much faster than a full rebuild each
  iteration.
- **Pin optional deps explicitly.** A stray host library (we hit `lz4`) gets
  auto-detected by `./configure` and then fails to link. We pass
  `--without-bz2lib --without-lz4 …` so the link is deterministic on any host.
- **ASAN is all-or-nothing.** If the library is built with `-fsanitize=address`,
  every binary that links it (including `describe`) must pass `-fsanitize=address`
  too, or you get `undefined reference to __asan_report_*`.

```bash
cd workspace/libarchive
bash build.sh all          # → targets/archive_plain + archive_ijon
# IJON pass: Found 1 functions to process
# Instrumented 1 IJON calls for tracking (IJON_STATE: 1).   ← the annotation took
```

Sanity check the two binaries genuinely differ (if their md5s match, your
`AFL_PATH` is wrong — see §0):

```bash
md5sum targets/archive_plain targets/archive_ijon   # must differ
```

---

## 4. Seeds

A handful of valid inputs across the formats/filters you care about. We generated
one per container/compression with the system tools:

```bash
tar cf a.tar f; tar czf a.tar.gz f; tar cJf a.tar.xz f; … | cpio -o > a.cpio
```

→ [`workspace/libarchive/in/`](../workspace/libarchive/in) (tar, tar.gz, tar.xz,
cpio). Four seeds is plenty; the fuzzer expands from there.

---

## 5. Give the loop a class-matched reward

This is the step people skip and then wonder why a good annotation gets reverted.

A class-2 annotation (reorder exploration of **already-covered** code) adds **no
new source functions** — so a coverage-based keep/revert would throw it away. The
reward has to measure the thing the annotation exposes: **distinct state
sequences**. We extract that with a tiny tool that decodes each corpus input the
same way the harness does and prints its token sequence —
[`src/archive_describe.c`](../workspace/libarchive/src/archive_describe.c):

```bash
bash build.sh describe
./targets/archive_describe in/archive.tar.gz     # → "1.196612:32768 1.196612:32768"
#                                                    filter.format:filetype  per entry
```

Counting distinct output lines across a corpus = distinct sequences explored.
(If your roadblock is class-3 — the annotation should reach *new code* — use the
`coverage` reward instead, which replays the corpus through an llvm-cov build and
counts new functions. Set `"reward": "coverage"` in the manifest. That's the
[libpng loop](../scripts/libpng_loop.py)'s mode.)

---

## 6. The manifest — `target.json`

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

`focus` is what the agent is shown (after stripping). `reward` selects the
keep/revert metric. `build.agent` is how the loop rebuilds the agent's annotation.

---

## 7. Deterministic A/B — does the annotation even matter?

Before involving the LLM, prove the *reference* annotation moves the needle. Same
seeds, same wall-clock, plain vs IJON, then count distinct sequences:

```
[A/B] 120s per arm from 4 seeds
  plain : corpus= 975  distinct sequences=43
  ijon  : corpus=10667 distinct sequences=10033
  => sequence-diversity ratio: 233.3x
```

Plain AFL retains 43 distinct format/entry sequences (4.4% of its corpus); IJON
retains 10,033 (94% of its corpus). **Honest scope:** the *magnitude* is
budget-dependent and amplified by IJON's corpus retention — every new state
sequence is saved as "new coverage." The durable point is the **mechanism**: to
plain AFL these inputs are coverage-equivalent, so it discards them and stops
differentiating; IJON keeps them and keeps mutating them. That retention *is* how
it explores deeper state. (Same story, different domain, as libpng's 41× chunk
sequences and libtpms's 11.5× command sequences.)

---

## 8. The autonomous round

Now the actual product: the agent, shown only the fairness-stripped harness, has
to diagnose the roadblock and re-derive an annotation.

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

What this shows:

- **It diagnosed class 2 blind** — "edge coverage sees the same loop code for each
  header; no gradient" — and reached for `IJON_STATE`, the right primitive.
- **It re-derived the rolling-state-hash idiom** on iteration 2 (the same idiom the
  IJON authors used for TPM commands), having *seen its own iteration-1 annotation*
  and noticed it lacked order/history.
- **Keep/revert stayed honest.** The richer sequence annotation is conceptually
  better, but it did not measurably beat the simpler one in a 90 s window, so the
  reward did not credit it. The loop keeps what *measurably* wins, not what sounds
  good — exactly the discipline you want, and the same "the agent's blind
  annotation is coarser than the reference; headroom remains" gap we report on
  libtpms. The deterministic A/B (§7) shows where that headroom goes: the full
  `(filter, format, type)` sequence reaches 233×.

### What the agent actually sees

The fairness gate (`harness/build.py: make_clean_source`) removes the
`#ifdef _USE_IJON` block **and every line mentioning `ijon`** (so even the
`IJON-ANCHOR` comment is gone), then hard-asserts no `ijon` token survives. The
model knows the IJON *API* from its own prompt; it never sees this target's
answer. That assertion is printed every run (`no 'ijon' token present (verified)`).

---

## Localization: how the agent is pointed at the right code

- **Focus mode (used here).** For a compact harness, hand the agent the whole
  (stripped) harness via `focus`. The annotation site is obvious; no extra setup.
- **Frontier mode (advanced).** For a big library where the question is *which of
  hundreds of functions* is the wall, the localizer intersects a
  fuzz-introspector static call graph with llvm-cov runtime coverage to find the
  coverage frontier and feeds only those functions to the agent. That's how the
  [libpng loop](../scripts/libpng_loop.py) works; its setup (a `fi_out/*.data.yaml`
  and a `cov-build.sh`) is the worked example to copy. It is *not* required to get
  started — libarchive ran fine in focus mode.

---

## Adapting this to your `libxyz` — the checklist

1. **Harness** — instrument a utility, or write a persistent-mode harness that
   links `libxyz`. Put a `#ifdef _USE_IJON` reference annotation at the loop or
   decision site whose *state* edge coverage can't see. (Template:
   `workspace/libarchive/src/archive_fuzzer.c`.)
2. **`build.sh`** — implement the `plain` / `agent` variants (and `describe` or
   `cov` for the reward). Pin optional deps; keep `AFL_PATH=$AFL_ROOT/include`;
   match ASAN everywhere. (Template: `workspace/libarchive/build.sh`.)
3. **Reward** — class 2 (reorder covered code) → a `describe`-style sequence
   extractor + `"reward":"diversity"`. class 3 (reach new code) →
   `"reward":"coverage"` + a `cov` build.
4. **Seeds** — a few valid inputs in `in/`.
5. **`target.json`** — fill in the paths above.
6. **Validate** then **run**:
   ```bash
   .venv/bin/python scripts/run_target.py --workspace workspace/libxyz --iters 3
   ```

If `run_target.py` reaches the verdict and the kept annotation moved the reward,
you've reproduced the libarchive result on your own target.

---

## Honest notes

- **No new bug here.** libarchive is OSS-Fuzz-hardened; this round demonstrates the
  *workflow* and a class-2 state win, not a crash. (As on libtpms.)
- **Numbers are budget-dependent.** The 233× (full A/B) vs 3.6× (90 s loop window)
  gap is mostly eval-window length and annotation richness, not a contradiction —
  see §7/§8. Treat them as evidence of the mechanism, not as a fixed score.
- **One generic runner, target-specific plumbing.** `run_target.py` is reusable;
  the harness + `build.sh` are inherently per-library. No tool removes the harness
  step — every fuzzer needs one.
