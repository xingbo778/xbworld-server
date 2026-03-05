#!/usr/bin/env bash
#
# Build the Freeciv C server from the submodule source.
#
# The source lives in freeciv/ (a git submodule pointing at
# xingbo778/freeciv, branch xbworld). All patches are already
# committed there — no download or patching step is needed.
#
# Usage:
#   ./prepare_freeciv.sh          # normal build
#   ./prepare_freeciv.sh TEST     # build with -Dwerror=true
#   ./prepare_freeciv.sh clean    # wipe build dir and rebuild

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"
cd "${DIR}"

if [ ! -d freeciv/server ]; then
  echo "Freeciv source not found. Initializing submodule..."
  if ! ( cd .. && git submodule update --init --recursive freeciv/freeciv ); then
    echo "ERROR: Failed to initialize freeciv submodule." >&2
    echo "Make sure you are inside the xbworld repository." >&2
    exit 1
  fi
fi

if [ "${1:-}" = "clean" ]; then
  echo "Cleaning build directory..."
  rm -rf build
fi

export PATH=${HOME}/freeciv/meson-install:${PATH}

EXTRA_MESON_PARAMS=()
if [ "${1:-}" = "TEST" ]; then
  EXTRA_MESON_PARAMS+=("-Dwerror=true")
fi

mkdir -p build

if [ ! -f build/build.ninja ]; then
  echo "Configuring with meson..."
  ( cd build
    meson setup ../freeciv -Dserver='freeciv-web' \
          -Dclients=[] -Dfcmp=cli -Djson-protocol=true -Dnls=false \
          -Daudio=none -Dtools=manual \
          -Dproject-definition=../freeciv-web.fcproj \
          -Ddefault_library=static -Dprefix="${HOME}/freeciv" \
          -Doptimization=3 ${EXTRA_MESON_PARAMS[@]+"${EXTRA_MESON_PARAMS[@]}"}
  )
else
  echo "Build already configured, skipping meson setup (use 'clean' to reconfigure)."
fi

echo "Building..."
ninja -C build

echo "Installing to ~/freeciv/ ..."
ninja -C build install

echo "Done. Binary: ~/freeciv/bin/freeciv-web"
