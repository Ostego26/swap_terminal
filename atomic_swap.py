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
direction... btc/ltc or ltc/btc etc. whatever", alongside
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
    XRP<->any        A DIFFERENT DRIVER, and it works: atomic_swap_xrp.py, both
                     directions, --chain btc|ltc|grc. The XRP Ledger has no script -- it
                     uses EscrowCreate with a PREIMAGE-SHA-256 crypto-condition rather
                     than a P2SH HTLC -- so its legs are a different protocol and cannot
                     be a row in this file's table.

                     AND IT FUNDS ALL THREE NOW. This paragraph said "IT STILL ONLY
                     FUNDS GRC" until 2026-09-29, which was true for part of that day
                     and false by the end of it: both runners moved onto the chain
                     clients (modules/script_leg.py), XRP<->GRC completed OK=15 FAIL=0
                     and XRP<->LTC completed OK=15 FAIL=0 on the new path. BTC has a
                     client and has not been run. chain-first has not been run on any
                     chain and is absent from that driver's PROVEN_LIVE for that reason.

                     THE FIX IS THIS FILE'S OWN INTERFACE. BTCClient, LTCClient and
                     GRCClient all expose create_contract()/redeem_contract()/
                     refund_contract() and build the P2SH themselves -- none of them
                     mentions createhtlc. That driver has a SECOND implementation of
                     "fund an HTLC on a script chain" covering one chain where this one
                     covers three: rule 8, found by running it rather than reading it.

So: six directed pairs here, and two in the XRP driver until its funding
step moves onto the clients above.

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

from chains.daemon_conf import conf_fallback_settings, rpc_url
from chains.registry import missing_settings, why_unconfigured
from config import Config
from microfortnights import format_duration
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
from modules.htlc_contract_api import AMOUNT_KEYWORD as HTLC_AMOUNT_KEYWORD
from modules.htlc_contract_api import create_contract_kwargs
from modules.htlc_spend import preimage_from_scriptsig
from modules.htlc_timelock import (
    ROLE_INITIATOR,
    ROLE_PARTICIPANT,
    SECONDS_PER_BLOCK,
    contract_locktime,
)
from regtest.keys import generate_key
from step_console import Console

# The assets whose legs this file can build: the three with a P2SH HTLC and a client
# exposing create, redeem AND refund. XRP is absent for a PROTOCOL reason rather than
# missing work -- see the module docstring -- and adding it here would be a claim no code
# can honor.
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
# IMPORTED, NOT SPELLED. This file had its own copy and modules/atomic_swapper.py had a
# second, both correct on the day they were written -- and on 2026-09-29 a THIRD caller
# (modules/script_leg.py) reached neither and called positionally instead, which misroutes
# every argument on LTC. Three copies of one fact, and the defect landed in the gap between
# them (rule 8).
AMOUNT_KEYWORD = HTLC_AMOUNT_KEYWORD

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
class PlannedLeg:
    """A leg's chain tip and the absolute locktime derived from it -- BEFORE it is funded.

    THIS TYPE EXISTS BECAUSE THE ORDERING CHECK RAN TOO LATE, and the run that proved it was
    the operator's first real one (2026-09-27, BTC regtest 1358 / LTC regtest 3657). Both legs
    were funded, and THEN step 6 refused -- while its own refusal message said "Nothing was
    funded", which was false at the moment it printed. Two contracts existed on two chains and
    both had to be refunded.

    A locktime is an ABSOLUTE HEIGHT, so it can be computed from a tip read now and still be
    the right value when the funding lands a moment later. That is what makes planning both
    legs first, judging the ordering, and only then funding them, safe -- and it is what makes
    "Nothing was funded" true rather than aspirational.
    """

    leg: Leg
    tip: int
    locktime: int

    @property
    def blocks_remaining(self) -> int:
        return self.locktime - self.tip

    @property
    def seconds_remaining(self) -> float:
        """Blocks converted to wall-clock USING THIS CHAIN'S OWN TARGET INTERVAL.

        The whole point of the type. SECONDS_PER_BLOCK is imported from
        modules/htlc_timelock rather than re-spelled here, because that table is the same
        vocabulary contract_locktime() derives the locktime FROM -- a second copy would be
        free to disagree with the numbers it is checking (rule 8).

        An ESTIMATE, not a deadline the chain owes anybody: it is a target interval, and a
        chain can run fast or slow. It is the right basis for the comparison anyway, because
        the alternative -- comparing block COUNTS across two chains -- is not an estimate but
        a category error.
        """
        return self.blocks_remaining * SECONDS_PER_BLOCK[self.leg.asset]


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

        # THE CLIENT'S OWN vout IS CROSS-CHECKED, NOT TRUSTED AND NOT IGNORED.
        #
        # All three clients return one (atomic_btc_client.py:332 and its siblings), and this
        # driver ignored it entirely -- which was defensible, because matching the scriptPubKey
        # on chain is the stronger method and is why htlc_vout() exists. But ignoring a second
        # opinion throws away a free check: the client derived its index from its own view of
        # the funding transaction, so a DISAGREEMENT means the transaction on chain is not the
        # one the client thinks it funded. That is a refusal, not a preference between two
        # numbers. Spending the wrong index spends nothing and burns a fee.
        reported = self.funded.get("reported_vout")
        if reported is not None and reported != vout:
            raise SwapError(
                f"{self.asset}: the client reported the contract at vout {reported} and the "
                f"chain says {vout}, matched on scriptPubKey {self.script_pubkey_hex()}. Those "
                f"cannot both be right, so neither is used: the funding transaction "
                f"{self.funded['txid']} is not the one the client believes it created"
            )
        return vout


