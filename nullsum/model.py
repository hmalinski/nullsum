"""Random op-sequence model + replay harness for the closed-universe XRP oracle.

An *op sequence* is a flat list of `Op`s. Replaying it against a FRESH standalone
node (new genesis) is deterministic: wallets are derived from account indices,
and the ledger is advanced only by the explicit `close` ops (via the
`ledger_accept` admin method, not consensus).

Index 0 is the genesis/master account. Indices 1..N are funded accounts. Ops
that are impossible in a given (possibly ddmin-reduced) sequence -- e.g. a
`pay` whose source was never funded -- are skipped and counted, never sent, so
the universe stays closed and every subsequence is a valid input for `ddmin`.

After EVERY close, the harness reads COMMITTED state back from the node:
  * `ledger` (expanded) for the closed ledger's transactions -> fees burned,
    per-tx engine results;
  * `account_info` at the closed ledger index for every tracked account;
and calls `oracles.check_xrp_conservation(before, after, fees)`.

As a harness self-check (not part of the oracle), it also sums EVERY
AccountRoot in the closed ledger via `ledger_data`; if that differs from the
tracked-account sum, the universe was not closed and the run is flagged
`universe_open` instead of being trusted.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from xrpl.constants import CryptoAlgorithm
from xrpl.core import keypairs
from xrpl.models.transactions import Payment
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from .oracles import ConservationResult, check_xrp_conservation
from .rpc import NodeController, RpcClient, RpcError

MASTER_SEED = "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"  # secret of "masterpassphrase"
MASTER_ADDRESS = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
FEE_DROPS = 10
FUND_DROPS = 100_000_000  # 100 XRP per funded account
# The mutant's Edit A fires on exactly this delivered amount (drops). The
# model draws it occasionally in EVERY run, baseline included, so the very same
# sequence is a fair differential test.
SENTINEL_DROPS = 1_234_567


@dataclass(frozen=True)
class Op:
    kind: str  # "fund" | "pay" | "close"
    src: int = -1
    dst: int = -1
    drops: int = 0

    def __str__(self) -> str:
        if self.kind == "fund":
            return f"fund(acct{self.dst})"
        if self.kind == "pay":
            s = "master" if self.src == 0 else f"acct{self.src}"
            d = "master" if self.dst == 0 else f"acct{self.dst}"
            tag = "  <- sentinel amount" if self.drops == SENTINEL_DROPS else ""
            return f"pay({s} -> {d}, {self.drops} drops){tag}"
        return "close()"


def generate_ops(
    seed: int, n_accounts: int = 6, n_pay: int = 40, p_sentinel: float = 0.04
) -> List[Op]:
    """Deterministic random sequence: fund everyone, then random payments with
    ledger closes interleaved. Payment amounts stay below the base reserve so a
    payment can never create an untracked account."""
    rng = random.Random(seed)
    ops: List[Op] = []
    for i in range(1, n_accounts + 1):
        ops.append(Op("fund", dst=i))
        if rng.random() < 0.5:
            ops.append(Op("close"))
    ops.append(Op("close"))
    for _ in range(n_pay):
        src = rng.randint(0, n_accounts)
        dst = rng.randint(0, n_accounts)
        while dst == src:
            dst = rng.randint(0, n_accounts)
        amt = SENTINEL_DROPS if rng.random() < p_sentinel else rng.randint(1, 5_000_000)
        ops.append(Op("pay", src=src, dst=dst, drops=amt))
        if rng.random() < 0.35:
            ops.append(Op("close"))
    ops.append(Op("close"))
    return ops


def wallet_for(index: int) -> Wallet:
    if index == 0:
        # The genesis "masterpassphrase" key is SECP256K1 (address rHb9...). Pin it
        # explicitly: xrpl-py 4.x defaults from_seed() to ED25519 (which derives a
        # different, wrong address and breaks every funding op); 5.x auto-detects. The
        # explicit algorithm keeps derivation correct on either.
        return Wallet.from_seed(MASTER_SEED, algorithm=CryptoAlgorithm.SECP256K1)
    # Frozen derivation salt: pins the deterministic test-account addresses so the
    # captured demo-output reproduces exactly. Do not rename (breaks recorded repros).
    entropy = hashlib.sha256(f"conservator-acct-{index}".encode()).digest()[:16]
    seed = keypairs.generate_seed(entropy=entropy.hex(), algorithm=CryptoAlgorithm.ED25519)
    return Wallet.from_seed(seed, algorithm=CryptoAlgorithm.ED25519)


@dataclass
class TxRecord:
    hash: str
    type: str
    result: str
    account: str
    destination: str
    amount: str


@dataclass
class CloseReport:
    ledger_index: int
    oracle: ConservationResult
    txs: List[TxRecord]
    tracked_sum: int
    all_accountroots_sum: int
    total_coins: int

    @property
    def universe_open(self) -> bool:
        return self.tracked_sum != self.all_accountroots_sum


@dataclass
class RunReport:
    node_binary: str
    ops_total: int
    ops_skipped: int
    closes: List[CloseReport] = field(default_factory=list)
    submit_log: List[str] = field(default_factory=list)

    @property
    def violations(self) -> List[CloseReport]:
        return [c for c in self.closes if not c.oracle.ok]

    @property
    def first_violation(self) -> Optional[CloseReport]:
        v = self.violations
        return v[0] if v else None

    @property
    def universe_open(self) -> bool:
        return any(c.universe_open for c in self.closes)

    def summary(self) -> str:
        n_tx = sum(len(c.txs) for c in self.closes)
        n_ok = sum(1 for c in self.closes for t in c.txs if t.result == "tesSUCCESS")
        fees = sum(c.oracle.fees_burned for c in self.closes)
        net = sum(c.oracle.created for c in self.closes)
        status = "CONSERVED" if not self.violations else f"VIOLATION x{len(self.violations)}"
        return (
            f"{status}: {len(self.closes)} ledger closes, {n_tx} txs ({n_ok} tesSUCCESS), "
            f"{self.ops_total - self.ops_skipped}/{self.ops_total} ops applied, "
            f"fees burned {fees} drops, net created {net:+d} drops, "
            f"universe_open={self.universe_open}"
        )


class Replayer:
    def __init__(self, node_binary: str):
        self.node_binary = node_binary
        self._wallets: Dict[int, Wallet] = {}
        self.evals = 0

    def wallet(self, i: int) -> Wallet:
        if i not in self._wallets:
            self._wallets[i] = wallet_for(i)
        return self._wallets[i]

    # ---- node reads (committed state only) ---------------------------------
    @staticmethod
    def _balance(client: RpcClient, addr: str, ledger_index) -> Optional[int]:
        try:
            r = client.call("account_info", account=addr, ledger_index=ledger_index)
        except RpcError as e:
            if "actNotFound" in str(e):
                return None
            raise
        return int(r["account_data"]["Balance"])

    @staticmethod
    def _all_accountroot_sum(client: RpcClient, ledger_index: int) -> int:
        total, marker = 0, None
        while True:
            params = {"ledger_index": ledger_index, "type": "account", "limit": 400}
            if marker:
                params["marker"] = marker
            r = client.call("ledger_data", **params)
            for e in r["state"]:
                if e.get("LedgerEntryType") == "AccountRoot":
                    total += int(e["Balance"])
            marker = r.get("marker")
            if not marker:
                return total

    # ---- replay -------------------------------------------------------------
    def replay(self, ops: List[Op], keep_node_dir: bool = False) -> RunReport:
        self.evals += 1
        report = RunReport(self.node_binary, len(ops), 0)
        with NodeController(self.node_binary, keep_dir=keep_node_dir) as node:
            c = node.client
            funded = {0}  # optimistic: master + successfully-submitted funds
            seq: Dict[int, int] = {}
            pending = 0
            prev_idx = int(c.call("ledger", ledger_index="closed")["ledger"]["ledger_index"])
            prev_total = self._balance(c, MASTER_ADDRESS, prev_idx)
            assert prev_total is not None, "genesis account missing"

            def next_seq(i: int) -> int:
                if i not in seq:
                    r = c.call(
                        "account_info",
                        account=self.wallet(i).classic_address,
                        ledger_index="current",
                    )
                    seq[i] = int(r["account_data"]["Sequence"])
                return seq[i]

            def send(src: int, dst: int, drops: int) -> bool:
                nonlocal pending
                w = self.wallet(src)
                tx = Payment(
                    account=w.classic_address,
                    destination=self.wallet(dst).classic_address,
                    amount=str(drops),
                    fee=str(FEE_DROPS),
                    sequence=next_seq(src),
                )
                blob = sign(tx, w).blob()
                r = c.call("submit", tx_blob=blob)
                er = r.get("engine_result", "?")
                report.submit_log.append(f"submit {src}->{dst} {drops}: {er}")
                if er == "tesSUCCESS" or er.startswith("tec"):
                    seq[src] = seq[src] + 1
                    pending += 1
                return er == "tesSUCCESS"

            def close() -> None:
                nonlocal pending, prev_idx, prev_total
                a = c.call("ledger_accept")
                idx = int(a["ledger_current_index"]) - 1
                led = c.call("ledger", ledger_index=idx, transactions=True, expand=True)["ledger"]
                txs, fees = [], 0
                for t in led.get("transactions", []):
                    fees += int(t["Fee"])
                    meta = t.get("metaData") or t.get("meta") or {}
                    amt = t.get("Amount", "")
                    txs.append(
                        TxRecord(
                            t.get("hash", ""),
                            t["TransactionType"],
                            meta.get("TransactionResult", "?"),
                            t.get("Account", ""),
                            t.get("Destination", ""),
                            amt if isinstance(amt, str) else str(amt),
                        )
                    )
                total_after, exists = 0, set()
                for i in sorted(funded | {0}):
                    b = self._balance(c, self.wallet(i).classic_address, idx)
                    if b is not None:
                        total_after += b
                        exists.add(i)
                funded.clear()
                funded.update(exists)
                ar_sum = self._all_accountroot_sum(c, idx)
                res = check_xrp_conservation(prev_total, total_after, fees)
                report.closes.append(
                    CloseReport(idx, res, txs, total_after, ar_sum, int(led["total_coins"]))
                )
                seq.clear()  # resync sequences from committed state
                prev_idx, prev_total, pending = idx, total_after, 0

            for op in ops:
                if op.kind == "close":
                    close()
                elif op.kind == "fund":
                    if op.dst in funded or op.dst == 0:
                        report.ops_skipped += 1
                    elif send(0, op.dst, FUND_DROPS):
                        funded.add(op.dst)
                    else:
                        report.ops_skipped += 1
                elif op.kind == "pay":
                    if op.src not in funded or op.dst not in funded or op.src == op.dst:
                        report.ops_skipped += 1
                    else:
                        send(op.src, op.dst, op.drops)
                else:
                    raise ValueError(op.kind)
            if pending:  # implicit final close so every submitted tx is checked
                close()
        return report


def fails(replayer: Replayer):
    """Predicate for ddmin: does this subsequence reproduce a conservation violation?"""

    def _f(sub: List[Op]) -> bool:
        return replayer.replay(sub).first_violation is not None

    return _f
