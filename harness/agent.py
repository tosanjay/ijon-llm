"""The 'analyst' agent: given a stuck target's source + plateau telemetry,
classify why the fuzzer is stuck and synthesize one IJON annotation.

The prompt gives the model what a human IJON analyst has — the source, the
fuzzer's situation, the IJON primitive API, and the paper's taxonomy of
roadblocks — but never the specific answer. The model must reason out which
program state is invisible to edge coverage and expose it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .build import Annotation
from .fuzzer import Snapshot
from .model import AnalystModel, LLMResult

FAILURE_CLASSES = {
    "known_relevant_state_values",   # interesting behavior is a data VALUE, not an edge
    "known_state_changes",           # log which operations fired as a proxy state
    "missing_intermediate_state",    # no gradient at all (magic bytes/checksum/hash)
}

# A faithful, answer-free cheatsheet of the IJON primitives (from afl-ijon-min.h).
IJON_REFERENCE = """\
IJON annotations reshape AFL's feedback function. Insert ONE C statement into
the target. Available primitives (all are macros; helpers combine values):

  IJON_SET(x)        Treat each distinct value of integer x as a new coverage
                     event (new value -> input saved). Use to expose a state
                     VALUE so the fuzzer explores its distinct values.
  IJON_INC(x)        Like IJON_SET but rewards how OFTEN a value occurs.
  IJON_MAX(x)        Maximize x via a separate hill-climbing map (e.g. push a
                     counter/coordinate as far as possible). IJON_MIN(x) too.
  IJON_STATE(n)      Add a 'virtual state' n to edge encoding: the SAME code
                     path counts as different coverage in different states.
  IJON_CMP(x,y)      Reward closeness of two ints (differing-bit count).
  IJON_STRDIST(a,b)  Reward growing the common prefix of two strings.

Helpers usable inside the above:
  ijon_hashint(old, val)  combine two integers into one hash (e.g. fold two
                          coordinates into a single value for IJON_SET).
  ijon_hashstr(old, s) / ijon_hashmem(old, p, len)  hash a string / buffer.

The annotation must reference only variables in scope at the chosen line."""

_SYSTEM = """\
You are a fuzzing analyst automating the IJON technique (Aschermann et al.,
"Exploring Deep State Spaces via Fuzzing"). An AFL++ campaign has plateaued:
mutation produces no new edge coverage, so the fuzzer is stuck. Edge coverage
is blind to interesting program state that lives in DATA rather than in which
branches execute. Your job: figure out what state matters, then add ONE tiny
IJON annotation that exposes it to the feedback function.

The paper's taxonomy of why coverage gets stuck:
- known_relevant_state_values: the interesting behavior is a specific data
  value (a coordinate, an index, an internal register), reachable by code that
  doesn't add new edges. Expose the value (IJON_SET/IJON_INC), or push it
  (IJON_MAX/IJON_MIN).
- known_state_changes: state is spread across operations; log which ones fired
  (e.g. message types) and expose that as a proxy (IJON_STATE / IJON_SET).
- missing_intermediate_state: a hard equality with no gradient (magic bytes,
  checksum, hash). Manufacture a gradient (IJON_CMP / IJON_STRDIST).

PRIMITIVE SELECTION (critical):
- To pass a hard equality `x == TARGET` or `x == y` (magic value, checksum,
  hash, tag), use IJON_CMP(x, TARGET) / IJON_CMP(x, y) — it rewards matching
  more bits, giving a gradient to equality. NEVER use IJON_SET/IJON_INC for
  this: exposing a wide value creates one map entry per distinct value, which
  floods (saturates) the coverage map and overwhelms the fuzzer.
- Use IJON_SET/IJON_INC only to explore a SMALL, bounded set of states (a
  position in a small grid, a message-type id) — never a full 32/64-bit word.
- Use IJON_MAX/IJON_MIN to push a value as far as possible (counters, depth,
  coordinates) when there is no single target value.