def client_for(asset: str):
    """Construct the chain client from the environment, or refuse naming the variables."""
    if asset not in CLIENTS:
        raise SwapError(
            f"no client for {asset!r}; this file drives {', '.join(ASSETS)}. XRP is a different "
            f"protocol (see atomic_swap_xrp.py) rather than a missing row here"
        )
    url_default, user_default, _ = DEFAULT_RPC[asset]

    # THREE ROUTES, IN THIS ORDER, and the middle one is the fix for 2026-09-29.
    #
    # This file read {ASSET}_RPC_URL and fell back to its own default -- 18332 for
    # BTC, which is TESTNET3 -- while chain_balances.py and atomic_swap_xrp.py read
    # Config.RPC's host and port and found the operator's REGTEST daemon on 18443.
    # Two environment schemes for one fact, so with BTC_RPC_PASS set and
    # BTC_RPC_URL unset this file silently dialed a port nothing was listening on:
    #
    #     HTTPConnectionPool(host='127.0.0.1', port=18332): Connection refused
    #
    # and BTC_RPC_PASS being present also meant the conf fallback never ran. That
    # is the worst shape this gap has taken. The other five instances refused or
    # failed loudly; this one pointed at a DIFFERENT DAEMON than the operator had
    # configured, and on a testnet3 box that was actually running it would have
    # found one and funded a contract there.
    #
    # Config.RPC comes second rather than first so an explicit {ASSET}_RPC_URL
    # still wins -- it is the only route that can name a host this scheme cannot
    # express, and somebody who set it meant it.
    if os.environ.get(f"{asset}_RPC_URL"):
        url = os.environ[f"{asset}_RPC_URL"]
        user = os.environ.get(f"{asset}_RPC_USER", user_default)
        password = os.environ.get(f"{asset}_RPC_PASS", "")
        if password:
            return CLIENTS[asset](url, user, password)

    configured = Config.RPC.get(asset) or {}
    if not missing_settings(Config.RPC, asset):
        print(f"    {asset}: {asset}_RPC_HOST/_PORT/_USER/_PASS from the environment, the same "
              f"source chain_balances.py and atomic_swap_xrp.py read", flush=True)
        return CLIENTS[asset](rpc_url(configured), configured["user"], configured["password"])

    # THE CONF, for the chains whose conf names one daemon. GRC is excluded -- its
    # conf is shared with the operator's mainnet staking wallet -- so GRC stays
    # explicit, which is the right asymmetry.
    settings, line = conf_fallback_settings(asset)
    if settings is not None:
        print(f"    {line}", flush=True)
        return CLIENTS[asset](rpc_url(settings), settings["user"], settings["password"])

    # DEFAULT_RPC LAST, AND ANNOUNCED. It is kept rather than removed because for
    # GRC it is right -- 25715 IS the operator's testnet port -- and a password
    # alone was enough to run this file for weeks. What it must not be any more is
    # SILENT: for BTC the default is 18332, which is testnet3, while the operator's
    # BTC is regtest on 18443, so this route dialed a daemon nobody had configured
    # and said nothing about having guessed. Five earlier instances of this gap
    # refused or failed loudly; this one pointed somewhere else, which is worse.
    password = os.environ.get(f"{asset}_RPC_PASS", "")
    if password:
        print(f"    {asset}: NO url and no configured host/port, so falling back to "
              f"DEFAULT_RPC[{asset!r}] = {url_default}. THAT IS A GUESS at which test network you "
              f"meant -- set {asset}_RPC_PORT if your daemon is elsewhere.", flush=True)
        return CLIENTS[asset](url_default, os.environ.get(f"{asset}_RPC_USER", user_default),
                              password)

    raise SwapError(
        f"{asset} could not be addressed by any route. {asset}_RPC_URL is unset, "
        f"{why_unconfigured(asset, Config.RPC)}, the conf did not supply one ({line}), and "
        f"{asset}_RPC_PASS is not set either -- so even the {url_default} default cannot be used. "
        f"This file will not guess a credential"
    )


