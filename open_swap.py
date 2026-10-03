#!/usr/bin/env python3
"""Open ONE swap from the shell, so testing a pair end to end needs no browser.

Role: file (entry point, at the repository root per CLAUDE.md rule 10)
Reads: config.Config (ALLOWED_PAIRS, fee, quote TTL, amount tolerance, the RPC
       endpoints and the database path), swap_terminal.db, CoinGecko through
       services/pricing.py, and the destination chain's daemon for
       validate_address()
Writes: swap_terminal.db, and ONLY with --apply: one `quotes` row, one `swaps`
       row, one `swap_audit_log` row, and -- for a tag-attributed source chain
       -- one `xrp_destination_tags` row. Every one of those is written by the
       service functions this file CALLS; there is no INSERT, no UPDATE and no
       tag allocation in this file. Without --apply it writes nothing at all,
       not even the schema, and it does not create the database file.
Can move funds: no. It signs nothing and broadcasts nothing. It does fix two
       things that are FINAL, and both happen only with --apply:
         - the PAYOUT ADDRESS, which workers/payout_worker.py later broadcasts
           to and which cannot be changed afterward;
         - on an address-attributed source chain (BTC, LTC, GRC), a fresh
           deposit key DERIVED in the hot wallet by getnewaddress.
       That is the whole reason --apply is not the default.
Mainnet-safe: it chooses no network. It reaches whatever endpoints
       config.Config names, so a GRC_RPC_PORT of 15715 points this at real
       money, and XRP_DEPOSIT_ACCOUNT is whatever the operator set.

       IT DOES NOT PRINT WHICH NETWORK EACH CHAIN IS ON, and this paragraph
       claimed it did. Three reviewers flagged the same sentence on 2026-09-26:
       it said the file prints every configured chain's target "through the same
       network_target.classify() the workers' banner uses". This module does not
       import network_target and prints no such line. A false claim in the
       Mainnet-safe field is worse than an absent one -- it is the field a reader
       checks precisely when they are deciding whether it is safe to run, and it
       was telling them a guard existed that did not.

       What it DOES print is `adapters here`, the chains this process built, and
       the deposit and payout targets. To see which NETWORK those endpoints are,
       run `python3 swap_readiness.py`, which asks the daemon rather than reading
       a port table. Wiring classify() in here is worth doing and is not done.

WHY THIS EXISTS, MEASURED 2026-09-26.

The operator pasted a four-command sequence twice -- send the deposit, watch it
credit, pay it out, verify. Both times the first two commands printed

    REFUSED: no XRP swap is awaiting a deposit in .../swap_terminal.db. (none)
    is the answer, not an error -- create one in the web UI first. Nothing was
    sent.

and the watcher and the payout worker then found nothing to do. Four commands,
two runs, zero work, because creating the swap needed a browser while the rest
of the loop is a terminal. That refusal named the web UI, which is the defect
rather than the remedy: a terminal tool pointing at a GUI. It now names this
file, and this file prints the next command back with the real swap id already
in it.

WHAT THIS FILE DOES NOT DO, AND IT IS THE LOAD-BEARING PART.

It reimplements no part of the two service functions. There is no INSERT here,
no tag allocation, no address validation of its own, no rate derivation and no
fee arithmetic. `services/quote_service.create_quote()` fixes the rate and
`services/swap_service.create_swap()` writes the swap and allocates the tag,
exactly as `POST /api/quotes` and `POST /api/swaps` do -- so a swap opened from
this terminal and a swap opened from the browser are the same rows, written by
the same code, and a fix to either service reaches both callers.

The refusals are the services' own, surfaced rather than duplicated:

    pair not in ALLOWED_PAIRS     services/quote_service.validate_pair()
    chain has no adapter here     chains/registry.unconfigured_chains() and
                                  why_unconfigured(), the same two functions
                                  create_swap() calls for the same sentence
    payout address invalid        adapters[to_asset].validate_address()
    XRP_DEPOSIT_ACCOUNT unset     services/swap_service.deposit_account()

Three of those are called HERE as well, before the quote is priced, and that is
deliberate rather than a second gate: they are the same functions, so the
sentence cannot drift, and checking early means an unreachable chain or a
mistyped payout address does not leave a priced `quotes` row behind. create_swap()
still runs every one of its own checks afterward; nothing here bypasses it, and
the cost is one extra `validateaddress` call per --apply run.

THE THREE JUDGMENT CALLS, AND WHAT DECIDED THEM.

1. DRY RUN BY DEFAULT. Measured 2026-09-26 by reading the argument parser of
   every .py at the repository root, rather than by recalling a convention:

       xrp_send_tagged.py        --send    submits a testnet Payment
       xrp_payout_verify.py      --send    submits a testnet Payment
       migrate_deposit_vouts.py  --apply   deletes deposit_events rows
       install_desktop_icon.py   --apply   writes files
       swap_readiness.py         (none)    read-only, so no flag
       xrp_chain_check.py        (none)    read-only
       solana_chain_check.py     (none)    read-only

   Four writers, four opt-in flags that default off. The first draft of this
   paragraph said "zero exceptions", and that was an overclaim worth correcting
   rather than quietly dropping, because two root scripts do NOT follow it:

       fund_testnets.py          every action is behind its own chain flag
                                 (--btc, --xrp, ...), so a bare run does
                                 nothing and the action flag IS the opt-in.
                                 A different shape, same direction.
       regtest_htlc_verify.py    acts on a bare run -- but against throwaway
                                 regtest chains it starts itself, so there is
                                 nothing to write to yet.

   Neither is a counterexample to the direction; both are a reason not to claim
   more than was read. `--apply` rather than `--send` because `--send` in this
   tree means "broadcast to a chain" and this broadcasts nothing; `--apply` is
   what the two database/filesystem writers use.

   A swap row is not a fund movement, but it allocates a destination tag
   that db.py's triggers make immutable and undeletable and that is never
   reused, it fixes a payout address that cannot be changed afterward, and on
   BTC/LTC/GRC it derives a key in the hot wallet. The dry run is also the one
   chance to read the payout address back before it is final -- a base58 typo
   that still checksums is unlikely, but the address is the only value a human
   types here.

2. A FAILED PRICE FETCH IS A SENTENCE, NOT A TRACEBACK. create_quote() calls
   services/pricing.fetch_usd_prices(), which is the one outbound HTTP call on
   this path and which deliberately RAISES rather than returning a stale or
   zero price (its header says why: `except Exception: return 0` would make "the
   API is down" indistinguishable from "this asset is worthless", and the second
   pays out zero). So the failure is real and reaches here. It is caught by
   name, never broadly -- see fetch_prices_or_refuse() for the four families and
   where each comes from -- and reported as a refusal that says nothing was
   written.

3. AN ALREADY-OPEN SWAP WARNS, AND DOES NOT REFUSE. A second swap awaiting a
   deposit breaks `xrp_send_tagged.py --swap latest`, which refuses on ambiguity
   by design: "which one you meant is not knowable from here." But it IS knowable
   here -- this file just created one and prints `--swap <that id>` with the real
   id, so the condition the refusal exists for cannot arise from following the
   printed command. Refusing would also strand the operator after one abandoned
   swap, and attribution between two open XRP swaps is sound and tested
   (tests/test_xrp_swap_attribution.py: one shared account, distinct tags, the
   filter keys on the tag). So it lists them, says what `latest` will now do,
   and continues.
"""

