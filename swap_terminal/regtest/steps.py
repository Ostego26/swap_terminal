"""The nine stages, run against one chain.

Role: module (the stages the root entry point runs; each stage owns one
      question and holds no decision of its own -- the decisions are in
      regtest.txbuild, regtest.keys, and the REAL modules under test)
Reads: a regtest daemon over JSON-RPC; the real modules
       modules.atomic_htlc_scripts, modules.htlc_timelock, modules.utils,
       modules.atomic_btc_client, modules.atomic_ltc_client
Writes: the regtest chain and the regtest wallet -- mined blocks, funded
       contracts, broadcast spends
Can move funds: YES on regtest, and nowhere else. regtest.daemons.assert_regtest
       has already refused anything but `regtest` before any of this runs.
Mainnet-safe: NO.

WHAT IS UNDER TEST AND WHAT IS THE INSTRUMENT, BECAUSE THE DIFFERENCE IS THE
WHOLE VALUE OF THE OUTPUT.

  under test   modules/atomic_htlc_scripts.build_htlc_redeem_script -- the
               script, both branches, the locktime encoding.
               modules/htlc_timelock.contract_locktime -- the number in it.
               BTCClient.create_contract / redeem_contract and
               LTCClient.create_contract / redeem_contract -- the real fund and
               redeem paths, called with real arguments against a real daemon.

  instrument   regtest.txbuild -- a spender written for this harness, used
               because NO refund implementation exists in the tree to drive.
               Every line it produces on screen is labeled `control`.

THE FAILURE THIS HARNESS EXPECTS, AND WHY IT MUST NOT BE WRITTEN AROUND.

All three clients' `redeem_contract()` accept a `secret: bytes` parameter and
never reference it. That was established by walking each function's AST and is
recorded in all three module headers. What follows from it -- that the
scriptSig those functions build cannot satisfy the hashlock branch -- had
never been confirmed against a chain by anything in this repository. Step 7
is that confirmation. It is expected to FAIL, it is reported as XFAIL rather
than FAIL, and the assertion is not adjusted to let it pass.

Step 7 then runs the control redeem on the SAME contract output. Between the
two, the run answers the question that was previously unanswerable: whether a
funded contract is spendable by its hashlock branch at all, or only by refund.
If the real call fails and the control succeeds, the script is sound and the
client is the defect. If both fail, the script is the defect. Those two
verdicts have completely different fixes, and no amount of reading tells them
apart.

WHY STEP 8 HAS TWO PARTS.

"Refund before expiry is rejected" can be true for two unrelated reasons, and
only one of them says anything about CLTV:

  8a  nLockTime = the script's locktime, tip = locktime - 1. The MEMPOOL
      refuses it as non-final before any script runs. This proves the timelock
      is enforced end to end, and proves nothing at all about the opcode.
  8b  nLockTime = the current tip, which is final, so the script DOES run --
      and CHECKLOCKTIMEVERIFY compares the script's locktime against that
      smaller nLockTime and fails. This is the one that proves the opcode is
      enforcing something, and it is the assertion the refund branch has never
      had.

Both must be rejected. A refund that succeeds at 8b would mean the locktime in
the script is not the one the builder was asked for -- which is exactly the
defect this repository shipped until 2026-09-24, when the locktime was encoded
as a varint and 500,000 was enforced as 128,000,254.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from decimal import Decimal

from chains.base import RPCError
from microfortnights import format_duration
from modules.atomic_htlc_scripts import (
    build_htlc_redeem_script,
    parse_and_reencode_as_testnet_p2pkh,
    script_to_p2sh_address,
)
from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime, describe_locktime
from modules.utils import generate_secret, sha256_hash
from regtest import daemons
from regtest.console import FAIL, OK, SKIP, XFAIL, Console, redact
from regtest.daemons import COINBASE_MATURITY_HEIGHT, ChainConfig, RegtestSetupError
from regtest.keys import RegtestKey, generate_key, hash160
from regtest.txbuild import Outpoint, build_branch_spend, coins_to_satoshis, describe_script_sig

TOTAL_STEPS = 9

# How much goes into each contract, and the fee each control spend pays. Both
# are coin amounts as strings so the satoshi conversion is exact.
CONTRACT_AMOUNT = "1.0"
CONTROL_FEE = "0.0001"

# generatetoaddress is called in batches so that mining a four-figure number of
# blocks prints progress instead of sitting silent (rule 14). A COUNT OF
# BLOCKS, never a duration.
MINE_BATCH_BLOCKS = 144

# sendrawtransaction's maxfeerate, in coin per kvB. Zero disables the check.
# The control spends pay a flat 0.0001 on a ~250-byte transaction, which is
# roughly 0.4 coin/kvB -- four times the 0.10 default -- so without this every
# control spend is refused as `absurdly-high-fee` and the harness reports a fee
# policy as if it were a script failure. The flat fee is kept (rather than
# reduced) because it is the same constant the real redeem_contract() hardcodes,
# and a harness that quietly pays a different fee is not measuring the same
# thing.
NO_FEE_LIMIT = 0

# The `max_wait` BTCClient.create_contract() passes to wait_for_tx_output().
# SECONDS, because that is the unit the real client uses at its own call site;
# it is rendered in microfortnights wherever the harness prints it (rule 6).
# Spelled here only so the announcement can state the scale up front.
REAL_CLIENT_WAIT_SECONDS = 300


@dataclass
class SpawnRecord:
    """Whether THIS run spawned the daemon, recorded the instant it happens.

    THIS EXISTS BECAUSE THE OBVIOUS VERSION LEAKED A PROCESS (rule 13). It was
    first written as `we_started_it = steps.step_2_daemon(run)` in the entry
    point, which assigns only if step 2 RETURNS. Step 2 spawns the daemon and
    then waits up to a minute for RPC, and that wait raises RegtestSetupError
    on timeout -- so a daemon that started but answered slowly was spawned,
    never recorded, and then deliberately left running by the teardown, which
    believed it had adopted something it had actually started. The next run
    fails on the datadir lock, which reads like a permissions problem.

    A mutable record set BEFORE anything that can raise closes that window:
    the spawn and its reap are one change, and the reaper must never depend on
    the spawner returning normally.
    """

    started: bool = False


@dataclass(frozen=True)
class Run:
    """The three things every stage needs: where to print, which chain, which wallet.

    They travel together because they ARE one thing -- a run against one chain
    -- and passing them separately pushed most of this file's functions past
    the argument ceiling. The ceiling was right: a stage that takes
    (console, config, wallet, contract, outpoint, outcome) is orchestration
    carrying its context by hand. CLAUDE.md rule 12 is explicit that the fix
    for a complexity finding is to extract, not to raise the ceiling, and rule
    19 forbids a `noqa` that only quiets the count.
    """

    console: Console
    config: ChainConfig
    wallet: str = ""
    # Mutable on purpose, inside a frozen Run: see SpawnRecord. The teardown in
    # regtest_htlc_verify.main() reads this, and it must be true by the time
    # anything after the spawn can raise.
    spawn: SpawnRecord = field(default_factory=SpawnRecord)

    @property
    def asset(self) -> str:
        return self.config.asset

    def node(self, wallet: str | None = None):
        """An RPCAdapter for this chain, at the wallet endpoint unless told otherwise."""
        return daemons.adapter_for(self.config, wallet=self.wallet if wallet is None else wallet)

    def say(self, text: str) -> None:
        """A detail line, already prefixed with which chain is speaking."""
        self.console.say(f"{self.asset}: {text}")

    def check(self, label: str, got: object, expected: object, outcome: str) -> str:
        return self.console.check(f"{self.asset} {label}", got, expected, outcome)

    def step(self, number: int, title: str) -> None:
        self.console.step(number, self.asset, title)


@dataclass
class ChainOutcome:
    """What this chain's run established, for the verdict at the end."""

    asset: str
    real_create_contract: str = SKIP
    real_redeem: str = SKIP
    # WHICH STAGE the real redeem failed at, when it failed. Recorded because
    # the verdict must not say "redeem_contract() cannot spend the hashlock
    # branch" about a call that never reached the signing step -- see
    # classify_redeem_failure().
    real_redeem_stage: str = ""
    control_redeem: str = SKIP
    refund_before_expiry_rejected: str = SKIP
    refund_after_expiry: str = SKIP
    notes: list[str] = field(default_factory=list)

    def verdict(self) -> str:
        """One sentence naming which branch of a funded contract actually works.

        This is the single most valuable line the harness prints, so it is
        assembled from the recorded outcomes rather than from anything a step
        decided in passing.
        """
        hashlock_by_real_client = self.real_redeem == OK
        hashlock_by_control = self.control_redeem == OK
        timelock = self.refund_after_expiry == OK
        if hashlock_by_real_client and timelock:
            return "both branches spend: the real redeem_contract() worked and the refund worked"
        if hashlock_by_control and timelock:
            if self.real_redeem_stage == REDEEM_FAILED_ON_LOOKUP:
                # The real call fell over before signing, so this run cannot
                # say whether it would have pushed the preimage. Say what was
                # measured and not one word more (rule 17).
                return (
                    "the SCRIPT is sound -- both branches spend when the scriptSig is built correctly. The real "
                    "redeem_contract() was NOT judged on the hashlock branch: it failed earlier, on its own "
                    "getrawtransaction lookup of the contract, and never reached signing. What is measured is that "
                    "a funded contract is spendable by refund and by a correctly built hashlock spend; whether "
                    "redeem_contract() pushes the preimage remains inferred from the source."
                )
            return (
                "the SCRIPT is sound -- both branches spend when the scriptSig is built correctly -- but the real "
                "redeem_contract() CANNOT spend the hashlock branch. A funded contract is recoverable in practice "
                "only by refund, until redeem_contract() pushes the preimage."
            )
        if timelock and not hashlock_by_control:
            return (
                "ONLY THE REFUND BRANCH SPENDS. Neither the real client nor a correctly built scriptSig could take "
                "the hashlock branch, which points at the redeem SCRIPT rather than at the client."
            )
        if hashlock_by_control and not timelock:
            return "the hashlock branch spends and the REFUND branch does not -- the timelock path is broken"
        return "neither branch was shown to spend; read the failures above before concluding anything"


