"""Nullsum phase 3: IOU-asset lending scenario where LEGITIMATE rounding creates per-step noise.

Why this scenario exists
------------------------
With an integral MPT asset every expected delta is an exact integer, so a strict (eps=0)
per-step check is already sufficient and "cumulative drift" adds nothing.  An IOU is a
16-significant-digit decimal.  Here the loan principal is 3e15 split over 7 payments, so the
exact per-payment principal is 428571428571428.571428...; rippled rounds each payment to a whole
unit (alternating ...428 / ...429 so the principal still sums exactly), and party balances of
~1e15..1e16 can only hold whole units.  The independent oracle computes the EXACT rational
expectation, so every ledger shows a small legitimate residual (< 1 unit).  A per-step check
therefore NEEDS a tolerance; the question this module answers empirically is whether a
below-tolerance skim, which the per-step check cannot see, shows up as CUMULATIVE residual.

Expectations are computed in the harness from the transactions it submits (Decimal, 60 digits);
rippled's aggregate counters are never consulted.
"""
from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, getcontext
from typing import Dict, List, Optional, Tuple

from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import (
    AccountSet,
    LoanBrokerSet,
    LoanPay,
    Payment,
    TrustSet,
    VaultCreate,
    VaultDeposit,
)
from xrpl.transaction import sign

from .lending import LendingReplayer
from .model import wallet_for
from .rpc import NodeController, RpcClient, RpcError

getcontext().prec = 60
AMENDMENTS = ["MPTokensV1", "SingleAssetVault", "LendingProtocol"]
FEE = 1000
FUND_DROPS = 1_000_000_000
CUR = "USD"
ISSUER, OWNER, BORROWER = 1, 2, 3
NAMES = {0: "master", 1: "issuer", 2: "owner", 3: "borrower"}
D = Decimal


@dataclass(frozen=True)
class IOp:
    kind: str
    a: int = 0
    b: int = 0
    v: int = 0  # integral IOU units (or service fee); principal in a

    def __str__(self) -> str:
        k = self.kind
        if k == "fund":
            return f"fund({NAMES[self.a]}, 1000 XRP)"
        if k == "defaultripple":
            return "AccountSet(issuer, DefaultRipple)"
        if k == "trust":
            return f"TrustSet({NAMES[self.a]} -> issuer, {CUR})"
        if k == "mint":
            return f"issuer pays {self.v} {CUR} -> {NAMES[self.a]}"
        if k == "vaultcreate":
            return f"VaultCreate(owner, asset={CUR}, scale=0)"
        if k == "deposit":
            return f"VaultDeposit(owner, {self.v} {CUR})"
        if k == "brokerset":
            return "LoanBrokerSet(owner)"
        if k == "loanset":
            return (f"LoanSet(owner<->borrower, principal={self.a} {CUR}, payments={self.b}, "
                    f"interest=0, service_fee={self.v})")
        if k == "loanpay":
            return "LoanPay(borrower)"
        return "close()"


def generate_iou_ops(n_payments: int = 7, principal: int = 3_000_000_000_000_000, fee: int = 888) -> List[IOp]:
    ops = [IOp("fund", a=p) for p in (ISSUER, OWNER, BORROWER)]
    ops += [IOp("defaultripple"), IOp("trust", a=OWNER), IOp("trust", a=BORROWER),
            IOp("mint", a=OWNER, v=9_000_000_000_000_000), IOp("mint", a=BORROWER, v=5_000_000_000_000_000),
            IOp("vaultcreate"), IOp("deposit", v=5_000_000_000_000_000), IOp("brokerset"),
            IOp("loanset", a=principal, b=n_payments, v=fee)]
    ops += [IOp("loanpay") for _ in range(n_payments)]
    ops.append(IOp("close"))
    return ops


@dataclass
class IRow:
    party: str
    asset: str
    expected: Decimal
    actual: Decimal

    @property
    def residual(self) -> Decimal:
        return self.actual - self.expected