from __future__ import annotations

import argparse
import math
import shlex
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.base import RPCError
from chains.registry import unconfigured_chains, why_unconfigured
from chains.solana import SolanaRPCError
from chains.xrp import XRPRPCError
from config import Config
from db import SCHEMA, apply_migrations, connect_db, db_session
from microfortnights import format_duration
from modules.address_authority import check_address
from report_block import CONTINUATION, labeled
from requests.exceptions import RequestException
from services.pair_view import allowed_pair_rows
from services.payout_capacity import as_amount, largest_fundable_payout
from services.pricing import fetch_usd_prices
from services.quote_service import create_quote, get_network_fee_reserve, validate_pair
from services.swap_service import (
    TAG_ATTRIBUTED_ASSETS,
    create_swap,
    deposit_account,
    payout_source_account,
)
from services.xrp_tag_service import XRPTagAllocationError
from workers.common import build_adapters_from_config, db_path_source, get_config_dict


class SwapRefused(RuntimeError):
    """This run will not open a swap, and nothing has been written.

    Its own type, and ONE place prints it (main()), for the reason
    migrate_deposit_vouts.MigrationRefused gives: a refusal is a sentence the
    operator has to act on, and a traceback buries it under a stack they cannot
    act on. Every check below raises this rather than printing and returning, so
    there is exactly one "REFUSED:" in the file and every refusal reads the same.
    """


# EVERY ADAPTER TRANSPORT ERROR, so a daemon that cannot be asked is a sentence
# rather than a traceback. One per adapter family, which is the complete set
# chains/registry.build_adapters() can construct -- grepped 2026-09-26 for
# `^class .*Error` across chains/: base.RPCError (BTC, LTC, GRC),
# xrp.XRPRPCError, solana.SolanaRPCError. All three are listed even though
# ALLOWED_PAIRS mentions only BTC, LTC, GRC and XRP today, because listing only
# the reachable ones is rule 8's drift with a delay on it: adding a SOL pair
# would then turn a down daemon back into a traceback, and nothing would point
# at this line.
#
# They are NOT collapsed with SwapRefused. An RPC failure means "the daemon could
# not be asked", which is a different fact from "the answer is no" -- the
# distinction chains/base.validate_address() exists to preserve, and the one whose
# loss sent an operator to check a customer's address during an outage.
ADAPTER_ERRORS = (RPCError, XRPRPCError, SolanaRPCError)

# Accepted spellings of the pair argument. Both are shell-safe, which is the
# whole selection rule: `XRP->GRC` is NOT accepted and never will be, because
# `>` is a redirect and an unquoted arrow would silently create a file called
# GRC and hand argparse half a pair.
PAIR_SEPARATORS = (":", "/")

# A pair names exactly two assets. A constant rather than a bare `!= 2` because
# the literal alone says nothing about what is being counted, and this is the
# check that decides whether `--pair XRP:GRC:LTC` is read as a direction.
ASSETS_PER_PAIR = 2

# The root-level tool that pays a deposit INTO a swap, per source asset.
#
# One entry, and that is a measurement rather than an omission: grepped the root
# 2026-09-26 for anything that submits a payment. xrp_send_tagged.py sends
# testnet XRP to a swap's (account, tag); xrp_payout_verify.py sends XRP but pays
# OUT, standing in for the payout worker; fund_testnets.py mines regtest coins or
# asks a faucet, into the wallet rather than into a swap; regtest_htlc_verify.py
# drives an HTLC, not a brokered swap. So for a BTC, LTC or GRC deposit there is
# no scripted next command and next_command() says so instead of inventing one.
DEPOSIT_SENDERS = {"XRP": "python3 xrp_send_tagged.py --swap {swap_id}"}

# THE LABEL COLUMN IS SHARED, and it moved out of this file on 2026-09-26.
#
# Every block this file prints goes through labeled(), so the values line up
# whether they come from the header, the deposit preview or the report -- and a
# reader scanning for one number is scanning a column rather than a ragged left
# edge. Hand-padding each f-string is how it goes ragged: the first draft of
# this file printed `min confirmations` (17 characters) and `estimated payout`
# (16) into a 16-wide column and both lines stuck out.
#
# It lives in swap_terminal/report_block.py now because show_swap.py prints the
# same block, and a second copy of a layout is rule 8's shape at its smallest:
# two tools whose columns agree until one of them widens. The names are imported
# rather than re-exported by hand, so `open_swap.LABEL_WIDTH` still resolves for
# the test that checks this file's own output.


def parse_pair(text: str) -> tuple[str, str]:
    """`FROM:TO` -> ("FROM", "TO"). The one place a pair argument becomes two assets.

    Upper-cased and stripped here because every line below uses the tokens before
    create_quote() is reached -- the adapter lookups, the deposit preview, the
    refusal messages. create_quote() normalizes its own arguments again
    (quote_service.py:34-35) and that is not a duplicate to remove: it is the
    service defending itself against every caller, and the two agree because they
    do the same two operations. Removing this one would make `--pair xrp:grc`
    look up `adapters["xrp"]` and report XRP as unconfigured while the quote
    priced fine.
    """
    cleaned = text.strip()
    for separator in PAIR_SEPARATORS:
        if separator in cleaned:
            parts = [part.strip().upper() for part in cleaned.split(separator)]
            if len(parts) != ASSETS_PER_PAIR or not all(parts):
                raise SwapRefused(
                    f"--pair {text!r} does not name two assets. Write it as FROM{separator}TO, for example "
                    f"XRP{separator}GRC. Nothing was written."
                )
            return parts[0], parts[1]
    raise SwapRefused(
        f"--pair {text!r} has no separator, so which asset is the source is not knowable from it. Write "
        f"FROM:TO (or FROM/GRC-style with a slash), for example XRP:GRC. An arrow is deliberately not "
        f"accepted: `>` is a shell redirect. Nothing was written."
    )


def swappable_amount(raw: str) -> float:
    """An amount a swap can be created for, or an argparse refusal saying why not.

    type=float ACCEPTED INFINITY. Flagged by review 2026-09-26: `--amount 1e400` is
    a valid float literal that overflows to inf, and float("nan") parses too, so a
    swap row was written with expected_input_amount = inf. Everything downstream
    then compares a deposit against infinity -- the tolerance band is
    inf * 0.01, which is inf -- so no deposit could ever match and the swap would sit
    at awaiting_deposit forever with a deposit address the operator had already been
    handed.

    NOT a second copy of create_quote()'s own `input_amount <= 0` check, which still
    runs and is still the authority. This refuses at the ARGUMENT, before a database
    file is created or a price is fetched, and it refuses the two values that check
    cannot see: inf passes `<= 0`, and so does nan (every comparison with nan is
    False, so `nan <= 0` is False and it sails through).
    """
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number") from error
    if math.isnan(value):
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not-a-number. It would pass every `<= 0` check downstream, because every "
            f"comparison with nan is False"
        )
    if math.isinf(value):
        raise argparse.ArgumentTypeError(
            f"{raw!r} overflows to infinity. A swap expecting an infinite deposit can never be "
            f"satisfied: the tolerance band around it is also infinite"
        )
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} is not positive, and a swap deposits something")
    return value