- Use IJON_STRDIST for string / byte-buffer prefix comparisons.
- Use IJON_STATE(n) to explore SEQUENCES or COMBINATIONS of operations — a
  protocol/message dispatcher or a state machine where the goal depends on which
  operations fired in what order, and per-operation handlers are already covered
  so coverage is blind to protocol depth. Set n to the current state (or a
  rolling log of processed operations); it folds that into the edge encoding so
  the same handler code counts as new coverage in each state, letting the fuzzer
  build up the required sequence. Place it where the state is updated, each step.

PLACEMENT RULES (critical): the annotation must run on EVERY relevant
execution and BEFORE/OUTSIDE the branch it is meant to help the fuzzer reach.
Insert it on the normal path right after the relevant state is computed/updated
-- never inside the success branch of the very condition you are trying to
satisfy (code there runs only once the goal is already reached, giving no
gradient). For a hard `if (a == b)` gate, the annotation belongs immediately
BEFORE that `if`, not inside its body. In a loop, place it inside the loop body
so it fires each iteration. `after_substring` selects the line to insert AFTER,
so pick the statement that precedes the gate.

ANCHOR RULES (critical -- a bad anchor makes the patch fail to COMPILE):
`after_substring` MUST identify an EXECUTABLE STATEMENT line that sits INSIDE a
function body. Your annotation is a C statement, so the line you insert it after
must be one where a following statement is legal AND your state variable is
already in scope and live. NEVER anchor on:
- a preprocessor directive or any line containing `#if`/`#ifdef`/`#ifndef`/
  `#define`/`#endif`/`#else` or a bare `defined(...)` -- inserting a statement
  there lands outside any function or splits a macro and will not compile;
- a function signature / opening-brace line, a variable or type DECLARATION, a
  `struct`/`enum` body, a label, or a comment line.
Prefer a line that ends in `;` or `{` within the function where the state is
computed. The variable in your annotation must be declared at or above that line
in the same function (e.g. a function parameter or a local already assigned).

EXISTING ANNOTATIONS: the target source may already contain IJON_* calls added
in earlier iterations. Treat them as working prior steps — never propose one
identical to what is already there. If the fuzzer is still stuck, the barrier
those annotations addressed has been passed; identify the NEXT roadblock
(usually a deeper/later condition the fuzzer can now reach) and annotate THAT.

Reply with ONLY a JSON object, no prose, no markdown, of this exact shape:
{
  "why_stuck": "<1-3 sentences: what behavior the fuzzer cannot reach and why edge coverage misses it>",
  "failure_class": "<one of: known_relevant_state_values | known_state_changes | missing_intermediate_state>",
  "relevant_state": "<the specific in-scope variable(s) that capture the interesting state>",
  "annotation": {
    "macro": "<IJON_SET | IJON_INC | IJON_MAX | IJON_MIN | IJON_STATE | IJON_CMP | IJON_STRDIST>",
    "code": "<one C statement to insert verbatim, e.g. SOMEMACRO(expr);>",
    "after_substring": "<an exact, unique substring of an EXECUTABLE STATEMENT line (inside a function body; NOT a #if/#define/declaration/comment) to insert the statement AFTER>",
    "placement_reason": "<why this location: the variables must be in scope and updated each relevant iteration>"
  }
}"""


_REPAIR_SYSTEM = """\
You are a C build-repair assistant. A fuzzing analyst chose an IJON annotation to
insert into a target; applying or compiling it FAILED. Your job is NARROW: return
a corrected annotation that COMPILES and is placed legally, while PRESERVING the
analyst's intent. You are an engineer fixing a colleague's patch, not a re-designer.

KEEP (do NOT change unless the error proves it is impossible here):
- the macro / primitive (IJON_CMP, IJON_STATE, IJON_SET, ...),
- the program state it tracks (the variable/expression being exposed),
- the analyst's failure_class and why_stuck.

