#!/usr/bin/env python3
"""Can this terminal actually run a swap right now, on the pairs it allows? Read-only preflight.

Role: file (entry point; the operator runs this)
Reads: the process environment, config.Config, and -- over the network -- the
      configured XRP endpoint, the configured Solana cluster and the Gridcoin
      wallet. One CoinGecko price request.
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

IT USED TO ANSWER THAT QUESTION ABOUT THE WRONG SWAP, and that is why the SOL and
unlock checks below exist. Measured 2026-10-01: this file's title said
"XRP <-> GRC", check_pair_is_allowed() filtered Config.ALLOWED_PAIRS to pairs
containing XRP, and there was no SOL check of any kind -- while the direction the
operator was actually rehearsing, three times that day, was SOL -> GRC. A
preflight that passes or fails about a leg nobody is running is worse than no
preflight: it is a verdict, in the register of a measurement, about something
else.

And the precondition that actually broke those three rehearsals was not checked
by anything. All three got the whole way through -- memo attributed, deposit
credited, swap advanced, payout claimed -- and all three died on
`GRIDCOIN_WALLET_PASSPHRASE is not set in this process`s environment`, because the
supervisor had been started from a shell without it, landing the swap in 'failed',
which nothing retries. That check is one line and it is now the first thing
after the pair list, through payout_service.unlock_readiness_lines() rather than
a second reading of the same environment variable (rule 8).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.registry import build_adapters, missing_settings
from chains.solana_units import SOL_DECIMALS, base_units_to_amount
from chains.xrp import XRPAdapter
from chains.xrp_signing import reserve_drops
from config import Config
from db import SCHEMA
from microfortnights import format_duration
from network_target import CHAIN_PORTS, classify, solana_cluster
from services.payout_service import payable_assets, unlock_readiness_lines
from services.pricing import fetch_usd_prices

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(state: str, name: str, detail: str) -> None:
    """One line per check, with the verdict first so the column scans."""
    _results.append((state, name, detail))
    print(f"  {state}  {name:<28} {detail}", flush=True)


#: The FROM legs this file has a check for. Derived into the pair line below so
#: the report can say which allowed pairs it is actually answering about, rather
#: than implying it covers all of them -- it does not, and BTC and LTC legs are
#: unchecked here. Naming the gap beats a silent one (rule 14).
CHECKED_LEGS = ("XRP", "SOL", "GRC")


class PairRefused(RuntimeError):
    """--pair named something this terminal cannot answer about. Nothing was read."""


def parse_pair(text: str) -> tuple[str, str] | None:
    """"SOL:GRC" -> ("SOL", "GRC"), or None for no --pair at all.

    Raises PairRefused for a malformed value or a pair outside ALLOWED_PAIRS,
    rather than quietly checking everything: a typo in a gate's argument that
    widens the gate is the opposite of what a gate is for.
    """
    if not text:
        return None
    cleaned = text.upper().replace("->", ":").replace("/", ":").strip()
    parts = [part.strip() for part in cleaned.split(":") if part.strip()]
    if len(parts) != 2:  # noqa: PLR2004 -- a pair is two assets; naming the 2 would say less than the sentence below
        raise PairRefused(
            f"--pair {text!r} is not a pair. Write it as FROM:TO, for example SOL:GRC. Nothing was read."
        )
    pair = (parts[0], parts[1])
    if pair not in Config.ALLOWED_PAIRS:
        allowed = ", ".join(sorted(f"{a}:{b}" for a, b in Config.ALLOWED_PAIRS))
        raise PairRefused(
            f"--pair {parts[0]}:{parts[1]} is not in Config.ALLOWED_PAIRS, so no quote for it could be "
            f"made whatever this preflight said. Allowed: {allowed}. Nothing was read."
        )
    return pair


def legs_to_check(pair: tuple[str, str] | None) -> tuple[str, ...]:
    """Which chain legs this run should check at all.

    WHY A SCOPE EXISTS, MEASURED 2026-10-01. I handed the operator
    `swap_readiness.py && supervisor.py start` as a gate so that a broken config
    could not spawn workers. That gate is UNSATISFIABLE on their host: XRP is not
    configured and is not going to be -- they are running SOL -> GRC -- so
    XRP_RPC_URL and XRP_DEPOSIT_ACCOUNT fail forever and the verdict is NOT READY
    forever. A gate that can never open is not a gate; it is a thing people learn
    to bypass, which is worse than no gate because the next real failure gets
    bypassed with it.

    The whole-terminal verdict is still the DEFAULT and still right for "is
    everything I own working". What was missing is the question an operator
    actually asks before a run: can THIS pair be created and paid.
    """
    if pair is None:
        return CHECKED_LEGS
    # Only the legs of the named pair, and only those this file knows how to check
    # -- a BTC leg is unverified here and saying so is check_pair_is_allowed()'s job.
    return tuple(leg for leg in CHECKED_LEGS if leg in pair)


def check_pair_is_allowed(pair: tuple[str, str] | None = None) -> None:
    """Every allowed pair, and which of them this preflight actually covers.

    IT USED TO FILTER TO XRP and print the result as `pair allowed`. On the
    operator's host that rendered as

        PASS  pair allowed    GRC->XRP, XRP->BTC, XRP->GRC, XRP->LTC

    with SOL->GRC -- the pair in ALLOWED_PAIRS that was being rehearsed that
    afternoon -- absent from a line whose name promises to list what is allowed.
    Nothing was false; the line simply answered a narrower question than it
    appeared to.
    """
    pairs = sorted(f"{a}->{b}" for a, b in Config.ALLOWED_PAIRS)
    if not pairs:
        record(FAIL, "pair allowed",
               "(none) -- Config.ALLOWED_PAIRS is empty, so no quote of any kind can be made")
        return
    legs = legs_to_check(pair)
    # HOISTED OUT OF THE f-STRING, not a style choice. A multi-line conditional
    # inside an f-string is PEP 701, which is Python 3.12; ruff accepts it here
    # because pyproject.toml sets target-version = "py312", and the interpreter in
    # this environment is 3.11.15, which raises SyntaxError on it.
    #
    # THAT IS ALSO THE ANSWER TO AN EARLIER PUZZLE IN THIS SESSION. `ruff check`
    # reported "All checks passed!" on a file python could not parse, once, and I
    # could not reproduce it deliberately and said so rather than guessing a cause.
    # The cause is this: ruff and the interpreter disagree about what is valid
    # syntax, and ruff is the MORE PERMISSIVE one. So `python -m compileall` beside
    # ruff is not belt-and-braces; it catches a class ruff cannot see.
    scoping = (
        "--pair scoped this run to one pair; every other pair is UNVERIFIED here"
        if pair
        else "No --pair given, so this is the whole terminal"
    )
    covered = sorted(
        f"{a}->{b}" for a, b in Config.ALLOWED_PAIRS
        if a in legs and b in legs and (pair is None or (a, b) == pair)
    )
    record(PASS, "pair allowed", f"{', '.join(pairs)}  <- all {len(pairs)} in Config.ALLOWED_PAIRS")
    record(
        PASS if covered else FAIL,
        "pairs checked here",
        f"{', '.join(covered) or '(none)'}  <- {len(covered)} of {len(pairs)}. {scoping}. Legs "
        f"checked: {', '.join(legs)}; a pair with a BTC or LTC leg is ALLOWED and is NOT verified by "
        f"anything below",
    )


def check_payout_unlock(adapters, pair: tuple[str, str] | None = None) -> None:
    """Whether a payout chain's wallet can be unlocked from THIS process.

    THE PRECONDITION THAT BROKE THREE LIVE REHEARSALS, 2026-10-01, and the one
    nothing checked. Each run reached the payout and died on
    GRIDCOIN_WALLET_PASSPHRASE being unset in the supervisor's environment,
    leaving the swap in 'failed' -- a terminal status nothing retries, so each
    failure cost a whole new swap and a new deposit.

    Through services/payout_service.unlock_readiness_lines(), which is what the
    supervisor's start banner prints, so this preflight and that banner cannot
    come to disagree about whether a payout can be attempted (rule 8). It reports
    PRESENCE and never correctness: a wrong passphrase still fails at the send,
    and saying otherwise here would be the reassuring answer rather than the
    measured one.

    The passphrase itself is never read into a line, never logged, and its length
    is never reported.
    """
    # NOTHING PAYABLE IS A FAIL, NOT A SKIP, and it rendered as a SKIP on the
    # operator's 2026-10-01 run: "no configured chain needs a wallet unlock to pay
    # out", which is true and reads as nothing-to-worry-about. With GRC_RPC_PASS
    # unset the only adapter was SOL, SOL is never a TO asset, and therefore NO
    # swap this terminal allows could ever have been paid. A SKIP beside that is
    # the same did-nothing-looks-like-did-work the supervisor's spawn warning had
    # in the identical case (see services/payout_service.payable_assets()).
    # The pair's OWN destination when one was named, so "nothing can be paid" means
    # "this pair cannot be paid" rather than "no pair anywhere can be".
    wanted = {pair} if pair else Config.ALLOWED_PAIRS
    payable = payable_assets(adapters.keys(), wanted)
    if not payable:
        # The destinations of the SCOPED pairs, not of every allowed pair. Scoped to
        # SOL:GRC this printed "destinations any allowed pair needs: BTC, GRC, LTC,
        # XRP", which names three chains the run was not asking about and buries
        # the one it was -- rule 14's "state what the number means" turned into
        # noise by a set that did not follow the scope.
        needed = ", ".join(sorted({to for _, to in wanted}))
        record(FAIL, "payout chain",
               f"NOTHING CAN BE PAID OUT. Adapters built: {', '.join(sorted(adapters)) or '(none)'}; "
               f"destination(s) needed: {needed}. A deposit would still be watched and CREDITED, and the "
               f"payout would then refuse and land the swap in 'failed', which nothing retries")
        return
    record(PASS, "payout chain", f"{', '.join(sorted(payable))}  <- has an adapter AND is the destination "
                                 f"of an allowed pair. A chain missing from here cannot be paid")

    lines = unlock_readiness_lines(adapters.keys())
    if not lines:
        record(SKIP, "payout unlock",
               f"no payable chain needs a wallet unlock  <- GRC is the only one that does, and it is not "
               f"in {', '.join(sorted(payable))}")
        return
    for line in lines:
        # The shared function returns a whole banner line, label and all. Only the
        # detail belongs in this file's verdict column, so the asset prefix it
        # already carries is stripped rather than printed twice.
        detail = line.strip()
        blocked = "IS NOT SET" in detail
        record(FAIL if blocked else PASS, "payout unlock", detail)


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


# Gridcoin's getwalletinfo fields for lock state, MEASURED rather than guessed.
#
# Established 2026-09-26 from the wallet's own `help wallet` output and from a
# live getwalletinfo on the operator's testnet GUI wallet (rpcport 25715):
#
#   * `unlocked_until` EXISTS and is the only lock-related field getwalletinfo
#     returns. The live response was exactly {'unlocked_until': 0}.
#   * `walletpassphrase <passphrase> <timeout> [stakingonly]` is the unlock, and
#     that third parameter is the staking-only switch. So the STATE exists.
#   * NO wallet-category RPC reports it back. The full command list is
#     getwalletinfo, walletlock, walletpassphrase, walletpassphrasechange,
#     walletdiagnose -- and only getwalletinfo introspects, with that one field.
#
# THREE INVENTED FIELD NAMES WERE DELETED FROM HERE: unlocked_for_staking_only,
# staking_only, walletunlockstakingonly. None of them exists. They were a guess at
# a name for a field that is not returned at all, and rule 2 says delete a dead
# guess rather than leave it looking authoritative -- a reader would have taken
# that tuple for a list of things somebody had seen.
#
# WHAT FOLLOWS FROM IT, and it is a limitation rather than a bug: a staking-only
# unlock and a full unlock may be indistinguishable over RPC. If both set
# `unlocked_until` to a timestamp, then this check cannot tell "can send" from
# "staking only, cannot send", and the only way to learn which is to attempt the
# send. That is why a timestamp is NOT reported as "can send" below -- it is
# reported as "unlocked, but staking-only cannot be ruled out from here".
#
# SETTLED 2026-09-26, by the one unlock cycle this paragraph asked for. A
# staking-only unlock SETS unlocked_until to its timeout, it does not leave it at 0:
#
#     walletpassphrase <phrase> 31536000 true   ->   unlocked_until 1821989553
#
# So the two states are:
#
#   unlocked_until == 0        LOCKED, unambiguously. Not staking either.
#   unlocked_until > now       unlocked -- and staking-only versus full is STILL not
#                              distinguishable from this field, which is the
#                              limitation the paragraph above describes and it stands.
#
# WHY THIS WAS BELIEVED THE OTHER WAY. The operator's earlier run read
# `unlocked_until 0` on a wallet they described as "regularly unlocked for staking",
# which looked like evidence that staking-only reports 0. It was not: the staking
# unlock had been FAILING, because this codebase was sending it a timeout of 0 that
# Gridcoin refuses with rpc -8 (see chains/gridcoin_wallet_lock.py). The wallet
# really was locked, and the 0 was correct. A measurement taken through a bug
# measured the bug.
_LOCK_FIELDS = ("unlocked_until",)


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
    present = {key: info[key] for key in _LOCK_FIELDS if key in info}
    if not present:
        return SKIP, (
            "lock state NOT ESTABLISHED -- getwalletinfo reported none of "
            f"{', '.join(_LOCK_FIELDS)}. Keys it DID return: "
            f"{', '.join(sorted(info)) or '(none)'}  <- paste this line back; the field names are "
            "unconfirmed against a real Gridcoin daemon and this is how they get confirmed"
        )

    unlocked_until = info.get("unlocked_until")

    if unlocked_until in (0, None):
        # Echoes what it READ, which the first version did not -- and that omission
        # cost a measurement. The operator's run 2026-09-26 printed this exact line
        # for a wallet they described as normally "regularly unlocked for staking",
        # and without the fields there was no way to tell which of two things was
        # true: the wallet really was locked, or Gridcoin reports unlocked_until=0
        # for a staking-only unlock as well. Both cannot send, so the VERDICT is
        # right either way -- but "which field distinguishes staking-only" is still
        # unmeasured, and a line that showed its inputs would have answered it.
        return FAIL, (
            f"wallet is LOCKED ({present}) -- a GRC payout cannot send, and it is not staking "
            f"either. This is now UNAMBIGUOUS: measured 2026-09-26, a staking-only unlock sets "
            f"unlocked_until to its timeout rather than leaving it at 0, so a 0 here means "
            f"locked and nothing else. payout_worker performs the full unlock itself when "
            f"GRIDCOIN_WALLET_PASSPHRASE is set; this line is about the resting state"
        )
    # A timestamp is NOT reported as "can send", and the 2026-09-26 measurement
    # CONFIRMED that caution rather than removing it: a staking-only unlock does set
    # unlocked_until, so this state genuinely covers both, and one of the two cannot
    # send. The `if` above was written when that was a hypothetical; it is now the
    # measured case. Saying PASS here would be a guess in the voice of a measurement
    # about the one thing that decides whether a payout works.
    #
    # A staking wallet in its normal resting state lands HERE, not in the locked
    # branch, and that is expected rather than a warning about the wallet.
    return SKIP, (
        f"wallet is UNLOCKED until {unlocked_until} ({present}) -- but Gridcoin returns no field "
        f"saying whether that unlock was `walletpassphrase ... stakingonly`, and a staking-only "
        f"unlock CANNOT send. So this is not confirmation it can pay. If the payout fails, unlock "
        f"again with stakingonly omitted; re-lock and re-unlock for staking when the swap is done"
    )


def explain_grc_failure(error: Exception, port: int) -> str:
    """What a failed Gridcoin RPC call actually TELLS you. Returns the detail line.

    Added 2026-09-26 because the previous version appended one hardcoded hint --
    "is the testnet daemon running?" -- to every failure, and the operator's run
    produced a 401. A 401 PROVES the daemon is running: something accepted the
    connection, parsed the request and rejected the credentials. The hint asserted
    the opposite of what the response established, which sends a reader to restart
    a wallet that was working.

    That is the same defect as xrp_chain_check.py's "the decoder is wrong" line,
    one file over: one message covering several situations that mean different
    things, so it could only be right about one of them. Here the shape matters
    more than usual, because the wrong hint's remedy is "restart the staking
    wallet" -- and a needless restart of a staking wallet is a real cost, which
    this session has already caused once by misreading a different signal.

    The three cases a reader must be able to tell apart:

      401 / 403     it IS listening. The credentials are wrong or absent.
      refused       nothing is listening on that port.
      anything else reported as itself, with no hint invented for it.
    """
    text = str(error)
    if "401" in text or "403" in text or "Authorization" in text:
        return (
            f"{type(error).__name__}: the wallet IS listening on {port} and REJECTED the "
            f"credentials. The daemon is fine -- do not restart it. Check GRC_RPC_USER and "
            f"GRC_RPC_PASS against rpcuser/rpcpassword in the conf that wallet actually reads "
            f"(and check you exported the VALUE, not a placeholder)"
        )
    if "refused" in text.lower() or "NewConnectionError" in text or "Max retries" in text:
        return (
            f"{type(error).__name__}: nothing is listening on {port}. The wallet is not "
            f"running, or is running without server=1, or is reading a different conf with a "
            f"different rpcport"
        )
    return f"{type(error).__name__}: {text[:150]}  <- reported as-is; this failure has no known interpretation"


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
        # THIS BRANCH WAS UNREACHABLE UNTIL 2026-09-26 AND ITS ADVICE WAS RIGHT
        # ANYWAY. build_adapters() tested the port alone, so once the port was set
        # GRC was always in adapters -- the FAIL could not fire, while the sentence
        # it would have printed ("check GRC_RPC_USER/GRC_RPC_PASS") named exactly
        # the values that were being ignored. The registry now requires all three,
        # so this fires, and it names which of them is actually missing rather than
        # listing two for the operator to check by hand.
        missing = missing_settings(Config.RPC, "GRC")
        record(
            FAIL,
            "GRC wallet",
            f"port {port} is set but no adapter was built: {', '.join(missing)} unset in THIS process. "
            f"chains/base.py authenticates with (user, password) and cannot read a cookie file, so an "
            f"adapter without them would 401 on every call -- which is why it is not built at all.",
        )
        return
    started = time.monotonic()
    try:
        balance = adapters["GRC"].get_balance()
    except Exception as error:  # noqa: BLE001 -- checked: a down daemon, a refused login and a bad response all mean "the GRC leg cannot run", the type and message are printed, and the exit code is non-zero. Telling them apart would not change what the operator does next, which is to look at the daemon.
        record(FAIL, "GRC wallet", explain_grc_failure(error, port))
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


def check_solana(adapters) -> None:
    """The SOL DEPOSIT leg: cluster, deposit account, and the discovery read path.

    SOL IS ONLY EVER A FROM ASSET HERE, which is what these checks are shaped
    around. config.ALLOWED_PAIRS carries ("SOL", "GRC") and deliberately not the
    reverse -- chains/solana.py cannot sign, so a swap whose TO asset is SOL could
    be quoted, could take a deposit, and could never be paid out. So there is no
    hot-wallet check and no unlock check for SOL; adapter.get_balance() is not
    called at all, because it reads SOL_HOT_WALLET, which this direction does not
    use and which being unset is not a defect.

    What CAN go wrong on the deposit leg, in the order it is checked:

      no SOL_RPC_URL           chains/registry builds no SOL adapter, so no SOL
                               deposit is ever seen. Silent -- the watcher logs
                               "SOL not configured" once a cycle and keeps going.
      the wrong cluster        identified by GENESIS HASH, never by the hostname,
                               because a "devnet" alias can point at mainnet and
                               the word in a banner would then escort an operator
                               all the way to a real transfer.
      no SOL_DEPOSIT_ACCOUNT   create_swap() refuses every SOL swap while it is
                               empty. A pair being ALLOWED and a swap being
                               creatable are two different gates, and only the
                               first one is visible in the pair line above.
      the account does not     every SOL swap shares this one account; a deposit
      exist on the cluster     to an account the cluster has never seen cannot be
                               read back, and on devnet an unfunded account does
                               not exist.
    """
    url = Config.RPC.get("SOL", {}).get("url", "")
    if not url:
        record(FAIL, "SOL_RPC_URL",
               "(unset) -- chains/registry builds no SOL adapter, so no SOL deposit is ever seen and no "
               "SOL swap can be watched. The watcher does NOT fail on this; it logs 'SOL not configured' "
               "once a cycle and credits nothing, forever")
        return
    if "SOL" not in adapters:
        record(FAIL, "SOL adapter",
               f"SOL_RPC_URL is set to {url} but no adapter was built: "
               f"{', '.join(missing_settings(Config.RPC, 'SOL')) or '(registry gave no reason)'}")
        return

    adapter = adapters["SOL"]
    started = time.monotonic()
    try:
        genesis = str(adapter.call("getGenesisHash"))
    except Exception as error:  # noqa: BLE001 -- checked: a dead cluster, an HTTP 429 and a malformed response all mean "the Solana endpoint did not answer", the type and the message are both printed, and the exit code is non-zero. Telling them apart would not change what the operator does next, which is to look at the endpoint. A 429 in particular is what this whole preflight is for: it is the failure that killed a live deposit watcher on 2026-10-01.
        record(FAIL, "SOL endpoint", f"{type(error).__name__}: {str(error)[:110]}  <- {url}")
        return
    cluster = solana_cluster(genesis)
    # MAINNET is a FAIL here, not a note. Every address and keypair this project has
    # used is a devnet one, and the only reason to be pointed at mainnet-beta during a
    # rehearsal is a mistake -- the same judgment check_gridcoin() makes about port
    # 15715, where looking is itself the hazard.
    record(FAIL if cluster.startswith("MAINNET") else PASS, "SOL cluster",
           f"{cluster}  (genesis {genesis})  in {format_duration(time.monotonic() - started)}  "
           f"<- identified by genesis hash, NOT by the hostname in {url}")

    account = Config.SOL_DEPOSIT_ACCOUNT
    if not account:
        record(FAIL, "SOL_DEPOSIT_ACCOUNT",
               "(unset) -- create_swap() refuses every SOL swap until this names an account you hold the "
               "key for. The SOL->GRC pair being allowed does not make a SOL swap creatable")
        return
    if not adapter.validate_address(account):
        record(FAIL, "SOL_DEPOSIT_ACCOUNT",
               f"{account} is not a valid Solana account  <- about half of all 32-byte base58 strings are "
               f"off-curve and are refused; this is checked before anything is told to send there")
        return

    started = time.monotonic()
    try:
        result = adapter.call("getBalance", account, {"commitment": "finalized"})
        lamports = int(result["value"] if isinstance(result, dict) else result)
    except Exception as error:  # noqa: BLE001 -- checked: same judgment as the genesis call. Every failure means "this account's state could not be read", which is reported as a FAIL with the type and message, never as a zero balance -- a swallowed outage reading as an empty account is the shape rule 12 names.
        record(FAIL, "SOL deposit account", f"{type(error).__name__}: {str(error)[:110]}")
        return
    # ZERO IS NOT A FAILURE FOR A DEPOSIT ACCOUNT and that is the opposite of the
    # XRP and GRC checks above, where a zero balance means nothing can be paid out.
    # Nothing is ever SENT from this account -- it only receives -- so what matters
    # is that the cluster knows it, which a successful getBalance establishes.
    record(PASS, "SOL deposit account",
           f"{account}  {lamports} lamports ({base_units_to_amount(lamports, SOL_DECIMALS)} SOL)  "
           f"in {format_duration(time.monotonic() - started)}  <- shared by EVERY SOL swap; attribution is "
           f"by memo, not by address. A zero balance is fine: nothing is ever sent FROM here")


def check_pricing(pair: tuple[str, str] | None = None) -> None:
    """Every asset a CHECKED pair needs must have a USD price, or no rate exists.

    IT USED TO CHECK EXACTLY TWO, XRP and GRC, hardwired. On 2026-10-01 that
    printed a PASS for a terminal whose live direction was SOL -> GRC, about two
    assets, one of which was not in the pair being run -- and it would have printed
    the same PASS with SOL_USD missing entirely, which is the failure that renders
    in a customer's browser as a KeyError repr (the same shape
    get_network_fee_reserve() was rewritten for). The assets are derived from
    CHECKED_LEGS and ALLOWED_PAIRS now, so enabling a pair cannot leave this check
    silently answering about the old one (rule 11).

    The rate is printed per checked pair rather than as a single number, because
    "1 XRP = N GRC" says nothing about whether SOL can be quoted.
    """
    try:
        prices = fetch_usd_prices()
    except Exception as error:  # noqa: BLE001 -- checked: a network failure, a rate limit and a missing asset all mean "no rate can be quoted", and the message distinguishes them for the reader.
        record(FAIL, "pricing", f"{type(error).__name__}: {str(error)[:110]}")
        return
    legs = legs_to_check(pair)
    pairs = sorted(
        (a, b) for a, b in Config.ALLOWED_PAIRS
        if a in legs and b in legs and (pair is None or (a, b) == pair)
    )
    assets = sorted({asset for pair in pairs for asset in pair})
    usd = {asset: prices.get(f"{asset}_USD") for asset in assets}
    missing = [asset for asset, price in usd.items() if not price]
    shown = ", ".join(f"{asset} ${price}" for asset, price in usd.items())
    if missing:
        record(FAIL, "pricing",
               f"no USD price for {', '.join(missing)}  <- every checked pair needs BOTH legs priced, or "
               f"create_quote() raises and the browser renders the exception. Read: {shown or '(none)'}")
        return
    rates = ", ".join(f"1 {a} = {usd[a] / usd[b]:.4f} {b}" for a, b in pairs)
    record(PASS, "pricing", f"{shown}  ->  {rates or '(no checked pair to rate)'}  <- before fees")


def check_schema() -> None:
    """The column the whole tag scheme writes into."""
    if "deposit_tag" in SCHEMA:
        record(PASS, "schema", "swaps.deposit_tag present  <- XRP swaps store their tag here")
    else:
        record(FAIL, "schema", "swaps.deposit_tag MISSING -- this checkout predates the tag work")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Can this terminal create and pay a swap right now? Read-only preflight.",
        epilog=(
            "With no --pair it answers about every pair it can check, which is the right question for "
            "'is everything I own working'. With --pair it answers about ONE pair, which is the question "
            "to gate a run on -- an unconfigured chain you are not using should not block a direction "
            "that works."
        ),
    )
    parser.add_argument(
        "--pair", default="", metavar="FROM:TO",
        help="scope every check to one pair, e.g. SOL:GRC. The exit code then describes that pair alone.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pair = parse_pair(args.pair)
    except PairRefused as refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2
    scope = f"{pair[0]} -> {pair[1]} ONLY" if pair else "every pair this terminal allows"
    print(f"swap readiness -- {scope}. Read-only: creates no swap, signs nothing.", flush=True)
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
    # BUILT ONCE and passed in, rather than each check calling build_adapters()
    # for itself. Three callers would be three chances to disagree about what is
    # configured, and check_payout_unlock() in particular must see the SAME set
    # the chain checks saw -- a passphrase warning for a chain with no adapter is
    # the cried-wolf noise rule 14 refuses, and a MISSING warning for one that
    # does have an adapter is the failure that cost three rehearsals.
    #
    # Wrapped, because build_adapters() reads config for every chain and a raise
    # here would kill the preflight before its first line -- which is the defect
    # the per-check wrapper below exists for, one level up.
    try:
        adapters = build_adapters(Config.RPC)
    except Exception as error:  # noqa: BLE001 -- checked: this is a reporting tool and the alternative is a traceback instead of a report. It records a FAIL naming the exception, so the exit code is non-zero and the operator sees which call failed; the chain checks below each re-derive what they need and will fail individually with their own sentences.
        record(FAIL, "adapters (build crashed)",
               f"{type(error).__name__}: {str(error)[:100]}  <- a bug in chains/registry.py or in config, "
               f"not in any one chain. Every chain check below will fail for lack of an adapter")
        adapters = {}
    # FAIL ON ZERO, and the first run of this line is why. It read
    #
    #     PASS  adapters built    (none)
    #
    # in a shell with nothing exported: a green verdict on a terminal that cannot
    # reach a single chain. That is rule 13's "'skipped' plus 'success' in the same
    # output is a defect in the OUTPUT" and rule 14's "make did-nothing look
    # different from did-work", in one line, printed by the tool whose whole job is
    # to not do that.
    built = ", ".join(sorted(adapters)) or (
        "(none) -- NOTHING is reachable, so every chain check below fails for the same one reason"
    )
    record(FAIL if not adapters else PASS, "adapters built",
           f"{built}  <- the chains this process can reach at all. A chain absent here is one no worker "
           f"will touch, silently")

    # SCOPED, and the legs a --pair does not name are SKIPPED RATHER THAN DROPPED.
    # A check that vanishes leaves a reader unable to tell "not asked" from "not
    # run" (rule 14), and the tally at the bottom counts what it printed.
    legs = legs_to_check(pair)
    for name, check in (
        ("pair allowed", lambda: check_pair_is_allowed(pair)),
        ("schema", check_schema),
        ("payout unlock", lambda: check_payout_unlock(adapters, pair)),
        ("XRP", lambda: check_xrp(check_deposit_account())),
        ("SOL", lambda: check_solana(adapters)),
        ("GRC", check_gridcoin),
        ("pricing", lambda: check_pricing(pair)),
    ):
        if name in CHECKED_LEGS and name not in legs:
            record(SKIP, name, f"not checked: --pair {pair[0]}:{pair[1]} has no {name} leg")
            continue
        try:
            check()
        except Exception as error:  # noqa: BLE001 -- checked: this is the outermost handler of a reporting tool, and it does not swallow -- it records a FAIL naming the check, the exception type and the message, which makes the run's exit code non-zero. The alternative is the traceback that already cost a run. Each check has its own narrow handlers inside it; this catches only what THEY missed, which by definition is a defect in this file.
            record(FAIL, f"{name} (check crashed)",
                   f"{type(error).__name__}: {str(error)[:100]}  <- a bug in swap_readiness.py, "
                   f"not necessarily in what it was checking")

    failures = [(name, detail) for state, name, detail in _results if state == FAIL]
    print("\n" + "=" * 70, flush=True)
    if not failures:
        subject = f"{pair[0]} -> {pair[1]}" if pair else "Every pair named on the `pairs checked here` line"
        print(f"READY: all {len(_results)} checks passed. {subject} can be created and paid.", flush=True)
        return 0
    print(f"NOT READY for {scope}: {len(failures)} of {len(_results)} checks failed.\n", flush=True)
    for name, detail in failures:
        print(f"  - {name}: {detail}", flush=True)
    print("\nEach line above names the value to change. Nothing was written and no swap exists.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