def pair_catalog(config: dict, adapters: dict | None = None) -> str:
    """Every allowed direction, marked with whether it can actually complete.

    Printed in the header of every run so that the terse refusal validate_pair()
    raises -- "Unsupported trading pair" -- lands next to the list that answers it.
    Rule 14: echo the parameters that decide the answer.

    IT READ ALLOWED_PAIRS ALONE, AND THE PAGE STOPPED DOING THAT HOURS EARLIER.
    Flagged by review 2026-09-26. ALLOWED_PAIRS is what this terminal is WILLING to
    swap; the adapters say what it can REACH, and whether the destination can PAY
    OUT. The swap page learned both the same day -- a pair failing either test is
    badged DISABLED there -- while this line went on listing all six as if they were
    equivalent. So an operator reading `pairs allowed BTC->GRC, GRC->XRP, ...` was
    being handed the same overclaim the page had just stopped making, from the tool
    written to replace the page.

    Through services/pair_view.allowed_pair_rows(), which is the function the page
    itself calls, so the two cannot disagree (rule 8). `adapters` is optional only so
    that a caller with nothing constructed still gets the allowed list rather than a
    crash; when it is omitted the line says the reachability half was not checked
    rather than implying it passed.
    """
    if adapters is None:
        pairs = sorted(f"{source}->{destination}" for source, destination in config["ALLOWED_PAIRS"])
        joined = ", ".join(pairs)
        return (
            f"{joined}  <- from ALLOWED_PAIRS only; whether each is REACHABLE was not checked here"
            if pairs
            else "(none) -- Config.ALLOWED_PAIRS is empty, so no swap of any kind can be priced"
        )
    rows = allowed_pair_rows(config, adapters)
    if not rows:
        return "(none) -- Config.ALLOWED_PAIRS is empty, so no swap of any kind can be priced"
    marked = ", ".join(
        f"{row['from_asset']}->{row['to_asset']}" + ("" if row["enabled"] else " (UNAVAILABLE)")
        for row in rows
    )
    unavailable = [row for row in rows if not row["enabled"]]
    if not unavailable:
        return marked
    return f"{marked}  <- UNAVAILABLE = allowed but not completable here. {blocked_by(unavailable)}"


def blocked_by(unavailable: list[dict]) -> str:
    """Why each unavailable pair is unavailable, GROUPED BY CAUSE. Never one for all.

    THIS FIXED A DEFECT I SHIPPED AN HOUR EARLIER. The first version of pair_catalog()
    printed `unavailable[0]["reason"]` as THE reason, and the operator's own run showed
    what that produces:

        GRC->XRP (UNAVAILABLE) ... UNAVAILABLE means allowed but not completable from
        this process: BTC has no adapter in this process: BTC_RPC_PORT ... unset

    GRC -> XRP is unavailable because XRP cannot PAY OUT -- on the day that was measured,
    for want of a signing key; since 2026-10-02 it is because XRP_PAYOUT_SECRET_SEED is
    unset, which is the same verdict reached through a variable rather than an absence --
    and has nothing to do with BTC_RPC_PORT. One row's reason presented as every row's
    is the same overclaim this whole day was spent removing, and I committed a fresh
    one into the line that removes it.

    THREE causes now, and they are three different actions for the operator: a chain
    with no adapter needs settings exported, a chain that cannot pay out needs a
    signing decision that is theirs (rule 16), and a chain that cannot take deposits
    needs one shared-account variable set. Naming them separately is the whole point;
    concatenating every full sentence instead would produce a paragraph nobody reads,
    so each cause names its CHAINS and the fix is one clause.

    THE THIRD ARRIVED 2026-10-01 AND THIS FUNCTION CALLED ITSELF DEFECTIVE, exactly as
    written: pair_view gained a deposit-source test, four pairs became unavailable for a
    reason this function did not know, and the fallback below printed "Cause NOT
    ESTABLISHED ... this is a defect in pair_catalog()". It was. That sentence existing
    is why the gap took one test run to find instead of reaching an operator as an
    unexplained UNAVAILABLE -- a wrong explanation is worse than an absent one, and an
    absent one that names itself as a bug is better than both.
    """
    no_adapter = sorted({asset for row in unavailable for asset in row.get("missing") or []})
    # The adapters' OWN sentences, DEDUPLICATED -- not a paraphrase of them. An earlier
    # draft of this function summarised the payout cause in its own words, which put a
    # second copy of the explanation here (rule 8) and broke the test asserting the
    # adapter's wording reaches the screen. chains/registry.why_cannot_pay_out() builds
    # these from each adapter's payout_refusal, so the adapter stays the one place that
    # says why it cannot pay. Deduplicated because two pairs can share one cause and
    # printing it twice reads as two problems.
    cannot_pay = sorted({row["cannot_pay"] for row in unavailable if row.get("cannot_pay")})
    # THE SAME TREATMENT FOR THE DEPOSIT SIDE: the sentence comes from
    # services/swap_service.why_cannot_take_deposits(), which derives the variable's name from
    # TAG_ATTRIBUTION, so that table stays the one place that knows it (rule 11). Deduplicated
    # for the same reason as above -- XRP->BTC, XRP->GRC and XRP->LTC share one cause, and
    # printing it three times reads as three problems.
    cannot_take = sorted({row["cannot_take"] for row in unavailable if row.get("cannot_take")})
    causes = []
    if no_adapter:
        # The FACT, and the remedy is not restated here: create_swap()'s refusal names
        # the exact variables, the `adapters here` line one row above lists what this
        # process built, and swap_readiness.py asks the daemons. Repeating
        # why_unconfigured()'s full sentence for each of up to three chains would make
        # this line unreadable and would be a third copy of it.
        causes.append(
            f"{', '.join(no_adapter)} ha{'s' if len(no_adapter) == 1 else 've'} no adapter in this "
            f"process"
        )
    causes.extend(cannot_pay)
    causes.extend(cannot_take)
    if not causes:
        # Neither cause recognised. Say that rather than inventing one: a pair marked
        # UNAVAILABLE with no explanation is a bug report, and a wrong explanation is
        # worse than an absent one.
        return "Cause NOT ESTABLISHED -- run swap_readiness.py; this is a defect in pair_catalog()"
    return "Blocked by: " + "; ".join(causes)


def check_pair(config: dict, from_asset: str, to_asset: str) -> None:
    """validate_pair(), with the allowed list attached to the refusal.

    The service's own gate, called early rather than copied. create_quote() calls
    it again a moment later; this one exists so the message carries the catalog
    and so no `quotes` row is written for a direction that cannot be swapped.
    """
    try:
        validate_pair(config, from_asset, to_asset)
    except ValueError as error:
        raise SwapRefused(
            f"{error}: {from_asset}->{to_asset} is not in Config.ALLOWED_PAIRS, so it is refused before a "
            f"quote is even priced. What is allowed: {pair_catalog(config)}. Nothing was written."
        ) from error


