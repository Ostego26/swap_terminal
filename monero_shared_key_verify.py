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

    The two ports being separate ints is also the transposition worth designing out:
    swapping `wallet_port` and `daemon_port` would point the miner at the wallet and
    the wallet at the daemon, and it would fail obscurely rather than loudly. Named
    fields make that impossible to write by accident, which a pair of positional ints
    does not.
    """

    wallet_port: int
    daemon_port: int
    shares_path: Path


def rpc(port: int, method: str, params: dict | None = None, timeout: int = 120) -> dict:
    """One wallet-rpc call. Errors arrive as HTTP 200 with an `error` member, so the
    error is read BEFORE anything else -- the same trap htlc_rpc.rpc_result() exists
    for: calling raise_for_status() first discards the reason."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
    ).encode()
    # The scheme and host are literal and only an integer port interpolates, which is
    # what S310 asks about; there is no scheme or host a caller can choose.
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json_rpc",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read())
    except urllib.error.URLError as error:
        raise VerifyError(
            f"{method} on port {port}: {error}. Is a monero-wallet-rpc running there? "
            f"`python3 monero_regtest.py --run` starts one on {DEFAULT_WALLET_PORT}"
        ) from error
    if "error" in payload:
        raise VerifyError(f"{method}: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise VerifyError(f"{method}: no `result` object in the reply")
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


def refuse_mainnet_and_a_funded_wallet(
    console: Console, port: int, daemon_port: int, allow_open_wallet: bool = False
) -> str:
    """Step 1. Ask the DAEMON which network, and refuse two situations outright.

    The nettype comes from the wallet's own view of its daemon rather than from the
    port, because a port number is a convention and a convention is not a check --
    the same reason chains/monero.py asks rather than infers.
    """
    version = rpc(port, "get_version")
    console.say(f"wallet rpc version {version.get('version')} on port {port}")

    # THE NETWORK COMES FROM monerod's get_info, NOT FROM validate_address, AND THAT
    # WAS A BUG THAT REFUSED THE CORRECT CASE.
    #
    # The first version asked the wallet to validate its own address and read the
    # `nettype` out of the reply. On a REGTEST chain that answers "mainnet" -- measured
    # on the operator's host 2026-09-27 -- because a regtest chain uses MAINNET ADDRESS
    # PREFIXES. validate_address reports the network an address FORMAT belongs to,
    # which is a different question from which network the daemon is on, and on regtest
    # the two disagree. So this script refused a regtest wallet as mainnet: the exact
    # inverse of the safety property it was written for, and it blocked the one
    # configuration it was meant to run in.
    #
    # monerod's get_info reports the real thing and spells regtest "fakechain", the
    # same FAKECHAIN that gates generateblocks. monero_regtest.py has always asked that
    # way; this file asked the wallet because the wallet was already in hand, which is
    # the whole mistake in one sentence.
    nettype = str(rpc(daemon_port, "get_info").get("nettype", "(not reported)"))
    try:
        current = str(rpc(port, "get_address", {"account_index": 0})["address"])
        console.say(f"open wallet primary {current[:12]}...{current[-6:]}")
    except VerifyError as error:
        raise VerifyError(
            f"could not read the open wallet's address ({error}). This script needs a "
            f"monero-wallet-rpc started in --wallet-dir mode with a wallet open; "
            f"`python3 monero_regtest.py --run` produces exactly that"
        ) from error
    console.check("network, from monerod's get_info", nettype.upper(),
                  "fakechain (regtest), stagenet or testnet -- NOT mainnet",
                  nettype.lower() != "mainnet")
    if nettype.lower() == "mainnet":
        raise VerifyError(
            "REFUSING: this wallet's daemon is on MAINNET. This script samples private key "
            "shares and writes them to a file IN THE CLEAR, which is a test fixture on a test "
            "network and a key-disclosure bug anywhere else. Point it at a stagenet or regtest "
            "wallet-rpc"
        )

    balance = rpc(port, "get_balance", {"account_index": 0})
    total = int(balance.get("balance", 0))
    console.check(
        "open wallet balance",
        f"{total} atomic units",
        "0, or --allow-open-wallet" if total else "0 -- a throwaway wallet",
        total == 0 or allow_open_wallet,
    )
    if total and not allow_open_wallet:
        raise VerifyError(
            f"REFUSING: the wallet currently open on port {port} holds {total} atomic units. "
            f"`generate_from_keys` SWITCHES this wallet-rpc to a different wallet, and doing "
            f"that to a process someone is using to watch deposits is not this script's call.\n"
            f"          If this IS a throwaway -- and a wallet left open by "
            f"`monero_regtest.py --run` is one, holding 80 blocks of regtest coinbase -- pass "
            f"--allow-open-wallet. It will be CLOSED and not reopened: monero-wallet-rpc exposes "
            f"no way to ask which file was open, so this script cannot put it back."
        )
    if total and allow_open_wallet:
        console.say(
            f"--allow-open-wallet: the wallet holding {total} atomic units is about to be "
            f"CLOSED and will not be reopened"
        )
    return nettype.lower()


def create_shared_wallet(console: Console, port: int, shares: dict, address: str) -> str:
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
    result = rpc(port, "generate_from_keys", {
        "filename": SHARED_WALLET_NAME,
        "address": address,
        "spendkey": scalar_to_bytes_le(shares["spend_summed"]).hex(),
        "viewkey": scalar_to_bytes_le(shares["view_summed"]).hex(),
        "password": SHARED_WALLET_PASSWORD,
        "restore_height": 0,
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


def mine_to_shared_address(console: Console, daemon_port: int, address: str, blocks: int) -> int:
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
    console.say(f"mining {blocks} blocks to the shared address on daemon port {daemon_port}")
    console.say("a coinbase output is locked for 60 confirmations, so this needs more than 60")
    try:
        result = rpc(daemon_port, "generateblocks",
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
    create_shared_wallet(console, target.wallet_port, shares, address)
    target.shares_path.parent.mkdir(parents=True, exist_ok=True)
    save_shares(target.shares_path, shares, address, network)

    console.step(4, "fund the shared address, or say how to")
    console.say("THE ARITHMETIC IS CONFIRMED against monero-wallet-rpc's own derivation.")
    console.say("Remaining: whether the chain lets the summed key spend it.")
    console.say(f"shares saved to {target.shares_path} (mode 0600, private keys in the clear)")
    if mine_blocks:
        mine_to_shared_address(console, target.daemon_port, address, mine_blocks)
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
    """Steps 2-4 of --sweep: reload the fixture, wait for the coins, spend them out."""
    console.step(2, "reload the shares and re-derive the address")
    load_shares(target.shares_path)
    console.say(f"loaded four shares from {target.shares_path}; both sums recomputed and agree")

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
    parser.add_argument("--daemon-port", type=int, default=DEFAULT_DAEMON_PORT,
                        help="monerod's RPC port, used only by --mine")
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
    target = Target(
        wallet_port=args.port,
        daemon_port=args.daemon_port,
        shares_path=Path(args.shares_file).expanduser(),
    )

    if not args.run and not args.sweep:
        return print_plan(console, target.wallet_port, target.shares_path)

    try:
        console.banner("Monero 2-of-2 shared key -- the last untested claim in the GRC<->XMR work")
        console.step(1, "refuse mainnet, and refuse a wallet that holds anything")
        network = refuse_mainnet_and_a_funded_wallet(
            console, target.wallet_port, target.daemon_port, args.allow_open_wallet
        )
        if args.run:
            return run_phase(console, target, network, args.mine)
        return sweep_phase(console, target, args.sweep, args.wait)
    except VerifyError as error:
        console.check("run", str(error), "no refusal", False)
        return console.summary()


if __name__ == "__main__":
    sys.exit(main())
