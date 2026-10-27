"""Minimal-reproduction reduction via delta debugging (ddmin).

Given a failing operation sequence and a predicate `still_fails(subsequence)` --
which REPLAYS that subsequence against a FRESH standalone node and runs the
oracle -- `ddmin` reduces it in chunks down toward single operations and returns
a subsequence that is locally minimal with respect to its chunk schedule (it does
not separately test removing the final remaining singleton).

This is a *defined* reduction algorithm with an explicit evaluation budget, not a
vague "minimization". It is a LOCAL, within-budget reduction, not global
minimality: `max_evals` bounds the number of node replays so a demo cannot run
away, and if the budget is hit it returns the smallest sequence found so far.
"""
from __future__ import annotations

from typing import Callable, List, Sequence, TypeVar

T = TypeVar("T")


def ddmin(
    seq: Sequence[T],
    still_fails: Callable[[List[T]], bool],
    max_evals: int = 500,
) -> List[T]:
    work: List[T] = list(seq)
    evals = 0
    n = 2
    while len(work) >= 2:
        chunk = max(1, len(work) // n)
        reduced = False
        i = 0
        while i < len(work):
            complement = work[:i] + work[i + chunk :]
            if evals >= max_evals:
                return work
            evals += 1
            if len(complement) < len(work) and still_fails(complement):
                work = complement
                n = max(n - 1, 2)
                reduced = True
                break
            i += chunk
        if not reduced:
            if n >= len(work):
                break
            n = min(len(work), n * 2)
    return work
