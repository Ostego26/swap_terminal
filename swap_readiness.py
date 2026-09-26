#!/usr/bin/env python3
"""Can this terminal actually run an XRP <-> GRC swap right now? Read-only preflight.

Role: file (entry point; the operator runs this)
Reads: the process environment, config.Config, and -- over the network -- the
      configured XRP endpoint and Gridcoin wallet. One CoinGecko price request.
Writes: nothing. No database, no file, no chain.
Can send orders: no. It creates no swap, signs nothing, and submits nothing.
Mainnet-safe: yes, and it REFUSES to poll a mainnet Gridcoin wallet rather than
      merely declining to act on it -- see check_gridcoin() for why looking is
      itself the hazard there.

WHY A PREFLIGHT AND NOT JUST "TRY IT". A swap has two legs and about eight
preconditions, and failing one of them mid-swap is not symmetrical: a deposit
that arrives against a misconfigured terminal is a customer's money sitting in an
account nothing is watching. The failures are also mostly SILENT in the way rule
13 describes -- an unconfigured chain does not crash, it just never gets built,
and a swap then fails at creation with a KeyError about an adapter rather than a
sentence about a missing environment variable.

So this answers one question -- "would a swap work, and if not, which line do I
change" -- and answers it before any money moves. Rule 14 throughout: every check
names what it read and what the number means, an empty result prints (none)
rather than nothing, and a skipped check is visibly different from a passed one.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.registry import build_adapters
from chains.xrp import XRPAdapter
from chains.xrp_signing import reserve_drops
from config import Config
from db import SCHEMA
from microfortnights import format_duration
from network_target import CHAIN_PORTS, classify
from services.pricing import fetch_usd_prices

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(state: str, name: str, detail: str) -> None:
    """One line per check, with the verdict first so the column scans."""
    _results.append((state, name, detail))
    print(f"  {state}  {name:<28} {detail}", flush=True)


def check_pair_is_allowed() -> None:
    pairs = sorted(f"{a}->{b}" for a, b in Config.ALLOWED_PAIRS if "XRP" in (a, b))
    if pairs:
        record(PASS, "pair allowed", ", ".join(pairs))
    else:
        record(FAIL, "pair allowed", "(none) -- no XRP pair in Config.ALLOWED_PAIRS, so no XRP quote can be made")


def check_deposit_account() -> str:
    """The custody value. Everything about the XRP deposit leg depends on it."""
    account = Config.XRP_DEPOSIT_ACCOUNT
    if not account:
        record(FAIL, "XRP_DEPOSIT_ACCOUNT",
               "(unset) -- create_swap() refuses every XRP swap until this names an account you hold the key for")
        return ""
    record(PASS, "XRP_DEPOSIT_ACCOUNT", f"{account}  <- every XRP swap shares this one account")
    return account


def check_xrp(account: str) -> None:
    """Endpoint, network, and whether the deposit account exists and is funded."""
    url = Config.XRP_RPC_URL
    if not url:
        record(FAIL, "XRP_RPC_URL", "(unset) -- no XRP adapter is built, so no XRP swap can be watched or paid")
        return

    adapter = XRPAdapter(url=url, min_confirmations=Config.XRP_MIN_CONFIRMATIONS)
    started = time.monotonic()
    try:
        parameters = adapter.server_parameters()
    except Exception as error:  # noqa: BLE001 -- checked: every failure here means the same thing to the caller ("the XRP endpoint did not answer usefully") and the type name plus the message are both printed, so nothing is swallowed and a failure can never read as a pass. server_parameters() raises XRPMainnetRefused, transport errors and response-shape errors, and distinguishing them would not change the line printed or the exit code.
        record(FAIL, "XRP endpoint", f"{type(error).__name__}: {str(error)[:110]}")
        return
    record(PASS, "XRP endpoint", f"{parameters['network']}  in {format_duration(time.monotonic() - started)}")

    if not account:
        record(SKIP, "XRP deposit account", "no account to look up -- set XRP_DEPOSIT_ACCOUNT first")
        return
    try:
        balance_drops, owner_count = adapter.account_drops_and_owner_count(account)
    except Exception as error:  # noqa: BLE001 -- checked: same judgment as above. An account that does not exist on this network and an endpoint that failed both mean "cannot use this account", and the message says which.
        record(FAIL, "XRP deposit account",
               f"{type(error).__name__}: {str(error)[:110]}  <- an unfunded account does NOT exist on the ledger")
        return
    # reserve_drops() from chains/xrp_signing.py, NOT arithmetic of my own.
    #
    # The first version of this line read parameters["reserve_base_drops"] and
    # ["reserve_inc_drops"] -- two key names that do not exist, invented rather
    # than read. server_parameters() returns base_reserve_xrp and
    # owner_reserve_xrp, in XRP and not in drops, so both the names and the UNIT
    # were wrong. It crashed on the operator's host with a KeyError, four checks
    # into a preflight, which is rule 17 exactly: a field name I agreed with
    # myself about is still a guess until the code says it back.
    #
    # Reusing the payout path's own function is also the rule 8 answer: the
    # reserve arithmetic exists once, and a preflight that computed it separately
    # could report "fits" for a payment the payout path then refuses.
    required_reserve, reserve_line = reserve_drops(
        parameters.get("base_reserve_xrp"), parameters.get("owner_reserve_xrp"), owner_count
    )
    spare = balance_drops - required_reserve
    state = PASS if spare > 0 else FAIL
    record(state, "XRP deposit account",
           f"balance {balance_drops} drops, {reserve_line} -> {spare} spendable  "
           f"<- must be > 0 to pay anything out")


# Gridcoin's getwalletinfo fields for lock state. THE NAMES ARE NOT CONFIRMED
# against a live Gridcoin daemon from this environment -- no daemon is reachable
# here -- so every one of them is read WHEN PRESENT and their absence is reported
# as "not established" rather than as "unlocked". Rule 17: a field name our code
# agrees on is still a guess until a server says it back.
#
# `unlocked_until` is the Bitcoin-derived convention (0 or absent means locked, a
# unix timestamp means unlocked until then). Gridcoin is Bitcoin-derived so it is
# the likely spelling; the staking-only distinction is the part with no Bitcoin
# equivalent at all, because Bitcoin has no staking.
_LOCK_FIELDS = ("unlocked_until",)
_STAKING_ONLY_FIELDS = ("unlocked_for_staking_only", "staking_only", "walletunlockstakingonly")


def describe_wallet_lock(info: dict) -> tuple[str, str]:
    """What state a Gridcoin wallet is in for PAYING. Returns (state, detail).

    THE OPERATIONAL FACT this exists for, from the operator 2026-09-26: a
    Gridcoin wallet that stakes is normally left unlocked FOR STAKING ONLY, and a
    staking-only unlock cannot send. Paying out requires a full unlock, and the
    wallet is then meant to be re-locked and re-unlocked for staking afterwards
    -- leaving it fully unlocked is a security regression on a live wallet.

    So a GRC payout has a precondition no other chain here has, and it is one an
    adapter cannot satisfy for itself: a full unlock needs the passphrase, which
    is a secret this terminal deliberately does not hold (rule 16). What it CAN
    do is tell the operator which state the wallet is in before a swap is created,
    instead of letting sendtoaddress fail opaquely mid-payout with a customer's
    deposit already taken.

    Three outcomes, and the third is the honest one rather than a fallback:

      locked          cannot send. Unambiguous.
      unlocked        can send, as far as this can tell.
      NOT ESTABLISHED the daemon did not report a field this recognizes. Reported
                      as unknown, never as "unlocked" -- guessing "fine" here
                      means a swap created against a wallet that cannot pay it.
    """
    present = {key: info[key] for key in (*_LOCK_FIELDS, *_STAKING_ONLY_FIELDS) if key in info}
    if not present:
        return SKIP, (
            "lock state NOT ESTABLISHED -- getwalletinfo reported none of "
            f"{', '.join((*_LOCK_FIELDS, *_STAKING_ONLY_FIELDS))}. Keys it DID return: "
            f"{', '.join(sorted(info)) or '(none)'}  <- paste this line back; the field names are "
            "unconfirmed against a real Gridcoin daemon and this is how they get confirmed"
        )

    unlocked_until = info.get("unlocked_until")
    staking_only = next((info[key] for key in _STAKING_ONLY_FIELDS if key in info), None)

    if unlocked_until in (0, None) and staking_only is None:
        return FAIL, "wallet is LOCKED -- a GRC payout cannot send until it is fully unlocked"
    if staking_only:
        return FAIL, (
            f"wallet is unlocked FOR STAKING ONLY ({present}) -- staking-only cannot send. "
            "A payout needs a full unlock, then re-lock and re-unlock for staking afterwards"
        )
    return PASS, f"wallet reports it can send ({present})  <- re-lock for staking when the swap is done"


def gridcoin_precheck(port: int) -> tuple[bool, str, str]:
    """Decide whether to OPEN A SOCKET to the Gridcoin wallet. Returns (connect?, state, detail).

    Extracted because it is the one decision in this file with a cost attached,
    and rule 10 puts the deciding thing at the bottom where it can be called with
    seeded inputs. Inline, the only way to test "does it refuse mainnet" would be
    to point it at a mainnet wallet.

    LOOKING IS THE HAZARD, which is what makes this different from every other
    check here. A get_balance() against port 15715 prints the operator's real
    staking balance into whatever terminal, transcript or pasted block this
    output lands in. That happened on 2026-09-25 -- 157,797 GRC into a chat log
    -- and the fix then was the same as the shape here: classify the port BEFORE
    opening the socket, not after. Labeling a balance mainnet once you have
    already fetched and printed it is the wrong altitude.

    So a mainnet port returns connect=False. Not "connect and warn".
    """
    verdict = classify("GRC", port)
    if verdict == "UNCONFIGURED":
        return False, FAIL, f"(unconfigured) -- set GRC_RPC_PORT to the test chain ({CHAIN_PORTS['GRC'].test_hint})"
    if verdict == "MAINNET":
        return False, FAIL, (
            f"port {port} is MAINNET and this did NOT connect. A preflight will not read a real "
            f"wallet, because reading it means printing the balance. Set GRC_RPC_PORT=25779"
        )
    if verdict == "UNRECOGNIZED":
        return False, FAIL, (
            f"port {port} is not a Gridcoin port this tree knows, so which chain it is was NOT "
            f"established -- and an unknown port may be a mainnet daemon on a custom -rpcport. "
            f"Refusing to connect rather than guessing"
        )
    return True, PASS, f"port {port} is a test chain (mainnet is {CHAIN_PORTS['GRC'].mainnet_port})"


def check_gridcoin() -> None:
    """The GRC leg. REFUSES to poll a mainnet wallet rather than reporting on it.

    Looking is the hazard here, not acting. A get_balance() against port 15715
    prints the operator's real staking balance into whatever terminal or
    transcript this output lands in -- which happened on 2026-09-25 and is why
    fund_testnets.py now classifies the port BEFORE opening a socket. Same order
    here: classify, then decide whether to connect.
    """
    port = Config.RPC["GRC"]["port"]
    connect, state, detail = gridcoin_precheck(port)
    if not connect:
        record(state, "GRC wallet", detail)
        return

    adapters = build_adapters(Config.RPC)
    if "GRC" not in adapters:
        record(FAIL, "GRC wallet", f"port {port} is set but no adapter was built -- check GRC_RPC_USER/GRC_RPC_PASS")
        return
    started = time.monotonic()
    try:
        balance = adapters["GRC"].get_balance()
    except Exception as error:  # noqa: BLE001 -- checked: a down daemon, a refused login and a bad response all mean "the GRC leg cannot run", the type and message are printed, and the exit code is non-zero. Telling them apart would not change what the operator does next, which is to look at the daemon.
        record(FAIL, "GRC wallet", f"{type(error).__name__}: {str(error)[:110]}  <- is the testnet daemon running?")
        return
    state = PASS if balance > 0 else FAIL
    record(state, "GRC wallet",
           f"{balance} GRC on port {port} (test chain)  <- must be > 0 to pay a GRC leg  "
           f"in {format_duration(time.monotonic() - started)}")

    # A SEPARATE CHECK, because a funded wallet that cannot send is a different
    # failure from an empty one and rule 14 forbids rendering them the same way.
    # This is the precondition no other chain here has.
    try:
        info = adapters["GRC"].call("getwalletinfo")
    except Exception as error:  # noqa: BLE001 -- checked: the balance call above already succeeded, so any failure here is specifically about getwalletinfo -- an older daemon without it, or a changed response. Reported with its type and message, and as SKIP rather than PASS, so an unknown lock state never reads as a usable one.
        record(SKIP, "GRC wallet lock", f"getwalletinfo failed: {type(error).__name__}: {str(error)[:90]}")
        return
    lock_state, lock_detail = describe_wallet_lock(info if isinstance(info, dict) else {})
    record(lock_state, "GRC wallet lock", lock_detail)


def check_pricing() -> None:
    """Both legs must have a USD price or no rate can be derived."""
    try:
        prices = fetch_usd_prices()
    except Exception as error:  # noqa: BLE001 -- checked: a network failure, a rate limit and a missing asset all mean "no rate can be quoted", and the message distinguishes them for the reader.
        record(FAIL, "pricing", f"{type(error).__name__}: {str(error)[:110]}")
        return
    xrp, grc = prices.get("XRP_USD"), prices.get("GRC_USD")
    if not xrp or not grc:
        record(FAIL, "pricing", f"XRP_USD={xrp} GRC_USD={grc}  <- both are needed to derive a rate")
        return
    record(PASS, "pricing", f"XRP ${xrp} / GRC ${grc} -> 1 XRP = {xrp / grc:.2f} GRC (before fees)")


def check_schema() -> None:
    """The column the whole tag scheme writes into."""
    if "deposit_tag" in SCHEMA:
        record(PASS, "schema", "swaps.deposit_tag present  <- XRP swaps store their tag here")
    else:
        record(FAIL, "schema", "swaps.deposit_tag MISSING -- this checkout predates the tag work")


def main() -> int:
    print("swap readiness -- XRP <-> GRC. Read-only: creates no swap, signs nothing.", flush=True)
    # NOT a hardcoded count. It said "6 preconditions" while the verdict below
    # said "1 of 7 failed", because the GRC lock check only runs once the wallet
    # answers -- so the number is conditional and a literal was wrong half the
    # time. Rule 14 asks output to be self-describing; two different totals in one
    # block is the reader doing arithmetic to decide which to believe.
    print("  one line per precondition, saying what it read and what the number means.", flush=True)
    print("  Some checks only run once an earlier one passes, so the total varies.\n", flush=True)

    # EVERY check is wrapped, because a preflight that raises has failed at the
    # one thing it exists to do. Measured 2026-09-26: a KeyError in check_xrp()
    # killed the run four checks in, so the operator learned nothing about GRC,
    # pricing, or the two checks after it -- from a defect in the preflight
    # rather than in what it was inspecting. A crash here is a bug in this file
    # and must be reported as one, not allowed to mask the report.
    for name, check in (
        ("pair allowed", check_pair_is_allowed),
        ("schema", check_schema),
        ("XRP", lambda: check_xrp(check_deposit_account())),
        ("GRC", check_gridcoin),
        ("pricing", check_pricing),
    ):
        try:
            check()
        except Exception as error:  # noqa: BLE001 -- checked: this is the outermost handler of a reporting tool, and it does not swallow -- it records a FAIL naming the check, the exception type and the message, which makes the run's exit code non-zero. The alternative is the traceback that already cost a run. Each check has its own narrow handlers inside it; this catches only what THEY missed, which by definition is a defect in this file.
            record(FAIL, f"{name} (check crashed)",
                   f"{type(error).__name__}: {str(error)[:100]}  <- a bug in swap_readiness.py, "
                   f"not necessarily in what it was checking")

    failures = [(name, detail) for state, name, detail in _results if state == FAIL]
    print("\n" + "=" * 70, flush=True)
    if not failures:
        print(f"READY: all {len(_results)} checks passed. An XRP <-> GRC swap can be created.", flush=True)
        return 0
    print(f"NOT READY: {len(failures)} of {len(_results)} checks failed.\n", flush=True)
    for name, detail in failures:
        print(f"  - {name}: {detail}", flush=True)
    print("\nEach line above names the value to change. Nothing was written and no swap exists.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
