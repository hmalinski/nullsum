#!/bin/bash
# Build the lending mutant (Debug + Release): a throwaway rippled worktree with the
# labeled synthetic lending fault (patches/lending-loanpay.patch) applied. The fault
# is gated at runtime by env vars (NULLSUM_MUTANT_NO_NET / _ALWAYS / _SKIM / NULLSUM_SKIM_TENTHS)
# AND by the sentinel loan-service fees 777 (misattribution) and 888 (skim); with no env set and
# no sentinel fee, the binaries behave like a clean build.
#
# Env (override as needed):
#   RIPPLED_SRC  path to a rippled checkout (measured at commit 097cb5e8)
#   NULLSUM      path to this repo (defaults to the parent of scripts/)
set -euo pipefail
RIPPLED_SRC=${RIPPLED_SRC:?set RIPPLED_SRC to a rippled checkout}
NULLSUM=${NULLSUM:-"$(cd "$(dirname "$0")/.." && pwd)"}
WORKTREE=${WORKTREE:-"$RIPPLED_SRC/../rippled-nullsum"}

cd "$RIPPLED_SRC"
git worktree add -f --detach "$WORKTREE" 097cb5e8
cd "$WORKTREE"
git apply "$NULLSUM/patches/lending-loanpay.patch"
git diff --stat

for MODE in Debug Release; do
  D="$WORKTREE/build-$(echo "$MODE" | tr A-Z a-z)"
  mkdir -p "$D" && cd "$D"
  if [ "$MODE" = Debug ]; then
    conan install .. --output-folder . --build missing -s build_type=Release -s "&:build_type=Debug" -o "&:tests=True" -o "&:benchmark=False"
  else
    conan install .. --output-folder . --build missing -s build_type=Release -o "&:tests=True" -o "&:benchmark=False"
  fi
  cmake -DCMAKE_TOOLCHAIN_FILE:FILEPATH="$D/build/generators/conan_toolchain.cmake" \
    -DCMAKE_BUILD_TYPE="$MODE" -Dxrpld=ON -Dtests=ON -Dbenchmark=OFF ..
  cmake --build . --target xrpld --parallel "${PAR:-4}"
  ./xrpld --version
done