@dataclass
class Contract:
    """One funded HTLC output and everything needed to spend it."""

    redeem_script: bytes
    p2sh_address: str
    p2sh_script: bytes
    locktime: int
    secret: bytes
    secret_hash: bytes
    participant: RegtestKey
    refund: RegtestKey
    outpoint: Outpoint | None = None
    funded_by: str = "(none: not funded)"


def _p2sh_script_for(redeem_script: bytes) -> bytes:
    """OP_HASH160 <20-byte script hash> OP_EQUAL.

    Built from the hash rather than from a rendered address on purpose. Bitcoin
    and Litecoin disagree about the base58 version byte for P2SH on
    testnet/regtest -- 0xC4 on Bitcoin, and Litecoin prints 0x3A while still
    accepting 0xC4 -- so an assertion written against an address STRING fails on
    one chain for a reason that has nothing to do with the contract. The
    scriptPubKey is the same bytes on both.
    """
    script_hash = hash160(redeem_script)
    return b"\xa9" + bytes([len(script_hash)]) + script_hash + b"\x87"


# --------------------------------------------------------------------------
# steps 1-4: the chain is up, on regtest, with a wallet and spendable coins
# --------------------------------------------------------------------------


def step_1_binaries(run: Run) -> None:
    run.step(1, "binaries present, versions printed")
    daemons.check_binaries(run.console, run.config)