def check_chains_reachable(config: dict, adapters: dict, from_asset: str, to_asset: str) -> None:
    """Both chains must have an adapter IN THIS PROCESS. The services' own sentence.

    unconfigured_chains() and why_unconfigured() are the two functions
    create_swap() uses for this refusal, called here with the same arguments --
    including `config.get("RPC")`, without which why_unconfigured() can only name
    a chain's primary setting and on 2026-09-26 would have named the one variable
    the operator already had right.

    Checked before the quote for the reason the module header gives: a chain this
    process cannot reach leaves a priced quote and no swap, and the operator then
    has a rate for a swap that could never be created.
    """
    missing = unconfigured_chains(adapters, from_asset, to_asset)
    if missing:
        raise SwapRefused(
            " Also: ".join(why_unconfigured(asset, config.get("RPC")) for asset in missing)
            + f" The {from_asset}->{to_asset} pair may well be in ALLOWED_PAIRS -- that says what this "
            f"terminal is WILLING to swap, and the adapters say what it can REACH. Nothing was written."
        )


def check_payout_address(adapters: dict, to_asset: str, payout_address: str) -> tuple[str, str]:
    """Ask the destination chain about the payout address. (state, detail).

    THREE states, not two, and the third is the point:

        VALID       the daemon said yes
        INVALID     the daemon said no, OR the string does not decode here
        UNASKABLE   the daemon could not be asked at all

    Collapsing the third into the second is the defect chains/base.py's
    validate_address() docstring is about: it used to end `except Exception:
    return False`, so an outage and a malformed address produced the same answer,
    and the operator's response to a down daemon was to go and check the
    customer's address. Returned rather than printed so a test can assert the
    state directly, and so main() decides what a state means.

    A LOCAL DECODE RUNS FIRST, added 2026-09-27, and it is strictly better than the
    daemon call it precedes rather than a duplicate of it (rule 8 asks which, and the
    answer is "they differ and the difference is the point"):

      - it needs NO DAEMON. A typo'd address is refused with a reason on a host with
        nothing running, where the round trip returns UNASKABLE and tells the operator
        nothing about the address they actually mistyped.
      - it CANNOT BE FOOLED BY A LOOSE ANSWER. chains/xrp.XRPAdapter.validate_address()
        accepts any X-address without verifying its checksum -- that adapter's own
        docstring carries the review finding -- and a stub, a future adapter, or a daemon
        answering `isvalid` from a cached table can all say yes to a string that decodes
        as nothing.
      - it knows which CHAIN. `ltc1...` handed in as a BTC payout is a real, well-formed
        mainnet address, and a Bitcoin daemon asked about it says no -- but so does it for
        a typo, and the two want different fixes.

    The daemon is still asked whenever the local check does not refuse, because it
    answers a question this cannot: whether THAT wallet, on THAT network, will accept it.
    Neither replaces the other.

    NO_VALIDATOR and UNDETERMINED both fall through to the daemon rather than refusing. See
    modules/address_authority.py's header for why an address we cannot place must not be an
    outage; here it costs nothing, because the daemon is about to be asked anyway -- and the
    daemon is the better authority in exactly that case, since it is the one holding the
    chain's real address tables.
    """
    local = check_address(to_asset, payout_address)
    if local.refuses:
        return "INVALID", (
            f"refused locally, before any daemon was asked -- {local.why}. Money sent to this string "
            f"would be unspendable by anybody, so no swap is worth creating against it"
        )
    # Rule 14: an UNCHECKED address must not report like a checked one, even when the daemon
    # is about to answer. Carried in the RETURNED detail rather than printed here, because
    # this function's contract (see above) is that it returns and main() prints -- a print
    # inside it would be the one thing a test of it could not assert on.
    unchecked = f"NOT CHECKED LOCALLY ({local.state}: {local.why}); " if local.unchecked else ""
    try:
        answered = adapters[to_asset].validate_address(payout_address)
    except ADAPTER_ERRORS as error:
        return "UNASKABLE", unchecked + f"{type(error).__name__}: {error}"
    # NOT EVERY ADAPTER ASKS A DAEMON, and saying it did was a false claim about
    # where the answer came from. chains/xrp.py validates LOCALLY against the
    # ledger's own checksum constants and opens no socket -- deliberately, since
    # asking the server would answer "does this account exist", and an unfunded but
    # valid address is exactly what a first payment creates. Flagged by review
    # 2026-09-26: the line read `VALID <- the XRP daemon accepts it` when no daemon
    # was asked.
    how = "checked locally against the ledger's checksum" if to_asset == "XRP" else f"the {to_asset} daemon"
    if not answered:
        return "INVALID", unchecked + (how if to_asset == "XRP" else f"{how} rejects it")
    detail = how if to_asset == "XRP" else f"{how} accepts it"
    return "VALID", unchecked + detail + payout_destination_note(adapters[to_asset], payout_address, to_asset)


def payout_destination_note(adapter, payout_address: str, to_asset: str) -> str:
    """Whether the payout lands back in this terminal's own wallet. "" when not.

    THE LINE THAT WOULD HAVE SAVED AN HOUR, 2026-09-26. The operator's first
    end-to-end XRP -> GRC swap paid 55.52645238 GRC to an address in their own
    Gridcoin wallet, so the wallet reported a send AND a matching receive and the net
    movement was the 0.001 GRC fee. Everything worked. What they said was "there's
    still no goddamn grc from xrp testnets", because a payout into the wallet it came
    out of looks exactly like nothing happening.

    The screen had said `address check VALID <- the GRC daemon accepts it`, which is
    true and answers a different question. validateaddress says WELL-FORMED, never
    YOURS -- and for a customer's swap those are the right semantics, since a payout
    address should NOT be the terminal's. So this is not a refusal. It is the fact
    that decides how to read the result, printed beside the address rather than left
    to be worked out from a wallet afterwards.

    Silent for None as well as False: chains/base.owns_address() returns None when
    the daemon answers the validity question and not the ownership one -- `ismine` is
    a wallet field and Bitcoin Core moved it to getaddressinfo in 0.18 -- and saying
    "not yours" for "not established" would be inventing the reassuring answer.
    """
    owns = adapter.owns_address(payout_address) if hasattr(adapter, "owns_address") else None
    if not owns:
        return ""
    return (
        f"; and it is THIS WALLET'S OWN address (ismine), so the {to_asset} payout returns to the wallet "
        f"it is paid from -- the net movement will be the transaction fee only. Correct for a self-test, "
        f"and NOT what a real customer's payout address should be"
    )


def deposit_preview(config: dict, adapters: dict, from_asset: str) -> list[str]:
    """Where the deposit will go, WITHOUT deriving or allocating anything.

    Two shapes, and the difference decides whether this may be called at all
    before --apply:

      tag-attributed (XRP)   deposit_account() is a pure read -- a config lookup
                             and one validate_address() call -- so it is called
                             here, in both modes. It is also the function that
                             refuses when XRP_DEPOSIT_ACCOUNT is unset or invalid,
                             which is a refusal worth having in the dry run: a
                             swap created against an unset account would hand a
                             customer a deposit instruction this terminal cannot
                             receive against.
      address-attributed     deposit_account() would call get_new_address(), which
      (BTC, LTC, GRC)        DERIVES A KEY IN THE HOT WALLET. That is a wallet
                             write, so a dry run must not reach it. This says what
                             --apply will do instead of doing it.

    The empty swap_id passed below is unused on the tag path -- swap_service.py's
    tag branch never references it, and only the address branch interpolates it
    into the getnewaddress label -- and the branch here is what guarantees the
    address path is never entered from this function. tests/test_open_swap.py
    pins that with an adapter whose get_new_address() raises.
    """
    if from_asset not in TAG_ATTRIBUTED_ASSETS:
        return [
            labeled("deposit target", f"a fresh {from_asset} address, DERIVED in the hot wallet by "
                                      f"getnewaddress when --apply runs"),
            CONTINUATION + "<- not derived now: deriving a key is a wallet write, and a dry run writes nothing",
        ]
    try:
        account, needs_tag = deposit_account(config, adapters, from_asset, "")
    except ValueError as error:
        raise SwapRefused(f"{error}") from error
    except ADAPTER_ERRORS as error:
        raise SwapRefused(
            f"the {from_asset} daemon could not be asked whether XRP_DEPOSIT_ACCOUNT is a valid account "
            f"({type(error).__name__}: {error}), so whether a deposit could be received was NOT established. "
            f"Nothing was written."
        ) from error
    lines = [labeled("deposit target", f"{account}  <- every {from_asset} swap shares this one account "
                                       f"(XRP_DEPOSIT_ACCOUNT)")]
    if needs_tag:
        lines.append(
            CONTINUATION + "a destination tag is allocated when --apply runs, and it is MANDATORY: the "
            "account alone is not an instruction, because every swap has the same one"
        )
    return lines


