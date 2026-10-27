"""Lending scenario driver + per-party EXPECTED-balance oracle (Nullsum phase 2).

Scenario (one MPT asset, zero-interest loan so every expectation is exact integer maths):

  fund parties (XRP) -> MPTokenIssuanceCreate -> MPTokenAuthorize -> issuer pays MPT ->
  VaultCreate -> VaultDeposit -> LoanBrokerSet -> LoanSet (owner + borrower countersign) ->
  LoanPay x N

ORACLE INDEPENDENCE.  The expected balance of every party is computed here, from the
transactions THIS HARNESS submitted plus the genesis baseline, with the semantics derived
from the loan terms we chose (periodic payment = principal / N, plus the flat service fee):

    LoanPay:  borrower -= principal/N + fee   vault pseudo-account += principal/N
              broker owner += fee             broker pseudo-account += 0

rippled's own aggregates (header `total_coins`, OutstandingAmount, fee counters) are NEVER
used for a verdict.  They are recorded as diagnostics only.  Only a transaction's committed
RESULT CODE (tesSUCCESS / tec*) is read back from the closed ledger to know whether the
effect applied, and per-party balances are read from committed ledger entries.

Three detectors run over the same per-party residuals (residual = actual - expected):
  strict_step   : any party's residual CHANGED by more than 0 in this ledger
  tol_step(eps) : any party's residual changed by more than eps in this ledger.  eps models the
                  rounding allowance a single-snapshot check needs for non-integral assets.
  cumulative(eps): any party's residual, accumulated since genesis, exceeds eps (a bound that
                  does NOT grow with the number of ledgers).
A "global" check (sum of all MPT held by tracked parties == sum expected) is also reported,
to show what an aggregate-only check sees.
"""
from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from xrpl.core.addresscodec import decode_classic_address
from xrpl.models.amounts import MPTAmount
from xrpl.models.currencies import MPTCurrency
from xrpl.models.transactions import (
    LoanBrokerSet,
    LoanPay,
    MPTokenAuthorize,
    MPTokenIssuanceCreate,
    Payment,
    VaultCreate,
    VaultDeposit,
)
from xrpl.transaction import sign

from .model import wallet_for
from .rpc import NodeController, RpcClient, RpcError

AMENDMENTS = ["MPTokensV1", "SingleAssetVault", "LendingProtocol"]
FEE = 1000  # drops; every lending tx in the scenario uses this explicit fee
NOISE_FEE = 10
FUND_DROPS = 1_000_000_000  # 1000 XRP per party
ISSUER, OWNER, BORROWER = 1, 2, 3  # wallet indices (0 = genesis master)
NAMES = {0: "master", 1: "issuer", 2: "owner", 3: "borrower"}


@dataclass(frozen=True)
class LOp:
    kind: str
    a: int = 0
    b: int = 0
    v: int = 0

    def __str__(self) -> str:
        k = self.kind
        if k == "fund":
            return f"fund({NAMES[self.a]}, 1000 XRP)"
        if k == "mptcreate":
            return "MPTokenIssuanceCreate(issuer)"
        if k == "auth":
            return f"MPTokenAuthorize({NAMES[self.a]})"
        if k == "mint":
            return f"issuer pays {self.v} MPT -> {NAMES[self.a]}"
        if k == "vaultcreate":
            return "VaultCreate(owner)"
        if k == "deposit":
            return f"VaultDeposit(owner, {self.v})"
        if k == "brokerset":
            return "LoanBrokerSet(owner)"
        if k == "loanset":
            return (f"LoanSet(owner<->borrower, principal={self.a}, payments={self.b}, "
                    f"interest=0, service_fee={self.v})")
        if k == "loanpay":
            return "LoanPay(borrower)"
        if k == "xrppay":
            return f"XRP Payment({NAMES[self.a]} -> {NAMES[self.b]}, {self.v} drops)"
        return "close()"