def step_2_daemon(run: Run) -> bool:
    run.step(2, f"start the daemon on regtest and wait for RPC at {run.config.base_url}")
    run.say(
        f"datadir={run.config.datadir}  conf={run.config.conf_path}  "
        f"rpcport={run.config.port}  rpcuser={run.config.rpc_user}"
    )
    we_started_it = daemons.start_daemon(run.console, run.config)
    # Recorded BEFORE the wait below, which can raise. See SpawnRecord.
    run.spawn.started = we_started_it
    daemons.wait_for_rpc(run.console, run.config)
    # THE UNCONDITIONAL REFUSAL, and it is the first thing asserted after the
    # connection exists. Everything below this line mines or broadcasts.
    info = daemons.assert_regtest(run.console, run.config)
    run.say(f"chain={info.get('chain')} blocks={info.get('blocks')} (a height, not a duration)")
    return we_started_it


def step_3_wallet(run: Run) -> dict:
    run.step(3, f"create or load the wallet {run.wallet!r}")
    daemons.ensure_wallet(run.console, run.config, run.wallet)
    capabilities = daemons.probe_capabilities(run.console, run.config, wallet=run.wallet)
    if capabilities.get("descriptor_wallet") is True:
        run.say(
            "this is a DESCRIPTOR wallet. importaddress and importprivkey exist but refuse on one, so the real "
            "create_contract()'s import step is expected to fail here -- that failure is reported, not worked around."
        )
    return capabilities


@dataclass(frozen=True)
class Mined:
    """What a mining call produced: the new tip, and the hashes of the blocks.

    THE HASHES ARE THE POINT, and they were being thrown away. `generatetoaddress`
    already returns them, and a block hash is what lets `getrawtransaction` read
    a CONFIRMED transaction back on a node with no `-txindex`. See _verbose_tx()
    for the failure that taught this.
    """

    height: int
    hashes: list[str]

    @property
    def first_hash(self) -> str | None:
        """The block a transaction broadcast just before this mine landed in.

        None rather than an exception when nothing was mined: the caller passes
        it to _verbose_tx(), which has two further routes, and a missing hash
        should degrade to the next route rather than abort the step.
        """
        return self.hashes[0] if self.hashes else None


def _mine(run: Run, blocks: int) -> Mined:
    """Mine `blocks` blocks in batches, printing progress. Returns tip and hashes."""
    node = run.node()
    address = node.call("getnewaddress", "regtest-harness-mining")
    started = time.monotonic()
    remaining = blocks
    hashes: list[str] = []
    while remaining > 0:
        batch = min(MINE_BATCH_BLOCKS, remaining)
        hashes.extend(node.call("generatetoaddress", batch, address) or [])
        remaining -= batch
        height = int(node.call("getblockcount"))
        run.say(
            f"mined {blocks - remaining}/{blocks} blocks, height={height} (a height, not a duration), "
            f"{format_duration(time.monotonic() - started)} elapsed"
        )
    return Mined(height=int(node.call("getblockcount")), hashes=hashes)


def _verbose_tx(node, txid: str, block_hash: str | None = None) -> dict:
    """Read a transaction back as a decoded dict, WITHOUT requiring -txindex.

    MEASURED ON THE OPERATOR'S MACHINE, 2026-09-25, first run of this harness.
    The funding transaction was sent and mined, and then:

        RPCError: {'code': -5, 'message': 'No such mempool transaction. Use
        -txindex or provide a block hash to enable blockchain transaction
        queries. Use gettransaction for wallet transactions.'}

    A bare `getrawtransaction <txid> true` only ever searched the MEMPOOL, and
    the transaction had just been mined out of it. This killed BTC before steps
    7, 8 and 9 ran -- so the one question the harness exists to answer went
    unmeasured because of a lookup, not because of anything about HTLCs.

    The daemon's own error names all three fixes and this takes two of them,
    in order, and deliberately not the third:

      1. WITH A BLOCK HASH. `getrawtransaction <txid> true <blockhash>` reads
         the block directly. Every caller here mines the block itself and so
         already knows the hash -- `generatetoaddress` returns it -- which
         makes this the exact route the daemon asked for. It also works for a
         transaction the WALLET DOES NOT OWN, which the redeem and refund
         spends are: their inputs are a P2SH the wallet never imported and
         their outputs pay keys generated inside this harness.

      2. THE WALLET'S OWN RECORD. `gettransaction <txid>` needs no index and
         carries the raw hex plus the confirmation count, which
         `decoderawtransaction` turns into the same shape. It works on both
         Bitcoin Core 28.1 and Litecoin 0.21.4. It only knows transactions the
         wallet was involved in, which covers the funding but not the spends.

      3. NOT `-txindex=1`. Adding it to the daemon's arguments changes the
         datadir's indexes and forces a reindex on an operator who already has
         a regtest chain. A harness must not silently re-shape the machine it
         is measured on.

    The confirmation count is spliced in from whichever route supplied it,
    because `decoderawtransaction` does not carry one and every caller asserts
    on it.
    """
    attempts: list[str] = []
    if block_hash:
        try:
            return node.call("getrawtransaction", txid, True, block_hash)
        except Exception as exc:  # noqa: BLE001 -- checked: a failure of ONE route is not an answer, it is a reason to try the next. Every attempt is collected and re-raised together below, so no route can return a value a caller would mistake for a real transaction.
            attempts.append(f"getrawtransaction with blockhash: {exc}")
    try:
        wallet_record = node.call("gettransaction", txid)
        decoded = node.call("decoderawtransaction", wallet_record["hex"])
        decoded["confirmations"] = int(wallet_record.get("confirmations", 0))
        if wallet_record.get("blockhash"):
            decoded["blockhash"] = wallet_record["blockhash"]
        return decoded
    except Exception as exc:  # noqa: BLE001 -- checked: same. This route only knows wallet transactions, so its failure is expected for the redeem and refund spends and must not end the step.
        attempts.append(f"gettransaction + decoderawtransaction: {exc}")
    try:
        return node.call("getrawtransaction", txid, True)
    except Exception as exc:  # noqa: BLE001 -- checked: the last route. Its failure ends the search, and the RegtestSetupError below carries every attempt so the operator sees which three things were tried rather than only the last.
        attempts.append(f"getrawtransaction without blockhash (needs -txindex): {exc}")
    raise RegtestSetupError(
        f"could not read transaction {txid} back from the node. Tried, in order: "
        + "; ".join(attempts)
        + ". The harness does NOT add -txindex=1, because that would reindex a datadir the operator already has."
    )