FIX only the mechanics that caused the failure:
- PLACEMENT: `after_substring` must match an EXECUTABLE STATEMENT line INSIDE a
  function body where the referenced variables are in scope -- never a preprocessor
  line (#if/#define/#endif/defined(...)), a declaration, a function signature, a
  label, or a comment. If the statement landed in the wrong place, move the anchor
  to a real statement line near where the state is computed.
- SYNTAX: balance parentheses, add a needed cast, fix a typo, qualify a field
  (e.g. png_ptr->field), reference a variable that is actually in scope. Make the
  minimal edit that makes the compiler accept it.

Read the compiler error's file:line and message literally; it tells you what is
wrong. If the analyst's state genuinely cannot be referenced at any compilable
location in the shown source, return your closest valid attempt anyway -- the outer
loop will escalate back to the analyst.

Reply with ONLY a JSON object of the analyst's exact shape:
{
  "why_stuck": "<carry over unchanged>",
  "failure_class": "<carry over unchanged>",
  "relevant_state": "<carry over unchanged>",
  "annotation": {
    "macro": "<same primitive as the analyst chose>",
    "code": "<one corrected C statement to insert verbatim>",
    "after_substring": "<an exact substring of a real EXECUTABLE STATEMENT line to insert AFTER>",
    "placement_reason": "<what you changed and why it now compiles / is in scope>"
  }
}"""


@dataclass
class AnnotationProposal:
    why_stuck: str
    failure_class: str
    relevant_state: str
    annotation: Annotation
    macro: str
    placement_reason: str
    llm: LLMResult

    @property
    def summary(self) -> str:
        return (f"[{self.failure_class}] state={self.relevant_state!r} "
                f"-> {self.annotation.code!r} after {self.annotation.after_substring!r}")


def _telemetry_block(snap: Snapshot) -> str:
    return (
        f"- executions run: {snap.stats.get('execs_done')}\n"
        f"- edges covered: {snap.edges_found} of {snap.stats.get('total_edges')} "
        f"(flat; no growth)\n"
        f"- corpus size: {snap.corpus_count} inputs\n"
        f"- seconds since last new find: {snap.time_wo_finds}\n"
        f"- favored queue entries left to fuzz: {snap.pending_favs} "
        f"(0 = nothing left to try)\n"
        f"- crashes/goals reached: {snap.saved_crashes}\n")


def _history_block(history: list) -> str:
    """Render previously-tried annotations that did NOT help, so the model
    avoids repeating them. Each item is an AnnotationProposal plus an outcome
    note string, as (proposal, note) tuples."""
    if not history:
        return ""
    lines = ["PREVIOUS ATTEMPTS THIS SESSION THAT DID NOT HELP "
             "(do something different — change the primitive AND/OR the "
             "placement; address the stated reason):"]
    for i, (prop, note) in enumerate(history, 1):
        lines.append(
            f"  {i}. {prop.macro}: code `{prop.annotation.code}` inserted "
            f"after line `{prop.annotation.after_substring}` "
            f"-> {note}")
    return "\n".join(lines) + "\n\n"


def build_user_prompt(source: str, snap: Snapshot,
                      source_name: str = "target.c",
                      history: list = None,
                      localization: str = None) -> str:
    loc = ""
    if localization:
        loc = (f"LOCALIZATION (for a large multi-function target, this is where "
               f"the fuzzer is stuck — only the relevant functions' source is "
               f"shown below, not the whole program):\n{localization}\n\n")
    return (
        f"IJON PRIMITIVE REFERENCE:\n{IJON_REFERENCE}\n\n"
        f"FUZZER SITUATION (plateaued):\n{_telemetry_block(snap)}\n"
        f"{_history_block(history)}"
        f"{loc}"
        f"TARGET SOURCE ({source_name}):\n```c\n{source}\n```\n\n"
        f"Analyze why the fuzzer is stuck and propose exactly one IJON "
        f"annotation to break the plateau. The annotation will be inserted into "
        f"the source file that contains your chosen `after_substring` line; pick "
        f"a line that is unique and in scope. Respond with only the JSON object.")


def parse_proposal(obj: dict, llm: LLMResult) -> AnnotationProposal:
    if not obj:
        raise ValueError("empty proposal object")
    ann = obj.get("annotation") or {}
    code = (ann.get("code") or "").strip()
    after = ann.get("after_substring")
    if not code:
        raise ValueError("proposal missing annotation.code")
    if not after:
        raise ValueError("proposal missing annotation.after_substring")
    fc = obj.get("failure_class", "")
    if fc not in FAILURE_CLASSES:
        raise ValueError(f"unknown failure_class: {fc!r}")
    return AnnotationProposal(
        why_stuck=obj.get("why_stuck", ""),
        failure_class=fc,
        relevant_state=obj.get("relevant_state", ""),
        annotation=Annotation(code=code, after_substring=after),
        macro=ann.get("macro", ""),
        placement_reason=ann.get("placement_reason", ""),
        llm=llm,
    )


def propose_annotation(model: AnalystModel, source: str, snap: Snapshot,
                       source_name: str = "target.c",
                       history: list = None,
                       localization: str = None,
                       max_tokens: int = 8192) -> AnnotationProposal:
    # v4-pro is a reasoning model: its chain-of-thought (reasoning_content) is
    # billed as completion tokens, so on a big multi-function source the reasoning
    # alone can approach 4096 and truncate the JSON answer -> empty/partial object.
    # 8192 leaves headroom; cheap targets simply stop early and cost less.
    user = build_user_prompt(source, snap, source_name, history, localization)
    res = model.complete_json(_SYSTEM, user, max_tokens=max_tokens)
    return parse_proposal(res.obj, res)


def build_repair_prompt(source: str, failed: Annotation, error: str,
                        macro: str, relevant_state: str,
                        source_name: str = "target.c",
                        patched_region: str = None,
                        localization: str = None) -> str:
    loc = f"LOCALIZATION:\n{localization}\n\n" if localization else ""
    region = (f"WHERE YOUR STATEMENT LANDED (the patched region as it failed):\n"
              f"```c\n{patched_region}\n```\n\n") if patched_region else ""
    return (
        f"IJON PRIMITIVE REFERENCE:\n{IJON_REFERENCE}\n\n"
        f"THE ANALYST'S ANNOTATION THAT FAILED:\n"
        f"  macro          : {macro}\n"
        f"  state to expose : {relevant_state}\n"
        f"  code           : {failed.code}\n"
        f"  after_substring : {failed.after_substring!r}\n\n"
        f"BUILD / PLACEMENT ERROR:\n{error}\n\n"
        f"{region}"
        f"{loc}"
        f"TARGET SOURCE ({source_name}):\n```c\n{source}\n```\n\n"
        f"Return a corrected annotation -- SAME macro and SAME state -- that "
        f"compiles and is placed on a legal executable statement line. Respond with "
        f"only the JSON object.")


def repair_annotation(model: AnalystModel, source: str,
                      failed: AnnotationProposal, error: str,
                      source_name: str = "target.c",
                      patched_region: str = None,
                      localization: str = None,
                      max_tokens: int = 8192) -> AnnotationProposal:
    """Mechanical fix of a non-building annotation. Preserves the analyst's
    strategy (macro/state/failure_class are carried over, not re-decided)."""
    user = build_repair_prompt(source, failed.annotation, error, failed.macro,
                               failed.relevant_state, source_name,
                               patched_region, localization)
    res = model.complete_json(_REPAIR_SYSTEM, user, max_tokens=max_tokens)
    obj = dict(res.obj or {})
    # enforce "repair does not change strategy": carry the analyst's fields over.
    obj["failure_class"] = failed.failure_class
    obj.setdefault("why_stuck", failed.why_stuck)
    obj.setdefault("relevant_state", failed.relevant_state)
    return parse_proposal(obj, res)