def swaps_awaiting_deposit(db, from_asset: str) -> list[dict]:
    """The swaps of this asset already waiting for a deposit, newest first.

    RULE 8, AND THE SECOND SITE IS NAMED BECAUSE THE TWO GENUINELY DIFFER.
    xrp_send_tagged.pending_xrp_swap() runs the same SELECT and then REFUSES on
    anything but exactly one row, because it is about to send a payment to one
    (account, tag) and a tag has no checksum behind it. This one never refuses,
    because it is about to CREATE another and knows its id -- so the ambiguity
    that refusal exists for cannot arise from following the command this file
    prints. Same query, opposite decisions, and each site points at the other.

    The merge both callers want is one reader in services/swap_service.py, which
    owns the `swaps` table. It is not done here: that file was being edited by
    another session while this was written (measured -- `git status` showed it
    modified at 17:01 while this file was being read), and landing a second edit
    into a file somebody else holds is how a merge loses work. Named as owed work
    rather than left for someone to find.
    """
    return db.execute(
        "SELECT id, deposit_address, deposit_tag, expected_input_amount, created_at FROM swaps "
        "WHERE from_asset = ? AND status = 'awaiting_deposit' ORDER BY created_at DESC",
        (from_asset,),
    ).fetchall()


def open_swap_warning(rows: list[dict], from_asset: str) -> list[str]:
    """What to say about swaps already awaiting a deposit. Never returns [].

    Rule 14: "(none)" is a result, and a blank gap is ambiguous between zero rows
    and a query that broke. So there is always exactly one line to print, and the
    no-rows case says what it means rather than saying nothing.
    """
    if not rows:
        return [
            labeled("already open", f"(none) -- no {from_asset} swap is awaiting a deposit, so "
                                    f"`--swap latest` will resolve to the one this run creates")
        ]
    # NO `<id>` IN THIS SENTENCE, and that is not fussiness. A placeholder in
    # anything printed here is a thing somebody pastes -- three mis-runs and two
    # exposed secrets in this project came from exactly that -- so the warning
    # points at the ready-made command below instead of spelling a flag with a
    # blank in it.
    lines = [
        labeled("already open", f"{len(rows)} {from_asset} swap(s) are ALREADY awaiting a deposit. Creating "
                                f"another is allowed and is not a mistake, but `xrp_send_tagged.py --swap "
                                f"latest` will refuse from now on (it refuses on ambiguity rather than "
                                f"guessing newest-wins), so use the ready-made command this run prints at "
                                f"the end, which names the new swap id:")
    ]
    lines.extend(
        CONTINUATION + f"{row['id']}  tag {row['deposit_tag']}  expects {row['expected_input_amount']} "
        f"{from_asset}  created {row['created_at']}"
        for row in rows
    )
    return lines


def read_open_swaps(db_path: str, from_asset: str) -> list[str]:
    """open_swap_warning() over a database that may not exist yet. Opens no writer.

    Path().exists() BEFORE connecting, on purpose: sqlite3.connect() CREATES the
    file, and a dry run that leaves an empty database behind has written
    something while announcing that it would not. Same reason the schema is not
    applied until --apply.
    """
    if not Path(db_path).exists():
        return [
            labeled("already open", f"(none) -- there is no database at {db_path} yet, so no swap of any "
                                    f"kind is open. --apply creates the file and the schema.")
        ]
    connection = connect_db(db_path)
    try:
        return open_swap_warning(swaps_awaiting_deposit(connection, from_asset), from_asset)
    except sqlite3.OperationalError as error:
        # Named, not broad: this is what an existing file with no `swaps` table
        # raises ("no such table: swaps"), which is an ordinary state for a
        # database that was created but never initialized -- and it must not read
        # as "no swaps are open", which is the same answer a healthy empty table
        # gives.
        return [
            labeled("already open", f"NOT ESTABLISHED -- {db_path} could not be queried ({error}). --apply "
                                    f"applies the schema; until then this run cannot tell an empty table "
                                    f"from a missing one.")
        ]
    finally:
        connection.close()


def fetch_prices_or_refuse(config: dict) -> dict:
    """Fetch the USD prices create_quote() will price against. Legible on failure.

    TWO THINGS, and the second is why this is not simply left to create_quote().

    Legibility. fetch_usd_prices() raises rather than returning a stale or zero
    price, deliberately (services/pricing.py's header says why), so the failure
    reaches the operator. Uncaught it arrives as a traceback whose top frame is
    inside a price cache. Caught here it is one sentence saying the feed did not
    answer and nothing was written. The four families are named rather than
    caught broadly, measured against pricing.py line by line with requests 2.33.1:

        RequestException   the transport and the HTTP status -- ConnectionError,
                           Timeout and HTTPError from raise_for_status() all
                           subclass it, and so does requests' own JSONDecodeError
                           for a body that is not JSON.
        KeyError           pricing.py's own explicit raise for a response missing
                           an asset ("a swap priced off a missing leg is a swap
                           priced wrong"), and a `usd` key absent from one entry.
        TypeError          float(None), from an entry whose price is JSON null.
        ValueError         float("") or float("n/a"), from a price that is not a
                           number.

    Warming the cache. The fetch is process-wide and TTL'd (RATE_CACHE_SECONDS),
    so create_quote()'s own call a moment later reads this result instead of
    making a second HTTP request. With RATE_CACHE_SECONDS=0 it would refetch --
    `now < expires_at` is false when the TTL is zero -- which costs one extra
    request and is still legible, because create_quote() is wrapped for
    RequestException at its call site too.
    """
    ttl = config["RATE_CACHE_SECONDS"]
    print(
        labeled("prices", f"fetching USD prices from CoinGecko (services/pricing.py), cached for "
                          f"{format_duration(ttl)}  <- RATE_CACHE_SECONDS"),
        flush=True,
    )
    started = time.monotonic()
    try:
        prices = fetch_usd_prices(ttl)
    except (RequestException, KeyError, TypeError, ValueError) as error:
        raise SwapRefused(
            f"no USD price could be fetched, so no rate could be derived and NOTHING was written "
            f"({type(error).__name__}: {error}). services/pricing.py raises rather than returning a stale "
            f"or zero price on purpose -- a zero would pay out zero. This is the one outbound network call "
            f"on this path; retry when the feed answers."
        ) from error
    print(
        CONTINUATION + f"got {len(prices) - 1} prices in {format_duration(time.monotonic() - started)}  "
        f"<- the count excludes the fetched_at stamp; create_quote() derives the rate from these, not this file",
        flush=True,
    )
    return prices