def step_4_maturity(run: Run) -> int:
    run.step(4, f"mine to coinbase maturity so a coinbase is spendable (>= {COINBASE_MATURITY_HEIGHT} blocks)")
    node = run.node()
    height = int(node.call("getblockcount"))
    run.say(f"current height={height}, maturity needs {COINBASE_MATURITY_HEIGHT}")
    if height < COINBASE_MATURITY_HEIGHT:
        height = _mine(run, COINBASE_MATURITY_HEIGHT - height).height
    run.check("chain height", height, f">= {COINBASE_MATURITY_HEIGHT}",
              OK if height >= COINBASE_MATURITY_HEIGHT else FAIL)
    balance = Decimal(str(node.call("getbalance")))
    needed = Decimal(CONTRACT_AMOUNT) * 2 + Decimal("0.01")
    run.check(
        "spendable wallet balance",
        f"{balance} (two contracts of {CONTRACT_AMOUNT} plus fees need about {needed})",
        f">= {needed}",
        OK if balance >= needed else FAIL,
    )
    if balance < needed:
        raise RegtestSetupError(
            f"{run.asset}: wallet balance {balance} is below the {needed} this run needs. On a fresh regtest chain, "
            "mine more blocks -- or rerun with --wipe if a previous run left the wallet holding only immature "
            "coinbases."
        )
    return height


# --------------------------------------------------------------------------
# step 5: the contract, built by the REAL builder with a REAL derived locktime
# --------------------------------------------------------------------------


def step_5_build_contract(run: Run, height: int) -> Contract:
    run.step(5, "build an HTLC with the real script builder and a real derived locktime")

    # The preimage comes from the real generator, and the ONLY thing printed
    # about it is its SHA-256 hash. modules/utils.generate_secret() used to log
    # the preimage itself at DEBUG through a handler it installed at import;
    # that is fixed, and this harness must not reintroduce it by another route.
    secret = generate_secret()
    secret_hash = sha256_hash(secret)
    run.say(f"preimage generated -- {redact(secret)}")
    run.say(f"secret_hash={secret_hash.hex()} (public by construction; it goes in the script)")

    participant = generate_key()
    refund = generate_key()
    run.say(f"participant address={participant.address} (the counterparty's: redeems with the preimage)")
    run.say(f"refund address={refund.address} (ours: claims back after the locktime)")

    # If the builder's re-encoder changed either address, the hash160 in the
    # script would not be the one our key hashes to, and every spend would fail
    # at OP_EQUALVERIFY for a reason that looks like a signing bug. Assert the
    # round trip rather than assume it.
    for label, key in (("participant", participant), ("refund", refund)):
        reencoded = parse_and_reencode_as_testnet_p2pkh(key.address)
        run.check(
            f"{label} address survives the builder's re-encoding",
            reencoded,
            key.address,
            OK if reencoded == key.address else FAIL,
        )

    locktime = contract_locktime(run.asset, ROLE_INITIATOR, height)
    run.say(describe_locktime(run.asset, ROLE_INITIATOR, height, locktime))

    redeem_script = build_htlc_redeem_script(
        secret_hash=secret_hash.hex(),
        participant_address=participant.address,
        refund_address=refund.address,
        locktime=locktime,
    )
    run.say(f"redeem script ({len(redeem_script)} bytes) = {redeem_script.hex()}")

    p2sh_address = _report_p2sh_address(run, redeem_script)
    return Contract(
        redeem_script=redeem_script,
        p2sh_address=p2sh_address,
        p2sh_script=_p2sh_script_for(redeem_script),
        locktime=locktime,
        secret=secret,
        secret_hash=secret_hash,
        participant=participant,
        refund=refund,
    )


