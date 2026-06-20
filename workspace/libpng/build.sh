#!/usr/bin/env bash
# Reproducible build of the libpng CRC-roadblock target (M4 first real target).
#
# Builds zlib + libpng instrumented with AFL++, then the CRC-enforced fuzz
# harness (src/libpng_crc_fuzzer.cc -> PNG_CRC_ERROR_QUIT). Also builds the
# CRC-disabled contrast target for quantifying the roadblock.
#
# Variants (for the generic runner, scripts/run_target.py):
#   ./build.sh             full build (clone + zlib + libpng + plain targets)
#   ./build.sh plain       refresh + (re)link the plain control target
#   ./build.sh agent       recompile the on-disk-patched library under IJON and
#                          relink the harness -> $IJON_OUT  (env: IJON_OUT)
# The llvm-cov variant is a separate script: ./cov-build.sh
# Scratch/clone tree goes in ./build/ (gitignored)
set -euo pipefail

export TMPDIR="${TMPDIR:-/tmp}"; mkdir -p "$TMPDIR"
AFL="${AFL_ROOT:?set AFL_ROOT to your AFL++ (with IJON) build}"
export PATH="$AFL:$PATH"
WS="$(cd "$(dirname "$0")" && pwd)"
BUILD="$WS/build"; ZINST="$BUILD/zlib/install"; LIBPNG="$BUILD/libpng"
mkdir -p "$BUILD" "$WS/targets" "$WS/in"

INC="-I$LIBPNG -I$ZINST/include"
LIBS="$LIBPNG/.libs/libpng16.a $ZINST/lib/libz.a"

ensure_libs() {
  # 1. sources (pinned)
  [ -d "$BUILD/zlib" ] || git clone --depth 1 -b v1.3.1  https://github.com/madler/zlib.git    "$BUILD/zlib"
  [ -d "$LIBPNG" ]     || git clone --depth 1 -b v1.6.58 https://github.com/pnggroup/libpng.git "$LIBPNG"
  # NOTE: the recorded experimental numbers (CRC A/B, frontier localization, the
  # chunk-sequence-diversity result) were measured on libpng 1.6.44; the pin is
  # now the latest 1.6.58. The roadblock mechanisms (per-chunk CRC, chunk
  # dispatch) are unchanged across 1.6.x, so the conclusions hold.

  # 2. zlib (instrumented, static)
  [ -f "$ZINST/lib/libz.a" ] || \
    ( cd "$BUILD/zlib" && CC=afl-clang-fast ./configure --static --prefix="$ZINST" && \
      AFL_QUIET=1 make -j4 && make install )

  # 3. libpng (instrumented, static)
  [ -f "$LIBPNG/.libs/libpng16.a" ] || \
    ( cd "$LIBPNG" && CC=afl-clang-fast CPPFLAGS="-I$ZINST/include" LDFLAGS="-L$ZINST/lib" \
      ./configure --disable-shared --enable-static && AFL_QUIET=1 make -j4 )

  # 4. seeds from libpng's own test suite (if not present)
  for s in basn0g01 basn2c08 basn6a08; do
    [ -f "$WS/in/$s.png" ] || cp "$LIBPNG/contrib/pngsuite/$s.png" "$WS/in/"
  done
  mkdir -p "$WS/in_single"; cp -n "$WS/in/basn0g01.png" "$WS/in_single/" || true
}

build_plain() {
  ensure_libs
  # Refresh any source the loop annotated then restored to pristine, back to a
  # PLAIN (non-IJON) object, so the control is never contaminated by a prior
  # agent build. AFL_LLVM_IJON must be unset (it enables on getenv != NULL).
  unset AFL_LLVM_IJON || true
  AFL_QUIET=1 make -j4 -C "$LIBPNG" >/dev/null

  # 5. targets: CRC enforced (roadblock) and CRC disabled (contrast)
  AFL_QUIET=1 afl-clang-fast++ -g -O2 -fsanitize=fuzzer $INC \
    "$WS/src/libpng_crc_fuzzer.cc" $LIBS -o "$WS/targets/libpng_crc_plain"

  sed 's/PNG_CRC_ERROR_QUIT, PNG_CRC_ERROR_QUIT);.*/PNG_CRC_QUIET_USE, PNG_CRC_QUIET_USE);/' \
    "$WS/src/libpng_crc_fuzzer.cc" > "$BUILD/libpng_crcoff_fuzzer.cc"
  AFL_QUIET=1 afl-clang-fast++ -g -O2 -fsanitize=fuzzer $INC \
    "$BUILD/libpng_crcoff_fuzzer.cc" $LIBS -o "$WS/targets/libpng_crc_off"
  echo "built: targets/libpng_crc_plain (CRC enforced) + targets/libpng_crc_off (contrast)"
}

build_agent() {
  : "${IJON_OUT:?build.sh agent needs IJON_OUT (the output target path)}"
  # The runner has already written the annotation into a library .c on disk.
  # Recompile the library with the IJON instrumentation pass (it processes the
  # changed file), then relink the harness against it.
  export AFL_LLVM_IJON=1 AFL_QUIET=1
  make -j4 -C "$LIBPNG" libpng16.la >/dev/null
  afl-clang-fast++ -g -O2 -fsanitize=fuzzer $INC \
    "$WS/src/libpng_crc_fuzzer.cc" $LIBS -o "$IJON_OUT"
  echo "built: $IJON_OUT (AFL+IJON, annotated library)"
}

case "${1:-all}" in
  all|lib) build_plain ;;
  plain)   build_plain ;;
  agent)   build_agent ;;
  *) echo "unknown variant: $1 (use: all|plain|agent)" >&2; exit 2 ;;
esac
