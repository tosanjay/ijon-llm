#!/usr/bin/env bash
# Reproducible build of the libarchive read-path target (IJON-Reloaded walkthrough).
#
# libarchive decodes containers (tar/cpio/zip/7z/...) wrapped in filters
# (gzip/xz/zstd/...). The state IJON exposes is the *sequence* of formats/entry
# types the decoder walks -- invisible to edge coverage. We build the library
# once with afl-clang-fast+ASAN and link the harness (src/archive_fuzzer.c) in
# the variants the loop needs.
#
# Usage:
#   ./build.sh                 # lib + plain + ijon  (the default the loop wants)
#   ./build.sh plain           # control fuzzer (no IJON)
#   ./build.sh ijon            # IJON target (annotation compiled in, AFL_LLVM_IJON=1)
#   ./build.sh cov             # llvm-cov driver for source-coverage keep/revert
#
# Prereqs: AFL_ROOT (AFL++ with IJON). cov needs LLVM_BIN (clang+llvm-cov).
# Source tree: cloned to ./build/libarchive, or set LIBARCHIVE_SRC to a tree that
# already has a generated ./configure (avoids needing autotools/libtool).
set -euo pipefail

export TMPDIR="${TMPDIR:-/tmp}"; mkdir -p "$TMPDIR"
AFL="${AFL_ROOT:?set AFL_ROOT to your AFL++ (with IJON) build}"
export PATH="$AFL:$PATH"; export AFL_PATH="$AFL/include"; export AFL_QUIET=1
WS="$(cd "$(dirname "$0")" && pwd)"
BUILD="$WS/build"
SRC="$BUILD/libarchive"          # afl+asan source/build tree
COVSRC="$BUILD/libarchive-cov"   # llvm-cov source/build tree
mkdir -p "$BUILD" "$WS/targets" "$WS/in"

# Pin optional deps explicitly so the harness link is deterministic regardless of
# what the host happens to have: keep zlib (gzip) + xz (lzma) + zstd; drop the rest.
CONFFLAGS="--disable-shared --enable-static \
  --without-bz2lib --without-lz4 --without-lzo2 --without-libb2 \
  --without-xml2 --without-openssl --without-nettle --without-cng"

fetch_src() {  # $1 = dest dir
  local dst="$1"
  [ -d "$dst" ] && return 0
  if [ -n "${LIBARCHIVE_SRC:-}" ]; then
    cp -a "$LIBARCHIVE_SRC" "$dst"
    ( cd "$dst" && make distclean >/dev/null 2>&1 || true )   # wipe foreign artifacts
  else
    git clone --depth 1 -b v3.8.6 https://github.com/libarchive/libarchive.git "$dst"
    ( cd "$dst" && [ -f configure ] || ./build/autogen.sh )
  fi
}

build_lib_afl() {
  [ -f "$SRC/.libs/libarchive.a" ] && return 0
  fetch_src "$SRC"
  ( cd "$SRC" && CC=afl-clang-fast CFLAGS="-g -O1 -fsanitize=address" \
      ./configure $CONFFLAGS && AFL_QUIET=1 make -j"$(nproc)" libarchive.la )
}

build_lib_cov() {
  [ -f "$COVSRC/.libs/libarchive.a" ] && return 0
  local LLVM="${LLVM_BIN:?set LLVM_BIN to your clang/llvm-cov bin dir}"
  fetch_src "$COVSRC"
  ( cd "$COVSRC" && CC="$LLVM/clang" \
      CFLAGS="-g -O1 -fprofile-instr-generate -fcoverage-mapping" \
      ./configure $CONFFLAGS && make -j"$(nproc)" libarchive.la )
}

INC="-I$SRC/libarchive"
HARNESS="$WS/src/archive_fuzzer.c"

link_plain() {
  build_lib_afl
  afl-clang-fast -g -O1 -fsanitize=fuzzer,address $INC \
    "$HARNESS" "$SRC/.libs/libarchive.a" -lz -llzma -lzstd \
    -o "$WS/targets/archive_plain"
  echo "built: targets/archive_plain (control, no IJON)"
}

link_ijon() {
  build_lib_afl
  AFL_LLVM_IJON=1 afl-clang-fast -g -O1 -fsanitize=fuzzer,address -D_USE_IJON $INC \
    "$HARNESS" "$SRC/.libs/libarchive.a" -lz -llzma -lzstd \
    -o "$WS/targets/archive_ijon"
  echo "built: targets/archive_ijon (IJON reference annotation compiled in)"
}

link_cov() {
  build_lib_cov
  local LLVM="${LLVM_BIN:?set LLVM_BIN}"
  "$LLVM/clang" -g -O1 -fsanitize=fuzzer -fprofile-instr-generate -fcoverage-mapping \
    "-I$COVSRC/libarchive" \
    "$HARNESS" "$COVSRC/.libs/libarchive.a" -lz -llzma -lzstd \
    -o "$WS/targets/archive_cov"
  echo "built: targets/archive_cov (llvm-cov driver for keep/revert)"
}

# class-2 metric extractor (uncomplicated build: just libarchive, no fuzzer/IJON)
build_describe() {
  build_lib_afl
  afl-clang-fast -g -O1 -fsanitize=address $INC \
    "$WS/src/archive_describe.c" "$SRC/.libs/libarchive.a" -lz -llzma -lzstd \
    -o "$WS/targets/archive_describe"
  echo "built: targets/archive_describe (format:filetype sequence extractor)"
}

# autonomous-loop build: compile an AGENT-PATCHED harness ($IJON_HARNESS, with the
# agent's annotation inserted unconditionally -- no -D_USE_IJON) under AFL_LLVM_IJON.
link_agent() {
  build_lib_afl
  local h="${IJON_HARNESS:-$HARNESS}"
  local out="${IJON_OUT:-$WS/targets/archive_agent}"
  AFL_LLVM_IJON=1 afl-clang-fast -g -O1 -fsanitize=fuzzer,address $INC \
    "$h" "$SRC/.libs/libarchive.a" -lz -llzma -lzstd -o "$out"
  echo "built: $out (agent-proposed annotation, AFL_LLVM_IJON)"
}

case "${1:-all}" in
  lib)      build_lib_afl ;;
  plain)    link_plain ;;
  ijon)     link_ijon ;;
  cov)      link_cov ;;
  describe) build_describe ;;
  agent)    link_agent ;;
  all)      link_plain; link_ijon ;;
  *) echo "usage: $0 {all|lib|plain|ijon|cov|describe|agent}" >&2; exit 2 ;;
esac