def chain_name(asset: str, client) -> str:
    """Which network this daemon is on, ASKED rather than inferred from its port.

    Two routes because the two daemon families keep the answer in different places: Bitcoin
    and Litecoin answer `getblockchaininfo` with a `chain`; Gridcoin does not have that KEY
    and carries a `testnet` boolean on `getinfo` instead. Inferring from the port is what
    this refuses to do -- a port is a convention and a convention is not a check, and
    config.py records what happened the last time one was trusted.

    GRIDCOIN DOES HAVE `getblockchaininfo`. THIS FUNCTION USED TO SAY IT DOES NOT, AND THAT
    IS THE DEFECT WORTH RECORDING HERE, because the claim decided which branch a reader
    thought GRC took. Measured 2026-09-28 on the operator's testnet daemon, through
    adaptor_regtest_verify.py's step 2:

        FAIL  GRC getblockchaininfo.chain: got=(none)  expected=one of ('regtest','test',...)

    `got=(none)` with no exception text is what a SUCCESSFUL call with a missing key prints;
    the failed-call branch prints the error. So the method answered. Gridcoin master registers
    it (src/rpc/server.cpp) and its response pushes eight fields -- blocks, in_sync,
    moneysupply, difficulty, testnet, errors -- with no `chain` among them at any version.

    THE OUTCOME WAS ALWAYS RIGHT AND THE REASON WAS WRONG, which is the only reason this is a
    comment change and not a logic change: GRC falls through on the missing key rather than on
    a method-not-found, reaches `getinfo`, and is named from its `testnet` boolean either way.
    A future reader trusting the old sentence would have gone looking for a -32601 that never
    arrives (rule 16: a wrong comment is a bug, and fix it with the same seriousness).

    NOT CHANGED HERE, and named as work rather than done: `getblockchaininfo` carries
    `testnet` in the SAME response this function already reads, so the GRC answer could come
    from one round trip instead of two -- and Gridcoin's own command table marks `getinfo`
    heritage_removed_upstream, so this file's only GRC route is the deprecated one. That is a
    change to the gate that refuses a mainnet daemon, on the file that funds legs, so it is
    the operator's call rather than a tidy-up (rule 16).
    """
    call = client_caller(client)
    try:
        info = call("getblockchaininfo") or {}
        if info.get("chain"):
            return str(info["chain"])
    except Exception as error:  # noqa: BLE001 -- checked: this is a PROBE for one of two places the answer lives, and any failure of it is the signal to try `getinfo` rather than a verdict. A daemon older than the caller's assumptions, a method this family never had, and a transport failure all mean the same thing here: ask the other way. Nothing is swallowed -- the reason is carried in `first` and folded into the refusal below if BOTH routes fail, and neither route can return a value a caller would mistake for a named network.
        # THE MESSAGE, NOT JUST THE TYPE. `getblockchaininfo: RPCError` cannot be told apart
        # from a wrong rpcpassword, and "nothing answered" is what sent the operator to check
        # credentials that were fine on 2026-09-28 when the real cause was a method this
        # family does not have. The daemon's own `code=-32601` is the one thing that separates
        # the two, and it is inside the message.
        first = f"getblockchaininfo: {type(error).__name__}: {error}"
    else:
        first = "getblockchaininfo: answered, with no `chain` key -- which is Gridcoin's shape, not a fault"
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


