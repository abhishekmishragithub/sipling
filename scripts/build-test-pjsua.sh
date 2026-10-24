#!/bin/sh
# Build a separate upstream test tool. Never copied into sipling distributions.
set -eu
build_dir="${1:?usage: build-test-pjsua.sh ABSOLUTE_BUILD_DIR}"
mkdir -p "$build_dir"
curl -fsSL https://codeload.github.com/pjsip/pjproject/tar.gz/refs/tags/2.17 \
  -o "$build_dir/pjproject.tar.gz"
python3 - "$build_dir/pjproject.tar.gz" <<'PY'
import hashlib
import sys
from pathlib import Path
expected = '065fe06c06788d97c35f563796d59f00ce52fe9558a52d7b490a042a966facce'
if hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() != expected:
    raise SystemExit('upstream archive checksum mismatch')
PY
tar -xzf "$build_dir/pjproject.tar.gz" -C "$build_dir"
cd "$build_dir/pjproject-2.17"
./configure --disable-sound --disable-video --disable-ssl --disable-libyuv \
  --disable-ffmpeg --disable-v4l2
make dep
make -j4
mkdir -p "$build_dir/bin"
for executable in pjsip-apps/bin/pjsua-*; do
  if [ -f "$executable" ] && [ -x "$executable" ]; then
    cp "$executable" "$build_dir/bin/pjsua"
    break
  fi
done
test -x "$build_dir/bin/pjsua"