def generate_lending_ops(seed: int, n_payments: int, fee: int, noise: int = 1) -> List[LOp]:
    import random

    rng = random.Random(seed)
    ops: List[LOp] = []
    for p in (ISSUER, OWNER, BORROWER):
        ops.append(LOp("fund", a=p))
    ops += [LOp("mptcreate"), LOp("auth", a=OWNER), LOp("auth", a=BORROWER),
            LOp("mint", a=OWNER, v=200_000), LOp("mint", a=BORROWER, v=200_000),
            LOp("vaultcreate"), LOp("deposit", v=100_000), LOp("brokerset"),
            LOp("loanset", a=n_payments * 1000, b=n_payments, v=fee)]
    for _ in range(n_payments):
        for _ in range(rng.randint(0, noise)):
            s, d = rng.sample([ISSUER, OWNER, BORROWER], 2)
            ops.append(LOp("xrppay", a=s, b=d, v=rng.randint(1, 5_000_000)))
        ops.append(LOp("loanpay"))
        if rng.random() < 0.3:
            ops.append(LOp("deposit", v=rng.randint(1, 500)))
    ops.append(LOp("close"))
    return ops


# ---------------------------------------------------------------------------------------------
@dataclass
class PartyRow:
    party: str
    asset: str  # "XRP" or "MPT"
    expected: int
    actual: int

    @property
    def residual(self) -> int:
        return self.actual - self.expected


@dataclass
class LClose:
    ledger_index: int
    tx_summaries: List[str]
    rows: List[PartyRow]
    step_residual_max: int  # max over parties of |residual change this ledger|
    cum_residual_max: int  # max over parties of |cumulative residual|
    global_mpt_ok: bool
    diag_outstanding: Optional[int]  # rippled OutstandingAmount (diagnostic only)
    diag_expected_outstanding: int
    unexpected: List[str] = field(default_factory=list)  # committed results != tesSUCCESS for txs this harness expected to succeed

    def bad_rows(self) -> List[PartyRow]:
        return [r for r in self.rows if r.residual != 0]


@dataclass
class LRun:
    binary: str
    label: str
    closes: List[LClose] = field(default_factory=list)
    ops_total: int = 0
    ops_skipped: int = 0
    crashed: Optional[str] = None  # node process died; text from its own log
    crash_op: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    submit_log: List[str] = field(default_factory=list)
    invariant_log: List[str] = field(default_factory=list)  # rippled's own 'Invariant failed' messages

    def first_close(self, pred) -> Optional[LClose]:
        for c in self.closes:
            if pred(c):
                return c
        return None

    # detectors
    def detect_strict(self): return self.first_close(lambda c: c.step_residual_max > 0)
    def detect_tol_step(self, eps: int = 1): return self.first_close(lambda c: c.step_residual_max > eps)
    def detect_cumulative(self, eps: int = 1): return self.first_close(lambda c: c.cum_residual_max > eps)
    def detect_global(self): return self.first_close(lambda c: not c.global_mpt_ok)
    def detect_result(self): return self.first_close(lambda c: bool(c.unexpected))

    @property
    def n_loanpay_ok(self) -> int:
        return sum(1 for c in self.closes for t in c.tx_summaries if t.startswith("LoanPay tesSUCCESS"))