def assert_ordering(step: Step, initiator: PlannedLeg, participant: PlannedLeg) -> None:
    """The PARTICIPANT's leg must expire STRICTLY FIRST -- compared in TIME, not in blocks.

    THIS IS THE SECURITY PROPERTY AND IT IS NOT A CONVENTION. If the participant's lock
    outlived the initiator's, the initiator could sit on the secret until their OWN refund
    became spendable, take their coins back, and then claim the participant's leg with the
    secret they never used -- taking both. The participant has no counter: the secret is
    theirs to learn, not to produce.

    THE UNIT IS SECONDS, AND THE PREVIOUS VERSION OF THIS FUNCTION GOT IT WRONG IN THE EXACT
    WAY ITS OWN DOCSTRING WARNED ABOUT. It compared BLOCKS REMAINING and explained, correctly,
    that "Litecoin at 2.5 minutes a block and Bitcoin at 10 means the same block count is four
    times the wall-clock" -- and then subtracted one block count from the other anyway. Blocks
    remaining is the comparable unit WITHIN one chain; across two it is a category error.

    Measured on the operator's first real run, 2026-09-27, BTC regtest tip 1358 / LTC regtest
    tip 3657, and this is a correctly built swap that the old check REFUSED:

        initiator   BTC  288 blocks x 600s = 172800s = 48h   <- INITIATOR_LOCK_HOURS
        participant LTC  576 blocks x 150s =  86400s = 24h   <- PARTICIPANT_LOCK_HOURS
        margin in blocks:   288 - 576 = -288   -> refused, wrongly
        margin in seconds: 172800 - 86400 = +86400 = 24h -> correct, and safe

    modules/htlc_timelock derives each leg from a policy stated in HOURS (48 and 24) and
    converts through that chain's own SECONDS_PER_BLOCK, so the two legs were never meant to
    have comparable block counts. Litecoin needs FOUR TIMES the blocks for half the time.
    A false refusal is not a safe failure here: it refused after both legs were funded, so it
    cost two refunds and could have cost a swap a counterparty was waiting on.

    Refuses rather than warns. A warning on this is a warning nobody reads until a swap has
    been taken.
    """
    margin_seconds = initiator.seconds_remaining - participant.seconds_remaining
    for label, planned in (("initiator", initiator), ("participant", participant)):
        step.say(
            f"{label} leg ({planned.leg.asset}) expires in {planned.blocks_remaining} blocks "
            f"= {format_duration(planned.seconds_remaining)} at {planned.leg.asset}'s "
            f"{SECONDS_PER_BLOCK[planned.leg.asset]}s target interval (height {planned.locktime})"
        )
    step.check("participant expires FIRST", f"margin {format_duration(margin_seconds)}",
               "a positive margin", margin_seconds > 0)
    if margin_seconds <= 0:
        raise SwapError(
            f"REFUSING: the participant's leg expires {format_duration(-margin_seconds)} LATER "
            f"than the initiator's, not earlier. That lets the initiator wait out their own "
            f"lock, refund their leg, and THEN claim the participant's with the secret -- "
            f"taking both. Nothing was funded"
        )
    if participant.blocks_remaining <= 0:
        raise SwapError(
            f"REFUSING: the participant's leg is already expired at funding time "
            f"({participant.blocks_remaining} blocks). Its refund branch would be spendable "
            f"the moment it is funded, which is the absence of a timelock rather than a short "
            f"one. Nothing was funded"
        )


