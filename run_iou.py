#!/usr/bin/env python3
"""Nullsum phase 3 driver: IOU scenario, per-step vs cumulative detection of a sub-tolerance skim."""
from __future__ import annotations

import argparse
import json
import sys
import time

from nullsum.lending_iou import IouReplayer, generate_iou_ops, iou_fails
from nullsum.repro import ddmin

STEP_EPS = [0, "0.6", 1, "1.5", 2]
CUM_BOUNDS = ["0.6", 1, 2]


def fmt(d) -> str:
    return format(d.quantize(type(d)("0.0001")), "f")


def table(c, only_bad=True) -> str:
    out = [f"  ledger #{c.ledger_index}: {c.tx_summaries}"]
    for r in c.rows:
        if r.asset != "XRP" and (not only_bad or r.residual != 0):
            out.append(f"    {r.party:9s} {r.asset}: expected={fmt(r.expected)} actual={fmt(r.actual)} residual={fmt(r.residual)}")
    out.append(f"    aggregate IOU sum diff (actual-expected, all tracked parties) = {fmt(c.global_diff)}")
    return "\n".join(out)


def sweep(run) -> str:
    def w(c):
        return f"ledger #{c.ledger_index}" if c else "NEVER"
    lines = [f"  observed over {len(run.closes)} closes / {run.n_loanpay_ok} LoanPay ok: "
             f"max per-step |residual change| = {fmt(run.max_step)}, max cumulative |residual| = {fmt(run.max_cum)}"]
    lines.append("  per-step detector (flags if any party's residual changes by more than eps in one ledger):")
    for e in STEP_EPS:
        lines.append(f"      eps={str(e):4s}: first flag {w(run.first_step(e))}")
    lines.append("  cumulative detector (flags if any party's residual since genesis exceeds bound):")
    for b in CUM_BOUNDS:
        lines.append(f"      bound={str(b):4s}: first flag {w(run.first_cum(b))}")
    lines.append(f"  unexpected tx result (not tesSUCCESS): first flag {w(run.first_result())}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--label", default="iou")
    p.add_argument("--fee", type=int, default=888)
    p.add_argument("--payments", type=int, default=7)
    p.add_argument("--principal", type=int, default=3_000_000_000_000_000)
    p.add_argument("--env", default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--shrink-bound", default=None, help="ddmin with the cumulative detector at this bound")
    p.add_argument("--max-evals", type=int, default=150)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    rep = IouReplayer(a.binary, a.label, json.loads(a.env) if a.env else None)
    ops = generate_iou_ops(a.payments, a.principal, a.fee)
    t0 = time.time()
    r = rep.replay(ops)
    print(f"[{a.label}] fee={a.fee} payments={a.payments}: {len(ops)} ops ({r.ops_total - r.ops_skipped} applied), "
          f"{len(r.closes)} closes ({time.time()-t0:.1f}s)")
    if r.errors:
        print("  HARNESS ERRORS:", r.errors[:3])
    if r.crashed:
        print("  NODE STOPPED RESPONDING during replay:", r.crashed)
    for m in r.invariant_log[:2]:
        print("  rippled invariant log:", m)
    print(sweep(r))
    if a.verbose:
        for c in r.closes:
            print(table(c, only_bad=False))
    worst = max(r.closes, key=lambda c: c.cum_max, default=None)
    if worst and worst.cum_max > 0:
        print("LEDGER WITH LARGEST CUMULATIVE RESIDUAL:")
        print(table(worst, only_bad=True))
    if a.shrink_bound is not None and not r.crashed:
        pred = iou_fails(rep, float(a.shrink_bound))
        if not pred(ops):
            print(f"shrink: cumulative bound {a.shrink_bound} does not flag this run; nothing to shrink")
        else:
            e0, t0 = rep.evals, time.time()
            minimal = ddmin(ops, pred, max_evals=a.max_evals)
            print(f"\nddmin[cumulative>{a.shrink_bound}]: {len(ops)} ops -> {len(minimal)} ops, {rep.evals - e0} node replays, {time.time()-t0:.0f}s")
            print("MINIMAL REPRO:")
            for i, o in enumerate(minimal, 1):
                print(f"  {i}. {o}")
            fin = rep.replay(minimal)
            print("re-verify on the minimal repro:")
            print(sweep(fin))
            c = fin.first_cum(float(a.shrink_bound))
            if c:
                print(table(c))
            if a.out:
                json.dump({"label": a.label, "bound": a.shrink_bound, "original_ops": len(ops),
                           "minimal": [o.__dict__ for o in minimal]}, open(a.out, "w"), indent=2)
    # Non-zero only for a harness/transport problem (an expected in-process mutant abort is a
    # valid measurement, so it stays 0); this lets a matrix script tell a broken run apart.
    if r.errors or (r.crashed and "harness/transport" in str(r.crashed)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
