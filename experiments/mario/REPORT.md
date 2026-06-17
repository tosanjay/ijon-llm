# Mario (SuperMarioBros-C) — the paper's flagship benchmark, as a demo

The IJON paper's most iconic demo: AFL "plays" Super Mario Bros. via one
annotation, `ijon_max(pos_y/16, world_pos)`. We use the authors' own C++ port +
harness (RUB-SysSec/ijon-data), built headless on Ubuntu 22.04 (no ROM, no
display; build at `-O0` — the codegen'd 6502 emulation miscompiles at `-O2`).

Here Mario serves as a **demo of autonomous annotation** — the agent re-deriving
the paper's flagship annotation blind, and that annotation driving a real
playthrough. It is **not** used as an effectiveness benchmark; see the scope note
below.

## Autonomous annotation (blind)
Blind (annotation stripped, plateau telemetry, no answer), the model proposed:

    IJON_MAX(world_pos);   // after: uint64_t world_pos = screen*255 + pos;

`known_relevant_state_values` — the **right primitive on the right state**,
reproducing the core of the paper's iconic annotation on its own benchmark. A
class-1 `IJON_MAX` human-vs-LLM datapoint (complements the maze's class-1 SET).
Honest nuance: the human buckets by height (`pos_y/16`) to also preserve vertical
exploration; the LLM used a single bucket.
(`scripts/mario_annotation.py`, record: `annotation.json`)

## The video (ROM-free) — the agent's annotation, playing
`mario_playthrough.gif` — the agent-annotated playthrough, headless-rendered with
a synthesized CHR (each tile a solid block in the game's *real* palette; the
dominant sky tile 292 + tile 0 left blank so the sky reads correctly), with a
live `world_pos` overlay climbing 40 → 1789. **No Nintendo ROM data is used or
produced** — the game logic + palette are compiled C++; only the tile graphics
are synthetic. Reproduce: `python scripts/mario_video.py` (capture path:
`workspace/mario/patches/capture_mode.patch`, seed: `workspace/mario/playthrough.input`).

## Scope: why there is no plain-vs-IJON effectiveness number here
We deliberately do **not** report a Mario effectiveness A/B. Two reasons:

1. **Mario is a poor target to *measure* an IJON effect on.** `world_pos` partly
   leaks into edge coverage — each new screen runs new level code — so a strong
   modern AFL++ already has a forward gradient. IJON's advantage shows up where
   state is *invisible* to coverage, which Mario's position is not.
2. **Our Mario control build did not cleanly exclude the annotation.** The
   "plain" binary was later found byte-identical to the IJON binary (the
   annotation was not compiled out), so any plain-vs-IJON comparison from this
   setup would be invalid. We withdrew it rather than report an unreliable tie.

For effectiveness, the evidence is on targets whose relevant state is genuinely
coverage-invisible: **libpng** (41× chunk-sequence diversity), **libtpms** (11.5×
command sequences), and the synthetic **maxclimb / checksum** solves. Mario's role
is the autonomous-annotation reproduction + the demo video above.
