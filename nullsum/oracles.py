"""XRP conservation oracle (MVP scope).

Scope, stated honestly:
  * Covers XRP held in ACCOUNT ROOTS only. Escrow, payment-channel, and
    pseudo-account (vault / AMM / loan) XRP are NOT summed here -- documented
    future work, not claimed by this MVP.
  * Correct only for a CLOSED universe: every account funded from genesis and
    tracked, including the genesis account itself. The harness guarantees this
    by construction in standalone mode.

Principle: XRP is never created. Across a ledger close, the sum of all
account-root balances must fall by EXACTLY the drops burned as transaction fees.
Anything else is a conservation violation:

    created = total_after - (total_before - fees_burned)
      created > 0  -> XRP created from nothing
      created < 0  -> XRP destroyed beyond fees

Both are violations. This oracle is pure: it makes no network calls, so it can
be unit-tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ConservationResult:
    ok: bool
    total_before: int  # drops
    total_after: int  # drops
    fees_burned: int  # drops
    created: int  # signed drops; 0 == conserved
    detail: str


def sum_balances(balances_drops: Iterable[int]) -> int:
    """Sum account-root balances given as integer drops."""
    return sum(int(b) for b in balances_drops)


def check_xrp_conservation(
    total_before: int, total_after: int, fees_burned: int
) -> ConservationResult:
    created = int(total_after) - (int(total_before) - int(fees_burned))
    ok = created == 0
    if ok:
        detail = "conserved: total fell by exactly the fees burned"
    elif created > 0:
        detail = f"VIOLATION: {created} drops created from nothing"
    else:
        detail = f"VIOLATION: {-created} drops destroyed beyond fees"
    return ConservationResult(
        ok=ok,
        total_before=int(total_before),
        total_after=int(total_after),
        fees_burned=int(fees_burned),
        created=created,
        detail=detail,
    )