class LendingReplayer:
    def __init__(self, binary: str, label: str = "", env: Optional[Dict[str, str]] = None):
        self.binary, self.label, self.env = binary, label, env
        self.evals = 0

    def replay(self, ops: List[LOp]) -> LRun:
        self.evals += 1
        run = LRun(self.binary, self.label, ops_total=len(ops))
        node = NodeController(self.binary, features=AMENDMENTS, env=self.env, keep_dir=True)
        try:
            c = node.start()
            try:
                self._drive(run, node, c, ops)
            except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
                run.crashed = self._crash_text(node, e)
        finally:
            try:
                for fn in ("debug.log", "stdout.log"):
                    for line in open(f"{node.workdir}/{fn}", errors="replace"):
                        if "nvariant failed" in line and line not in run.invariant_log:
                            run.invariant_log.append(line.strip()[-220:])
            except Exception:
                pass
            node.keep_dir = False
            node.stop()
        return run

    @staticmethod
    def _crash_text(node: NodeController, exc: Exception) -> str:
        try:
            node.proc.wait(timeout=5)
        except Exception:
            pass
        rc = node.proc.poll()
        tail = ""
        try:
            tail = open(f"{node.workdir}/stdout.log", errors="replace").read()[-600:]
        except Exception:
            pass
        full = ""
        try:
            full = open(f"{node.workdir}/stdout.log", errors="replace").read()
        except Exception:
            pass
        asserts = [l for l in full.splitlines() if "ssertion" in l]
        what = asserts[-1].split("src/libxrpl/")[-1][:200] if asserts else " | ".join(
            [l for l in tail.splitlines() if l.strip()][-2:])
        # An in-process abort shows up as a negative rc (e.g. SIGABRT rc=-6) and/or an
        # assertion line in the log. Absent both, this is more likely a harness/transport
        # error and must NOT be attributed to an in-ledger check.
        aborted = bool(asserts) or (rc is not None and rc < 0)
        if aborted:
            lead = (f"node process aborted (rc={rc}) — consistent with an in-process "
                    f"assertion/invariant; see the node log")
        else:
            lead = (f"harness/transport error ({type(exc).__name__}, rc={rc}) — node may not "
                    f"have aborted; check the node log before attributing to rippled")
        return f"{lead}; {what}"

    # ------------------------------------------------------------------------------------
    def _drive(self, run: LRun, node: NodeController, c: RpcClient, ops: List[LOp]) -> None:
        W = {i: wallet_for(i) for i in range(4)}
        addr = {i: W[i].classic_address for i in range(4)}
        # --- independent expectation state (built ONLY from what we submit) ---------------
        exp_xrp: Dict[int, int] = {}
        exp_mpt: Dict[str, int] = {}  # keyed by role name
        genesis = int(c.call("account_info", account=addr[0], ledger_index="closed")["account_data"]["Balance"])
        exp_xrp[0] = genesis
        prev_resid: Dict[Tuple[str, str], int] = {}
        funded = {0}
        authed = set()
        st: Dict[str, object] = {"mptid": None, "vault": None, "vaultP": None, "broker": None,
                                 "brokerP": None, "loan": None, "remaining": 0, "per": 0, "fee": 0,
                                 "principal": 0}
        # pending[hash] = (label, effect callable applied on tesSUCCESS, sender index, fee)
        pending: Dict[str, Tuple[str, object, int, int]] = {}
        expected_actual_cache: Dict[str, int] = {}

        def seq(i: int) -> int:
            return int(c.call("account_info", account=addr[i], ledger_index="current")["account_data"]["Sequence"])

        def role_addr(role: str) -> Optional[str]:
            if role in ("owner", "borrower"):
                return addr[OWNER if role == "owner" else BORROWER]
            return st.get(role)  # vaultP / brokerP

        def mpt_actual(a: str, idx: int) -> int:
            if not st["mptid"] or not a:
                return 0
            try:
                r = c.call("ledger_entry", mptoken={"mpt_issuance_id": st["mptid"], "account": a},
                           ledger_index=idx)
                return int(r["node"].get("MPTAmount", "0"))
            except RpcError:
                return 0

        def xrp_actual(i: int, idx: int) -> int:
            try:
                return int(c.call("account_info", account=addr[i], ledger_index=idx)["account_data"]["Balance"])
            except RpcError:
                return 0

        def submit(label: str, blob: str, sender: int, fee: int, effect) -> Optional[str]:
            r = c.call("submit", tx_blob=blob)
            er = r.get("engine_result", "?")
            run.submit_log.append(f"{label}: {er}")
            if er == "tesSUCCESS" or er.startswith("tec"):
                pending[r["tx_json"]["hash"]] = (label, effect, sender, fee)
                return r["tx_json"]["hash"]
            return None

        def sign_send(tx_cls, i: int, effect, fee: int = FEE, **kw) -> Optional[str]:
            tx = tx_cls(account=addr[i], fee=str(fee), sequence=seq(i), **kw)
            return submit(tx_cls.__name__, sign(tx, W[i]).blob(), i, fee, effect)

        def close() -> Optional[dict]:
            a = c.call("ledger_accept")
            idx = int(a["ledger_current_index"]) - 1
            led = c.call("ledger", ledger_index=idx, transactions=True, expand=True)["ledger"]
            summaries, seen = [], set()
            created_nodes = []
            for t in led.get("transactions", []):
                h = t.get("hash")
                meta = t.get("metaData") or t.get("meta") or {}
                res = meta.get("TransactionResult", "?")
                if h in pending:
                    label, effect, sender, fee = pending[h]
                    seen.add(h)
                    exp_xrp[sender] = exp_xrp.get(sender, 0) - fee  # fee we set, applied or claimed
                    if res == "tesSUCCESS":
                        effect()
                    summaries.append(f"{label} {res}")
                    for n in meta.get("AffectedNodes", []):
                        if "CreatedNode" in n:
                            created_nodes.append((label, n["CreatedNode"]))
                else:
                    summaries.append(f"UNKNOWN-TX {t.get('TransactionType')} {res}")
                    run.errors.append(f"ledger {idx}: transaction not submitted by harness: {h}")
            for h in list(pending):
                if h in seen:
                    del pending[h]
            # discover IDs from committed metadata
            for label, cn in created_nodes:
                et = cn["LedgerEntryType"]
                if label == "MPTokenIssuanceCreate" and et == "MPTokenIssuance":
                    st["mptid"] = format(cn["NewFields"]["Sequence"], "08X") + \
                        decode_classic_address(addr[ISSUER]).hex().upper()
                elif label == "VaultCreate" and et == "Vault":
                    st["vault"], st["vaultP"] = cn["LedgerIndex"], cn["NewFields"]["Account"]
                elif label == "LoanBrokerSet" and et == "LoanBroker":
                    st["broker"], st["brokerP"] = cn["LedgerIndex"], cn["NewFields"]["Account"]
                elif et == "Loan":
                    st["loan"] = cn["LedgerIndex"]
            # -------- per-party rows ----------------------------------------------------
            rows: List[PartyRow] = []
            for i in (0, 1, 2, 3):
                rows.append(PartyRow(NAMES[i], "XRP", exp_xrp.get(i, 0) if i in funded or i == 0 else 0,
                                     xrp_actual(i, idx) if (i in funded or i == 0) else 0))
            if st["mptid"]:
                for role in ("owner", "borrower", "vaultP", "brokerP"):
                    rows.append(PartyRow(role, "MPT", exp_mpt.get(role, 0), mpt_actual(role_addr(role), idx)))
            step_max, cum_max = 0, 0
            for r in rows:
                key = (r.party, r.asset)
                step_max = max(step_max, abs(r.residual - prev_resid.get(key, 0)))
                cum_max = max(cum_max, abs(r.residual))
                prev_resid[key] = r.residual
            mpt_rows = [r for r in rows if r.asset == "MPT"]
            global_ok = sum(r.actual for r in mpt_rows) == sum(r.expected for r in mpt_rows)
            diag = None
            if st["mptid"]:
                try:
                    diag = int(c.call("ledger_entry", mpt_issuance=st["mptid"], ledger_index=idx)["node"]
                               .get("OutstandingAmount", "0"))
                except RpcError:
                    diag = None
            unexpected = [t for t in summaries if not t.endswith("tesSUCCESS")]
            lc = LClose(idx, summaries, rows, step_max, cum_max, global_ok, diag,
                        int(st.get("issued", 0)), unexpected)
            run.closes.append(lc)
            return lc

        def flush() -> None:
            if pending:
                close()

        for op in ops:
            k = op.kind
            if k == "close":
                flush()
                continue
            if k == "xrppay":
                if op.a not in funded or op.b not in funded or op.a == op.b:
                    run.ops_skipped += 1
                    continue
                def eff(s=op.a, d=op.b, v=op.v):
                    exp_xrp[s] = exp_xrp.get(s, 0) - v
                    exp_xrp[d] = exp_xrp.get(d, 0) + v
                tx = Payment(account=addr[op.a], destination=addr[op.b], amount=str(op.v),
                             fee=str(NOISE_FEE), sequence=seq(op.a))
                submit("Payment", sign(tx, W[op.a]).blob(), op.a, NOISE_FEE, eff)
                continue
            flush()  # every lending op is submitted against fully-committed state
            h: Optional[str] = None
            if k == "fund":
                if op.a in funded:
                    run.ops_skipped += 1; continue
                def eff(p=op.a):
                    exp_xrp[0] -= FUND_DROPS; exp_xrp[p] = exp_xrp.get(p, 0) + FUND_DROPS; funded.add(p)
                h = sign_send(Payment, 0, eff, fee=NOISE_FEE, destination=addr[op.a], amount=str(FUND_DROPS))
            elif k == "mptcreate":
                if ISSUER not in funded or st["mptid"]:
                    run.ops_skipped += 1; continue
                h = sign_send(MPTokenIssuanceCreate, ISSUER, lambda: None, flags=0x20,
                              maximum_amount="1000000000")
            elif k == "auth":
                if not st["mptid"] or op.a not in funded or op.a in authed:
                    run.ops_skipped += 1; continue
                def eff(p=op.a):
                    authed.add(p)
                h = sign_send(MPTokenAuthorize, op.a, eff, mptoken_issuance_id=st["mptid"])
            elif k == "mint":
                if not st["mptid"] or op.a not in authed:
                    run.ops_skipped += 1; continue
                role = NAMES[op.a]
                def eff(r=role, v=op.v):
                    exp_mpt[r] = exp_mpt.get(r, 0) + v
                    st["issued"] = int(st.get("issued", 0)) + v
                h = sign_send(Payment, ISSUER, eff, destination=addr[op.a],
                              amount=MPTAmount(mpt_issuance_id=st["mptid"], value=str(op.v)))
            elif k == "vaultcreate":
                if not st["mptid"] or OWNER not in funded or st["vault"]:
                    run.ops_skipped += 1; continue
                h = sign_send(VaultCreate, OWNER, lambda: None, asset=MPTCurrency(mpt_issuance_id=st["mptid"]))
            elif k == "deposit":
                if not st["vault"] or exp_mpt.get("owner", 0) < op.v:
                    run.ops_skipped += 1; continue
                def eff(v=op.v):
                    exp_mpt["owner"] -= v; exp_mpt["vaultP"] = exp_mpt.get("vaultP", 0) + v
                h = sign_send(VaultDeposit, OWNER, eff, vault_id=st["vault"],
                              amount=MPTAmount(mpt_issuance_id=st["mptid"], value=str(op.v)))
            elif k == "brokerset":
                if not st["vault"] or st["broker"]:
                    run.ops_skipped += 1; continue
                h = sign_send(LoanBrokerSet, OWNER, lambda: None, vault_id=st["vault"], management_fee_rate=0)
            elif k == "loanset":
                if (not st["broker"] or st["loan"] or BORROWER not in authed
                        or exp_mpt.get("vaultP", 0) < op.a or op.b <= 0 or op.a % op.b):
                    run.ops_skipped += 1; continue
                def eff(a=op.a, n=op.b, f=op.v):
                    exp_mpt["vaultP"] -= a; exp_mpt["borrower"] = exp_mpt.get("borrower", 0) + a
                    st["remaining"], st["per"], st["fee"], st["principal"] = n, a // n, f, a
                txj = {"TransactionType": "LoanSet", "Account": addr[OWNER], "Fee": str(FEE),
                       "Sequence": seq(OWNER), "LoanBrokerID": st["broker"],
                       "Counterparty": addr[BORROWER], "PrincipalRequested": str(op.a),
                       "InterestRate": 0, "PaymentTotal": op.b, "PaymentInterval": 60,
                       "LoanServiceFee": str(op.v)}
                s1 = c.call("sign", seed=W[OWNER].seed, key_type="ed25519", tx_json=txj)
                s2 = c.call("sign", seed=W[BORROWER].seed, key_type="ed25519",
                            signature_target="CounterpartySignature", tx_json=s1["tx_json"])
                h = submit("LoanSet", s2["tx_blob"], OWNER, FEE, eff)
            elif k == "loanpay":
                due = int(st["per"]) + int(st["fee"])
                if not st["loan"] or int(st["remaining"]) <= 0 or exp_mpt.get("borrower", 0) < due:
                    run.ops_skipped += 1; continue
                def eff(per=int(st["per"]), fee=int(st["fee"])):
                    exp_mpt["borrower"] -= per + fee
                    exp_mpt["vaultP"] += per
                    exp_mpt["owner"] += fee
                    st["remaining"] = int(st["remaining"]) - 1
                run.submit_log.append(f"-> LoanPay amount {due}")
                h = sign_send(LoanPay, BORROWER, eff, loan_id=st["loan"],
                              amount=MPTAmount(mpt_issuance_id=st["mptid"], value=str(due)))
            else:
                raise ValueError(k)
            if h is None:
                run.ops_skipped += 1
            close()
        flush()


def lending_fails(rep: "LendingReplayer", detector: str = "cumulative", eps: int = 1):
    def _f(sub: List[LOp]) -> bool:
        r = rep.replay(sub)
        if r.crashed:
            return False
        if detector == "result":
            return r.detect_result() is not None
        if detector == "strict":
            return r.detect_strict() is not None
        if detector == "tol_step":
            return r.detect_tol_step(eps) is not None
        return r.detect_cumulative(eps) is not None
    return _f
