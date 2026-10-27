"""Pure-Python unit tests for the conservation oracle and the ddmin reducer.

Run from the repo root with: python -m pytest -q
These exercise nullsum/oracles.py and nullsum/repro.py with no node.
"""
from __future__ import annotations

from nullsum.oracles import check_xrp_conservation, sum_balances
from nullsum.repro import ddmin


# --- oracle ---------------------------------------------------------------

def test_conserved_when_total_falls_by_exactly_fees():
    r = check_xrp_conservation(total_before=1_000_000, total_after=999_990, fees_burned=10)
    assert r.ok
    assert r.created == 0


def test_creation_from_nothing_is_a_violation():
    # total should have fallen to 999_990; it only fell to 999_995 -> +5 drops created
    r = check_xrp_conservation(total_before=1_000_000, total_after=999_995, fees_burned=10)
    assert not r.ok
    assert r.created == 5
    assert "created" in r.detail


def test_destruction_beyond_fees_is_a_violation():
    # fell further than the fees account for -> negative created (destroyed)
    r = check_xrp_conservation(total_before=1_000_000, total_after=999_980, fees_burned=10)
    assert not r.ok
    assert r.created == -10
    assert "destroyed" in r.detail


def test_zero_fee_conserved():
    r = check_xrp_conservation(total_before=500, total_after=500, fees_burned=0)
    assert r.ok and r.created == 0


def test_sum_balances_coerces_to_int():
    assert sum_balances(["10", 20, "30"]) == 60


# --- ddmin ----------------------------------------------------------------

def test_ddmin_isolates_the_required_pair():
    seq = [1, 2, 3, 4, 5, 6, 7, 8]
    # the "failure" needs both 2 and 6 present
    fails = lambda sub: 2 in sub and 6 in sub
    out = ddmin(seq, fails)
    assert fails(out)
    assert set(out) == {2, 6}  # locally minimal: nothing extra survives


def test_ddmin_isolates_a_single_required_element():
    seq = list(range(1, 9))
    out = ddmin(seq, lambda sub: 5 in sub)
    assert out == [5]


def test_ddmin_respects_eval_budget():
    calls = {"n": 0}

    def fails(sub):
        calls["n"] += 1
        return 3 in sub and 7 in sub

    ddmin(list(range(1, 20)), fails, max_evals=5)
    assert calls["n"] <= 5


def test_ddmin_preserves_order():
    seq = ["a", "b", "c", "d", "e"]
    out = ddmin(seq, lambda sub: "b" in sub and "d" in sub)
    assert out == ["b", "d"]