def report_lines(swap: dict, quote: dict, db_path: str, config: dict, explicit_db: str = "") -> list[str]:
    """The block the operator reads and pastes back. Pure, over the rows as written.

    Every number comes from the `swap` or `quote` dict the services returned, so
    this cannot disagree with the database: there is no arithmetic here except
    turning AMOUNT_TOLERANCE_PCT into a percentage for display, which
    migrate_deposit_vouts.py:646 and templates/index.html both already do at
    their own print sites.

    Each line says what the number MEANS next to the number (rule 14), because
    the operator reads the screen and not the source -- and a pasted block is
    usually read a day later, by which time "1.0" with no unit and no label is
    two questions rather than an answer.
    """
    from_asset, to_asset = swap["from_asset"], swap["to_asset"]
    tolerance = float(config["AMOUNT_TOLERANCE_PCT"])
    tag = swap["deposit_tag"]
    tag_line = (
        labeled("destination tag", f"{tag}  <- MANDATORY on {from_asset}: the account above is shared by "
                                   f"every swap, so an untagged payment to it is credited to nobody")
        if tag is not None
        else labeled("destination tag", f"(none) -- {from_asset} deposits are attributed by ADDRESS, so the "
                                        f"address above identifies this swap on its own")
    )
    return [
        "swap opened. One quote row, one swap row, one audit row -- written by the same service functions the",
        "web form calls. Nothing has been signed and nothing has been broadcast.",
        "",
        # IT USED TO CLAIM "the SAME file the workers read (SWAP_DB_PATH)" and both
        # halves were unfounded. The provenance was asserted whether or not
        # SWAP_DB_PATH was set -- measured 2026-10-01 in a shell where it was not,
        # via the identical defect copied into show_fees.py -- and the sameness is a
        # claim about ANOTHER PROCESS's environment, which this one cannot see: the
        # workers inherit the shell that started them, which may not be this one.
        # Rule 17's line between a reason to believe something and having checked it.
        labeled("database", f"{db_path}  <- {db_path_source(db_path, explicit_db)}. A swap in any other "
                            f"database is invisible to the workers"),
        labeled("swap id", f"{swap['id']}  <- names this swap to every command below, and to /swap/{swap['id']}"),
        labeled("quote id", f"{quote['id']}  <- the rate this swap was created against"),
        labeled("pair", f"{from_asset} -> {to_asset}"),
        labeled("deposit account", f"{swap['deposit_address']}  <- send the deposit HERE"),
        tag_line,
        labeled("expected input", f"{swap['expected_input_amount']} {from_asset}  <- send EXACTLY this. "
                                  f"AMOUNT_TOLERANCE_PCT={tolerance} accepts {tolerance * 100:.2f}% either "
                                  f"side; outside it the swap is held for a person instead of paid out"),
        labeled("quoted rate", f"{swap['quoted_rate']} {to_asset} per {from_asset}  <- fixed now, not "
                               f"re-priced at payout time"),
        labeled("fee", f"{swap['fee_bps']} bps  <- taken off the gross output by create_quote()"),
        # as_amount(), NOT the raw float. The reserve became MEASURED on 2026-10-03
        # and measured figures are small: this line printed "2.82e-05 BTC reserved"
        # on the operator's screen, which is the fourth place this session that a
        # small number reached a human in exponent notation (the others:
        # swap_readiness's rate line, show_payout_fees's live fee, and
        # payout_capacity's ceiling). A constant of 0.001 never exposed it.
        labeled("network fee", f"{as_amount(swap['network_fee_reserve'])} {to_asset} reserved  <- held back for the "
                               f"payout transaction's own chain fee"),
        # "payout (est.)" rather than "estimated payout": the label column is 16
        # wide and the longer spelling filled it exactly, printing
        # `estimated payout49.24`. The estimate is still named as an estimate,
        # which is the part that must not be lost -- payout_worker broadcasts what
        # the row holds, and the row holds an estimate until the deposit lands.
        labeled("payout (est.)", f"{swap['output_amount_estimate']} {to_asset}  <- what payout_worker "
                                 f"broadcasts, after the fee and the reserve above"),
        labeled("payout address", f"{swap['payout_address']}  <- FINAL. A payout is final the moment it is "
                                  f"broadcast"),
        labeled("confirmations", f"{swap['min_confirmations']}  <- min_confirmations, a COUNT of "
                                 f"confirmations, never a duration (rule 6). The deposit watcher releases "
                                 f"the payout at or above it"),
        labeled("status", f"{swap['status']}"),
        labeled("rate window", f"{swap['expires_at']}  <- the QUOTE window, "
                               f"{format_duration(config['QUOTE_TTL_SECONDS'])} wide. Nothing in this tree "
                               f"expires a SWAP (measured in services/swap_view.quote_window()), so a "
                               f"deposit after it still credits"),
    ]


def next_command(swap_id: str, from_asset: str, db_path: str = "") -> list[str]:
    """The exact next command, with the real id in it. No placeholder, ever.

    A placeholder in a pasted command has cost this project three mis-runs and
    twice put something in a shell that should never have been there -- once a
    wallet passphrase. The entire reason this file exists is that a swap id had
    to travel from a browser into a terminal by hand, so printing `<swap id>`
    here would reintroduce exactly the defect it was written to remove. Hence a
    function with its own test: a literal `<` in what this returns is a failure.

    An asset with no scripted sender gets an instruction rather than an invented
    command -- see DEPOSIT_SENDERS for how that list was established.

    `db_path` IS PART OF "no placeholder", not an extra. Found by three reviewers
    independently on 2026-09-26: this printed `xrp_send_tagged.py --swap <real id>`
    with no --db, so after `--apply --db <somewhere>` the sender looked for that swap
    in the DEFAULT database and did not find it. The id was real and the command
    still did not run as printed, which is the same failure as a placeholder wearing
    a different hat. Empty when --db was not passed, so the common command stays
    short.
    """
    suffix = f" --db {shlex.quote(db_path)}" if db_path else ""
    template = DEPOSIT_SENDERS.get(from_asset)
    if template:
        return [
            "Next, paste this. The swap id is already in it:",
            f"    {template.format(swap_id=swap_id)}{suffix}",
            "    add --send to actually submit; without it that command describes what it would send.",
        ]
    return [
        f"There is no scripted sender for a {from_asset} deposit in this tree, so the deposit is sent from "
        f"your own {from_asset} wallet to the account above. Then watch it credit:",
        "    python3 -m swap_terminal.workers.deposit_watcher   # or leave the running worker to it",
    ]


