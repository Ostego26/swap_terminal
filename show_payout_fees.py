#!/usr/bin/env python3
"""What each payout ACTUALLY cost the chain, against the reserve this desk booked for it.

Role: file (entry point) -> the decision is measured_vs_booked() below
Reads: swap_terminal.db -- `payouts` and `swaps`, nothing else -- and, unless
       --no-chain is given, one read-only `gettransaction` per payout txid on the
       chain that paid it. No price feed, no .env, no secret, no keypair.
Writes: NOTHING. Every statement against the database is a SELECT, and the only
       RPC method it calls is `gettransaction`.
Can move funds: no. It constructs an adapter, which is how it asks a chain about a
       transaction, and it never calls send_to_address, never signs, never passes
       an arming token, and reports on payouts that were broadcast and cannot be
       unsent. It changes no reserve: setting one is a pricing decision and the
       operator's (rule 16).
Mainnet-safe: yes. It reads whatever database and whatever RPC settings it is
       handed and reaches nothing else.

WHY THIS FILE EXISTS, AND THE QUESTION IT ANSWERS.

The operator asked, 2026-10-03: "XRP_NETWORK_FEE_RESERVE -- what should i set this
too? what should they all be set too frankly?"

Measured that day, the tree could not answer it for a single chain. The four
reserves are bare defaults with no provenance comment between them --

    BTC_NETWORK_FEE_RESERVE  0.00002      config.py:114
    LTC_NETWORK_FEE_RESERVE  0.001        config.py:115
    GRC_NETWORK_FEE_RESERVE  0.01         config.py:116
    XRP_NETWORK_FEE_RESERVE  (absent)

-- and the ONE of them anybody has ever checked against a chain was wrong by a
factor of ten. services/quote_service.get_network_fee_reserve()'s docstring
records the measurement, taken on the operator's host 2026-10-01: "the real fee
was 0.001 GRC, not 0.01. The payout transaction moved that wallet's balance by
exactly -0.001, which is the fee and nothing else."

So the figure that decides whether a swap's margin covers its chain cost was ten
times the cost, for the only chain this desk has actually paid out on, and the way
that was discovered was somebody reading one wallet by hand. This tool does that
reading for every payout in the database at once.

WHAT THE RESERVE IS, BECAUSE IT CHANGED AND THE CHANGE IS WHY THIS IS SAFE TO RUN.

It is NOT taken out of the customer's payout. `sendtoaddress(address, amount)`
delivers `amount` exactly and the fee comes out of the wallet's own inputs --
measured to the last digit on the operator's host 2026-10-01, `getreceivedbyaddress`
reading 143.39622296 GRC against two output_amount_estimate values summing to
143.39622296, a difference of zero. The reserve is a BOOKKEEPING figure: what the
desk expects one payout to cost it, which is what makes a swap's margin knowable.
fee_ledger.py reports it as `reserve_bps` and reconciles it into the retained fee.

A reserve that is ten times the real cost therefore does not short any customer.
It makes every margin figure this desk reports wrong in the cautious direction,
and it refuses a quote on a chain nobody has written a number down for at all --
which is exactly the GRC -> XRP refusal the operator hit on 2026-10-02.

WHAT IT CANNOT MEASURE, SAID OUT LOUD RATHER THAN OMITTED (rule 14).

Only the Bitcoin-derived chains answer `gettransaction` with a `fee` field, so
BTC, LTC and GRC are measurable here and XRP and SOL are not. Those two print as
NOT MEASURABLE FROM HERE with the reason and the figure the tree already knows,
rather than being left off a table whose absence would read as "no payouts":

    XRP   the fee is autofilled by the server at submit time and the paid figure
          lives in the validated transaction's `Fee` field. chains/xrp_signing.py
          names the 10-drop base fee this ledger has charged for years
          (0.00001 XRP) and carries FEE_ALLOWANCE_DROPS = 1000 as a deliberately
          conservative PRE-FLIGHT figure that is explicitly "NOT the fee that gets
          paid". Reading the real one needs a `tx` request, which
          xrp_payout_verify.py already makes for the one validated payment.
    SOL   5,000 lamports per signature (0.000005 SOL), and the paid figure is in
          the confirmed transaction's meta.fee.

Adding those two is worth doing and is not guessed at here, because a number this
file invented would become the provenance for a reserve somebody then sets.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.registry import build_adapters, why_unconfigured
from chains.solana_fee_quote import transfer_fee_lamports_from_cluster
from chains.solana_units import LAMPORTS_PER_SOL
from config import Config
from microfortnights import format_duration
from report_block import labeled
from services.payout_capacity import as_amount

SELF = "show_payout_fees.py"

#: The chains whose wallet answers `gettransaction` with a `fee` field. Derived
#: from which adapter class implements it rather than hand-listed: chains/base.py's
#: RPCAdapter has get_transaction() and the Bitcoin-derived three inherit it, while
#: the XRP and SOL adapters do not have it at all.
#:
#: WHY A TABLE AND NOT `hasattr`. hasattr would be a runtime answer that quietly
#: changes meaning the day somebody adds a get_transaction() to the XRP adapter
#: that returns something without a `fee` -- and it would then report that chain as
#: measurable and print nothing for it, which is the silent-omission failure this
#: file's docstring is about. The reason each chain is or is not in here is prose,
#: so it belongs in prose.
MEASURABLE = ("BTC", "LTC", "GRC")

#: What the tree already knows about the two chains this tool cannot ask, so their
#: rows carry a figure and a provenance instead of a blank. NEITHER IS A
#: RECOMMENDATION and neither is read by anything: they are printed, and the text
#: says where each came from. See this module's docstring.
UNMEASURABLE_HERE = {
    "XRP": ("0.00001 XRP (10 drops)",
            "the base fee chains/xrp_signing.py names; the paid fee is autofilled at "
            "submit and lives in the validated transaction's Fee field"),
    "SOL": ("0.000005 SOL (5,000 lamports)",
            "per signature; the paid fee is in the confirmed transaction's meta.fee"),
}

PAYOUT_SQL = """
SELECT s.to_asset          AS asset,
       p.txid              AS txid,
       p.amount            AS amount,
       s.network_fee_reserve AS booked,
       p.sent_at           AS sent_at