def report_completed_swap(console: Console, claim_a: str, claim_b: str) -> int:
    """What a successful run did and did NOT establish. Extracted for rule 12's ceiling.

    The caveat is the point, and it is the one an operator most easily reads past: both sides
    were played by ONE process, so the cryptography and the chain behavior are proven while the
    thing a counterparty actually depends on -- that they can READ the preimage off a claim they
    did not make -- is not. That difference is a lookup route, not a protocol question, and it
    is named at claim_scriptsig_hex() rather than left here as a footnote.
    """
    console.say(f"both legs claimed: {claim_b[:16]}... and {claim_a[:16]}...")
    console.say("")
    console.say("BOTH SIDES WERE PLAYED BY THIS PROCESS, so this is a rehearsal of the")
    console.say("protocol rather than a swap with a counterparty. The one step a real")
    console.say("participant does differently is step 8: they are not the claimer, so")
    console.say("`gettransaction` will not find the claim for them and they need -txindex")
    console.say("or a block scan. claim_scriptsig_hex() says so at the site.")
    return console.summary()


def report_dry_run(console: Console) -> int:
    """Say what a dry run PROVED and what it did not, then stop. Extracted for rule 12's
    statement ceiling, and it earns the name: the distinction it draws is the whole value of a
    dry run, and a reader has to be able to find it."""
    console.say("")
    console.say("DRY RUN COMPLETE -- nothing was funded, and every check above was read-only.")
    console.say("PROVEN: both daemons answered, both are on test networks, and the timelock")
    console.say("ordering for this pair is safe.")
    console.say("NOT PROVEN: that either wallet holds a spendable balance, that an encrypted")
    console.say("wallet will unlock, or that create_contract will be accepted by the daemon.")
    console.say("Add --run to fund both legs.")
    return console.summary()


def open_test_clients(step: Step, assets: tuple[str, ...]) -> dict:
    """A client per asset, each REFUSED unless its daemon says it is on a test network.

    The network is ASKED, never inferred from a port number -- a regtest daemon on 18332 and a
    mainnet daemon on 18443 are both one config line away, and a port is a convention while
    `getblockchaininfo.chain` is the daemon's own answer.

    Extracted from main() for ruff's statement ceiling, which rule 12 says to answer by
    extracting the decision rather than raising the limit. The decision here is what counts as
    a test network, and it is now callable with a seeded client.
    """
    clients = {}
    for asset in assets:
        clients[asset] = client_for(asset)
        name = chain_name(asset, clients[asset])
        step.check(f"{asset} network", name.upper(), "a test network",
                   name.lower() in TEST_CHAIN_NAMES)
        if name.lower() not in TEST_CHAIN_NAMES:
            raise SwapError(
                f"REFUSING: the {asset} daemon says its chain is {name!r}, which is not one of "
                f"{sorted(TEST_CHAIN_NAMES)}. Nothing was funded"
            )
    return clients


def plan_leg(step: Step, leg: Leg, client) -> PlannedLeg:
    """Read this chain's tip and derive the leg's absolute locktime. FUNDS NOTHING.

    Split out of fund_leg() so assert_ordering() can run before either chain is touched. The
    old order was fund, fund, check -- which meant a refusal left two contracts on two chains
    and a message claiming nothing had been funded.
    """
    tip = int(client_caller(client)("getblockcount"))
    locktime = contract_locktime(leg.asset, leg.role, tip)
    planned = PlannedLeg(leg=leg, tip=tip, locktime=locktime)
    step.say(
        f"{leg.asset} tip {tip}; role {leg.role}; locktime {locktime} "
        f"({planned.blocks_remaining} blocks = {format_duration(planned.seconds_remaining)})"
    )
    return planned


