"""Move credited ICP deposits out of their per-swap subaccounts and into the desk's.

Role: submodule (one stage: sweep credited deposits into spendable inventory)
Reads: icp_deposit_subaccounts, icp_subaccount_sweeps, swaps; the ICP ledger
Writes: icp_subaccount_sweeps (append-only)
Can move funds: YES. Every sweep is a signed ledger `transfer`.
Mainnet-safe: no -- see the arming note below. Nothing moves unless this process
      holds ICP_PAYOUT_IDENTITY, and the operator decides whether it does.

=============================================================================
WHY THIS EXISTS: A CREDITED ICP DEPOSIT WAS NEVER SPENDABLE
=============================================================================

chains/icp.transfer_argument() hardcoded `from_subaccount = null`, so every send
debited the owner principal's DEFAULT subaccount, while deposits land at allocated
subaccounts with index >= 1. Detection worked -- find_deposits_to_address() scans
the ledger and the generic credit path records it -- and then the coins sat where
nothing could spend them. subaccount_balance() had no production caller at all.

MEASURED 2026-11-22, with the default account empty and the coins in subaccounts:

    why_the_payout_cannot_be_funded refuses = True
      "the ICP payout wallet holds 0.0 ICP spendable and this payout needs 10.0001
       ICP ... so it is short 10.0001"

That fires inside create_swap(), so an X -> ICP swap is REFUSED AT CREATION while
the desk holds the coins -- a halt and the safe direction, costing a retry. The
other direction is quieter: ICP -> X pays out on the far chain and receives ICP
that never becomes inventory and never funds a later payout.

RE-COUNTING WAS THE WRONG FIX and is worth naming because it is the obvious one.
The funding gate and the payout read the SAME get_balance(). Teaching it to sum
subaccounts would let a swap be created against coins the payout cannot spend,
turning a creation-time refusal into an InsufficientFunds after the deposit is
irreversible. The funds have to be moved.

=============================================================================
WHERE THIS RUNS, AND WHY NOT WHERE IT WAS FIRST SUGGESTED
=============================================================================

workers/payout_worker.py, before refresh_wallet_inventory(), so a deposit swept
this cycle is inventory in the same cycle.

IT WAS PROPOSED FOR THE RECONCILE WORKER and that worker's own header forbids it:
"Can move funds: no -- it calls neither sendtoaddress nor any signing method."
workers/deposit_watcher.py says the same. A sweep is a signed transfer, so putting
it in either would have made a module header false -- the wrong-comment defect
(rule 16) committed into the one field an operator reads to decide what a process
is allowed to do. The payout worker already says "Can move funds: YES. This is the
only process in the Flask suite that calls..." and is the only honest home.

=============================================================================
ARMING, AND WHAT IS STILL NOT ESTABLISHED
=============================================================================

NOTHING MOVES WITHOUT ICP_PAYOUT_IDENTITY. The sweep signs with the same identity
that signs ICP payouts, so a process not armed to pay ICP out is not armed to
sweep, and this returns a "not armed" summary rather than refusing loudly -- an
unarmed deposit-watching host is a normal deployment, not a fault.

NO ICP TRANSFER HAS EVER BEEN SIGNED OR BROADCAST BY THIS CODE, ON ANY NETWORK.
HANDOFF.md states it for the payout and it is equally true of this: there is no
dfx, no replica and no reachable ledger in the environment this was written in.
Every behavior below is measured against a seeded transport. The arithmetic, the
candid argument, the enumeration and the append-only record are proven; that a
real ledger accepts the transfer is not.

AND THE PRINCIPAL EQUALITY IS CHECKED PER SWEEP rather than assumed.
chains/icp_payout_identity.py:131 records that nothing verifies the armed
identity's principal equals ICP_OWNER_PRINCIPAL. For a payout that mismatch
surfaces as InsufficientFunds; for a sweep it would be quieter and worse -- a
transfer signed by a principal owning none of these subaccounts, moving nothing,
while the operator can see the funds in the ledger. ICPAdapter.
sweep_subaccount_to_desk() calls verify_derivation() first for that reason.
"""

from __future__ import annotations

import logging

from chains.icp_payout_identity import payout_identity_is_present
from services.helpers import utc_now_iso
from services.icp_subaccount_service import (
    record_subaccount_sweep,
    unswept_deposit_subaccounts,
)

logger = logging.getLogger(__name__)

#: What one cycle reports. A dict rather than a bare count, because rule 14 asks
#: that "did nothing" and "did work" not share a success line: `swept` and
#: `refused` are different facts and an operator needs to see which happened.
EMPTY_SUMMARY = {"armed": False, "candidates": 0, "swept": 0, "refused": 0, "e8s": 0}