@dataclass
class IClose:
    ledger_index: int
    tx_summaries: List[str]
    rows: List[IRow]
    step_max: Decimal  # max |change of residual this ledger| over IOU parties
    cum_max: Decimal  # max |cumulative residual| over IOU parties
    unexpected: List[str]
    global_ok: bool
    global_diff: Decimal


@dataclass
class IRun:
    label: str
    closes: List[IClose] = field(default_factory=list)
    crashed: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    invariant_log: List[str] = field(default_factory=list)
    ops_total: int = 0
    ops_skipped: int = 0

    def first_step(self, eps) -> Optional[IClose]:
        return next((c for c in self.closes if c.step_max > D(str(eps))), None)

    def first_cum(self, bound) -> Optional[IClose]:
        return next((c for c in self.closes if c.cum_max > D(str(bound))), None)

    def first_result(self) -> Optional[IClose]:
        return next((c for c in self.closes if c.unexpected), None)

    @property
    def max_step(self) -> Decimal:
        return max((c.step_max for c in self.closes), default=D(0))

    @property
    def max_cum(self) -> Decimal:
        return max((c.cum_max for c in self.closes), default=D(0))

    @property
    def n_loanpay_ok(self) -> int:
        return sum(1 for c in self.closes for t in c.tx_summaries if t.startswith("LoanPay tesSUCCESS"))


