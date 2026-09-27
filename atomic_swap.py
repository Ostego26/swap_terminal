#!/usr/bin/env python3
"""Drive BOTH legs of an atomic swap between ANY two HTLC-capable chains, either direction.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: two daemons, through modules/atomic_*_client.py, and the *_RPC_* environment
       variables that address them. Nothing else -- no database, no .env loading.
Writes: THE TWO CHAINS. Every leg it funds is an irreversible transfer.
Can move funds: YES, on both chains, and that is the whole point. It refuses without
       --run, refuses unless BOTH daemons say they are on a test network, and refuses
       a timelock ordering that would let either side steal.
Mainnet-safe: NO, and it refuses rather than warning. Step 1 asks each daemon which
       chain it is on and stops unless both are test networks. It never infers the
       network from a port number.
Live-safe: yes in the sense that matters -- it opens no swap database, reads no live
       configuration, and shares no wallet with the brokered service.

ANY PAIR, EITHER DIRECTION -- AND WHICH PAIRS THAT ACTUALLY IS

Operator, 2026-09-27: "we should be able to inter swap with any currency listed... either
direction... btc/ltc or ltc/btc or ltc/xmr or grc/xmr etc. whatever. xmr/btc", alongside
"basically we are creating swap opportunities for grc and the other shit is just a side
hustle."

Both instructions are honored by the same shape: this file is SYMMETRIC over its asset
set, with no hub. GRC being the business focus is a fact about which pairs get used, not
a constraint on which the machinery can express, and hard-coding a hub would have meant
rewriting it the first time somebody wanted BTC/LTC. The asset is a parameter here, which
it can be because all three clients now expose the same three methods -- create, redeem
and refund -- as of 2026-09-27.

    ASSET_PAIRS below is generated from ASSETS rather than listed, so six directed pairs
    exist because three assets do, and a fourth asset would make twelve without anybody
    editing a list. That is rule 11's shape: one vocabulary, derived in one place.

WHAT IS REACHABLE TODAY, and "any currency listed" IS NOT YET TRUE:

    pair family      status
    BTC<->LTC        THIS FILE, both directions.
    BTC<->GRC        THIS FILE, both directions.
    LTC<->GRC        THIS FILE, both directions.
    XRP<->GRC        A DIFFERENT DRIVER, and it works: atomic_swap_xrp_grc.py, both
                     directions, proven live on testnet. The XRP Ledger has no script --
                     it uses EscrowCreate with a PREIMAGE-SHA-256 crypto-condition rather
                     than a P2SH HTLC -- so its legs are a different protocol and cannot
                     be a row in this file's table. XRP<->BTC and XRP<->LTC would each be
                     that same escrow leg against a P2SH leg, which is real work and not
                     yet done.
    ANY<->XMR        NOT POSSIBLE YET, and not for want of effort. Monero has NO SCRIPT AT
                     ALL, so there is nowhere to put a hashlock -- ltc/xmr, grc/xmr and
                     xmr/btc are all blocked on the same single missing thing, not on
                     three different ones. Their swap needs adaptor signatures plus a
                     cross-curve discrete-log-equality proof. All four components are now
                     individually tested (modules/adaptor_ecdsa.py,
                     modules/ed25519_group.py, modules/dleq_helper.py, and
                     chains/monero_keys.py, whose shared 2-of-2 key was swept live on
                     stagenet on 2026-09-27) and NOTHING COMPOSES THEM. See
                     docs/dleq_cross_curve_design.md section 6 stage 5.
    ANY<->SOL        no HTLC. The one that existed was a stub that could not run, deleted
                     in c4ea027 for four independent reasons.

So: six directed pairs here, two more in the XRP driver, and the XMR family blocked behind
one named piece of protocol work rather than behind this file.

THE PROTOCOL, AND THE ONE PROPERTY THAT MAKES IT ATOMIC

    1. one 32-byte secret, and its SHA-256 committed on both chains
    2. the INITIATOR funds their leg with the LONGER timelock
    3. the PARTICIPANT funds theirs with the same hash, expiring STRICTLY FIRST
    4. the initiator claims the participant's leg with the secret -- which PUBLISHES it
    5. the participant reads the secret OFF THE CHAIN, out of that claim's scriptSig,
       and claims the initiator's leg with it

Step 3's ordering is the security property and it is not a convention. If the
participant's lock outlived the initiator's, the initiator could sit on the secret until
their own refund became spendable, take their coins back, and THEN claim the
participant's leg with the secret -- taking both. modules/htlc_timelock.py owns that rule
and assert_ordering() below enforces it with a positive margin, refusing rather than
warning.

Step 5 is the other half, and it is why claim_scriptsig_hex() exists: the participant
must read the preimage from the CHAIN, never be handed it. A protocol where the secret
arrives by message is a protocol where the counterparty can simply not send it.

WHAT THIS FILE DOES NOT DO

It plays BOTH sides. Both legs are funded from wallets this operator controls, which
makes it a rehearsal of the protocol rather than a swap with a counterparty -- and the
distinction matters for one specific reason: step 5 succeeds here partly because the
claim is in this wallet, so `gettransaction` can find it. A real participant is not the
claimer and needs -txindex or a block scan. claim_scriptsig_hex()'s docstring says so at
the site.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from modules.atomic_btc_client import BTCClient
from modules.atomic_grc_client import GRCClient
from modules.atomic_htlc_scripts import p2sh_script_for
from modules.atomic_ltc_client import LTCClient
from modules.htlc_chain_read import (
    adapter_for,
    claim_scriptsig_hex,
    client_caller,
    htlc_vout,
)
from modules.htlc_spend import preimage_from_scriptsig
from modules.htlc_timelock import ROLE_INITIATOR, ROLE_PARTICIPANT, contract_locktime
from regtest.keys import generate_key
from step_console import Console

# The assets whose legs this file can build: the three with a P2SH HTLC and a client
# exposing create, redeem AND refund. XRP and XMR are absent for PROTOCOL reasons rather
# than missing work -- see the module docstring -- and adding either here would be a claim
# no code can honor.
CLIENTS = {"BTC": BTCClient, "GRC": GRCClient, "LTC": LTCClient}
ASSETS = tuple(sorted(CLIENTS))

# DERIVED, not listed (rule 11). Six directed pairs because three assets, and a fourth
# asset makes twelve without anybody editing a list. A hand-written pair table is rule 8's
# shape: it agrees with CLIENTS on the day it is written and drifts the first time one of
# them changes.
ASSET_PAIRS = tuple(
    (source, destination)
    for source in ASSETS
    for destination in ASSETS
    if source != destination
)

# create_contract's amount keyword differs per client: amount_btc, amount_ltc, amount_grc.
# That divergence is real and predates this file; it is mapped in ONE place here rather
# than branched at each call, so adding a spoke is a row and not an if.
AMOUNT_KEYWORD = {"BTC": "amount_btc", "GRC": "amount_grc", "LTC": "amount_ltc"}

# 32 bytes, from secrets. The preimage IS the swap: whoever learns it can claim, so it is
# never logged, never passed on a command line, and never written to disk by this file.
SECRET_BYTES = 32

# Default RPC endpoints, TESTNET ports for all three. Deliberately not mainnet defaults:
# config.py records an incident where unset ports fell through to mainnet 8332/9332/15715
# and polled the operator's live staking wallet on a loop. GRC testnet is 25715, measured
# on the operator's host 2026-09-27 (pid 8897 listening), NOT 25779 which an older comment
# in config.py guesses at.
DEFAULT_RPC = {
    "BTC": ("http://127.0.0.1:18332", "bitcoinrpc", ""),
    "LTC": ("http://127.0.0.1:19332", "litecoinrpc", ""),
    "GRC": ("http://127.0.0.1:25715", "gridcoinrpc", ""),
}

TEST_CHAIN_NAMES = {"test", "testnet", "testnet3", "testnet4", "regtest", "signet"}


class SwapError(RuntimeError):
    """A refusal, or a chain that did not do what it was asked."""


@dataclass(frozen=True)
class Step:
    """The console and which step number this is.

    They travel together through every step function and ruff's PLR0913 counted them as
    two of the six. Grouping them is rule 12's "extract, do not raise the ceiling" -- and
    it is not only a count fix: `step(n)` and `say()` on one object means a step function
    cannot announce itself as one number and report under another, which a loose int
    parameter allows.
    """

    console: Console
    number: int

    def announce(self, title: str) -> None:
        self.console.step(self.number, title)

    def say(self, text: str) -> None:
        self.console.say(text)

    def check(self, label: str, got: object, expected: object, ok: bool) -> bool:
        return self.console.check(label, got, expected, ok)


@dataclass(frozen=True)
class Party:
    """The key that spends a leg and where the coins go.

    Two fields together because they always travel together and because ruff's PLR0913 on
    claim_leg() was right that seven arguments wanted grouping. Rule 12: extract, do not
    raise the ceiling. Keeping the WIF beside its destination also makes the pairing
    visible -- a claim signed by one party's key paying another party's address is the
    mistake this shape makes hard to write.
    """

    privkey: str
    destination: str


@dataclass(frozen=True)
class Leg:
    """One side of the swap: which asset, which role, and the addresses involved.

    `participant_address` is the COUNTERPARTY's address on THIS chain -- whoever holds its
    key claims with the preimage -- and `refund_address` is the funder's own. They were one
    value in atomic_swapper.py until 2026-09-24 and that made every contract unclaimable;
    build_htlc_redeem_script() now refuses a script whose two branches hash to the same
    key, but they are separate fields here so the confusion cannot be expressed.
    """

    asset: str
    role: str
    amount: Decimal
    participant_address: str
    refund_address: str


@dataclass(frozen=True)
class FundedLeg:
    """A leg that exists on a chain: what it is, what funding it produced, and the client
    that can spend it.

    The third grouping ruff's PLR0913 asked for, and the one that most wanted to exist.
    `leg`, the `funded` dict and the `client` were three separate arguments to claim_leg,
    read_secret_off_chain and refund_leg, and they are meaningless apart -- a funded dict
    belongs to exactly one leg on exactly one chain, reachable by exactly one client.
    Passing them separately is what allows the mistake of spending leg A's outpoint with
    leg B's client, which on two chains with similar RPC shapes fails obscurely.
    """

    leg: Leg
    funded: dict
    client: object

    @property
    def asset(self) -> str:
        return self.leg.asset

    def call(self):
        """The `call(method, *args)` callable modules/htlc_chain_read expects."""
        return client_caller(self.client)

    def redeem_script(self) -> bytes:
        return bytes.fromhex(self.funded["redeem_script"])

    def script_pubkey_hex(self) -> str:
        """The P2SH scriptPubKey the funding transaction must pay, derived from the redeem
        script the DAEMON returned -- so a mismatch means the funding does not pay the
        contract the daemon described, which is a refusal rather than an index to guess."""
        return p2sh_script_for(self.redeem_script()).hex()

    def find_vout(self, step: Step) -> int:
        """Which output holds the contract, matched on chain. Never defaulted to 0.

        The defect this repository has fixed three times -- BTC 2026-09-25, GRC 2026-09-26,
        and it is why modules/htlc_chain_read.htlc_vout() exists. Gridcoin's createhtlc
        returns no vout and funds via SendMoney(), which adds change, so the contract sits
        at index 0 or 1 by coin selection.
        """
        vout, why = htlc_vout(adapter_for(self.call()), self.funded["txid"], self.script_pubkey_hex())
        step.check(f"{self.asset} contract vout", vout, "found by scriptPubKey match",
                   vout is not None)
        if vout is None:
            raise SwapError(f"{self.asset}: could not find the contract output. {why}")
        step.say(why)
        return vout


def client_for(asset: str):
    """Construct the chain client from the environment, or refuse naming the variables."""
    if asset not in CLIENTS:
        raise SwapError(
            f"no client for {asset!r}; this file drives {', '.join(ASSETS)}. XRP is a different "
            f"protocol (see atomic_swap_xrp_grc.py) and XMR has no script at all -- neither is a "
            f"missing row here"
        )
    url_default, user_default, _ = DEFAULT_RPC[asset]
    url = os.environ.get(f"{asset}_RPC_URL", url_default)
    user = os.environ.get(f"{asset}_RPC_USER", user_default)
    password = os.environ.get(f"{asset}_RPC_PASS", "")
    if not password:
        raise SwapError(
            f"{asset}_RPC_PASS is not set. This file will not guess a credential. Set "
            f"{asset}_RPC_URL (default {url_default}), {asset}_RPC_USER and {asset}_RPC_PASS "
            f"for the TESTNET daemon"
        )
    return CLIENTS[asset](url, user, password)


def chain_name(asset: str, client) -> str:
    """Which network this daemon is on, ASKED rather than inferred from its port.

    Three routes because the three daemons differ: Bitcoin and Litecoin answer
    `getblockchaininfo` with a `chain`; Gridcoin is an older fork whose `getinfo` carries a
    `testnet` boolean instead. Inferring from the port is what this refuses to do -- a port
    is a convention and a convention is not a check, and config.py records what happened
    the last time one was trusted.
    """
    call = client_caller(client)
    try:
        info = call("getblockchaininfo") or {}
        if info.get("chain"):
            return str(info["chain"])
    except Exception as error:  # noqa: BLE001 -- checked: Gridcoin has no getblockchaininfo and answers with a method-not-found, which is the signal to try getinfo rather than a failure. The reason is folded into the refusal below if every route fails.
        first = f"getblockchaininfo: {type(error).__name__}"
    else:
        first = "getblockchaininfo: answered with no `chain`"
    try:
        info = call("getinfo") or {}
    except Exception as error:
        raise SwapError(
            f"{asset}: could not determine the network. {first}; getinfo: {type(error).__name__}. "
            f"Refusing rather than assuming: this file will not fund a contract on a chain it "
            f"cannot name"
        ) from error
    if "testnet" in info:
        return "testnet" if info["testnet"] else "main"
    raise SwapError(
        f"{asset}: could not determine the network. {first}; getinfo answered with no `testnet`"
    )


def assert_ordering(step: Step, initiator_lock: int, initiator_tip: int,
                    participant_lock: int, participant_tip: int) -> None:
    """The PARTICIPANT's leg must expire STRICTLY FIRST, with a positive margin.

    THIS IS THE SECURITY PROPERTY AND IT IS NOT A CONVENTION. If the participant's lock
    outlived the initiator's, the initiator could sit on the secret until their OWN refund
    became spendable, take their coins back, and then claim the participant's leg with the
    secret they never used -- taking both legs. The participant has no counter to that
    because the secret is theirs to learn, not to produce.

    Measured in BLOCKS REMAINING rather than raw heights, which is the only comparable
    unit: the two chains have unrelated tips and unrelated block intervals, so
    `initiator_lock > participant_lock` compares two numbers that mean nothing to each
    other. Litecoin at 2.5 minutes a block and Bitcoin at 10 means the same block count is
    four times the wall-clock, which is exactly why modules/htlc_timelock.py derives each
    lock from its own chain's tip and role rather than from one constant.

    Refuses rather than warns. A warning on this is a warning nobody reads until a swap
    has been taken.
    """
    initiator_blocks = initiator_lock - initiator_tip
    participant_blocks = participant_lock - participant_tip
    margin = initiator_blocks - participant_blocks
    step.say(f"initiator leg expires in {initiator_blocks} blocks (height {initiator_lock})")
    step.say(f"participant leg expires in {participant_blocks} blocks (height {participant_lock})")
    step.check("participant expires FIRST", f"margin {margin} blocks", "a positive margin",
               margin > 0)
    if margin <= 0:
        raise SwapError(
            f"REFUSING: the participant's leg expires {-margin} blocks LATER than the "
            f"initiator's, not earlier. That lets the initiator wait out their own lock, "
            f"refund their leg, and THEN claim the participant's with the secret -- taking "
            f"both. Nothing was funded"
        )
    if participant_blocks <= 0:
        raise SwapError(
            f"REFUSING: the participant's leg is already expired at funding time "
            f"({participant_blocks} blocks). Its refund branch would be spendable the moment "
            f"it is funded, which is the absence of a timelock rather than a short one"
        )


def fund_leg(step: Step, leg: Leg, secret_hash: str, client) -> dict:
    """Create and fund one leg, then find its vout ON CHAIN rather than assuming 0.

    The vout matters and is the defect this repository has now fixed three times -- on BTC
    on 2026-09-25, on GRC on 2026-09-26, and it is why modules/htlc_chain_read.htlc_vout()
    exists. Gridcoin's createhtlc returns no vout at all and funds through SendMoney(),
    which adds a CHANGE output, so the contract sits at index 0 or 1 depending on coin
    selection. A claim aimed at the wrong index spends nothing and burns a fee.
    """
    tip = int(client_caller(client)("getblockcount"))
    locktime = contract_locktime(leg.asset, leg.role, tip)
    step.announce(f"fund the {leg.role} leg: {leg.amount} {leg.asset}, locktime {locktime}")
    step.say(f"{leg.asset} tip {tip}; role {leg.role}; hash {secret_hash}")

    contract = client.create_contract(**{
        AMOUNT_KEYWORD[leg.asset]: leg.amount,
        "participant_address": leg.participant_address,
        "refund_address": leg.refund_address,
        "locktime": locktime,
        "secret_hash": secret_hash,
    })
    txid = str(contract.get("txid") or contract.get("transaction_id") or "")
    p2sh = str(contract.get("p2sh_address") or contract.get("address") or "")
    script_hex = str(contract.get("redeem_script") or contract.get("redeemScript") or "")
    step.check(f"{leg.asset} funding txid", txid[:16] + "..." if txid else None,
               "a txid", bool(txid))
    step.say(f"{leg.asset} contract at {p2sh}")
    if not txid or not script_hex:
        raise SwapError(
            f"{leg.asset}: create_contract returned no txid or no redeem script. Keys present: "
            f"{sorted(contract)}"
        )
    return {"txid": txid, "p2sh_address": p2sh, "redeem_script": script_hex,
            "locktime": locktime, "tip": tip}


def claim_leg(step: Step, funded_leg: FundedLeg, secret: bytes, party: Party) -> str:
    """Claim a leg with the preimage. This PUBLISHES the secret, which is the point.

    Step 4 of the protocol. The initiator does this to the participant's leg, and doing so
    puts the 32 bytes into a scriptSig on a public chain -- which is how the participant
    learns them without anybody sending a message. The secret is never logged here.
    """
    step.announce(f"claim the {funded_leg.leg.role} leg with the secret -- this PUBLISHES it")
    vout = funded_leg.find_vout(step)
    txid = funded_leg.client.redeem_contract(
        funded_leg.funded["txid"], vout, funded_leg.redeem_script(),
        secret.hex(), party.privkey, party.destination,
    )
    step.check(f"{funded_leg.asset} claim txid", str(txid)[:16] + "...", "a txid", bool(txid))
    return str(txid)


def read_secret_off_chain(step: Step, funded_leg: FundedLeg, claim_txid: str, secret_hash: str,
                          *, attempts: int = 20) -> bytes:
    """Read the preimage OUT OF THE CLAIM'S scriptSig. Never accept it in a message.

    Step 5, and the reason the protocol is trustless. A design where the counterparty sends
    the secret is a design where they can simply not send it; here the act of claiming is
    the act of publishing, so the initiator cannot take their leg without giving up the
    secret that unlocks the other.

    Polls, because the claim has to be visible before it can be read, and prints a line per
    attempt -- rule 14: a silent wait here is indistinguishable from a hang, and this is the
    step where abandoning is worst, since both legs are funded and the secret is already
    public.
    """
    step.announce(f"read the secret OFF the {funded_leg.asset} chain -- never from the counterparty")
    for attempt in range(1, attempts + 1):
        script_sig, reasons = claim_scriptsig_hex(adapter_for(funded_leg.call()), claim_txid)
        if script_sig:
            found = preimage_from_scriptsig(bytes.fromhex(script_sig), bytes.fromhex(secret_hash))
            if found is not None:
                step.check("preimage recovered from the chain", f"{len(found)} bytes",
                           "32 bytes matching the committed hash", len(found) == SECRET_BYTES)
                return found
            raise SwapError(
                f"the claim transaction {claim_txid} was read but its scriptSig carries NO push "
                f"whose SHA-256 is {secret_hash}. That is a claim of a different contract, or a "
                f"refund rather than a claim -- either way the secret is not in it"
            )
        step.say(f"attempt {attempt}/{attempts}: not readable yet -- {'; '.join(reasons) or 'no reason given'}")
        time.sleep(3.0)
    raise SwapError(
        f"could not read the claim {claim_txid} after {attempts} attempts. BOTH LEGS ARE FUNDED "
        f"and the secret is already public on that chain, so this is a failure to READ and not a "
        f"failure of the swap -- a daemon with -txindex, or a block scan, will find it. Do NOT "
        f"refund until you have checked"
    )


def refund_leg(step: Step, funded_leg: FundedLeg, party: Party) -> str:
    """Spend a leg's TIMELOCK branch, returning the funder's own coins.

    The path that exists so a stalled swap is recoverable rather than lost, and it is only
    complete as of 2026-09-27: GRCClient had no refund until then, so a Gridcoin leg could
    be funded and never recovered. All three clients have one now, which is what makes this
    function writable at all.

    No platform fee is charged on a refund, by any of the three clients. A refund returns
    the funder's own coins after a counterparty failed to show -- the swap did not happen,
    so there is no service to charge for, and charging would take a cut of a recovery.
    """
    step.announce(f"REFUND the {funded_leg.asset} leg -- its timelock has passed")
    vout = funded_leg.find_vout(step)
    txid = funded_leg.client.refund_contract(
        contract_txid=funded_leg.funded["txid"],
        contract_vout=vout,
        redeem_script=funded_leg.redeem_script(),
        locktime=funded_leg.funded["locktime"],
        refund_privkey=party.privkey,
        refund_address=party.destination,
    )
    step.check(f"{funded_leg.asset} refund txid", str(txid)[:16] + "...", "a txid", bool(txid))
    return str(txid)


def mint_parties(step: Step, assets: tuple[str, str]) -> dict[tuple[str, str], object]:
    """Four throwaway keypairs -- one per (chain, role) -- generated IN THIS PROCESS.

    NOT `dumpprivkey`, AND THAT IS NOT A STYLE CHOICE. The obvious route is getnewaddress
    followed by dumpprivkey, and swap_terminal/regtest/keys.py's header explains why this
    repository forbids it: CLAUDE.md's chain-safety rules say never move, copy or read back
    a key, and a tool that teaches the operator to type dumpprivkey is teaching the reflex
    that leaked a live GRIDCOIN_RPC_PASSWORD into this repo's own history. It also happens
    not to work: dumpprivkey is a legacy-wallet RPC and Bitcoin Core 28.1 creates
    descriptor wallets by default, which refuse it.

    So the keys are generated here, exist nowhere else, control nothing but the contract
    branches built around them seconds later, and are never printed, logged or written.
    regtest/keys.generate_key() already does exactly this and is reused rather than
    re-spelled (rule 8) -- its testnet P2PKH version byte 0x6F is correct for BTC, LTC and
    GRC testnet alike, which is what makes it chain-generic despite the `regtest` in its
    module name. THAT NAME IS THE THING THAT IS WRONG, not the reuse: the module generates
    testnet keys and says so in its own docstring, and renaming it is a separate change
    that would touch the harness.

    FOUR KEYS AND NOT TWO, because each leg's two branches must hash to DIFFERENT keys.
    build_htlc_redeem_script() refuses a script whose branches resolve to the same hash160
    -- the guard added after atomic_swapper.py passed one address as both for six
    directions, making every contract unclaimable. Four distinct keys means that state
    cannot be constructed here at all.

        (chain A, initiator)    funds leg A; its address is leg A's REFUND branch
        (chain A, participant)  claims leg A with the preimage
        (chain B, initiator)    claims leg B with the preimage
        (chain B, participant)  funds leg B; its address is leg B's REFUND branch
    """
    step.announce("mint four throwaway keypairs, in this process, never written to disk")
    parties: dict[tuple[str, str], object] = {}
    for asset in assets:
        for role in (ROLE_INITIATOR, ROLE_PARTICIPANT):
            key = generate_key()
            parties[(asset, role)] = key
            # The ADDRESS is safe to print; the WIF is not, and nothing here prints it.
            step.say(f"{asset} {role}: {key.address}")
    addresses = {key.address for key in parties.values()}
    step.check("four distinct addresses", len(addresses), "4 -- no branch may share a key",
               len(addresses) == len(parties))
    if len(addresses) != len(parties):
        raise SwapError(
            "two generated keys collided, which is a 2^-160 event and therefore a bug in "
            "generate_key() rather than luck. Nothing was funded"
        )
    return parties


def wallet_destination(step: Step, asset: str, client, label: str) -> str:
    """A WALLET address for a claim or refund to pay OUT to, from getnewaddress.

    THE SUBTLETY THIS EXISTS FOR, and getting it wrong makes a rehearsal end with
    unspendable coins.

    The contract's two BRANCHES must name the in-process keys, because those keys are what
    sign the claim and the refund. But the claim's and refund's DESTINATION is a separate
    argument, and paying it to an in-process address would send the swapped coins to a key
    that is discarded when this process exits -- a successful swap whose proceeds nobody
    can spend, which reads as success and is a loss.

    So: branches use the minted keys, destinations use the wallet. The coins come home.
    """
    address = str(client_caller(client)("getnewaddress", label))
    step.say(f"{asset} payout destination (wallet): {address}")
    return address


def build_legs(from_asset: str, to_asset: str, from_amount, to_amount,
               parties: dict) -> tuple[Leg, Leg]:
    """The two legs, and THE ROLE INVERSION THAT IS EASY TO GET BACKWARDS.

    Extracted out of main() because ruff's C901 said main was too complex, and rule 12 is
    explicit about what that means: "a main() past the ceiling is orchestration that has
    swallowed decisions ... the fix is to extract the decision so it can be called with
    seeded inputs, not to raise the ceiling." This IS the decision, and it is one a reader
    can get wrong in a way no chain would report.

    Leg A is the INITIATOR's. They fund the --from asset with the LONGER lock. Its HASHLOCK
    branch names the PARTICIPANT's key -- the counterparty is who claims it with the
    preimage -- and its REFUND branch names the initiator's own.

    Leg B is the PARTICIPANT's, the --to asset, expiring FIRST. THE ROLES INVERT: its
    hashlock branch names the INITIATOR's key, because the initiator is the one who claims
    leg B with the secret and thereby publishes it, and its refund branch names the
    participant's.

    Getting that inversion backwards builds two contracts each claimable only by the party
    who funded it -- which is not a broken swap that errors, it is two self-payments that
    both "succeed". build_htlc_redeem_script() catches the degenerate case where both
    branches name the SAME key (the 2026-09-24 defect) but it cannot catch a consistent
    swap of the two, because that script is perfectly well formed. Only this function's
    correctness does, which is why it is a function with seeded inputs and a test rather
    than four lines inside main().
    """
    legs = (
        Leg(asset=from_asset, role=ROLE_INITIATOR, amount=from_amount,
            participant_address=parties[(from_asset, ROLE_PARTICIPANT)].address,
            refund_address=parties[(from_asset, ROLE_INITIATOR)].address),
        Leg(asset=to_asset, role=ROLE_PARTICIPANT, amount=to_amount,
            participant_address=parties[(to_asset, ROLE_INITIATOR)].address,
            refund_address=parties[(to_asset, ROLE_PARTICIPANT)].address),
    )
    for leg, flag in zip(legs, ("--from-amount", "--to-amount"), strict=True):
        if leg.amount is None or leg.amount <= 0:
            raise SwapError(
                f"{flag} must be a positive number; got {leg.amount!r}. Nothing was funded"
            )
        if leg.participant_address == leg.refund_address:
            raise SwapError(
                f"the {leg.asset} leg's two branches resolve to the same address "
                f"({leg.participant_address}). build_htlc_redeem_script() would refuse it, and "
                f"it means the minted keys collided or were assigned twice"
            )
    return legs


def claim_both_legs(console: Console, funded_a: FundedLeg, funded_b: FundedLeg,
                    parties: dict, secret: bytes) -> tuple[str, str]:
    """Steps 4 and 5 of the protocol: claim the PARTICIPANT's leg first, read the preimage
    back off that chain, and only then claim the initiator's.

    WHY THIS IS A FUNCTION AND NOT SIX LINES INSIDE main(). Rule 12: a main() past the
    statement ceiling is orchestration that has swallowed a decision, and the fix is to
    extract the decision so it can be called with seeded inputs rather than to raise the
    ceiling. There are three decisions in here, and every one of them is a way to lose
    money that no amount of reading main() would have caught:

      WHICH LEG IS CLAIMED FIRST. funded_b -- the participant's, the one with the SHORTER
      timelock -- and it is not interchangeable with funded_a. The initiator is the party
      holding the secret, so they are the only one who can move first, and the leg they
      move against is the counterparty's. Claiming funded_a first would publish the
      preimage on the initiator's OWN chain while the participant's leg is still locked:
      the participant learns the secret, claims the leg they were going to receive anyway,
      and the initiator has published their only leverage for nothing.

      WHOSE KEY SIGNS EACH CLAIM. The claim on leg B is signed by the INITIATOR's key on
      leg B's chain, and the claim on leg A by the PARTICIPANT's key on leg A's chain --
      the same inversion build_legs() encodes, one level further on. Swapping these does
      not produce an error a reader would recognize: it produces a signature that fails
      script verification, which surfaces as a rejected transaction on a chain where the
      other leg is already funded.

      WHETHER THE PREIMAGE IS TRUSTED FROM MEMORY. It is not: the claim on leg A is made
      from the bytes read out of leg B's scriptSig, in a function that cannot see the
      generated secret. See below for why that is scope and not a comparison.

    THE THIRD OF THOSE IS ENFORCED BY SCOPE RATHER THAN BY A CHECK, and the first version
    of this function got it wrong in a way its own tests could not see.

    It ended with `if recovered != secret: raise`, which read like the safety net for
    exactly that defect. It is unreachable. read_secret_off_chain() finds the preimage by
    scanning the scriptSig for a push whose SHA-256 equals the committed hash, and the
    committed hash IS sha256(secret) -- so a `recovered` that differs from `secret` and
    still gets returned is a SHA-256 collision. A mutation check on 2026-09-27 proved it
    both ways: deleting the branch killed no test, and swapping `recovered` for `secret` on
    the leg-A claim killed no test either, because in a correct read the two values are
    equal by construction and NOTHING behavioral can separate them.

    So the branch is gone (rule 2: a check that cannot fire is read, greped past and copied
    from) and the guarantee is structural instead: the leg-A claim happens in
    claim_initiator_leg() below, which takes the recovered bytes and has no access to
    `secret` at all. Passing the in-memory copy there is not a mistake a reader has to avoid
    -- it is a name that does not exist. That matters because in a real swap the participant
    HAS no in-memory copy, so a rehearsal that quietly used one would pass while the
    off-chain read was broken.
    """
    leg_b = funded_b.leg
    secret_hash = hashlib.sha256(secret).hexdigest()

    initiator_payout = Party(
        privkey=parties[(leg_b.asset, ROLE_INITIATOR)].wif,
        destination=wallet_destination(Step(console, 7), leg_b.asset,
                                       funded_b.client, "atomic-swap initiator payout"),
    )
    claim_b = claim_leg(Step(console, 7), funded_b, secret, initiator_payout)

    recovered = read_secret_off_chain(Step(console, 8), funded_b, claim_b, secret_hash)
    console.say(f"{len(recovered)} bytes recovered from the {leg_b.asset} chain -- never messaged")
    claim_a = claim_initiator_leg(console, funded_a, parties, recovered)
    return claim_b, claim_a


def claim_initiator_leg(console: Console, funded_a: FundedLeg, parties: dict,
                        preimage: bytes) -> str:
    """Claim the INITIATOR's leg with bytes that came off the other chain.

    Separate from claim_both_legs() for one reason: `secret` is not in scope here. This is
    the participant's move in a real swap, and the participant has only what they read out
    of the initiator's claim -- so a function that cannot see the generated secret is a
    function that cannot accidentally rehearse with it. See claim_both_legs()' docstring for
    the mutation check that made this a structural guarantee instead of a comment.

    The key is the PARTICIPANT's on leg A's chain, because leg A's hashlock branch names it
    -- the inversion build_legs() encodes, two levels on. The destination is a wallet
    address, never a minted one: the minted keys are discarded when this process exits, so
    paying a claim to one is a successful swap whose proceeds nobody can spend.
    """
    leg_a = funded_a.leg
    participant_payout = Party(
        privkey=parties[(leg_a.asset, ROLE_PARTICIPANT)].wif,
        destination=wallet_destination(Step(console, 8), leg_a.asset,
                                       funded_a.client, "atomic-swap participant payout"),
    )
    return claim_leg(Step(console, 8), funded_a, preimage, participant_payout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Drive both legs of an atomic swap between any two of "
                    f"{', '.join(ASSETS)}, either direction.",
    )
    parser.add_argument("--from", dest="from_asset", choices=ASSETS,
                        help="the asset the INITIATOR funds (the longer timelock)")
    parser.add_argument("--to", dest="to_asset", choices=ASSETS,
                        help="the asset the PARTICIPANT funds (expires first)")
    parser.add_argument("--from-amount", type=Decimal, help="amount of --from to lock")
    parser.add_argument("--to-amount", type=Decimal, help="amount of --to to lock")
    parser.add_argument("--run", action="store_true", help="actually fund both legs")
    parser.add_argument("--pairs", action="store_true",
                        help="print every pair this file can drive, and what the rest need")
    return parser


def print_pairs(console: Console) -> int:
    """Every directed pair, derived from ASSETS, plus an honest line on what is missing."""
    console.banner(f"{len(ASSET_PAIRS)} directed pairs this file drives, from {len(ASSETS)} assets")
    for source, destination in ASSET_PAIRS:
        console.say(f"  --from {source} --to {destination}")
    console.say("")
    console.say("NOT here, and each for a PROTOCOL reason rather than missing work:")
    console.say("  XRP  no script. Uses EscrowCreate with a PREIMAGE-SHA-256 condition, which")
    console.say("       is a hashlock but not a P2SH one. XRP<->GRC has its own working driver")
    console.say("       (atomic_swap_xrp_grc.py, both directions, live on testnet). XRP<->BTC")
    console.say("       and XRP<->LTC are that escrow leg against a P2SH leg: real work, undone.")
    console.say("  XMR  NO SCRIPT AT ALL, so there is nowhere to put a hashlock. Every XMR pair")
    console.say("       -- xmr/btc, ltc/xmr, grc/xmr, xrp/xmr -- is blocked on ONE piece of")
    console.say("       protocol work, not four: the adaptor-signature swap. Its four components")
    console.say("       are each tested (adaptor_ecdsa, ed25519_group, dleq_helper, monero_keys")
    console.say("       whose shared 2-of-2 key was swept live on stagenet 2026-09-27) and")
    console.say("       NOTHING COMPOSES THEM. docs/dleq_cross_curve_design.md section 6 stage 5.")
    console.say("  SOL  no HTLC. The stub that existed could not run and was deleted (c4ea027).")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    console = Console(total_steps=8)

    if args.pairs:
        return print_pairs(console)
    if not args.from_asset or not args.to_asset:
        console.banner("PLAN ONLY -- nothing funded. Pass --from, --to, amounts and --run.")
        console.say(f"assets: {', '.join(ASSETS)}   pairs: {len(ASSET_PAIRS)} (--pairs to list)")
        console.say("example: --from GRC --to LTC --from-amount 1000 --to-amount 0.05 --run")
        console.say("")
        console.say("Both daemons must be on TEST networks -- step 1 asks each one and refuses")
        console.say("otherwise. Both legs are funded from wallets you control, so this is a")
        console.say("rehearsal of the protocol rather than a swap with a counterparty.")
        return 0
    if args.from_asset == args.to_asset:
        console.say(f"--from and --to are both {args.from_asset}; a swap needs two chains")
        return 1
    if not args.run:
        console.banner(f"PLAN ONLY: {args.from_asset} -> {args.to_asset}. Add --run to fund.")
        console.say(f"initiator funds {args.from_amount} {args.from_asset} with the LONGER lock")
        console.say(f"participant funds {args.to_amount} {args.to_asset}, expiring FIRST")
        return 0

    try:
        console.banner(f"atomic swap {args.from_asset} -> {args.to_asset}, both legs, TEST networks only")
        console.step(1, "both daemons say which network they are on, and both must be a test one")
        clients = {}
        for asset in (args.from_asset, args.to_asset):
            clients[asset] = client_for(asset)
            name = chain_name(asset, clients[asset])
            console.check(f"{asset} network", name.upper(), "a test network",
                          name.lower() in TEST_CHAIN_NAMES)
            if name.lower() not in TEST_CHAIN_NAMES:
                raise SwapError(
                    f"REFUSING: the {asset} daemon says its chain is {name!r}, which is not one of "
                    f"{sorted(TEST_CHAIN_NAMES)}. Nothing was funded"
                )
        assets = (args.from_asset, args.to_asset)
        parties = mint_parties(Step(console, 2), assets)

        console.step(3, f"one {SECRET_BYTES}-byte secret, its SHA-256 committed on both chains")
        secret = secrets.token_bytes(SECRET_BYTES)
        secret_hash = hashlib.sha256(secret).hexdigest()
        console.say(f"sha256 = {secret_hash}")
        console.say("the preimage itself is NOT printed: whoever learns it can claim either leg")

        leg_a, leg_b = build_legs(args.from_asset, args.to_asset,
                                  args.from_amount, args.to_amount, parties)

        funded_a = FundedLeg(leg_a, fund_leg(Step(console, 4), leg_a, secret_hash,
                                            clients[leg_a.asset]), clients[leg_a.asset])
        funded_b = FundedLeg(leg_b, fund_leg(Step(console, 5), leg_b, secret_hash,
                                            clients[leg_b.asset]), clients[leg_b.asset])

        # AFTER both are funded is too late to refuse, so the ordering is checked against the
        # locktimes the two fund_leg calls DERIVED -- from each chain's own tip and role --
        # before either claim is attempted. A bad ordering here means refund both legs and
        # start again, which the message says, rather than proceeding into a swap the
        # initiator could take both sides of.
        console.step(6, "the participant's leg must expire FIRST -- the security property")
        assert_ordering(
            Step(console, 6),
            initiator_lock=funded_a.funded["locktime"], initiator_tip=funded_a.funded["tip"],
            participant_lock=funded_b.funded["locktime"], participant_tip=funded_b.funded["tip"],
        )

        claim_b, claim_a = claim_both_legs(console, funded_a, funded_b, parties, secret)
        console.say(f"both legs claimed: {claim_b[:16]}... and {claim_a[:16]}...")
        console.say("")
        console.say("BOTH SIDES WERE PLAYED BY THIS PROCESS, so this is a rehearsal of the")
        console.say("protocol rather than a swap with a counterparty. The one step a real")
        console.say("participant does differently is step 8: they are not the claimer, so")
        console.say("`gettransaction` will not find the claim for them and they need -txindex")
        console.say("or a block scan. claim_scriptsig_hex() says so at the site.")
        return console.summary()
    except SwapError as error:
        console.check("swap", str(error), "no refusal", False)
        return console.summary()


if __name__ == "__main__":
    sys.exit(main())
