#!/bin/bash
# Reproduce the phase-2 (MPT lending) harness matrix in demo-output/phase2/.
# Env: BASELINE_BIN, DEBUG_BIN, RELEASE_BIN = rippled binaries (baseline + the two
#      mutant builds from build-nullsum.sh); PY = python with requirements installed.
set -euo pipefail
NULLSUM=${NULLSUM:-"$(cd "$(dirname "$0")/.." && pwd)"}
PY=${PY:-python}
B=${BASELINE_BIN:?set BASELINE_BIN}
D=${DEBUG_BIN:?set DEBUG_BIN}
R=${RELEASE_BIN:?set RELEASE_BIN}
OUT=${OUT:-"$NULLSUM/out2"}; mkdir -p "$OUT"
cd "$NULLSUM"
run() { label=$1; shift; echo "################ $label"; "$PY" run_nullsum.py run --label "$label" "$@" 2>&1; }
{
run baseline-debug-fee777-N4 --binary "$B" --fee 777 --payments 4
run baseline-debug-fee888-N40 --binary "$B" --fee 888 --payments 40
run release-dormant-fee250-N40 --binary "$R" --fee 250 --payments 40
run release-dormant-NONET-fee250-N40 --binary "$R" --fee 250 --payments 40 --env '{"NULLSUM_MUTANT_NO_NET":"1"}'
run debug-dormant-fee250-N40 --binary "$D" --fee 250 --payments 40
run release-M1-misattribution-net-present --binary "$R" --fee 777 --payments 4
run release-M2-skim-net-present --binary "$R" --fee 888 --payments 40
run debug-M1-misattribution --binary "$D" --fee 777 --payments 4
run debug-M2-skim --binary "$D" --fee 888 --payments 40
run release-M1-misattribution-NET-REMOVED --binary "$R" --fee 777 --payments 4 --env '{"NULLSUM_MUTANT_NO_NET":"1"}' --shrink strict --out "$OUT/minimal_M1_release_net_removed.json"
run release-M2-skim-NET-REMOVED --binary "$R" --fee 888 --payments 40 --env '{"NULLSUM_MUTANT_NO_NET":"1"}' --shrink cumulative --out "$OUT/minimal_M2_release_net_removed.json"
run debug-M1-NET-REMOVED --binary "$D" --fee 777 --payments 4 --env '{"NULLSUM_MUTANT_NO_NET":"1"}'
} > "$OUT/harness_matrix.txt"
echo finished
