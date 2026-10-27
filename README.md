# Nullsum

**An external, independent conservation-invariant assurance harness for the XRP Ledger.**

Nullsum drives a local `--standalone` `rippled` node over JSON-RPC, runs transaction
sequences, and checks **value conservation independently of rippled's own code** — then
*measures* how well rippled's own in-ledger invariants and test suites catch seeded faults,
per build configuration.

It is **defense-in-depth alongside** rippled's invariant layer, not a replacement for it. In
my own measurements rippled's invariants are robust; Nullsum's job is to *quantify* that and
to backstop paths where a specific in-ledger check is a known stub.

> Status: **M1 proof-of-concept — built and measured.** The thesis below is demonstrated on
> real, reproducible runs (see [`demo-output/`](demo-output/)). Broader mutant coverage and a
> cross-version differential mode are future milestones, not claimed here.

---

## What it does

1. **Seeded-fault measurement.** Inject a labeled synthetic value fault into a mutant `rippled`
   build and measure which configuration (Debug / Release) catches it — the node's own
   invariants, its own unit tests, and Nullsum's external oracle — side by side.
2. **Independent external oracle — two detection strengths, by asset path.** Every verdict is
   computed from the transactions *Nullsum submitted*, never from rippled's own aggregate drop
   counter (`total_coins`) or `OutstandingAmount`; those are recorded as diagnostics only. The
   two paths differ in what they can catch:
   - **MPT and IOU (per-party):** Nullsum holds each party's *own* expected balance, derived
     from the submitted transactions plus the genesis baseline, and compares it to that party's
     committed balance. Because the expectation is per-party, this catches **misattribution** —
     value moved to the wrong account — even when the total is unchanged and a counter-trusting
     or aggregate-sum check would see nothing.
   - **XRP phase-1 (closed-universe aggregate):** Nullsum re-derives the *sum* of all tracked
     account-root balances and checks that it fell across each close by exactly the fees burned,
     independently re-summing the balances rather than trusting `total_coins`. This catches net
     **creation or destruction** of XRP, but **not** a zero-sum misattribution, and it
     re-baselines to the observed total after every close. It still reads each account's
     `Balance` and each transaction's `Fee` from the node — "independent of rippled's counters"
     here means it does not trust the aggregate `total_coins`, not that it avoids reading
     committed state.
3. **Sequence-level, tolerance-aware drift detection.** Cumulative per-party tracking that
   catches slow, below-the-per-step-floor skims that a single-step tolerance check misses.
4. **Shrinking.** A `ddmin` delta-debugging reducer shrinks a failing run toward a smaller
   reproduction. Within its replay budget (`max_evals`) it reduces in chunks down toward single
   operations and stops at a local minimum with respect to that chunk schedule — removing a
   tested chunk no longer reproduces the fault; if the budget is exhausted first it returns the
   smallest sequence found so far. Every reduction shipped in `demo-output/` finished well under
   budget.

## Honest scope of this MVP

- **Assets:** XRP (account-root, closed-universe *aggregate* conservation — net create/destroy,
  not misattribution) and MPT + IOU (*per-party* expected-balance oracles — also catch
  misattribution).
- **Paths exercised:** account funding + `Payment`; and a lending flow
  (`VaultCreate → VaultDeposit → LoanBrokerSet → LoanSet → LoanPay`).