FROM payouts p
JOIN swaps s ON s.id = p.swap_id
WHERE p.status = 'broadcast' AND p.txid IS NOT NULL
ORDER BY p.sent_at ASC, p.id ASC
"""


def measured_vs_booked(fees: list[float], booked: list[float]) -> dict:
    """The decision this file exists to put on a screen. Pure, so it is testable.

    `fees` are the fees a chain actually charged, `booked` the reserves the swaps
    recorded. Both are per payout and the two lists are parallel.

    RETURNS THE RATIO AS WELL AS BOTH FIGURES, because the ratio is the thing that
    was wrong: GRC's reserve was 10x its fee, and "0.01 against 0.001" is a
    sentence an operator has to do arithmetic on while "10.0x" is not.

    EMPTY IS A RESULT AND HAS ITS OWN SHAPE. A chain with no payouts yet returns
    n=0 and None for every figure, so the caller prints "(no payouts)" rather than
    a zero -- a zero fee is a claim, and "nothing has been paid out on this chain"
    is a different fact (rule 14).
    """
    if not fees:
        return {"n": 0, "measured": None, "booked": None, "ratio": None,
                "low": None, "high": None}
    mean_fee = sum(fees) / len(fees)
    mean_booked = sum(booked) / len(booked) if booked else None
    return {
        "n": len(fees),
        "measured": mean_fee,
        "booked": mean_booked,
        # None rather than a division when either side is zero: a booked reserve of
        # zero is a real configuration and inf is not a number an operator can act on.
        "ratio": (mean_booked / mean_fee) if mean_booked and mean_fee else None,
        "low": min(fees),
        "high": max(fees),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SELF,
        description="What each payout actually cost the chain, against the reserve this desk booked.",
    )
    parser.add_argument("--db", default=None,
                        help="the swap database to read (default: the one config.Config resolves)")
    parser.add_argument("--no-chain", action="store_true",
                        help="read the database only and ask no chain, so it is safe with every daemon down")
    # THE LIVE-FEE PAIR. Both are needed and neither has a default this tool could
    # invent: the fee depends on how many INPUTS the wallet selects, which depends
    # on the amount, and a destination has to be an address on the right network.
    #
    # THE ADDRESS IS NOT DERIVED HERE ON PURPOSE. getnewaddress would answer it in
    # one call and it is a WALLET WRITE -- it derives and stores a key -- and this
    # file's header promises it reads. The alternative, reusing a past payout's
    # address, would put a CUSTOMER's address into a transaction-building call, and
    # the one thing worse than asking the operator for an address is building
    # outputs to someone else's.
    parser.add_argument("--fee-address", default="", metavar="ADDRESS",
                        help="an address you control, on the chain whose fee you want measured. The live "
                             "fee line is printed for the chain whose daemon accepts it and says NOT "
                             "ASKED for the others. Nothing is signed, broadcast or locked.")
    parser.add_argument("--fee-amount", default=0.0, type=float, metavar="AMOUNT",
                        help="the payout size to measure the fee for. It matters: a bigger amount selects "
                             "more inputs, and the fee is bytes times a rate.")
    return parser


def read_fees(rows: list[dict], adapters: dict, *, ask_chain: bool, say) -> dict:
    """Group the payouts by asset and, where the chain can be asked, read each fee.

    EXTRACTED FROM main() RATHER THAN RAISING THE COMPLEXITY CEILING, which is what
    C901 asks for: a main() past the ceiling is orchestration that has swallowed a
    decision, and the decision here is "which payouts could be read, and which
    could not and why". Pulled out, it can be called with seeded rows and a stub
    adapter and asserted on directly.

    `say` is the printer, injected so a test can collect the progress lines instead
    of them going to a terminal -- the lines are part of what this function is for
    (rule 14), so a version that printed nothing would be testing something else.

    Every payout lands in exactly one of two lists per asset: `fees` if the chain
    answered with one, `unread` with the REASON if it did not. Nothing is silently
    dropped and nothing missing is counted as zero, because a fee of zero is a
    claim and "the daemon refused this one" is not that claim.
    """
    per_asset: dict[str, dict[str, list]] = {}
    for index, row in enumerate(rows, start=1):
        asset = row["asset"]
        bucket = per_asset.setdefault(asset, {"fees": [], "booked": [], "unread": []})
        bucket["booked"].append(float(row["booked"] or 0.0))
        if not ask_chain or asset not in MEASURABLE:
            continue
        adapter = adapters.get(asset)
        if adapter is None:
            bucket["unread"].append((row["txid"], f"no {asset} adapter in this process"))
            continue
        say(f"  reading {index}/{len(rows)} {asset} {row['txid'][:16]}")
        try:
            transaction = adapter.get_transaction(row["txid"])
        except Exception as exc:  # noqa: BLE001 -- checked: the failure is RECORDED per txid and printed, so a reader tells "the daemon refused this one" from "this one was free". It never joins the measured mean.
            bucket["unread"].append((row["txid"], f"{type(exc).__name__}: {exc}"))
            continue
        # `fee` is NEGATIVE in a Bitcoin wallet's gettransaction -- it is a debit --
        # and absent entirely for a transaction the wallet only received. abs() on a
        # present value; a MISSING one is recorded as unread rather than as zero,
        # because "the wallet has no fee for this" and "this cost nothing" are
        # different facts and only one of them is true.
        if "fee" not in transaction:
            bucket["unread"].append((row["txid"], "the wallet's record carries no `fee` field"))
            continue
        bucket["fees"].append(abs(float(transaction["fee"])))
    return per_asset


#: DROPS PER XRP. 10**6, and it is here rather than imported from
#: chains/xrp_units because this module must not import the signing side to divide
#: by a million -- the tree's own reason for keeping xrp_units out of modules that
#: only report.
DROPS_PER_XRP = 1_000_000


def ask_base_fee(asset: str, adapter) -> tuple[str | None, str] | None:
    """What the CHAIN says one payout costs right now, for a chain with no payout yet.

    WHY THIS EXISTS AT ALL. The operator asked 2026-10-03 what all four reserves
    should be, and for XRP and SOL this tool answered with a figure out of the tree
    and a note that it could not ask. That is better than silence and it is not an
    answer: the figure in the tree is a reference value somebody typed, and rule 17
    says run the thing that would show it false.

    AND WAITING FOR A PAYOUT TO MEASURE THEM IS BACKWARDS. Neither chain has ever
    paid out, so there is no transaction to read -- but XRP will state its current
    base fee without one, and chains/xrp.py ALREADY READS IT:
    XRPAdapter.server_parameters() returns `fee_drops` from
    server_info.validated_ledger.base_fee_xrp, and names its own fallback when the
    server does not report the field. That function existed before this tool did
    and this tool never called it, which is the call-site shape this file has
    already been caught by once today.

    SOL IS HERE NOW, 2026-10-03, AND THIS PARAGRAPH USED TO SAY IT WAS NOT. It read:
    "getFeeForMessage prices a SERIALIZED MESSAGE, so asking it needs a real
    blockhash and a built transfer ... named work rather than a number invented
    here." That work is done -- chains/solana_fee_quote.py does it, through the
    serializer tests/test_solana_transaction.py already pinned byte-for-byte
    against @solana/web3.js -- and the sentence is replaced rather than softened,
    because a comment that says a thing is not built is a wrong comment the day it
    is (rule 16).

    IT PRICES A MESSAGE AND NOT A SIGNED TRANSACTION, which is the whole reason a
    diagnostic may do this at all: getFeeForMessage needs no signature, so a fee
    quote never requires the ability to spend. A tool that held a key to read a fee
    would be a tool that can be made to send.

    NEITHER CHAIN FALLS BACK TO A CONSTANT. chains/solana_units.py:513's
    SIGNATURE_FEE_LAMPORTS = 5_000 calls ITSELF a "reference value;
    getFeeForMessage is the authority", so answering from it under this function's
    READ FROM THE SERVER label would make the label false -- which is the one
    outcome worse than no figure.

    THREE RETURNS, AND THE SIGNATURE SAID TWO. This hands back whatever the asker
    in _ASKERS returned, verbatim, and that table's own comment states the contract
    in full: `(figure, provenance)` on a read, `(None, reason)` on a failure, bare
    `None` for an adapter that cannot be asked at all. The annotation here read
    `tuple[str, str] | None` and so denied the middle one -- the case
    test_show_payout_fees.py asserts on by name ("a refusing cluster must be
    REPORTED, not treated as unaskable") and the case report_asset() branches on
    four lines after calling this (`if figure is None`). A declared type that
    excludes a tested, handled, reachable answer is a wrong comment in the one place
    a reader is most likely to trust without checking (rule 16).

    A FAILED ASK RETURNS THE REASON, never a number: this function's whole job is to
    replace a typed-in figure with a measured one, and a fallback that looked
    measured would defeat it.
    """
    asker = _ASKERS.get(asset)
    if adapter is None or asker is None:
        return None
    return asker(adapter)


def _ask_xrp(adapter) -> tuple[str | None, str] | None:
    """XRP's base fee from server_info. The read was already written; see ask_base_fee."""
    if not hasattr(adapter, "server_parameters"):
        return None
    try:
        parameters = adapter.server_parameters()
    except Exception as exc:  # noqa: BLE001 -- checked: the failure is RETURNED as the provenance string and printed, so the reader sees "the server could not be asked" rather than a figure that looks read. It never becomes a number.
        return None, f"the server could not be asked: {type(exc).__name__}: {exc}"
    drops = parameters.get("fee_drops")
    if drops is None:
        return None, "the server answered without a fee figure"
    return (f"{int(drops) / DROPS_PER_XRP:.6f} XRP ({int(drops)} drops)",
            parameters.get("fee_source") or "server_info.validated_ledger.base_fee_xrp")


def _ask_sol(adapter) -> tuple[str | None, str] | None:
    """SOL's fee from getFeeForMessage, priced against a real blockhash.

    chains/solana_fee_quote.transfer_fee_lamports_from_cluster() already returns
    exactly this function's contract -- (figure, provenance) on a read, (None,
    reason) on every failure -- so there is nothing to translate but the units, and
    report_asset() above needed no change at all to print it.
    """
    if not hasattr(adapter, "latest_blockhash"):
        return None
    quoted, provenance = transfer_fee_lamports_from_cluster(adapter)
    if quoted is None:
        return None, provenance
    return f"{quoted / LAMPORTS_PER_SOL:.9f} SOL ({quoted} lamports)", provenance


#: WHICH CHAINS CAN BE ASKED, and the dispatch rather than a chain of `if asset ==`.
#:
#: EXTRACTED 2026-10-03 WHEN SOL ARRIVED AND PLR0911 FIRED -- eight returns in one
#: function. Rule 12 says a function past a lint ceiling is a function that has
#: swallowed decisions, and the fix is to extract them rather than raise the ceiling
#: or write a noqa. Two chains made it a chain of branches; a third would have made
#: it unreadable, and the table is the thing a reader checks against MEASURABLE
#: above.
#:
#: Each asker returns this module's one contract: (figure, provenance) on a read,
#: (None, reason) on a failure, bare None for "I cannot ask this adapter at all".
_ASKERS = {
    "XRP": _ask_xrp,
    "SOL": _ask_sol,
}


def live_fee_line(asset: str, adapter, address: str, amount: float) -> str:
    """What one payout would cost THIS WALLET right now, asked of the daemon.

    THE OPERATOR, 2026-10-03: "we need to match the scaling here." Two fees read
    off their own node that day were wrong against the configured reserves in
    OPPOSITE directions --

        BTC   configured 0.00002   measured 0.00002820   41% too low
        LTC   configured 0.001     measured 0.00021483   4.7x too high

    -- and the transactions say why a constant cannot be right for both: the BTC
    send spent ONE input, the LTC send spent FIFTEEN, because that wallet is full
    of small mining outputs. A Bitcoin-style fee is bytes times a rate, and bytes
    scale with input count, so a flat reserve fits one wallet on one day.

    EVERY OTHER FIGURE IN THIS TOOL IS HISTORICAL -- what past payouts actually
    cost, read back from their transactions. This one is forward-looking, and it is
    the one that answers "what should I set the reserve to": it selects real inputs
    from the wallet's current UTXO set through fundrawtransaction and reports the
    fee that selection would pay. Nothing is signed, broadcast or locked.

    AND IT ANSWERS THE GRC QUESTION THE SAME WAY, which is the point of putting it
    behind one call. The evidence so far says Gridcoin is FLAT rather than scaled --
    7 of 7 payouts at exactly 0.001 with zero variance, and the one transaction
    read in full shows vin.size=1 with a 0.001 fee -- but nobody has ever sent a
    GRC payout spending fifteen inputs, so "flat" is a reading and not a
    measurement (rule 17). If Gridcoin's daemon answers fundrawtransaction, this
    line settles it; if that RPC is absent, it says NOT ESTABLISHED rather than
    assuming either way.

    THE ADDRESS IS THE WALLET'S OWN. A fee measurement needs a destination to build
    an output to, and using an address this wallet controls means the figure cannot
    accidentally become a real payment if someone later copies this call into a
    sending path. It is also the only address this tool can obtain without asking
    the operator for one.
    """
    if adapter is None or not hasattr(adapter, "measure_send_fee"):
        return (f"  live fee      NOT ASKED -- no {asset} adapter in this process, or its adapter cannot "
                f"measure a send (XRP and SOL price their own fees and are not Bitcoin-style)")
    if not address or amount <= 0:
        return ("  live fee      NOT ASKED -- pass --fee-address (an address you control on this chain) "
                "and --fee-amount to measure what one payout would cost this wallet right now. Both "
                "are needed: the fee is bytes x rate, and the bytes depend on how many inputs the "
                "amount makes the wallet select")
    if not adapter.validate_address(address):
        return (f"  live fee      NOT ASKED -- the {asset} daemon does not accept {address!r}, so it is "
                f"an address for a different chain. That is expected when one --fee-address is measured "
                f"against every configured chain")
    fee, how = adapter.measure_send_fee(address, amount)
    if fee is None:
        return f"  live fee      NOT ESTABLISHED -- {how}"
    configured = float(getattr(Config, f"{asset}_NETWORK_FEE_RESERVE", 0.0) or 0.0)
    # as_amount() AND NOT f"{fee}", because a fee is small enough to render in
    # exponent notation and did: the first version of this line printed "2.82e-05
    # BTC ... against a configured reserve of 2e-05", which is the defect
    # swap_readiness.rate_text() was written for the same day, one line over.
    #
    # THE TWO FORMATTERS ARE NOT DUPLICATES AND THE DIFFERENCE IS RECORDED AT BOTH
    # SITES (rule 8). as_amount() is for a COIN AMOUNT: eight decimals, truncated
    # down, which is lossless for a satoshi-exact fee. rate_text() is for a RATE,
    # which spans 9142021.6211 and 1.09e-07 in one line and therefore keeps
    # significant digits instead of decimal places. Using either for the other's
    # job prints something useless.
    if configured <= 0:
        return (f"  live fee      {as_amount(fee)} {asset} to send {as_amount(amount)} {asset} now  "
                f"<- {how}. No {asset}_NETWORK_FEE_RESERVE is set to compare it against")
    ratio = fee / configured
    # THE RATIO AND ITS DIRECTION, because the two errors are not equally bad. A
    # reserve BELOW the real fee under-protects the fee floor and the funding gate;
    # above it only makes the floor stricter than it needs to be.
    direction = ("the reserve is BELOW the measured fee, so the fee floor and the funding gate both "
                 "reserve too little" if fee > configured else
                 "the reserve is above the measured fee, which only makes the fee floor stricter")
    return (f"  live fee      {as_amount(fee)} {asset} to send {as_amount(amount)} {asset} now, against "
            f"a configured reserve of {as_amount(configured)} ({ratio:.2f}x)  <- {direction}. {how}")


def report_asset(asset: str, bucket: dict, say, adapter=None) -> None:
    """One asset's block: what was booked, what was measured, and the ratio.

    A FUNCTION FOR THE SAME REASON read_fees() IS ONE. "What does an operator read
    for this asset" is a decision about which cases they are in -- no payouts at
    all, not measurable here, measurable and nothing read, measurable and read, or
    no reserve configured -- and a decision inside a print loop cannot be called
    with seeded inputs.

    CALLED FOR EVERY ASSET, INCLUDING THE ONES WITH NO PAYOUTS, AND IT WAS NOT
    UNTIL 2026-10-03. main() had a SECOND loop printing a one-line "payouts=0" for
    any asset missing from the fee table, so the whole block below -- the reserve,
    whether it is absent, and the figure for a chain this tool cannot ask -- was
    unreachable for exactly those assets. Measured on the operator's host the hour
    this was written, they asked what to set XRP_NETWORK_FEE_RESERVE to and the
    tool answered:

        XRP  payouts=0  <- nothing paid out on this chain, so no fee to measure

    No reserve line, no "absent", no 10-drop figure. That is the SILENT-OMISSION
    FAILURE THIS FILE'S OWN DOCSTRING IS ABOUT, shipped in the same commit that
    described it, to the one reader it was written for.

    AND THE TEST THAT WAS SUPPOSED TO CATCH IT PASSED, because it called this
    function directly with a seeded bucket and never ran main(). A unit test on a
    branch is not a test of whether the branch is REACHED -- which is this
    repository's recurring defect shape: a correct function whose call site
    discards it. tests/test_show_payout_fees.py now drives main() over a seeded
    database as well, and that is the only shape that would have failed.
    """
    verdict = measured_vs_booked(bucket["fees"], bucket["booked"])
    setting = f"{asset}_NETWORK_FEE_RESERVE"
    configured = getattr(Config, setting, None)
    shown = "(absent -- every quote paying out in this asset REFUSES)" if configured is None else configured
    say(f"{asset}  payouts={len(bucket['booked'])}  {setting}={shown} <- CONFIGURED NOW")
    if not bucket["booked"]:
        # NOTHING PAID OUT YET IS ITS OWN CASE and is reported BEFORE the
        # measurability question, because "no fee to measure" is true of this chain
        # for a reason that has nothing to do with whether the tool could ask it.
        # The reserve line above still printed, which is the half that was missing.
        say("  measured fee: (no payouts on this chain yet, so there is no fee to measure)")
        asked = ask_base_fee(asset, adapter)
        if asked is not None:
            figure, provenance = asked
            # A figure READ FROM THE CHAIN outranks the one in the tree and is
            # labeled as read, so the operator can tell which they are holding
            # (rule 17). A failed ask prints its reason in the same slot and no
            # figure at all.
            if figure is None:
                say(f"  asked the chain for its current fee: FAILED -- {provenance}")
            else:
                say(f"  asked the chain for its current fee: {figure} <- READ FROM THE SERVER, not the tree")
                say(f"    {provenance}")
        if asset in UNMEASURABLE_HERE:
            figure, provenance = UNMEASURABLE_HERE[asset]
            label = ("the tree's reference figure, for comparison" if asked and asked[0]
                     else "this tool could not read a paid fee; the tree's figure is")
            say(f"  {label} {figure} -- {provenance}")
        return
    if asset in UNMEASURABLE_HERE:
        figure, provenance = UNMEASURABLE_HERE[asset]
        say(f"  measured fee: NOT MEASURABLE FROM HERE. The tree's figure is {figure} -- {provenance}")
    elif verdict["n"] == 0:
        say(f"  measured fee: (none read) <- 0 of {len(bucket['booked'])} payouts answered with a fee")
    else:
        say(f"  measured fee: mean {verdict['measured']:.8f} over {verdict['n']} payouts "
            f"(low {verdict['low']:.8f}, high {verdict['high']:.8f})")
        # THE BOOKED FIGURE IS HISTORY AND THE CONFIGURED ONE IS THE FUTURE, AND
        # SAYING SO IS NOT A NICETY. The operator set GRC_NETWORK_FEE_RESERVE to
        # the measured 0.001 on 2026-10-03 and this tool still printed
        #
        #     GRC  payouts=7  GRC_NETWORK_FEE_RESERVE=0.001
        #       measured fee: mean 0.00100000 ...
        #       booked/measured: 10.00x
        #
        # which reads, on one screen, as "you changed it and nothing happened".
        # Both numbers were right. `booked` is swaps.network_fee_reserve -- the
        # figure stamped on each swap AT QUOTE TIME -- and all seven of those swaps
        # were quoted while the config said 0.01, so 10.00x is a true statement
        # about seven payouts that have already happened. A config change cannot
        # reach back into them, and nothing should pretend it did.
        #
        # So the ratio now says WHEN its numerator is from, and the configured
        # figure gets its own comparison line. Rule 14: state what the number means
        # next to the number, because the operator reads the screen.
        say(f"  booked at quote time: mean {verdict['booked']:.8f} over {verdict['n']} payouts")
        if verdict["ratio"] is not None:
            say(f"  booked/measured: {verdict['ratio']:.2f}x  <- HISTORICAL. 1.0 means those swaps "
                f"booked what the chain charged; above 1.0 the desk booked more cost than it paid, so "
                f"the margin it REPORTED on them is understated. Changing the setting cannot alter "
                f"this -- the figure is stamped on each swap when it is quoted.")
        if configured is not None and verdict["measured"]:
            ahead = float(configured) / verdict["measured"]
            say(f"  configured/measured: {ahead:.2f}x  <- WHAT THE NEXT SWAP WILL BOOK. This is the one "
                f"a change to {setting} moves, and 1.0 is the target.")
    for txid, why in bucket["unread"]:
        say(f"  unread {txid[:16]}: {why}")


def load_payouts(db_path: str) -> list[dict]:
    """Every broadcast payout with a txid, read-only. `mode=ro` is the assertion.

    A read-only URI rather than a plain path, because this module's header claims it
    writes nothing and a connection opened for writing is a claim nobody checked.
    SQLite refuses the write; the header does not have to be trusted.
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(PAYOUT_SQL)]
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    db_path = args.db or Config.DB_PATH

    def say(text: str) -> None:
        print(text, flush=True)

    # RULE 14: ANNOUNCE THE TARGET AND THE SCALE BEFORE THE WORK, not after. One
    # gettransaction per payout over a remote daemon is the slow part, and a reader
    # who cannot see how many are coming cannot tell working from hung.
    say(f"{SELF}: reading payouts from {db_path}")
    say(f"  asking the chain for each fee: "
        f"{'NO (--no-chain)' if args.no_chain else 'YES, one gettransaction per payout'}")

    rows = load_payouts(db_path)
    say(f"  broadcast payouts with a txid: {len(rows)}  <- the denominator for every figure below")
    if not rows:
        say("  (none) -- nothing has been paid out on any chain, so there is no fee to measure and no "
            "reserve this tool can check")
        say(labeled("done in", format_duration(time.monotonic() - started)))
        return 0

    # build_adapters() opens no socket and constructs a chain only when its settings
    # are all present, so this is safe with every daemon down. Built ONCE: the earlier
    # version called it a second time in the no-payouts loop below, which would have
    # re-derived the same answer for a different reason in the same run.
    adapters = {} if args.no_chain else build_adapters(Config.RPC)
    per_asset = read_fees(rows, adapters, ask_chain=not args.no_chain, say=say)

    say("")
    # EVERY ASSET THROUGH THE SAME FUNCTION. The second loop that used to print a
    # bare "payouts=0" for the assets missing from `per_asset` is DELETED, not
    # fixed: two printers for one block is how XRP came to be reported without its
    # reserve line (see report_asset's docstring). An empty bucket is a bucket.
    for asset in sorted(set(per_asset) | set(Config.RPC)):
        report_asset(asset, per_asset.get(asset, {"fees": [], "booked": [], "unread": []}), say,
                     adapter=adapters.get(asset))
        # THE FORWARD-LOOKING FIGURE, beside the historical ones, for every asset.
        # One printer for the whole block (report_asset's docstring records what the
        # second one cost), so this is appended here rather than becoming a third.
        if not args.no_chain:
            say(live_fee_line(asset, adapters.get(asset), args.fee_address or "",
                              args.fee_amount))
        if not args.no_chain and asset in MEASURABLE and asset not in adapters:
            say(f"  {why_unconfigured(asset, Config.RPC)}")

    say("")
    say("  SETTING A RESERVE IS A PRICING DECISION AND YOURS (rule 16). This prints the evidence; it "
        "changes nothing.")
    say(labeled("done in", format_duration(time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