# WHAT create_contract() ACTUALLY RETURNS, read from the three clients rather than guessed.
#
# All three -- atomic_btc_client.py:330, atomic_ltc_client.py:388, atomic_grc_client.py:430 --
# return the SAME shape, and it is not the shape this driver first assumed:
#
#     {"txid": str, "vout": int, "redeemScript": BYTES, "p2shAddress": str}
#
# camelCase, and the script is BYTES, not hex. The driver looked for "redeem_script" and
# "p2sh_address", found neither, and fell through to its `or ""` defaults -- which is how the
# operator's 2026-09-27 run printed a bare "BTC contract at" with nothing after it, and then
# died on `bytes.fromhex("b'\x63\xa8...")` once the address was derived instead: str() of a
# bytes object is its repr, so position 1 is the quote character.
#
# BOTH SPELLINGS ARE ACCEPTED because accepting one and guessing is what produced the blank
# line; a key that is ABSENT under every spelling is a refusal, never a default (rule 2's "I
# could not find a caller is not there is no caller", applied to a dict key).
CONTRACT_TXID_KEYS = ("txid", "transaction_id")
CONTRACT_SCRIPT_KEYS = ("redeemScript", "redeem_script", "redeemscript")
CONTRACT_ADDRESS_KEYS = ("p2shAddress", "p2sh_address", "address")
CONTRACT_VOUT_KEYS = ("vout", "n")


def _first_present(contract: dict, keys: tuple[str, ...]):
    """The first key of `keys` that the contract carries, or None. Never a default value."""
    for key in keys:
        if contract.get(key) is not None:
            return contract[key]
    return None


def script_hex_from(value) -> str:
    """A redeem script as HEX, whether the client handed back bytes or a hex string.

    THE BYTES CASE IS THE REAL ONE -- all three clients return bytes -- and it is handled here
    rather than at the call site because `str(b"\x63")` is "b'c'" and not an error. A wrong
    conversion that raises is a good day; this one produces a plausible string that fails five
    lines later with a message about hexadecimal, which is what happened on 2026-09-27.
    """
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    text = str(value).strip()
    try:
        bytes.fromhex(text)
    except ValueError as error:
        raise SwapError(
            f"the redeem script is neither bytes nor hex: {text[:40]!r}... ({error}). A client "
            f"returning something else is a contract this driver cannot spend"
        ) from error
    return text


def read_contract(step: Step, asset: str, contract: dict) -> dict:
    """Normalize one client's create_contract answer, or REFUSE naming what was missing.

    One place, because three clients answer in one shape and a fourth would be free to differ
    (rule 8). The refusal lists the keys the contract DID carry, so an operator can see the
    spelling that arrived instead of the one expected.
    """
    txid = _first_present(contract, CONTRACT_TXID_KEYS)
    script = _first_present(contract, CONTRACT_SCRIPT_KEYS)
    if not txid or script is None:
        raise SwapError(
            f"{asset}: create_contract returned no txid or no redeem script. Keys present: "
            f"{sorted(contract)}; txid looked for {CONTRACT_TXID_KEYS}, script "
            f"{CONTRACT_SCRIPT_KEYS}"
        )
    script_hex = script_hex_from(script)
    reported_vout = _first_present(contract, CONTRACT_VOUT_KEYS)
    return {
        "txid": str(txid),
        "redeem_script": script_hex,
        "p2sh_address": str(_first_present(contract, CONTRACT_ADDRESS_KEYS) or ""),
        "reported_vout": None if reported_vout is None else int(reported_vout),
        "script_pubkey": p2sh_script_for(bytes.fromhex(script_hex)).hex(),
    }


