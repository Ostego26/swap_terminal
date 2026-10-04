#!/usr/bin/env python3
"""Report the fee this desk has retained, per asset, and sweep it out. Report-only by default.

Role: file (operator entry point at the project root per CLAUDE.md rule 10; every
      decision is a function in swap_terminal/fee_sweep.py and is callable with
      seeded rows)
Reads: swap_terminal.db -- `swaps`, `payouts` (through fee_ledger.py),
      `wallet_inventory` and `fee_sweeps` -- and ONE read-only balance call per
      asset that has retained a fee (services/payout_capacity.
      largest_fundable_payout(), which is `getbalance`). It reads no .env, no
      keypair file, no WIF, no wallet.dat and no price feed. It never reads or
      prints GRIDCOIN_WALLET_PASSPHRASE: the unlock is performed by
      services/payout_service.payout_unlock_context(), which takes the passphrase
      from the environment itself and logs no parameters.
Writes: with --apply ONLY -- rows in `fee_sweeps`, and nothing else. No swap, no
      payout, no inventory row, no quote, no audit row, no fee rate.
Can move funds: YES, WITH --apply, AND THAT IS WHAT IT IS FOR. --apply BROADCASTS
      a transaction sending this desk's retained fee to the configured
      destination, and a broadcast is final. Without --apply nothing is signed,
      nothing is sent and no row is written -- every check still runs and the
      exact send is printed.
Mainnet-safe: the default run is read-only in both directions and safe against a
      live wallet while workers are cycling. --apply is fund movement on whatever
      network the configured endpoints point at; it states the destination, the
      amount and the wallet before it moves anything, and it refuses outright
      rather than improvising whenever it cannot establish that the wallet can
      still fund the swaps it owes.

=============================================================================
OPERATOR, 2026-10-04: "i NEED to collect a fee to be profitable"
=============================================================================

The fee was never missing. services/quote_service.py:615 charges it as a
subtraction --

    output_amount_estimate = max(gross_output * (1 - fee_bps / 10000.0), 0.0)

-- so the desk keeps 150bps by sending that much less, and the money is already
theirs and already in the hot wallet. What did not exist was any way to SEE it
apart from trading inventory, or to TAKE IT OUT.

Half of that was answered on 2026-10-01: swap_terminal/fee_ledger.py derives
`retained = realized_gross - paid` per completed swap, in SQL, over `swaps` and
`payouts`, and show_fees.py prints it. This file is the other half, and it adds
exactly two things to that ledger -- a record of what has already been taken out,
and the send.

THE ACCRUED FIGURE IS NOT RE-DERIVED HERE. fee_ledger.FEE_LEDGER_SQL is the one
place the arithmetic exists and this file contains no arithmetic about what a swap
earned: it calls fee_rows() and asset_totals() and reads `.retained`. A second
copy of that expression would be rule 8's defect on the number that decides what
leaves the wallet.

=============================================================================
WHAT MAKES RUNNING IT TWICE SAFE, AND WHY A TABLE WAS NEEDED FOR THAT
=============================================================================

The accrued fee is DERIVED from completed swaps, so it does not go down when the
fee is collected -- the retained coin never had a row, it simply stayed in the
wallet. Run a sweep twice against a derived accrual and the second send comes out
of customer deposits.

So `fee_sweeps` records every sweep, and `swept` is subtracted from `accrued`.
Its DDL in db.py carries the full reasoning; the two properties that matter here:

  THE ROW IS COMMITTED BEFORE THE SEND. record_sweep_intent() INSERTs 'created'
  and commits, so a process that dies between there and the broadcast leaves a
  row that is COUNTED AS SWEPT. The fee stays in the wallet until somebody looks,
  which costs a delay; the alternative costs a double send.

  THE TXID IS RECORDED INSIDE THE WALLET-UNLOCK CONTEXT. Not after it. On
  2026-09-26 services/payout_service.py recorded a payout AFTER the `with` block
  and the context's restore raised -- Gridcoin refuses a staking unlock timeout of
  0 -- so control jumped past the record and 55.52645238 GRC that had already left
  the wallet was written down as a failure with no txid. Money out, no record. The
  send below is wrapped in that same context for GRC, so it has that same shape,
  and record_sweep_broadcast() is called inside it with `recorded` set only after
  it returns.

=============================================================================
THE ONE SAFETY PROPERTY: A SWEEP MUST NOT STRAND A PAYOUT
=============================================================================

The hot wallet holds customer deposits awaiting payout AND retained fees, in the
same coin, indistinguishably. Sweeping more than the fee would spend money owed to
a customer whose deposit is already confirmed and irreversible -- and not paying
them does not give it back.

fee_sweep.obligation() is the floor and fee_sweep.sweep_plan() is the refusal.
The arithmetic is printed per asset:

    could leave   services/payout_capacity.largest_fundable_payout(): the wallet's
                  spendable balance less the sweep transaction's own chain fee
    obligated     the larger of (the payout every open swap will need, each at
                  services/payout_service.payout_amount() plus its own chain fee)
                  and wallet_inventory.hot_reserved
    headroom      could leave - obligated
    refused when  unswept > headroom

A sweep that strands a payout is worse than no sweep at all, so an unreadable
balance REFUSES. That is the opposite of how services/payout_capacity.py resolves
the same unknown for a PAYOUT, deliberately and for a stated reason: by the time
that gate is asked the customer's deposit is already credited, so refusing costs
them their money, where refusing a sweep costs nobody anything at all.

=============================================================================
WHAT IT DOES NOT DECIDE, AND WHAT IT DOES NOT REFUSE
=============================================================================

THE DESTINATION IS CONFIGURATION AND HAS NO DEFAULT. config.fee_sweep_destination()
reads <ASSET>_FEE_SWEEP_DESTINATION and returns "" when unset; a chain with no
destination is REFUSED with the variable named. A defaulted fee address is a final,
unrecoverable transaction to somewhere nobody chose.

THERE IS NO --amount. The figure is `accrued - swept`, both of which are queries.
A flag naming a number would be a second authority for what the desk is owed, and
the two would disagree the first time somebody typed a figure off yesterday's
report.

THE CHAIN FEE DOES NOT BLOCK ANYTHING. Where the unswept fee is close to or below
what the chain charges to move it, the line SAYS SO with both numbers and the
sweep stays available. Whether 0.001 GRC is worth paying to move 0.002 GRC is a
judgment about the desk's own money, and inventing a threshold here would be this
tool deciding something nobody asked it to (rule 16).

THE FEE RATE IS UNTOUCHED. No --set, no DEFAULT_FEE_BPS, no reserve, no pricing.

=============================================================================
WHAT THE ACCRUED FIGURE DOES NOT SUBTRACT, SAID HERE RATHER THAN DISCOVERED
=============================================================================

`retained` is what stayed in the wallet out of each swap's gross. It is NOT profit
net of the chain fees the desk already paid to DELIVER those payouts: each
`sendtoaddress` cost the wallet its own fee, out of its own inputs, and those are
not in `payouts.amount` and so not in the ledger. show_payout_fees.py is the tool
that reads what each payout actually cost (all 7 of 7 GRC payouts on the
operator's host answered 0.00100000, mean, low and high identical), and this file
does not duplicate it -- it names it on screen instead, so the figure here is read
as gross retention rather than as net profit.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.gridcoin_wallet_lock import GridcoinLockError
from chains.registry import build_adapters
from config import FEE_SWEEP_DESTINATION_TEMPLATE, Config, fee_sweep_destination
from db import db_session
from fee_ledger import asset_totals, fee_rows
from fee_sweep import (
    ALREADY_SWEPT,
    NOTHING_ACCRUED,
    REFUSE,
    SWEEP,
    Accrual,
    WalletCeiling,
    obligation,
    record_sweep_broadcast,
    record_sweep_failed,
    record_sweep_intent,
    sweep_plan,
    swept_by_asset,
    unresolved_sweeps,
)
from microfortnights import format_duration
from report_block import CONTINUATION, labeled
from services.payout_capacity import as_amount, largest_fundable_payout
from services.payout_service import broadcast_payout, payout_unlock_context
from services.quote_service import get_network_fee_reserve
from workers.common import db_path_source, get_config_dict, root_tool_command

SELF = "collect_fees.py"

#: The verdicts, in the order they are printed, each with what the section MEANS.
#:
#: A LIST RATHER THAN PRINTING AS IT GOES, so a section with no rows can print
#: `(none)` instead of nothing (rule 14) and so the refusals cannot end up buried
#: under the sweeps. The same shape, for the same reason, as
#: correct_payout_amounts._report().
VERDICT_SECTIONS = (
    (SWEEP, "a fee is accrued, a destination is set, and the wallet can spare it"),
    (REFUSE, "something has to be fixed or decided first -- NOTHING is sent for these"),
    (ALREADY_SWEPT, "the accrued fee has already been collected; a re-run sends nothing"),
    (NOTHING_ACCRUED, "no delivered payout in this asset has retained anything"),
)


def chain_fee_for(config, asset: str) -> tuple[float | None, str]:
    """What the chain charges to move `asset`, from <ASSET>_NETWORK_FEE_RESERVE. (fee, how).

    services/quote_service.get_network_fee_reserve() IS THE AUTHORITY AND IS CALLED
    RATHER THAN THE CONFIG KEY READ DIRECTLY (rule 8). That function's docstring
    carries the 2026-10-01 measurement of what the figure is and is not -- it is
    what the DESK expects one payout to cost it, paid out of the wallet's own
    inputs, never deducted from the amount sent -- and reading
    `config[f"{asset}_NETWORK_FEE_RESERVE"]` here would be a second reader of the
    same key with none of that attached.

    IT RAISES ValueError FOR AN ASSET WITH NO RESERVE, and that is translated into
    (None, sentence) rather than propagated, because a missing reserve is a thing
    to report per asset and not a reason to abandon a report about four others.
    None then reaches fee_sweep.sweep_plan() as a ceiling that could not be
    established, which refuses -- the right answer, since without the chain fee the
    headroom arithmetic has no term for the cost of the sweep itself.

    THE MEASURED COSTS, so the figures on screen can be read against something:
    GRC is a flat 0.001 (9 of 9 of the operator's payouts, zero variance), BTC
    0.0000282 at one input rising to 0.00084240 at 2701 inputs, LTC about
    0.0000150 per input, XRP 10 drops, SOL 5,000 lamports. The per-input scaling is
    why a BTC figure here is an expectation rather than a quote: the wallet picks
    the inputs at send time and this tool does not ask it to plan a transaction.
    """
    try:
        return get_network_fee_reserve(config, asset), f"{asset}_NETWORK_FEE_RESERVE"
    except ValueError as refusal:
        return None, (
            f"{asset}_NETWORK_FEE_RESERVE could not be read, so the cost of the sweep transaction "
            f"itself is unknown and no headroom can be computed: {refusal}"
        )


def plan_for_asset(db, config, adapters, total, already_swept: dict) -> tuple[object, object]:
    """One asset's sweep plan and its obligation. The reading; the decisions are below.

    Returns both because the printer needs the obligation's breakdown -- the open
    count, the reservation, the revivable total -- and recomputing it for display
    would be a second answer to one question on one screen (rule 8).

    `already_swept` IS PASSED IN RATHER THAN QUERIED HERE, and that is not
    micro-optimization: fee_sweep.swept_by_asset() is ONE GROUP BY over the whole
    table, so calling it inside this function would run it once per asset and --
    worse -- could return different totals to two assets in one run if a sweep
    committed between them. One read, one answer, one screen (rule 14: the
    parameters that decide the answer have to be the same ones the report echoes).

    ONE BALANCE CALL PER ASSET, and only for an asset the LEDGER already shows a
    retention in: this iterates fee_ledger.asset_totals(), so a chain this desk has
    never earned in costs no round trip. That matters because a daemon that is down
    hangs for its whole timeout, and rule 14's complaint is about exactly the wait
    this would otherwise take on five chains.
    """
    asset = total.asset
    chain_fee, fee_how = chain_fee_for(config, asset)
    owed = obligation(db, asset)
    if chain_fee is None:
        ceiling = WalletCeiling(None, fee_how)
    else:
        ceiling = WalletCeiling.from_largest_fundable_payout(
            *largest_fundable_payout(adapters, asset, chain_fee)
        )
    swept, sweeps = already_swept.get(asset, (0.0, 0))
    # RECORDED SO THE PRINTER CAN SHOW THE FIGURES THE VERDICT USED. See _accruals.
    accrual = Accrual(asset=asset, accrued=total.retained, swept=swept, sweeps=sweeps)
    _accruals[asset] = accrual
    plan = sweep_plan(
        accrual=accrual,
        destination=fee_sweep_destination(asset),
        ceiling=ceiling,
        owed=owed,
        # 0.0 ONLY WHEN THE RESERVE COULD NOT BE READ, in which case the ceiling
        # above is already None and sweep_plan() refuses before this is used. It is
        # not a default standing in for a real fee of zero.
        chain_fee=0.0 if chain_fee is None else chain_fee,
    )
    return plan, owed


def describe(plan, owed, total) -> list[str]:
    """The lines one asset prints. Every number with what it means beside it (rule 14).

    THE ACCRUED FIGURE CARRIES ITS DENOMINATOR, which is the swap count: a retention
    with no count behind it cannot be told from one lucky swap (rule 3), and
    fee_ledger.AssetTotal exists partly to supply it.

    `as_amount()` FOR DISPLAY AND NOT FOR THE PLAN. It truncates at eight decimals,
    down, which is what makes a GRC figure readable -- but the amount actually sent
    is the quantized float, and the sentence in `plan.why` prints the exact values
    the refusal was computed from. A reader comparing the two sees the same number
    at different precisions rather than two different numbers.
    """
    unswept = total.retained - plan_swept(plan)
    lines = [
        f"  {plan.verdict:<16} {plan.asset}",
        labeled("accrued", f"{as_amount(total.retained)} {plan.asset} across {total.swaps} delivered "
                           f"payout(s)  <- retained = received minus paid out, from fee_ledger.py. "
                           f"GROSS retention: the chain fees this desk already paid to deliver those "
                           f"payouts are NOT subtracted (show_payout_fees.py measures those)"),
        labeled("swept", f"{as_amount(plan_swept(plan))} {plan.asset} in {plan_sweeps(plan)} recorded "
                         f"sweep(s)  <- what has already left, from `fee_sweeps`. This is the only "
                         f"thing that stops a second run sending the same fee again"),
        labeled("sweepable", f"{as_amount(unswept)} {plan.asset}  <- accrued minus swept. What --apply "
                             f"would send for this asset, before quantizing it to what the chain can "
                             f"express"),
        labeled("chain fee", f"{as_amount(plan.chain_fee)} {plan.asset}  <- what moving it is expected "
                             f"to cost, from {plan.asset}_NETWORK_FEE_RESERVE. Paid by the wallet out "
                             f"of its own inputs, NOT deducted from the amount swept"),
        labeled("obligated", f"{as_amount(owed.floor)} {plan.asset}  <- what this wallet must KEEP: the "
                             f"larger of {as_amount(owed.open_total)} for {owed.open_swaps} open "
                             f"swap(s) and {as_amount(owed.reserved)} reserved in wallet_inventory. A "
                             f"sweep is refused rather than reduced if it would eat into this"),
        labeled("destination", f"{plan.destination or '(none) -- ' + FEE_SWEEP_DESTINATION_TEMPLATE.format(asset=plan.asset) + ' is not set'}"),
    ]
    if owed.revivable_swaps:
        lines.append(
            CONTINUATION + f"NOT in the obligated figure: {as_amount(owed.revivable)} {plan.asset} "
                           f"across {owed.revivable_swaps} halted or failed swap(s), which WOULD need "
                           f"funding if resolve_halted_swap.py or settle_payout.py revived them. Each "
                           f"of those tools checks the balance itself at the moment it runs, which is "
                           f"after any sweep -- so this is yours to weigh, not a refusal."
        )
    if plan.verdict == SWEEP and plan.chain_fee > 0 and unswept <= plan.chain_fee * 2:
        lines.append(
            CONTINUATION + f"PROBABLY NOT WORTH SWEEPING: {as_amount(unswept)} {plan.asset} sweepable "
                           f"against {as_amount(plan.chain_fee)} {plan.asset} to move it -- the chain "
                           f"would take about {plan.chain_fee / unswept * 100:.0f}% of it. Nothing is "
                           f"blocked on that and the sweep is still offered; it is your call."
        )
    lines.append(CONTINUATION + plan.why)
    return lines


def plan_swept(plan) -> float:
    """What `plan` was told had already been swept.

    A FUNCTION BECAUSE fee_sweep.SweepPlan DELIBERATELY DOES NOT CARRY IT. That
    tuple holds the verdict and the amount to send; the accrual it was computed
    from belongs to the caller that read it, and duplicating it onto the plan would
    let the two drift. This and plan_sweeps() below are where the printer gets it
    back, from the same Accrual the plan was built with -- so there is one read of
    `fee_sweeps` per asset per run and the figure on screen is the figure the
    verdict used.
    """
    return _accruals[plan.asset].swept


def plan_sweeps(plan) -> int:
    """How many sweeps that total came from. The denominator for plan_swept() (rule 3)."""
    return _accruals[plan.asset].sweeps


#: The Accrual each plan was built from, by asset, so the printer can show the
#: figures the verdict actually used without re-querying.
#:
#: MODULE STATE IS A SMELL AND THIS ONE IS BOUNDED AND SAID OUT LOUD: it is written
#: once per asset in plan_for_asset() during a single run of main(), read only by
#: the two accessors above, and never consulted by any decision -- fee_sweep.
#: sweep_plan() receives the Accrual as an argument and reads nothing global. The
#: honest alternative was a sixth field on SweepPlan, which would have put the
#: accrual in two places (see plan_swept()).
_accruals: dict[str, Accrual] = {}


def apply_sweep(db, config, adapters, plan) -> list[str]:
    """Broadcast ONE asset's sweep and record it. Returns the lines to print.

    THE AMOUNT IS ALREADY QUANTIZED when it gets here -- fee_sweep.sweep_plan()
    does it, for the reason recorded there -- so this function decides nothing
    about how much moves. It decides the ORDER in which the send and the record
    happen, and that order is the whole of it.

    THE ORDERING IS services/payout_service.process_pending_payouts()'S, AND IT IS
    COPIED DELIBERATELY RATHER THAN INVENTED. That function's own comments carry the
    measurement; this is the same sequence applied to a sweep:

      record intent   committed BEFORE the send. From this instant the amount is
                      counted as swept, so an interruption cannot double-send it.
      unlock          payout_unlock_context(): lock -> full unlock -> send -> lock
                      -> back to staking, in a `finally`, for GRC; a nullcontext
                      for every other chain. REUSED, not reimplemented -- a second
                      lock dance in this tree would be rule 8's defect on the
                      wallet's security state. The passphrase is read from the
                      environment by that function and is never named, printed or
                      logged here.
      send            broadcast_payout(), which is the one dispatch that knows what
                      each chain's send requires. Also reused: a sweep is a send
                      like any other, and a second spelling of XRP's arming token
                      or SOL's confirm_send would hide from `grep -rn`.
      record txid     INSIDE the context, before the re-lock can raise, with
                      `recorded` set only after the commit returns.

    A FAILED RE-LOCK IS NOT A FAILED SWEEP, which is the whole point of the
    GridcoinLockError branch: the sweep is already durable, the wallet's lock state
    is a separate problem with its own loud sentence from
    chains/gridcoin_wallet_lock.restore_failed_because(), and treating it as a
    failure is what mislabeled a delivered payment on 2026-09-26. Re-raised when
    the send never got as far as being recorded, because then it IS the sweep's
    failure.

    A FAILED SEND RELEASES THE ROW. record_sweep_failed() puts it outside
    SWEPT_SQL's predicate, so the fee is offered again next run -- correct only
    because this branch is reached when no txid came back at all.
    """
    asset = plan.asset
    # plan.amount VERBATIM. fee_sweep.sweep_plan() has already run it through
    # chains/payout_quantization.quantize_for_chain(), so quantizing again here
    # would be a second place the figure is decided -- and the figure a plan
    # PRINTS and the figure a plan SENDS diverging is the defect this whole tool
    # is built around not reproducing. A plan whose amount quantized to nothing
    # never reaches here: it is ALREADY SWEPT, with the residue named.
    amount = plan.amount
    lines = [f"  {asset} sweeping {amount!r} to {plan.destination}"]
    sweep_id = record_sweep_intent(db, asset, plan.destination, amount)
    lines.append(f"  {asset} fee_sweeps.id={sweep_id} recorded as 'created' and COMMITTED before the "
                 f"send, so this amount cannot be swept twice even if this process dies now")
    recorded = False
    txid = ""
    try:
        try:
            with payout_unlock_context(asset, adapters[asset]):
                txid = broadcast_payout(adapters[asset], asset, config, plan.destination, amount)
                written = record_sweep_broadcast(db, sweep_id, txid)
                recorded = True
                lines.append(f"  {asset} BROADCAST txid={txid}  <- final; this cannot be unsent. "
                             f"fee_sweeps.id={sweep_id} "
                             + ("updated to 'broadcast' with that txid, inside the wallet-unlock "
                                "context and before the re-lock could raise"
                                if written else
                                "was ALREADY resolved by something else, so the txid above is NOT "
                                "recorded against it -- read the row and reconcile it by hand"))
        except GridcoinLockError:
            if not recorded:
                raise
            lines.append(f"  {asset} the sweep IS broadcast ({txid}) and IS recorded. The wallet's lock "
                         f"state could not be restored afterwards -- read the error above and act on "
                         f"the WALLET, not on this sweep")
    except Exception as exc:  # noqa: BLE001 -- checked: the caller CAN tell this from success, both in the returned lines (which say NOT SENT and name the exception) and in the database (the row is moved to 'failed', which puts the fee back in the sweepable figure). A narrow catch is impossible here for the reason services/payout_service.py gives at its own send: a send fails for transport, daemon, insufficient-funds or rejection reasons, every one of which must land this sweep in 'failed' with the reason on screen rather than aborting the run and leaving the other assets unreported.
        released = record_sweep_failed(db, sweep_id, str(exc))
        lines.append(
            f"  {asset} NOT SENT -- {type(exc).__name__}: {exc}"
        )
        lines.append(
            f"  {asset} fee_sweeps.id={sweep_id} "
            + ("moved to 'failed', so this fee is sweepable again on the next run. Nothing reached a "
               "chain: no txid came back"
               if released else
               "could NOT be moved to 'failed' -- it no longer reads 'created' with no txid. Read the "
               "row before running this again; the fee may be counted as swept")
        )
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SELF,
        description=("Report the fee this desk has retained per asset, and with --apply sweep it to the "
                     "configured destination. Report-only by default."),
        epilog=("There is no --amount: the figure is `accrued - swept`, both of which are queries, and a "
                "typed number would be a second authority for what the desk is owed. There is no "
                "--force: a sweep that would leave the wallet unable to fund an open swap's payout is "
                "refused, and the remedy is to fund the wallet or let those swaps settle. The fee RATE "
                "is untouched by this tool."),
    )
    parser.add_argument("--db", default="", help=f"database to read (default: {Config.DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="actually BROADCAST the sweeps. This moves funds and cannot be undone. "
                             "Without it nothing is signed, nothing is sent and no row is written, and "
                             "every check still runs.")
    parser.add_argument("--asset", default="",
                        help="sweep only this asset. Every asset is still REPORTED -- this narrows what "
                             "--apply sends, so one chain can be collected without touching another.")
    return parser


def _announce(args, db_path: str) -> None:
    """Everything that decides the answer, before anything is read (rule 14)."""
    print(f"{SELF}: {'APPLY -- transactions WILL BE BROADCAST and cannot be unsent' if args.apply else 'REPORT ONLY -- nothing is sent and no row is written'}",
          flush=True)
    print(labeled("database", f"{db_path}  <- {db_path_source(db_path, args.db)}. A fee earned in any "
                              f"other database is invisible to this run, and a sweep recorded in "
                              f"another one would not stop this one re-sending it"), flush=True)
    print(labeled("accrued from", "fee_ledger.py over `swaps` JOIN `payouts`, counting only a payout "
                                  "with status='broadcast' AND a txid. Money whose delivery is not "
                                  "established is excluded, so the accrual never counts a fee on a "
                                  "payout that may not have happened"), flush=True)
    print(labeled("swept from", "`fee_sweeps`, counting 'created' AND 'broadcast'. A sweep recorded and "
                                "never reported sent is counted as GONE, because over-counting costs an "
                                "uncollected fee and under-counting costs a double send"), flush=True)
    print(labeled("destination", f"{FEE_SWEEP_DESTINATION_TEMPLATE.format(asset='<ASSET>')} per chain, "
                                 f"with NO default. A chain whose variable is unset is refused by name"),
          flush=True)
    print(labeled("only", f"{args.asset or 'every asset with a retention'}  <- which asset(s) --apply "
                          f"may send for. All of them are reported either way"), flush=True)
    if args.apply:
        print("  --apply SIGNS AND BROADCASTS. GRC sends go through the wallet lock/unlock cycle "
              "payouts already use; the passphrase is read from the environment by that code and is "
              "never printed, logged or named here.", flush=True)
    else:
        print("  this run SIGNS NOTHING and BROADCASTS NOTHING, and writes no row in any table.",
              flush=True)


def stranded_names(stranded: list[dict]) -> str:
    """"id=4 1.25 GRC, id=7 0.5 LTC" for the alarm line, or "(none)".

    A FUNCTION BECAUSE THE JOIN CANNOT BE WRITTEN INLINE. Nesting a comprehension
    that subscripts `row["id"]` inside an f-string using the same quote character
    is a SyntaxError before Python 3.12, and this tree targets py312 in
    pyproject.toml while running on 3.11 here -- so the inline version passes ruff
    and fails to import, which is the worst of both. It is also the easier thing to
    assert on: the alarm's whole job is naming each stranded row, and a test can
    call this with seeded dicts.

    "(none)" IS UNREACHABLE FROM THE ONE CALLER, which only prints the alarm when
    the list is non-empty. It is here anyway because a blank string in the middle
    of a sentence about money possibly on chain is rule 14's ambiguous gap, and a
    future second caller should not have to know the guard exists.
    """
    return ", ".join(
        f"id={row['id']} {row['amount']} {row['asset']}" for row in stranded
    ) or "(none)"


def _report(planned, applied: list[str]) -> None:
    """Every asset under its verdict, and never a blank section (rule 14)."""
    for verdict, meaning in VERDICT_SECTIONS:
        chosen = [(plan, owed, total) for plan, owed, total in planned if plan.verdict == verdict]
        print(f"\n{verdict}  {len(chosen)} asset(s)  <- {meaning}", flush=True)
        if not chosen:
            print("  (none)", flush=True)
            continue
        for plan, owed, total in chosen:
            for line in describe(plan, owed, total):
                print(line, flush=True)
    if applied:
        print(flush=True)
        for line in applied:
            print(line, flush=True)


def _footer(args, db_path: str, *, sweepable: list, sent: int) -> list[str]:
    """What to do next, which is a different sentence in each state (rule 14).

    RETURNED RATHER THAN PRINTED so the states can be asserted on with seeded
    counts (rule 10), which is the shape correct_payout_amounts._footer() settled
    on after shipping a footer that offered a command to collect "the 0 row(s)
    above".

    FOUR STATES, and the one that matters is the third: an --apply run narrowed by
    --asset leaves other assets sweepable, and a footer that said nothing would
    make a partial collection look like a complete one.
    """
    lines: list[str] = []
    names = [plan.asset for plan in sweepable]
    if args.apply:
        if sent:
            lines.append(f"\n{sent} sweep(s) were BROADCAST. Each one is final. Read them back:\n"
                         f"    {root_tool_command(SELF)}")
        remaining = [name for name in names if args.asset and name != args.asset]
        if remaining:
            lines.append(f"\nSTILL SWEEPABLE and NOT sent, because --asset {args.asset} narrowed this "
                         f"run: {', '.join(remaining)}. Nothing about those assets changed.")
        if not sent and not remaining:
            lines.append("\nNOTHING WAS SENT. Every asset is in one of the sections above with the "
                         "reason beside it; --apply sends only for an asset under SWEEP.")
        return lines
    if names:
        lines.append(f"\nREPORT ONLY: nothing sent, nothing written. To sweep {', '.join(names)}:\n"
                     f"    {_rerun_command(args, db_path, '--apply')}\n"
                     f"  That BROADCASTS. Read each SWEEP line above first: it names the destination, "
                     f"the amount and the wallet the money leaves.")
    else:
        lines.append("\nREPORT ONLY, AND THERE IS NOTHING TO SWEEP: no asset is under SWEEP above, so "
                     "--apply would send nothing. No command is offered because there is no work to "
                     "run one on.")
    return lines


def _rerun_command(args, db_path: str, *extra: str) -> str:
    """This run again with `extra` added, echoing the flags that decided it (rule 14).

    --db appears only when it was given, because the default is already on the
    `database` line and a reader who did not pass it did not choose it. --asset
    appears whenever it was given, because leaving it out would offer a command
    that sweeps MORE assets than the run being described -- the exact defect
    correct_payout_amounts._rerun_command() records shipping and reading.
    """
    flags = []
    if args.asset:
        flags.append(f"--asset {args.asset}")
    if args.db:
        flags.append(f"--db {db_path}")
    return " ".join([root_tool_command(SELF), *extra, *flags])


def refusal_before_reading(args, db_path: str) -> str | None:
    """Why this run must not start at all, or None. Nothing has been read yet.

    A MISSING DATABASE IS REFUSED BEFORE sqlite3.connect(), WHICH CREATES ONE.
    show_fees.py and correct_payout_amounts.py guard the same way and both say
    why: a tool that connects to a mistyped path leaves an empty database behind
    and then reports "(none)", which is indistinguishable from a desk that has
    earned nothing. Here it would also mean an --apply run that swept nothing and
    said so cheerfully.

    AN --asset THAT IS NOT A CHAIN THIS TERMINAL KNOWS is refused rather than
    silently matching nothing, because "--asset GCR swept nothing" and "GRC has
    nothing to sweep" would otherwise read identically -- rule 14's "did nothing
    must not look like did work", reached by a typo.
    """
    if args.asset and args.asset not in Config.RPC:
        return (f"--asset {args.asset!r} is not a chain this terminal knows. The ones it does are "
                f"{', '.join(sorted(Config.RPC))}. Refusing rather than reporting that nothing matched, "
                f"which a typo and an empty ledger would say in the same words")
    if not Path(db_path).exists():
        return (f"{db_path} does not exist. NOT created -- connecting would make an empty database and "
                f"this run would report no fee for a file nothing uses. Check SWAP_DB_PATH, or pass "
                f"--db with the path the workers read")
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    db_path = args.db or str(Config.DB_PATH)
    config = get_config_dict()
    _announce(args, db_path)

    refusal = refusal_before_reading(args, db_path)
    if refusal:
        print(f"  REFUSED: {refusal}", file=sys.stderr)
        return 2

    adapters = build_adapters(Config.RPC)
    print(labeled("adapters", f"{', '.join(sorted(adapters)) or '(none)'}  <- the chains whose balance "
                              f"CAN be read in this process. An asset with a retention and no adapter "
                              f"is refused, because its headroom cannot be established"), flush=True)

    _accruals.clear()
    sent = 0
    with db_session(db_path) as db:
        totals = asset_totals(fee_rows(db))
        print(labeled("assets", f"{len(totals)}  <- destination assets with at least one delivered "
                                f"payout. Never pooled: adding coins of different value and calling the "
                                f"result revenue describes neither"), flush=True)
        stranded = unresolved_sweeps(db)
        if stranded:
            print(labeled("*** ALARM", f"{len(stranded)} fee_sweeps row(s) are 'created' with no txid: "
                                       f"{stranded_names(stranded)}. "
                                       f"Each is money POSSIBLY on chain that nothing recorded. They are "
                                       f"counted as SWEPT below so this run cannot re-send them; "
                                       f"resolving one means looking at the chain"), flush=True)
        if not totals:
            print("\n  (none)  <- no payout in this database has been broadcast with a txid, so no fee "
                  "has been realized anywhere. That is NOT a fee of zero: nothing has been delivered "
                  "to charge one on.", flush=True)
            print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
            return 0

        # ONE READ OF `fee_sweeps` FOR THE WHOLE RUN. See plan_for_asset().
        already_swept = swept_by_asset(db)
        planned = []
        for index, total in enumerate(totals, start=1):
            # Rule 14: a balance read can hang for a daemon's whole timeout, and an
            # operator watching a blinking cursor cannot tell that from a hung tool.
            print(f"  reading the {total.asset} wallet  {index}/{len(totals)}", flush=True)
            plan, owed = plan_for_asset(db, config, adapters, total, already_swept)
            planned.append((plan, owed, total))

        applied: list[str] = []
        if args.apply:
            for plan, _owed, _total in planned:
                if plan.verdict != SWEEP or (args.asset and plan.asset != args.asset):
                    continue
                applied.extend(apply_sweep(db, config, adapters, plan))
                sent += 1
        _report(planned, applied)

    sweepable = [plan for plan, _owed, _total in planned if plan.verdict == SWEEP]
    refusals = [plan for plan, _owed, _total in planned if plan.verdict == REFUSE]
    print(labeled("summary", _summary_line(db_path, planned=planned, sweepable=sweepable,
                                           refusals=refusals)), flush=True)
    for line in _footer(args, db_path, sweepable=sweepable, sent=sent):
        print(line, flush=True)
    print(labeled("done in", format_duration(time.monotonic() - started)), flush=True)
    # A refusal is not a failure of this run and it is not a success either: the
    # assets that could be reported were, and something was found that a person has
    # to decide or fix. Rule 13 -- "did nothing" and "did work" must not share an
    # exit code any more than they share a line.
    return 5 if refusals else 0


def _summary_line(db_path: str, *, planned: list, sweepable: list, refusals: list) -> str:
    """The one-line count, with its denominator (rule 3). `(none)` when there is none.

    A FUNCTION SO THE EMPTY CASE CAN BE ASSERTED ON, and because a breakdown of an
    empty set is not a result either -- the defect correct_payout_amounts.
    _summary_line() records shipping: `0 correctable of 23; 0 of those 0 verified`
    is arithmetically correct and reads like a broken tool.
    """
    if not sweepable:
        return (f"(none) sweepable of {len(planned)} asset(s) with a retention in {db_path}; "
                f"{len(refusals)} refused, so there is no amount to break down")
    amounts = ", ".join(f"{as_amount(plan.amount)} {plan.asset}" for plan in sweepable)
    return (f"{len(sweepable)} of {len(planned)} asset(s) sweepable in {db_path}: {amounts}"
            + (f"; {len(refusals)} refused" if refusals else "; (none) refused"))


if __name__ == "__main__":
    raise SystemExit(main())