def _report_p2sh_address(run: Run, redeem_script: bytes) -> str:
    """Compare the builder's P2SH address with the node's, and pick one to fund.

    They can differ without the contract differing. modules/atomic_htlc_scripts
    hardcodes Bitcoin testnet's P2SH version byte 0xC4; Litecoin prints P2SH
    addresses with 0x3A while still decoding 0xC4, so on LTC the two strings
    disagree and the scriptPubKey behind them is identical. The harness funds
    the node's rendering when it has one, and asserts on the scriptPubKey hex
    everywhere afterwards, so nothing downstream depends on which was chosen.
    """
    node = run.node()
    builder_address = script_to_p2sh_address(redeem_script)
    decoded = node.call("decodescript", redeem_script.hex())
    node_address = decoded.get("p2sh")
    run.say(f"builder's P2SH address={builder_address}  node's decodescript p2sh={node_address}")
    if node_address and node_address != builder_address:
        run.say(
            "the two DIFFER. That is a base58 version byte difference, not a different contract -- the "
            "scriptPubKey asserted on below is identical either way."
        )
    validity = node.call("validateaddress", builder_address)
    run.check(
        "node accepts the builder's own P2SH address",
        validity.get("isvalid"),
        True,
        OK if validity.get("isvalid") else FAIL,
    )
    return node_address or builder_address


# --------------------------------------------------------------------------
# step 6: fund it, mine, and confirm the P2SH output is on chain
# --------------------------------------------------------------------------


def _find_vout_by_script(raw_tx: dict, script_hex: str) -> int | None:
    """Locate the output paying a given scriptPubKey.

    Matched on the scriptPubKey HEX, not on `scriptPubKey.addresses`. That is
    the version-independent route and it is also a finding: the real
    `modules/utils.wait_for_tx_output()` and `LTCClient.create_contract()` both
    match on `addresses`, a field Bitcoin Core deprecated in 0.20 and removed
    in 22.0. On a daemon that does not return it, the real code cannot find its
    own contract output no matter how correct the funding was. This helper is
    how the harness gets past that to reach steps 7-9, and step 6 prints which
    fields the node actually returned so the operator can see which case they
    are in.
    """
    for index, vout in enumerate(raw_tx.get("vout", [])):
        if vout.get("scriptPubKey", {}).get("hex") == script_hex:
            return index
    return None


def _report_scriptpubkey_fields(run: Run, raw_tx: dict) -> None:
    vouts = raw_tx.get("vout", [])
    if not vouts:
        run.say("the funding transaction has (none: no vout entries) -- that is itself wrong")
        return
    fields = sorted(vouts[0].get("scriptPubKey", {}).keys())
    run.say(f"this daemon's scriptPubKey fields are {fields}")
    if "addresses" not in fields:
        run.say(
            "`addresses` is ABSENT. modules/utils.wait_for_tx_output() and LTCClient.create_contract() both look "
            "for it, so on this daemon they cannot locate a contract output. That is a real defect in the code "
            "under test, reported here rather than worked around: the harness matches on the scriptPubKey hex "
            "instead so steps 7-9 can still run."
        )


def _fund_directly(run: Run, contract: Contract, label: str) -> Outpoint:
    """Fund the contract with a plain sendtoaddress from the harness's wallet."""
    node = run.node()
    run.say(f"[{label}] sending {CONTRACT_AMOUNT} to {contract.p2sh_address}")
    txid = node.call("sendtoaddress", contract.p2sh_address, float(CONTRACT_AMOUNT))
    mined = _mine(run, 1)
    raw_tx = _verbose_tx(node, txid, mined.first_hash)
    _report_scriptpubkey_fields(run, raw_tx)
    vout = _find_vout_by_script(raw_tx, contract.p2sh_script.hex())
    if vout is None:
        raise RegtestSetupError(
            f"{run.asset}: funded {txid} but no output pays the contract's scriptPubKey "
            f"{contract.p2sh_script.hex()}. The node was asked to pay {contract.p2sh_address}; if those two "
            "disagree, the address encoding and the script hash have diverged."
        )
    return Outpoint(txid=txid, vout=vout, value_satoshis=coins_to_satoshis(CONTRACT_AMOUNT))


