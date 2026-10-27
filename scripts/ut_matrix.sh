#!/bin/bash
# Run rippled's own unit-test suites across the mutant configurations (Debug + Release),
# producing the per-config .tsv tables in demo-output/phase2/unittests/.
# Env: DEBUG_BIN, RELEASE_BIN = the two mutant builds from build-nullsum.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
D=${DEBUG_BIN:?set DEBUG_BIN}
R=${RELEASE_BIN:?set RELEASE_BIN}
O=${OUT:-"$HERE/../out/ut"}; mkdir -p "$O"
export PAR=${PAR:-1}
for build in release debug; do
  if [ "$build" = release ]; then BIN=$R; else BIN=$D; fi
  "$HERE/run_unittests.sh" "$BIN" "$build-dormant" "$O" ""
  "$HERE/run_unittests.sh" "$BIN" "$build-M1misattr" "$O" "NULLSUM_MUTANT_ALWAYS=1"
  "$HERE/run_unittests.sh" "$BIN" "$build-M2skim" "$O" "NULLSUM_MUTANT_SKIM=1"
  "$HERE/run_unittests.sh" "$BIN" "$build-M1misattr-NETREMOVED" "$O" "NULLSUM_MUTANT_ALWAYS=1 NULLSUM_MUTANT_NO_NET=1"
  "$HERE/run_unittests.sh" "$BIN" "$build-M2skim-NETREMOVED" "$O" "NULLSUM_MUTANT_SKIM=1 NULLSUM_MUTANT_NO_NET=1"
done
echo ALLDONE
