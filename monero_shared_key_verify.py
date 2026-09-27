#!/usr/bin/env python3
"""Prove a 2-of-2 shared Monero address is real and spendable. The last untested claim.

Role: file (operator entry point, at the project root per CLAUDE.md rule 10)
Reads: a running monero-wallet-rpc, and chains/monero_keys.py
Writes: a new wallet file in the wallet-rpc's --wallet-dir (step 2 creates one from
        the summed keys), and the two key shares to a file under --shares-file so
        step 4 can reconstruct them. NOTHING in the repository, nothing in the swap
        database.
Can move funds: ONLY with --sweep, and only out of the shared wallet this script
        itself created, to a destination the caller names. Without --sweep it is
        read-only and creates no transaction. It never touches the wallet that was
        open when it started -- see THE REFUSALS.
Mainnet-safe: NO, and it refuses rather than relying on care: step 1 asks the daemon
        which network it is on and REFUSES on mainnet. The keys it generates are
        throwaway test keys written to a file in the clear, which is correct for a
        stagenet or regtest rehearsal and is exactly wrong anywhere else.
Live-safe: yes in the sense that matters -- it opens no swap database and reads no
        live configuration.

WHY THIS EXISTS: ONE CLAIM IN THE GRC<->XMR WORK IS STILL A PROPOSAL

chains/monero_keys.shared_address() is checked two ways and neither is the one that
matters. Its encoding is checked against a real wallet-generated stagenet address
(the Keccak checksum reproduces byte for byte), and its arithmetic is checked
against libsodium and against the homomorphism S_a + S_b == (s_a + s_b)*B. What is
NOT checked is the property the swap actually rests on:

    that XMR sent to a shared address is SPENDABLE with the summed private key.

Its docstring says so and calls itself a proposal under rule 16. This script is the
experiment that settles it, and the settling does not need the whole swap protocol
-- only two key shares, one address, and one sweep.

THE CHEAP HALF IS DECISIVE ON ITS OWN, WHICH IS THE BEST PART

monero-wallet-rpc's `generate_from_keys` takes an address, a spend key and a view
key, and builds a wallet from them. It returns the address IT derives. So step 2
compares the address this repo computed offline against the address Monero's own
wallet computes from the same summed scalars -- and a mismatch is a refutation of
shared_address() with NO coins involved at all.

That is a full cross-check of the arithmetic against an independent implementation,
available before anybody sends anything. If it passes, the remaining question is
only whether the chain agrees, and that is what --sweep answers.

THE REFUSALS, AND WHY EACH ONE

  mainnet                 refused in step 1, from the DAEMON's own nettype rather
                          than from a port number. The shares are written to a file
                          in the clear; on mainnet that is a key-disclosure bug, not
                          a test fixture.
  a funded open wallet    refused UNLESS --allow-open-wallet. `generate_from_keys`
                          switches the wallet-rpc to a different wallet, and doing
                          that to a process someone is using to watch real deposits
                          is rude at best.

                          THIS REFUSAL MADE THE DOCUMENTED SEQUENCE IMPOSSIBLE and
                          that was a defect, found by the operator running it on
                          2026-09-27. `monero_regtest.py --run` leaves a wallet
                          holding 80 blocks of coinbase open on port 28083 -- which
                          is exactly the throwaway this script tells you to point at
                          -- so the refusal fired on the one wallet it was meant to
                          permit. Two scripts that cannot be used together as their
                          own documentation says is worse than either refusal alone.

                          The flag is the fix rather than weakening the check,
                          because the check protects the case that matters (a
                          stagenet wallet watching deposits) and the flag is an
                          explicit statement that this wallet is disposable. It also
                          says what it costs: the open wallet is CLOSED and this
                          script does not reopen it, because monero-wallet-rpc
                          exposes no way to ask which file was open.
  --sweep with no coins   refused before building a transaction, with the balance
                          printed. Rule 14: an empty result is a result and must not
                          read as a failure to look.

FUNDING THE SHARED ADDRESS ON REGTEST NEEDS NO SECOND WALLET, which is the other
half of the fix above. `--mine N` calls the DAEMON's generateblocks with the shared
address as the miner, so the coins are created directly at the address under test.
No faucet, no transfer from another wallet, and no need to reopen anything -- the
shared wallet is already the open one.

That is safe by construction rather than by care: generateblocks is refused inside
monerod unless the nettype is FAKECHAIN
(src/rpc/core_rpc_server.cpp:1956 -> CORE_RPC_ERROR_CODE_REGTEST_REQUIRED), so
--mine cannot fund anything on stagenet or mainnet even if asked. On stagenet the
route is a faucet or an ordinary transfer, and the script says so rather than
failing obscurely.
  a destination that is   refused by chains/monero_keys.decode_address() before any
  not decodable           RPC call, so a typo costs nothing.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.monero_keys import (
    MoneroAddressError,
    decode_address,
    public_key_for_share,
    shared_address,
    shared_private_spend_key,
    shared_public_key,
)
from modules.ed25519_group import scalar_to_bytes_le
from step_console import Console

# Default to the REGTEST wallet port from monero_regtest.py, not the stagenet one.
# A script that creates wallets and sweeps should point at a throwaway by default and
# require an explicit --port to touch anything else.
DEFAULT_WALLET_PORT = 28083

# monerod's RPC port from monero_regtest.py, used only by --mine. Deliberately not
# Monero's real 18081 or 38081, for the reason that file gives: a regtest daemon on a
# real port is one typo away from a wallet that meant a real network.
DEFAULT_DAEMON_PORT = 28081

SHARED_WALLET_NAME = "shared-2of2"
SHARED_WALLET_PASSWORD = ""

# A share must be a non-zero scalar below l. Sampling below 2^252 keeps every share
# and their sum inside the range the cross-curve DLEQ also needs (see
# docs/dleq_cross_curve_design.md section 2.1), so the shares this script produces
# are the same shape the real protocol would use rather than a looser test fixture.
SHARE_UPPER_BOUND = 1 << 252


class VerifyError(RuntimeError):
    """A refusal, or a wallet that disagreed with this repo's arithmetic."""


