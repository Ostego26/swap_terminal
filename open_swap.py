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
       money, and XRP_DEPOSIT_ACCOUNT is whatever the operator set. It prints
       every configured chain's target before it writes anything, through the
       same network_target.classify() the workers' banner uses, so "which
       chain" is on screen before the decision rather than after it.

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

1. DRY RUN BY DEFAULT. Every root script in this tree that writes anything is
   dry-run by default, measured 2026-09-26 by reading all seven:

       xrp_send_tagged.py        --send    submits a testnet Payment
       xrp_payout_verify.py      --send    submits a testnet Payment
       migrate_deposit_vouts.py  --apply   deletes deposit_events rows
       install_desktop_icon.py   --apply   writes files
       swap_readiness.py         (none)    read-only, so no flag
       xrp_chain_check.py        (none)    read-only
       solana_chain_check.py     (none)    read-only

   Four writers, four opt-in flags, zero exceptions. `--apply` rather than
   `--send` because `--send` in this tree means "broadcast to a chain" and this
   broadcasts nothing; `--apply` is what the two database/filesystem writers
   use. A swap row is not a fund movement, but it allocates a destination tag
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
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.base import RPCError
from chains.monero import MoneroRPCError
from chains.registry import unconfigured_chains, why_unconfigured
from chains.solana import SolanaRPCError
from chains.xrp import XRPRPCError
from config import Config
from db import SCHEMA, apply_migrations, connect_db, db_session
from microfortnights import format_duration
from requests.exceptions import RequestException
from services.pricing import fetch_usd_prices
from services.quote_service import create_quote, validate_pair
from services.swap_service import TAG_ATTRIBUTED_ASSETS, create_swap, deposit_account
from services.xrp_tag_service import XRPTagAllocationError
from workers.common import build_adapters_from_config, get_config_dict


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
# xrp.XRPRPCError, solana.SolanaRPCError, monero.MoneroRPCError. All four are
# listed even though ALLOWED_PAIRS mentions only BTC, LTC, GRC and XRP today,
# because listing only the reachable ones is rule 8's drift with a delay on it:
# adding a SOL or XMR pair would then turn a down daemon back into a traceback,
# and nothing would point at this line.
#
# They are NOT collapsed with SwapRefused. An RPC failure means "the daemon could
# not be asked", which is a different fact from "the answer is no" -- the
# distinction chains/base.validate_address() exists to preserve, and the one whose
# loss sent an operator to check a customer's address during an outage.
ADAPTER_ERRORS = (RPCError, XRPRPCError, SolanaRPCError, MoneroRPCError)

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


