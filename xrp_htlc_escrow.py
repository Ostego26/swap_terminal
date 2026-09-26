#!/usr/bin/env python3
"""Prove -- or refute -- an HTLC on the XRP Ledger, using Escrow and a crypto-condition.

Role: file (the entry point; the decisions live in
      swap_terminal/chains/xrp_crypto_condition.py and the RPC in
      chains/xrp_testnet.py)
Reads: the XRPL TESTNET JSON-RPC endpoint in chains/xrp_testnet.py, and the
       faucet accounts in ~/.config/swap_terminal/keys/xrp-testnet-*.json
Writes: nothing on disk. It SUBMITS transactions -- EscrowCreate, EscrowFinish,
       EscrowCancel -- on the XRP testnet, and only with --run.
Can move funds: YES, testnet XRP between two faucet accounts the operator
       already holds. Never mainnet: refuse_mainnet() asks the server for its
       network_id and aborts on a mainnet one, before anything is submitted.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED, the same unconditional shape
       regtest_htlc_verify.py uses. The endpoint is not a flag.

NOT YET RUN AGAINST A LEDGER. SAY SO BEFORE QUOTING IT.

The container this was written in cannot reach the XRP testnet: both
s.altnet.rippletest.net:51234 and faucet.altnet.rippletest.net are refused by
its egress proxy (403 on CONNECT, measured 2026-09-26). So every claim in this
file about what XRPL will DO is a reading of the protocol, not a measurement,
and CLAUDE.md rule 17 says which one you are holding matters more than being
right. The encoding underneath it is a different story and IS measured -- see
chains/xrp_crypto_condition.py, whose output was compared byte for byte against
an independent implementation across five preimage lengths.

The first operator run settles it. Until then this is a proposal with a
verifier attached, exactly as rule 16 defines one.

WHY XRPL CAN DO THIS AT ALL, WHICH IS NOT OBVIOUS.

There is no Bitcoin script on the XRP Ledger, so the redeem script
modules/atomic_htlc_scripts.build_htlc_redeem_script() builds cannot be
expressed. XRPL's Escrow object carries the two fields separately instead:

    Condition     a PREIMAGE-SHA-256 crypto-condition. An EscrowFinish is
                  rejected unless it carries a Fulfillment whose SHA-256
                  matches it. THE HASHLOCK.
    CancelAfter   a time after which anybody may EscrowCancel and the XRP
                  returns to the sender. THE TIMELOCK.

And the two legs interlock, which is the whole reason a cross-chain swap is
possible: the condition commits to sha256(preimage), and so does `OP_SHA256
<hash> OP_EQUALVERIFY` in the BTC and LTC redeem script. ONE preimage opens both
sides. tests/test_xrp_crypto_condition.py asserts that the digest in the
condition is the identical sha256 the scripts hash, because if either leg ever
moved to HASH160 the swap would look complete on each chain separately and the
preimage revealed on one would not open the other.

WHAT NINE STEPS ARE FOR. Same shape as regtest_htlc_verify.py, deliberately
(rule 11: one vocabulary), because the questions are the same ones:

    1  the endpoint is a test network, and it says which
    2  two funded faucet accounts exist on this machine
    3  a 32-byte secret and its condition (the condition is printed; the
       secret never is)
    4  EscrowCreate [A] with Condition and CancelAfter
    5  EscrowFinish with a WRONG fulfillment must be REFUSED -- the hashlock
       actually locking, rather than the escrow merely existing
    6  EscrowFinish with the RIGHT fulfillment must SUCCEED, and the
       destination's balance must rise
    7  EscrowCreate [B], a second escrow for the refund path -- never the one
       step 6 already finished
    8  EscrowCancel BEFORE CancelAfter must be REFUSED
    9  EscrowCancel AFTER CancelAfter must SUCCEED, and the sender's balance
       must come back

Step 5 is the one that makes this more than a demo. An escrow that pays out to
any fulfillment is not a hashlock, and an escrow that pays out with no
fulfillment at all is not even an escrow -- both would produce a green step 6.

THE FEE ON AN EscrowFinish IS NOT THE DEFAULT, and this is the detail most
likely to produce a confusing failure. XRPL charges a finish carrying a
fulfillment 330 drops plus 10 drops per 16 bytes of it, so rippled's autofill --
which fills the ordinary 10-drop reference fee -- produces telINSUF_FEE_P. The
fee is therefore set explicitly here and the arithmetic is shown on screen.

HOW TO RUN IT

    cd <repo>
    source .venv/bin/activate
    python3 xrp_htlc_escrow.py            # describes every step, submits nothing
    python3 xrp_htlc_escrow.py --run      # submits, on testnet

It needs TWO faucet accounts in ~/.config/swap_terminal/keys/. fund_testnets.py
makes them. Step 9 waits for CancelAfter to pass, so a run takes at least
--cancel-after seconds of wall clock; the default keeps that under two minutes
and the step says how long it is waiting and why.

THE SECRET IS NEVER PRINTED. Not the preimage, not the fulfillment that contains
it, not at any verbosity. The condition and its sha256 are printed, because both
are public by construction -- the condition goes on a public ledger in step 4.
The same rule modules/htlc_spend.py follows for a scriptSig.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"
if str(APP_ROOT) not in sys.path:
    # Same rootless-import gap regtest_htlc_verify.py documents: the application
    # imports its own modules as `from chains... import`, so swap_terminal/ has
    # to be importable. CLAUDE.md rule 10's layout debt, not a new one.
    sys.path.insert(0, str(APP_ROOT))

from chains.xrp_crypto_condition import (  # noqa: E402 -- the sys.path line above must run first
    HTLC_PREIMAGE_BYTES,
    condition_matches_preimage,
    preimage_condition,
    preimage_fulfillment,
)
from chains.xrp_testnet import TESTNET_URL, refuse_mainnet, rpc, saved_faucet_accounts  # noqa: E402 -- same
from microfortnights import format_duration  # noqa: E402 -- same

# Ripple's epoch is 2000-01-01T00:00:00Z, which is this many seconds after the
# Unix epoch. CancelAfter and FinishAfter are in RIPPLE seconds, and handing
# XRPL a Unix timestamp instead produces an escrow whose timelock expired 30
# years ago -- immediately cancellable by anyone, which on a real swap is the
# counterparty's money walking away.
RIPPLE_EPOCH_OFFSET_SECONDS = 946_684_800

# What each escrow locks. Small on purpose: two faucet accounts hold 100 XRP
# each and XRPL's base reserve must stay free, so this is a demonstration
# amount rather than a stress test.
ESCROW_DROPS = 1_000_000  # 1 XRP

# Two accounts, and the reason each is needed: one to send the escrow and one to
# be its destination. Named rather than inline so the assertion in step 2 and the
# message that explains it cannot disagree about the number.
ACCOUNTS_NEEDED = 2

# EscrowFinish's fee floor, from the protocol: 330 drops plus 10 per 16 bytes of
# fulfillment. Computed rather than spelled, and printed, because autofill gets
# this wrong and telINSUF_FEE_P does not say which of the two numbers is short.
FINISH_BASE_FEE_DROPS = 330
FINISH_FEE_DROPS_PER_16_BYTES = 10

# How long to wait for a submitted transaction to appear in a validated ledger.
# Testnet ledgers close in roughly 4 seconds, so this is many ledgers' worth --
# a timeout here means the transaction did not make it, not that it was slow.
VALIDATION_TIMEOUT_SECONDS = 60.0
VALIDATION_POLL_SECONDS = 2.0


def finish_fee_drops(fulfillment_hex: str) -> int:
    """The minimum fee for an EscrowFinish carrying this fulfillment.

    The fulfillment is counted in BYTES, so the hex string's length is halved
    first -- a fee sized from the hex length would be double, which is harmless,
    and sized from the preimage length instead of the encoded fulfillment would
    be short by the four framing bytes, which is telINSUF_FEE_P.
    """
    size_bytes = len(fulfillment_hex) // 2
    chunks = -(-size_bytes // 16)  # ceiling division; a partial 16 bytes still costs
    return FINISH_BASE_FEE_DROPS + FINISH_FEE_DROPS_PER_16_BYTES * chunks


def ripple_time(unix_seconds: float) -> int:
    """A Unix timestamp as XRPL's own clock. See RIPPLE_EPOCH_OFFSET_SECONDS."""
    return int(unix_seconds) - RIPPLE_EPOCH_OFFSET_SECONDS


