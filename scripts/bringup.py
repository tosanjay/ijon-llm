#!/usr/bin/env python3
"""One-time build bring-up assistant (Phase 1: deterministic, no LLM).

Writing the per-library build.sh by hand is the fiddly part of onboarding a new
target: the AFL/IJON/ASAN wiring plus the right -I/-L/-l and configure flags. The
AFL/IJON/ASAN structure is INVARIANT across targets, so we template it (the variant
layout, the AFL_PATH gotcha, the ASAN-match rule are baked in, not asked of you).
Only the library-specific slots are filled by probing the cloned source:

  - build system        (autotools / cmake / meson / make)
  - OSS-Fuzz harness     (contrib/oss-fuzz/*.cc etc. -- the best starting harness)
  - configure options    (./configure --help -> --without-*/--disable-* to pin deps)
  - link deps            (*.pc.in Requires.private / Libs.private -> -l flags)

The output is a DRAFT build.sh + target.json: the scaffolding is correct by
construction; every uncertain library-specific slot is marked `# TODO(verify)`.
Phase 2 (the LLM build-doctor) will iterate the draft against real build errors.

Usage:
    python3 scripts/bringup.py --lib /path/to/cloned/libxyz --name libxyz \
        --mode library --reward coverage [--harness path/to/harness.cc]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

# Requires.private name -> linker flag (best-effort; falls back to -l<name>).
PKG_TO_LIB = {
    "zlib": "-lz", "liblzma": "-llzma", "lzma": "-llzma", "libzstd": "-lzstd",
    "zstd": "-lzstd", "bzip2": "-lbz2", "liblz4": "-llz4", "libxml-2.0": "-lxml2",
    "openssl": "-lcrypto", "libcrypto": "-lcrypto", "libb2": "-lb2", "libm": "-lm",
}


# --------------------------------------------------------------------------- #
#  Probes (all best-effort; never raise)                                       #
# --------------------------------------------------------------------------- #
def detect_build_system(src: Path) -> str:
    if (src / "configure").exists() or (src / "configure.ac").exists() \
            or (src / "autogen.sh").exists():
        return "autotools"
    if (src / "CMakeLists.txt").exists():
        return "cmake"
    if (src / "meson.build").exists():
        return "meson"
    if (src / "Makefile").exists() or (src / "Makefile.in").exists():
        return "make"
    return "unknown"


def find_oss_fuzz(src: Path) -> dict:
    """Locate fuzz harnesses + an OSS-Fuzz build.sh. A harness is identified by
    DEFINING the libFuzzer entrypoint `LLVMFuzzerTestOneInput`, not by its file
    name -- libcoap names them *_target.c, others *_fuzzer.cc, etc. This both
    catches the real harnesses and excludes helper files that merely live in the
    fuzz dir (e.g. libcoap's coap_fuzz_helper.c)."""
    exts = (".c", ".cc", ".cpp", ".cxx")
    # prefer dirs whose name signals fuzzing (tests/oss-fuzz, fuzz, ...); also grab
    # any build.sh there as the OSS-Fuzz starting point. Fall back to the whole tree.
    fuzz_dirs = [d for d in src.rglob("*")
                 if d.is_dir() and any(k in d.name.lower()
                                       for k in ("fuzz", "oss-fuzz"))]
    buildsh = None
    for d in fuzz_dirs:
        bs = list(d.glob("build.sh"))
        buildsh = buildsh or (bs[0] if bs else None)
    search_dirs = fuzz_dirs or [src]
    cand = {p for d in search_dirs for p in d.rglob("*") if p.suffix in exts}

    def is_harness(p: Path) -> bool:
        try:
            return "LLVMFuzzerTestOneInput" in p.read_text(errors="replace")
        except Exception:
            return False
    harnesses = [p for p in sorted(cand) if is_harness(p)]
    if not harnesses:                      # rare: no entrypoint found -> name-match
        pats = ("fuzz", "_target", "harness")
        harnesses = sorted({p for p in cand
                            if any(k in p.name.lower() for k in pats)})
    return {"harnesses": sorted(set(harnesses)), "buildsh": buildsh}


def scan_configure(src: Path) -> dict:
    """Run `./configure --help` (no side effects) for --without-*/--disable-*
    options and static-build support. Falls back to scanning configure.ac."""
    out = {"without": [], "disable": [], "static": False, "shared": False}
    text = ""
    cfg = src / "configure"
    if cfg.exists():
        try:
            r = subprocess.run(["./configure", "--help"], cwd=str(src),
                               capture_output=True, text=True, timeout=30)
            text = r.stdout + r.stderr
        except Exception:
            text = ""
    if not text:
        ac = src / "configure.ac"
        text = ac.read_text(errors="replace") if ac.exists() else ""
    def real(opts):  # drop autoconf generic-help placeholders (--without-PACKAGE etc.)
        return sorted({o for o in opts if not o.split("-")[-1].isupper()})
    out["without"] = real(re.findall(r"--without-[A-Za-z0-9_-]+", text))
    out["disable"] = real(re.findall(r"--disable-[A-Za-z0-9_-]+", text))
    out["static"] = "--enable-static" in text
    out["shared"] = "--disable-shared" in text
    return out


def scan_pc_deps(src: Path, name: str) -> list:
    """External link deps from a pkg-config *.pc.in: only the PRIVATE deps
    (Requires.private / Libs.private), never the public `Libs:` (that is the
    library's own -l, which the static .a already provides)."""
    core = name[3:] if name.startswith("lib") else name           # libpng -> png
    libs = []
    for pc in list(src.rglob("*.pc.in")) + list(src.rglob("*.pc")):
        txt = pc.read_text(errors="replace")
        for m in re.findall(r"^Requires\.private:\s*(.+)$", txt, re.MULTILINE):
            for dep in re.split(r"[\s,]+", m.strip()):
                dep = re.sub(r"[<>=].*", "", dep)                 # drop version constraints
                if dep and "@" not in dep:
                    libs.append(PKG_TO_LIB.get(dep, f"-l{dep}"))
        for m in re.findall(r"^Libs\.private:\s*(.+)$", txt, re.MULTILINE):
            libs += [t for t in m.split() if t.startswith("-l") and "@" not in t]
        if libs:
            break                                                 # first .pc wins
    seen, out = set(), []
    for l in libs:
        if l in seen or l in (f"-l{core}", f"-l{name}"):          # drop self + dups
            continue
        seen.add(l); out.append(l)
    return out


# --------------------------------------------------------------------------- #
#  Render                                                                       #
# --------------------------------------------------------------------------- #
def render_build_sh(p: dict) -> str:
    cc = "afl-clang-fast++" if p["cxx"] else "afl-clang-fast"
    fuzzer = " -fsanitize=fuzzer"
    name = p["name"]
    without_hint = ("   # candidates to pin (the deterministic-link lesson): "
                    + " ".join(p["without"][:12])) if p["without"] else \
                   "   # (no --without-* options detected)"
    confflags = "--disable-shared --enable-static" if p["static"] else ""
    oss_comment = (f'\n                                    # starting point: '
                   f'$SRC/{p["oss_rel"]} (copy + adapt into HARNESS above)') \
                  if p.get("oss_rel") else ""

    if p["mode"] == "library":
        agent = f'''build_agent() {{
  : "${{IJON_OUT:?build.sh agent needs IJON_OUT}}"
  # The runner has written the annotation into a library .c on disk; recompile the
  # library under the IJON pass (it instruments the changed file), then relink.
  export AFL_LLVM_IJON=1 AFL_QUIET=1
  make -j4 -C "$SRC" >/dev/null
  {cc} -g -O1 $ASAN{fuzzer} $INC "$HARNESS" "$(static_lib)" $LINKLIBS -o "$IJON_OUT"
  echo "built: $IJON_OUT (AFL+IJON, annotated library)"
}}'''
        plain_refresh = ('  unset AFL_LLVM_IJON || true\n'
                         '  AFL_QUIET=1 make -j4 -C "$SRC" >/dev/null   '
                         '# refresh restored sources to PLAIN objects\n')
    else:  # harness mode
        agent = f'''build_agent() {{
  : "${{IJON_HARNESS:?}}"; : "${{IJON_OUT:?}}"
  # Harness mode: the library is prebuilt; compile the patched harness under IJON.
  AFL_LLVM_IJON=1 AFL_QUIET=1 {cc} -g -O1 $ASAN{fuzzer} $INC "$IJON_HARNESS" \\
    "$(static_lib)" $LINKLIBS -o "$IJON_OUT"
  echo "built: $IJON_OUT (AFL+IJON harness)"
}}'''
        plain_refresh = ""

    return f'''#!/usr/bin/env bash
# GENERATED DRAFT by scripts/bringup.py -- review every `# TODO(verify)` line.
# The AFL/IJON/ASAN scaffolding below is correct by construction; the
# library-specific slots are best-effort probes. Build system: {p["buildsystem"]}.
set -euo pipefail
export TMPDIR="${{TMPDIR:-/tmp}}"; mkdir -p "$TMPDIR"
AFL="${{AFL_ROOT:?set AFL_ROOT to your AFL++ (with IJON) build}}"
export PATH="$AFL:$PATH"   # afl-clang-fast on PATH; keep AFL_PATH=$AFL_ROOT/include in your env
WS="$(cd "$(dirname "$0")" && pwd)"
BUILD="$WS/build"; SRC="$BUILD/{name}"
mkdir -p "$BUILD" "$WS/targets"

# --- library-specific slots (probed; verify) -------------------------------
REPO_URL="{p["repo_url"]}"          # TODO(verify)
REPO_TAG="{p["repo_tag"]}"          # TODO(verify)
CONFFLAGS="{confflags}"             # TODO(verify): pin optional deps for a deterministic link
{without_hint}
ASAN="-fsanitize=address"           # all-or-nothing: keep on EVERY binary that links the lib
LINKLIBS="{p["linklibs"]}"          # TODO(verify): extra -l deps (from *.pc.in)
HARNESS="$WS/{p["harness"]}"        # TODO(verify): your harness source{oss_comment}
INC="-I$SRC"                        # TODO(verify): add -I for generated/config headers + deps
# ---------------------------------------------------------------------------

static_lib() {{ find "$SRC" -name 'lib*.a' -path '*.libs*' 2>/dev/null | head -1; }}

ensure_src() {{
  [ -d "$SRC" ] || git clone --depth 1 -b "$REPO_TAG" "$REPO_URL" "$SRC"
  [ -x "$SRC/configure" ] || ( cd "$SRC" && [ -x ./autogen.sh ] && ./autogen.sh ) || true
}}

ensure_lib() {{
  ensure_src
  [ -n "$(static_lib)" ] || ( cd "$SRC" && \\
    CC=afl-clang-fast CFLAGS="-g -O1 $ASAN" ./configure $CONFFLAGS && AFL_QUIET=1 make -j4 )
}}

build_plain() {{
  ensure_lib
{plain_refresh}  AFL_QUIET=1 {cc} -g -O1 $ASAN{fuzzer} $INC "$HARNESS" "$(static_lib)" $LINKLIBS \\
    -o "$WS/targets/{name}_plain"
  echo "built: targets/{name}_plain"
}}

{agent}

case "${{1:-all}}" in
  all|plain) build_plain ;;
  agent)     build_agent ;;
  # TODO(reward): add `describe` (diversity reward) or `cov` (coverage reward) here.
  #   describe: compile your src/{name}_describe.c (same ASAN) -> targets/{name}_describe
  #   cov:      clang + -fprofile-instr-generate -fcoverage-mapping (see workspace/libpng/cov-build.sh)
  *) echo "unknown variant: $1 (use: all|plain|agent)" >&2; exit 2 ;;
esac
'''


def render_target_json(p: dict) -> str:
    d = {
        "name": p["name"],
        "source_name": f"{p['name']} read path  # TODO(verify)",
        "annotate": p["mode"],
        "harness": p["harness"],
        "seeds": "in",
        "reward": p["reward"],
        "build": {"plain": ["bash", "build.sh", "plain"],
                  "agent": ["bash", "build.sh", "agent"]},
        "targets": {"plain": f"targets/{p['name']}_plain",
                    "agent": f"targets/{p['name']}_agent"},
    }
    if p["mode"] == "library":
        d["library_src"] = "build/" + p["name"]
        d["localize"] = {"fi": "fi_out/TODO.data.yaml  # fuzz-introspector output",
                         "cov": "build/cov/coverage.json  # llvm-cov of the plain corpus"}
    else:
        d["focus"] = [p["harness"]]
    if p["reward"] == "diversity":
        d["describe"] = f"targets/{p['name']}_describe"
        d["build"]["describe"] = ["bash", "build.sh", "describe"]
    else:
        d["build"]["cov"] = ["bash", "cov-build.sh"]
        d["targets"]["cov"] = f"targets/{p['name']}_cov"
    return json.dumps(d, indent=2)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True, help="path to the cloned library source")
    ap.add_argument("--name", default=None, help="short target name (default: dir basename)")
    ap.add_argument("--mode", choices=["library", "harness"], default="library")
    ap.add_argument("--reward", choices=["coverage", "diversity"], default="coverage")
    ap.add_argument("--harness", default=None,
                    help="harness to use: a file path, OR a substring of a discovered "
                         "candidate (e.g. --harness pdu_parse_udp). Default: a weak "
                         "name heuristic -- prefer choosing explicitly.")
    ap.add_argument("--list-harnesses", action="store_true",
                    help="list the harness candidates found in --lib and exit")
    ap.add_argument("--repo-url", default="TODO_REPO_URL")
    ap.add_argument("--repo-tag", default="TODO_TAG")
    ap.add_argument("--out", default=None, help="workspace dir to write (default: workspace/<name>)")
    args = ap.parse_args()

    src = Path(args.lib).resolve()
    if not src.is_dir():
        print(f"error: {src} is not a directory"); return 2
    name = args.name or src.name.split("-")[0]

    of = find_oss_fuzz(src)
    if args.list_harnesses:
        print(f"=== harness candidates in {src} (define LLVMFuzzerTestOneInput) ===")
        for i, h in enumerate(of["harnesses"], 1):
            print(f"  {i:2d}. {h.relative_to(src)}")
        if not of["harnesses"]:
            print("  (none found)")
        print("\nPick one:  --harness <path-or-substring>   e.g. --harness pdu_parse_udp")
        return 0

    bs = detect_build_system(src)
    cfg = scan_configure(src) if bs == "autotools" else {"without": [], "static": False}
    deps = scan_pc_deps(src, name)

    # Resolve the chosen harness SOURCE file in the lib tree. Explicit --harness
    # (a path, or a unique substring of a candidate) wins; otherwise a WEAK name
    # heuristic that often guesses wrong (so we print all candidates + how to override).
    core = name[3:] if name.startswith("lib") else name

    def resolve_harness(spec):
        if not spec:
            return None
        p = Path(spec)
        if p.is_file():
            return p.resolve()
        matches = [h for h in of["harnesses"] if spec in str(h)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(f"error: --harness '{spec}' matches {len(matches)} candidates; "
                  f"be more specific:")
            for h in matches:
                print(f"   {h.relative_to(src)}")
            raise SystemExit(2)
        print(f"error: --harness '{spec}' matched no file or candidate; "
              f"run --list-harnesses")
        raise SystemExit(2)

    chosen = resolve_harness(args.harness)
    if chosen is None:                      # weak heuristic default
        chosen = next((h for h in of["harnesses"] if core in h.name.lower()),
                      of["harnesses"][0] if of["harnesses"] else None)
    try:
        oss_rel = str(chosen.relative_to(src)) if chosen else None
    except ValueError:
        oss_rel = str(chosen) if chosen else None
    ext = chosen.suffix if chosen else ".c"
    cxx = ext in (".cc", ".cpp", ".cxx")
    harness = f"src/{name}_fuzzer{ext}"

    params = {
        "name": name, "mode": args.mode, "reward": args.reward,
        "buildsystem": bs, "cxx": cxx,
        "without": cfg.get("without", []), "static": cfg.get("static", False),
        "linklibs": " ".join(deps), "harness": harness, "oss_rel": oss_rel,
        "repo_url": args.repo_url, "repo_tag": args.repo_tag,
    }

    out = Path(args.out) if args.out else (Path("workspace") / name)
    out.mkdir(parents=True, exist_ok=True)
    bsh = out / "build.sh.draft"
    tj = out / "target.json.draft"
    bsh.write_text(render_build_sh(params)); bsh.chmod(0o755)
    tj.write_text(render_target_json(params))

    # Copy the chosen harness into the workspace so it is present and ready to
    # adapt (the HARNESS slot points at this copy, not a TODO placeholder).
    copied = False
    if chosen and chosen.is_file():
        (out / "src").mkdir(parents=True, exist_ok=True)
        (out / harness).write_text(chosen.read_text(errors="replace"))
        copied = True

    explicit = bool(args.harness)
    print(f"=== bring-up probe: {name} ({src}) ===")
    print(f"  build system : {bs}")
    print(f"  OSS-Fuzz     : {len(of['harnesses'])} harness candidate(s)"
          + (f"; build.sh at {of['buildsh'].relative_to(src.parent)}" if of["buildsh"] else ""))
    for i, h in enumerate(of["harnesses"], 1):
        mark = " <- chosen" if (chosen and h == chosen) else ""
        print(f"                 {i:2d}. {h.relative_to(src)}{mark}")
    pick = "explicit (--harness)" if explicit else "WEAK name heuristic — verify / override with --harness"
    print(f"  chosen harness: {oss_rel}  [{pick}]")
    print(f"                  -> copied into {harness}  ({'C++' if cxx else 'C'})"
          if copied else f"                  (HARNESS slot: {harness} — fill it)")
    print(f"  configure     : static={cfg.get('static')}  "
          f"{len(cfg.get('without', []))} --without-* options")
    print(f"  link deps     : {' '.join(deps) or '(none found — verify *.pc.in)'}")
    print(f"\n  wrote DRAFTS (review the TODO(verify) lines, then drop the .draft suffix):")
    print(f"    {bsh}")
    print(f"    {tj}")
    print(f"\n  Still on you: confirm REPO_URL/TAG, pin CONFFLAGS, the harness, seeds,")
    print(f"  and (library mode) generate fi_out/ + build/cov for `localize`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