def sweep_credited_deposits(db, config, adapters, should_stop=None) -> dict:
    """Sweep every credited ICP deposit subaccount into the desk's own account.

    Returns a summary dict; never raises for one bad subaccount.

    EACH SUBACCOUNT IS GUARDED INDIVIDUALLY, which is the shape
    services/deposit_service.process_active_swaps() was changed to on 2026-10-10
    after one unreachable chain took out a whole cycle. One subaccount whose
    derivation does not verify, or whose balance is below the ledger fee, must not
    stop the others: the others hold different customers' deposits.

    THE BROAD CATCH IS DELIBERATE AND REPORTS ITSELF (rule 12's BLE001). A sweep
    reaches a network, so the failure space is open -- but the caller CAN tell a
    failure from a real answer, because `refused` is counted separately from
    `swept` and every refusal is logged with its reason. What must never happen is
    a cycle that refuses silently and prints a success line.
    """
    if not payout_identity_is_present():
        # NOT AN ERROR. A host that watches ICP deposits without being armed to pay
        # them out is an ordinary deployment -- it is the posture
        # tests/test_optional_dependency_posture.py pins for the signing libraries
        # one layer down -- so this reports "not armed" and moves on.
        return dict(EMPTY_SUMMARY)

    adapter = (adapters or {}).get("ICP")
    if adapter is None:
        return dict(EMPTY_SUMMARY)

    owner = str((config or {}).get("ICP_OWNER_PRINCIPAL") or getattr(adapter, "owner_principal", "") or "")
    if not owner:
        logger.warning("ICP sweep skipped: no owner principal in config or on the adapter")
        return dict(EMPTY_SUMMARY)

    candidates = unswept_deposit_subaccounts(db, owner)
    summary = {"armed": True, "candidates": len(candidates), "swept": 0, "refused": 0, "e8s": 0}

    for row in candidates:
        if should_stop is not None and should_stop():
            # A STOP LANDS BETWEEN SUBACCOUNTS, never inside one. The transfer and
            # its record are not one atomic step, so stopping mid-sweep is the one
            # ordering this loop must not allow -- tests/test_a_stop_lands_between_
            # swaps.py makes the same argument for the payout loop.
            break
        index, swap_id = row["subaccount_index"], row["swap_id"]
        try:
            amount_e8s, fee_e8s, block_index = adapter.sweep_subaccount_to_desk(index)
        except Exception as exc:  # noqa: BLE001 -- see the docstring: counted as `refused`, logged with its reason, and never reported as success
            summary["refused"] += 1
            logger.warning("ICP sweep refused for subaccount %s (swap %s): %s", index, swap_id, exc)
            continue
        record_subaccount_sweep(
            db,
            owner=owner,
            subaccount_index=index,
            swap_id=swap_id,
            amount_e8s=amount_e8s,
            fee_e8s=fee_e8s,
            block_index=block_index,
            swept_at=utc_now_iso(),
        )
        summary["swept"] += 1
        summary["e8s"] += amount_e8s
        logger.info(
            "ICP sweep: subaccount %s (swap %s) -> desk, %s e8s, fee %s e8s, block %s",
            index, swap_id, amount_e8s, fee_e8s, block_index,
        )
    return summary


# _sweep_one() WAS HERE AND IS GONE, which is worth a line because its absence is
# the better design rather than a deletion. It called three accessors --
# ledger_fee_e8s(), subaccount_balance_e8s() and sweep_created_at_nanos() -- that
# this module had invented for itself and the adapter did not have. Writing them
# would have meant asking the ledger for the balance and the fee a SECOND time, after
# the sweep, to record what the sweep carried: two reads of a number that can change
# between them, with the audit row taking the later one. ICPAdapter.
# sweep_subaccount_to_desk() now returns (amount_e8s, fee_e8s, block_index) -- what
# it actually moved -- so the row records the transfer rather than a re-measurement
# of it, and the idempotency-key policy stays in the one place that knows the
# ledger's 24-hour window.


def sweep_note(summary: dict) -> str:
    """One line for the payout worker's cycle, or "" when there was nothing to say.

    Rule 14: a cycle that swept a customer's deposit into the hot wallet must not
    look like a cycle that did nothing, and a cycle that REFUSED must not look like
    either. "" only when this process is unarmed or had no candidates -- the two
    cases where silence is the truth.
    """
    if not summary.get("armed") or not summary.get("candidates"):
        return ""
    if summary.get("refused") and not summary.get("swept"):
        return (
            f"ICP SWEEP REFUSED {summary['refused']}/{summary['candidates']} subaccount(s) and moved "
            f"nothing -- those deposits are NOT spendable inventory yet; the reason is logged per "
            f"subaccount"
        )
    parts = [f"ICP swept {summary['swept']}/{summary['candidates']} subaccount(s), {summary['e8s']} e8s into the desk's own account"]
    if summary.get("refused"):
        parts.append(f"{summary['refused']} REFUSED and still unswept")
    return " -- ".join(parts)
