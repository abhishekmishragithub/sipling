#!/bin/sh
# From anywhere: sh sipling/examples/run-local.sh /tmp/sipling-pjbuild
# Requires an already configured/built pjproject and the sibling earshot checkout.
set -eu
project=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=${1:?usage: run-local.sh ABSOLUTE_PJPROJECT_BUILD_DIR}
python=${SIPLING_PYTHON:-"$project/.venv/bin/python"}
make -s -C "$project/helpers" PJDIR="$build_dir/pjproject-2.17" OUTPUT="$build_dir/bin/sipling-stream"
export PATH="$build_dir/bin:$PATH"
export PYTHONPATH="$project/src:$project/../earshot/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python" "$project/examples/pair.py"
