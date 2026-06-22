"""The build-doctor: a THIRD agent role for IJON-Reloaded (Mode 2, standalone).

Two roles already exist (harness/agent.py): the ANALYST (designs the IJON
annotation) and the annotation-REPAIR agent (makes a non-compiling annotation
build). The build-doctor is orthogonal: it makes the *target itself* build. Given
a draft build.sh (from scripts/bringup.py) that fails to compile/link the IJON
harness, it reads the compiler/linker error and edits the LIBRARY-SPECIFIC slots
of build.sh (-I/-L/-l, configure/cmake flags, paths) until the build is green --
while PRESERVING the invariant AFL/IJON/ASAN scaffolding it must never touch.

This is the headless/API counterpart to what Claude Code does interactively in
Mode 1 (the `ijon-reloaded` skill). Same job, different driver.

The driver lives in scripts/build_doctor.py; this module is the prompt + the call.
"""
from __future__ import annotations

from harness.model import AnalystModel

# Tokens that MUST survive a build-doctor edit -- the IJON wiring is the whole
# point; if a "fix" drops it the IJON binary would silently equal plain. The
# driver hard-checks these and rejects an edit that removes any (see apply guard).
INVARIANT_TOKENS = ("afl-clang-fast", "AFL_LLVM_IJON", "IJON_OUT")

_BUILD_DOCTOR_SYSTEM = """\
You are a build-doctor for an AFL++/IJON fuzzing setup. A draft `build.sh` is
supposed to (1) build a C/C++ library instrumented with afl-clang-fast, and (2)
compile + link a fuzz harness against it -- a `plain` control variant and an
`agent` variant that adds the IJON instrumentation pass (AFL_LLVM_IJON=1). The
build is FAILING. Your job: edit build.sh so it builds, and return the COMPLETE
corrected file.

PRESERVE (do not remove or weaken -- this is the IJON wiring, the entire purpose):
- afl-clang-fast / afl-clang-fast++ as the compiler.
- AFL_LLVM_IJON=1 on the `agent` variant (and the IJON pass relink), and IJON_OUT.
- AFL_PATH semantics: the macros come from $AFL_ROOT/include (set in the env).
- ASAN is ALL-OR-NOTHING: if any binary uses -fsanitize=address, every binary
  linking the library must too (or you get undefined __asan_report_* references).
- The variant dispatch (the `case "$1"` block) and the plain/agent structure.

FIX only library-specific slots that the error points at:
- include paths (-I): missing generated config headers (often in the BUILD dir,
  e.g. cmake's binary dir) or internal/public headers the harness #includes.
- link deps (-l) and search paths (-L): an "undefined reference" usually means a
  missing -l<dep>; "cannot find -l<x>" means a wrong/absent lib.
- configure/cmake flags: pin/disable optional features for a deterministic, dep-
  free link (TLS backends, tests, examples, docs); avoid features that pull in
  missing host libs.
- wrong paths to the source tree, the static lib (lib*.a), or the harness.

Read the error's file:line and message literally. Make the MINIMAL change that
addresses THIS error; do not refactor. If the same error persists across rounds,
try a different hypothesis (a different -I dir, a different dep) rather than
repeating.

Reply with ONLY a JSON object:
{
  "diagnosis": "<1-3 sentences: what the error means and the one change you are making>",
  "fixed_build_sh": "<the COMPLETE corrected build.sh, verbatim, ready to write to disk>"
}"""


def build_doctor_prompt(build_sh: str, variant: str, error: str,
                        repo_facts: str, history: list = None) -> str:
    hist = ""
    if history:
        hist = ("EDITS ALREADY TRIED THIS SESSION (did not fix it — do something "
                "different):\n" + "\n".join(f"  {i}. {d}" for i, d in
                                            enumerate(history, 1)) + "\n\n")
    return (
        f"BUILD FACTS (probed from the source tree):\n{repo_facts}\n\n"
        f"{hist}"
        f"THE COMMAND THAT FAILED:  bash build.sh {variant}\n\n"
        f"BUILD ERROR (captured stderr/stdout tail):\n{error}\n\n"
        f"CURRENT build.sh:\n```bash\n{build_sh}\n```\n\n"
        f"Return the corrected complete build.sh as JSON. Preserve the AFL/IJON/"
        f"ASAN scaffolding; change only what THIS error requires.")


def repair_build(model: AnalystModel, build_sh: str, variant: str, error: str,
                 repo_facts: str, history: list = None,
                 max_tokens: int = 8192) -> dict:
    """Ask the build-doctor for a corrected build.sh. Returns
    {"diagnosis": str, "fixed_build_sh": str}. Raises ValueError if the model's
    reply is missing the file or drops an INVARIANT_TOKEN (the IJON wiring)."""
    user = build_doctor_prompt(build_sh, variant, error, repo_facts, history)
    res = model.complete_json(_BUILD_DOCTOR_SYSTEM, user, max_tokens=max_tokens)
    obj = res.obj or {}
    fixed = (obj.get("fixed_build_sh") or "").strip()
    if not fixed:
        raise ValueError("build-doctor returned no fixed_build_sh")
    missing = [t for t in INVARIANT_TOKENS if t not in fixed]
    if missing:
        raise ValueError(f"build-doctor dropped invariant IJON wiring {missing}; "
                         f"refusing the edit")
    return {"diagnosis": obj.get("diagnosis", ""), "fixed_build_sh": fixed,
            "raw": res}