def fund_leg(step: Step, planned: PlannedLeg, secret_hash: str, client) -> dict:
    """Fund a leg whose locktime was already decided, then find its vout ON CHAIN.

    Takes a PlannedLeg rather than reading the tip itself, so the ordering check has already
    passed by the time anything is funded. The locktime is an absolute height, so the value
    planned a moment ago is still the right one now.

    The vout matters and is the defect this repository has now fixed three times -- BTC
    2026-09-25, GRC 2026-09-26 -- and it is why modules/htlc_chain_read.htlc_vout() exists.
    The clients DO return a vout, and it is kept as `reported_vout` and cross-checked rather
    than trusted: find_vout() matches the scriptPubKey on chain, and a disagreement means the
    funding transaction does not pay the contract the daemon described.
    """
    leg = planned.leg
    step.announce(f"fund the {leg.role} leg: {leg.amount} {leg.asset}, locktime {planned.locktime}")

    # THROUGH THE ONE BUILDER. This site spelled the five keys itself, which was
    # correct -- and was also the fifth copy of a call shape whose sixth copy
    # (modules/script_leg.py) got it wrong by going positional. create_contract_kwargs
    # also REFUSES an empty secret hash, which this site always passed and now
    # cannot stop passing.
    fields = read_contract(step, leg.asset, client.create_contract(**create_contract_kwargs(
        leg.asset,
        amount=leg.amount,
        secret_hash=secret_hash,
        participant_address=leg.participant_address,
        refund_address=leg.refund_address,
        locktime=planned.locktime,
    )))
    step.check(f"{leg.asset} funding txid", fields["txid"][:16] + "...", "a txid",
               bool(fields["txid"]))
    address = fields["p2sh_address"] or "(none reported; derived below)"
    step.say(f"{leg.asset} contract at {address}, scriptPubKey {fields['script_pubkey']}")
    return {"txid": fields["txid"], "p2sh_address": fields["p2sh_address"],
            "redeem_script": fields["redeem_script"], "reported_vout": fields["reported_vout"],
            "locktime": planned.locktime, "tip": planned.tip}