def apply_swap(args, config: dict, adapters: dict, pair: tuple[str, str], db_path: str) -> dict:
    """Price a quote and create the swap. Returns (swap, quote) merged for reporting.

    The whole write, and every row in it is written by a service. The two calls
    are inside ONE db_session so a refusal from create_swap() rolls back -- except
    that create_quote() commits its own row before returning, which db_session's
    rollback cannot undo.

    THAT USED TO SAY "Named, not fixed", and it is fixed now. Review 2026-09-26
    pointed at what the note was tolerating: three refusal messages told the operator
    "Nothing was committed" while an orphaned quotes row sat in the database. The row
    is inert -- nothing reads `quotes` except get_quote_or_raise() by id, and it
    expires -- so nothing broke. A message that is wrong about what is in the database
    is still the failure this session kept paying for, twice in the payout path alone.
    The quote is deleted in a `finally` when the swap is not created, rather than
    reimplementing create_quote() without its commit.
    """
    from_asset, to_asset = pair
    print("\n" + labeled("writing to", db_path), flush=True)
    fetch_prices_or_refuse(config)
    started = time.monotonic()
    try:
        with db_session(db_path) as db:
            # The same bootstrap init_db() and every worker's cycle run: the
            # schema is idempotent, and apply_migrations() is what adds
            # swaps.deposit_tag to a database created before 2026-09-26. Without
            # it, create_swap()'s INSERT names a column that database does not
            # have and the failure arrives as "no such column" from inside a
            # service rather than as a migration here.
            db.executescript(SCHEMA)
            migrated = apply_migrations(db)
            print(
                labeled("schema", f"ensured; deposit_tag column added now: {migrated['deposit_tag_added']}  "
                                  f"<- False means it was already there, which is the normal case"),
                flush=True,
            )
            # `adapters=` so the CLI path gets the same quote-time gate the web
            # form does. services/quote_service.require_deliverable_sol_payout()
            # needs a chain to ask for the smallest deliverable SOL payout, and
            # a gate that only one of two entry points applies is not a gate
            # (rule 19). The dict is the one this file already built above.
            quote = create_quote(db, config, from_asset, to_asset, args.amount, adapters=adapters)
            print(
                labeled("quote", f"{quote['id']}  rate {quote['quoted_rate']} {to_asset} per {from_asset}, "
                                 f"estimated payout {quote['output_amount_estimate']} {to_asset}"),
                flush=True,
            )
            # THE ORPHAN QUOTE, AND IT WAS NAMED RATHER THAN FIXED.
            #
            # create_quote() commits its own row before returning, so every refusal
            # from create_swap() used to leave that row behind -- and db_session's
            # rollback cannot reach it, because the commit already happened. This
            # function's docstring said "Named, not fixed", and review 2026-09-26
            # pointed out the consequence: three refusal messages then told the
            # operator "Nothing was committed" while a quotes row sat there.
            #
            # A `finally` rather than an except clause, so it covers KeyboardInterrupt
            # and SystemExit too -- Ctrl-C between the quote and the swap is exactly
            # when this happens -- and so nothing needs to catch and re-raise.
            #
            # The row is INERT (nothing reads `quotes` except get_quote_or_raise() by
            # id, and it expires), which is why this was survivable. It is still a
            # write the operator was told did not happen, and a message that is wrong
            # about what is in the database is the kind of wrong this whole session
            # has been paying for.
            swap_created = False
            try:
                swap = create_swap(db, config, adapters, quote["id"], args.payout_address)
                swap_created = True
            finally:
                if not swap_created:
                    db.execute("DELETE FROM quotes WHERE id = ?", (quote["id"],))
                    db.commit()
    except RequestException as error:
        # BEFORE the ValueError clause, and the order is load-bearing: requests'
        # JSONDecodeError subclasses BOTH RequestException and ValueError
        # (verified against requests 2.33.1), so a ValueError clause first would
        # report a mangled price response as a service refusal.
        raise SwapRefused(
            f"the price feed failed while pricing the quote ({type(error).__name__}: {error}). Nothing was "
            f"written: the quote row is deleted when the swap is not created."
        ) from error
    except (ValueError, XRPTagAllocationError) as refusal:
        # The services' own refusals, surfaced verbatim. XRPTagAllocationError is
        # not a ValueError (it derives from Exception) and would otherwise arrive
        # as a traceback; it means no tag was allocated, so there is nothing to
        # display and the swap does not exist.
        raise SwapRefused(f"{refusal} (rolled back: no swap row was committed)") from refusal
    except ADAPTER_ERRORS as error:
        raise SwapRefused(
            f"a chain daemon could not be asked ({type(error).__name__}: {error}), so whether this swap is "
            f"valid was NOT established and no swap row was committed. This is an outage, not a bad address."
        ) from error
    except sqlite3.Error as error:
        raise SwapRefused(
            f"SQLite refused the write ({type(error).__name__}: {error}) against {db_path}. If that says "
            f"'database is locked', a worker holds the one write lock -- retry, or stop the workers first. "
            f"Nothing was committed."
        ) from error
    print(labeled("wrote", f"in {format_duration(time.monotonic() - started)}"), flush=True)
    return {"swap": swap, "quote": quote}


def apply_command(args, from_asset: str, to_asset: str) -> str:
    """The --apply command for THIS dry run, with the real values already in it.

    Echoes what was passed rather than a template, for the same reason
    next_command() does: the payout address is the one value a person states, and
    asking them to retype it into a second command is asking for the transcription
    error the dry run was supposed to catch. A payout address is public -- it is
    printed on the swap page -- so echoing it leaks nothing.

    --db IS CARRIED, AND LEAVING IT OUT WAS THE WHOLE BUG. Three reviewers found it
    independently on 2026-09-26: this claimed "every value is already in it" and
    dropped the one flag that decides WHICH DATABASE gets written. So
    `--db /tmp/scratch` would dry-run against the scratch file, print a command
    without it, and that command would create the swap -- and its permanent,
    immutable destination tag -- in the default database instead. A tool whose
    reason for existing is that nothing gets transcribed by hand cannot print a
    command that silently changes its target.

    Omitted when --db was not passed, so the ordinary command stays short. shlex.quote
    on the path because a database path may contain a space.
    """
    parts = [
        "python3 open_swap.py",
        f"--pair {from_asset}:{to_asset}",
        f"--amount {args.amount}",
        f"--payout-address {shlex.quote(args.payout_address)}",
    ]
    if args.db:
        parts.append(f"--db {shlex.quote(args.db)}")
    parts.append("--apply")
    return " ".join(parts)