def _attempt_real_create_contract(run: Run, client, contract: Contract) -> Outpoint | None:
    """Drive the real create_contract(). Returns its outpoint, or None if it failed.

    The two clients take their arguments in DIFFERENT ORDERS -- BTC takes
    (amount, secret_hash, participant, refund, locktime) and LTC takes
    (amount, participant, refund, locktime, secret_hash=None). Everything is
    passed by KEYWORD here for exactly that reason, which is the same defense
    modules/atomic_swapper.py adopted: passing positionally in the BTC order
    hands LTC the secret hash as its participant address.
    """
    amount_kwarg = {"BTC": "amount_btc", "LTC": "amount_ltc"}[run.asset]
    kwargs = {
        amount_kwarg: Decimal(CONTRACT_AMOUNT),
        "secret_hash": contract.secret_hash.hex(),
        "participant_address": contract.participant.address,
        "refund_address": contract.refund.address,
        "locktime": contract.locktime,
    }
    run.say(f"calling the REAL {type(client).__name__}.create_contract({', '.join(sorted(kwargs))})")
    # Announce the scale BEFORE the call, because this one can sit for minutes
    # with nothing of its own to say (rule 14), and an operator who cannot tell
    # working from hung presses Ctrl-C -- which here can land between a
    # broadcast funding transaction and the line that records it.
    run.say(
        f"this call can block for up to {format_duration(REAL_CLIENT_WAIT_SECONDS)} on BTC: after it broadcasts, "
        "BTCClient.create_contract() polls modules/utils.wait_for_tx_output(), which looks for the contract output "
        "under `scriptPubKey.addresses` -- a field Bitcoin Core removed in 22.0. On a daemon that does not return "
        "it, the poll runs to its deadline and then times out. That is the code under test, not a stall."
    )
    try:
        result = client.create_contract(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- checked: this is the measurement. Any failure of the real fund path is the answer this step exists to record; it is reported verbatim with its type, and the harness then funds the same contract itself so steps 7-9 can still run. Nothing downstream reads a value from the failed call.
        run.check("REAL create_contract()", f"{type(exc).__name__}: {exc}", "a funded contract", FAIL)
        run.say(
            "the real fund path did not complete. The harness will fund the SAME contract itself so that steps 7-9 "
            "still measure the redeem and refund branches. This is reported, not papered over."
        )
        return None
    run.check(
        "REAL create_contract()",
        f"txid={result.get('txid')} vout={result.get('vout')} p2sh={result.get('p2shAddress')}",
        "a funded contract",
        OK,
    )
    return Outpoint(
        txid=result["txid"],
        vout=int(result["vout"]),
        value_satoshis=coins_to_satoshis(CONTRACT_AMOUNT),
    )


def _assert_output_on_chain(run: Run, contract: Contract, outpoint: Outpoint, label: str) -> None:
    """gettxout: the P2SH output exists, unspent, with the expected script and value."""
    entry = run.node().call("gettxout", outpoint.txid, outpoint.vout)
    if not entry:
        run.check(f"[{label}] contract output on chain", None,
                  f"an unspent output at {outpoint.txid}:{outpoint.vout}", FAIL)
        return
    got_script = entry.get("scriptPubKey", {}).get("hex")
    run.check(
        f"[{label}] contract scriptPubKey",
        got_script,
        contract.p2sh_script.hex(),
        OK if got_script == contract.p2sh_script.hex() else FAIL,
    )
    got_value = coins_to_satoshis(entry.get("value", 0))
    run.check(
        f"[{label}] contract value in satoshis",
        got_value,
        outpoint.value_satoshis,
        OK if got_value == outpoint.value_satoshis else FAIL,
    )
    run.check(
        f"[{label}] confirmations (a count, never a duration)",
        entry.get("confirmations"),
        ">= 1",
        OK if int(entry.get("confirmations", 0)) >= 1 else FAIL,
    )


def step_6_fund(run: Run, client, contract: Contract, outcome: ChainOutcome) -> tuple[Outpoint, Outpoint]:
    run.step(6, "fund the contract, mine a block, and confirm the P2SH output is on chain")
    run.say("two outputs will be funded -- [A] for the redeem attempts, [B] for the refund attempts")

    real_outpoint = _attempt_real_create_contract(run, client, contract)
    outcome.real_create_contract = OK if real_outpoint else FAIL
    if real_outpoint is not None:
        # create_contract() can return before the funding is mined, so mine one
        # regardless; mining over an already-mined transaction costs a block
        # and nothing else.
        _mine(run, 1)
        contract_a = real_outpoint
        contract.funded_by = "the REAL create_contract()"
    else:
        contract_a = _fund_directly(run, contract, "A")
        contract.funded_by = "the harness (the real create_contract() failed above)"
    run.say(f"contract [A] was funded by {contract.funded_by}")
    _assert_output_on_chain(run, contract, contract_a, "A")

    contract_b = _fund_directly(run, contract, "B")
    run.say(
        "contract [B] is funded by the harness in every run -- no code in this tree can create a contract for the "
        "refund path, because no refund implementation exists to call."
    )
    _assert_output_on_chain(run, contract, contract_b, "B")
    contract.outpoint = contract_a
    return contract_a, contract_b


# --------------------------------------------------------------------------
# step 7: redeem with the preimage -- the real client, then the control
# --------------------------------------------------------------------------


def _broadcast(run: Run, raw_hex: str) -> str:
    """sendrawtransaction with the fee ceiling removed. See NO_FEE_LIMIT."""
    return run.node().call("sendrawtransaction", raw_hex, NO_FEE_LIMIT)


# How the real redeem_contract() failed, which decides what the harness may
# claim from it. These are the only three, and the third is deliberately not a
# synonym for the second: a failure nobody classified must not be reported as
# a confirmation of anything (rule 17).
REDEEM_FAILED_ON_LOOKUP = "lookup"
REDEEM_FAILED_ON_SIGNING = "signing"
REDEEM_FAILED_UNCLASSIFIED = "unclassified"

# Fragments of the daemon's own wording for "I cannot find that transaction
# without an index". Matched on the message because the harness must not
# depend on which of the two clients' exception wrappers the text arrived in.
_LOOKUP_FAILURE_MARKERS = ("no such mempool transaction", "-txindex", "code=-5", "'code': -5")
_SIGNING_FAILURE_MARKERS = ("signing incomplete", "complete': false", "complete\": false")


def classify_redeem_failure(message: str) -> str:
    """Which stage of redeem_contract() failed, from the exception's text.

    WHY THIS EXISTS, AND IT IS THE DIFFERENCE BETWEEN A MEASUREMENT AND A GUESS.

    redeem_contract()'s FIRST line is `getrawtransaction(contract_txid, True)`.
    On a node without -txindex that call cannot see a transaction that has been
    mined out of the mempool -- the same defect the harness itself had until
    2026-09-25. So on a freshly mined contract the real client fails at its own
    lookup and NEVER REACHES the signing step where the unused `secret`
    parameter matters.

    If the harness printed its "this is the known preimage defect" block for
    that failure it would be asserting a conclusion the run did not establish:
    the call failed for an unrelated reason and the preimage question was never
    put to the node. That is precisely presenting a hypothesis in the register
    of a measurement, so the two are separated here and reported differently.
    """
    lowered = message.lower()
    if any(marker in lowered for marker in _LOOKUP_FAILURE_MARKERS):
        return REDEEM_FAILED_ON_LOOKUP
    if any(marker in lowered for marker in _SIGNING_FAILURE_MARKERS):
        return REDEEM_FAILED_ON_SIGNING
    return REDEEM_FAILED_UNCLASSIFIED


def _explain_redeem_failure(run: Run, contract: Contract, message: str) -> str:
    """Say what the failure does and does not establish. Returns the classification."""
    kind = classify_redeem_failure(message)

    if kind == REDEEM_FAILED_ON_LOOKUP:
        run.say("THIS IS NOT THE PREIMAGE DEFECT, AND THE HARNESS WILL NOT CLAIM THAT IT IS.")
        run.say(
            "redeem_contract() failed on its FIRST line -- `getrawtransaction(contract_txid, True)` -- which on a "
            "node without -txindex cannot see a transaction that has already been mined out of the mempool. The "
            "call never reached the signing step, so this run has NOT put the preimage question to the node."
        )
        run.say(
            "that is a SECOND finding about the real client and it is newly measured: redeem_contract() cannot read "
            "back a confirmed contract on a default node. `gettransaction`, or `getrawtransaction` with the "
            "contract's block hash, both work without an index."
        )
        run.say(
            "the preimage defect therefore remains INFERRED from the source, exactly as the module headers say. "
            "The control spend below is the only evidence this run produces about the hashlock branch."
        )
        return kind

    if kind == REDEEM_FAILED_ON_SIGNING:
        run.say("THIS IS THE KNOWN PREIMAGE DEFECT, CONFIRMED AGAINST A REAL CHAIN FOR THE FIRST TIME.")
        run.say(
            "redeem_contract() accepts `secret: bytes` and never references it. It builds the spend with "
            "createrawtransaction and hands it to a signrawtransaction* call, which constructs a scriptSig by "
            "recognizing a script PATTERN. An HTLC is OP_IF/OP_ELSE/OP_ENDIF, which matches no pattern, so the "
            "signer has nothing to build and no argument that would let it push a preimage and a TRUE flag."
        )
    else:
        run.say("THIS FAILURE IS NOT ONE THE HARNESS RECOGNIZES, so it claims nothing about which defect caused it.")
        run.say(
            "it is neither the -txindex lookup failure nor an incomplete signing result. Read the message above "
            "before concluding anything; the control spend below still says what the SCRIPT can do."
        )

    run.say("the redeem script's hashlock branch expects, bottom to top:")
    run.say("    <signature> <pubkey> <preimage> OP_1   then the redeem script itself")
    run.say(f"the preimage is {redact(contract.secret)}; its sha256 is {contract.secret_hash.hex()}")
    run.say(f"the redeem script it must satisfy is {contract.redeem_script.hex()}")
    return kind


def step_7_redeem(run: Run, client, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    run.step(7, "redeem with the preimage -- the REAL redeem_contract(), then the harness control")
    destination = contract.participant.address
    run.say(f"redeeming [A] {outpoint.txid}:{outpoint.vout} to the participant address {destination}")

    # The real BTC client reads its signing key out of the environment. The key
    # handed to it was generated in this process seconds ago, controls nothing
    # but regtest coins this harness mined, and is never printed.
    os.environ["BTC_HTLC_PRIVKEY"] = contract.participant.wif
    try:
        txid = client.redeem_contract(
            outpoint.txid,
            outpoint.vout,
            contract.redeem_script,
            contract.secret,
            contract.participant.wif,
            destination,
        )
    except Exception as exc:  # noqa: BLE001 -- checked: the failure IS the measurement. It is recorded as XFAIL (a predicted failure, not an unexplained one), printed with its type and message, and followed by the explanation block. The run continues to the control spend, which is what distinguishes "the client is broken" from "the script is broken".
        outcome.real_redeem = run.check(
            "REAL redeem_contract() spends the hashlock branch",
            f"{type(exc).__name__}: {exc}",
            "a broadcast txid -- but this harness PREDICTS this failure",
            XFAIL,
        )
        outcome.real_redeem_stage = _explain_redeem_failure(run, contract, str(exc))
        outcome.notes.append(f"real redeem_contract() failed at the {outcome.real_redeem_stage} stage")
        _control_redeem(run, contract, outpoint, outcome)
        return

    outcome.real_redeem = run.check(
        "REAL redeem_contract() spends the hashlock branch",
        f"txid={txid}",
        "a broadcast txid",
        OK,
    )
    mined = _mine(run, 1)
    _assert_spend_landed(run, txid, contract.participant, "real redeem", mined.first_hash)
    outcome.control_redeem = run.check(
        "control redeem",
        "not attempted: the real client already spent the output",
        "n/a",
        SKIP,
    )


def _control_redeem(run: Run, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    """The harness's own hashlock spend, on the same output the real client failed on.

    This is the instrument, not the code under test, and the label on screen
    says so. Its only purpose is to answer whether the SCRIPT can be spent by
    its hashlock branch at all -- which decides whether the defect is in
    redeem_contract() or in build_htlc_redeem_script(), two bugs with entirely
    different fixes.
    """
    run.say("control -- the harness now builds the hashlock scriptSig itself, on the SAME output")
    raw_hex, script_sig = build_branch_spend(
        outpoint=outpoint,
        redeem_script=contract.redeem_script,
        key=contract.participant,
        destination_script=contract.participant.p2pkh_script,
        fee_satoshis=coins_to_satoshis(CONTROL_FEE),
        locktime=0,
        secret=contract.secret,
    )
    run.say(f"control scriptSig = {describe_script_sig(script_sig)}")
    try:
        txid = _broadcast(run, raw_hex)
    except RPCError as exc:
        outcome.control_redeem = run.check("control hashlock spend", f"RPCError: {exc}", "a broadcast txid", FAIL)
        run.say(
            "the control could not spend the hashlock branch either. That points at the redeem SCRIPT rather than "
            "at redeem_contract(), and is a different bug with a different fix."
        )
        return
    outcome.control_redeem = run.check("control hashlock spend", f"txid={txid}", "a broadcast txid", OK)
    mined = _mine(run, 1)
    _assert_spend_landed(run, txid, contract.participant, "control redeem", mined.first_hash)


def _assert_spend_landed(run: Run, txid: str, key: RegtestKey, label: str, block_hash: str | None = None) -> None:
    """The spend confirmed, and its output pays the expected key.

    Asserted on the scriptPubKey hex rather than on a rendered address, for the
    same encoding reason as everywhere else in this file.

    `block_hash` is the block the caller just mined. It is passed rather than
    looked up because these spends are NOT wallet transactions -- they spend a
    P2SH the wallet never imported and pay keys this harness generated -- so
    `gettransaction` cannot see them and a bare `getrawtransaction` searches
    only the mempool they have just left. See _verbose_tx().
    """
    raw_tx = _verbose_tx(run.node(), txid, block_hash)
    confirmations = int(raw_tx.get("confirmations", 0))
    run.check(
        f"{label} confirmations (a count, never a duration)",
        confirmations,
        ">= 1",
        OK if confirmations >= 1 else FAIL,
    )
    vout = _find_vout_by_script(raw_tx, key.p2pkh_script.hex())
    run.check(
        f"{label} pays {key.address}",
        f"vout={vout}" if vout is not None else None,
        f"an output paying scriptPubKey {key.p2pkh_script.hex()}",
        OK if vout is not None else FAIL,
    )


# --------------------------------------------------------------------------
# steps 8 and 9: the refund branch, before and after expiry
# --------------------------------------------------------------------------


def _build_refund(contract: Contract, outpoint: Outpoint, nlocktime: int) -> tuple[str, bytes]:
    return build_branch_spend(
        outpoint=outpoint,
        redeem_script=contract.redeem_script,
        key=contract.refund,
        destination_script=contract.refund.p2pkh_script,
        fee_satoshis=coins_to_satoshis(CONTROL_FEE),
        locktime=nlocktime,
        secret=None,
    )


def _attempt_refund_expecting_refusal(run: Run, contract: Contract, outpoint: Outpoint, nlocktime: int, label: str) -> str:
    raw_hex, script_sig = _build_refund(contract, outpoint, nlocktime)
    run.say(f"{label} -- nLockTime={nlocktime}, script locktime={contract.locktime} (both heights)")
    run.say(f"{label} scriptSig = {describe_script_sig(script_sig)}")
    try:
        txid = _broadcast(run, raw_hex)
    except RPCError as exc:
        return run.check(f"{label} is REFUSED", f"RPCError: {exc}", "the node to refuse it", OK)
    return run.check(f"{label} is REFUSED", f"the node ACCEPTED it: txid={txid}", "the node to refuse it", FAIL)


def step_8_refund_before_expiry(run: Run, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    run.step(8, "refund BEFORE expiry must be REJECTED -- twice, for two different reasons")
    height = int(run.node().call("getblockcount"))
    target = contract.locktime - 1
    run.say(
        f"height={height}, mining to {target} = one block short of the script's locktime {contract.locktime} "
        "(heights, never durations)"
    )
    if height < target:
        height = _mine(run, target - height).height
    run.check("height one block short of the locktime", height, target, OK if height == target else FAIL)

    # 8a: final-ness. The mempool refuses before any script runs.
    first = _attempt_refund_expecting_refusal(
        run, contract, outpoint, contract.locktime,
        "8a refund with nLockTime = the script's locktime (mempool non-final check; proves nothing about CLTV)",
    )
    # 8b: the one that proves CHECKLOCKTIMEVERIFY is enforcing something. The
    # transaction is final for this block, so the script actually executes, and
    # CLTV compares the script's larger locktime against this smaller nLockTime.
    second = _attempt_refund_expecting_refusal(
        run, contract, outpoint, height,
        "8b refund with nLockTime = the current tip (final, so the script RUNS; this is the CLTV assertion)",
    )
    outcome.refund_before_expiry_rejected = OK if first == OK and second == OK else FAIL
    if outcome.refund_before_expiry_rejected == OK:
        run.say(
            "both refusals arrived. 8b is the one that matters: the script executed and CHECKLOCKTIMEVERIFY refused "
            "it, which is the first time this repository's refund branch has been shown to enforce anything."
        )


def step_9_refund_after_expiry(run: Run, contract: Contract, outpoint: Outpoint, outcome: ChainOutcome) -> None:
    run.step(9, "refund AFTER expiry must SUCCEED")
    height = _mine(run, 1).height
    run.check(
        "height now reaches the locktime",
        height,
        contract.locktime,
        OK if height >= contract.locktime else FAIL,
    )
    raw_hex, script_sig = _build_refund(contract, outpoint, contract.locktime)
    run.say(f"refund scriptSig = {describe_script_sig(script_sig)}")
    try:
        txid = _broadcast(run, raw_hex)
    except RPCError as exc:
        outcome.refund_after_expiry = run.check("refund after expiry", f"RPCError: {exc}", "a broadcast txid", FAIL)
        return
    outcome.refund_after_expiry = run.check("refund after expiry", f"txid={txid}", "a broadcast txid", OK)
    mined = _mine(run, 1)
    _assert_spend_landed(run, txid, contract.refund, "refund", mined.first_hash)
