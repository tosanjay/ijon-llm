#!/usr/bin/env bash
# Source-coverage (llvm-cov) variant of the libcoap PDU harness, for replaying the
# AFL corpus to get source-level coverage (the localization lens + the keep/revert
# reward). Clean clang (NO AFL/IJON) + -fprofile-instr-generate -fcoverage-mapping.
# libFuzzer driver (-fsanitize=fuzzer) so `./bin <files...>` replays them.
set -euo pipefail
export TMPDIR="${TMPDIR:-/tmp}"; mkdir -p "$TMPDIR"
LLVM="${LLVM_BIN:?set LLVM_BIN to your LLVM bin dir}"
export PATH="$LLVM:$PATH"
WS="$(cd "$(dirname "$0")" && pwd)"
BUILD="$WS/build"; SRC="$BUILD/libcoap"; COVB="$BUILD/build_cov"
mkdir -p "$WS/targets"
COVFLAGS="-fprofile-instr-generate -fcoverage-mapping -g -O0"
INC="-I$COVB -I$COVB/include -I$SRC/include"
HARNESS="$WS/src/coap_pdu_fuzzer.c"

cov_static_lib() { find "$COVB" -name 'libcoap-3*.a' 2>/dev/null | head -1; }

[ -d "$SRC" ] || { echo "run build.sh plain first (clones libcoap)"; exit 1; }
[ -f "$COVB/build.ninja" ] || cmake -G Ninja -S "$SRC" -B "$COVB" \
    -DCMAKE_C_COMPILER=clang -DCMAKE_C_FLAGS="$COVFLAGS" \
    -DENABLE_DTLS=OFF -DENABLE_OSCORE=OFF \
    -DENABLE_TESTS=OFF -DENABLE_EXAMPLES=OFF -DENABLE_DOCS=OFF \
    -DBUILD_SHARED_LIBS=OFF >/dev/null
ninja -C "$COVB" coap-3 >/dev/null
clang $COVFLAGS -fsanitize=fuzzer $INC "$HARNESS" "$(cov_static_lib)" \
  -o "$WS/targets/coap_pdu_cov"
echo "built: targets/coap_pdu_cov (llvm-cov coverage build)"
