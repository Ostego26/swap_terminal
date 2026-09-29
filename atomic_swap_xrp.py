#!/usr/bin/env python3
"""Run a REAL atomic swap between XRP and a script chain (BTC, LTC or GRC) on test networks.

RENAMED AND GENERALIZED 2026-09-29, from atomic_swap_xrp_grc.py. It could only put the
non-XRP leg on Gridcoin, and the reason was never protocol -- it was eleven hardcoded
"GRC" strings. Measured before the change: the file mentioned GRC 122 times and only 11
of those were code. It already reached the chain through chains/registry.build_adapters()
and the shared modules/htlc_* family, which atomic_swap.py had been proving work
identically across BTC, LTC and GRC; and modules/htlc_timelock.SECONDS_PER_BLOCK already
carried all three intervals. The generality was there; the strings were not.

WHY THE FILENAME CHANGED TOO, rather than keeping a working name. A file called
atomic_swap_xrp_grc.py that also swaps XRP against Bitcoin is a file somebody hunting for
an XRP<->BTC swap will not open. Rule 10 puts entry points at the root precisely so they
are discoverable by looking, and a name that describes half of what the file does defeats
that more thoroughly than a bad directory would. Fourteen references followed it.

WHAT THE THREE CHAINS SHARE AND DO NOT. They take the same P2SH HTLC, the same sha256
preimage, and the same claim/refund scripts -- that is why one driver covers them. They
differ in exactly two things this file has to know: the block interval that converts an
hours policy into a height (600s, 150s, 90s -- a 6.7x spread, and using the wrong one
sets a timeout that is hours off), and what their daemon calls a network it is safe to
lose coins on. Both are tables here, keyed by chain, and neither is derived from a rule,
because there is no rule -- they are three daemons' own vocabularies.

Role: file (the entry point; the decisions are swap_timelocks() and
      assert_timelock_ordering() here, the preimage read in
      modules/htlc_spend.preimage_from_scriptsig(), the condition encoding in
      chains/xrp_crypto_condition.py, and the lock policy in
      modules/htlc_timelock.py)
Reads: the XRPL testnet endpoint in chains/xrp_testnet.py, the faucet accounts
       in ~/.config/swap_terminal/keys/, and the script chain's daemon through
       Config.RPC[<the --chain asset>]
Writes: nothing on disk. It SUBMITS an EscrowCreate, an EscrowFinish, a P2SH
       HTLC funding transaction and its claim, and only with --run. The two
       script-chain transactions are built by modules/script_leg.py through the
       chain clients -- NOT by Gridcoin's createhtlc/claimhtlc, which is what
       confined this driver to one chain until 2026-09-29.
Can move funds: YES, on both chains, and this is the first file in this tree
       that moves money on two chains in one program. Testnet only.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED, on BOTH legs, before anything is
       submitted: refuse_mainnet() interrogates the rippled server's network_id
       and step 1 refuses any script-chain daemon whose network is not in that
       chain's own CHAIN_TEST_NETWORKS allowlist. Neither refusal has a flag that
       turns it off, and the allowlist refuses an unknown network rather than
       admitting anything that is merely not named "main".

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
    3. B funds the script leg: a P2SH HTLC on the same sha256, claimable by A,
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
    python3 atomic_swap_xrp.py            # describes every step, submits nothing
    python3 atomic_swap_xrp.py --run      # funds both legs and completes the swap
    python3 atomic_swap_xrp.py --run --direction grc-first   # the other way round

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
<CHAIN>_WALLET_PASSPHRASE for the claim. The dry run needs none of the secrets and
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

# DROPS_PER_XRP is imported, not respelled: chains/xrp_units.py owns it and a
# second copy of a unit conversion is rule 8's bug with a delay on it.
from chains.wallet_lock import encryption_state  # noqa: E402 -- same
from chains.xrp_crypto_condition import (  # noqa: E402 -- same
    HTLC_PREIMAGE_BYTES,
    preimage_condition,
    preimage_from_escrow_finish,
    preimage_fulfillment,
)
from chains.xrp_submit import LocalSigningUnavailable, Submitter  # noqa: E402 -- same
from chains.xrp_testnet import TESTNET_URL, refuse_mainnet, rpc, saved_faucet_accounts  # noqa: E402 -- same
from chains.xrp_units import DROPS_PER_XRP  # noqa: E402 -- same
from config import Config  # noqa: E402 -- same
from microfortnights import format_duration  # noqa: E402 -- same
from modules.atomic_btc_client import BTCClient  # noqa: E402 -- same
from modules.atomic_grc_client import GRCClient  # noqa: E402 -- same
from modules.atomic_ltc_client import LTCClient  # noqa: E402 -- same
from modules.htlc_chain_read import claim_scriptsig_hex  # noqa: E402 -- same
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
from modules.script_leg import ScriptLegKeys, mint_leg_keys  # noqa: E402 -- same
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


def chain_amount_for_rate(xrp_drops: int, xrp_per_chain_unit: Decimal) -> Decimal:
    """How much GRC one side of the swap is worth at this rate.

    `xrp_per_chain_unit` is how many XRP one GRC costs -- services/pricing's
    derive_pair_rate("GRC", "XRP") -- so the GRC amount is the XRP amount divided
    by it. Written as a division rather than folded into the caller so the
    direction of the rate is visible in one place: inverting it silently makes a
    swap off by the square of the price, which on testnet looks like a large
    number and nothing else.

    Decimal throughout. A float here would round at the 17th digit and the
    quantize below would inherit it, and the amount is what a daemon is asked to
    send.
    """
    if xrp_per_chain_unit <= 0:
        raise ValueError(f"a rate must be positive; got {xrp_per_chain_unit}. Nothing was priced.")
    xrp = Decimal(xrp_drops) / Decimal(DROPS_PER_XRP)
    return (xrp / xrp_per_chain_unit).quantize(Decimal(1).scaleb(-GRC_DECIMALS), rounding=ROUND_DOWN)

# Chains whose daemon must NOT be mainnet. Gridcoin reports its network in
# getblockchaininfo.chain on a modern build and getinfo.testnet on an old one;
# both are read, and anything that is not one of these aborts.
#: What each script chain CALLS a network that is safe to lose coins on. Per chain and
#: not one shared set, because the strings differ and a shared set is how a mainnet
#: answer slips through: Bitcoin says "main" for mainnet and "test"/"regtest"/"signet"
#: otherwise, Litecoin the same, and Gridcoin answers "test" or "testnet".
#:
#: NOT DERIVED FROM A COMMON RULE, deliberately. There is no rule -- these are three
#: daemons' own vocabularies, and inventing "anything that is not main" would authorize
#: a network none of them has ever answered. An allowlist refuses the unknown; a
#: denylist admits it.
#: What to CALL each chain in a line an operator reads. Only ever cosmetic -- nothing
#: branches on it -- but the banner said "(Gridcoin testnet)" beside a BTC leg until
#: 2026-09-29, which is rule 14's defect at the one moment it costs the most: the line
#: that tells an operator what is about to be funded.
#: The chains a full swap has actually COMPLETED on, and the evidence. Absence from this
#: table is not a gap in the table -- it is the honest state of a chain, and the banner
#: says so out loud rather than letting silence read as reassurance.

PROVEN_LIVE: dict[str, str] = {
    # EMPTIED 2026-09-29 AND NOT CARRIED ACROSS. GRC's two completed swaps (xrp-first claim
    # 78472df347a91f31..., chain-first claim 845315f4c2063670..., 2026-09-27) went through
    # Gridcoin's `createhtlc`/`claimhtlc`. This driver no longer calls either: both runners
    # now build and spend the P2SH through the chain clients, which is what lets BTC and LTC
    # work at all.
    #
    # THE EVIDENCE BELONGS TO THE PATH THAT PRODUCED IT. Leaving GRC listed would tell an
    # operator a failure is a regression on a route no run has ever taken -- the same
    # falsehood the per-chain banner was added to stop, one commit later and with the chain
    # name still technically correct. Re-earned by a run, not by a rename.
}

CHAIN_LABELS = {"BTC": "Bitcoin test network", "LTC": "Litecoin test network",
                "GRC": "Gridcoin testnet"}

CHAIN_TEST_NETWORKS = {
    "BTC": frozenset({"test", "testnet", "regtest", "signet"}),
    "LTC": frozenset({"test", "testnet", "regtest"}),
    "GRC": frozenset({"test", "testnet", "regtest"}),
}

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
#   CHAIN_FIRST  the GRC leg is funded first and the XRP leg is CLAIMED first, so
#              the secret appears in an XRPL EscrowFinish's Fulfillment and the
#              participant reads it with
#              chains/xrp_crypto_condition.preimage_from_escrow_finish().
#
# The direction names the INITIATOR's chain rather than "who wants what", because
# the initiator is the role the timelock policy is written against
# (modules/htlc_timelock.py) and naming it after the desire would invert on every
# reading.
#: The script chains this driver can put the non-XRP leg on. Not a new list: it is
#: exactly the keys of modules/htlc_timelock.SECONDS_PER_BLOCK, which already carried
#: BTC=600, LTC=150 and GRC=90 before this driver could use any of them but GRC, and
#: exactly the assets atomic_swap.py's CLIENTS drives. A fourth copy of the vocabulary
#: would be rule 8's defect with a delay on it, so this derives rather than declares.
SCRIPT_CHAINS = tuple(sorted(SECONDS_PER_BLOCK))
#: GRC unless told otherwise, because every recorded run of this driver was GRC and a
#: changed default would silently re-point an operator's existing command.
DEFAULT_CHAIN = "GRC"

#: The chains whose HTLC this driver can actually FUND. Not the chains it can reason
#: about -- it converts timelocks, prices legs and validates networks for all three -- but
#: the ones where steps 6 and 7 have a method to call.
#:
#: MEASURED 2026-09-29, AND THIS IS THE GAP THE --chain FLAG DID NOT CLOSE. Both runners
#: fund the script leg with `adapter.call("createhtlc", ...)`, and createhtlc is a
#: GRIDCOIN RPC. bitcoind and litecoind have no such method; they answer "Method not
#: found". Everything before step 6 succeeds on BTC -- the adapter, the regtest network,
#: the bech32 addresses, the commitment -- which is precisely what makes refusing here
#: rather than there necessary.
#:
#: WHY IT REFUSES BEFORE STEP 1 RATHER THAN FAILING AT STEP 6. In xrp-first the XRP
#: ESCROW IS FUNDED AT STEP 5. A --run that discovered the missing method at step 6 would
#: have one leg funded on a live chain and no way to fund the other -- the exact
#: one-sided state every timelock in this file exists to prevent, arriving through the
#: driver instead of through a counterparty.
#:
#: THE FIX IS NOT A NEW METHOD. modules/atomic_btc_client.py and
#: modules/atomic_grc_client.py already expose create_contract()/redeem_contract()/
#: refund_contract() and build the P2SH themselves -- atomic_swap.py funds HTLCs on all
#: three chains through exactly that interface, and neither client mentions createhtlc.
#: So this driver has a SECOND implementation of "fund an HTLC on a script chain" that
#: works on one chain where the first works on three: rule 8's defect, found by running
#: the thing rather than by reading it. Routing steps 6 and 7 through the clients is the
#: work, and it is not done.
CAN_FUND_THE_HTLC = frozenset(SCRIPT_CHAINS)

XRP_FIRST = "xrp-first"
CHAIN_FIRST = "chain-first"
#: The pre-2026-09-29 spelling of CHAIN_FIRST, when this driver could only put the
#: non-XRP leg on Gridcoin. Accepted and mapped, never carried further.
LEGACY_CHAIN_FIRST = "grc-first"
DIRECTIONS = (XRP_FIRST, CHAIN_FIRST)


@dataclass(frozen=True)
class ScriptLeg:
    """The script-chain half of the swap: which chain, its tip, and its timeout height.

    ONE VALUE BECAUSE THEY ARE ONE FACT. A timeout height means nothing without the tip
    it was measured from (the gap is what converts to wall-clock) and neither means
    anything without the chain, because the seconds-per-block that converts them is
    per chain. Passed separately they are three chances to hand one function a height
    from one chain and an interval from another, and the result of that is a timelock
    ordering that reads as safe and is not.

    IT IS ALSO WHAT RULE 12 ASKED FOR. assert_timelock_ordering() took four positional
    arguments and gained `chain` on 2026-09-29, which tripped PLR0913 -- and rule 12 is
    explicit that a function past the ceiling is a decision that has swallowed something,
    and the fix is to extract rather than to raise the ceiling or suppress the code. The
    thing it had swallowed was this: a leg.
    """

    chain: str
    tip_height: int
    timeout_height: int


def swap_timelocks(now_unix: float, chain_tip_height: int, *, hours_scale: float = 1.0,
                   direction: str = XRP_FIRST, chain: str = DEFAULT_CHAIN) -> tuple[int, int, dict]:
    """The two legs' timelocks, in the two chains' own clocks.

    Returns (xrp_cancel_after_ripple_seconds, chain_timeout_height, explanation).

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
    chain_hours = participant_hours if direction == XRP_FIRST else initiator_hours
    xrp_cancel_after = ripple_time(now_unix + xrp_hours * SECONDS_PER_HOUR)
    chain_blocks = int(chain_hours * SECONDS_PER_HOUR // SECONDS_PER_BLOCK[chain])
    chain_timeout = chain_tip_height + chain_blocks
    leg = ScriptLeg(chain=chain, tip_height=chain_tip_height, timeout_height=chain_timeout)
    return xrp_cancel_after, leg, {
        "direction": direction,
        "initiator_hours": initiator_hours,
        "participant_hours": participant_hours,
        "xrp_hours": xrp_hours,
        "chain_hours": chain_hours,
        "chain_blocks": chain_blocks,
        "chain": chain,
        "chain_seconds_per_block": SECONDS_PER_BLOCK[chain],
    }


def assert_timelock_ordering(xrp_cancel_after: int, leg: ScriptLeg, now_unix: float,
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
    chain = leg.chain
    chain_expiry_unix = now_unix + (leg.timeout_height - leg.tip_height) * SECONDS_PER_BLOCK[chain]
    # THE PARTICIPANT'S LEG IS THE ONE THAT MUST EXPIRE FIRST, whichever chain
    # that is. In XRP_FIRST the initiator is on XRP, so the GRC leg is the
    # participant's; in CHAIN_FIRST it is the other way round. Asserting "GRC
    # before XRP" unconditionally would pass the reverse direction while the
    # expiries were inverted, which is the one failure this function exists for.
    if direction == XRP_FIRST:
        initiator_expiry, participant_expiry = xrp_expiry_unix, chain_expiry_unix
        initiator_leg, participant_leg = "XRP", chain
    else:
        initiator_expiry, participant_expiry = chain_expiry_unix, xrp_expiry_unix
        initiator_leg, participant_leg = chain, "XRP"
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
        f"participant's favour. {chain} is height {leg.timeout_height} "
        f"({leg.timeout_height - leg.tip_height} blocks at an ESTIMATED {SECONDS_PER_BLOCK[chain]}s, a target "
        f"interval and not a guarantee); XRP is CancelAfter "
        f"{xrp_cancel_after}."
    )


def chain_network(adapter) -> str:
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


# claim_scriptsig_hex() and htlc_vout() MOVED to modules/htlc_chain_read.py on
# 2026-09-27, because a second driver (atomic_swap_grc_ltc.py) needs both on both of its
# legs and copying them would be rule 8's shape exactly. Nothing about them changed: they
# were already chain-generic, mention neither XRP nor Gridcoin, and take the same
# `adapter` with a `.call`. They are imported below rather than re-spelled here.



def _pinned_chain_amount(console: Console, raw: str, chain: str) -> tuple[Decimal | None, str]:
    """--grc-amount, validated. Its own function so resolve_chain_amount() stays
    under the return ceiling by SHAPE rather than by a suppression (rule 19)."""
    try:
        pinned = Decimal(raw)
    except (ArithmeticError, ValueError):
        console.check(f"the {chain} leg's size", f"--grc-amount {raw!r} is not a number", "a decimal", False)
        return None, ""
    if pinned <= 0:
        console.check(f"the {chain} leg's size", f"--grc-amount {pinned}", "a positive amount", False)
        return None, ""
    return pinned, "--grc-amount, pinned by hand; NO rate was applied and the legs are not priced"


def _rated_chain_amount(console: Console, raw: str, chain: str) -> tuple[Decimal | None, str]:
    """--rate, validated and applied. See chain_amount_for_rate for the direction.

    THE SENTENCE SAYS THE RATE BOTH WAYS ROUND, and that is worth four lines of code.
    `--rate` is XRP PER UNIT of the script chain, and the label always said so -- but the
    number an operator has in their head is usually the other one, because it is the one
    the recorded runs print ("1 XRP for 66.10250498 GRC"). Passing 66.1 to a flag that
    wants 0.0151 is accepted, arithmetically fine, and off by a factor of 4,300.

    Measured 2026-09-29: that is exactly what happened. `--rate 66.1` was suggested,
    passed, and produced a leg of 0.01512859 GRC against 1 XRP -- which the output
    labelled correctly and which nobody read, because a correct label is only half of
    rule 14. Printing the INVERSE beside it makes the mistake visible without the reader
    having to do the division, and the division is the step that was skipped.
    """
    try:
        rate = Decimal(raw)
        amount = chain_amount_for_rate(XRP_DROPS, rate)
    except (ArithmeticError, ValueError) as error:
        console.check(f"the {chain} leg's size", f"--rate {raw!r}: {error}", "a positive rate", False)
        return None, ""
    return amount, (
        f"--rate {rate} XRP per {chain} -- so 1 XRP buys {amount} {chain}, and 1 {chain} costs "
        f"{rate} XRP. IF THOSE ARE THE WRONG WAY ROUND FOR YOU, the flag wants XRP per {chain} "
        f"and you have passed its inverse. Supplied by hand and NOT checked against a market"
    )


def resolve_chain_amount(console: Console, args) -> tuple[Decimal | None, str]:
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
    if args.chain_amount:
        return _pinned_chain_amount(console, args.chain_amount, args.chain)
    if args.rate:
        return _rated_chain_amount(console, args.rate, args.chain)

    # LAZY, and PLC0415 is suppressed for one checked reason written here: importing
    # services.pricing at module scope would make --help reach for `requests` and a
    # config import on a machine with neither, and the dry run should be able to
    # describe a swap without a price.
    from services.pricing import IDS, derive_pair_rate, fetch_usd_prices  # noqa: PLC0415
    chain = getattr(args, "chain", DEFAULT_CHAIN)
    if chain not in IDS or "XRP" not in IDS:
        console.check(f"the {chain} leg's size", f"services/pricing.IDS covers {sorted(IDS)}",
                      f"both {chain} and XRP priced", False)
        return None, ""
    try:
        prices = fetch_usd_prices()
        # derive_pair_rate("GRC", "XRP") is GRC_USD / XRP_USD = how many XRP one
        # GRC costs, which is the direction chain_amount_for_rate divides by. Getting
        # this backwards makes the swap off by the square of the price, and on
        # testnet that looks like a large number and nothing else.
        rate = Decimal(str(derive_pair_rate(chain, "XRP", prices)))
        amount = chain_amount_for_rate(XRP_DROPS, rate)
    except Exception as error:  # noqa: BLE001 -- checked: fetch_usd_prices can fail on the network, on a non-200, on a partial response (it raises KeyError naming the missing asset), or on a rate of zero. EVERY one of those must stop the run rather than fall back to an unpriced amount, and the message names --rate as the way through. Nothing here treats a failure as a price.
        console.check(f"the {chain} leg's size", f"{type(error).__name__}: {error}",
                      f"a live XRP/{chain} rate", False)
        console.say("NOTHING WAS SUBMITTED. A swap will not be priced 1:1 by default -- that is how a thousand "
                    "dollars of one thing moves for a dollar of another, and on testnet it looks fine. Pass "
                    f"--rate <XRP per {chain}> to price it by hand, or --chain-amount to pin the size "
                    "outright.")
        return None, ""
    return amount, (
        f"services/pricing.py (CoinGecko): {chain} ${prices[chain + '_USD']}, XRP ${prices['XRP_USD']}, so "
        f"{rate:.8f} XRP per {chain}. {XRP_DROPS} drops buys {amount} {chain}, rounded DOWN to "
        f"{GRC_DECIMALS} places in the {chain} holder's favour"
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
    #: Which script chain this run is on. Carried rather than re-derived, because every
    #: operator-facing line in both runners names it, and a line that says GRC while the
    #: run funds BTC is rule 14's defect at the one moment it costs money.
    chain: str
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
    chain_amount: Decimal
    chain_timeout: int
    xrp_cancel_after: int
    passphrase: str
    #: Whether the script chain's wallet has a passphrase at all. Carried rather than
    #: re-read at each of the three unlock sites: getwalletinfo is a network call, and
    #: three reads are three chances to get three answers mid-swap.
    wallet_encrypted: bool
    #: The chain client that builds and spends the P2SH. NOT the adapter above: `grc` is
    #: the raw RPC used for getnewaddress, the tip and reading a claim back, while this is
    #: BTCClient/LTCClient/GRCClient, which own the script. One driver, two handles on one
    #: daemon, and they do different jobs -- see modules/script_leg.py.
    script_client: object
    #: The two throwaway keypairs this swap's HTLC branches pay to. Minted per run, never
    #: written, never printed.
    leg_keys: ScriptLegKeys


def run_xrp_first(ctx: SwapContext) -> bool:  # noqa: PLR0915 -- checked: this is the protocol's ORDER, five acts across two chains, and every decision inside it is extracted (the timelocks above, the preimage read in modules/htlc_spend, the payloads in xrp_htlc_escrow, the vout lookup in htlc_vout). Rule 10 puts the order in the file named after the thing being done; splitting it would hide the sequence, and the sequence IS the security property.
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
    from chains.wallet_lock import unlocked_for_payout  # noqa: PLC0415
    from modules.script_leg import (  # noqa: PLC0415 -- checked: only the --run path funds anything
        claim_the_script_leg,
        fund_the_script_leg,
    )

    ctx.console.step(7, f"B funds the {ctx.chain} leg: {ctx.chain_amount} {ctx.chain}, same hash, expiring FIRST")
    ctx.console.say(
        "ONLY THE FUNDING NEEDS THE WALLET. It sends coins to the P2SH, so an encrypted wallet is "
        "opened for that and closed again immediately. The CLAIM in step 8 signs with a key minted "
        "in this process (modules/script_leg.py), not with a wallet key, so it happens OUTSIDE the "
        "unlock -- the wallet is shut before the irreversible step, not during it."
    )
    contract = None
    claim_txid = None
    try:
        with unlocked_for_payout(ctx.grc, ctx.passphrase, chain=ctx.chain,
                                 encrypted=ctx.wallet_encrypted):
            contract = fund_the_script_leg(ctx.script_client, ctx.chain_amount,
                                           ctx.secret_hash.hex(), ctx.leg_keys, ctx.chain_timeout)
        # THE CLIENT ALREADY LOCATED THE OUTPUT, so there is no index to guess here.
        # create_contract() calls wait_for_tx_output() against the P2SH script itself and
        # returns the index it found -- which is the check the old createhtlc path had to
        # perform separately with htlc_vout(), because createhtlc funds through SendMoney
        # and adds a change output, making index 0 as likely to be the change as the HTLC.
        funding_txid = contract["txid"]
        ctx.console.check(f"{ctx.chain} leg funded",
                          f"p2sh={contract['p2shAddress']} txid={funding_txid} vout={contract['vout']}",
                          "a funded HTLC with its output located", bool(funding_txid))
        ctx.console.say(f"{ctx.chain} redeem script={contract['redeemScript'].hex()}")
        ctx.console.say(
            f"claim branch pays the key minted for it; refund branch pays a SECOND minted key at "
            f"height {ctx.chain_timeout}. Neither key exists anywhere but this process, and neither "
            f"controls anything else."
        )

        ctx.console.step(8, f"A claims the {ctx.chain} with the secret -- which PUBLISHES it")
        ctx.console.say("this is the irreversible step for A: claiming requires pushing the secret into a "
                        "scriptSig that lands in a block. A cannot take the coins without giving B what B needs.")
        claim_txid = claim_the_script_leg(ctx.script_client, contract, ctx.secret,
                                          ctx.leg_keys, ctx.a_grc)
        ctx.console.check(f"A claimed the {ctx.chain}", f"txid={claim_txid}", "a broadcast txid",
                          bool(claim_txid))
    except Exception as error:  # noqa: BLE001 -- checked: create_contract and redeem_contract each refuse for several named reasons (a locked wallet, insufficient funds, a funding output that never appeared, a wrong preimage, a script failure) and the unlock/restore can fail on its own. It is reported rather than raised because the XRP leg is ALREADY FUNDED here, and which recovery line applies depends on how far the block got -- an operator needs that sentence, not a traceback. The unlock context restores the wallet on the way out regardless.
        ctx.console.check(f"the {ctx.chain} leg", f"{type(error).__name__}: {error}",
                      "a funded HTLC, its output located, and a claim", False)
        if claim_txid:
            ctx.console.say(f"the claim went out as {claim_txid} -- the secret IS public. B must finish the escrow "
                        f"with it; read the secret out of that transaction. Do not let the escrow expire.")
        elif contract and contract.get("txid"):
            ctx.console.say(f"BOTH LEGS ARE FUNDED AND NEITHER IS CLAIMED. The secret has NOT been published, so "
                        f"nobody can finish the escrow: the {ctx.chain} refund branch returns it at height "
                        f"{ctx.chain_timeout} and A recovers the XRP at CancelAfter {ctx.xrp_cancel_after}. "
                        f"Do NOT publish the secret.")
        else:
            # "nobody has the ctx.secret" stood here until 2026-09-29 -- an attribute
            # expression that had leaked into prose, printed to an operator deciding what
            # to do with a funded escrow. Rule 14: the line an operator reads at the worst
            # moment is the one that must be readable.
            ctx.console.say(f"THE XRP LEG IS FUNDED AND THE {ctx.chain} LEG IS NOT. Nothing is lost: nobody has "
                        f"the secret, so nobody can finish the escrow, and it returns to A at CancelAfter "
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
        ctx.console.say(f"B cannot finish the escrow without it and recovers the {ctx.chain} at height {ctx.chain_timeout}... f"
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
    ctx.console.say(f"{ctx.chain}: {ctx.chain_amount} from B's wallet to {ctx.a_grc}, claimed with the secret (txid {claim_txid})f")
    ctx.console.say(f"XRP: {XRP_DROPS} drops from {ctx.a_xrp} to {ctx.b_xrp}, released by the same secret")
    ctx.console.say("interlocked by one sha256, with neither party ever sending the other the preimage.")
    return False
    return True


def run_chain_first(ctx: SwapContext) -> bool:  # noqa: PLR0915 -- checked: same as run_xrp_first, and the two are deliberately parallel so a reader can diff them. The decisions are extracted; what is left is the order.
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
    from chains.wallet_lock import unlocked_for_payout  # noqa: PLC0415
    from modules.script_leg import claim_the_script_leg, fund_the_script_leg  # noqa: PLC0415

    console.step(6, f"B funds the {ctx.chain} leg FIRST: {ctx.chain_amount} {ctx.chain}, hashlocked, expiring LAST")
    console.say("the initiator funds first and takes the LONGER lock. Here that is the Gridcoin side, so the "
                "GRC timeout is the one that outlives the XRP escrow -- the reverse of the other direction.")
    funding_txid = None
    contract = None
    try:
        with unlocked_for_payout(grc, ctx.passphrase, chain=ctx.chain,
                                 encrypted=ctx.wallet_encrypted):
            contract = fund_the_script_leg(ctx.script_client, ctx.chain_amount,
                                           ctx.secret_hash.hex(), ctx.leg_keys, ctx.chain_timeout)
        funding_txid = contract["txid"]
        # THE OUTPUT INDEX IS THE CLIENT'S ANSWER, NOT A GUESS. create_contract() waits for
        # an output paying the P2SH script itself and returns the index it found, which is
        # the check the old createhtlc path had to make separately -- that RPC funds through
        # SendMoney and adds a change output, so index 0 is as likely to be the change.
        console.check(f"{ctx.chain} leg funded",
                      f"p2sh={contract['p2shAddress']} txid={funding_txid} vout={contract['vout']}",
                      "a funded HTLC with its output located", bool(funding_txid))
        console.say(f"{ctx.chain} redeem script={contract['redeemScript'].hex()}")
    except Exception as error:  # noqa: BLE001 -- checked: create_contract and the unlock each refuse for named reasons (a locked wallet, insufficient funds, a funding output that never appeared) and the message says which. Reported rather than raised because this is the FIRST leg: nothing else is funded, so the recovery line is short and the operator needs it rather than a traceback. The unlock context restores the wallet on the way out.
        console.check(f"the {ctx.chain} leg", f"{type(error).__name__}: {error}", "a funded HTLC with its output located",
                      False)
        console.say(f"NOTHING ELSE WAS SUBMITTED. If the funding transaction did go out, the {ctx.chain} returns "
                    f"to the refund branch at height {ctx.chain_timeout}; no XRP was escrowed and the secret was "
                    f"never published.")
        return False

    console.step(7, f"A funds the XRP leg: {XRP_DROPS} drops to B's counterparty, expiring FIRST")
    a_before = balance_drops(ctx.a_xrp)
    created = ctx.submit_xrp(escrow_create_tx(ctx.b_xrp, ctx.a_xrp, XRP_DROPS, ctx.condition,
                                              ctx.xrp_cancel_after), ctx.b_xrp_secret)
    if not console.check("XRP leg funded", describe_result(created), "tesSUCCESS",
                         engine_result(created) == "tesSUCCESS"):
        console.say(f"the {ctx.chain} leg IS funded ({funding_txid}) and the XRP leg is not. Nobody has the secret, so "
                    f"nobody can claim the GRC: it returns to B at height {ctx.chain_timeout}. Do NOT publish the "
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
                    f"GRC at height {ctx.chain_timeout}. Do NOT publish the secret.")
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
        console.say(f"B cannot claim the {ctx.chain} without it and the {ctx.chain} returns to B at height {ctx.chain_timeout} -- "
                    f"except that A HAS ALREADY TAKEN THE XRP. Read {finish_hash} by hand; the secret is in its "
                    f"Fulfillment field.")
        return False
    # The same assertion the other direction makes: `revealed` came from the
    # ledger and `secret` from memory. A version that claimed with `secret`
    # directly would work here and prove nothing, because a real B has no such
    # variable.
    console.check("what the ledger gave B equals what A committed to", revealed == ctx.secret, "True",
                  revealed == ctx.secret)

    console.step(10, f"B claims the {ctx.chain} with the secret it read")
    claim_txid = None
    # NO UNLOCK HERE, and that is the change rather than an omission. The claim signs with
    # the key minted for the contract's hashlock branch (modules/script_leg.py), not with a
    # wallet key, so the wallet is never opened for the irreversible step. Until 2026-09-29
    # this called Gridcoin's claimhtlc, which needs EnsureWalletIsUnlocked() -- so the
    # wallet was held open across a claim that publishes a secret.
    try:
        claim_txid = claim_the_script_leg(ctx.script_client, contract, revealed,
                                          ctx.leg_keys, ctx.a_grc)
    except Exception as error:  # noqa: BLE001 -- checked: redeem_contract refuses on a wrong preimage, an output it cannot read back, or a script failure, and the message says which. Reported because A already has the XRP at this point, so the operator needs to know the coins are still claimable with a secret that is now public rather than getting a traceback.
        console.check(f"B claimed the {ctx.chain}", f"{type(error).__name__}: {error}", "a broadcast txid", False)
        console.say(f"A HAS THE XRP AND B HAS NOT CLAIMED THE {ctx.chain}. The secret is PUBLIC (in {finish_hash}), "
                    f"so the claim can be retried before height {ctx.chain_timeout}, after which the coins return "
                    f"to the refund branch anyway.")
        return False
    console.check(f"B claimed the {ctx.chain}", f"txid={claim_txid}", "a broadcast txid", bool(claim_txid))

    console.banner("WHAT CHANGED HANDS")
    console.say(f"XRP: {XRP_DROPS} drops from {ctx.b_xrp} to {ctx.a_xrp}, claimed with the secret ({finish_hash})")
    console.say(f"{ctx.chain}: {ctx.chain_amount} from B's wallet through the HTLC to {ctx.a_grc}, released by the "
                f"same secret (txid {claim_txid})")
    console.say("interlocked by one sha256, with neither party ever sending the other the preimage.")
    return True


#: The chain clients that own the P2SH, keyed the same way everything else here is.
#: Imported rather than re-implemented -- these are the ones atomic_swap.py swaps BTC,
#: LTC and GRC against each other with, and a second construction of the same three would
#: be the duplication this whole change removed.
SCRIPT_CLIENTS = {"BTC": BTCClient, "LTC": LTCClient, "GRC": GRCClient}


def build_script_client(chain: str, rpc: dict):
    """The chain client for the script leg, from the same Config.RPC the adapter came from.

    TWO HANDLES ON ONE DAEMON, DELIBERATELY. `build_adapters()` gives a generic RPC object
    -- what getnewaddress, the tip and reading a claim back need. The client below owns the
    HTLC script: it builds the redeem script, funds the P2SH, locates the output and signs
    the spend. They are different jobs and neither is a superset of the other, so the driver
    holds both rather than widening one.

    THE URL IS ASSEMBLED FROM host AND port RATHER THAN READ FROM A SECOND VARIABLE.
    atomic_swap.py reads {ASSET}_RPC_URL; this driver already has the host and port in
    Config.RPC because the adapter needed them, and asking an operator to set a URL as well
    as a port -- which must agree -- is a second source for one fact (rule 8).
    """
    host = rpc.get("host") or "127.0.0.1"
    port = rpc.get("port")
    if not port:
        raise ValueError(f"{chain}_RPC_PORT is not set, so no {chain} client can be built")
    wallet = (rpc.get("wallet") or "").strip()
    url = f"http://{host}:{port}" + (f"/wallet/{wallet}" if wallet else "")
    return SCRIPT_CLIENTS[chain](url, rpc.get("user") or "", rpc.get("password") or "")


def describe_the_dry_run(console: Console, args, chain: str, leg: ScriptLeg,  # noqa: PLR0913, PLR0917 -- checked: these eight ARE the swap as far as a reader is concerned -- both legs' sizes, both timelocks, and where each side's coins land. None is derivable from another and none can be defaulted. Bundling them would mean building the SwapContext before the dry run, which is the one thing a dry run must not do: it mints keys and constructs a client, both of which are real work for a run that is not happening.
                         chain_amount, xrp_cancel_after: int, a_xrp: str, b_xrp: str,
                         a_grc: str) -> None:
    """What --run WOULD do, described from the same values the run would use.

    IT DESCRIBED A PATH THAT NO LONGER EXISTED. Until 2026-09-29 this said "step 6 would
    fund the GRC leg: createhtlc receiver=<wallet address> sender=<wallet address>", and
    both halves were wrong the moment the runners moved onto the chain clients: there is no
    createhtlc call, and those wallet addresses are where the claim and refund LAND, not
    the HTLC's branches. Caught by an operator running it, not by the suite -- a dry run's
    output is prose, and no test read it.

    That is worse than an ordinary stale comment. A dry run exists to be read BEFORE
    committing money, so a dry run describing the wrong mechanism is a wrong comment at the
    exact moment it is load-bearing.

    THE BRANCH ADDRESSES ARE NOT SHOWN HERE, and the absence is deliberate rather than an
    omission: the keys are minted in prepare_the_script_leg(), which a dry run never
    reaches, because minting two keypairs and constructing a client is real work for a run
    that is not happening. So this says WHAT would be built rather than pretending to know
    the addresses a future run will mint.
    """
    console.banner("DRY RUN -- nothing was submitted")
    xrp_step, chain_step = (6, 7) if args.direction == XRP_FIRST else (7, 6)
    console.say(f"step {xrp_step} would fund the XRP leg: Escrow of {XRP_DROPS} drops from {a_xrp} to "
                f"{b_xrp}, Condition above, CancelAfter {xrp_cancel_after}.")
    console.say(f"step {chain_step} would fund the {chain} leg: a P2SH HTLC paying {chain_amount} {chain}, "
                f"committing to the sha256 above, claimable by a key minted for it and refundable to a "
                f"SECOND minted key at height {leg.timeout_height}. Built and funded through "
                f"modules/script_leg.py, which is why btc and ltc work here too.")
    console.say(f"the claim would push the secret into a scriptSig on {chain}; the other side would read it "
                f"back OFF THAT CHAIN and finish the XRP escrow with it. Claimed coins land at {a_grc}.")
    console.say("THE WALLET IS OPENED FOR THE FUNDING ONLY. The claim signs with the minted key, so the "
                "wallet is shut before the step that publishes the secret.")
    console.say("re-run with --run to perform the swap.")


def say_what_has_actually_run(console: Console, chain: str) -> None:
    """What evidence exists for THIS chain on THIS code path, before anything is funded.

    A CLAIM ABOUT EVIDENCE IS A CLAIM, and this driver printed the wrong one twice in one
    day. First it printed GRC's two completed swaps on every chain, telling a BTC operator
    a failure was a regression on a route no run had taken. Then -- after that was keyed
    per chain -- the funding path moved onto the chain clients, and GRC's entry would have
    gone on asserting evidence for a route that had also never run, with the chain name
    still technically correct. The second version is the more dangerous one: it is right
    about everything except the thing that matters.

    So PROVEN_LIVE is keyed by chain AND emptied whenever the path changes, and absence is
    not a gap in a table -- it is the honest state, printed as such. Re-earned by a run.
    """
    if chain in PROVEN_LIVE:
        console.say(PROVEN_LIVE[chain])
        return
    console.say(
        f"NO {chain} RUN HAS COMPLETED ON THIS CODE PATH. Both runners fund and spend the HTLC "
        f"through the chain clients (modules/script_leg.py) as of 2026-09-29, which is what lets "
        f"BTC and LTC work at all -- and it is NOT the path GRC's 2026-09-27 swaps took, so that "
        f"evidence was not carried across. What is exercised here is seeded tests: the block "
        f"arithmetic, the timelock ordering, the branch ordering and the key handling. A failure "
        f"is a DISCOVERY, not a regression, and is worth reading rather than retrying."
    )


def prepare_the_script_leg(console: Console, chain: str) -> tuple[object, ScriptLegKeys] | None:
    """The client that owns the HTLC and the two keys its branches pay to, or None.

    BOTH OR NEITHER, which is why they are made together. A client with no keys cannot
    fund anything and keys with no client are two secrets nobody asked for; returning them
    as a pair means no caller can hold half of the arrangement.

    THE ADDRESSES ARE PRINTED AND THE KEYS ARE NOT. An address is public by construction --
    it is what the contract pays, and it will be on the chain within seconds. The private
    halves are never printed, never logged and never written; CLAUDE.md's chain-safety
    rules put that above convenience, and modules/script_leg.py deliberately has no repr
    that would make printing the pair look safe.

    Returns None having SAID WHY rather than raising, because nothing is funded at this
    point and a traceback at step 5 of ten tells an operator less than the sentence does.
    """
    try:
        client = build_script_client(chain, Config.RPC[chain])
    except Exception as error:  # noqa: BLE001 -- checked: a missing port, an unknown chain, or the client constructor refusing all mean one thing to this caller -- the script leg cannot be built, and NOTHING has been funded. The name and message are reported and the run stops.
        console.check(f"{chain} script client", f"{type(error).__name__}: {error}",
                      "a client that can build the HTLC", False)
        return None
    keys = mint_leg_keys()
    console.say(
        f"minted two throwaway keypairs for this swap's {chain} HTLC branches -- claim "
        f"{keys.claim_address}, refund {keys.refund_address}. Generated in this process, "
        f"never written, and they control nothing but this contract. The ADDRESSES are "
        f"public; the keys are never printed."
    )
    return client, keys


def normalize_arguments(args) -> None:
    """Turn what an operator TYPED into what the tables are keyed by. In place, once.

    `--chain btc` is what a person types and "BTC" is what SECONDS_PER_BLOCK,
    CHAIN_TEST_NETWORKS, CHAIN_LABELS, Config.RPC and services.pricing.IDS are all keyed
    by. Converting at each use is five chances to miss one, and the miss is a KeyError at
    whichever step got there first.

    `grc-first` resolves to `chain-first` here so nothing downstream ever sees two names
    for one direction. It is accepted because it is the spelling in every recorded run of
    this driver and in the operator's history, and a direction that vanishes on a rename
    fails at the moment somebody is trying to move money.

    IN PLACE AND RETURNING NONE, deliberately: an argparse Namespace is the thing every
    later line reads, so handing back a copy would leave two of them and a reader would
    have to know which one main() kept.
    """
    args.chain = args.chain.upper()
    if args.direction == LEGACY_CHAIN_FIRST:
        args.direction = CHAIN_FIRST


def refuse_a_chain_this_driver_cannot_fund(console: Console, chain: str) -> bool:
    """True if this chain's HTLC cannot be funded here, having said why. Contacts nothing.

    A FUNCTION SO IT CAN BE CALLED WITH A SEEDED CONSOLE, and because the branches it
    added put main() over PLR0912 -- rule 12's answer being to extract the decision rather
    than raise the ceiling. The decision is "can step 6 happen at all", and it is knowable
    before the first network call, which is the entire reason it is asked here.
    """
    if chain in CAN_FUND_THE_HTLC:
        return False
    console.say(
        f"REFUSED BEFORE ANYTHING IS CONTACTED: this driver funds the script leg with `createhtlc`, "
        f"which is a GRIDCOIN RPC. {chain} has no such method and would answer 'Method not found' at "
        f"step 6 -- AFTER the XRP escrow is funded at step 5, leaving one leg funded on a live chain "
        f"and the other impossible. That is the one-sided state every timelock in this file exists to "
        f"prevent, so it refuses here instead."
    )
    console.say(
        f"EVERYTHING ELSE ABOUT {chain} WORKS and is not the problem: the adapter, the network "
        f"allowlist, the addresses, the {SECONDS_PER_BLOCK[chain]}s block interval and the timelock "
        f"ordering are all exercised."
    )
    console.say(
        "THE FIX IS NOT A NEW METHOD. modules/atomic_btc_client.py and modules/atomic_ltc_client.py "
        "already expose create_contract()/redeem_contract()/refund_contract() and build the P2SH "
        "themselves -- atomic_swap.py funds HTLCs on all three chains through them. Steps 6 and 7 here "
        "need to go through that interface instead of a raw RPC. Until then --chain grc is the only one "
        "that can complete."
    )
    console.check(f"{chain} HTLC can be funded by this driver", "no",
                  f"a chain in {sorted(CAN_FUND_THE_HTLC)}", False)
    return True


def resolve_wallet_passphrase(console: Console, adapter, chain: str) -> tuple[str, bool] | None:
    """(passphrase, is_encrypted), or None meaning REFUSE before anything is funded.

    ASKED OF THE WALLET, NOT ASSUMED FROM THE CHAIN. Until 2026-09-29 the driver refused
    to start unless <CHAIN>_WALLET_PASSPHRASE was set -- a demand an UNENCRYPTED wallet
    cannot satisfy, because it has no passphrase to set. A fresh regtest bitcoind wallet
    is the ordinary case of one, and it is the wallet somebody reaches for first when
    trying --chain btc. The refusal was unsatisfiable and said nothing about why.

    A FUNCTION RATHER THAN FOUR BRANCHES IN main(), and rule 12's reason rather than
    taste: adding them put main() over the branch ceiling, and rule 12 is explicit that a
    function past the ceiling is orchestration that has swallowed a decision -- the fix
    being to extract it so it can be called with a seeded adapter, never to raise the
    ceiling. This is the decision it had swallowed.

    THE VALUE IS NEVER RETURNED TO A LOG OR A CONSOLE LINE. Only the NAME of the variable
    is printed, which is what an operator needs in order to set it; argv is world-readable
    through /proc and `ps`, and a passphrase that reached this process any other way would
    be a disclosure this file could not undo.
    """
    encrypted, why = encryption_state(adapter)
    console.say(f"{chain} wallet: {why}")
    variable = f"{chain}_WALLET_PASSPHRASE"
    passphrase = os.environ.get(variable, "")
    if not encrypted:
        console.check(f"{variable} needed", "no", "not needed for an unencrypted wallet", True)
        return passphrase, False
    if not console.check(f"{variable} present", "yes" if passphrase else None,
                         "set, because this wallet is encrypted and claimhtlc signs",
                         bool(passphrase)):
        console.say("Nothing was submitted. The value is never printed or logged.")
        return None
    return passphrase, True


#: Which runner each direction uses. A TABLE rather than a conditional, for the reason
#: selected_chains() in fund_testnets.py gives about its own: "which, in what order" is
#: data. A ternary here also read as if there were two cases when DIRECTIONS is the thing
#: that decides how many there are -- add a third and the ternary silently routes it to
#: the else branch, while this raises KeyError naming it.
RUNNERS = {XRP_FIRST: run_xrp_first, CHAIN_FIRST: run_chain_first}


def main() -> int:  # noqa: C901, PLR0911, PLR0915 -- checked: this is the swap's SEQUENCE, and every decision in it is extracted -- the timelocks and their ordering above, the preimage read in modules/htlc_spend, the condition in chains/xrp_crypto_condition, the payloads in xrp_htlc_escrow. What is left is the order of five acts on two chains, which is what rule 10 says a file at the root is for. Splitting it would put the order somewhere other than the file named after the thing being done, and the order IS the protocol.
    parser = argparse.ArgumentParser(
        description="A real atomic swap: XRP on the XRPL testnet against a script chain (BTC, LTC or "
                    "GRC) on its own testnet, interlocked by one sha256 preimage. Testnet only, "
                    "structurally.",
    )
    parser.add_argument("--run", action="store_true",
                        help="actually submit. Without it every step is described and nothing is sent")
    parser.add_argument("--chain", choices=[c.lower() for c in SCRIPT_CHAINS], default=DEFAULT_CHAIN.lower(),
                        type=str.lower,
                        help=f"which script chain the non-XRP leg is on (default {DEFAULT_CHAIN.lower()}). All "
                             f"three take the same P2SH HTLC -- see atomic_swap.py, which swaps them against "
                             f"each other -- and differ here only in their block interval and what their "
                             f"daemon calls a test network")
    # grc-first IS STILL ACCEPTED, for the reason --grc-amount is: it is the spelling in
    # every recorded run of this driver (docs/atomic_swap_runs_2026_09_27.md names it
    # twice) and in the operator's history. It resolves to chain-first below rather than
    # being a second direction, so nothing downstream sees two names for one thing.
    parser.add_argument("--direction", choices=(*DIRECTIONS, LEGACY_CHAIN_FIRST), default=XRP_FIRST,
                        help=f"which chain the INITIATOR is on: {XRP_FIRST} (XRP funded first, GRC claimed "
                             f"first, secret read from a Gridcoin scriptSig) or {CHAIN_FIRST} (GRC funded first, "
                             f"XRP claimed first, secret read from an XRPL Fulfillment). The initiator always "
                             f"takes the longer lock")
    parser.add_argument("--rate", type=str, default="",
                        help="XRP per unit of the script chain, overriding the live price. Use it when "
                             "CoinGecko is unreachable or "
                             "when a specific figure is wanted; the run prints which source was used")
    # --grc-amount IS STILL ACCEPTED, and that is not tidiness. It is in the operator's
    # shell history and possibly in a script, and a flag that vanishes on a rename fails
    # with "unrecognized arguments" at the point somebody is trying to move money.
    parser.add_argument("--chain-amount", "--grc-amount", type=str, default="", dest="chain_amount",
                        help="pin the script-chain leg outright and skip pricing entirely. Mutually exclusive "
                             "in effect with --rate, which sizes it instead. --grc-amount is the old spelling "
                             "and still works")
    parser.add_argument("--hours-scale", type=float, default=1.0,
                        help="shorten BOTH legs by this factor for a demonstration (default 1.0 = the real "
                             "48h/24h policy). It scales both, so the 2:1 ordering is preserved")
    args = parser.parse_args()
    # NORMALIZED ONCE, HERE, and not at each use. `--chain btc` is what an operator types
    # and "BTC" is what SECONDS_PER_BLOCK, CHAIN_TEST_NETWORKS, Config.RPC and
    # services.pricing.IDS are all keyed by; converting at four call sites is four chances
    # to miss one, and the miss would be a KeyError at the point of funding.
    normalize_arguments(args)

    console = Console(total_steps=10)
    console.banner(f"ATOMIC SWAP -- XRP (XRPL testnet) for {args.chain} ({CHAIN_LABELS[args.chain]})")
    if refuse_a_chain_this_driver_cannot_fund(console, args.chain):
        return console.summary()
    console.say(f"XRP endpoint={TESTNET_URL}")
    console.say(f"mode={'--run: BOTH LEGS WILL BE FUNDED' if args.run else 'DRY RUN: nothing is submitted'}")
    console.say(f"A holds XRP and wants {args.chain} (the INITIATOR, longer lock). B holds {args.chain} and wants XRP (the "
                "PARTICIPANT, shorter lock).")
    console.say("one operator plays both parties here, so counterparty misbehavior is NOT exercised -- the "
                "mechanism is, on real chains. See this file's header.")
    # WHAT HAS ACTUALLY RUN, PER CHAIN, because "a failure here is a regression" is a
    # claim about evidence and it was printed on every chain until 2026-09-29. On a BTC or
    # LTC run it was false in the dangerous direction: it told an operator a failure would
    # be a regression when no run had ever happened, so a genuine first-time discovery
    # would read as a known-good path breaking. Rule 17's register error, printed.
    say_what_has_actually_run(console, args.chain)

    console.step(1, "both networks are TEST networks, and each says which")
    try:
        console.check("XRP network", refuse_mainnet(), "a non-mainnet network_id", True)
    except Exception as error:  # noqa: BLE001 -- checked: refuse_mainnet raises RuntimeError on a mainnet answer and requests raises a connection error when the endpoint is unreachable, and BOTH must arrive as a labeled FAIL rather than a traceback at step 1 of nine (rule 14). Nothing treats the failure as a pass; it returns non-zero through the summary.
        console.check("XRP network", f"{type(error).__name__}: {error}", "a non-mainnet network_id", False)
        return console.summary()

    adapters = build_adapters(Config.RPC)
    chain = args.chain
    grc = adapters.get(chain)
    if not console.check(f"{chain} adapter configured", "yes" if grc else None,
                         f"{chain}_RPC_* set in the environment", grc is not None):
        console.say(f"chains/registry.why_unconfigured({chain!r}) names the missing variable. "
                    "Nothing was submitted.")
        return console.summary()
    network = chain_network(grc)
    safe = CHAIN_TEST_NETWORKS[chain]
    if not console.check(f"{chain} network", network, f"one of {sorted(safe)}", network in safe):
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
        console.check(f"{chain} addresses", f"{type(error).__name__}: {error}", "two wallet addresses", False)
        return console.summary()
    console.check(f"{chain} addresses", f"A claims to {a_grc}, B refunds to {b_grc}", "two wallet addresses", True)
    console.say("THESE TWO ARE WHERE THE CLAIMED AND REFUNDED COINS LAND, not the HTLC's own branches. The "
                "branches pay two keys minted in this process (step 5b) and neither needs to be in the wallet "
                "-- which is what removed the 'import your counterparty's pubkey' requirement Gridcoin's "
                "createhtlc imposed until 2026-09-29.")

    console.step(3, f"one {HTLC_PREIMAGE_BYTES}-byte secret, committed on both chains")
    secret = os.urandom(HTLC_PREIMAGE_BYTES)
    secret_hash = hashlib.sha256(secret).digest()
    condition = preimage_condition(secret)
    console.say(f"sha256(secret)={secret_hash.hex()}  <- the commitment, public on both chains")
    console.say(f"XRPL condition={condition}")
    console.say(f"the secret itself is never printed. The {chain} P2SH script this driver builds commits to the "
                f"sha256 above through OP_SHA256; the XRPL condition's fingerprint is the same 32 bytes. One "
                f"preimage, both legs.")

    console.step(4, "what each leg is worth, at the real rate")
    chain_amount, rate_source = resolve_chain_amount(console, args)
    if chain_amount is None:
        return console.summary()
    console.check(f"the {chain} leg's size", f"{chain_amount} {chain} against {XRP_DROPS} drops", "a positive amount",
                  chain_amount > 0)
    console.say(f"rate source: {rate_source}")

    console.step(5, "the two timelocks, in the two chains' different clocks")
    tip = int(grc.call("getblockcount"))
    now = time.time()
    xrp_cancel_after, leg, why = swap_timelocks(now, tip, chain=chain, hours_scale=args.hours_scale,
                                                       direction=args.direction)
    console.say(f"{chain} tip={tip} (a height, not a duration)")
    console.say(f"policy: initiator {why['initiator_hours']}h, participant {why['participant_hours']}h "
                f"(scale={args.hours_scale}); GRC {why['chain_blocks']} blocks at an estimated "
                f"{why['chain_seconds_per_block']}s")
    try:
        console.check("timelock ordering", assert_timelock_ordering(xrp_cancel_after, leg, now,
                                                                   direction=args.direction),
                      "the participant's leg to expire first", True)
    except SystemExit as refusal:
        console.check("timelock ordering", str(refusal), "the participant's leg to expire first", False)
        return console.summary()

    if not args.run:
        describe_the_dry_run(console, args, chain, leg, chain_amount,
                             xrp_cancel_after, a_xrp, b_xrp, a_grc)
        return console.summary()

    # PER CHAIN, because the wallet that signs is per chain. This read
    # GRC_WALLET_PASSPHRASE unconditionally until 2026-09-29, so a --chain btc run would
    # have asked for a Gridcoin passphrase, been handed one, and offered it to bitcoind.
    # A name built from the chain is the only version that cannot do that.
    #
    # THE VALUE IS NEVER PRINTED, LOGGED, OR PUT ON A COMMAND LINE -- only the NAME of the
    # variable is, which is what an operator needs to set it. argv is world-readable
    # through /proc and `ps`, so a passphrase reaching this process any other way would be
    # a disclosure this file cannot undo.
    resolved = resolve_wallet_passphrase(console, grc, chain)
    if resolved is None:
        return console.summary()
    passphrase, wallet_encrypted = resolved

    submitter = Submitter(console.say)

    def submit_xrp(tx_json: dict, secret_for_this_tx: str) -> dict:
        """One XRPL submission, signed with the secret the CALLER names.

        The secret is a parameter rather than a closure over one account,
        because the CHAIN_FIRST direction has the escrow CREATED by one account
        and FINISHED by the other -- two secrets in one run. Closing over a
        single secret worked for XRP_FIRST and would have signed the finish as
        the wrong party here, which the ledger answers with a bare
        `badSecret`/invalid signature rather than anything that names the cause.
        """
        try:
            return submitter.submit(tx_json, secret_for_this_tx)
        except LocalSigningUnavailable as error:
            return {"error": "localSigningUnavailable", "error_message": str(error)}
    prepared = prepare_the_script_leg(console, chain)
    if prepared is None:
        return console.summary()
    script_client, leg_keys = prepared

    ctx = SwapContext(
        console=console, chain=chain, grc=grc, submit_xrp=submit_xrp,
        secret=secret, secret_hash=secret_hash, condition=condition,
        a_xrp=a_xrp, a_xrp_secret=a_xrp_secret, b_xrp=b_xrp, b_xrp_secret=b_xrp_secret,
        a_grc=a_grc, b_grc=b_grc,
        chain_amount=chain_amount, chain_timeout=leg.timeout_height, xrp_cancel_after=xrp_cancel_after,
        passphrase=passphrase, wallet_encrypted=wallet_encrypted,
        script_client=script_client, leg_keys=leg_keys,
    )
    runner = RUNNERS[args.direction]
    console.say(f"direction={args.direction}: running {runner.__name__}()")
    runner(ctx)
    return console.summary()


if __name__ == "__main__":
    sys.exit(main())
