#!/usr/bin/env python3
"""Nullsum phase-1 XRP demo: baseline negative control, mutant catch, ddmin shrink.

Usage:
  python run_demo.py baseline --binary /path/to/xrpld --seeds 1 2 3
  python run_demo.py mutant   --binary /path/to/mutant/xrpld --baseline-binary /path/to/xrpld
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from nullsum.model import SENTINEL_DROPS, Replayer, fails, generate_ops, Op
from nullsum.repro import ddmin


def describe_close(c) -> str:
    lines = [
        f"  ledger #{c.ledger_index}: {c.oracle.detail}",
        f"    total_before={c.oracle.total_before} total_after={c.oracle.total_after} "
        f"fees_burned={c.oracle.fees_burned} created={c.oracle.created:+d} drops",
        f"    (self-check) sum of ALL AccountRoots={c.all_accountroots_sum} "
        f"tracked={c.tracked_sum} header total_coins={c.total_coins}",
    ]
    for t in c.txs:
        lines.append(f"    tx {t.hash[:12]}.. {t.type} {t.result} amount={t.amount}")
    return "\n".join(lines)


def run_baseline(args) -> int:
    rep = Replayer(args.binary)
    bad = 0
    for seed in args.seeds:
        ops = generate_ops(seed, args.accounts, args.payments)
        sent = sum(1 for o in ops if o.kind == "pay" and o.drops == SENTINEL_DROPS)
        t0 = time.time()
        r = rep.replay(ops)
        print(f"[baseline seed={seed}] ({sent} sentinel pays in sequence, {time.time()-t0:.1f}s) {r.summary()}")
        if r.violations or r.universe_open:
            bad += 1
            for c in r.violations:
                print(describe_close(c))
    print(f"BASELINE RESULT: {'conservation held on every close of every run' if not bad else 'FAILED'}")
    return 1 if bad else 0


def run_mutant(args) -> int:
    rep = Replayer(args.binary)
    seed = args.seeds[0]
    ops = generate_ops(seed, args.accounts, args.payments)
    sent = [o for o in ops if o.kind == "pay" and o.drops == SENTINEL_DROPS]
    print(f"[mutant seed={seed}] sequence: {len(ops)} ops, {len(sent)} sentinel pays")
    r = rep.replay(ops)
    print(f"[mutant seed={seed}] {r.summary()}")
    v = r.first_violation
    if not v:
        print("MUTANT RESULT: no violation detected")
        return 1
    print("OFFENDING CLOSE(S):")
    for c in r.violations:
        print(describe_close(c))
    n_vio = len(r.violations)
    print(f"violating closes: {n_vio}, matching sentinel pays that were applied: "
          f"{sum(1 for c in r.closes for t in c.txs if t.type == 'Payment' and t.amount == str(SENTINEL_DROPS) and t.result == 'tesSUCCESS')}")

    print("\n=== ddmin shrink ===")
    base_evals = rep.evals
    t0 = time.time()
    minimal = ddmin(ops, fails(rep), max_evals=args.max_evals)
    print(f"ddmin: {len(ops)} ops -> {len(minimal)} ops in {rep.evals - base_evals} node replays, {time.time()-t0:.0f}s")
    print("MINIMAL REPRO:")
    for i, o in enumerate(minimal, 1):
        print(f"  {i}. {o}")
    final = rep.replay(minimal)
    print(f"re-verify on mutant: {final.summary()}")
    for c in final.violations:
        print(describe_close(c))
    if args.baseline_binary:
        b = Replayer(args.baseline_binary).replay(minimal)
        print(f"same minimal repro on BASELINE: {b.summary()}")
    with open(args.out, "w") as f:
        json.dump({"seed": seed, "original_ops": len(ops),
                   "minimal": [o.__dict__ for o in minimal],
                   "violation_created_drops": final.first_violation.oracle.created if final.first_violation else None,
                   "violation_ledger": final.first_violation.ledger_index if final.first_violation else None},
                  f, indent=2)
    print(f"wrote {args.out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["baseline", "mutant"])
    p.add_argument("--binary", required=True)
    p.add_argument("--baseline-binary", default=None)
    p.add_argument("--seeds", type=int, nargs="+", default=[1])
    p.add_argument("--accounts", type=int, default=6)
    p.add_argument("--payments", type=int, default=40)
    p.add_argument("--max-evals", type=int, default=200)
    p.add_argument("--out", default="minimal_repro.json")
    args = p.parse_args()
    return run_baseline(args) if args.mode == "baseline" else run_mutant(args)


if __name__ == "__main__":
    sys.exit(main())
