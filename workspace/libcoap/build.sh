#!/usr/bin/env bash
# Reproducible build of the libcoap CoAP-PDU-parse target (CMake; libtool absent
# on this host so the autotools path is avoided). Builds libcoap (static, no TLS)
# instrumented with AFL++, then the OSS-Fuzz pdu_parse_udp harness.
#
# Variants (for the generic runner, scripts/run_target.py):
#   ./build.sh plain   refresh + (re)link the plain control target
#   ./build.sh agent   recompile the on-disk-patched library under IJON and
#                      relink the harness -> $IJON_OUT  (env: IJON_OUT)
# The llvm-cov variant is a separate script: ./cov-build.sh
# Clone + CMake build tree go in ./build/ (gitignored).
set -euo pipefail
export TMPDIR="${TMPDIR:-/tmp}"; mkdir -p "$TMPDIR"
AFL="${AFL_ROOT:?set AFL_ROOT to your AFL++ (with IJON) build}"
export PATH="$AFL:$PATH"   # afl-clang-fast on PATH; keep AFL_PATH=$AFL_ROOT/include in env
WS="$(cd "$(dirname "$0")" && pwd)"
BUILD="$WS/build"; SRC="$BUILD/libcoap"; CB="$BUILD/build_coap"
mkdir -p "$BUILD" "$WS/targets" "$WS/in"

ASAN="-fsanitize=address"
INC="-I$CB -I$CB/include -I$SRC/include"          # generated coap_config.h/coap_defines.h + public/internal headers
HARNESS="$WS/src/coap_pdu_fuzzer.c"

static_lib() { find "$CB" -name 'libcoap-3*.a' 2>/dev/null | head -1; }

ensure_src() {
  [ -d "$SRC" ] || git clone --depth 1 -b develop https://github.com/obgm/libcoap.git "$SRC"
}

cmake_configure() {
  # static, no DTLS/OSCORE/tests/examples/docs -> core CoAP only, no extra -l deps.
  cmake -G Ninja -S "$SRC" -B "$CB" \
    -DCMAKE_C_COMPILER=afl-clang-fast \
    -DCMAKE_C_FLAGS="-g -O1 $ASAN" \
    -DENABLE_DTLS=OFF -DENABLE_OSCORE=OFF \
    -DENABLE_TESTS=OFF -DENABLE_EXAMPLES=OFF -DENABLE_DOCS=OFF \
    -DBUILD_SHARED_LIBS=OFF >/dev/null
}

ensure_lib() {
  ensure_src
  [ -f "$CB/build.ninja" ] || cmake_configure
}

build_plain() {
  ensure_lib
  # Refresh any library source the loop annotated then restored to pristine back
  # to a PLAIN object, so the control is never contaminated. AFL_LLVM_IJON unset.
  unset AFL_LLVM_IJON || true
  AFL_QUIET=1 ninja -C "$CB" coap-3 >/dev/null
  AFL_QUIET=1 afl-clang-fast -g -O1 $ASAN -fsanitize=fuzzer $INC \
    "$HARNESS" "$(static_lib)" -o "$WS/targets/coap_pdu_plain"
  echo "built: targets/coap_pdu_plain"
}

build_describe() {
  ensure_lib
  # class-2 metric tool: decodes each input via libcoap's public API and prints
  # its (type, code, option-number sequence). NOT IJON-instrumented; plain lib.
  # ASAN must match the library (all-or-nothing).
  unset AFL_LLVM_IJON || true
  AFL_QUIET=1 ninja -C "$CB" coap-3 >/dev/null
  AFL_QUIET=1 afl-clang-fast -g -O1 $ASAN $INC \
    "$WS/src/coap_pdu_describe.c" "$(static_lib)" -o "$WS/targets/coap_pdu_describe"
  echo "built: targets/coap_pdu_describe"
}

build_agent() {
  : "${IJON_OUT:?build.sh agent needs IJON_OUT (the output target path)}"
  # The runner has written the annotation into a library .c on disk; recompile the
  # library under the IJON pass (ninja rebuilds the changed file), then relink the
  # harness (also under IJON, so a harness-landed annotation is instrumented too).
  export AFL_LLVM_IJON=1 AFL_QUIET=1
  ninja -C "$CB" coap-3 >/dev/null
  afl-clang-fast -g -O1 $ASAN -fsanitize=fuzzer $INC \
    "$HARNESS" "$(static_lib)" -o "$IJON_OUT"
  echo "built: $IJON_OUT (AFL+IJON, annotated library)"
}

case "${1:-all}" in
  all|plain) build_plain ;;
  describe)  build_describe ;;
  agent)     build_agent ;;
  *) echo "unknown variant: $1 (use: plain|describe|agent)" >&2; exit 2 ;;
esac