def pair_catalog(config: dict) -> str:
    """Every allowed direction, as one sorted line. Never empty-prints.

    Printed in the header of every run so that the terse refusal validate_pair()
    raises -- "Unsupported trading pair" -- lands next to the list that answers
    it. Rule 14: echo the parameters that decide the answer.
    """
    pairs = sorted(f"{source}->{destination}" for source, destination in config["ALLOWED_PAIRS"])
    return ", ".join(pairs) if pairs else "(none) -- Config.ALLOWED_PAIRS is empty, so no swap of any kind can be priced"


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
        INVALID     the daemon said no
        UNASKABLE   the daemon could not be asked at all

    Collapsing the third into the second is the defect chains/base.py's
    validate_address() docstring is about: it used to end `except Exception:
    return False`, so an outage and a malformed address produced the same answer,
    and the operator's response to a down daemon was to go and check the
    customer's address. Returned rather than printed so a test can assert the
    state directly, and so main() decides what a state means.
    """
    try:
        answered = adapters[to_asset].validate_address(payout_address)
    except ADAPTER_ERRORS as error:
        return "UNASKABLE", f"{type(error).__name__}: {error}"
    if answered:
        return "VALID", f"the {to_asset} daemon accepts it"
    return "INVALID", f"the {to_asset} daemon rejects it"


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
            f"  deposit target  a fresh {from_asset} address, DERIVED in the hot wallet by getnewaddress "
            f"when --apply runs",
            "                  <- not derived now: deriving a key is a wallet write, and a dry run "
            "writes nothing",
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
    lines = [f"  deposit target  {account}  <- every {from_asset} swap shares this one account (XRP_DEPOSIT_ACCOUNT)"]
    if needs_tag:
        lines.append(
            "                  a destination tag is allocated when --apply runs, and it is MANDATORY: the "
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
            f"  already open    (none) -- no {from_asset} swap is awaiting a deposit, so "
            f"`--swap latest` will resolve to the one this run creates"
        ]
    lines = [
        f"  already open    {len(rows)} {from_asset} swap(s) are ALREADY awaiting a deposit. Creating another "
        f"is allowed and is not a mistake, but `xrp_send_tagged.py --swap latest` will refuse from now on "
        f"(it refuses on ambiguity rather than guessing newest-wins), so use the `--swap <id>` command this "
        f"run prints:"
    ]
    lines.extend(
        f"                  {row['id']}  tag {row['deposit_tag']}  expects {row['expected_input_amount']} "
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
            f"  already open    (none) -- there is no database at {db_path} yet, so no swap of any kind is "
            f"open. --apply creates the file and the schema."
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
            f"  already open    NOT ESTABLISHED -- {db_path} could not be queried ({error}). --apply applies "
            f"the schema; until then this run cannot tell an empty table from a missing one."
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
        f"  prices          fetching USD prices from CoinGecko (services/pricing.py), cached for "
        f"{format_duration(ttl)} <- RATE_CACHE_SECONDS",
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
        f"                  got {len(prices) - 1} prices in {format_duration(time.monotonic() - started)}  "
        f"<- the count excludes the fetched_at stamp; create_quote() derives the rate from these, not this file",
        flush=True,
    )
    return prices


def report_lines(swap: dict, quote: dict, db_path: str, config: dict) -> list[str]:
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
        f"  destination tag {tag}  <- MANDATORY on {from_asset}: the account above is shared by every swap, so "
        f"an untagged payment to it is credited to nobody"
        if tag is not None
        else f"  destination tag (none) -- {from_asset} deposits are attributed by ADDRESS, so the address above "
        f"identifies this swap on its own"
    )
    return [
        "swap opened. One quote row, one swap row, one audit row -- written by the same service functions the",
        "web form calls. Nothing has been signed and nothing has been broadcast.",
        "",
        f"  database        {db_path}  <- the SAME file the workers read (SWAP_DB_PATH); a swap in any other "
        f"database is invisible to them",
        f"  swap id         {swap['id']}  <- names this swap to every command below, and to /swap/{swap['id']}",
        f"  quote id        {quote['id']}  <- the rate this swap was created against",
        f"  pair            {from_asset} -> {to_asset}",
        f"  deposit account {swap['deposit_address']}  <- send the deposit HERE",
        tag_line,
        f"  expected input  {swap['expected_input_amount']} {from_asset}  <- send EXACTLY this. "
        f"AMOUNT_TOLERANCE_PCT={tolerance} accepts {tolerance * 100:.2f}% either side; outside it the swap is "
        f"held for a person instead of paid out",
        f"  quoted rate     {swap['quoted_rate']} {to_asset} per {from_asset}  <- fixed now, not re-priced at "
        f"payout time",
        f"  fee             {swap['fee_bps']} bps  <- taken off the gross output by create_quote()",
        f"  network fee     {swap['network_fee_reserve']} {to_asset} reserved  <- held back for the payout "
        f"transaction's own chain fee",
        f"  estimated payout {swap['output_amount_estimate']} {to_asset}  <- what payout_worker broadcasts, "
        f"after the fee and the reserve above",
        f"  payout address  {swap['payout_address']}  <- FINAL. A payout is final the moment it is broadcast",
        f"  min confirmations {swap['min_confirmations']}  <- a COUNT of confirmations, never a duration "
        f"(rule 6). The deposit watcher releases the payout at or above it",
        f"  status          {swap['status']}",
        f"  rate window     {swap['expires_at']}  <- the QUOTE window, {format_duration(config['QUOTE_TTL_SECONDS'])} "
        f"wide. Nothing in this tree expires a SWAP (measured in services/swap_view.quote_window()), so a "
        f"deposit after it still credits",
    ]


def next_command(swap_id: str, from_asset: str) -> list[str]:
    """The exact next command, with the real id in it. No placeholder, ever.

    A placeholder in a pasted command has cost this project three mis-runs and
    twice put something in a shell that should never have been there -- once a
    wallet passphrase. The entire reason this file exists is that a swap id had
    to travel from a browser into a terminal by hand, so printing `<swap id>`
    here would reintroduce exactly the defect it was written to remove. Hence a
    function with its own test: a literal `<` in what this returns is a failure.

    An asset with no scripted sender gets an instruction rather than an invented
    command -- see DEPOSIT_SENDERS for how that list was established.
    """
    template = DEPOSIT_SENDERS.get(from_asset)
    if template:
        return [
            "Next, paste this. The swap id is already in it:",
            f"    {template.format(swap_id=swap_id)}",
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
    are inside ONE db_session so a refusal from create_swap() rolls back -- with
    one exception worth stating rather than discovering: create_quote() commits
    its own row before returning, so a swap refused after a successful quote
    leaves that quote behind. It is inert (nothing reads `quotes` except
    get_quote_or_raise() by id, and it expires), and the alternative -- holding
    the quote open across the swap's validation -- would mean reimplementing
    create_quote() without its commit. Named, not fixed.
    """
    from_asset, to_asset = pair
    print(f"\n  writing to      {db_path}", flush=True)
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
                f"  schema          ensured; deposit_tag column added now: {migrated['deposit_tag_added']}  "
                f"<- False means it was already there, which is the normal case",
                flush=True,
            )
            quote = create_quote(db, config, from_asset, to_asset, args.amount)
            print(
                f"  quote           {quote['id']}  rate {quote['quoted_rate']} {to_asset} per {from_asset}, "
                f"estimated payout {quote['output_amount_estimate']} {to_asset}",
                flush=True,
            )
            swap = create_swap(db, config, adapters, quote["id"], args.payout_address)
    except RequestException as error:
        # BEFORE the ValueError clause, and the order is load-bearing: requests'
        # JSONDecodeError subclasses BOTH RequestException and ValueError
        # (verified against requests 2.33.1), so a ValueError clause first would
        # report a mangled price response as a service refusal.
        raise SwapRefused(
            f"the price feed failed while pricing the quote ({type(error).__name__}: {error}). Nothing was "
            f"written, or -- if RATE_CACHE_SECONDS is 0 -- at most the quote row was."
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
    print(f"  wrote           in {format_duration(time.monotonic() - started)}", flush=True)
    return {"swap": swap, "quote": quote}


def apply_command(args, from_asset: str, to_asset: str) -> str:
    """The --apply command for THIS dry run, with the real values already in it.

    Echoes what was passed rather than a template, for the same reason
    next_command() does: the payout address is the one value a person states, and
    asking them to retype it into a second command is asking for the transcription
    error the dry run was supposed to catch. A payout address is public -- it is
    printed on the swap page -- so echoing it leaks nothing.
    """
    return (
        f"python3 open_swap.py --pair {from_asset}:{to_asset} --amount {args.amount} "
        f"--payout-address {args.payout_address} --apply"
    )


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
        "--amount", required=True, type=float,
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
    print(
        f"  mode            {'APPLY -- rows WILL be written' if args.apply else 'DRY RUN -- nothing is written, not even the schema'}",
        flush=True,
    )
    print(f"  database        {db_path}", flush=True)
    print(f"  pair            {from_asset} -> {to_asset}", flush=True)
    print(f"  amount          {args.amount} {from_asset}  <- what you will deposit", flush=True)
    print(f"  payout address  {args.payout_address}  <- where {to_asset} is sent; FINAL once the swap exists", flush=True)
    print(f"  pairs allowed   {pair_catalog(config)}  <- Config.ALLOWED_PAIRS", flush=True)
    print(f"  adapters here   {', '.join(sorted(adapters)) or '(none) -- no chain is configured in this process'}", flush=True)

    check_pair(config, from_asset, to_asset)
    check_chains_reachable(config, adapters, from_asset, to_asset)

    state, detail = check_payout_address(adapters, to_asset, args.payout_address)
    print(f"  address check   {state}  <- {detail}", flush=True)
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
    for line in report_lines(written["swap"], written["quote"], db_path, config):
        print(line, flush=True)
    print(f"\n  opened in       {format_duration(time.monotonic() - started)}", flush=True)
    print(flush=True)
    for line in next_command(written["swap"]["id"], from_asset):
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