def payout_wallet_line(config, adapters, to_asset: str, amount: float) -> str:
    """One preview line: the biggest payout the destination wallet could fund.

    WHY THE CEILING AND NOT A COMPARISON. The dry run deliberately computes no
    payout figure -- the rate is fixed by create_quote() at --apply time, and a
    second figure here would be a second copy of the fee arithmetic that decides
    it (the DRY RUN footer says so). So the amount-free form of the question is
    the one a preview can honestly ask, which is why
    services/payout_capacity.largest_fundable_payout() exists beside the gate.

    MEASURED 2026-10-03, AND THIS LINE IS THE PART I OWED WITHOUT BEING ASKED.
    The preview printed `payout (est.) 9049.685834122582 GRC` and said nothing
    about the wallet holding 3780.08854497 -- both numbers on one screen, one RPC
    call apart, never compared. The deposit was taken, confirmed, and the payout
    died on "Insufficient funds". Rule 14: state what the number means, next to
    the number.

    `amount` IS THE INPUT, NOT A PAYOUT, and is used only to say whether the
    ceiling is comfortable at the rate the header already printed. It is not
    multiplied by anything here.
    """
    reserve = get_network_fee_reserve(config, to_asset)
    ceiling, how = largest_fundable_payout(adapters, to_asset, reserve)
    if ceiling < 0:
        # NOT ESTABLISHED is not zero, and must not render as one (rule 13). A
        # printed 0.0 would send an operator to fund a wallet that may be full.
        #
        # AND IT MUST NOT CLAIM A REFUSAL THAT WILL NOT HAPPEN. This line read
        # "--apply will refuse rather than take a deposit against a wallet it could
        # not verify" unconditionally, which was TRUE when it was written on
        # 2026-10-03 and was made FALSE the same afternoon by the source_account
        # change a few commits later: services/payout_capacity.
        # why_the_payout_cannot_be_funded() now returns `unchecked` rather than
        # refusing for a chain whose payouts are debited from a NAMED account, so
        # --apply proceeds.
        #
        # Measured on the operator's screen minutes before they would have run it: a
        # GRC -> XRP preview told them --apply would refuse, and --apply would have
        # created the swap. A line that promises a gate the code does not have is
        # worse than no line, because it is read as protection.
        #
        # SO THE TWO CASES ARE SPLIT, and the discriminator is the same
        # payout_source_account() the gate itself uses -- not a second opinion about
        # which chains are which (rule 8).
        if payout_source_account(config, to_asset):
            return labeled("payout wallet", f"NOT CHECKED  <- {how}. {to_asset} payouts are debited "
                                            f"from a NAMED account this ceiling cannot read, so --apply "
                                            f"does NOT refuse on it: the swap is created and the "
                                            f"account's balance is first tested by the payout itself")
        return labeled("payout wallet", f"NOT ESTABLISHED  <- {how}. --apply will refuse rather than "
                                        f"take a deposit against a wallet it could not verify")
    return labeled("payout wallet", f"can fund a payout up to {as_amount(ceiling)} {to_asset}  <- {how}. --apply "
                                    f"REFUSES if this swap's payout exceeds it, before any row is "
                                    f"written: a payout that fails arrives after the deposit is "
                                    f"confirmed and irreversible (measured 2026-10-03)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open one swap from the shell. Dry run by default; --apply writes the rows.",
        epilog=(
            "--payout-address has no default and never will: it is where payout_worker broadcasts, it cannot "
            "be changed after the swap exists, and a payout is final the moment it is broadcast. It is the "
            "one value a person has to state."
        ),
    )
    parser.add_argument(
        "--pair", required=True, metavar="FROM:TO",
        help="the direction, e.g. XRP:GRC. Must be in Config.ALLOWED_PAIRS; the header prints the list.",
    )
    parser.add_argument(
        "--amount", required=True, type=swappable_amount,
        help="how much of the SOURCE asset you will deposit. This becomes swaps.expected_input_amount, and "
             "the deposit has to match it within AMOUNT_TOLERANCE_PCT or the swap is held for a person.",
    )
    parser.add_argument(
        "--payout-address", required=True, metavar="ADDRESS",
        help="where the payout is sent, on the DESTINATION chain. Validated by that chain's own daemon "
             "before anything is written.",
    )
    parser.add_argument(
        "--db", default="",
        help=f"database to write to (default: Config.DB_PATH, currently {Config.DB_PATH}). It must be the "
             f"same file the workers read, or nothing will ever see the swap.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write the quote and the swap. Without it, nothing is written -- not the rows, not the "
             "schema, not the database file -- and the checks below still run.",
    )
    return parser


def run(args) -> int:
    """Orchestration only: every decision above, in order, then print (rule 10)."""
    started = time.monotonic()
    db_path = args.db or Config.DB_PATH
    from_asset, to_asset = parse_pair(args.pair)
    config = get_config_dict()
    adapters = build_adapters_from_config()

    # ANNOUNCE BEFORE, NOT ONLY AFTER (rule 14). The two adapter calls below can
    # each wait on a daemon, and a person watching a blinking cursor cannot tell
    # working from hung -- which on this repo's own measurement resolves as Ctrl-C.
    print("open swap -- calls create_quote() then create_swap(), the same two functions the web form calls.", flush=True)
    mode = "APPLY -- rows WILL be written" if args.apply else "DRY RUN -- nothing is written, not even the schema"
    print(labeled("mode", mode), flush=True)
    print(labeled("database", db_path), flush=True)
    print(labeled("pair", f"{from_asset} -> {to_asset}"), flush=True)
    print(labeled("amount", f"{args.amount} {from_asset}  <- what you will deposit"), flush=True)
    print(labeled("payout address", f"{args.payout_address}  <- where {to_asset} is sent; FINAL once the "
                                    f"swap exists"), flush=True)
    print(
        labeled("pairs allowed", f"{pair_catalog(config, adapters)}  <- ALLOWED_PAIRS, and whether this "
                                 f"process can reach and pay out each"),
        flush=True,
    )
    print(labeled("adapters here", ", ".join(sorted(adapters))
                  or "(none) -- no chain is configured in this process"), flush=True)

    check_pair(config, from_asset, to_asset)
    check_chains_reachable(config, adapters, from_asset, to_asset)

    state, detail = check_payout_address(adapters, to_asset, args.payout_address)
    print(labeled("address check", f"{state}  <- {detail}"), flush=True)
    if state == "INVALID":
        raise SwapRefused(
            f"{args.payout_address} is not a valid {to_asset} address ({detail}), so no swap was created. "
            f"The payout address cannot be changed after the swap exists, which is why this is checked "
            f"before anything is written."
        )
    if state == "UNASKABLE":
        raise SwapRefused(
            f"the {to_asset} daemon could not be asked whether {args.payout_address} is valid ({detail}), so "
            f"whether the payout could ever be delivered was NOT established -- which is a different fact "
            f"from the address being bad. Nothing was written."
        )

    print(payout_wallet_line(config, adapters, to_asset, args.amount), flush=True)
    for line in deposit_preview(config, adapters, from_asset):
        print(line, flush=True)
    for line in read_open_swaps(db_path, from_asset):
        print(line, flush=True)

    if not args.apply:
        print(f"\nDRY RUN: nothing was written in {format_duration(time.monotonic() - started)}. No quote, no "
              f"swap, no destination tag, no derived address, no database file.", flush=True)
        print("  No rate is shown either, and that is deliberate: the rate is fixed by create_quote() at "
              "--apply time, and a second figure computed here would be a second copy of the fee arithmetic "
              "that decides the payout.", flush=True)
        print("\nTo open it, paste this. Every value is already in it:", flush=True)
        print(f"    {apply_command(args, from_asset, to_asset)}", flush=True)
        return 0

    written = apply_swap(args, config, adapters, (from_asset, to_asset), db_path)
    print(flush=True)
    for line in report_lines(written["swap"], written["quote"], db_path, config, args.db):
        print(line, flush=True)
    print("\n" + labeled("opened in", format_duration(time.monotonic() - started)), flush=True)
    print(flush=True)
    for line in next_command(written["swap"]["id"], from_asset, args.db):
        print(line, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except SwapRefused as refusal:
        # THE ONE PLACE A REFUSAL IS PRINTED, so every refusal in this file reads
        # the same and none of them arrives as a traceback. Same shape as
        # migrate_deposit_vouts.main(): the operator needs the sentence, and a
        # stack buries it under frames they cannot act on.
        print(f"\nREFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
