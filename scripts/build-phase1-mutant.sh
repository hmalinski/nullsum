#!/bin/bash
# Build the phase-1 XRP mutant: a throwaway rippled worktree with the labeled
# synthetic conservation fault (patches/phase1-xrpnotcreated.patch) applied.
#
# Env (override as needed):
#   RIPPLED_SRC  path to a rippled checkout (measured at commit 097cb5e8)
#   NULLSUM      path to this repo (defaults to the parent of scripts/)
set -euo pipefail
RIPPLED_SRC=${RIPPLED_SRC:?set RIPPLED_SRC to a rippled checkout}
NULLSUM=${NULLSUM:-"$(cd "$(dirname "$0")/.." && pwd)"}
WORKTREE=${WORKTREE:-"$RIPPLED_SRC/../rippled-nullsum-phase1"}

cd "$RIPPLED_SRC"
git worktree add -f --detach "$WORKTREE" 097cb5e8
cd "$WORKTREE"
git apply "$NULLSUM/patches/phase1-xrpnotcreated.patch"
git diff --stat

mkdir -p build && cd build
conan install .. --output-folder . --build missing -s build_type=Release -s "&:build_type=Debug" \
  -o "&:tests=True" -o "&:benchmark=False"
cmake -DCMAKE_TOOLCHAIN_FILE:FILEPATH=build/generators/conan_toolchain.cmake \
  -DCMAKE_BUILD_TYPE=Debug -Dxrpld=ON -Dtests=ON -Dbenchmark=OFF ..
cmake --build . --target xrpld --parallel "${PAR:-4}"
./xrpld --version
