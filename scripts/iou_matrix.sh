#!/bin/bash
# Reproduce the phase-3 (IOU drift) matrix in demo-output/phase3/.
# Env: BASELINE_BIN, DEBUG_BIN, RELEASE_BIN = rippled binaries; PY = python.
set -euo pipefail
NULLSUM=${NULLSUM:-"$(cd "$(dirname "$0")/.." && pwd)"}
PY=${PY:-python}
B=${BASELINE_BIN:?set BASELINE_BIN}
D=${DEBUG_BIN:?set DEBUG_BIN}
R=${RELEASE_BIN:?set RELEASE_BIN}
OUT=${OUT:-"$NULLSUM/out3"}; mkdir -p "$OUT"
cd "$NULLSUM"
run() { label=$1; shift; echo "################ $label"; "$PY" run_iou.py --label "$label" "$@" 2>&1; }
NN='"NULLSUM_MUTANT_NO_NET":"1"'
{
run baseline-debug-IOU-N7 --binary "$B" --payments 7
run baseline-debug-IOU-N29 --binary "$B" --payments 29
run release-dormant-NONET-IOU-N29 --binary "$R" --payments 29 --fee 250 --env "{$NN}"
run release-M2-net-present-IOU-N7 --binary "$R" --payments 7 --env '{"NULLSUM_SKIM_TENTHS":"10"}'
run debug-M2-IOU-N7 --binary "$D" --payments 7 --env "{$NN,\"NULLSUM_SKIM_TENTHS\":\"10\"}"
run debug-M2-net-present-IOU-N7 --binary "$D" --payments 7 --fee 888
run release-M2-NETREMOVED-skim1.0-IOU-N7 --binary "$R" --payments 7 --env "{$NN,\"NULLSUM_SKIM_TENTHS\":\"10\"}"
run release-M2-NETREMOVED-skim1.0-IOU-N29 --binary "$R" --payments 29 --env "{$NN,\"NULLSUM_SKIM_TENTHS\":\"10\"}"
run release-M2-NETREMOVED-skim0.5-IOU-N29 --binary "$R" --payments 29 --env "{$NN,\"NULLSUM_SKIM_TENTHS\":\"5\"}"
run release-M2-NETREMOVED-skim0.2-IOU-N29 --binary "$R" --payments 29 --env "{$NN,\"NULLSUM_SKIM_TENTHS\":\"2\"}" --shrink-bound 1 --out "$OUT/minimal_IOU_skim0.2_cum1.json"
} > "$OUT/iou_matrix.txt"
echo finished