- **Every injected fault is synthetic and clearly labeled** (`// NULLSUM MUTANT`) in a
  throwaway build. Nullsum neither sought nor found any Release-surviving gap in unmodified
  `rippled`. See **[Disclosure policy](#disclosure-policy)**.
- **Not yet built:** a generated mutant *population* (so no mutation *score* is claimed at this
  n — two faults), broader amendment coverage, and the cross-version differential replay mode.

## Measured against

- **rippled:** commit `097cb5e8` (develop), built **Debug** and **Release** via Conan.
- **Amendments enabled:** `MPTokensV1`, `SingleAssetVault`, `LendingProtocol`.
- **Driver:** Python 3.11+ with `xrpl-py >= 4.4.0` (see [`requirements.txt`](requirements.txt)).
- **Build + run recipes:** [`scripts/`](scripts/); the labeled-fault patches are in
  [`patches/`](patches/). Raw per-suite unit-test logs ship as
  `demo-output/phase2/unittest-logs.tar.gz`.
- **Full environment + exact per-phase reproduction parameters:**
  [`demo-output/ENVIRONMENT.md`](demo-output/ENVIRONMENT.md).

## What I measured (reproducible; see `demo-output/`)

On a lending scenario with two labeled synthetic faults (a broker-fee misattribution and a
per-payment skim):

- **rippled's own invariants held against both faults.** In a Release build, `ValidLoanBroker`
  rejected both at the first payment (`tecINVARIANT_FAILED`). In a Debug build of the MPT
  scenario, a *second, independent* layer — `LoanPay`'s own conservation assertion
  (`LoanPay.cpp:907`) — aborts the node even with that invariant disabled. On the IOU scenario
  that assertion tolerates a one-unit skim within its rounding allowance, so
  `ValidLoanBroker` is the single in-ledger catch there. This is robustness against *these two
  mutations* — not a claim of comprehensive coverage. (The assertion is `LoanPay`'s unmodified
  "funds are conserved (with rounding)" check in `LoanPay::doApply`; the captured abort logs show
  it at `LoanPay.cpp:907` in the mutant build, where the synthetic patch's inserted lines have
  shifted it down from its position in the pinned commit.)
- **Defense-in-depth, shown conditionally.** The bad state committed to the ledger only when I
  *also* disabled that one invariant (a labeled switch — an analogue of a stub-check class, not
  a live vulnerability). Then Nullsum's per-party oracle caught the leak from submitted
  transactions alone — no access to rippled's test fixtures or source — and `ddmin` shrank the
  run to a minimal reproduction.
- **Drift detection, bounded and honest.** On an IOU scenario with genuine rounding noise (max
  cumulative residual **0.48** over 29 payments), a cumulative detector at `bound=1` never false-
  flagged the clean run, yet caught the tested **0.5** and **0.2** unit/payment skims (first
  flag at the 2nd and 6th payment respectively) — while a per-step `tolerance=1` check **never**
  flagged either over the same 29-payment runs. At **1.0** unit/payment the per-step check also
  catches it, so the advantage is specific to *slow, below-the-floor* skims. Tolerances are
  calibrated to this scenario.

One benign quirk in `harness_matrix.txt`: in the Release skim run with the net present, the 40th
of 40 payments returns `tecEXPIRED` rather than `tecINVARIANT_FAILED`. Because the preceding 39
payments were rejected, the loan's due date never advanced, and standalone `ledger_accept`
advances close time every ledger — so by the final submission the first installment was overdue
and `LoanPay` returned `tecEXPIRED` before the invariant ran. In the net-removed run, where
payments succeed and advance the due date, all 40 commit. Both are expected.

Captured runs: [`demo-output/phase1/`](demo-output/phase1/) (XRP),
[`demo-output/phase2/`](demo-output/phase2/) (MPT), [`demo-output/phase3/`](demo-output/phase3/)
(IOU).

## Layout

- `nullsum/oracles.py` — pure XRP aggregate conservation oracle.
- `nullsum/model.py` — seeded random op sequences (fund / pay / close), replay + per-close oracle.
- `nullsum/lending.py` — MPT lending replayer + per-party oracle.
- `nullsum/lending_iou.py` — IOU lending replayer + per-step / cumulative detectors.
- `nullsum/rpc.py` — standalone-node JSON-RPC client + fresh-node controller.
- `nullsum/repro.py` — `ddmin` minimal-reproduction reducer.
- `run_demo.py` — XRP baseline/mutant demo. `run_nullsum.py` — MPT lending runner.
  `run_iou.py` — IOU drift runner.
- `patches/` — the labeled synthetic-fault patches (`// NULLSUM MUTANT`).
- `scripts/` — build + measurement-matrix recipes.
- `tests/` — pure-Python unit tests for the oracle and reducer (no node needed).
- `demo-output/` — captured, reproducible runs (phase1 XRP, phase2 MPT, phase3 IOU).

## Quick start

Requires Python 3.11+ and a `rippled` binary (a standard standalone build); the node driver is
Linux/macOS (it sends `SIGKILL` to the node). The demos also need a mutant build —
[`scripts/build-phase1-mutant.sh`](scripts/build-phase1-mutant.sh) for the XRP demo and
[`scripts/build-nullsum.sh`](scripts/build-nullsum.sh) for the MPT/IOU lending demos.

```bash
pip install -r requirements.txt       # runtime: xrpl-py
pip install -r requirements-dev.txt   # + pytest, for the tests below

# Pure-Python unit tests (no node needed)
python -m pytest -q

# XRP: conservation holds on baseline; a seeded leak is caught + shrunk on the mutant.
# (These are the exact parameters behind demo-output/phase1/ — see ENVIRONMENT.md.)
python run_demo.py baseline --binary /path/to/xrpld --seeds 1 2 --accounts 8 --payments 120
python run_demo.py mutant   --binary /path/to/mutant/xrpld --baseline-binary /path/to/xrpld \
    --seeds 2 --accounts 8 --payments 120

# MPT lending: per-party oracle + detector table against a chosen binary
python run_nullsum.py run --binary /path/to/xrpld --label demo --fee 777 --payments 4

# IOU: per-step vs cumulative drift detection
python run_iou.py --binary /path/to/xrpld --label demo --payments 29
```

The oracle and reducer (`nullsum/oracles.py`, `nullsum/repro.py`) are pure Python and covered by
`tests/` without a node.

## Why this is not redundant with rippled's own invariants

rippled already runs ~30 in-ledger invariants in production (`XRPNotCreated`, `ValidLoanBroker`,
`ValidVault`, `ValidMPTBalanceChanges`, …) plus `jtx` unit tests and continuous Antithesis
fuzzing. Nullsum's value is being **outside** that code: an invariant that is a no-op or returns
`true` for a path (the publicly tracked rippled #7690 lending stub is one example) cannot catch
its own bug, but an independent external oracle can — and seeded-fault measurement records which
faults the net catches in which build, which I have not seen published elsewhere.

## Disclosure policy

Any finding against an amendment in a released `rippled` tag goes **privately to Ripple's
security / bounty channel first**. This repository publishes **oracles and fixed regression
tests only — never an unfixed finding** — and all seeded-fault demonstrations use **synthetic,
clearly labeled** faults in throwaway builds.

## License

MIT — see [LICENSE](LICENSE).
