# Measurement environment & exact reproduction

Everything in `demo-output/` was produced in this environment. The invariant/unit-test
behaviour is a property of the specific rippled build and the labeled synthetic faults, not of
rippled in general.

## Toolchain
- **rippled:** commit `097cb5e8` (develop), built **Debug** and **Release** via Conan.
  - Baseline binary: a clean build at that commit (Release).
  - Mutant binaries (Debug + Release): the same commit with `patches/lending-loanpay.patch`
    applied (see `scripts/build-nullsum.sh`). The fault is gated at runtime by env vars
    (`NULLSUM_MUTANT_NO_NET` / `_ALWAYS` / `_SKIM` / `NULLSUM_SKIM_TENTHS`) and the sentinel
    loan-service fees 777 / 888; with none set the binaries behave like a clean build.
  - Phase-1 (XRP) mutant: `patches/phase1-xrpnotcreated.patch` (see `scripts/build-phase1-mutant.sh`).
- **Amendments enabled on the standalone node:** `MPTokensV1`, `SingleAssetVault`,
  `LendingProtocol` (the set the controller requests at `nullsum/rpc.py`; this manifest records
  the requested set, which the node reports as enabled in standalone mode).
- **Driver:** Python 3.11+ with **xrpl-py 5.2.0** (minimum `>= 4.4.0`, the floor for Lending
  Protocol support). The genesis key is pinned to SECP256K1 in `nullsum/model.py`, so derivation
  is correct regardless of xrpl-py's default-algorithm change at 5.0.0.

## Exact reproduction
With `NULLSUM` = repo root, `BASELINE_BIN` / `DEBUG_BIN` / `RELEASE_BIN` = the three binaries
above, and `PY` = a Python with `requirements.txt` installed:

- **Phase 1 (XRP), `demo-output/phase1/`:**
  ```
  python run_demo.py baseline --binary "$BASELINE_BIN" --seeds 1 2 --accounts 8 --payments 120
  python run_demo.py mutant   --binary "$DEBUG_BIN" --baseline-binary "$BASELINE_BIN" \
      --seeds 2 --accounts 8 --payments 120
  ```
  (seed 2 → a 170-op sequence with 6 sentinel payments; the mutant run shows +6 drops created
  across 4 closes and shrinks to a 2-op reproduction.)
- **Phase 2 (MPT), `demo-output/phase2/`:** `BASELINE_BIN DEBUG_BIN RELEASE_BIN PY bash scripts/harness_matrix.sh`
  and the unit-test matrix `DEBUG_BIN RELEASE_BIN bash scripts/ut_matrix.sh` (plus one baseline
  run via `scripts/run_unittests.sh "$BASELINE_BIN" baseline-debug <out> ""`). `scripts/summarize_unittests.py`
  turns the TSVs into `unittest_table.txt`.
- **Phase 3 (IOU), `demo-output/phase3/`:** `BASELINE_BIN DEBUG_BIN RELEASE_BIN PY bash scripts/iou_matrix.sh`

## Notes
- The raw per-suite logs in `phase2/unittest-logs.tar.gz` retain the original container build
  path (`/root/projects/rippled-nullsum/...`) inside compiler assertion messages — historical
  provenance, not a runtime dependency. The committed `.tsv` summaries are stripped to
  repo-relative `src/libxrpl/...` paths.
- Exact wall-clock and machine specs are not pinned; the measurements are deterministic in
  outcome (pass/fail/abort and residuals), not in timing.