@dataclass(frozen=True)
class Target:
    """Where everything this script talks to lives: two ports and the fixture path.

    One object because they always travel together -- every phase needs all three --
    and because ruff's PLR0913 on the six-argument run_phase() was right that they
    wanted to be one. Rule 12: extract, do not raise the ceiling.

    The two being separate values is also the transposition worth designing out:
    swapping the wallet and the daemon would point the miner at the wallet and the
    wallet at the daemon, and it would fail obscurely rather than loudly. Named fields
    make that impossible to write by accident, which a pair of positional ints does not.

    `daemon` is an int port on 127.0.0.1 or a "host:port" string, because a STAGENET
    wallet usually talks to a remote node -- see rpc()'s docstring for the measurement
    that forced this.
    """

    wallet_port: int
    daemon: int | str
    shares_path: Path


def rpc(
    endpoint: int | str, method: str, params: dict | None = None, timeout: int = 120
) -> dict:
    """One JSON-RPC call to a wallet or a daemon. Errors arrive as HTTP 200 with an
    `error` member, so the error is read BEFORE anything else -- the same trap
    htlc_rpc.rpc_result() exists for: calling raise_for_status() first discards the
    reason.

    `endpoint` IS EITHER A PORT OR A host:port, AND THAT IS NOT LAZINESS. It was an int
    port on 127.0.0.1 only, which assumed the daemon is local -- and on stagenet it
    usually is not. Measured on the operator's host 2026-09-27: no monerod on 38081,
    38089 or 18081, while the wallet-rpc on 38083 reported height 2,217,113, the real
    stagenet tip. It was talking to a REMOTE node the whole time, so every daemon call
    this file makes would have gone to a port with nothing behind it.

    The wallet is still always local -- it is a process this operator runs -- so an int
    remains the ordinary spelling for it.
    """
    body = json.dumps(
        {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
    ).encode()
    # The scheme and host are literal and only an integer port interpolates, which is
    # what S310 asks about; there is no scheme or host a caller can choose.
    host = f"127.0.0.1:{endpoint}" if isinstance(endpoint, int) else endpoint
    request = urllib.request.Request(
        f"http://{host}/json_rpc",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read())
    # OSError, NOT urllib.error.URLError. THIS ESCAPED AS A RAW TRACEBACK.
    #
    # urllib wraps a failure to CONNECT in URLError, but a READ that times out raises
    # TimeoutError straight out of socket.recv_into, and TimeoutError is not a URLError.
    # So a wallet-rpc that accepted the connection and then went quiet -- which is
    # exactly what a freshly created wallet scanning a remote chain does -- produced
    # fourteen frames of traceback instead of a sentence. Measured on stagenet
    # 2026-09-27; it never happened on regtest, where the wallet answers instantly.
    #
    # Both are OSError subclasses, which is the one except clause that covers the whole
    # class rather than the two members I happened to think of.
    except OSError as error:
        raise VerifyError(
            f"{method} on {host}: {error}. If that is a wallet port, is a "
            f"monero-wallet-rpc running there? `python3 monero_regtest.py --run` starts one "
            f"on {DEFAULT_WALLET_PORT}. If it is a daemon, check the address -- a stagenet "
            f"wallet usually talks to a REMOTE node, and `ps aux | grep monero-wallet-rpc` "
            f"shows which one in its --daemon-address"
        ) from error
    if "error" in payload:
        raise VerifyError(f"{method} on {host}: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise VerifyError(f"{method} on {host}: no `result` object in the reply")
    return result


def sample_share() -> int:
    """One private key share, uniform in [1, 2^252).

    secrets, not random: these are keys. They are throwaway keys on a test network
    and they get written to a file in the clear, which is why step 1 refuses to run
    on mainnet -- but sampling them weakly would make the test measure something
    other than what the protocol does.
    """
    while True:
        candidate = secrets.randbelow(SHARE_UPPER_BOUND)
        if candidate:
            return candidate


def build_shares() -> dict:
    """Four shares, the two sums, and the shared address -- all computed HERE.

    Returned as plain ints and hex so the whole fixture can be written to a file and
    read back by a later invocation: the operator has to send coins between step 3
    and step 4, and that is not a thing a single process can wait through.
    """
    spend_a, spend_b = sample_share(), sample_share()
    view_a, view_b = sample_share(), sample_share()
    return {
        "spend_share_a": spend_a,
        "spend_share_b": spend_b,
        "view_share_a": view_a,
        "view_share_b": view_b,
        "spend_summed": shared_private_spend_key(spend_a, spend_b),
        "view_summed": shared_private_spend_key(view_a, view_b),
        "public_spend": shared_public_key(
            public_key_for_share(spend_a), public_key_for_share(spend_b)
        ).hex(),
        "public_view": shared_public_key(
            public_key_for_share(view_a), public_key_for_share(view_b)
        ).hex(),
    }


def address_for(shares: dict, network: str) -> str:
    return shared_address(
        network,
        public_key_for_share(shares["spend_share_a"]),
        public_key_for_share(shares["spend_share_b"]),
        public_key_for_share(shares["view_share_a"]),
        public_key_for_share(shares["view_share_b"]),
    )


def daemon_nettype(console: Console, daemon: int | str) -> str:
    """The network, from monerod. Refuses mainnet. Shared by both paths unchanged.

    This is the ONE check both entry paths genuinely share, and it is the only thing
    left in common. See the note above preflight_run/preflight_sweep for why the rest
    was split.
    """
    nettype = str(rpc(daemon, "get_info").get("nettype", "(not reported)"))
    console.check("network, from monerod's get_info", nettype.upper(),
                  "fakechain (regtest), stagenet or testnet -- NOT mainnet",
                  nettype.lower() != "mainnet")
    if nettype.lower() == "mainnet":
        raise VerifyError(
            "REFUSING: this daemon is on MAINNET. This script samples private key shares and "
            "writes them to a file IN THE CLEAR, which is a correct test fixture on a test "
            "network and a key-disclosure bug anywhere else"
        )
    return nettype.lower()


def wait_for_wallet(console: Console, port: int, seconds: int = 300) -> dict:
    """get_version, retried, because a freshly created wallet is legitimately BUSY.

    monero-wallet-rpc runs its RPC on one thread ("Run net_service loop( 1 threads)" in
    its own banner). A wallet created by generate_from_keys against a REMOTE node then
    starts refreshing, and while it does it accepts connections and answers nothing --
    so the first call after --run can block for minutes. On regtest this never appears,
    because the daemon is local and the chain is 160 blocks.

    Treating that as "unreachable" would be wrong twice over: the wallet is there, and
    the right response is to wait rather than to refuse. So this retries with a progress
    line, which is also rule 14's requirement -- a blinking cursor for two minutes is
    indistinguishable from a hang, and the operator's answer to a hang is Ctrl-C.
    """
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return rpc(port, "get_version", timeout=20)
        except VerifyError as error:
            # A REFUSED CONNECTION IS NOT A BUSY WALLET, and retrying it is the same
            # conflation this file keeps producing: two different failures treated as
            # one. "Connection refused" means nothing is listening on that port -- no
            # amount of waiting changes that -- while a TIMEOUT means something accepted
            # the connection and has not answered, which is the busy case this function
            # exists for.
            #
            # Retrying the refused case cost 300 seconds per invocation and was measured
            # immediately: the two tests that drive main() against an unreachable port
            # went from instant to hanging the whole suite. An operator who mistyped
            # --port would have waited five minutes to be told nothing was there.
            if "Connection refused" in str(error) or "refused" in str(error).lower():
                raise VerifyError(
                    f"nothing is listening on port {port} -- not a busy wallet, an absent one, "
                    f"so waiting cannot help. Start a monero-wallet-rpc there, or check --port. "
                    f"Original: {error}"
                ) from error
            elapsed = time.monotonic() - started
            if elapsed >= seconds:
                raise VerifyError(
                    f"the wallet on port {port} did not answer get_version within {seconds}s "
                    f"({attempt} attempts). A wallet created against a remote node refreshes "
                    f"before it answers, so this can legitimately take minutes -- raise --wait, "
                    f"or check the wallet's own log for a sync failure. Last error: {error}"
                ) from error
            console.say(
                f"wallet on {port} is busy (attempt {attempt}, {elapsed:.0f}s of {seconds}s) "
                f"-- a new wallet refreshes against the remote node before it answers"
            )
            time.sleep(5.0)


def open_wallet_address(port: int) -> str | None:
    """The open wallet's primary address, or None if NO WALLET IS OPEN.

    None rather than an exception, because "no wallet open" is the NORMAL state of a
    fresh `--wallet-dir` process and is a refusal on only one of the two paths. A
    wallet-rpc with no wallet answers get_address with code -13 "No wallet file", which
    is a state rather than a fault; any OTHER error is a real problem and propagates.
    """
    try:
        return str(rpc(port, "get_address", {"account_index": 0})["address"])
    except VerifyError as error:
        if "No wallet file" in str(error) or "-13" in str(error):
            return None
        raise


def wallet_balance(port: int) -> int:
    """Total balance of the open wallet, in atomic units. Assumes one is open."""
    return int(rpc(port, "get_balance", {"account_index": 0}).get("balance", 0))


# WHY THERE ARE TWO PREFLIGHTS AND NOT ONE WITH FLAGS.
#
# There was one -- `refuse_mainnet_and_a_funded_wallet(console, port, daemon,
# allow_open_wallet, check_balance)` -- and it produced FOUR bugs in one day, every one
# of the same shape: a guard written for the state one entry path leaves behind, applied
# to a sibling that does not produce that state.
#
#   1. --regtest passed to monero-wallet-rpc, which has no such flag (monerod does).
#   2. generateblocks aimed at a subaddress, correct for the payee and refused for the
#      miner.
#   3. the funded-wallet refusal applied to --sweep, which switches no wallets and where
#      a funded wallet is the SUCCESS condition.
#   4. and this one: requiring a wallet to be OPEN on --run, when --run's whole job is
#      to CREATE one and a fresh --wallet-dir process correctly has none.
#
# Each was patched by adding a parameter, and the third and fourth exist BECAUSE of the
# patching: two booleans on one function is a function that does two different jobs and
# is trusted to remember which. The accumulation was the defect.
#
# So the shared function is gone. What the two paths genuinely share is one check --
# "which network is this" -- and that is `daemon_nettype()`. Everything else differs and
# now says so in its own name. A future path adds a third preflight rather than a third
# boolean.


def preflight_run(console: Console, port: int, daemon: int | str, allow_open_wallet: bool) -> str:
    """Step 1 for --run: refuse mainnet, and protect a wallet that is already open.

    NO WALLET OPEN IS THE GOOD CASE HERE. --run's job is to CREATE the shared wallet
    with generate_from_keys, so a fresh `--wallet-dir` process having nothing open is
    exactly right -- there is nothing to protect and nothing to close.

    When a wallet IS open, generate_from_keys will switch away from it, and doing that
    to a process someone is using to watch deposits is not this script's call. That is
    what --allow-open-wallet consents to, and it says what it costs: the wallet is
    CLOSED and not reopened, because monero-wallet-rpc exposes no way to ask which file
    was open.
    """
    version = wait_for_wallet(console, port)
    console.say(f"wallet rpc version {version.get('version')} on port {port}")
    nettype = daemon_nettype(console, daemon)

    address = open_wallet_address(port)
    if address is None:
        console.check("open wallet", "none -- nothing to protect", "none, or a throwaway", True)
        return nettype

    console.say(f"open wallet primary {address[:12]}...{address[-6:]}")
    total = wallet_balance(port)
    console.check("open wallet balance", f"{total} atomic units",
                  "0, or --allow-open-wallet" if total else "0 -- a throwaway wallet",
                  total == 0 or allow_open_wallet)
    if total and not allow_open_wallet:
        raise VerifyError(
            f"REFUSING: the wallet currently open on port {port} holds {total} atomic units. "
            f"`generate_from_keys` SWITCHES this wallet-rpc to a different wallet.\n"
            f"          If this IS a throwaway -- and a wallet left open by "
            f"`monero_regtest.py --run` is one, holding 80 blocks of regtest coinbase -- pass "
            f"--allow-open-wallet. It will be CLOSED and not reopened."
        )
    if total:
        console.say(
            f"--allow-open-wallet: the wallet holding {total} atomic units is about to be "
            f"CLOSED and will not be reopened"
        )
    return nettype


def preflight_sweep(console: Console, port: int, daemon: int | str) -> str:
    """Step 1 for --sweep: refuse mainnet, and require the shared wallet to BE open.

    The inverse of preflight_run on both counts, which is why they are two functions.
    --sweep calls no generate_from_keys and switches no wallets; it spends what the
    open wallet holds. So a wallet MUST be open, and its holding a balance is the
    success condition rather than a hazard -- the coins about to be swept.

    WHICH wallet it is gets checked in sweep_phase(), against the fixture's shared
    address and against the address the fixture's shares re-derive to. That is the
    check that matters here: sweeping the wrong wallet moves funds from somewhere
    nobody asked about, and a balance check never detected that.
    """
    version = wait_for_wallet(console, port)
    console.say(f"wallet rpc version {version.get('version')} on port {port}")
    nettype = daemon_nettype(console, daemon)

    address = open_wallet_address(port)
    console.check("a wallet is open", "yes" if address else "NO WALLET OPEN",
                  "yes -- --sweep spends from it", address is not None)
    if address is None:
        raise VerifyError(
            f"REFUSING: no wallet is open on port {port}, so there is nothing to sweep. Run "
            f"--run first: it creates the shared wallet from the summed keys and leaves it "
            f"open on this same port. If --run was used against a DIFFERENT wallet-rpc, point "
            f"--port at that one"
        )
    console.say(f"open wallet primary {address[:12]}...{address[-6:]}")
    total = wallet_balance(port)
    console.say(
        f"open wallet holds {total} atomic units -- expected here, and what is about to be spent"
    )
    return nettype


def create_shared_wallet(
    console: Console, port: int, daemon: int | str, shares: dict, address: str
) -> str:
    """Step 2. THE DECISIVE CHECK, and it needs no coins.

    generate_from_keys is handed the address this repo computed and the two SUMMED
    scalars, and it returns the address Monero's own wallet derives from them. So a
    mismatch refutes chains/monero_keys.shared_address() outright, against an
    independent implementation, before anything is sent anywhere.

    A wrong address is the failure mode that matters: it does not error, it produces
    a valid Monero address whose spend key nobody holds, and the coins are gone with
    no transaction to point at. This is the check that catches it while it is still
    free.
    """
    console.say(f"this repo computed:  {address}")

    # RESTORE HEIGHT IS THE CURRENT TIP, NOT 0, AND ON A REAL NETWORK THAT IS THE
    # DIFFERENCE BETWEEN SECONDS AND HOURS.
    #
    # restore_height=0 tells the wallet to scan the chain from genesis. On regtest that
    # is 160 blocks and invisible. On STAGENET it is ~2,217,000 blocks fetched from a
    # remote node -- measured 2026-09-27, the operator's wallet reported height
    # 2,217,113 against node.monerodevs.org:38089 -- and the first refresh would have
    # sat there for a very long time with nothing to show for it.
    #
    # The keys were sampled seconds ago by this very process, so the wallet CANNOT have
    # a transaction older than now: every block before the current tip is provably
    # irrelevant to it. Scanning them is not caution, it is waste.
    #
    # One block of margin, because the tip can advance between this call and the
    # wallet being created, and a restore height above the block a deposit lands in
    # would make that deposit invisible -- which would look exactly like the shared
    # address not working, the one wrong conclusion this script exists to prevent.
    tip = int(rpc(daemon, "get_info").get("height", 0))
    restore_height = max(0, tip - 1)
    console.say(f"daemon tip {tip}; restoring the new wallet from height {restore_height}")
    console.say("(not 0: these keys were sampled seconds ago, so no earlier block can")
    console.say(" hold a transaction for them, and on stagenet scanning from genesis")
    console.say(" would mean 2.2 million blocks from a remote node)")

    result = rpc(port, "generate_from_keys", {
        "filename": SHARED_WALLET_NAME,
        "address": address,
        "spendkey": scalar_to_bytes_le(shares["spend_summed"]).hex(),
        "viewkey": scalar_to_bytes_le(shares["view_summed"]).hex(),
        "password": SHARED_WALLET_PASSWORD,
        "restore_height": restore_height,
    })
    reported = str(result.get("address", ""))
    console.say(f"the wallet derived: {reported}")
    console.say(f"wallet info: {result.get('info') or '(none)'}")
    console.check(
        "wallet's address == this repo's shared_address()",
        "identical" if reported == address else f"DIFFERENT ({reported})",
        "identical", reported == address,
    )
    if reported != address:
        raise VerifyError(
            "REFUTED: monero-wallet-rpc derived a different address from the same summed "
            "scalars than chains/monero_keys.shared_address() computed. One of the two is "
            "wrong and it is almost certainly ours. No coins were involved"
        )
    return reported


def mine_to_shared_address(console: Console, daemon: int | str, address: str, blocks: int) -> int:
    """Fund the shared address directly, by mining to it. REGTEST ONLY, by construction.

    This is what removes the need for a second wallet. The alternative -- transfer
    from the wallet that monero_regtest.py funded -- cannot work in one process,
    because generate_from_keys has already CLOSED that wallet to open the shared one,
    and monero-wallet-rpc exposes no way to ask which file was open so it cannot be
    put back. Mining puts the coinbase straight at the address under test.

    SAFE BY CONSTRUCTION RATHER THAN BY CARE: generateblocks is refused inside monerod
    unless the nettype is FAKECHAIN (src/rpc/core_rpc_server.cpp:1956 returns
    CORE_RPC_ERROR_CODE_REGTEST_REQUIRED), and regtest is its own nettype rather than
    a mode over another one. So this cannot fund anything on stagenet or mainnet even
    if asked, and the refusal that comes back is passed through verbatim rather than
    reworded -- it is monerod's own sentence and it is clearer than a paraphrase.
    """
    console.say(f"mining {blocks} blocks to the shared address on daemon {daemon}")
    console.say("a coinbase output is locked for 60 confirmations, so this needs more than 60")
    try:
        result = rpc(daemon, "generateblocks",
                     {"amount_of_blocks": blocks, "wallet_address": address}, timeout=300)
    except VerifyError as error:
        raise VerifyError(
            f"generateblocks refused: {error}\n"
            f"          If that says REGTEST REQUIRED, this daemon is not a regtest daemon and "
            f"--mine cannot work here -- which is monerod protecting a real network, not a bug. "
            f"On stagenet, fund the address with a faucet or an ordinary transfer instead and "
            f"then run --sweep."
        ) from error
    height = int(result.get("height", 0))
    console.check("height after mining", height, f"at least {blocks}", height >= blocks)
    return height


def report_balance(console: Console, port: int, seconds: int) -> int:
    """Step 4a. Refresh until the shared wallet SEES the coins, printing every pass."""
    started = time.monotonic()
    attempt = 0
    while time.monotonic() - started < seconds:
        attempt += 1
        rpc(port, "refresh")
        balance = rpc(port, "get_balance", {"account_index": 0})
        total = int(balance.get("balance", 0))
        unlocked = int(balance.get("unlocked_balance", 0))
        console.say(f"refresh {attempt}: balance={total} unlocked={unlocked} "
                    f"({time.monotonic() - started:.1f}s)")
        if unlocked:
            console.check("unlocked balance in the SHARED wallet", unlocked, "greater than 0", True)
            return unlocked
        if total:
            console.say("arrived but still locked -- Monero locks an output for 10 blocks")
        time.sleep(5.0)
    balance = rpc(port, "get_balance", {"account_index": 0})
    raise VerifyError(
        f"nothing UNLOCKED in the shared wallet after {seconds}s. balance="
        f"{balance.get('balance', 0)} unlocked={balance.get('unlocked_balance', 0)} atomic "
        f"units. If balance is 0 the coins were never sent to the shared address; if it is "
        f"non-zero they are still within the 10-block lock"
    )


def sweep_out(console: Console, port: int, destination: str) -> str:
    """Step 4b. THE CLAIM ITSELF: spend the shared output with the summed key.

    sweep_all rather than transfer, because "can this key move everything at this
    address" is the question, and a partial spend leaves the interesting case
    untested.
    """
    try:
        decoded = decode_address(destination)
    except MoneroAddressError as error:
        raise VerifyError(f"the --destination is not a decodable Monero address: {error}") from error
    console.say(f"sweeping to {decoded.network}/{decoded.kind} {destination[:12]}...{destination[-6:]}")

    result = rpc(port, "sweep_all", {"address": destination, "account_index": 0,
                                     "get_tx_keys": True}, timeout=300)
    hashes = result.get("tx_hash_list") or []
    amounts = result.get("amount_list") or []
    console.check("sweep_all tx_hash_list", len(hashes), "at least 1", len(hashes) >= 1)
    if not hashes:
        raise VerifyError(
            "sweep_all returned no transaction hashes. The summed key did not move the funds, "
            "which REFUTES the shared-key construction -- this is the result the whole script "
            "exists to obtain, in the negative"
        )
    for txid, amount in zip(hashes, amounts or [None] * len(hashes), strict=False):
        console.say(f"swept {amount} atomic units, txid {txid}")
    return str(hashes[0])


def save_shares(path: Path, shares: dict, address: str, network: str) -> None:
    """Write the fixture so a later invocation can finish the experiment.

    THESE ARE PRIVATE KEYS AND THEY GO IN THE CLEAR. That is correct for a stagenet
    or regtest rehearsal -- the coins are worthless and the whole point is that a
    second process can reconstruct the sum -- and it is why step 1 refuses on
    mainnet. The file is written 0600 anyway, because a habit that only holds on test
    networks is not a habit.
    """
    payload = {
        "network": network,
        "shared_address": address,
        "public_spend": shares["public_spend"],
        "public_view": shares["public_view"],
        "WARNING": "PRIVATE KEY SHARES IN THE CLEAR. Test networks only. Delete when done.",
        **{key: hex(value) for key, value in shares.items() if isinstance(value, int)},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    path.chmod(0o600)


def load_shares(path: Path) -> dict:
    """Read a fixture back, and REFUSE one this script did not write cleanly."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise VerifyError(
            f"could not read the shares file {path}: {error}. Run without --sweep first; it "
            f"writes one"
        ) from error
    shares = {}
    for key in ("spend_share_a", "spend_share_b", "view_share_a", "view_share_b",
                "spend_summed", "view_summed"):
        if key not in payload:
            raise VerifyError(f"the shares file {path} has no `{key}` -- it was not written by this script")
        shares[key] = int(str(payload[key]), 16)
    # Recompute the sums rather than trusting the file's own. A fixture whose stored
    # sum disagrees with its stored shares would make every later step measure the
    # wrong thing, and this is one line.
    for label, summed, a, b in (
        ("spend", "spend_summed", "spend_share_a", "spend_share_b"),
        ("view", "view_summed", "view_share_a", "view_share_b"),
    ):
        recomputed = shared_private_spend_key(shares[a], shares[b])
        if recomputed != shares[summed]:
            raise VerifyError(
                f"the {label} sum stored in {path} does not equal the sum of its own shares. "
                f"The file is inconsistent and nothing computed from it would mean anything"
            )
    shares["public_spend"] = str(payload.get("public_spend", ""))
    shares["public_view"] = str(payload.get("public_view", ""))
    shares["shared_address"] = str(payload.get("shared_address", ""))
    return shares


def print_plan(console: Console, port: int, shares_path: Path) -> int:
    """What --run and --sweep each do, printed instead of done. Rule 14: a bare
    invocation that creates wallets and moves coins has announced nothing."""
    console.banner("PLAN ONLY -- nothing done. Pass --run, then --sweep <address>.")
    console.say("--run    samples two spend shares and two view shares, computes the shared")
    console.say("         address with chains/monero_keys.shared_address(), and hands the")
    console.say("         SUMMED scalars to monero-wallet-rpc's generate_from_keys.")
    console.say("         THE DECISIVE CHECK NEEDS NO COINS: the wallet reports the address")
    console.say("         it derives, and a mismatch refutes shared_address() for free.")
    console.say("--sweep  after sending to that address: refresh, then spend it all out with")
    console.say("         the summed key. That is the claim chains/monero_keys.py labels a")
    console.say("         proposal under rule 16.")
    console.say("")
    console.say(f"port {port} (monero_regtest.py --run starts a wallet-rpc there)")
    console.say(f"shares file {shares_path}  <- PRIVATE KEYS IN THE CLEAR, test nets only")
    return 0


def run_phase(console: Console, target: Target, network: str, mine_blocks: int) -> int:
    """Steps 2-4 of --run: compute the address, let the wallet confirm it, save the fixture."""
    console.step(2, "generate four shares and compute the shared address OFFLINE")
    shares = build_shares()
    # A REGTEST ("fakechain") CHAIN USES MAINNET ADDRESS PREFIXES, which is the same
    # fact that caused the nettype bug above and is load-bearing here: an address built
    # with the stagenet prefix would not be one the regtest wallet recognizes. So
    # fakechain maps to "mainnet" and everything else maps to itself.
    address_network = "stagenet" if network == "stagenet" else "mainnet"
    console.say(f"daemon says {network}; building the address with {address_network} prefixes")
    address = address_for(shares, address_network)
    console.say(f"public spend key (sum) {shares['public_spend']}")
    console.say(f"public view  key (sum) {shares['public_view']}")
    decoded = decode_address(address)
    console.check("the computed address decodes", f"{decoded.network}/{decoded.kind}",
                  "a standard address", True)

    console.step(3, "hand the SUMMED scalars to the wallet -- the decisive check")
    create_shared_wallet(console, target.wallet_port, target.daemon, shares, address)
    target.shares_path.parent.mkdir(parents=True, exist_ok=True)
    save_shares(target.shares_path, shares, address, network)

    console.step(4, "fund the shared address, or say how to")
    console.say("THE ARITHMETIC IS CONFIRMED against monero-wallet-rpc's own derivation.")
    console.say("Remaining: whether the chain lets the summed key spend it.")
    console.say(f"shares saved to {target.shares_path} (mode 0600, private keys in the clear)")
    if mine_blocks:
        mine_to_shared_address(console, target.daemon, address, mine_blocks)
        console.say("")
        console.say("funded. Now finish it -- the destination below is this same shared wallet's")
        console.say("own address, which is a real spend and needs nothing else to exist:")
        console.say(f"  python3 monero_shared_key_verify.py --sweep {address} "
                    f"--port {target.wallet_port}")
    else:
        console.say("")
        console.say(f"send any amount to:\n          {address}")
        console.say("on REGTEST, add --mine 80 to this command instead and it funds itself.")
        console.say("Then sweep it out. The destination can be this same address, which is a")
        console.say("real spend and needs no other wallet to exist:")
        console.say(f"  python3 monero_shared_key_verify.py --sweep {address} "
                    f"--port {target.wallet_port}")
    return console.summary()


def sweep_phase(console: Console, target: Target, destination: str, wait: int) -> int:
    """Steps 2-4 of --sweep: reload the fixture, confirm the wallet, spend it out."""
    console.step(2, "reload the shares and confirm the OPEN wallet is the shared one")
    shares = load_shares(target.shares_path)
    console.say(f"loaded four shares from {target.shares_path}; both sums recomputed and agree")

    # THE CHECK THAT MATTERS ON THIS PATH, and it replaces the balance refusal that
    # used to fire here for no reason. What could go wrong on a sweep is not "the
    # wallet has money" -- it is "this is the WRONG wallet", and sweeping the wrong
    # one moves funds from somewhere nobody asked about. The fixture records the
    # shared address; the wallet reports its own; they must be the same string.
    #
    # Re-derived from the SHARES as well as read from the file, so a fixture whose
    # stored address does not match its own keys is caught here rather than producing
    # a confident sweep of something else.
    open_address = str(rpc(target.wallet_port, "get_address", {"account_index": 0})["address"])
    recomputed = address_for(shares, decode_address(open_address).network)
    console.check("open wallet == the fixture's shared address",
                  "identical" if open_address == shares["shared_address"] else
                  f"DIFFERENT (open {open_address[:12]}...)",
                  "identical", open_address == shares["shared_address"])
    console.check("and it is what the SHARES re-derive to",
                  "identical" if recomputed == open_address else f"DIFFERENT ({recomputed[:12]}...)",
                  "identical", recomputed == open_address)
    if open_address != shares["shared_address"] or recomputed != open_address:
        raise VerifyError(
            f"REFUSING: the wallet open on port {target.wallet_port} is not the shared wallet "
            f"this fixture describes. Sweeping it would move funds from somewhere nobody asked "
            f"about.\n          open wallet  {open_address}\n"
            f"          fixture      {shares['shared_address']}\n"
            f"          shares give  {recomputed}\n"
            f"          Run --run again to recreate the shared wallet, or point --port at the "
            f"wallet-rpc that holds it"
        )

    console.step(3, "refresh the shared wallet until the coins UNLOCK")
    report_balance(console, target.wallet_port, wait)

    console.step(4, "spend it all out with the SUMMED key")
    sweep_out(console, target.wallet_port, destination)
    console.say("")
    console.say("THE PROPOSAL IS NOW A TESTED CLAIM: XMR sent to an address built from two")
    console.say("public key shares was spent with the sum of the two private shares.")
    return console.summary()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove a 2-of-2 shared Monero address is spendable with the summed key."
    )
    parser.add_argument("--run", action="store_true",
                        help="generate shares and create the shared wallet (steps 1-3)")
    parser.add_argument("--sweep", metavar="DESTINATION",
                        help="finish the experiment: sweep the shared address to DESTINATION")
    parser.add_argument("--port", type=int, default=DEFAULT_WALLET_PORT)
    parser.add_argument("--daemon", default=str(DEFAULT_DAEMON_PORT),
                        help="monerod's RPC endpoint: a port on localhost, or host:port for a "
                             "REMOTE node, which is what a stagenet wallet normally uses")
    parser.add_argument("--mine", type=int, default=0, metavar="BLOCKS",
                        help="REGTEST ONLY: mine this many blocks to the shared address, funding "
                             "it directly. 80 clears the 60-block coinbase lock with margin")
    parser.add_argument("--allow-open-wallet", action="store_true",
                        help="proceed even though the open wallet holds funds. It will be CLOSED "
                             "and not reopened -- for a throwaway only")
    parser.add_argument("--shares-file",
                        default=str(Path.home() / "xmr-regtest" / "shared-shares.json"))
    parser.add_argument("--wait", type=int, default=300,
                        help="seconds to wait for the coins to unlock, with --sweep")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    console = Console(total_steps=4)
    # A bare number means a port on localhost; anything else is passed through as a
    # host:port. Accepting both from one flag keeps the local and remote cases spelled
    # the same way at the call site.
    daemon: int | str = int(args.daemon) if str(args.daemon).isdigit() else str(args.daemon)
    target = Target(
        wallet_port=args.port,
        daemon=daemon,
        shares_path=Path(args.shares_file).expanduser(),
    )

    if not args.run and not args.sweep:
        return print_plan(console, target.wallet_port, target.shares_path)

    try:
        console.banner("Monero 2-of-2 shared key -- the last untested claim in the GRC<->XMR work")
        console.step(1, "refuse mainnet, and refuse a wallet that holds anything")
        if args.run:
            network = preflight_run(
                console, target.wallet_port, target.daemon, args.allow_open_wallet
            )
            return run_phase(console, target, network, args.mine)
        network = preflight_sweep(console, target.wallet_port, target.daemon)
        return sweep_phase(console, target, args.sweep, args.wait)
    except VerifyError as error:
        console.check("run", str(error), "no refusal", False)
        return console.summary()


if __name__ == "__main__":
    sys.exit(main())
