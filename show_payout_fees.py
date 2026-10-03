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
from config import Config
from microfortnights import format_duration
from report_block import labeled

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


def report_asset(asset: str, bucket: dict, say) -> None:
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
    say(f"{asset}  payouts={len(bucket['booked'])}  {setting}={shown}")
    if not bucket["booked"]:
        # NOTHING PAID OUT YET IS ITS OWN CASE and is reported BEFORE the
        # measurability question, because "no fee to measure" is true of this chain
        # for a reason that has nothing to do with whether the tool could ask it.
        # The reserve line above still printed, which is the half that was missing.
        say("  measured fee: (no payouts on this chain yet, so there is no fee to measure)")
        if asset in UNMEASURABLE_HERE:
            figure, provenance = UNMEASURABLE_HERE[asset]
            say(f"  and this tool could not ask anyway: the tree's figure is {figure} -- {provenance}")
        return
    if asset in UNMEASURABLE_HERE:
        figure, provenance = UNMEASURABLE_HERE[asset]
        say(f"  measured fee: NOT MEASURABLE FROM HERE. The tree's figure is {figure} -- {provenance}")
    elif verdict["n"] == 0:
        say(f"  measured fee: (none read) <- 0 of {len(bucket['booked'])} payouts answered with a fee")
    else:
        say(f"  measured fee: mean {verdict['measured']:.8f} over {verdict['n']} payouts "
            f"(low {verdict['low']:.8f}, high {verdict['high']:.8f})")
        if verdict["ratio"] is not None:
            say(f"  booked/measured: {verdict['ratio']:.2f}x  <- 1.0 means the reserve matches what the "
                f"chain charged; above 1.0 the desk books more cost than it pays, so every margin figure "
                f"it reports is understated")
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
        report_asset(asset, per_asset.get(asset, {"fees": [], "booked": [], "unread": []}), say)
        if not args.no_chain and asset in MEASURABLE and asset not in adapters:
            say(f"  {why_unconfigured(asset, Config.RPC)}")

    say("")
    say("  SETTING A RESERVE IS A PRICING DECISION AND YOURS (rule 16). This prints the evidence; it "
        "changes nothing.")
    say(labeled("done in", format_duration(time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
