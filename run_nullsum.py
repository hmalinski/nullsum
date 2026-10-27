#!/usr/bin/env python3
"""Nullsum phase 2: per-party lending oracle, measured against baseline / mutant binaries.

  python run_nullsum.py run --binary BIN --label NAME --fee 777 --payments 4 [--env '{"K":"V"}'] [--shrink cumulative|strict|tol_step]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from nullsum.lending import LendingReplayer, LOp, generate_lending_ops, lending_fails
from nullsum.repro import ddmin


def fmt_close(c, only_bad=True) -> str:
    out = [f"  ledger #{c.ledger_index}: txs={c.tx_summaries}"]
    for r in c.rows:
        if r.residual != 0 or not only_bad:
            out.append(f"    {r.party:9s} {r.asset}: expected={r.expected} actual={r.actual} residual={r.residual:+d}")
    out.append(f"    global MPT sum (all tracked parties) == expected: {c.global_mpt_ok}")
    out.append(f"    (diagnostic only) rippled OutstandingAmount={c.diag_outstanding} vs harness-issued={c.diag_expected_outstanding}")
    return "\n".join(out)


def verdict(run, eps: int) -> str:
    def where(c):
        return f"ledger #{c.ledger_index}" if c else "NEVER"
    return (f"detectors on {len(run.closes)} closes / {run.n_loanpay_ok} LoanPay ok:\n"
            f"    strict per-party step (eps=0)   : first flag {where(run.detect_strict())}\n"
            f"    per-party step, tolerance eps={eps} : first flag {where(run.detect_tol_step(eps))}\n"
            f"    per-party cumulative, bound eps={eps}: first flag {where(run.detect_cumulative(eps))}\n"
            f"    global MPT sum (aggregate)      : first flag {where(run.detect_global())}\n"
            f"    unexpected tx result (not tesSUCCESS): first flag {where(run.detect_result())}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["run"])
    p.add_argument("--binary", required=True)
    p.add_argument("--label", default="run")
    p.add_argument("--fee", type=int, default=250, help="loan service fee (777 = misattribution sentinel, 888 = skim sentinel)")
    p.add_argument("--payments", type=int, default=4)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--eps", type=int, default=1)
    p.add_argument("--env", default=None, help="JSON dict of env vars for the node process")
    p.add_argument("--shrink", default=None, choices=[None, "strict", "tol_step", "cumulative", "result"])
    p.add_argument("--max-evals", type=int, default=150)
    p.add_argument("--out", default=None)
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args()

    env = json.loads(a.env) if a.env else None
    rep = LendingReplayer(a.binary, a.label, env)
    ops = generate_lending_ops(a.seed, a.payments, a.fee)
    t0 = time.time()
    r = rep.replay(ops)
    print(f"[{a.label}] seed={a.seed} fee={a.fee} payments={a.payments}: {len(ops)} ops, "
          f"{r.ops_total - r.ops_skipped} applied, {len(r.closes)} ledger closes ({time.time()-t0:.1f}s)")
    if r.errors:
        print("  HARNESS ERRORS:", r.errors[:3])
    if r.crashed:
        print(f"  NODE STOPPED RESPONDING during replay: {r.crashed}")
        print(f"  closes completed before the stop: {len(r.closes)}; LoanPay ok: {r.n_loanpay_ok}")
    from collections import Counter
    res = Counter(t.split(" ", 1)[1] for c in r.closes for t in c.tx_summaries if t.startswith("LoanPay"))
    print(f"  LoanPay results in committed ledgers: {dict(res)}")
    for m in r.invariant_log[:3]:
        print(f"  rippled invariant log: {m}")
    print("  " + verdict(r, a.eps))
    first = r.detect_strict()
    if first:
        print("FIRST OFFENDING CLOSE (per-party table, nonzero residuals only):")
        print(fmt_close(first))
    if a.verbose:
        for c in r.closes:
            print(fmt_close(c, only_bad=False))
    if a.shrink and not r.crashed:
        pred = lending_fails(rep, a.shrink, a.eps)
        if not pred(ops):
            print(f"shrink: detector '{a.shrink}' does not flag the original run; nothing to shrink")
        else:
            e0 = rep.evals; t0 = time.time()
            minimal = ddmin(ops, pred, max_evals=a.max_evals)
            print(f"\nddmin[{a.shrink}]: {len(ops)} ops -> {len(minimal)} ops, {rep.evals - e0} node replays, {time.time()-t0:.0f}s")
            print("MINIMAL REPRO:")
            for i, o in enumerate(minimal, 1):
                print(f"  {i}. {o}")
            fin = rep.replay(minimal)
            print("re-verify:\n  " + verdict(fin, a.eps))
            c = fin.detect_cumulative(0) or fin.detect_strict()
            if c:
                print(fmt_close(c))
            if a.out:
                json.dump({"label": a.label, "detector": a.shrink, "original_ops": len(ops),
                           "minimal": [o.__dict__ for o in minimal]}, open(a.out, "w"), indent=2)
    # Non-zero only for a harness/transport problem (an expected in-process mutant abort is a
    # valid measurement, so it stays 0); this lets a matrix script tell a broken run apart.
    if r.errors or (r.crashed and "harness/transport" in str(r.crashed)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
