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
    preimage_fulfillment,
)
from chains.xrp_submit import LocalSigningUnavailable, Submitter  # noqa: E402 -- same
from chains.xrp_testnet import TESTNET_URL, refuse_mainnet, saved_faucet_accounts  # noqa: E402 -- same
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

# What each leg moves. Small, and both sides are testnet play money.
XRP_DROPS = 1_000_000          # 1 XRP
GRC_AMOUNT = "1.0"             # 1 GRC

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


def swap_timelocks(now_unix: float, grc_tip_height: int, *, hours_scale: float = 1.0) -> tuple[int, int, dict]:
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
    initiator_hours = lock_hours_for_role(ROLE_INITIATOR) * hours_scale
    participant_hours = lock_hours_for_role(ROLE_PARTICIPANT) * hours_scale
    xrp_cancel_after = ripple_time(now_unix + initiator_hours * SECONDS_PER_HOUR)
    grc_blocks = int(participant_hours * SECONDS_PER_HOUR // SECONDS_PER_BLOCK["GRC"])
    grc_timeout = grc_tip_height + grc_blocks
    return xrp_cancel_after, grc_timeout, {
        "initiator_hours": initiator_hours,
        "participant_hours": participant_hours,
        "grc_blocks": grc_blocks,
        "grc_seconds_per_block": SECONDS_PER_BLOCK["GRC"],
    }


def assert_timelock_ordering(xrp_cancel_after: int, grc_timeout_height: int, grc_tip: int, now_unix: float) -> str:
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
    margin_seconds = xrp_expiry_unix - grc_expiry_unix
    if margin_seconds <= 0:
        raise SystemExit(
            "REFUSED before funding anything: the participant's GRC leg would expire at or after the "
            f"initiator's XRP leg (GRC in {format_duration(grc_expiry_unix - now_unix)}, XRP in "
            f"{format_duration(xrp_expiry_unix - now_unix)}). In that order the initiator can take the GRC and "
            "then refund the XRP, and the participant has no recovery. Nothing was submitted."
        )
    return (
        f"timelock ordering OK: the GRC leg expires in {format_duration(grc_expiry_unix - now_unix)} "
        f"(height {grc_timeout_height}, {grc_timeout_height - grc_tip} blocks at an ESTIMATED "
        f"{SECONDS_PER_BLOCK['GRC']}s), the XRP leg in {format_duration(xrp_expiry_unix - now_unix)} "
        f"(CancelAfter {xrp_cancel_after}), a margin of {format_duration(margin_seconds)} in the participant's "
        "favour. The GRC figure rests on a target interval, not a guarantee."
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


def main() -> int:  # noqa: C901, PLR0911, PLR0912, PLR0915 -- checked: this is the swap's SEQUENCE, and every decision in it is extracted -- the timelocks and their ordering above, the preimage read in modules/htlc_spend, the condition in chains/xrp_crypto_condition, the payloads in xrp_htlc_escrow. What is left is the order of five acts on two chains, which is what rule 10 says a file at the root is for. Splitting it would put the order somewhere other than the file named after the thing being done, and the order IS the protocol.
    parser = argparse.ArgumentParser(
        description="A real atomic swap: XRP on the XRPL testnet against GRC on the Gridcoin testnet, "
                    "interlocked by one sha256 preimage. Testnet only, structurally.",
    )
    parser.add_argument("--run", action="store_true",
                        help="actually submit. Without it every step is described and nothing is sent")
    parser.add_argument("--hours-scale", type=float, default=1.0,
                        help="shorten BOTH legs by this factor for a demonstration (default 1.0 = the real "
                             "48h/24h policy). It scales both, so the 2:1 ordering is preserved")
    args = parser.parse_args()

    console = Console(total_steps=9)
    console.banner("ATOMIC SWAP -- XRP (XRPL testnet) for GRC (Gridcoin testnet)")
    console.say(f"XRP endpoint={TESTNET_URL}")
    console.say(f"mode={'--run: BOTH LEGS WILL BE FUNDED' if args.run else 'DRY RUN: nothing is submitted'}")
    console.say("A holds XRP and wants GRC (the INITIATOR, longer lock). B holds GRC and wants XRP (the "
                "PARTICIPANT, shorter lock).")
    console.say("one operator plays both parties here, so counterparty misbehavior is NOT exercised -- the "
                "mechanism is, on real chains. See this file's header.")

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
    (_, a_xrp, a_xrp_secret), (_, b_xrp, _b_secret) = accounts[0], accounts[1]
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

    console.step(4, "the two timelocks, in the two chains' different clocks")
    tip = int(grc.call("getblockcount"))
    now = time.time()
    xrp_cancel_after, grc_timeout, why = swap_timelocks(now, tip, hours_scale=args.hours_scale)
    console.say(f"GRC tip={tip} (a height, not a duration)")
    console.say(f"policy: initiator {why['initiator_hours']}h, participant {why['participant_hours']}h "
                f"(scale={args.hours_scale}); GRC {why['grc_blocks']} blocks at an estimated "
                f"{why['grc_seconds_per_block']}s")
    try:
        console.check("timelock ordering", assert_timelock_ordering(xrp_cancel_after, grc_timeout, tip, now),
                      "the GRC leg to expire first", True)
    except SystemExit as refusal:
        console.check("timelock ordering", str(refusal), "the GRC leg to expire first", False)
        return console.summary()

    if not args.run:
        console.banner("DRY RUN -- nothing was submitted")
        console.say(f"step 5 would fund the XRP leg: Escrow of {XRP_DROPS} drops from {a_xrp} to {b_xrp}, "
                    f"Condition above, CancelAfter {xrp_cancel_after}.")
        console.say(f"step 6 would fund the GRC leg: createhtlc receiver={a_grc} sender={b_grc} "
                    f"hash={secret_hash.hex()} timeout={grc_timeout} amount={GRC_AMOUNT}.")
        console.say("step 7 would claim the GRC with the secret; step 8 would read the secret back OFF THE "
                    "GRIDCOIN CHAIN; step 9 would finish the XRP escrow with what step 8 read.")
        console.say("re-run with --run to perform the swap.")
        return console.summary()

    passphrase = os.environ.get("GRC_WALLET_PASSPHRASE", "")
    if not console.check("GRC_WALLET_PASSPHRASE present", "yes" if passphrase else None,
                         "set, because claimhtlc signs", bool(passphrase)):
        console.say("Nothing was submitted. The value is never printed or logged.")
        return console.summary()

    submitter = Submitter(console.say)

    def submit_xrp(tx_json: dict) -> dict:
        try:
            return submitter.submit(tx_json, a_xrp_secret)
        except LocalSigningUnavailable as error:
            return {"error": "localSigningUnavailable", "error_message": str(error)}

    console.step(5, f"A funds the XRP leg: {XRP_DROPS} drops to B, hashlocked and timelocked")
    b_before = balance_drops(b_xrp)
    created = submit_xrp(escrow_create_tx(a_xrp, b_xrp, XRP_DROPS, condition, xrp_cancel_after))
    if not console.check("XRP leg funded", describe_result(created), "tesSUCCESS",
                         engine_result(created) == "tesSUCCESS"):
        return console.summary()
    escrow_sequence = (created.get("tx_json") or {}).get("Sequence")
    wait_validated(console, (created.get("tx_json") or {}).get("hash", ""))
    console.say(f"OfferSequence={escrow_sequence} -- how the finish in step 9 names this escrow")

    console.step(6, f"B funds the GRC leg: {GRC_AMOUNT} GRC, same hash, expiring FIRST")
    try:
        htlc = grc.call("createhtlc", a_grc, b_grc, secret_hash.hex(), grc_timeout, float(GRC_AMOUNT))
    except Exception as error:  # noqa: BLE001 -- checked: createhtlc refuses for several named reasons (a locked wallet, a pubkey not in the wallet, insufficient funds) and the message says which. It is a FAIL rather than a raise because THE XRP LEG IS ALREADY FUNDED at this point, and the operator needs the recovery line below rather than a traceback.
        console.check("GRC leg funded", f"{type(error).__name__}: {error}", "a funded HTLC", False)
        console.say(f"THE XRP LEG IS FUNDED AND THE GRC LEG IS NOT. Nothing is lost: nobody has the secret, so "
                    f"nobody can finish the escrow, and it returns to A at CancelAfter {xrp_cancel_after}. Do "
                    f"NOT publish the secret.")
        return console.summary()
    console.check("GRC leg funded", f"p2sh={htlc.get('address') or htlc.get('p2sh')} txid={htlc.get('txid')}",
                  "a funded HTLC", bool(htlc.get("txid")))
    console.say(f"GRC redeem script={htlc.get('redeemScript') or htlc.get('redeem_script')}")

    console.step(7, "A claims the GRC with the secret -- which PUBLISHES it")
    console.say("this is the irreversible step for A: claiming requires pushing the secret into a scriptSig that "
                "lands in a block. A cannot take the GRC without giving B what B needs.")
    # LAZY, and PLC0415 is suppressed for one checked reason written here rather
    # than on the line: the dry run must not touch the unlock path at all, and a
    # module-scope import would run gridcoin_wallet_lock's environment read on
    # every invocation including --help. The reason lives above the import
    # because the sorter re-wraps a long trailing comment and detaches it from
    # the line it is about, which is how a suppression's justification drifts.
    from chains.gridcoin_wallet_lock import unlocked_for_payout  # noqa: PLC0415
    try:
        with unlocked_for_payout(grc, passphrase):
            claim = grc.call("claimhtlc", htlc["txid"], int(htlc.get("vout", 0)), secret.hex(), a_grc)
    except Exception as error:  # noqa: BLE001 -- checked: claimhtlc refuses on a wrong preimage, a missing key or a script failure, and the unlock can fail on its own; both are reported with the recovery line below because BOTH legs are funded at this point, which is the state where an operator most needs to be told what is safe.
        console.check("A claimed the GRC", f"{type(error).__name__}: {error}", "a broadcast txid", False)
        console.say("BOTH LEGS ARE FUNDED AND NEITHER IS CLAIMED. The secret has NOT been published, so the "
                    f"escrow cannot be finished by anyone: B recovers the GRC at height {grc_timeout} and A "
                    f"recovers the XRP at CancelAfter {xrp_cancel_after}. Do NOT publish the secret.")
        return console.summary()
    claim_txid = claim.get("txid") if isinstance(claim, dict) else str(claim)
    console.check("A claimed the GRC", f"txid={claim_txid}", "a broadcast txid", bool(claim_txid))

    console.step(8, "B reads the secret OFF THE GRIDCOIN CHAIN -- never from A")
    console.say("this is the step that makes the swap atomic. B does not ask A for anything, and A cannot "
                "refuse: the secret is in A's own claim transaction.")
    revealed = None
    for attempt in range(1, READ_ATTEMPTS + 1):
        script_sig_hex, reasons = claim_scriptsig_hex(grc, claim_txid)
        if script_sig_hex:
            revealed = preimage_from_scriptsig(bytes.fromhex(script_sig_hex), secret_hash)
            if revealed is not None:
                console.say(f"attempt {attempt}: read the claim's scriptSig ({len(script_sig_hex) // 2} bytes) and "
                            f"one of its pushes hashes to the commitment")
                break
            # READ BUT NO MATCH is a different answer from COULD NOT READ, and
            # the two must not print the same line (rule 14). This one means the
            # transaction is there and does not carry the preimage.
            console.say(f"attempt {attempt}: read the scriptSig, but NO push hashes to the commitment -- this is "
                        f"not a claim of this contract")
        else:
            console.say(f"attempt {attempt}: could not read the claim yet ({'; '.join(reasons) or '(none)'})")
        time.sleep(READ_POLL_SECONDS)
    if not console.check("the secret was recovered from the chain", "yes" if revealed else None,
                         "a push whose sha256 matches the commitment", revealed is not None):
        console.say(f"B cannot finish the escrow without it and recovers the GRC at height {grc_timeout}... "
                    f"except that A HAS ALREADY CLAIMED the GRC. Read {claim_txid} by hand; the secret is in it.")
        return console.summary()
    # THE ASSERTION THAT THE READ IS REAL. `revealed` came from the chain and
    # `secret` from memory, and they must be equal -- if this file ever finished
    # the escrow using `secret` directly it would still WORK here, while proving
    # nothing about atomicity, because a real B has no `secret` variable.
    console.check("what the chain gave B equals what A committed to", revealed == secret, "True", revealed == secret)

    console.step(9, "B finishes the XRP escrow with the secret it read")
    fulfillment = preimage_fulfillment(revealed)
    fee = finish_fee_drops(fulfillment)
    finished = submit_xrp(escrow_finish_tx(a_xrp, a_xrp, escrow_sequence,
                                          condition=condition, fulfillment=fulfillment, fee=fee))
    if console.check("XRP leg claimed", describe_result(finished), "tesSUCCESS",
                     engine_result(finished) == "tesSUCCESS"):
        wait_validated(console, (finished.get("tx_json") or {}).get("hash", ""))
        b_after = balance_drops(b_xrp)
        # THE BALANCES, not the engine results. Two tesSUCCESS codes say two
        # transactions applied; the balances say the swap happened.
        console.check("B's XRP balance rose by the escrowed amount",
                      f"{b_before} -> {b_after} drops (+{b_after - b_before})", f"+{XRP_DROPS}",
                      b_after - b_before == XRP_DROPS)

    console.banner("WHAT CHANGED HANDS")
    console.say(f"GRC: {GRC_AMOUNT} from B's wallet to {a_grc}, claimed with the secret (txid {claim_txid})")
    console.say(f"XRP: {XRP_DROPS} drops from {a_xrp} to {b_xrp}, released by the same secret")
    console.say("interlocked by one sha256, with neither party ever sending the other the preimage.")
    return console.summary()


if __name__ == "__main__":
    sys.exit(main())
