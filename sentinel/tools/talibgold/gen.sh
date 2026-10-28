#!/bin/sh
# Rebuild the TA-Lib golden fixture. TALIB_PREFIX points at a TA-Lib install
# (https://github.com/TA-Lib/ta-lib, built with cmake + make install).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
prefix=${TALIB_PREFIX:-/usr/local}
out=$(mktemp -d)
cc -O2 -o "$out/gen" "$here/gen.c" -I"$prefix/include" "$prefix/lib/libta-lib.a" -lm
"$out/gen" "$root/testdata/btc_usdt_1h.csv" "$root/testdata/talib_golden.csv"
rm -rf "$out"
echo "wrote $root/testdata/talib_golden.csv"
