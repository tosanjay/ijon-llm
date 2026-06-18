# libarchive — a fresh real target, end to end (the getting-started worked example)

libarchive 3.8.6, read path with all formats + filters enabled. This is the
target the [getting-started walkthrough](../../docs/getting_started.md) builds
from scratch; it doubles as a fourth real-target datapoint for the class-2 thesis.

**The hidden state.** A container is a *sequence* of `(filter, format, entry
file-type)` triples. Two archives that run the same decode loop in a different
order are identical to AFL's edge map — so the ordering is invisible to coverage,
which is exactly where IJON helps. Metric: distinct such sequences across the
corpus, extracted by `targets/archive_describe` (libarchive decoding the inputs
itself). Harness: `workspace/libarchive/src/archive_fuzzer.c`.

## Deterministic A/B (reference annotation, 120 s/arm, 4 seeds)
| arm | corpus | distinct sequences |
|---|---|---|
| plain AFL | 975 | 43 (4.4% of corpus) |
| AFL+IJON | 10,667 | **10,033** (94% of corpus) |

**233× more distinct state sequences.** *Honest scope:* the magnitude is
budget-dependent and amplified by IJON corpus retention; the durable point is the
mechanism — plain AFL discards state-equivalent inputs, IJON retains and keeps
mutating them. (Same gradient as libpng 41×, libtpms 11.5×.)

## Autonomous loop (blind, `scripts/run_target.py`, deepseek-v4-pro)
Shown only the 54-line fairness-stripped harness, the agent:
- diagnosed **class 2** (`known_state_changes`) — "edge coverage sees the same
  loop code for each header; no gradient";
- proposed `IJON_STATE(archive_entry_filetype(entry))` → kept (45→160 sequences,
  3.6× in a 90 s window);
- on iteration 2, *seeing its own annotation*, re-derived the richer
  rolling-state-hash sequence idiom — correctly **reverted** by the coverage-blind
  reward because it didn't beat iteration 1 in-window (honest; the headroom the
  A/B's full triple realizes as 233×).

Records: [`result.json`](result.json). Reproduce: follow
[`docs/getting_started.md`](../../docs/getting_started.md) §7–§8.