class Console:
    """Announce before acting, print what was expected beside what arrived.

    A second small console rather than regtest/console.py's: that one is built
    around nine steps on two chains with a spawned daemon, and importing it here
    would drag its ChainConfig with it. The OUTPUT CONVENTIONS are the same on
    purpose -- microfortnights with seconds in parentheses (rule 6), `(none)` for
    an empty result (rule 14), and every assertion printing got beside expected.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.results: list[tuple[str, bool]] = []

    def _elapsed(self) -> str:
        return format_duration(time.monotonic() - self.started)

    def banner(self, text: str) -> None:
        print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78, flush=True)

    def step(self, number: int, title: str) -> None:
        print(f"\nstep {number}/9  {title}   [{self._elapsed()}]", flush=True)

    def say(self, text: str) -> None:
        print(f"          {text}", flush=True)

    def check(self, label: str, got: object, expected: object, ok: bool) -> bool:
        shown = got if got not in (None, "", [], {}) else "(none)"
        print(f"          {'OK  ' if ok else 'FAIL'}  {label}: got={shown}  expected={expected}  "
              f"[{self._elapsed()}]", flush=True)
        self.results.append((label, ok))
        return ok

    def summary(self) -> int:
        self.banner("SUMMARY")
        failures = [label for label, ok in self.results if not ok]
        print(f"  OK={len(self.results) - len(failures)}  FAIL={len(failures)}", flush=True)
        if failures:
            print("\n  unexpected failures, in the order they happened:", flush=True)
            for label in failures:
                print(f"    - {label}", flush=True)
        else:
            print("\n  unexpected failures: (none)", flush=True)
        return 1 if failures else 0


def submit(console: Console, secret: str, tx_json: dict) -> dict:
    """Submit one transaction through rippled's legacy server-side signing.

    The same mechanism xrp_send_tagged.submit_payment() uses, and for the same
    reason: nothing in this repository signs an XRPL transaction locally unless
    xrpl-py happens to be installed. It announces the transaction type and the
    fee BEFORE submitting (rule 14), and it NEVER prints tx_json verbatim --
    an EscrowFinish's tx_json contains the Fulfillment, which is the secret.
    """
    kind = tx_json.get("TransactionType", "(none)")
    console.say(f"submitting {kind}: Fee={tx_json.get('Fee', '(autofilled)')} drops "
                f"(Fulfillment is never printed)")
    result = rpc("submit", {"secret": secret, "tx_json": tx_json})
    status = str(result.get("engine_result") or result.get("error") or "(none)")
    message = result.get("engine_result_message") or result.get("error_message") or "(no message)"
    console.say(f"{kind} -> {status}: {message}")
    return result


def wait_validated(console: Console, tx_hash: str) -> dict:
    """Poll until the transaction is in a VALIDATED ledger, or give up saying so.

    engine_result is a PROVISIONAL answer from one server about what it expects
    to happen. tesSUCCESS from `submit` is not the ledger agreeing, and reporting
    it as a completed escrow is the same overclaim regtest_htlc_verify.py's step
    8 exists to avoid on the other chains: it is the mempool's opinion, not the
    chain's.
    """
    deadline = time.monotonic() + VALIDATION_TIMEOUT_SECONDS
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        found = rpc("tx", {"transaction": tx_hash})
        if found.get("validated"):
            console.say(f"{tx_hash[:16]}... validated after {polls} polls, {console._elapsed()}")
            return found
        console.say(f"waiting for validation: poll {polls}, not yet in a validated ledger")
        time.sleep(VALIDATION_POLL_SECONDS)
    console.say(f"gave up waiting for {tx_hash[:16]}... after "
                f"{format_duration(VALIDATION_TIMEOUT_SECONDS)}")
    return {}


def balance_drops(address: str) -> int:
    """One account's XRP balance in drops, or -1 if the account is not funded."""
    info = rpc("account_info", {"account": address, "ledger_index": "validated"})
    account = info.get("account_data") or {}
    return int(account.get("Balance", -1))


