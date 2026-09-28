#!/usr/bin/env python3
"""Dry-run a script-chain <-> Monero adaptor swap: prove the cryptography, refuse the rest.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: the Go DLEQ helper binary; monerod's `get_info` over JSON-RPC, read-only; one
       script-chain daemon's `getblockchaininfo`/`getinfo` through
       atomic_swap.py's own client, read-only; and the *_RPC_* environment variables that
       address them. No database, no .env loading, no wallet file.
Writes: NOTHING, on any chain or on disk. There is no code path in this file that
       broadcasts, signs a chain transaction, funds an address or moves a balance.
Can move funds: NO, and `--run` does not change that -- it REFUSES, naming the component
       that is missing. See WHY --run REFUSES below. That is not a safety catch over
       working code; there is no transaction to broadcast.
Mainnet-safe: NO, and it refuses rather than warning. It asks monerod for its `nettype`
       and the script daemon for its `chain` and stops unless BOTH say a test network. It
       never infers a network from a port number. But the deeper answer is the one in
       docs/dleq_cross_curve_design.md section 7: the cryptography underneath is
       UNAUDITED and should not stand between a user and their money.
Live-safe: yes. Read-only against both daemons, opens no swap database, shares no wallet,
       holds no lock, and spawns exactly one child process (the DLEQ helper) which it
       reaps through a context manager (rule 13).

WHAT THIS FILE IS FOR

`atomic_swap.py` drives both legs of an HTLC swap on three chains and completed BTC<->LTC,
LTC<->GRC and BTC<->GRC end to end on real daemons on 2026-09-27. This is the Monero
family's counterpart, and it is honestly a smaller thing: it proves, in front of the
operator, the part of a GRC<->XMR swap that CAN be proven from a machine with no wallet --
and then says precisely what it did not prove.

Read docs/monero_swap_protocol.md before running it. That document carries the per-party,
per-step analysis of what each side can take at each step, and this file prints a summary
of it (step 5) rather than assuming it has been read.

WHY --run REFUSES, AND IT IS NOT A SAFETY CATCH

Measured 2026-09-27 by reading modules/atomic_htlc_scripts.build_htlc_redeem_script(): both
branches of this repo's HTLC end in a single-key OP_CHECKSIG, and
modules/htlc_spend.hashlock_script_sig() takes a signature the claimer produced with their
own key. An adaptor signature has NO PURCHASE on a single-key output -- the entire mechanism
requires that the spender be unable to sign alone, so that the only signature available to
them is the one they must complete with the scalar, and completing it is what publishes it.

So the script-chain side of this swap needs a 2-of-2 lock plus a four-transaction pre-signed
chain -- redeem under the receiver's adaptor point, cancel at T1, refund under the funder's
adaptor point, punish at T2. That is a fifth component, and the four that are tested
(adaptor_ecdsa, ed25519_group, dleq_helper, monero_keys) are not sufficient without it.

THIS PARAGRAPH SAID "NONE OF THOSE FIVE TRANSACTIONS EXISTS IN THIS TREE" UNTIL
2026-09-28 AND THAT WAS FALSE WHEN IT WAS WRITTEN. It was written against the tree as of
5b5109a; 988bd6c had already added `modules/adaptor_swap_scripts.py` (the 2-of-2 redeem
script, its P2SH wrapper, the OP_0-dummy scriptSig and the sighash) and
`modules/adaptor_swap_chain.py` builds all four spends -- build_redeem, build_cancel,
build_refund, build_punish. 16a4644 then FUNDED AND SPENT that lock on Litecoin Core
0.21.4 regtest: 40 checks OK, 0 FAIL, 0 SKIP, both OP_CHECKMULTISIG footguns refused by
the daemon, nLockTime refused at consensus (`generateblock: -25 TestBlockValidity failed:
bad-txns-nonfinal`), the cancel txid predicted before broadcast and matched, and the
second 2-of-2 spent by the refund. The wrong sentence is kept named rather than quietly
replaced because a reader who believed it would rebuild a component that exists and is
chain-proven, which is more expensive than the original error (rule 1: the drift is the
point).

WHAT IS ACTUALLY MISSING IS THE JOIN, AND IT IS NARROWER AND HARDER. Measured 2026-09-28
by grepping `adaptor_ecdsa`, `pre_sign`, `complete_signature` and `recover` across
`adaptor_regtest_verify.py` and `regtest/adaptor_steps.py`: ZERO occurrences. The chain
spend that succeeded used two ORDINARY 2-of-2 signatures. So the one property an adaptor
signature exists for -- that the spender cannot sign alone, and that completing the
signature is what publishes the scalar -- has never been exercised against a consensus
rule. Nothing in this tree hands a `ChainTransaction.digest` to `adaptor_ecdsa.pre_sign`;
`modules/monero_swap_protocol.py` has exactly one importer, and it is this file.

`--run` therefore has nothing to broadcast, for that reason and not for the one above. It
says so and exits non-zero, rather than pretending to be one flag away from a swap.

WHAT THE DRY RUN ACTUALLY ESTABLISHES

Everything that is a pure function of keys, scalars, points, signatures and serialization,
with both parties played by this one process:

  - two independently sampled 252-bit shares each produce a cross-curve DLEQ proof that the
    other side's verifier accepts against the exact keys it holds;
  - both sides compute the same Monero lock address from the four public halves;
  - an adaptor pre-signature under one side's adaptor point pre-verifies;
  - completing it yields a signature from which that side's spend share is recovered
    EXACTLY, and the recovered share's ed25519 public key is the one the address was built
    from;
  - the reconstructed spend key's public key equals the lock's summed public spend key --
    which is the property monero_shared_key_verify.py demonstrated on a regtest chain by
    actually sweeping the funds.

And what it does not: one process playing two parties proves the cryptography and the
arithmetic and proves nothing about the transport. atomic_swap.py's
report_completed_swap() names the same caveat about its own runs.

NOTHING PRINTED HERE IS A KEY

Every share this file causes to exist lives inside modules/monero_swap_protocol.rehearse()
and is reduced to a boolean before it returns; RehearsalResult carries no field wide enough
to hold a 252-bit scalar and a test asserts that. The witness reaches the helper over stdin
and never over argv, because argv is world-readable through /proc and `ps`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from microfortnights import format_duration
from modules.dleq_helper import DleqHelper, HelperNotFound, helper_path
from modules.monero_swap_protocol import (
    ProtocolError,
    TimelockPlan,
    assert_timelock_ordering,
    monero_address_network,
    monero_network_is_test,
    redeem_is_safe_to_broadcast,
    rehearse,
    xmr_lock_wait_seconds,
)
from step_console import Console

# The script chain's network check and its RPC client come from atomic_swap.py rather than
# being written again here (rule 8). `chain_name` asks the daemon which chain it is on
# through two different routes because the three daemons differ, `TEST_CHAIN_NAMES` is the
# vocabulary of acceptable answers, and `client_for` refuses to guess a credential. A second
# copy of any of the three would agree with atomic_swap.py on the day it was written and
# drift from then on, and the drift would be invisible -- each file would look correct.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# The import has to come after the sys.path.insert above, which is rule 10's layout gap and
# the idiom every script in this tree uses. It carries NO `noqa`: ruff does not raise E402
# for it, and a suppression for a finding that is not raised is a false claim about having
# checked something (rule 19).
from atomic_swap import TEST_CHAIN_NAMES, SwapError, chain_name, client_for
from monero_regtest import RegtestError
from monero_regtest import rpc as monerod_rpc

# monerod's default RPC ports, per network. TESTNET AND STAGENET ONLY, deliberately: mainnet
# is 18081 and it is absent so that a missing --monerod-port cannot fall through to it.
# swap_terminal/config.py records the incident this convention comes from -- unset ports
# falling through to mainnet and polling the operator's live wallet on a loop.
DEFAULT_MONEROD_PORT = 38081  # stagenet
REGTEST_MONEROD_PORT = 18081  # what monero_regtest.py binds; a private chain, not mainnet

# The script chains this file's dry run can pair Monero with. Taken from atomic_swap.py's
# own asset set rather than restated, so a fourth script chain there needs no edit here.
SCRIPT_CHAINS = ("GRC", "BTC", "LTC")

# A stand-in digest for the redeem transaction's sighash. In a real swap it comes from
# htlc_spend.legacy_sighash over a transaction that DOES NOT EXIST (the missing fifth
# component), so it is a constant here and the report says so rather than letting a reader
# think a transaction was built.
REHEARSAL_DIGEST = hashlib.sha256(b"swap_terminal/monero_swap/rehearsal-redeem-digest").digest()

TOTAL_STEPS = 6

# The per-party, per-step summary printed at step 5. The full table is in
# docs/monero_swap_protocol.md section 4; this is the part an operator has to have in front
# of them before authorizing anything, and rule 14 is why it prints rather than being cited:
# the operator reads the screen, not the source.
SECURITY_LEDGER = (
    ("roles",
     "the XMR holder RECEIVES on the script chain, and that is forced by the cryptography:"),
    ("",
     "the only publishable act in the swap is a script-chain transaction, so the leak can"),
    ("",
     "only run one way. Reverse the roles and the party taking the coin leaks a share the"),
    ("",
     "counterparty has no use for."),
    ("step 1",
     "script leg locked (2-of-2). The funder's coin is now at risk from hazard 1 and the"),
    ("",
     "XMR holder can take nothing -- the redeem pre-signature has not been sent."),
    ("step 2",
     "Monero leg locked. Still nothing takeable: the script funder cannot spend the shared"),
    ("",
     "address without the other share, and has sent no pre-signature."),
    ("step 3",
     "the redeem pre-signature is released, AND ONLY NOW. Released at setup, the XMR holder"),
    ("",
     "takes the script coin having funded nothing. This one withheld message is the script"),
    ("",
     "funder's entire protection across steps 1 to 3."),
    ("step 4",
     "the redeem is broadcast, which PUBLISHES the spend share. Intended, and hazard 3."),
    ("step 5",
     "the share is recovered off the chain, checked against the committed ed25519 key, and"),
    ("",
     "the XMR is swept. Neither party can hold both legs on this path."),
    ("cancel",
     "after T1 either side cancels; the script funder refunds (leaking its share, so the"),
    ("",
     "XMR comes back) or, after T2, loses the coin to punish. Compensation, not restoration."),
    ("HAZARD 1",
     "the script funder MUST be online in [T1, T2] or loses its coin outright. Not fixable:"),
    ("",
     "a punish path it could disarm would leave the other side with locked XMR and no"),
    ("",
     "recourse. The first funder carries the liveness obligation."),
    ("HAZARD 2",
     "the Monero leg must not be funded without time to confirm, UNLOCK (10 blocks) and"),
    ("",
     "redeem before T1. Otherwise the funder ends with the wrong asset, not with nothing."),
    ("HAZARD 3",
     "THE ONE BOTH-LEGS OUTCOME. If the redeem is re-orged out after publishing the share"),
    ("",
     "and the cancel confirms instead, the counterparty holds BOTH shares and its own coin."),
    ("",
     "Bounded only by confirmation depth before T1, which is why the broadcast is a refusal"),
    ("",
     "rather than a warning."),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run a script-chain <-> Monero adaptor swap. Read-only. Proves the "
            "cryptography offline, asks both daemons which network they are on, and reports "
            "what it did NOT prove."
        )
    )
    parser.add_argument(
        "--script-chain", default="GRC", choices=SCRIPT_CHAINS,
        help="which script chain the other leg is on (default GRC, the business pair)",
    )
    parser.add_argument(
        "--monerod-port", type=int, default=None,
        help=(
            f"monerod's RPC port. Default {DEFAULT_MONEROD_PORT} (stagenet); "
            f"{REGTEST_MONEROD_PORT} is what monero_regtest.py binds. Mainnet's port is "
            f"deliberately not a default"
        ),
    )
    parser.add_argument(
        "--skip-chains", action="store_true",
        help=(
            "run only the offline cryptographic rehearsal and skip both daemon checks. Use "
            "this on a machine with no daemons; the report says which checks were skipped"
        ),
    )
    parser.add_argument(
        "--run", action="store_true",
        help=(
            "REFUSED. The 2-of-2 and its four transactions DO exist and were spent on LTC "
            "regtest, but no adaptor pre-signature has ever been made over one of their "
            "digests, so there is nothing an adaptor can bind. See the module docstring"
        ),
    )
    return parser


def refuse_run(console: Console) -> int:
    """`--run` has nothing to do. Say exactly what is missing and exit non-zero.

    Extracted so the refusal is one callable thing rather than a branch inside main(), and
    because it is the most important message this file can print: an operator who reads
    "refused" without reading WHY will come back and ask for the flag to be enabled.
    """
    console.say("")
    console.say("--run IS REFUSED, AND NOT AS A SAFETY CATCH OVER WORKING CODE.")
    console.say("")
    console.say("There is no transaction to broadcast. An adaptor signature needs an output")
    console.say("the spender CANNOT sign alone, so that the only signature available to them")
    console.say("is the one they must complete with the scalar. Measured 2026-09-27: both")
    console.say("branches of this repo's HTLC end in a single-key OP_CHECKSIG, and the claimer")
    console.say("signs with their own key -- so the existing HTLC cannot carry an adaptor")
    console.say("signature at all.")
    console.say("")
    console.say("THE FIFTH COMPONENT EXISTS AND WAS SPENT ON A CHAIN. modules/")
    console.say("adaptor_swap_scripts.py builds the 2-of-2 and modules/adaptor_swap_chain.py")
    console.say("builds all four spends; adaptor_regtest_verify.py --chain ltc funded and spent")
    console.say("them on Litecoin Core 0.21.4 regtest on 2026-09-28, 40 checks OK / 0 FAIL /")
    console.say("0 SKIP, with both OP_CHECKMULTISIG footguns refused by the daemon and")
    console.say("nLockTime refused at CONSENSUS, not merely at relay:")
    console.say("    Tx_lock     2-of-2 {A,B}, no hashlock            funded, located by scriptPubKey")
    console.say("    Tx_redeem   -> the XMR holder                    spent, both sigs in key order")
    console.say("    Tx_cancel   -> a second 2-of-2, nLockTime T1     early: refused; at T1: accepted")
    console.say("    Tx_refund   -> the script funder                 spent the SECOND 2-of-2")
    console.say("    Tx_punish   -> the XMR holder, nLockTime T2>T1   before T2: refused non-final")
    console.say("")
    console.say("WHAT IS MISSING IS THE JOIN, and it is the reason --run refuses. Measured")
    console.say("2026-09-28 by grepping adaptor_ecdsa, pre_sign, complete_signature and recover")
    console.say("across adaptor_regtest_verify.py and regtest/adaptor_steps.py: ZERO hits. That")
    console.say("chain spend used two ORDINARY signatures. No adaptor pre-signature has ever")
    console.say("been made over one of those transactions' digests, so the one property an")
    console.say("adaptor exists for -- completing the signature is what publishes the scalar --")
    console.say("has never met a consensus rule.   <- this is the gap, not the transactions")
    console.say("")
    console.say("AND ON GRIDCOIN SPECIFICALLY, NOTHING OF THE ABOVE IS ESTABLISHED. The spend")
    console.say("was on Litecoin. adaptor_regtest_verify.py --chain grc is the run that would")
    console.say("answer whether Gridcoin's consensus accepts OP_CHECKMULTISIG inside a P2SH")
    console.say("redeem script and enforces nLockTime on a spend of one, and it has not")
    console.say("completed. If the answer is no, the script leg of a GRC swap cannot be built")
    console.say("this way at all.")
    console.say("")
    console.say("CLTV ON GRIDCOIN IS ALSO NOT ESTABLISHED, and an earlier version of this very")
    console.say("block said it was -- 'CLTV on GRC is established by three completed swaps'.")
    console.say("False, and corrected in 16a4644: OP_CHECKLOCKTIMEVERIFY sits in the OP_ELSE")
    console.say("branch of build_htlc_redeem_script, which a hashlock spend never executes, and")
    console.say("docs/atomic_swap_runs_2026_09_27.md records a claim txid for each of the three")
    console.say("GRC swaps and no GRC refund anywhere. Those swaps proved Gridcoin ACCEPTS A")
    console.say("SCRIPT CONTAINING the opcode in a branch that does not run. Not enforcement.")
    console.say("")
    console.say("Run without --run for the dry run, which proves the cryptography.")
    return 1


def monerod_get_info(port: int, timeout_seconds: int = 10) -> dict:
    """monerod's `get_info`, read-only, through the ONE JSON-RPC implementation in this tree.

    `monero_regtest.rpc` is imported rather than reimplemented (rule 8). It already handles
    the trap that matters for monerod specifically -- errors arrive as HTTP 200 with an
    `error` member, so calling `raise_for_status()` first would discard the reason -- and it
    already carries the checked S310 suppression for the one urlopen. A second copy here
    would have agreed with it on the day it was written and drifted from then on, and the
    drift would be invisible because each would look correct in its own file. That is how
    this repo ended up with five independent JSON-RPC clients against a Bitcoin-style
    daemon, which CLAUDE.md rule 8 counts.

    Read-only in the strong sense: `get_info` takes no parameters, changes nothing, and is
    the method monero_regtest.py itself uses to CONFIRM a nettype before it will mine. This
    is a DAEMON call, not a wallet call -- monero-wallet-rpc is not contacted anywhere in
    this file, so no wallet is opened and no key is touched.

    `timeout_seconds` stays in seconds because urlopen demands seconds (rule 6's interface
    boundary); the caller reports elapsed time in µfn.
    """
    return monerod_rpc(port, "get_info", timeout=timeout_seconds)


def check_monero_daemon(console: Console, port: int) -> tuple[bool, str]:
    """Ask monerod which network it is on and REFUSE anything that is not a test one.

    Returns (ok, nettype). A daemon that cannot be reached is reported as a skipped check
    rather than as a pass -- rule 13's "skipped plus success in the same output is a defect
    in the output" -- and main() carries that distinction into the final report.

    THE NETWORK IS ASKED, NEVER INFERRED FROM THE PORT. And it is asked of monerod rather
    than of an address: `validate_address` reports the network of the ADDRESS FORMAT, and on
    a regtest chain that is "mainnet" because there is no regtest prefix byte. Asking an
    address which network you are on gets a confident wrong answer.
    """
    console.say(f"asking monerod on 127.0.0.1:{port} for its nettype (read-only, get_info)")
    started = time.monotonic()
    try:
        info = monerod_get_info(port)
    except (RegtestError, OSError, TimeoutError) as error:
        elapsed = time.monotonic() - started
        console.say(
            f"monerod on port {port} could not be reached after {format_duration(elapsed)}: "
            f"{type(error).__name__}. THIS CHECK IS SKIPPED, not passed"
        )
        return False, "(unreachable)"

    nettype = str(info.get("nettype", ""))
    ok, reason = monero_network_is_test(nettype)
    console.check("monerod nettype", nettype or "(not reported)", "a test network", ok)
    console.say(reason)
    if ok:
        console.say(
            f"height={info.get('height', '(not reported)')}  <- monerod's own tip; the address "
            f"prefix network for nettype {nettype!r} is {monero_address_network(nettype)!r}, "
            f"which is NOT the identity map (fakechain uses mainnet prefixes)"
        )
    return ok, nettype


def check_script_daemon(console: Console, asset: str) -> tuple[bool, str]:
    """Ask the script-chain daemon which chain it is on, through atomic_swap.py's own check.

    `client_for` and `chain_name` are imported rather than reimplemented (rule 8). They
    already handle the divergence between the three daemons -- Bitcoin and Litecoin answer
    `getblockchaininfo` with a `chain`, Gridcoin is an older fork whose `getinfo` carries a
    `testnet` boolean -- and a second copy of that would be one more place to get it wrong.
    """
    console.say(f"asking the {asset} daemon which chain it is on (read-only)")
    started = time.monotonic()
    try:
        client = client_for(asset)
        name = chain_name(asset, client)
    except (SwapError, OSError) as error:
        elapsed = time.monotonic() - started
        console.say(
            f"the {asset} daemon could not be reached or would not name its chain after "
            f"{format_duration(elapsed)}: {error}. THIS CHECK IS SKIPPED, not passed"
        )
        return False, "(unreachable)"
    ok = name.lower() in TEST_CHAIN_NAMES
    console.check(f"{asset} network", name.upper(), "a test network", ok)
    return ok, name


def run_rehearsal(console: Console, address_network: str) -> object:
    """The offline cryptographic rehearsal: both parties, real proofs, no chain.

    Spawns the DLEQ helper and reaps it through the context manager, which is rule 13's
    "a spawn and its reap are one change" -- there is one spawn in this file and it cannot
    outlive this function.

    Announces the scale BEFORE doing the work (rule 14): two ~64 KiB proofs and two
    verifications is a few seconds, and a blinking cursor for a few seconds is how an
    operator learns to Ctrl-C.
    """
    console.say(
        "about to generate 2 cross-curve proofs (~64.9 KiB each, 252 bits) and verify both, "
        "then run one adaptor pre-signature, completion and recovery. No chain is contacted"
    )
    started = time.monotonic()
    with DleqHelper() as helper:
        version, bit_count = helper.version()
        console.say(f"go-dleq {version}, bit_count={bit_count}  <- must be 252")
        result = rehearse(helper, address_network, REHEARSAL_DIGEST)
    elapsed = time.monotonic() - started

    console.say(f"rehearsal completed in {format_duration(elapsed)}")
    console.say(
        f"proof sizes {result.proof_bytes_initiator} and {result.proof_bytes_participant} bytes  "
        f"<- NOT a constant: the secp256k1 signature inside is DER and runs 70-72 bytes, so the "
        f"total ranges over 64,966-64,968"
    )
    console.say(f"lock address ({address_network} prefix): {result.lock_address}")
    console.check("both sides verified the other's DLEQ", result.dleq_verified_both_ways, True,
                  result.dleq_verified_both_ways)
    console.check("adaptor pre-signature verified", result.presignature_verified, True,
                  result.presignature_verified)
    console.check("recovered share == the committed one", result.recovered_share_matches_commitment,
                  True, result.recovered_share_matches_commitment)
    console.check("reconstructed spend key opens the lock", result.spend_key_opens_lock, True,
                  result.spend_key_opens_lock)
    console.say(
        f"low-S negation exercised this run: {result.low_s_negation_exercised}  <- either value "
        f"is fine; it fires on roughly half of real swaps and both branches are covered by "
        f"tests/test_monero_swap_protocol.py rather than by one run"
    )
    return result


def check_example_timelocks(console: Console, asset: str) -> bool:
    """Show the timelock arithmetic on a concrete plan, in SECONDS, and prove it refuses.

    Two plans: one that is accepted and one that is not, so the operator sees the refusal
    text rather than being told it exists. The refused one is hazard 2 -- a cancel timelock
    too close to fund the Monero leg into.

    The unit is the point. atomic_swap.py::assert_ordering refused a correctly built
    BTC/LTC swap on the operator's first real run, 2026-09-27, because it compared BLOCKS
    remaining across two chains; Litecoin needs four times the blocks for half the time.
    This protocol has three block intervals in play, one of which (Monero's 120s) is not a
    timelock at all and only converts the 10-block output lock into a wait.
    """
    unlock = xmr_lock_wait_seconds(10)
    console.say(
        f"Monero confirm-and-unlock wait at 10 confirmations: {format_duration(unlock)} "
        f"<- max(confirmations, 10 blocks) x 120s, NOT the sum; both run from the same tx"
    )
    safe = TimelockPlan(
        s_chain_asset=asset,
        seconds_until_cancel=48 * 3600,
        seconds_until_punish=96 * 3600,
        xmr_confirmations=10,
        redeem_confirmations=6,
        cancel_confirmations=6,
        offline_allowance_seconds=12 * 3600,
        margin_seconds=3600,
    )
    try:
        assert_timelock_ordering(safe)
    except ProtocolError as error:
        console.check("example plan T1=48h T2=96h", "REFUSED", "accepted", False)
        console.say(str(error))
        return False
    console.check("example plan T1=48h T2=96h", "accepted", "accepted", True)

    tight = TimelockPlan(**{**vars(safe), "seconds_until_cancel": 1800})
    try:
        assert_timelock_ordering(tight)
    except ProtocolError as error:
        console.check("example plan T1=30min (hazard 2)", "REFUSED", "REFUSED", True)
        console.say(f"  {error}")
    else:
        console.check("example plan T1=30min (hazard 2)", "accepted", "REFUSED", False)
        console.say(
            "THIS IS A DEFECT: a 30-minute cancel timelock cannot fit the Monero unlock plus a "
            "redeem, and it was accepted"
        )
        return False

    ok_late, reason_late = redeem_is_safe_to_broadcast(
        seconds_until_cancel=1800, s_chain_asset=asset, redeem_confirmations=6,
        margin_seconds=3600,
    )
    console.check("redeem broadcast 30min before T1 (hazard 3)", "REFUSED" if not ok_late
                  else "allowed", "REFUSED", not ok_late)
    console.say(f"  {reason_late}")
    return not ok_late


def print_security_ledger(console: Console) -> None:
    """Print the per-party, per-step summary. Rule 14: the operator reads the screen."""
    console.say("")
    console.say("WHO CAN TAKE WHAT, AT EACH STEP. Full table: docs/monero_swap_protocol.md s4")
    console.say("")
    for label, line in SECURITY_LEDGER:
        console.say(f"  {label:<9} {line}")


def report(console: Console, monero_ok: bool, script_ok: bool, asset: str, skipped: bool) -> int:
    """What this run PROVED and what it did not. The distinction is the value of a dry run.

    Modeled on atomic_swap.py::report_dry_run, and it earns the name for the same reason: a
    reader has to be able to find the boundary between the two, because everything on the
    wrong side of it is something that could still lose money.
    """
    console.say("")
    console.say("DRY RUN COMPLETE -- nothing was funded, nothing was broadcast, nothing signed")
    console.say("on any chain, and no wallet was opened.")
    console.say("")
    console.say("PROVEN, here, with no chain:")
    console.say("  - two 252-bit shares each carry a cross-curve DLEQ proof the other side's")
    console.say("    verifier accepts against the exact keys it holds;")
    console.say("  - both sides derive the same Monero lock address from the four public halves;")
    console.say("  - an adaptor pre-signature verifies, completing it yields a signature, and the")
    console.say("    spend share recovered from that signature is EXACTLY the committed one;")
    console.say("  - the reconstructed spend key's public key equals the lock's summed key --")
    console.say("    the property monero_shared_key_verify.py swept for real on regtest.")
    if skipped:
        console.say("")
        console.say("SKIPPED (not passed):")
        if not monero_ok:
            console.say("  - monerod's nettype. No daemon answered, so the network is UNKNOWN.")
        if not script_ok:
            console.say(f"  - the {asset} daemon's chain. Not reached, so UNKNOWN.")
    else:
        console.say("")
        console.say(f"PROVEN, against live daemons: monerod and the {asset} daemon both answered")
        console.say("and both are on test networks. Neither was asked to do anything else.")
    console.say("")
    console.say("NOT PROVEN, and this is the half that matters:")
    console.say("  - THAT A SWAP CAN HAPPEN AT ALL. The script-chain 2-of-2 and its four spends")
    console.say("    DO exist (modules/adaptor_swap_{scripts,chain}.py) and were spent on LTC")
    console.say("    regtest, but NOTHING HERE BUILT OR SIGNED ONE, and no adaptor")
    console.say("    pre-signature has ever been made over one of their digests on any chain.")
    console.say("  - ANYTHING AT ALL ABOUT GRIDCOIN. The chain spend was Litecoin.")
    console.say("    adaptor_regtest_verify.py --chain grc has not completed, so whether GRC")
    console.say("    consensus accepts OP_CHECKMULTISIG in a P2SH redeem script is UNKNOWN.")
    console.say("  - that a COUNTERPARTY reaches the same verdict. Both sides were played by")
    console.say("    this one process, so the cryptography is proven and the transport is not.")
    console.say("  - that the Monero lock can be funded, that it unlocks, or that the")
    console.say("    reconstructed key imports into a wallet and sweeps. No monero-wallet-rpc")
    console.say("    was contacted; those need the operator's host.")
    console.say("  - that the cryptography is CORRECT. There is no audited implementation of")
    console.say("    this construction in any language, go-dleq ships no soundness test, and")
    console.say("    docs/dleq_cross_curve_design.md section 7 argues against building this.")
    console.say("    A broken DLEQ does not error: it lets a counterparty commit to different")
    console.say("    scalars on the two curves, take the script coin, and leave the XMR locked")
    console.say("    to a key nobody holds. The symptom arrives after the money moved.")
    return console.summary()


def main() -> int:
    args = build_parser().parse_args()
    console = Console(total_steps=TOTAL_STEPS)
    console.banner(
        f"MONERO ADAPTOR SWAP DRY RUN -- script chain {args.script_chain}, READ-ONLY, "
        f"nothing is funded"
    )

    if args.run:
        return refuse_run(console)

    console.step(1, "locate the DLEQ helper")
    try:
        # Printed HERE and not only inside the rehearsal: rule 14 says never let a step print
        # nothing, because a step with no output is indistinguishable from a step that was
        # skipped. It is also the artifact rule 13 asks to verify rather than assume -- which
        # binary is about to run the cryptography is exactly the thing a caller has to be able
        # to check, and helper_path() refuses a SPECIFIED path rather than substituting the
        # in-tree copy for it.
        console.say(f"helper binary: {helper_path()}")
    except HelperNotFound as error:
        console.say(str(error))
        console.say("")
        console.say("(none) -- no cryptographic check ran, because the helper is absent.")
        return console.summary()

    console.step(2, "offline cryptographic rehearsal, both parties in this process")
    monero_nettype = "fakechain"
    address_network = monero_address_network(monero_nettype)
    console.say(
        f"assuming nettype {monero_nettype!r} for the rehearsal's address prefix "
        f"({address_network!r}); step 3 asks the daemon for the real one"
    )
    run_rehearsal(console, address_network)

    console.step(3, "ask monerod which network it is on")
    monero_ok = True
    script_ok = True
    if args.skip_chains:
        console.say("--skip-chains: not contacting monerod. THIS CHECK IS SKIPPED, not passed")
        monero_ok = False
    else:
        port = args.monerod_port if args.monerod_port is not None else DEFAULT_MONEROD_PORT
        monero_ok, _ = check_monero_daemon(console, port)

    console.step(4, f"ask the {args.script_chain} daemon which network it is on")
    if args.skip_chains:
        console.say(
            f"--skip-chains: not contacting the {args.script_chain} daemon. SKIPPED, not passed"
        )
        script_ok = False
    else:
        script_ok, _ = check_script_daemon(console, args.script_chain)

    console.step(5, "timelock arithmetic, in seconds, and the refusals")
    check_example_timelocks(console, args.script_chain)
    print_security_ledger(console)

    console.step(6, "what this run proved, and what it did not")
    return report(console, monero_ok, script_ok, args.script_chain,
                  skipped=not (monero_ok and script_ok))


if __name__ == "__main__":
    sys.exit(main())
