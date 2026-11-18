#!/bin/sh
# The wake words' runtime for the E5 (aarch64, musl): sherpa-onnx's keyword spotter with
# its own glibc, e5-kws (kws.c) and the model -- e5-kws-<version>.tar.gz, ~22 MB packed.
# Too big for a plugin package (20 MB unpacked at most): the NAS serves it
# (e5rec_server.py GET /v1/kws/<file>), wake.py fetches it the first time "wake" is on and
# checks its SHA-256 (KWS_SHA256 in assistant/wake.py: put the printed one there).
#
#   wake/build-runtime.sh OUT_DIR
#
# Needs: curl (through $PROXY if set), docker (arm64 debian's glibc: only copied, never
# run), python3 (zig from pip, to cross-compile kws.c).
set -eu
VERSION=5
SHERPA=v2.0.1
MODEL=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(cd "${1:?OUT_DIR}" && pwd)
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
C="curl -fsSL --retry 3 ${PROXY:+-x $PROXY}"
GH=https://github.com/k2-fsa/sherpa-onnx/releases/download

cd "$W"
$C -O "$GH/$SHERPA/sherpa-onnx-$SHERPA-linux-aarch64-shared-cpu.tar.bz2"
$C -O "$GH/kws-models/$MODEL.tar.bz2"
tar xjf "sherpa-onnx-$SHERPA-linux-aarch64-shared-cpu.tar.bz2"
tar xjf "$MODEL.tar.bz2"
SO=sherpa-onnx-$SHERPA-linux-aarch64-shared-cpu/lib

R=e5-kws
mkdir -p $R/lib $R/model
# glibc and the C++ runtime, from arm64 Debian (the E5 itself is musl)
id=$(docker create --platform linux/arm64 debian:bookworm-slim)
for f in ld-linux-aarch64.so.1 libc.so.6 libm.so.6 libdl.so.2 libpthread.so.0 librt.so.1 libstdc++.so.6 libgcc_s.so.1; do
	# (to stdout: a snap docker cannot write into /tmp)
	# (a link comes out under the name of what it points to: put back under the name asked for)
	rm -rf one && mkdir one
	{ docker cp -L "$id:/lib/aarch64-linux-gnu/$f" - 2>/dev/null || docker cp -L "$id:/usr/lib/aarch64-linux-gnu/$f" -; } | tar xf - -C one
	mv one/* "$R/lib/$f"
done
docker rm "$id" >/dev/null
cp $SO/libonnxruntime.so $SO/libsherpa-onnx-c-api.so $R/lib/

# e5-kws, against the C API of the same release (glibc 2.17: older than any Debian here)
python3 -m venv zig && zig/bin/pip install -q ziglang
$C -o c-api.h "https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/$SHERPA/sherpa-onnx/c-api/c-api.h"
zig/bin/python -m ziglang cc -target aarch64-linux-gnu.2.17 -O2 -s -Wall -o $R/lib/e5-kws \
	"$HERE/kws.c" -I. -I"$HERE" -L$SO -lsherpa-onnx-c-api -lm -Wl,-rpath,'$ORIGIN'

# the streaming model (chunk 8: the shorter wait), int8 where there is one
M=$MODEL
cp $M/encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx $R/model/encoder.int8.onnx
cp $M/decoder-epoch-13-avg-2-chunk-8-left-64.onnx $R/model/decoder.onnx
cp $M/joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx $R/model/joiner.int8.onnx
cp $M/tokens.txt $R/model/
echo "$VERSION sherpa-onnx $SHERPA $MODEL" > $R/VERSION

# (the same bytes every build of the same inputs: sorted, no owners, fixed times)
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 -cf - $R | gzip -n9 > "$OUT/e5-kws-$VERSION.tar.gz"
sha256sum "$OUT/e5-kws-$VERSION.tar.gz"