def main() -> int:  # noqa: PLR0915 -- checked: this is the nine-step SEQUENCE, and the decisions it makes are all extracted already (the encoding in chains/xrp_crypto_condition.py, the fee in finish_fee_drops, the clock in ripple_time, the RPC in chains/xrp_testnet.py). What remains is order and reporting, which is what rule 10 says a file at the root is FOR. regtest_htlc_verify.py's run_chain carries the same shape for the same reason. Splitting it would put the steps' order somewhere other than the file named after the thing being verified.
    parser = argparse.ArgumentParser(
        description="Prove an HTLC on the XRP Ledger with Escrow plus a PREIMAGE-SHA-256 condition. "
                    "Testnet only, structurally.",
    )
    parser.add_argument("--run", action="store_true",
                        help="actually submit. Without it, every step is described and nothing is sent")
    parser.add_argument("--cancel-after", type=int, default=90,
                        help="seconds until the escrow's timelock expires (default 90). Step 9 waits it out")
    args = parser.parse_args()

    console = Console()
    console.banner("XRP LEDGER HTLC verification -- Escrow with a PREIMAGE-SHA-256 condition")
    console.say(f"endpoint={TESTNET_URL}")
    console.say(f"mode={'--run: transactions WILL be submitted on testnet' if args.run else 'DRY RUN: nothing is submitted'}")
    console.say("NOT PREVIOUSLY RUN AGAINST A LEDGER -- see this file's header. Until it is, every")
    console.say("expectation printed below is read from the protocol, not measured (rule 17).")

    console.step(1, "the endpoint is a TEST network, and it says which")
    try:
        console.check("network", refuse_mainnet(), "a non-mainnet network_id", True)
    except Exception as error:  # noqa: BLE001 -- checked: there are two failures here and BOTH have to arrive as a labeled FAIL rather than as a traceback. refuse_mainnet() raises RuntimeError when the server says mainnet; requests raises ConnectionError, HTTPError or a timeout when the endpoint cannot be reached at all -- which is what happens in a container whose egress proxy refuses this host, measured 2026-09-26. A bare traceback at step 1 of nine is exactly the round trip this harness exists to avoid (rule 14), and nothing here treats the failure as a pass: it returns non-zero through the summary.
        console.check("network", f"{type(error).__name__}: {error}", "a non-mainnet network_id", False)
        console.say(f"nothing was submitted. If this is a connection failure rather than a mainnet refusal, "
                    f"{TESTNET_URL} is unreachable from this machine -- check egress before reading anything "
                    f"into it. This script cannot run where the ledger cannot be reached, and that is the "
                    f"reason its own header says it has never been run.")
        return console.summary()

    console.step(2, "two funded faucet accounts on this machine")
    accounts = saved_faucet_accounts()
    if not console.check("faucet accounts found", len(accounts), f">= {ACCOUNTS_NEEDED}", len(accounts) >= ACCOUNTS_NEEDED):
        console.say(f"fund_testnets.py creates them. {ACCOUNTS_NEEDED} are needed: one sends the escrow, one receives it.")
        return console.summary()
    (_, sender, sender_secret), (_, receiver, _receiver_secret) = accounts[0], accounts[1]
    console.say(f"sender={sender}  receiver={receiver}")
    console.say("NOTE the escrow's DESTINATION is the receiver, but an EscrowFinish may be submitted by "
                "anyone -- it is sent here by the sender, which is why only one secret is needed.")

    console.step(3, f"a {HTLC_PREIMAGE_BYTES}-byte secret and its condition")
    preimage = os.urandom(HTLC_PREIMAGE_BYTES)
    condition = preimage_condition(preimage)
    fulfillment = preimage_fulfillment(preimage)
    console.say(f"condition={condition}")
    console.say("the preimage and the fulfillment are NOT printed, here or anywhere below. The condition is,")
    console.say("because step 4 puts it on a public ledger.")
    console.check("the condition commits to this preimage",
                  condition_matches_preimage(condition, preimage), "True", 
                  condition_matches_preimage(condition, preimage))
    fee = finish_fee_drops(fulfillment)
    console.say(f"EscrowFinish fee = {FINISH_BASE_FEE_DROPS} + {FINISH_FEE_DROPS_PER_16_BYTES} per 16 bytes of a "
                f"{len(fulfillment) // 2}-byte fulfillment = {fee} drops (autofill would send the 10-drop "
                f"reference fee and earn telINSUF_FEE_P)")

    cancel_after = ripple_time(time.time() + args.cancel_after)
    console.say(f"CancelAfter={cancel_after} (RIPPLE seconds, {args.cancel_after}s from now; a Unix timestamp "
                f"here would expire the escrow 30 years ago)")

    if not args.run:
        console.banner("DRY RUN -- nothing was submitted")
        console.say(f"steps 4 to 9 would submit: EscrowCreate {ESCROW_DROPS} drops from {sender} to {receiver} "
                    f"with the condition above; an EscrowFinish with a WRONG fulfillment (must be refused); one "
                    f"with the right one (must succeed); a second EscrowCreate; an EscrowCancel before "
                    f"CancelAfter (must be refused); and one after it (must succeed).")
        console.say("re-run with --run to submit them.")
        return console.summary()

    before = balance_drops(receiver)
    console.step(4, f"EscrowCreate [A]: {ESCROW_DROPS} drops with the condition and the timelock")
    created = submit(console, sender_secret, {
        "TransactionType": "EscrowCreate",
        "Account": sender,
        "Destination": receiver,
        "Amount": str(ESCROW_DROPS),
        "Condition": condition,
        "CancelAfter": cancel_after,
    })
    tx_json = created.get("tx_json") or {}
    escrow_sequence = tx_json.get("Sequence")
    if not console.check("EscrowCreate [A] accepted", created.get("engine_result"), "tesSUCCESS",
                         created.get("engine_result") == "tesSUCCESS"):
        return console.summary()
    console.say(f"OfferSequence for the finish/cancel below = {escrow_sequence} (the CREATE's Sequence; "
                f"EscrowFinish names the escrow by it, not by a hash)")
    wait_validated(console, tx_json.get("hash", ""))

    console.step(5, "EscrowFinish with a WRONG fulfillment must be REFUSED -- this is the hashlock")
    wrong = preimage_fulfillment(bytes(HTLC_PREIMAGE_BYTES))  # a fulfillment for all-zero bytes
    console.say("submitting a fulfillment for a DIFFERENT preimage. An escrow that accepts this is not a "
                "hashlock, and step 6 would be green either way.")
    refused = submit(console, sender_secret, {
        "TransactionType": "EscrowFinish",
        "Account": sender,
        "Owner": sender,
        "OfferSequence": escrow_sequence,
        "Condition": condition,
        "Fulfillment": wrong,
        "Fee": str(fee),
    })
    wrong_result = str(refused.get("engine_result") or refused.get("error"))
    console.check("a wrong fulfillment is refused", wrong_result, "tecCRYPTOCONDITION_ERROR (any tec/tem is a refusal)",
                  wrong_result.startswith(("tec", "tem")))

    console.step(6, "EscrowFinish with the RIGHT fulfillment must SUCCEED")
    finished = submit(console, sender_secret, {
        "TransactionType": "EscrowFinish",
        "Account": sender,
        "Owner": sender,
        "OfferSequence": escrow_sequence,
        "Condition": condition,
        "Fulfillment": fulfillment,
        "Fee": str(fee),
    })
    ok_finish = console.check("the right fulfillment finishes the escrow", finished.get("engine_result"),
                              "tesSUCCESS", finished.get("engine_result") == "tesSUCCESS")
    if ok_finish:
        wait_validated(console, (finished.get("tx_json") or {}).get("hash", ""))
        after = balance_drops(receiver)
        # THE BALANCE, not the engine result. A txid says a transaction was
        # accepted; only the destination's balance says the escrow paid out --
        # the same distinction regtest_htlc_verify.py draws between a broadcast
        # redeem and a spend read back off the chain.
        console.check("the destination's balance rose by the escrowed amount",
                      f"{before} -> {after} drops (+{after - before})", f"+{ESCROW_DROPS}",
                      after - before == ESCROW_DROPS)
        console.say("the fulfillment is now PUBLIC on the ledger, which is how the other leg of a swap becomes "
                    "claimable. That is the mechanism, not a leak.")

    console.step(7, "EscrowCreate [B]: a second escrow, for the refund path")
    console.say("a dedicated escrow, never the one step 6 finished -- if an early cancel were wrongly accepted, "
                "the object it destroyed must not be the one the last step depends on.")
    refund_cancel_after = ripple_time(time.time() + args.cancel_after)
    created_b = submit(console, sender_secret, {
        "TransactionType": "EscrowCreate",
        "Account": sender,
        "Destination": receiver,
        "Amount": str(ESCROW_DROPS),
        "Condition": condition,
        "CancelAfter": refund_cancel_after,
    })
    tx_b = created_b.get("tx_json") or {}
    sequence_b = tx_b.get("Sequence")
    if not console.check("EscrowCreate [B] accepted", created_b.get("engine_result"), "tesSUCCESS",
                         created_b.get("engine_result") == "tesSUCCESS"):
        return console.summary()
    wait_validated(console, tx_b.get("hash", ""))
    sender_before_cancel = balance_drops(sender)

    console.step(8, "EscrowCancel BEFORE CancelAfter must be REFUSED -- this is the timelock")
    early = submit(console, sender_secret, {
        "TransactionType": "EscrowCancel",
        "Account": sender,
        "Owner": sender,
        "OfferSequence": sequence_b,
    })
    early_result = str(early.get("engine_result") or early.get("error"))
    console.check("an early cancel is refused", early_result, "tecNO_PERMISSION (any tec/tem is a refusal)",
                  early_result.startswith(("tec", "tem")))

    console.step(9, "EscrowCancel AFTER CancelAfter must SUCCEED")
    # The wait is announced with its reason and its length, because a silent
    # pause here is indistinguishable from a hang and the operator's answer to
    # that is Ctrl-C (rule 14).
    remaining = max(0.0, (refund_cancel_after + RIPPLE_EPOCH_OFFSET_SECONDS) - time.time()) + 5
    console.say(f"waiting {format_duration(remaining)} for the timelock to pass. Nothing is wrong; XRPL compares "
                f"CancelAfter against the LAST CLOSED LEDGER's time, so a few seconds of slack is added.")
    time.sleep(remaining)
    late = submit(console, sender_secret, {
        "TransactionType": "EscrowCancel",
        "Account": sender,
        "Owner": sender,
        "OfferSequence": sequence_b,
    })
    if console.check("a cancel after the timelock succeeds", late.get("engine_result"), "tesSUCCESS",
                     late.get("engine_result") == "tesSUCCESS"):
        wait_validated(console, (late.get("tx_json") or {}).get("hash", ""))
        sender_after = balance_drops(sender)
        console.check("the sender got the escrowed amount back",
                      f"{sender_before_cancel} -> {sender_after} drops", f"about +{ESCROW_DROPS} less fees",
                      sender_after > sender_before_cancel)

    console.banner("WHICH BRANCH OF A FUNDED ESCROW ACTUALLY PAYS")
    console.say("the hashlock: an escrow finished only by the fulfillment that matches its condition, with a "
                "wrong one refused (steps 5 and 6).")
    console.say("the timelock: an escrow cancelled only after CancelAfter, with an early cancel refused "
                "(steps 8 and 9).")
    return console.summary()


if __name__ == "__main__":
    sys.exit(main())
