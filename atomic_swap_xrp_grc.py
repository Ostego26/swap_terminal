#!/usr/bin/env python3
"""Run a REAL atomic swap between XRP and GRC on their test networks.

Role: file (the entry point; the decisions are swap_timelocks() and
      assert_timelock_ordering() here, the preimage read in
      modules/htlc_spend.preimage_from_scriptsig(), the condition encoding in
      chains/xrp_crypto_condition.py, and the lock policy in
      modules/htlc_timelock.py)
Reads: the XRPL testnet endpoint in chains/xrp_testnet.py, the faucet accounts
       in ~/.config/swap_terminal/keys/, and the Gridcoin daemon through
       Config.RPC["GRC"]
Writes: nothing on disk. It SUBMITS an EscrowCreate, an EscrowFinish, a
       createhtlc and a claimhtlc, and only with --run.
Can move funds: YES, on both chains, and this is the first file in this tree
       that moves money on two chains in one program. Testnet only.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED, on BOTH legs, before anything is
       submitted: refuse_mainnet() interrogates the rippled server's network_id
       and step 1 refuses any Gridcoin daemon that does not report testnet or
       regtest. Neither refusal has a flag that turns it off.

WHAT THIS IS, AND WHY IT IS NOT ANOTHER VERIFIER.

Three harnesses in this tree prove HTLC PRIMITIVES: regtest_htlc_verify.py for
BTC and LTC, xrp_htlc_escrow.py for XRPL, and the Gridcoin daemon's own
htlc_tests.cpp for GRC. All of them pass. None of them is a swap, and a pile of
verified primitives is not what anybody wanted -- the swap is the thing, and
this is it.

THE PROTOCOL, and every step below is named after the party who acts:

    A holds XRP and wants GRC.   A is the INITIATOR.
    B holds GRC and wants XRP.   B is the PARTICIPANT.

    1. A picks a 32-byte secret and publishes only sha256(secret).
    2. A funds the XRP leg: an Escrow to B, with a PREIMAGE-SHA-256 Condition
       and CancelAfter at A's own (LONGER) timelock.
    3. B funds the GRC leg: createhtlc with the same sha256, claimable by A,
       refundable to B after B's (SHORTER) timelock.
    4. A claims the GRC leg with the secret. Claiming REQUIRES pushing the
       secret into a scriptSig that lands in a block, so A cannot take the GRC
       without publishing it.
    5. B READS THE SECRET OFF THE GRIDCOIN CHAIN -- not from A, who is not
       trusted and never sends it -- and finishes the XRP escrow with it.

Step 5 is what makes this atomic rather than hopeful, and it is the piece that
did not exist until today: modules/htlc_spend.preimage_from_scriptsig() reads
A's claim transaction, hashes every push, and returns the one whose sha256
matches the commitment. It verifies rather than pattern-matching a 32-byte push,
because a signature, a pubkey and a redeem script are all attacker-influenced
lengths on a public chain.

WHY ONE PREIMAGE OPENS BOTH LEGS, which is the whole reason this pairing works.
Gridcoin's own HTLC script commits through `OP_SHA256 <hash> OP_EQUALVERIFY`
(src/htlc.cpp::CreateHTLCScript, read 2026-09-26) and XRPL's escrow commits
through a PREIMAGE-SHA-256 crypto-condition whose fingerprint is sha256 of the
same bytes. Not RIPEMD, not HASH160, not SHA-512/256. If either side ever
changed, this program would fund both legs and neither could be claimed.

THE TIMELOCK ORDERING IS THE ONE THING THAT CAN LOSE MONEY HERE, so it is
asserted before either leg is funded and the assertion is a function that can be
tested without a chain. If the PARTICIPANT's lock outlived the INITIATOR's, A
could take the GRC in step 4 and then refund the XRP as soon as its own lock
expired, leaving B with nothing -- B's only defence is that B's leg expires
first, so B can always recover before A can. modules/htlc_timelock.py already
states the policy (48 hours for the initiator against 24 for the participant)
and explains the asymmetry; this converts it into the two chains' different
CLOCKS, which is where it can go wrong: XRPL's CancelAfter is a wall-clock
instant and Gridcoin's timeout is a BLOCK HEIGHT.

BOTH DIRECTIONS RAN, PRICED, OK=16 FAIL=0 EACH. The state of this file as of
2026-09-27, and the numbers below are the operator's runs rather than a
description of what should happen:

    xrp-first   XRP escrow 0DFBA6CBC71C6620..., GRC HTLC
                4e7d1fa4fa2fbff1... at vout 1, GRC claimed
                78472df347a91f31..., secret read off the scriptSig on attempt 1,
                escrow finished 784CDE9E10A36470..., B +1000000 drops exactly.
    grc-first   GRC HTLC fb6c2b2493dfc4f0... at vout 1, XRP escrow
                D34AB1D50E4D6846..., XRP claimed 74B3DECC13BB12CD..., secret read
                off the Fulfillment on attempt 1, GRC claimed
                845315f4c2063670..., A +999640 = 1000000 less the 360-drop fee
                A itself paid.

AT THE MARKET RATE, which is the part that makes them swaps rather than two
interlocked transfers: GRC $0.02299459 against XRP $1.52, so 0.01512802 XRP per
GRC, and 1 XRP bought 66.10250498 GRC on both runs. The division direction is
the thing to check if a run ever looks wrong -- 1.52 / 0.02299459 = 66.1025, and
inverting it gives 0.0151, which is a plausible-looking number and wrong by the
square of the price.

THE FIRST RUN OF EACH DIRECTION, and what each one cost, kept because the
failures are the evidence:

xrp-first first completed 2026-09-26 at OK=15 (GRC claim b39897c6aa14970e...,
escrow finished B9B856C23BB36FB0...), after three step-6 defects -- the unlock
wrapped around the wrong call, snake_case response keys printing p2sh=None on a
successful call, and an ASSUMED vout that the run then proved wrong by landing
the HTLC at index 1 with the change at index 0.

grc-first completed on its first run with one FAIL, and the FAIL was the
assertion rather than the swap: +999640 against a +1000000 escrow is exactly the
EscrowFinish fee, because in this direction the claimer IS the destination and
pays it out of the account being credited. One expectation was covering two
different arithmetics.

IT RAN, AND IT WORKED -- the original xrp-first run, 2026-09-26, OK=15 FAIL=0.

The first end-to-end atomic swap in this repository. XRPL testnet build 3.4.1
against Gridcoin testnet at height 3294728, --hours-scale 0.02 (58 minutes
against 29), one sha256 opening both legs:

    step 5  XRP leg funded      escrow validated 24FACB8F1F40D64B...,
                                OfferSequence 21051278
    step 6  GRC leg funded      createhtlc d9966f6d7f6b6947...,
                                p2sh 2NEYG15rtRNdHEEGypNyBn91T3zc2gbTefF,
                                HTLC output located at VOUT 1
    step 7  A claimed the GRC   claimhtlc b39897c6aa14970e... -- the secret is
                                now public, by construction
    step 8  B read the secret   off A's own claim scriptSig (222 bytes), on the
                                first attempt, and it EQUALLED what A committed
                                to
    step 9  B claimed the XRP   escrow finished, validated B9B856C23BB36FB0...,
                                B's balance 116000000 -> 117000000 drops,
                                +1000000 EXACTLY

VOUT 1 IS THE LINE WORTH KEEPING. An earlier version of this file passed
`int(htlc.get("vout", 0))` to claimhtlc, because createhtlc returns no vout at
all. The real output was at index 1 -- index 0 was the change SendMoney added --
so the assumed index was wrong on the very first run that got that far, and the
claim would have spent, or failed against, the wrong output. htlc_vout() locates
it by scriptPubKey hex instead. An assumption that is wrong the first time it is
exercised is the argument for not making it.

Step 8 succeeding on attempt 1 is what makes the word "atomic" honest here: B's
fulfillment came out of a transaction A broadcast, not out of a variable this
program happened to be holding, and the assertion that the two are equal is in
the run above.

THREE DEFECTS CAME OUT OF THE RUNS BEFORE IT, all in step 6, all mine, and all
of them found by reading Gridcoin's own src/rpc/htlc.cpp rather than guessing:
the unlock was wrapped around claimhtlc only when createhtlc needs one too (rpc
-13, `Wallet is unlocked for staking only.`); the response keys are snake_case
(p2sh_address, redeem_script) so a successful call printed p2sh=None; and the
vout above. The first failure funded the XRP leg and stopped -- and the safety
line was right: nobody had the secret, so nothing was lost and the escrow
returned to A at its CancelAfter.

ONE OPERATOR PLAYS BOTH PARTIES, and that limit is stated rather than glossed.
The XRP accounts are two faucet accounts on one machine and both Gridcoin
addresses are in one wallet, so this does not exercise a counterparty who
disappears or cheats -- it exercises the MECHANISM, on real chains, with real
confirmations. Every refusal that protects a real counterparty is verified
separately and by branch: the wrong-fulfillment refusal and the early-cancel
refusal in xrp_htlc_escrow.py, the early-refund refusal by consensus in
regtest_htlc_verify.py step 8.

HOW TO RUN IT

    cd <repo>
    source .venv/bin/activate
    source ~/.config/swap_terminal/env.sh
    python3 atomic_swap_xrp_grc.py            # describes every step, submits nothing
    python3 atomic_swap_xrp_grc.py --run      # funds both legs and completes the swap
    python3 atomic_swap_xrp_grc.py --run --direction grc-first   # the other way round

BOTH DIRECTIONS RUN, and --direction names which chain the INITIATOR is on --
not who wants what, because the initiator is the role the timelock policy in
modules/htlc_timelock.py is written against, and naming it after the desire
inverts on every reading.

    xrp-first   A funds the XRP escrow, B funds the GRC HTLC, A claims the GRC
                (publishing the secret in a scriptSig), B reads it there and
                finishes the escrow. VERIFIED 2026-09-26, OK=15.
    grc-first   B funds the GRC HTLC, A funds the XRP escrow, A claims the XRP
                (publishing the secret in an EscrowFinish's Fulfillment), B
                reads it there and claims the GRC.

The reverse direction is not the same protocol with the labels swapped, and two
things genuinely differ:

  WHICH READER RECOVERS THE SECRET. Whichever leg is claimed FIRST is where the
  secret becomes public, so xrp-first reads a Gridcoin scriptSig
  (modules/htlc_spend.preimage_from_scriptsig) and grc-first reads an XRPL
  Fulfillment (chains/xrp_crypto_condition.preimage_from_escrow_finish). The
  second reader was written before the reverse direction, because without it the
  swap simply cannot run that way. Both verify the sha256 rather than trusting
  the framing: a well-formed fulfillment for somebody ELSE'S secret is a thing a
  public ledger carries for free.

  WHICH ACCOUNT SIGNS. In grc-first the escrow is CREATED by the XRP holder and
  FINISHED by the GRC holder, so two XRP secrets are used in one run. submit_xrp
  therefore takes the secret per call instead of closing over one account -- the
  version that closed over A's secret would have signed the finish as the wrong
  party, which the ledger answers with an invalid signature and no explanation.

AND THE TIMELOCKS MOVE WITH THE ROLE, which is the part that can lose money. The
initiator's 48 hours goes to whichever chain the initiator is on, and
assert_timelock_ordering() reads the direction to decide which leg must expire
first. A check hardcoded to "GRC before XRP" would have passed grc-first with the
expiries inverted -- both legs funding, every transaction succeeding, and the
loss arriving when a lock expired. tests/test_atomic_swap_timelocks.py scores
grc-first's correct timelocks under the FORWARD direction and requires a refusal,
which is what proves the check is not hardcoded.

It needs two XRP faucet accounts, a Gridcoin TESTNET daemon with the HTLC RPCs
(`gridcoinresearchd -testnet help createhtlc` must answer), and
GRC_WALLET_PASSPHRASE for the claim. The dry run needs none of the secrets and
reaches both chains read-only.

THE SECRET IS NEVER PRINTED. Not the preimage, not the fulfillment that contains
it, not at any verbosity -- the same rule modules/htlc_spend.py follows. Its
sha256 is printed, because that is the public commitment and it goes on two
public chains in steps 2 and 3.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"
if str(APP_ROOT) not in sys.path:
    # The rootless-import gap rule 10 names, and which regtest_htlc_verify.py
    # and xrp_htlc_escrow.py both document at their own copy of these lines.
    sys.path.insert(0, str(APP_ROOT))

from chains.registry import build_adapters  # noqa: E402 -- the sys.path line above must run first
from chains.xrp_crypto_condition import (  # noqa: E402 -- same
    HTLC_PREIMAGE_BYTES,
    preimage_condition,
    preimage_from_escrow_finish,
    preimage_fulfillment,
)
from chains.xrp_submit import LocalSigningUnavailable, Submitter  # noqa: E402 -- same
from chains.xrp_testnet import TESTNET_URL, refuse_mainnet, rpc, saved_faucet_accounts  # noqa: E402 -- same

# DROPS_PER_XRP is imported, not respelled: chains/xrp_units.py owns it and a
# second copy of a unit conversion is rule 8's bug with a delay on it.
from chains.xrp_units import DROPS_PER_XRP  # noqa: E402 -- same
from config import Config  # noqa: E402 -- same
from microfortnights import format_duration  # noqa: E402 -- same
from modules.htlc_spend import preimage_from_scriptsig  # noqa: E402 -- same

# SECONDS_PER_BLOCK is imported rather than respelled: it is the number that
# turns the participant's policy in hours into a Gridcoin block height, and a
# second copy is how the two legs would come to disagree about when the shorter
# leg expires (rule 8).
from modules.htlc_timelock import (  # noqa: E402 -- same
    ROLE_INITIATOR,
    ROLE_PARTICIPANT,
    SECONDS_PER_BLOCK,
    lock_hours_for_role,
)
from step_console import Console  # noqa: E402 -- same

from xrp_htlc_escrow import (  # noqa: E402 -- same: the escrow payloads and the read-only helpers are that file's, not copied here (rule 8)
    RIPPLE_EPOCH_OFFSET_SECONDS,
    balance_drops,
    describe_result,
    engine_result,
    escrow_create_tx,
    escrow_finish_tx,
    finish_fee_drops,
    ripple_time,
    wait_validated,
)

SECONDS_PER_HOUR = 3600

# WHAT THE XRP LEG MOVES. Fixed, and small: two faucet accounts hold 100 XRP
# each and XRPL's base reserve must stay free.
XRP_DROPS = 1_000_000          # 1 XRP

# THE GRC LEG IS PRICED, NOT FIXED, as of 2026-09-26. It used to be a hardcoded
# "1.0" beside 1 XRP, which is a 1:1 swap at no rate at all -- and on testnet
# that costs nothing, which is exactly why it would have survived into a place
# where it costs something. A swap that does not price its legs is not a swap,
# it is two transfers that happen to be interlocked.
#
# The rate comes from services/pricing.py, which is the same table the custodial
# quote path uses (rule 8: one price source, not a second one written for this
# file). --rate overrides it for a run where CoinGecko is unreachable or where
# the operator wants a specific figure, and --grc-amount pins the amount outright
# and skips pricing entirely.
#
# ROUNDED DOWN to 8 decimal places, toward the party who is NOT being asked to
# trust the rounding: the GRC leg is what the XRP buyer receives, so rounding it
# down favours the GRC holder, and a rate applied with no stated rounding
# direction is a fee nobody agreed to.
GRC_DECIMALS = 8
DEFAULT_GRC_AMOUNT = "1.0"


def grc_amount_for_rate(xrp_drops: int, xrp_per_grc: Decimal) -> Decimal:
    """How much GRC one side of the swap is worth at this rate.

    `xrp_per_grc` is how many XRP one GRC costs -- services/pricing's
    derive_pair_rate("GRC", "XRP") -- so the GRC amount is the XRP amount divided
    by it. Written as a division rather than folded into the caller so the
    direction of the rate is visible in one place: inverting it silently makes a
    swap off by the square of the price, which on testnet looks like a large
    number and nothing else.

    Decimal throughout. A float here would round at the 17th digit and the
    quantize below would inherit it, and the amount is what a daemon is asked to
    send.
    """
    if xrp_per_grc <= 0:
        raise ValueError(f"a rate must be positive; got {xrp_per_grc}. Nothing was priced.")
    xrp = Decimal(xrp_drops) / Decimal(DROPS_PER_XRP)
    return (xrp / xrp_per_grc).quantize(Decimal(1).scaleb(-GRC_DECIMALS), rounding=ROUND_DOWN)

# Chains whose daemon must NOT be mainnet. Gridcoin reports its network in
# getblockchaininfo.chain on a modern build and getinfo.testnet on an old one;
# both are read, and anything that is not one of these aborts.
GRC_TEST_NETWORKS = frozenset({"test", "testnet", "regtest"})

# Two XRP accounts: one funds the escrow, one receives it. Named so the
# assertion in step 2 and the sentence explaining it cannot disagree about the
# number, the same reason xrp_htlc_escrow.ACCOUNTS_NEEDED exists.
XRP_ACCOUNTS_NEEDED = 2

# How long step 8 keeps trying to read the secret off the Gridcoin chain. Sixty
# seconds of polling, because the claim is broadcast and then read within the
# same program -- if it is not readable in a minute the routes are wrong, not
# slow, and the reasons printed each attempt say which.
READ_ATTEMPTS = 30
READ_POLL_SECONDS = 2.0


# WHICH CHAIN THE INITIATOR IS ON. The initiator picks the secret, funds first,
# and takes the LONGER timelock; the participant funds second, takes the shorter
# one, and claims last using the secret the initiator was forced to publish.
#
# Both directions run the same protocol. What changes is which chain is claimed
# FIRST, and therefore which of the two preimage readers is used:
#
#   XRP_FIRST  the XRP leg is funded first and the GRC leg is CLAIMED first, so
#              the secret appears in a Gridcoin scriptSig and the participant
#              reads it with modules/htlc_spend.preimage_from_scriptsig().
#   GRC_FIRST  the GRC leg is funded first and the XRP leg is CLAIMED first, so
#              the secret appears in an XRPL EscrowFinish's Fulfillment and the
#              participant reads it with
#              chains/xrp_crypto_condition.preimage_from_escrow_finish().
#
# The direction names the INITIATOR's chain rather than "who wants what", because
# the initiator is the role the timelock policy is written against
# (modules/htlc_timelock.py) and naming it after the desire would invert on every
# reading.
XRP_FIRST = "xrp-first"
GRC_FIRST = "grc-first"
DIRECTIONS = (XRP_FIRST, GRC_FIRST)


def swap_timelocks(now_unix: float, grc_tip_height: int, *, hours_scale: float = 1.0,
                   direction: str = XRP_FIRST) -> tuple[int, int, dict]:
    """The two legs' timelocks, in the two chains' own clocks.

    Returns (xrp_cancel_after_ripple_seconds, grc_timeout_height, explanation).

    THE TWO CLOCKS ARE DIFFERENT KINDS and that is the whole difficulty. XRPL's
    CancelAfter is an instant, counted in seconds from 2000-01-01. Gridcoin's
    HTLC timeout is a BLOCK HEIGHT, compared by OP_CHECKLOCKTIMEVERIFY against
    the spending transaction's nLockTime. Converting B's hours into a height
    needs a block interval, and a block interval is an ESTIMATE -- rule 6's
    boundary in reverse: the policy is stated in hours, the chain enforces in
    blocks, and the conversion happens once, here, where it can be read.

    A chain that stalls does not care what its target interval was, which is why
    B's leg is the SHORTER one and B is the party exposed to that estimate being
    wrong. If Gridcoin produces blocks slower than its 90-second target, B's
    height arrives LATER in wall-clock than intended -- closer to A's expiry, not
    past it -- so the estimate erring slow eats B's safety margin rather than
    inverting the order. That is the direction to err in, and it is why this
    derives B's leg from the tip rather than deriving A's from B's.

    `hours_scale` shortens both legs together for a demonstration run. It scales
    both, never one, so the 2:1 ratio the policy asserts is preserved by
    construction -- a flag that could shorten only the initiator's leg would be a
    flag that inverts the ordering, which is the one failure this file exists to
    prevent.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown swap direction {direction!r}; expected one of {DIRECTIONS}")
    initiator_hours = lock_hours_for_role(ROLE_INITIATOR) * hours_scale
    participant_hours = lock_hours_for_role(ROLE_PARTICIPANT) * hours_scale
    # THE HOURS FOLLOW THE ROLE, NOT THE CHAIN. Whichever chain the initiator is
    # on gets the longer lock. Hardcoding 48 to XRP is how the reverse direction
    # would fund two legs whose expiries are the wrong way round -- both
    # transactions succeeding, and the loss arriving when a timelock expires.
    xrp_hours = initiator_hours if direction == XRP_FIRST else participant_hours
    grc_hours = participant_hours if direction == XRP_FIRST else initiator_hours
    xrp_cancel_after = ripple_time(now_unix + xrp_hours * SECONDS_PER_HOUR)
    grc_blocks = int(grc_hours * SECONDS_PER_HOUR // SECONDS_PER_BLOCK["GRC"])
    grc_timeout = grc_tip_height + grc_blocks
    return xrp_cancel_after, grc_timeout, {
        "direction": direction,
        "initiator_hours": initiator_hours,
        "participant_hours": participant_hours,
        "xrp_hours": xrp_hours,
        "grc_hours": grc_hours,
        "grc_blocks": grc_blocks,
        "grc_seconds_per_block": SECONDS_PER_BLOCK["GRC"],
    }


def assert_timelock_ordering(xrp_cancel_after: int, grc_timeout_height: int, grc_tip: int, now_unix: float,
                            *, direction: str = XRP_FIRST) -> str:
    """Refuse to fund anything unless B's leg expires strictly before A's.

    THE ONLY CHECK HERE THAT CAN PREVENT A LOSS, so it runs before either leg is
    funded and it raises rather than warning. If B's leg outlived A's, A could
    claim the GRC in step 4 and refund the XRP the moment A's own lock expired,
    and B would hold an expired claim on an escrow that no longer exists.

    The comparison has to happen in ONE unit, and neither chain's is shared, so
    both are converted to Unix seconds -- B's height through the same estimated
    block interval swap_timelocks() used, which is stated in the returned
    sentence because the reader is entitled to know the comparison rests on an
    estimate rather than on a fact the chain guarantees.

    Returns the sentence to print. Raises SystemExit on a bad ordering, because
    there is no recovery and no argument for continuing.
    """
    xrp_expiry_unix = xrp_cancel_after + RIPPLE_EPOCH_OFFSET_SECONDS
    grc_expiry_unix = now_unix + (grc_timeout_height - grc_tip) * SECONDS_PER_BLOCK["GRC"]
    # THE PARTICIPANT'S LEG IS THE ONE THAT MUST EXPIRE FIRST, whichever chain
    # that is. In XRP_FIRST the initiator is on XRP, so the GRC leg is the
    # participant's; in GRC_FIRST it is the other way round. Asserting "GRC
    # before XRP" unconditionally would pass the reverse direction while the
    # expiries were inverted, which is the one failure this function exists for.
    if direction == XRP_FIRST:
        initiator_expiry, participant_expiry = xrp_expiry_unix, grc_expiry_unix
        initiator_leg, participant_leg = "XRP", "GRC"
    else:
        initiator_expiry, participant_expiry = grc_expiry_unix, xrp_expiry_unix
        initiator_leg, participant_leg = "GRC", "XRP"
    margin_seconds = initiator_expiry - participant_expiry
    if margin_seconds <= 0:
        raise SystemExit(
            f"REFUSED before funding anything: the participant's {participant_leg} leg would expire at or after "
            f"the initiator's {initiator_leg} leg ({participant_leg} in "
            f"{format_duration(participant_expiry - now_unix)}, {initiator_leg} in "
            f"{format_duration(initiator_expiry - now_unix)}). In that order the initiator can take the "
            f"participant's coins and then refund its own, and the participant has no recovery. Nothing was "
            "submitted."
        )
    return (
        f"timelock ordering OK ({direction}): the participant's {participant_leg} leg expires in "
        f"{format_duration(participant_expiry - now_unix)}, the initiator's {initiator_leg} leg in "
        f"{format_duration(initiator_expiry - now_unix)}, a margin of {format_duration(margin_seconds)} in the "
        f"participant's favour. GRC is height {grc_timeout_height} ({grc_timeout_height - grc_tip} blocks at an "
        f"ESTIMATED {SECONDS_PER_BLOCK['GRC']}s, a target interval and not a guarantee); XRP is CancelAfter "
        f"{xrp_cancel_after}."
    )


def grc_network(adapter) -> str:
    """Which Gridcoin network this daemon is on. Two field names, because it moved.

    A modern build answers getblockchaininfo.chain; an older one only has
    getinfo.testnet as a boolean. Both are read and the answer is returned as a
    name, so step 1 compares one string rather than branching on which RPC
    answered. An unreadable network is NOT treated as a test network -- it
    returns "unknown", and step 1 refuses on it (fail closed).
    """
    reasons = []
    for method, field in (("getblockchaininfo", "chain"), ("getinfo", "testnet")):
        try:
            answer = adapter.call(method) or {}
        except Exception as error:  # noqa: BLE001 -- checked: an older daemon does not HAVE getblockchaininfo and answers "Method not found", which is not a failure here but the signal to try the next field. The reason is collected rather than discarded (no bare pass, S110) and returned in the "unknown" string, so an operator sees WHY the network could not be read. A failure of both routes returns "unknown", which step 1 refuses -- fail closed, never "probably testnet".
            reasons.append(f"{method}: {type(error).__name__}")
            continue
        value = answer.get(field)
        if field == "chain" and value:
            return str(value)
        if field == "testnet" and value is not None:
            # getinfo.testnet is a BOOLEAN on an old build. False means mainnet,
            # and "main" is returned rather than "" so the caller compares one
            # vocabulary (rule 11) instead of branching on which RPC answered.
            return "testnet" if value else "main"
        reasons.append(f"{method}: no `{field}` field")
    return f"unknown ({'; '.join(reasons) or 'no route answered'})"


def claim_scriptsig_hex(adapter, txid: str) -> tuple[str, list[str]]:
    """The claim transaction's input scriptSig, by whichever route answers.

    Returns (hex, reasons_tried). An empty hex with reasons is a result, not an
    exception -- the caller is polling and needs to say "not yet" per attempt.

    TWO ROUTES, FOR THE REASON modules/htlc_rpc.lookup_contract_output() HAS
    FOUR. `getrawtransaction` searches only the MEMPOOL unless the daemon runs
    -txindex, which is the exact defect that killed the BTC redeem path on
    2026-09-25 and cost a whole run to diagnose. Right after a broadcast the
    claim is in the mempool and route 1 answers; once it is mined it may not be
    findable that way at all, and this is the ONE step where failing is worst --
    both legs are funded and the secret is already public, so a participant who
    cannot read it has published nothing and lost the race to a timeout.

    Route 2 is the wallet: `gettransaction` returns the raw hex for any
    transaction the wallet knows, mined or not, with no -txindex, and
    `decoderawtransaction` turns it into the same shape. It works here because
    the claim was made by this wallet. A REAL participant is not the claimer and
    would not have it in their wallet -- for them route 1 plus -txindex, or a
    block scan, is the answer, and that is named here rather than discovered
    later.
    """
    reasons: list[str] = []
    try:
        raw = adapter.call("getrawtransaction", txid, 1) or {}
        script_sig = ((raw.get("vin") or [{}])[0].get("scriptSig") or {}).get("hex", "")
        if script_sig:
            return script_sig, reasons
        reasons.append("getrawtransaction: answered with no vin[0].scriptSig.hex")
    except Exception as error:  # noqa: BLE001 -- checked: the daemon answers "No information available about transaction" without -txindex once the claim is mined, which is not a failure but the signal to try the wallet. The reason is kept and printed rather than discarded, and a failure of BOTH routes returns "" which the caller reports as a FAIL -- never as "no preimage was revealed".
        reasons.append(f"getrawtransaction: {type(error).__name__}")
    try:
        wallet_tx = adapter.call("gettransaction", txid) or {}
        raw_hex = wallet_tx.get("hex")
        if not raw_hex:
            reasons.append("gettransaction: answered with no `hex`")
            return "", reasons
        decoded = adapter.call("decoderawtransaction", raw_hex) or {}
        script_sig = ((decoded.get("vin") or [{}])[0].get("scriptSig") or {}).get("hex", "")
        if script_sig:
            return script_sig, reasons
        reasons.append("decoderawtransaction: no vin[0].scriptSig.hex")
    except Exception as error:  # noqa: BLE001 -- checked: same, and this is the last route. Returning "" is reported by the caller as a failure to READ, which is a different thing from reading successfully and finding no preimage -- the caller prints the reasons so an operator can tell them apart.
        reasons.append(f"gettransaction/decoderawtransaction: {type(error).__name__}")
    return "", reasons


def htlc_vout(adapter, txid: str, p2sh_script_hex: str) -> tuple[int | None, str]:
    """Which output of the funding transaction IS the HTLC. Never assumed.

    Returns (vout, explanation). A None vout with an explanation is a result the
    caller reports; it is never defaulted to 0.

    WHY THIS EXISTS, and it is the same defect this repository fixed on the
    Bitcoin side on 2026-09-25. Gridcoin's `createhtlc` returns p2sh_address,
    redeem_script, sender_pubkey, receiver_pubkey, hash, timeout and txid -- and
    NO VOUT (read from src/rpc/htlc.cpp, 2026-09-26). It funds through
    SendMoney(), which adds a CHANGE output, so the HTLC is at index 0 or 1
    depending on coin selection. The first version of this file passed
    `int(htlc.get("vout", 0))` to claimhtlc, which is a guess about which output
    holds a real balance.

    MATCHED ON THE scriptPubKey HEX, not on a rendered address. That is the other
    half of the same 2026-09-25 lesson: `scriptPubKey.addresses` was removed in
    Bitcoin Core 22.0 and daemons disagree about whether it exists, while the hex
    is the same bytes everywhere. The hex here is derived from the redeem script
    the daemon itself returned, so a mismatch means the funding transaction does
    not pay the contract the daemon just described -- which is a refusal, not an
    index to fall back on.
    """
    reasons: list[str] = []
    for method, args in (("getrawtransaction", (txid, 1)), ("gettransaction", (txid,))):
        try:
            answer = adapter.call(method, *args) or {}
        except Exception as error:  # noqa: BLE001 -- checked: getrawtransaction answers "No information available about transaction" without -txindex once mined, which is the signal to try the wallet route, not a failure. Reasons are collected and returned rather than discarded, and a failure of both yields a None vout that the caller reports as a FAIL -- never a vout of 0.
            reasons.append(f"{method}: {type(error).__name__}")
            continue
        outputs = answer.get("vout")
        if outputs is None and answer.get("hex"):
            try:
                outputs = (adapter.call("decoderawtransaction", answer["hex"]) or {}).get("vout")
            except Exception as error:  # noqa: BLE001 -- checked: same; the wallet gave hex and the decode is the only step left. A failure is collected, not swallowed.
                reasons.append(f"decoderawtransaction: {type(error).__name__}")
                continue
        for entry in outputs or []:
            if ((entry.get("scriptPubKey") or {}).get("hex", "")).lower() == p2sh_script_hex.lower():
                return int(entry.get("n", -1)), f"matched scriptPubKey {p2sh_script_hex} via {method}"
        reasons.append(f"{method}: read {len(outputs or [])} outputs, none paying {p2sh_script_hex}")
    return None, "; ".join(reasons) or "no route answered"


def _pinned_grc_amount(console: Console, raw: str) -> tuple[Decimal | None, str]:
    """--grc-amount, validated. Its own function so resolve_grc_amount() stays
    under the return ceiling by SHAPE rather than by a suppression (rule 19)."""
    try:
        pinned = Decimal(raw)
    except (ArithmeticError, ValueError):
        console.check("the GRC leg's size", f"--grc-amount {raw!r} is not a number", "a decimal", False)
        return None, ""
    if pinned <= 0:
        console.check("the GRC leg's size", f"--grc-amount {pinned}", "a positive amount", False)
        return None, ""
    return pinned, "--grc-amount, pinned by hand; NO rate was applied and the legs are not priced"


def _rated_grc_amount(console: Console, raw: str) -> tuple[Decimal | None, str]:
    """--rate, validated and applied. See grc_amount_for_rate for the direction."""
    try:
        rate = Decimal(raw)
        amount = grc_amount_for_rate(XRP_DROPS, rate)
    except (ArithmeticError, ValueError) as error:
        console.check("the GRC leg's size", f"--rate {raw!r}: {error}", "a positive rate", False)
        return None, ""
    return amount, f"--rate {rate} XRP per GRC, supplied by hand and NOT checked against a market"


def resolve_grc_amount(console: Console, args) -> tuple[Decimal | None, str]:
    """How much GRC the swap moves, and WHERE that number came from.

    Returns (amount, source_sentence). A None amount means the run must stop, and
    the console already carries the failure.

    THREE SOURCES, IN THIS ORDER, and the source is always printed:

      --grc-amount   pinned outright, no pricing. For a run where the operator
                     wants a specific size.
      --rate         XRP per GRC, supplied by hand. For a run where CoinGecko is
                     unreachable, or where a particular rate is being tested.
      the live price  services/pricing.py, the SAME table the custodial quote path
                     uses. Not a second price source written for this file (rule
                     8) -- if the two ever disagreed, a swap and a quote for the
                     same pair would price differently and nothing would say so.

    IT REFUSES RATHER THAN FALLING BACK when the price cannot be fetched. A
    silent default to 1:1 is how a swap comes to move a thousand dollars of one
    thing for a dollar of another: the run would look identical, and on testnet it
    LOOKS fine, which is exactly why it must not be the behavior. The refusal
    names --rate as the way through.
    """
    # Each source is its own function, so this reads as the ORDER of three
    # sources and the return count is structural rather than suppressed (rule 19).
    if args.grc_amount:
        return _pinned_grc_amount(console, args.grc_amount)
    if args.rate:
        return _rated_grc_amount(console, args.rate)

    # LAZY, and PLC0415 is suppressed for one checked reason written here: importing
    # services.pricing at module scope would make --help reach for `requests` and a
    # config import on a machine with neither, and the dry run should be able to
    # describe a swap without a price.
    from services.pricing import IDS, derive_pair_rate, fetch_usd_prices  # noqa: PLC0415
    if "GRC" not in IDS or "XRP" not in IDS:
        console.check("the GRC leg's size", f"services/pricing.IDS covers {sorted(IDS)}",
                      "both GRC and XRP priced", False)
        return None, ""
    try:
        prices = fetch_usd_prices()
        # derive_pair_rate("GRC", "XRP") is GRC_USD / XRP_USD = how many XRP one
        # GRC costs, which is the direction grc_amount_for_rate divides by. Getting
        # this backwards makes the swap off by the square of the price, and on
        # testnet that looks like a large number and nothing else.
        rate = Decimal(str(derive_pair_rate("GRC", "XRP", prices)))
        amount = grc_amount_for_rate(XRP_DROPS, rate)
    except Exception as error:  # noqa: BLE001 -- checked: fetch_usd_prices can fail on the network, on a non-200, on a partial response (it raises KeyError naming the missing asset), or on a rate of zero. EVERY one of those must stop the run rather than fall back to an unpriced amount, and the message names --rate as the way through. Nothing here treats a failure as a price.
        console.check("the GRC leg's size", f"{type(error).__name__}: {error}", "a live XRP/GRC rate", False)
        console.say("NOTHING WAS SUBMITTED. A swap will not be priced 1:1 by default -- that is how a thousand "
                    "dollars of one thing moves for a dollar of another, and on testnet it looks fine. Pass "
                    "--rate <XRP per GRC> to price it by hand, or --grc-amount to pin the size outright.")
        return None, ""
    return amount, (
        f"services/pricing.py (CoinGecko): GRC ${prices['GRC_USD']}, XRP ${prices['XRP_USD']}, so "
        f"{rate:.8f} XRP per GRC. {XRP_DROPS} drops buys {amount} GRC, rounded DOWN to "
        f"{GRC_DECIMALS} places in the GRC holder's favour"
    )


@dataclass
class SwapContext:
    """Everything steps 5 to 9 need, on either chain, in either direction.

    A context object rather than fifteen parameters, and for the reason
    regtest/steps.py's Run gives: a stage that takes (console, adapters, secret,
    hash, condition, four addresses, two timelocks, a passphrase, a submitter) is
    orchestration carrying its context by hand, and CLAUDE.md rule 12 says the
    fix for an argument-count finding is to extract, never to suppress the count.

    The two direction runners take exactly this and nothing else, which is what
    makes them comparable side by side -- the only thing that differs between
    them is the ORDER of the same five acts and which preimage reader is used.
    """

    console: Console
    grc: object
    submit_xrp: object
    secret: bytes
    secret_hash: bytes
    condition: str
    a_xrp: str
    a_xrp_secret: str
    b_xrp: str
    b_xrp_secret: str
    a_grc: str
    b_grc: str
    grc_amount: Decimal
    grc_timeout: int
    xrp_cancel_after: int
    passphrase: str


def run_xrp_first(ctx: SwapContext) -> bool:  # noqa: C901, PLR0912, PLR0915 -- checked: this is the protocol's ORDER, five acts across two chains, and every decision inside it is extracted (the timelocks above, the preimage read in modules/htlc_spend, the payloads in xrp_htlc_escrow, the vout lookup in htlc_vout). Rule 10 puts the order in the file named after the thing being done; splitting it would hide the sequence, and the sequence IS the security property.
    """A funds XRP first, B funds GRC, A claims GRC, B reads the scriptSig, B claims XRP.

    THE DIRECTION THAT RAN ON 2026-09-26 (OK=15) -- see the module header for the
    txids. Returns True when the swap completed.
    """
    ctx.console.step(6, f"A funds the XRP leg: {XRP_DROPS} drops to B, hashlocked and timelocked")
    b_before = balance_drops(ctx.b_xrp)
    created = ctx.submit_xrp(escrow_create_tx(ctx.a_xrp, ctx.b_xrp, XRP_DROPS, ctx.condition,
                                          ctx.xrp_cancel_after), ctx.a_xrp_secret)
    if not ctx.console.check("XRP leg funded", describe_result(created), "tesSUCCESS",
                         engine_result(created) == "tesSUCCESS"):
        return False
    escrow_sequence = (created.get("tx_json") or {}).get("Sequence")
    wait_validated(ctx.console, (created.get("tx_json") or {}).get("hash", ""))
    ctx.console.say(f"OfferSequence={escrow_sequence} -- how the finish in step 9 names this escrow")

    # LAZY, and PLC0415 is suppressed for one checked reason written here rather
    # than on the line: the dry run must not touch the unlock path at all, and a
    # module-scope import would run gridcoin_wallet_lock's environment read on
    # every invocation including --help. The reason lives above the import
    # because the sorter re-wraps a long trailing comment and detaches it from
    # the line it is about, which is how a suppression's justification drifts.
    from chains.gridcoin_wallet_lock import unlocked_for_payout  # noqa: PLC0415
    from modules.atomic_htlc_scripts import p2sh_script_for  # noqa: PLC0415 -- checked: only the --run path needs it

    ctx.console.step(7, f"B funds the GRC leg: {ctx.grc_amount} GRC, same hash, expiring FIRST")
    ctx.console.say("BOTH createhtlc AND claimhtlc need the wallet FULLY unlocked -- Gridcoin's htlc.cpp calls "
                "EnsureWalletIsUnlocked() in each, and createhtlc also SENDS. So steps 6 and 7 run inside ONE "
                "unlock, which walletlocks first and therefore clears a staking-only unlock (rpc -13, measured "
                "on the operator's wallet 2026-09-26: `Wallet is unlocked for staking only.`).")
    htlc = None
    claim_txid = None
    grc_vout = None
    try:
        with unlocked_for_payout(ctx.grc, ctx.passphrase):
            htlc = ctx.grc.call("createhtlc", ctx.a_grc, ctx.b_grc, ctx.secret_hash.hex(), ctx.grc_timeout, float(ctx.grc_amount))
            funding_txid = htlc.get("txid")
            # THE KEYS ARE snake_case, read from src/rpc/htlc.cpp rather than
            # guessed: p2sh_address, redeem_script, sender_pubkey,
            # receiver_pubkey, hash, timeout, txid. An earlier version read
            # `address` and `redeemScript` and printed p2sh=None on a successful
            # call, which is rule 14's defect -- an instrument reporting less
            # than the run established.
            p2sh_address = htlc.get("p2sh_address")
            redeem_script_hex = htlc.get("redeem_script")
            ctx.console.check("GRC leg funded", f"p2sh={p2sh_address} txid={funding_txid}", "a funded HTLC",
                          bool(funding_txid))
            ctx.console.say(f"GRC redeem script={redeem_script_hex}")
            if not funding_txid or not redeem_script_hex:
                raise RuntimeError(
                    f"createhtlc answered without a txid or a redeem_script (keys: {sorted(htlc)}). Nothing "
                    "can be claimed from that, and nothing was."
                )
            expected_script = p2sh_script_for(bytes.fromhex(redeem_script_hex)).hex()
            grc_vout, how = htlc_vout(ctx.grc, funding_txid, expected_script)
            ctx.console.check("the HTLC's output index, located not assumed", grc_vout, "an output paying "
                          f"{expected_script}", grc_vout is not None)
            ctx.console.say(f"vout lookup: {how}")
            if grc_vout is None:
                raise RuntimeError(
                    "the funding transaction's HTLC output could not be located, and claiming a GUESSED index "
                    "would spend whichever output happened to be there -- createhtlc funds through SendMoney, "
                    "which adds a change output, so index 0 is as likely to be the change. Nothing was claimed."
                )

            ctx.console.step(8, "A claims the GRC with the secret -- which PUBLISHES it")
            ctx.console.say("this is the irreversible step for A: claiming requires pushing the secret into a "
                        "scriptSig that lands in a block. A cannot take the GRC without giving B what B needs.")
            claim = ctx.grc.call("claimhtlc", funding_txid, grc_vout, ctx.secret.hex(), ctx.a_grc)
            claim_txid = claim.get("txid") if isinstance(claim, dict) else str(claim)
            ctx.console.check("A claimed the GRC", f"txid={claim_txid}", "a broadcast txid", bool(claim_txid))
    except Exception as error:  # noqa: BLE001 -- checked: createhtlc and claimhtlc each refuse for several named reasons (a staking-only or locked wallet, a pubkey not in the wallet, insufficient funds, a wrong preimage, a script failure) and the unlock/restore can fail on its own. It is reported rather than raised because the XRP leg is ALREADY FUNDED here, and which recovery line applies depends on how far the block got -- an operator needs that sentence, not a traceback. The unlock context restores the wallet on the way out regardless.
        ctx.console.check("the GRC leg", f"{type(error).__name__}: {error}",
                      "a funded HTLC, its output located, and a claim", False)
        if claim_txid:
            ctx.console.say(f"the claim went out as {claim_txid} -- the secret IS public. B must finish the escrow "
                        f"with it; read the secret out of that transaction. Do not let the escrow expire.")
        elif htlc and htlc.get("txid"):
            ctx.console.say(f"BOTH LEGS ARE FUNDED AND NEITHER IS CLAIMED. The secret has NOT been published, so "
                        f"nobody can finish the escrow: B recovers the GRC at height {ctx.grc_timeout} and A "
                        f"recovers the XRP at CancelAfter {ctx.xrp_cancel_after}. Do NOT publish the secret.")
        else:
            ctx.console.say(f"THE XRP LEG IS FUNDED AND THE GRC LEG IS NOT. Nothing is lost: nobody has the ctx.secret, "
                        f"so nobody can finish the escrow, and it returns to A at CancelAfter "
                        f"{ctx.xrp_cancel_after}. Do NOT publish the secret.")
        return False

    ctx.console.step(9, "B reads the secret OFF THE GRIDCOIN CHAIN -- never from A")
    ctx.console.say("this is the step that makes the swap atomic. B does not ask A for anything, and A cannot "
                "refuse: the secret is in A's own claim transaction.")
    revealed = None
    for attempt in range(1, READ_ATTEMPTS + 1):
        script_sig_hex, reasons = claim_scriptsig_hex(ctx.grc, claim_txid)
        if script_sig_hex:
            revealed = preimage_from_scriptsig(bytes.fromhex(script_sig_hex), ctx.secret_hash)
            if revealed is not None:
                ctx.console.say(f"attempt {attempt}: read the claim's scriptSig ({len(script_sig_hex) // 2} bytes) and "
                            f"one of its pushes hashes to the commitment")
                break
            # READ BUT NO MATCH is a different answer from COULD NOT READ, and
            # the two must not print the same line (rule 14). This one means the
            # transaction is there and does not carry the preimage.
            ctx.console.say(f"attempt {attempt}: read the scriptSig, but NO push hashes to the commitment -- this is "
                        f"not a claim of this contract")
        else:
            ctx.console.say(f"attempt {attempt}: could not read the claim yet ({'; '.join(reasons) or '(none)'})")
        time.sleep(READ_POLL_SECONDS)
    if not ctx.console.check("the secret was recovered from the chain", "yes" if revealed else None,
                         "a push whose sha256 matches the commitment", revealed is not None):
        ctx.console.say(f"B cannot finish the escrow without it and recovers the GRC at height {ctx.grc_timeout}... "
                    f"except that A HAS ALREADY CLAIMED the GRC. Read {claim_txid} by hand; the secret is in it.")
        return False
    # THE ASSERTION THAT THE READ IS REAL. `revealed` came from the chain and
    # `ctx.secret` from memory, and they must be equal -- if this file ever finished
    # the escrow using `ctx.secret` directly it would still WORK here, while proving
    # nothing about atomicity, because a real B has no `ctx.secret` variable.
    ctx.console.check("what the chain gave B equals what A committed to", revealed == ctx.secret, "True", revealed == ctx.secret)

    ctx.console.step(10, "B finishes the XRP escrow with the secret it read")
    fulfillment = preimage_fulfillment(revealed)
    fee = finish_fee_drops(fulfillment)
    finished = ctx.submit_xrp(escrow_finish_tx(ctx.a_xrp, ctx.a_xrp, escrow_sequence,
                                              condition=ctx.condition, fulfillment=fulfillment, fee=fee),
                              ctx.a_xrp_secret)
    if ctx.console.check("XRP leg claimed", describe_result(finished), "tesSUCCESS",
                     engine_result(finished) == "tesSUCCESS"):
        wait_validated(ctx.console, (finished.get("tx_json") or {}).get("hash", ""))
        b_after = balance_drops(ctx.b_xrp)
        # THE BALANCES, not the engine results. Two tesSUCCESS codes say two
        # transactions applied; the balances say the swap happened.
        ctx.console.check("B's XRP balance rose by the escrowed amount",
                      f"{b_before} -> {b_after} drops (+{b_after - b_before})", f"+{XRP_DROPS}",
                      b_after - b_before == XRP_DROPS)

    ctx.console.banner("WHAT CHANGED HANDS")
    ctx.console.say(f"GRC: {ctx.grc_amount} from B's wallet to {ctx.a_grc}, claimed with the secret (txid {claim_txid})")
    ctx.console.say(f"XRP: {XRP_DROPS} drops from {ctx.a_xrp} to {ctx.b_xrp}, released by the same secret")
    ctx.console.say("interlocked by one sha256, with neither party ever sending the other the preimage.")
    return False
    return True


def run_grc_first(ctx: SwapContext) -> bool:  # noqa: C901, PLR0915 -- checked: same as run_xrp_first, and the two are deliberately parallel so a reader can diff them. The decisions are extracted; what is left is the order.
    """B funds GRC first, A funds XRP, B claims XRP, A reads the Fulfillment, A claims GRC.

    THE REVERSE DIRECTION, and it is not a mirror of the other one for free. Two
    things genuinely change:

      WHICH LEG IS CLAIMED FIRST. Here the initiator is on Gridcoin, so the
      initiator claims the XRP escrow -- and an EscrowFinish carries the secret
      in its `Fulfillment` field rather than in a scriptSig. The participant
      therefore reads it with
      chains/xrp_crypto_condition.preimage_from_escrow_finish(), the mirror of
      modules/htlc_spend.preimage_from_scriptsig(). Without that reader this
      direction cannot exist, which is why it was written before this function.

      WHO SIGNS WHAT ON XRPL. The escrow is CREATED by the XRP holder and
      FINISHED by the GRC holder, so two different XRP secrets are used in one
      run -- unlike the other direction, where one account both created and
      finished. An EscrowFinish may be submitted by anyone, but submitting it as
      the claimer is what makes the roles legible on the ledger.

    The GRC wallet is unlocked TWICE here, in two separate windows (createhtlc at
    step 5, claimhtlc at step 9), because XRPL calls happen in between. The other
    direction unlocks once because its two Gridcoin calls are adjacent.

    Returns True when the swap completed.
    """
    console, grc = ctx.console, ctx.grc
    # LAZY for the reason the other runner documents: the dry run must not touch
    # the unlock path, and a module-scope import would read the environment on
    # every invocation including --help.
    from chains.gridcoin_wallet_lock import unlocked_for_payout  # noqa: PLC0415
    from modules.atomic_htlc_scripts import p2sh_script_for  # noqa: PLC0415

    console.step(6, f"B funds the GRC leg FIRST: {ctx.grc_amount} GRC, hashlocked, expiring LAST")
    console.say("the initiator funds first and takes the LONGER lock. Here that is the Gridcoin side, so the "
                "GRC timeout is the one that outlives the XRP escrow -- the reverse of the other direction.")
    funding_txid = None
    grc_vout = None
    expected_script = ""
    try:
        with unlocked_for_payout(grc, ctx.passphrase):
            htlc = grc.call("createhtlc", ctx.a_grc, ctx.b_grc, ctx.secret_hash.hex(),
                            ctx.grc_timeout, float(ctx.grc_amount))
            funding_txid = htlc.get("txid")
            redeem_script_hex = htlc.get("redeem_script")
            console.check("GRC leg funded", f"p2sh={htlc.get('p2sh_address')} txid={funding_txid}",
                          "a funded HTLC", bool(funding_txid))
            console.say(f"GRC redeem script={redeem_script_hex}")
            if not funding_txid or not redeem_script_hex:
                raise RuntimeError(
                    f"createhtlc answered without a txid or a redeem_script (keys: {sorted(htlc)}). Nothing was "
                    "funded on the XRP side yet, so nothing is at risk."
                )
            expected_script = p2sh_script_for(bytes.fromhex(redeem_script_hex)).hex()
            grc_vout, how = htlc_vout(grc, funding_txid, expected_script)
            console.check("the HTLC's output index, located not assumed", grc_vout,
                          f"an output paying {expected_script}", grc_vout is not None)
            console.say(f"vout lookup: {how}")
            if grc_vout is None:
                raise RuntimeError(
                    "the HTLC output could not be located, and a guessed index would spend whichever output "
                    "happened to be there -- createhtlc funds through SendMoney, which adds change. Nothing "
                    "else was submitted."
                )
    except Exception as error:  # noqa: BLE001 -- checked: createhtlc and the unlock each refuse for named reasons and the message says which. Reported rather than raised because this is the FIRST leg: nothing else is funded, so the recovery line is short and the operator needs it rather than a traceback. The unlock context restores the wallet on the way out.
        console.check("the GRC leg", f"{type(error).__name__}: {error}", "a funded HTLC with its output located",
                      False)
        console.say("NOTHING ELSE WAS SUBMITTED. If the createhtlc transaction did go out, B recovers the GRC at "
                    f"height {ctx.grc_timeout}; no XRP was escrowed and the secret was never published.")
        return False

    console.step(7, f"A funds the XRP leg: {XRP_DROPS} drops to B's counterparty, expiring FIRST")
    a_before = balance_drops(ctx.a_xrp)
    created = ctx.submit_xrp(escrow_create_tx(ctx.b_xrp, ctx.a_xrp, XRP_DROPS, ctx.condition,
                                              ctx.xrp_cancel_after), ctx.b_xrp_secret)
    if not console.check("XRP leg funded", describe_result(created), "tesSUCCESS",
                         engine_result(created) == "tesSUCCESS"):
        console.say(f"the GRC leg IS funded ({funding_txid}) and the XRP leg is not. Nobody has the secret, so "
                    f"nobody can claim the GRC: it returns to B at height {ctx.grc_timeout}. Do NOT publish the "
                    f"secret.")
        return False
    escrow_sequence = (created.get("tx_json") or {}).get("Sequence")
    escrow_owner = ctx.b_xrp
    wait_validated(console, (created.get("tx_json") or {}).get("hash", ""))
    console.say(f"OfferSequence={escrow_sequence}, Owner={escrow_owner} -- how the finish below names this escrow")

    console.step(8, "A claims the XRP with the secret -- which PUBLISHES it in the Fulfillment")
    console.say("the irreversible step for the initiator: an EscrowFinish carries the fulfillment, and the "
                "fulfillment contains the preimage. A cannot take the XRP without giving B what B needs.")
    fulfillment = preimage_fulfillment(ctx.secret)
    fee = finish_fee_drops(fulfillment)
    # SUBMITTED AS THE CLAIMER, with Owner still the account that created the
    # escrow. Anyone may submit an EscrowFinish; doing it as A is what makes the
    # roles readable on the ledger, and it is the only place in this file where
    # two different XRP secrets are used in one run.
    finished = ctx.submit_xrp(escrow_finish_tx(ctx.a_xrp, escrow_owner, escrow_sequence,
                                               condition=ctx.condition, fulfillment=fulfillment, fee=fee),
                              ctx.a_xrp_secret)
    if not console.check("A claimed the XRP", describe_result(finished), "tesSUCCESS",
                         engine_result(finished) == "tesSUCCESS"):
        console.say("BOTH LEGS ARE FUNDED AND NEITHER IS CLAIMED. The secret was NOT published, so nobody can "
                    f"claim either: A recovers the XRP at CancelAfter {ctx.xrp_cancel_after} and B recovers the "
                    f"GRC at height {ctx.grc_timeout}. Do NOT publish the secret.")
        return False
    finish_hash = (finished.get("tx_json") or {}).get("hash", "")
    validated = wait_validated(console, finish_hash)
    a_after = balance_drops(ctx.a_xrp)
    # THE CLAIMER PAYS THE FINISH FEE, AND HERE THE CLAIMER IS THE DESTINATION,
    # so the fee comes out of the same account the escrow pays into and the net
    # rise is the escrowed amount MINUS the fee. Measured on the first grc-first
    # run, 2026-09-26: 81998800 -> 82998440, +999640 against a 1000000 escrow and
    # a 360-drop fee, and the assertion said FAIL on a swap that had completed
    # correctly.
    #
    # The other direction nets to exactly the escrowed amount because the
    # CREATOR submits the finish there, so the fee leaves a different account
    # from the one being credited. Asserting `+XRP_DROPS` in both places was one
    # expectation covering two different arithmetics -- and the wrong half of it
    # reported a successful swap as a failure, which is the instrument losing its
    # reader (rule 14). It is written as an exact figure rather than loosened to
    # `>=`, because a fee is a known number and a range would also pass a swap
    # that paid the wrong amount.
    expected_rise = XRP_DROPS - fee
    console.check("A's XRP balance rose by the escrowed amount less the finish fee A itself paid",
                  f"{a_before} -> {a_after} drops (+{a_after - a_before})",
                  f"+{expected_rise} = {XRP_DROPS} escrowed - {fee} fee",
                  a_after - a_before == expected_rise)

    console.step(9, "B reads the secret OFF THE XRP LEDGER -- out of A's own EscrowFinish")
    console.say("B does not ask A for anything. The fulfillment is a field of the transaction A just submitted, "
                "and it is public the moment that transaction validates.")
    revealed = None
    for attempt in range(1, READ_ATTEMPTS + 1):
        source = validated if attempt == 1 and validated else None
        if source is None:
            try:
                source = rpc("tx", {"transaction": finish_hash})
            except Exception as error:  # noqa: BLE001 -- checked: a `tx` lookup can fail transiently right after submission, and a poll that died on the first miss would fail a swap proceeding correctly. The reason is printed each attempt and the failure below is the loop running out.
                console.say(f"attempt {attempt}: could not read the finish yet ({type(error).__name__})")
                time.sleep(READ_POLL_SECONDS)
                continue
        revealed = preimage_from_escrow_finish(source, ctx.secret_hash)
        if revealed is not None:
            console.say(f"attempt {attempt}: read the Fulfillment off the ledger and it hashes to the commitment")
            break
        # READ BUT NO MATCH is a different answer from COULD NOT READ (rule 14).
        console.say(f"attempt {attempt}: read the transaction, but its Fulfillment does not hash to the "
                    f"commitment -- this is not a finish of this escrow")
        time.sleep(READ_POLL_SECONDS)
    if not console.check("the secret was recovered from the XRP ledger", "yes" if revealed else None,
                         "a Fulfillment whose sha256 matches the commitment", revealed is not None):
        console.say(f"B cannot claim the GRC without it and the GRC returns to B at height {ctx.grc_timeout} -- "
                    f"except that A HAS ALREADY TAKEN THE XRP. Read {finish_hash} by hand; the secret is in its "
                    f"Fulfillment field.")
        return False
    # The same assertion the other direction makes: `revealed` came from the
    # ledger and `secret` from memory. A version that claimed with `secret`
    # directly would work here and prove nothing, because a real B has no such
    # variable.
    console.check("what the ledger gave B equals what A committed to", revealed == ctx.secret, "True",
                  revealed == ctx.secret)

    console.step(10, "B claims the GRC with the secret it read")
    claim_txid = None
    try:
        with unlocked_for_payout(grc, ctx.passphrase):
            claim = grc.call("claimhtlc", funding_txid, grc_vout, revealed.hex(), ctx.a_grc)
            claim_txid = claim.get("txid") if isinstance(claim, dict) else str(claim)
    except Exception as error:  # noqa: BLE001 -- checked: claimhtlc refuses on a wrong preimage, a missing key or a script failure, and the unlock can fail separately. Reported because A already has the XRP at this point, so the operator needs to know the GRC is still claimable with a secret that is now public rather than getting a traceback.
        console.check("B claimed the GRC", f"{type(error).__name__}: {error}", "a broadcast txid", False)
        console.say(f"A HAS THE XRP AND B HAS NOT CLAIMED THE GRC. The secret is PUBLIC (in {finish_hash}), so "
                    f"the claim can be retried by hand before height {ctx.grc_timeout}, after which the GRC "
                    f"returns to B anyway.")
        return False
    console.check("B claimed the GRC", f"txid={claim_txid}", "a broadcast txid", bool(claim_txid))

    console.banner("WHAT CHANGED HANDS")
    console.say(f"XRP: {XRP_DROPS} drops from {ctx.b_xrp} to {ctx.a_xrp}, claimed with the secret ({finish_hash})")
    console.say(f"GRC: {ctx.grc_amount} from B's wallet to {ctx.a_grc}, released by the same secret (txid {claim_txid})")
    console.say("interlocked by one sha256, with neither party ever sending the other the preimage.")
    return True


def main() -> int:  # noqa: C901, PLR0911, PLR0915 -- checked: this is the swap's SEQUENCE, and every decision in it is extracted -- the timelocks and their ordering above, the preimage read in modules/htlc_spend, the condition in chains/xrp_crypto_condition, the payloads in xrp_htlc_escrow. What is left is the order of five acts on two chains, which is what rule 10 says a file at the root is for. Splitting it would put the order somewhere other than the file named after the thing being done, and the order IS the protocol.
    parser = argparse.ArgumentParser(
        description="A real atomic swap: XRP on the XRPL testnet against GRC on the Gridcoin testnet, "
                    "interlocked by one sha256 preimage. Testnet only, structurally.",
    )
    parser.add_argument("--run", action="store_true",
                        help="actually submit. Without it every step is described and nothing is sent")
    parser.add_argument("--direction", choices=DIRECTIONS, default=XRP_FIRST,
                        help=f"which chain the INITIATOR is on: {XRP_FIRST} (XRP funded first, GRC claimed "
                             f"first, secret read from a Gridcoin scriptSig) or {GRC_FIRST} (GRC funded first, "
                             f"XRP claimed first, secret read from an XRPL Fulfillment). The initiator always "
                             f"takes the longer lock")
    parser.add_argument("--rate", type=str, default="",
                        help="XRP per GRC, overriding the live price. Use it when CoinGecko is unreachable or "
                             "when a specific figure is wanted; the run prints which source was used")
    parser.add_argument("--grc-amount", type=str, default="",
                        help="pin the GRC leg outright and skip pricing entirely. Mutually exclusive in effect "
                             "with --rate, which sizes it instead")
    parser.add_argument("--hours-scale", type=float, default=1.0,
                        help="shorten BOTH legs by this factor for a demonstration (default 1.0 = the real "
                             "48h/24h policy). It scales both, so the 2:1 ordering is preserved")
    args = parser.parse_args()

    console = Console(total_steps=10)
    console.banner("ATOMIC SWAP -- XRP (XRPL testnet) for GRC (Gridcoin testnet)")
    console.say(f"XRP endpoint={TESTNET_URL}")
    console.say(f"mode={'--run: BOTH LEGS WILL BE FUNDED' if args.run else 'DRY RUN: nothing is submitted'}")
    console.say("A holds XRP and wants GRC (the INITIATOR, longer lock). B holds GRC and wants XRP (the "
                "PARTICIPANT, shorter lock).")
    console.say("one operator plays both parties here, so counterparty misbehavior is NOT exercised -- the "
                "mechanism is, on real chains. See this file's header.")
    console.say("BOTH directions have completed at the market rate, OK=16 FAIL=0 each (2026-09-27): xrp-first "
                "GRC claim 78472df347a91f31..., grc-first GRC claim 845315f4c2063670..., 1 XRP for "
                "66.10250498 GRC both ways. A failure here is a regression, not a discovery.")

    console.step(1, "both networks are TEST networks, and each says which")
    try:
        console.check("XRP network", refuse_mainnet(), "a non-mainnet network_id", True)
    except Exception as error:  # noqa: BLE001 -- checked: refuse_mainnet raises RuntimeError on a mainnet answer and requests raises a connection error when the endpoint is unreachable, and BOTH must arrive as a labeled FAIL rather than a traceback at step 1 of nine (rule 14). Nothing treats the failure as a pass; it returns non-zero through the summary.
        console.check("XRP network", f"{type(error).__name__}: {error}", "a non-mainnet network_id", False)
        return console.summary()

    adapters = build_adapters(Config.RPC)
    grc = adapters.get("GRC")
    if not console.check("GRC adapter configured", "yes" if grc else None, "GRC_RPC_* set in the environment",
                         grc is not None):
        console.say("chains/registry.why_unconfigured('GRC') names the missing variable. Nothing was submitted.")
        return console.summary()
    network = grc_network(grc)
    if not console.check("GRC network", network, f"one of {sorted(GRC_TEST_NETWORKS)}", network in GRC_TEST_NETWORKS):
        console.say("REFUSED: this daemon is not on a test network, or would not say. Nothing was submitted.")
        return console.summary()

    console.step(2, "the four accounts -- two on each chain")
    accounts = saved_faucet_accounts()
    if not console.check("XRP faucet accounts", len(accounts), f">= {XRP_ACCOUNTS_NEEDED}", len(accounts) >= XRP_ACCOUNTS_NEEDED):
        return console.summary()
    (_, a_xrp, a_xrp_secret), (_, b_xrp, b_xrp_secret) = accounts[0], accounts[1]
    console.say(f"A (initiator, pays XRP) = {a_xrp}")
    console.say(f"B (participant, receives XRP) = {b_xrp}")
    try:
        a_grc = grc.call("getnewaddress", "swap-A-claims-GRC")
        b_grc = grc.call("getnewaddress", "swap-B-refund")
    except Exception as error:  # noqa: BLE001 -- checked: getnewaddress fails on a locked or missing wallet, and the message names which. Reported as a FAIL because every later step needs both addresses; nothing continues on a partial answer.
        console.check("GRC addresses", f"{type(error).__name__}: {error}", "two wallet addresses", False)
        return console.summary()
    console.check("GRC addresses", f"A claims to {a_grc}, B refunds to {b_grc}", "two wallet addresses", True)
    console.say("BOTH must be in this wallet: Gridcoin's createhtlc reads each party's PUBKEY out of the wallet, "
                "so a swap with a real counterparty needs their pubkey imported, not just their address.")

    console.step(3, f"one {HTLC_PREIMAGE_BYTES}-byte secret, committed on both chains")
    secret = os.urandom(HTLC_PREIMAGE_BYTES)
    secret_hash = hashlib.sha256(secret).digest()
    condition = preimage_condition(secret)
    console.say(f"sha256(secret)={secret_hash.hex()}  <- the commitment, public on both chains")
    console.say(f"XRPL condition={condition}")
    console.say("the secret itself is never printed. Gridcoin's script commits to the sha256 above through "
                "OP_SHA256; the XRPL condition's fingerprint is the same 32 bytes. One preimage, both legs.")

    console.step(4, "what each leg is worth, at the real rate")
    grc_amount, rate_source = resolve_grc_amount(console, args)
    if grc_amount is None:
        return console.summary()
    console.check("the GRC leg's size", f"{grc_amount} GRC against {XRP_DROPS} drops", "a positive amount",
                  grc_amount > 0)
    console.say(f"rate source: {rate_source}")

    console.step(5, "the two timelocks, in the two chains' different clocks")
    tip = int(grc.call("getblockcount"))
    now = time.time()
    xrp_cancel_after, grc_timeout, why = swap_timelocks(now, tip, hours_scale=args.hours_scale,
                                                       direction=args.direction)
    console.say(f"GRC tip={tip} (a height, not a duration)")
    console.say(f"policy: initiator {why['initiator_hours']}h, participant {why['participant_hours']}h "
                f"(scale={args.hours_scale}); GRC {why['grc_blocks']} blocks at an estimated "
                f"{why['grc_seconds_per_block']}s")
    try:
        console.check("timelock ordering", assert_timelock_ordering(xrp_cancel_after, grc_timeout, tip, now,
                                                                   direction=args.direction),
                      "the participant's leg to expire first", True)
    except SystemExit as refusal:
        console.check("timelock ordering", str(refusal), "the participant's leg to expire first", False)
        return console.summary()

    if not args.run:
        console.banner("DRY RUN -- nothing was submitted")
        console.say(f"step 6 would fund the XRP leg: Escrow of {XRP_DROPS} drops from {a_xrp} to {b_xrp}, "
                    f"Condition above, CancelAfter {xrp_cancel_after}.")
        console.say(f"step 6 would fund the GRC leg: createhtlc receiver={a_grc} sender={b_grc} "
                    f"hash={secret_hash.hex()} timeout={grc_timeout} amount={grc_amount}.")
        console.say("step 8 would claim the GRC with the secret; step 9 would read the secret back OFF THE "
                    "GRIDCOIN CHAIN; step 10 would finish the XRP escrow with what step 9 read. In the "
                    "grc-first direction the same five acts run in the other order -- see --direction.")
        console.say("re-run with --run to perform the swap.")
        return console.summary()

    passphrase = os.environ.get("GRC_WALLET_PASSPHRASE", "")
    if not console.check("GRC_WALLET_PASSPHRASE present", "yes" if passphrase else None,
                         "set, because claimhtlc signs", bool(passphrase)):
        console.say("Nothing was submitted. The value is never printed or logged.")
        return console.summary()

    submitter = Submitter(console.say)

    def submit_xrp(tx_json: dict, secret_for_this_tx: str) -> dict:
        """One XRPL submission, signed with the secret the CALLER names.

        The secret is a parameter rather than a closure over one account,
        because the GRC_FIRST direction has the escrow CREATED by one account
        and FINISHED by the other -- two secrets in one run. Closing over a
        single secret worked for XRP_FIRST and would have signed the finish as
        the wrong party here, which the ledger answers with a bare
        `badSecret`/invalid signature rather than anything that names the cause.
        """
        try:
            return submitter.submit(tx_json, secret_for_this_tx)
        except LocalSigningUnavailable as error:
            return {"error": "localSigningUnavailable", "error_message": str(error)}
    ctx = SwapContext(
        console=console, grc=grc, submit_xrp=submit_xrp,
        secret=secret, secret_hash=secret_hash, condition=condition,
        a_xrp=a_xrp, a_xrp_secret=a_xrp_secret, b_xrp=b_xrp, b_xrp_secret=b_xrp_secret,
        a_grc=a_grc, b_grc=b_grc,
        grc_amount=grc_amount, grc_timeout=grc_timeout, xrp_cancel_after=xrp_cancel_after,
        passphrase=passphrase,
    )
    runner = run_xrp_first if args.direction == XRP_FIRST else run_grc_first
    console.say(f"direction={args.direction}: running {runner.__name__}()")
    runner(ctx)
    return console.summary()


if __name__ == "__main__":
    sys.exit(main())