def claim_leg(step: Step, funded_leg: FundedLeg, secret: bytes, party: Party) -> str:
    """Claim a leg with the preimage. This PUBLISHES the secret, which is the point.

    Step 4 of the protocol. The initiator does this to the participant's leg, and doing so
    puts the 32 bytes into a scriptSig on a public chain -- which is how the participant
    learns them without anybody sending a message. The secret is never logged here.
    """
    step.announce(f"claim the {funded_leg.leg.role} leg with the secret -- this PUBLISHES it")
    vout = funded_leg.find_vout(step)
    # `secret` GOES IN AS BYTES, NOT HEX. All three clients declare `secret: bytes` and push
    # it straight onto the stack via push_data(), so a hex STRING reaches
    # `bytes([length]) + data` and dies with "can't concat str to bytes" -- which is where the
    # operator's 2026-09-27 run stopped, with both legs already funded. Passing `.hex()` here
    # was the same mistake as reading `redeem_script` from the contract dict: an interface
    # guessed rather than read. The types are checked against all three signatures now
    # (atomic_{btc,ltc,grc}_client.redeem_contract): str, int, bytes, bytes, str, str.
    txid = funded_leg.client.redeem_contract(
        funded_leg.funded["txid"], vout, funded_leg.redeem_script(),
        secret, party.privkey, party.destination,
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
    # EITHER CASE, because the other driver takes either and one tree should not
    # disagree with itself about the spelling of an asset. atomic_swap_xrp.py has
    # `type=str.lower` on --chain and accepts `btc`; this file accepted only `BTC`
    # and answered
    #
    #     error: argument --from: invalid choice: 'btc' (choose from 'BTC', 'GRC', 'LTC')
    #
    # which is a correct message about a distinction that should not exist. `type`
    # runs BEFORE `choices` is checked, so upper-casing there is what makes both
    # spellings reach the same row rather than one of them reaching an error.
    parser.add_argument("--from", dest="from_asset", choices=ASSETS, type=str.upper,
                        help="the asset the INITIATOR funds (the longer timelock). Either case.")
    parser.add_argument("--to", dest="to_asset", choices=ASSETS, type=str.upper,
                        help="the asset the PARTICIPANT funds (expires first). Either case.")
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
    console.say("       (atomic_swap_xrp.py, both directions, live on testnet). Its --chain flag")
    console.say("       offers btc and ltc but REFUSES them: it funds with Gridcoin's createhtlc,")
    console.say("       and the fix is to route it through the clients above, which build the P2SH.")
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
        console.banner(f"DRY RUN: {args.from_asset} -> {args.to_asset}. Nothing will be funded.")
        console.say(f"initiator funds {args.from_amount} {args.from_asset} with the LONGER lock")
        console.say(f"participant funds {args.to_amount} {args.to_asset}, expiring FIRST")
        console.say("")
        console.say("This contacts BOTH daemons -- read-only -- and stops before funding.")

    try:
        if args.run:
            console.banner(
                f"atomic swap {args.from_asset} -> {args.to_asset}, both legs, TEST networks only"
            )
        console.step(1, "both daemons say which network they are on, and both must be a test one")
        clients = open_test_clients(Step(console, 1), (args.from_asset, args.to_asset))
        assets = (args.from_asset, args.to_asset)
        parties = mint_parties(Step(console, 2), assets)

        console.step(3, f"one {SECRET_BYTES}-byte secret, its SHA-256 committed on both chains")
        secret = secrets.token_bytes(SECRET_BYTES)
        secret_hash = hashlib.sha256(secret).hexdigest()
        console.say(f"sha256 = {secret_hash}")
        console.say("the preimage itself is NOT printed: whoever learns it can claim either leg")

        leg_a, leg_b = build_legs(args.from_asset, args.to_asset,
                                  args.from_amount, args.to_amount, parties)

        # PLAN BOTH LEGS, CHECK THE ORDERING, THEN FUND. The order used to be fund, fund,
        # check -- and the operator's first real run (2026-09-27) hit exactly the failure
        # that shape allows: both legs went onto two chains and the refusal printed
        # "Nothing was funded" while two contracts existed. A locktime is an absolute
        # height, so deciding it from a tip read now stays correct when the funding lands a
        # moment later; nothing is gained by reading the tip later and a refund is lost.
        console.step(4, "plan both legs from each chain's own tip -- nothing is funded yet")
        planned_a = plan_leg(Step(console, 4), leg_a, clients[leg_a.asset])
        planned_b = plan_leg(Step(console, 4), leg_b, clients[leg_b.asset])

        console.step(5, "the participant's leg must expire FIRST -- the security property")
        assert_ordering(Step(console, 5), initiator=planned_a, participant=planned_b)

        # A DRY RUN STOPS HERE, HAVING DONE EVERYTHING THAT DOES NOT MOVE MONEY.
        #
        # It used to stop before step 1 and print three lines from its own arguments, which is a
        # plan that cannot fail -- and a check that cannot fail is the defect rule 13 names when
        # it says "skipped" and "success" must not share an output. The operator's 2026-09-27
        # GRC dry run printed a clean plan against a daemon nobody had contacted; two runs
        # earlier the same clean plan preceded a connection-refused traceback on a chain the
        # driver had never reached.
        #
        # Everything above this line is read-only: getblockchaininfo/getinfo to name each
        # network, and getblockcount to derive each locktime. So a dry run now PROVES the two
        # daemons answer, that both are on test networks, and that the timelock ordering this
        # pair produces is safe -- which is every refusal the real run can hit before the first
        # irreversible transfer. What it cannot prove is a balance, a wallet unlock, or a
        # create_contract that the daemon refuses, and it says so rather than implying otherwise.
        if not args.run:
            return report_dry_run(console)

        console.step(6, "both legs are funded only now that the ordering is proven safe")
        funded_a = FundedLeg(leg_a, fund_leg(Step(console, 6), planned_a, secret_hash,
                                             clients[leg_a.asset]), clients[leg_a.asset])
        funded_b = FundedLeg(leg_b, fund_leg(Step(console, 6), planned_b, secret_hash,
                                             clients[leg_b.asset]), clients[leg_b.asset])

        claim_b, claim_a = claim_both_legs(console, funded_a, funded_b, parties, secret)
        return report_completed_swap(console, claim_a, claim_b)
    except SwapError as error:
        console.check("swap", str(error), "no refusal", False)
        return console.summary()


if __name__ == "__main__":
    sys.exit(main())