class IouReplayer:
    def __init__(self, binary: str, label: str = "", env: Optional[Dict[str, str]] = None):
        self.binary, self.label, self.env = binary, label, env
        self.evals = 0

    def replay(self, ops: List[IOp]) -> IRun:
        self.evals += 1
        run = IRun(self.label, ops_total=len(ops))
        node = NodeController(self.binary, features=AMENDMENTS, env=self.env, keep_dir=True)
        try:
            c = node.start()
            try:
                self._drive(run, c, ops)
            except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
                run.crashed = LendingReplayer._crash_text(node, e)
        finally:
            try:
                for fn in ("debug.log", "stdout.log"):
                    for line in open(f"{node.workdir}/{fn}", errors="replace"):
                        if "nvariant failed" in line and line.strip()[-220:] not in run.invariant_log:
                            run.invariant_log.append(line.strip()[-220:])
            except Exception:
                pass
            node.keep_dir = False
            node.stop()
        return run

    def _drive(self, run: IRun, c: RpcClient, ops: List[IOp]) -> None:
        W = {i: wallet_for(i) for i in range(4)}
        addr = {i: W[i].classic_address for i in range(4)}
        exp_xrp: Dict[int, int] = {}
        exp_iou: Dict[str, Decimal] = {}
        exp_xrp[0] = int(c.call("account_info", account=addr[0], ledger_index="closed")["account_data"]["Balance"])
        funded = {0}
        st: Dict[str, object] = {"vault": None, "vaultP": None, "broker": None, "brokerP": None,
                                 "loan": None, "remaining": 0, "per": D(0), "fee": D(0), "trusted": set()}
        pending: Dict[str, Tuple[str, object, int, int]] = {}
        prev_resid: Dict[Tuple[str, str], Decimal] = {}
        issuer_addr = addr[ISSUER]

        def seq(i: int) -> int:
            return int(c.call("account_info", account=addr[i], ledger_index="current")["account_data"]["Sequence"])

        def role_addr(role: str) -> Optional[str]:
            return {"owner": addr[OWNER], "borrower": addr[BORROWER]}.get(role) or st.get(role)

        def iou_actual(a: Optional[str], idx: int) -> Decimal:
            if not a:
                return D(0)
            try:
                lines = c.call("account_lines", account=a, peer=issuer_addr, ledger_index=idx)["lines"]
            except RpcError:
                return D(0)
            for ln in lines:
                if ln["currency"] == CUR:
                    return D(ln["balance"])
            return D(0)

        def xrp_actual(i: int, idx: int) -> int:
            try:
                return int(c.call("account_info", account=addr[i], ledger_index=idx)["account_data"]["Balance"])
            except RpcError:
                return 0

        def iou(v) -> IssuedCurrencyAmount:
            return IssuedCurrencyAmount(currency=CUR, issuer=issuer_addr, value=format(D(v), "f"))

        def submit(label: str, blob: str, sender: int, fee: int, effect) -> Optional[str]:
            r = c.call("submit", tx_blob=blob)
            er = r.get("engine_result", "?")
            if er == "tesSUCCESS" or er.startswith("tec"):
                pending[r["tx_json"]["hash"]] = (label, effect, sender, fee)
                return r["tx_json"]["hash"]
            run.errors.append(f"{label}: {er}")
            return None

        def sign_send(tx_cls, i: int, effect, fee: int = FEE, **kw) -> Optional[str]:
            tx = tx_cls(account=addr[i], fee=str(fee), sequence=seq(i), **kw)
            return submit(tx_cls.__name__, sign(tx, W[i]).blob(), i, fee, effect)

        def close() -> None:
            a = c.call("ledger_accept")
            idx = int(a["ledger_current_index"]) - 1
            led = c.call("ledger", ledger_index=idx, transactions=True, expand=True)["ledger"]
            summaries, created_nodes = [], []
            for t in led.get("transactions", []):
                h = t.get("hash")
                meta = t.get("metaData") or t.get("meta") or {}
                res = meta.get("TransactionResult", "?")
                if h in pending:
                    label, effect, sender, fee = pending.pop(h)
                    exp_xrp[sender] = exp_xrp.get(sender, 0) - fee
                    if res == "tesSUCCESS":
                        effect()
                    summaries.append(f"{label} {res}")
                    for n in meta.get("AffectedNodes", []):
                        if "CreatedNode" in n:
                            created_nodes.append((label, n["CreatedNode"]))
                else:
                    summaries.append(f"UNKNOWN-TX {t.get('TransactionType')} {res}")
                    run.errors.append(f"ledger {idx}: foreign tx {h}")
            for label, cn in created_nodes:
                et = cn["LedgerEntryType"]
                if label == "VaultCreate" and et == "Vault":
                    st["vault"], st["vaultP"] = cn["LedgerIndex"], cn["NewFields"]["Account"]
                elif label == "LoanBrokerSet" and et == "LoanBroker":
                    st["broker"], st["brokerP"] = cn["LedgerIndex"], cn["NewFields"]["Account"]
                elif et == "Loan":
                    st["loan"] = cn["LedgerIndex"]
            rows: List[IRow] = []
            for i in (0, 1, 2, 3):
                on = i in funded or i == 0
                rows.append(IRow(NAMES[i], "XRP", D(exp_xrp.get(i, 0)) if on else D(0), D(xrp_actual(i, idx)) if on else D(0)))
            for role in ("owner", "borrower", "vaultP", "brokerP"):
                rows.append(IRow(role, CUR, exp_iou.get(role, D(0)), iou_actual(role_addr(role), idx)))
            step_max, cum_max = D(0), D(0)
            for r in rows:
                key = (r.party, r.asset)
                if r.asset == CUR:
                    step_max = max(step_max, abs(r.residual - prev_resid.get(key, D(0))))
                    cum_max = max(cum_max, abs(r.residual))
                prev_resid[key] = r.residual
            irows = [r for r in rows if r.asset == CUR]
            gdiff = sum((r.actual for r in irows), D(0)) - sum((r.expected for r in irows), D(0))
            run.closes.append(IClose(idx, summaries, rows, step_max, cum_max,
                                     [t for t in summaries if not t.endswith("tesSUCCESS")],
                                     abs(gdiff) <= D("1"), gdiff))

        def flush() -> None:
            if pending:
                close()

        for op in ops:
            k = op.kind
            if k == "close":
                flush(); continue
            flush()
            h: Optional[str] = None
            if k == "fund":
                if op.a in funded:
                    run.ops_skipped += 1; continue
                def eff(p=op.a):
                    exp_xrp[0] -= FUND_DROPS; exp_xrp[p] = exp_xrp.get(p, 0) + FUND_DROPS; funded.add(p)
                h = sign_send(Payment, 0, eff, fee=10, destination=addr[op.a], amount=str(FUND_DROPS))
            elif k == "defaultripple":
                if ISSUER not in funded:
                    run.ops_skipped += 1; continue
                h = sign_send(AccountSet, ISSUER, lambda: None, set_flag=8)
            elif k == "trust":
                if op.a not in funded or op.a in st["trusted"]:
                    run.ops_skipped += 1; continue
                def eff(p=op.a):
                    st["trusted"].add(p)
                h = sign_send(TrustSet, op.a, eff, limit_amount=iou("1e30"))
            elif k == "mint":
                if op.a not in st["trusted"]:
                    run.ops_skipped += 1; continue
                def eff(r=NAMES[op.a], v=op.v):
                    exp_iou[r] = exp_iou.get(r, D(0)) + D(v)
                h = sign_send(Payment, ISSUER, eff, destination=addr[op.a], amount=iou(op.v))
            elif k == "vaultcreate":
                if OWNER not in st["trusted"] or st["vault"]:
                    run.ops_skipped += 1; continue
                h = sign_send(VaultCreate, OWNER, lambda: None,
                              asset=IssuedCurrency(currency=CUR, issuer=issuer_addr), scale=0)
            elif k == "deposit":
                if not st["vault"] or exp_iou.get("owner", D(0)) < op.v:
                    run.ops_skipped += 1; continue
                def eff(v=op.v):
                    exp_iou["owner"] -= D(v); exp_iou["vaultP"] = exp_iou.get("vaultP", D(0)) + D(v)
                h = sign_send(VaultDeposit, OWNER, eff, vault_id=st["vault"], amount=iou(op.v))
            elif k == "brokerset":
                if not st["vault"] or st["broker"]:
                    run.ops_skipped += 1; continue
                h = sign_send(LoanBrokerSet, OWNER, lambda: None, vault_id=st["vault"], management_fee_rate=0)
            elif k == "loanset":
                if (not st["broker"] or st["loan"] or BORROWER not in st["trusted"]
                        or exp_iou.get("vaultP", D(0)) < op.a or op.b <= 0):
                    run.ops_skipped += 1; continue
                def eff(a=op.a, n=op.b, f=op.v):
                    exp_iou["vaultP"] -= D(a); exp_iou["borrower"] = exp_iou.get("borrower", D(0)) + D(a)
                    st["remaining"], st["per"], st["fee"] = n, D(a) / D(n), D(f)  # EXACT rational share
                txj = {"TransactionType": "LoanSet", "Account": addr[OWNER], "Fee": str(FEE),
                       "Sequence": seq(OWNER), "LoanBrokerID": st["broker"], "Counterparty": addr[BORROWER],
                       "PrincipalRequested": str(op.a), "InterestRate": 0, "PaymentTotal": op.b,
                       "PaymentInterval": 60, "LoanServiceFee": str(op.v)}
                s1 = c.call("sign", seed=W[OWNER].seed, key_type="ed25519", tx_json=txj)
                s2 = c.call("sign", seed=W[BORROWER].seed, key_type="ed25519",
                            signature_target="CounterpartySignature", tx_json=s1["tx_json"])
                h = submit("LoanSet", s2["tx_blob"], OWNER, FEE, eff)
            elif k == "loanpay":
                if not st["loan"] or int(st["remaining"]) <= 0:
                    run.ops_skipped += 1; continue
                per, fee = st["per"], st["fee"]
                amount = (per + fee).quantize(D(1), rounding=ROUND_CEILING)  # whole units, >= exact due
                if exp_iou.get("borrower", D(0)) < amount:
                    run.ops_skipped += 1; continue
                def eff(per=per, fee=fee):
                    exp_iou["borrower"] -= per + fee
                    exp_iou["vaultP"] += per
                    exp_iou["owner"] += fee
                    st["remaining"] = int(st["remaining"]) - 1
                h = sign_send(LoanPay, BORROWER, eff, loan_id=st["loan"], amount=iou(amount))
            else:
                raise ValueError(k)
            if h is None:
                run.ops_skipped += 1
            close()
        flush()


def iou_fails(rep: IouReplayer, bound: float):
    def _f(sub: List[IOp]) -> bool:
        r = rep.replay(sub)
        return (not r.crashed) and r.first_cum(bound) is not None
    return _f
