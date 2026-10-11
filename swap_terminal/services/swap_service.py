"""Create a swap from a quote, and read one back.

Role: submodule -> function (create_swap is the decision)
Reads: swap_terminal.db (quotes, swaps, deposit_events, payouts), the
       destination adapter (validateaddress) and the source adapter
       (getnewaddress)
Writes: swap_terminal.db (swaps, swap_audit_log)
Can move funds: no broadcast. It DERIVES a deposit address in the hot wallet
       and fixes the payout address, and it sets min_confirmations from
       config -- the threshold that later decides when a payout is released.
       Since 2026-09-27 it also REFUSES both of those addresses when they
       cannot receive money, which is the cheapest moment either can be
       stopped: nothing has been written and nothing has been taken. See
       _refuse_unusable_deposit_address() below and the payout check inside
       create_swap().
Mainnet-safe: yes

set_swap_status() writes the status and the audit row together, so every
transition is recorded. It does NOT commit: the caller owns the transaction
boundary, which is what lets create_swap() insert the swap and its first audit
row atomically. Since 2026-10-02 it is a COMPARE-AND-SWAP and returns whether the
swap moved -- a caller that ignores that return value is back to the defect, which
is written out at the function.
"""

import logging
import os

from chains.gridcoin_wallet_lock import (
    WALLET_UNLOCK_ASSETS,
    WALLET_UNLOCK_ENV_VAR,
    needs_wallet_unlock,
    unlocked_for_payout,
)
from chains.registry import (
    unconfigured_chains,
    validate_min_confirmations,
    why_cannot_pay_out,
    why_unconfigured,
)
from modules.address_authority import check_address, check_receive_address, expected_network

from .custody_separation import payout_to_the_desk_refusal
from .helpers import new_id, parse_iso, utc_now_iso
from .icp_subaccount_service import allocate_subaccount_index
from .payout_capacity import why_the_payout_cannot_be_funded
from .xrp_tag_service import allocate_destination_tag

# No handler and no setLevel: a library module that configures logging decides policy for
# every program that imports it, which is the import-time side effect rule 12 names. The
# three atomic_*_client.py files carry the same note for the same reason.
logger = logging.getLogger(__name__)


def get_min_confirmations(config, asset: str) -> int:
    """The confirmation threshold that goes onto a swap row, validated for the asset.

    ONE CHOKEPOINT, AND THAT IS WHY THE VALIDATION IS HERE. Every asset's threshold
    passes through this function on its way onto `swaps.min_confirmations`, so a chain
    whose adapter never looks at the value is still covered -- which is the case ICP was
    in. chains/registry.validate_min_confirmations() has the measurement: with
    ICP_MIN_CONFIRMATIONS=2 a swap sat in `confirming` through forty watcher cycles with
    its deposit recorded, its amount correct, credited_at NULL, and nothing halted,
    logged or reported it.

    IT RAISES AT SWAP CREATION, which is the point: before the customer has been handed a
    deposit address, rather than after they have paid into one.

    XRP and SOL are not covered by this and do not need to be -- their adapters refuse a
    bad threshold in their constructors, which fires earlier and harder (no adapter is
    built at all). The difference is written down at both sites per rule 8; see
    FINAL_AT_ONE_CONFIRMATION.
    """
    return validate_min_confirmations(asset, config[f"{asset}_MIN_CONFIRMATIONS"])


def set_swap_status(
    db, swap_id: str, new_status: str, message: str | None = None, old_status: str | None = None
) -> bool:
    """Move one swap from the status it is IN to `new_status`, or decline. Returns whether it moved.

    COMPARE-AND-SWAP, AND IT WAS A BARE UPDATE UNTIL NOW. `old_status` existed and was used
    for ONE thing -- filling in the audit row -- while the write was
    `UPDATE swaps SET status = ? WHERE id = ?`. So a worker holding a stale read still wrote,
    and still wrote an audit row describing a transition out of a status the swap had already
    left.

    MEASURED ON THE OPERATOR'S HOST, FROM THEIR OWN AUDIT TRAIL. The identical two
    transitions, recorded twice, 0.83s apart, because deposit_watcher (15s) and
    reconcile_worker (60s) both ran process_active_swaps() over that swap:

        21:37:46.943103  s_95a807c181644190  confirming -> payout_pending  Deposit fully confirmed
        21:37:46.942952  s_95a807c181644190  awaiting_deposit -> confirming  Deposit detected
        21:37:46.113918  s_95a807c181644190  confirming -> payout_pending  Deposit fully confirmed
        21:37:46.113783  s_95a807c181644190  awaiting_deposit -> confirming  Deposit detected

    Exactly one payout row exists for that swap, so nobody was paid twice -- the payout claim
    guard held, and it held because claim_swap_for_payout() below in payout_service.py already
    does what this function did not. The damage was a false audit trail, which is the record an
    operator reads to understand what happened to somebody's money.

    THE WORSE CASE THE SAME DEFECT ALLOWED, and it is why this is a money fix rather than a
    bookkeeping one: a stale `awaiting_deposit` read could overwrite `payout_pending` with
    `deposit_seen`, or overwrite `paying` -- the status payout_worker sets to own a send --
    with a deposit status. Nothing read the UPDATE's rowcount, so no caller could tell.

    `old_status=None` MEANS "WHATEVER IT IS NOW", READ AND THEN COMPARED-AND-SWAPPED. It does
    NOT mean "skip the check", and that choice is deliberate: an argument that switches the
    guarantee off is two behaviors behind one name (rule 8), and the one call that forgets it
    is the one that clobbers. Read-then-CAS is strictly better than the bare UPDATE it
    replaces even though the read can go stale -- the write is still conditional, so another
    process that moves the row between the read and the UPDATE makes this one decline instead
    of overwrite. Every non-test caller in the tree passes `old_status` explicitly (grepped
    2026-10-02: four in deposit_service, three in payout_service), so the None path is reached
    only by a swap row that does not exist -- where `current` is None, the CAS matches nothing,
    and this returns False rather than writing an audit row for a swap that is not there.

    A LOSING CAS IS NOT AN ERROR AND MUST NOT RAISE. refresh_swap_from_chain() is called
    inside a list comprehension in process_active_swaps(); one exception there kills every
    other swap's processing in the same cycle, and the worker's `except Exception` turns that
    into a FAILED cycle on which nothing is credited on any chain. With two workers a lost
    race is the NORMAL case, so it is a quiet, VISIBLE no-op: False to the caller, one INFO
    line naming the swap and both statuses, and nothing written.

    THE SAME SHAPE AS payout_service.claim_swap_for_payout(), AND THE DIFFERENCE IS THE POINT
    (rule 8, stated at both sites). That function is also a conditional UPDATE whose audit row
    is written only on rowcount == 1, and its docstring carries the measurement that argues
    for moving a decision into the write. It differs in two ways and they are why it is not
    merged into this one: it COMMITS, because a claim held in an open transaction blocks the
    other worker's competing UPDATE for the length of an RPC call, and its return value is a
    claim of ownership that the caller must act on, not a report that a status moved.

    NO TRIGGER, AND THAT IS CONSIDERED RATHER THAN SKIPPED. db.py's
    `address_proofs_are_single_use` is the model: the conditional UPDATE is the mechanism and
    the trigger is the guarantee against a future writer who drops the predicate (see
    grc_login_service.py's note -- "the mechanism and the guarantee are separate on purpose").
    A trigger here would have to RAISE(ABORT) on a transition, and the only transitions it
    could forbid without enumerating every legitimate one are exactly the ones a LOST RACE
    produces -- so it would turn the normal case into an exception in that list comprehension.
    It would also abort the hand-written `UPDATE swaps SET status=...` that show_swap.py tells
    the operator to run. Enumerating which transitions the database should refuse is a live
    posture decision and it is theirs (rule 16), not a side effect of this fix.

    Returns:
        True when this call moved the swap and wrote the audit row; False when the swap was
        not in `old_status` (or does not exist) and NOTHING was written.
    """
    current = old_status
    if current is None:
        row = db.execute("SELECT status FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        current = row["status"] if row else None
    moved = db.execute(
        # `AND status IS ?` and not `AND status = ?`: `current` is None for a swap row that
        # does not exist, and SQL equality against NULL is never true -- which is the answer
        # we want, but `IS` says so for the right reason rather than by accident, and it keeps
        # the one case where the column itself could be NULL from reading as a match.
        "UPDATE swaps SET status = ?, updated_at = ? WHERE id = ? AND status IS ?",
        (new_status, utc_now_iso(), swap_id, current),
    ).rowcount == 1
    if not moved:
        # RULE 14: "DID NOTHING" MUST NOT LOOK LIKE "DID WORK". The caller gets False and an
        # operator gets a line, because the silent version of this is what produced the
        # duplicated trail above -- nobody could see it until they read the audit table.
        #
        # INFO AND NOT WARNING, with the reason in the text: with two workers on two
        # schedules this is the expected outcome of the loser, so a WARNING here would train
        # the operator to ignore warnings. It is not a flood either -- set_swap_status() is
        # called at TRANSITIONS, a handful per swap lifetime, not once per cycle.
        logger.info(
            "swap %s was NOT moved to %r and NOTHING was written: it is no longer %r, so "
            "another process has already advanced it. This is the normal outcome for the "
            "loser of a status race between deposit_watcher and reconcile_worker, not an "
            "error -- the winner wrote the transition and its audit row. message=%r",
            swap_id, new_status, current, message,
        )
        return False
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, current, new_status, message, utc_now_iso()),
    )
    return True


def get_quote_or_raise(db, quote_id: str) -> dict:
    quote = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        raise ValueError("Quote not found")
    if parse_iso(quote["expires_at"]) <= parse_iso(utc_now_iso()):
        raise ValueError("Quote expired")
    return quote


# The chains whose deposits are told apart by a TAG on one shared account rather
# than by a per-swap address. A set of one today, named rather than written as
# `if from_asset == "XRP"` so the concept is greppable and a second such chain
# (Stellar's memo, Cosmos's memo, several exchange-style deposit models) is one
# entry rather than a second branch to find. Rule 11: one vocabulary, in one place.
TAG_ATTRIBUTED_ASSETS = frozenset({"XRP", "SOL"})

#: WHICH SHARED ACCOUNT each tag-attributed chain pays into, and what the discriminator is
#: CALLED on that chain. Derived from the asset rather than spelled at the use site, which is
#: what adding SOL on 2026-09-29 forced: `deposit_address_for()` read XRP_DEPOSIT_ACCOUNT from
#: inside the branch and every refusal it raised said "DestinationTag" -- so a SOL swap would
#: have been refused for the absence of an XRP variable, in a sentence naming a field the
#: Solana blockchain does not have. Rule 11: one vocabulary, in one place, meaning the same
#: thing for every asset that uses it.
#:
#: THE DISCRIMINATOR NAME IS FOR HUMANS ONLY. Both chains carry the integer in the event's
#: `vout` -- see services/deposit_service.attributable_events() -- and what differs is what the
#: chain's own documentation calls it, which is what an operator will search for.
#: THE NETWORK NAME IS CARRIED, not derived by appending a word to the ticker. Generalizing
#: this on 2026-09-29 first produced "not a valid XRP account" where the message had said "XRP
#: LEDGER account" -- a real loss of precision caught by a test that pinned the wording, and
#: the wording was right: an operator searching for why their account was refused searches the
#: network's name, not the ticker's.
TAG_ATTRIBUTION = {
    "XRP": ("XRP_DEPOSIT_ACCOUNT", "DestinationTag", "XRP Ledger"),
    "SOL": ("SOL_DEPOSIT_ACCOUNT", "Memo instruction", "Solana"),
}

# THE COLUMN that holds the tag, named once so no reader can spell it differently.
#
# It is `deposit_tag` and NOT `destination_tag`, and the difference is deliberate
# rather than an accident -- so it is named at BOTH sites, per rule 8's "if they
# genuinely differ, the difference is the point and belongs in a comment at both,
# naming the other one".
#
#   deposit_tag       the SCHEMA's name. Generic, because the column is the
#                     integer discriminator for any tag-attributed chain -- a
#                     Stellar or Cosmos memo would live in the same column, and
#                     `destination_tag` would then be a lie about two of the three.
#   destination tag   the XRP LEDGER's own term, and what a customer sees on the
#                     page. services/swap_view.py renders it under that name
#                     because that is what their wallet's field is labeled.
#
# That split cost a real defect on 2026-09-26: swap_view.py read
# swap["destination_tag"], a key that does not exist, so a swap WITH a tag
# rendered "NO DESTINATION TAG HAS BEEN ISSUED" and told the customer not to send
# anything. It failed safe -- it refused to show a send target rather than showing
# a wrong one -- but the swap was unusable. The agent that wrote the page flagged
# the dependency and named the one .get() to change if the column were called
# something else. It was. This constant is so the next one cannot drift.
DEPOSIT_TAG_COLUMN = "deposit_tag"


def why_cannot_take_deposits(config, adapters: dict, asset: str) -> str:
    """Why this chain cannot be a deposit SOURCE, or "" if it can. The mirror of
    chains/registry.why_cannot_pay_out(), which asks the same question about the other end.

    WHAT THIS CLOSES, MEASURED 2026-10-01 rather than supposed. services/pair_view.py decides a
    pair is `enabled` when both chains are reachable AND the TO asset can pay out. It never
    asked whether the FROM asset can produce a deposit address, so with both shared accounts
    unset the dropdown offered FOUR pairs that cannot create a swap:

        SOL->GRC   SOL_DEPOSIT_ACCOUNT is not set
        XRP->BTC   XRP_DEPOSIT_ACCOUNT is not set
        XRP->GRC   XRP_DEPOSIT_ACCOUNT is not set
        XRP->LTC   XRP_DEPOSIT_ACCOUNT is not set

    Three of those predate the SOL work. The customer picks the pair, is quoted, accepts, and
    gets a refusal where the deposit address should be. That is the same defect
    why_cannot_pay_out()'s docstring records ("badged ENABLED ... a customer would have sent
    GRC, had it credited, and been left with a swap in `failed`") one door over -- offered and
    not completable, failing at the start of the flow instead of the end. Cheaper than that
    one, because nothing has been deposited yet, and still the first thing a customer hits.

    IT CALLS deposit_account() RATHER THAN RE-DERIVING THE RULE (rule 8). That function already
    knows every way a deposit address can be unavailable -- unset variable, invalid account,
    a network mismatch -- and a second copy here would agree today and drift.

    ADDRESS-ATTRIBUTED CHAINS RETURN "" WITHOUT BEING PROBED, and that is not laziness: for
    BTC, LTC and GRC the test deposit_account() applies is get_new_address(), which DERIVES A
    REAL ADDRESS from the wallet. Calling it to answer a question for a page would burn a fresh
    address on every page load. Their reachability is what can go wrong and
    chains/registry.unconfigured_chains() already reports that.

    THE SENTENCE IS SHORTER THAN deposit_account()'S, deliberately, and the difference is
    stated at both sites per rule 8: this one goes in a badge beside a greyed-out pair, where
    the reader needs the variable's name; that one is the API refusal a caller gets back, where
    the reader needs to be told no swap was created and why that is the intended failure.
    """
    if asset not in TAG_ATTRIBUTED_ASSETS:
        return ""
    if asset not in adapters:
        # UNREACHABLE IS A DIFFERENT QUESTION, and unconfigured_chains() answers it. Probing
        # here would raise KeyError on adapters[asset] inside deposit_account(), and two
        # reasons for one pair leaves the operator to work out which to act on -- the same
        # reasoning why_cannot_pay_out() gives for returning "" when there is no adapter.
        return ""
    variable, _discriminator, _network = TAG_ATTRIBUTION[asset]
    try:
        # swap_id="" because this is a PROBE: the tag-attributed branch reads config and
        # validates the account and allocates nothing, so no tag is burned. open_swap.py:568
        # already probes it this way.
        deposit_account(config, adapters, asset, "")
    except ValueError:
        return f"{asset} cannot take deposits: {variable} is unset or not a valid account"
    return ""


#: Assets whose deposit address cannot be known until a row exists in the database.
#: A THIRD SHAPE beside the two deposit_account() describes, and the reason is
#: ordering rather than custody:
#:
#:   by address   BTC, LTC, GRC. get_new_address() derives it from the wallet, so it
#:                is known before anything is written.
#:   by tag       XRP, SOL. The account is configuration and known up front; the tag
#:                is allocated after the swap row exists, because it has a FOREIGN KEY
#:                to it.
#:   db-allocated ICP. The address IS a function of an allocated index --
#:                account_identifier(owner, subaccount) -- and icp_deposit_subaccounts
#:                has the same FOREIGN KEY to swaps. So the index cannot be allocated
#:                before the swap row, and the address cannot be computed before the
#:                index. The swap is inserted with a placeholder and updated inside the
#:                SAME transaction.
#:
#: WHY NOT DERIVE THE INDEX FROM THE SWAP ID and skip the ordering problem: because
#: uniqueness would then rest on a hash not colliding rather than on a constraint, and
#: two swaps sharing an index share a deposit address -- one customer's payment
#: credited to another's swap. services/icp_subaccount_service's header argues this at
#: length; this is the consequence.
DB_ALLOCATED_DEPOSIT_ASSETS = frozenset({"ICP"})

#: What sits in deposit_address between the INSERT and the UPDATE. Chosen so that it
#: is NOT address-shaped: chains/icp.validate_address() rejects it, so even if a
#: future edit let it escape the transaction, nothing would send to it and nothing
#: would publish it as payable. An empty string would satisfy NOT NULL and look like
#: a missing value, which is the ambiguity rule 14 is about.
DEPOSIT_ADDRESS_PENDING_ALLOCATION = "PENDING_SUBACCOUNT_ALLOCATION"


def allocate_db_deposit_address(db, adapters: dict, from_asset: str, swap_id: str) -> str:
    """Allocate the index and return the deposit address for a db-allocated asset.

    THE DECISION, extracted so it can be called with seeded inputs rather than only
    through a swap creation that needs a quote and two live adapters (rule 10).

    Allocation and derivation are ONE step on purpose. Two callers doing them
    separately is how an index gets allocated and then an address derived from a
    different one, which is silent: both halves look right and the customer's payment
    lands where nobody is watching.

    DOES NOT COMMIT, and must be called inside the transaction that wrote the swap
    row -- the subaccount table has a FOREIGN KEY to swaps, so the row must exist, and
    the swap must not be visible with its placeholder address if the allocation fails.
    """
    if from_asset not in DB_ALLOCATED_DEPOSIT_ASSETS:
        raise ValueError(
            f"{from_asset} is not a db-allocated deposit asset, so this function must not be "
            f"called for it. The set is {sorted(DB_ALLOCATED_DEPOSIT_ASSETS)}"
        )
    adapter = adapters[from_asset]
    index = allocate_subaccount_index(db, adapter.owner_principal, swap_id)
    address = adapter.deposit_address(index)
    if not adapter.validate_address(address):
        raise ValueError(
            f"the address derived for {from_asset} swap {swap_id} at subaccount {index} does not "
            f"validate ({address!r}). NO swap may be created: a customer handed an address the "
            f"adapter itself rejects would pay somewhere nothing is watching."
        )
    return address


def deposit_account(config, adapters: dict, from_asset: str, swap_id: str) -> tuple[str, bool]:
    """Where a customer sends the deposit. Returns (address, needs_tag). Writes nothing.

    THE DECISION, extracted so it can be called with seeded inputs and asserted on
    directly (rule 10) rather than only through a swap creation that needs a quote,
    a payout address and two live adapters.

    Two shapes, and the difference is not cosmetic:

      by address   BTC, LTC, GRC. A fresh address per swap, so the ADDRESS is the
                   identity and the tag is None. get_new_address() derives it.
      by tag       XRP. One shared account for every swap, told apart by an
                   integer DestinationTag. The account is custody configuration,
                   the tag is allocated from the database, and BOTH halves are
                   mandatory -- the account alone is not an instruction, because
                   every XRP swap has the same one.

    Why XRP does not simply implement get_new_address(): it CANNOT, and the
    adapter refuses on purpose rather than returning something address-shaped.
    Deriving a fresh XRP account per swap would mean funding each one past the
    base reserve (1 XRP measured on testnet 2026-09-26) and holding a key for it,
    to solve a problem the ledger already solved with an integer. Every exchange
    on this ledger uses tags.

    The refusal when XRP_DEPOSIT_ACCOUNT is unset is deliberate and is the whole
    reason this is a decision rather than a lookup: the alternative is a swap
    created with a deposit instruction pointing at nothing, which the customer
    then pays. A swap that fails to be created costs a retry; a swap that takes a
    deposit it cannot see costs the deposit.
    """
    if from_asset not in TAG_ATTRIBUTED_ASSETS:
        derived = derive_deposit_address(adapters[from_asset], from_asset, swap_id)
        _refuse_unusable_deposit_address(config, from_asset, derived, "the wallet's own get_new_address()")
        return derived, False

    variable, discriminator, network = TAG_ATTRIBUTION[from_asset]
    account = (config.get(variable) or "").strip()
    if not account:
        raise ValueError(
            f"{from_asset} deposits are attributed by {discriminator} on one shared account, and "
            f"{variable} is not set, so there is no account to pay into. NO SWAP WAS "
            f"CREATED -- which is the intended failure: a swap created now would hand a customer a "
            f"deposit instruction this terminal cannot receive against. Set {variable} to "
            f"an account you hold the key for; it is a custody decision and has no default."
        )
    if not adapters[from_asset].validate_address(account):
        raise ValueError(
            f"{variable} ({account}) is not a valid {network} account, so no swap was "
            f"created. Checked BEFORE allocating a tag: a tag is never reused, so allocating one "
            f"against a bad account would burn it permanently for a swap that cannot exist."
        )
    # AND THE SAME LOCAL DECODE THE ADDRESS CHAINS GET, one line below the adapter's own
    # check rather than instead of it. The adapter is XRPAdapter, whose validate_address()
    # accepts ANY X-address without verifying its checksum -- its own docstring carries that
    # review finding -- so on this one chain the adapter's yes is the weaker of the two
    # answers. Keeping both is not duplication (rule 8): they answer different questions and
    # the difference is named here and at chains/xrp.py.
    _refuse_unusable_deposit_address(config, from_asset, account, variable)

    # The tag is NOT allocated here, and the split is not stylistic. It is a
    # WRITE with a FOREIGN KEY into swaps(id), so it cannot run until the swap row
    # exists -- measured 2026-09-26, allocating first raises XRPTagAllocationError
    # ("there is no swap row with id ..., so no tag was allocated and NOTHING was
    # written"). An earlier version of this function allocated here and carried a
    # comment claiming create_swap() inserted first. It did not. The comment was
    # false the moment it was written, which is the defect rule 16 calls a bug:
    # a reader would have trusted it instead of reading the order.
    #
    # So this function stays a pure read that can be tested without a database,
    # and create_swap() allocates after the INSERT, inside the same transaction.
    return account, True


# ASSETS WHOSE PAYOUT IS DEBITED FROM THE ONE SHARED ACCOUNT CUSTOMERS DEPOSIT INTO,
# rather than from a wallet a local daemon owns and picks inputs from.
#
# BTC, LTC and GRC are not here and must not be: their adapter calls
# `sendtoaddress` and the DAEMON chooses which coins to spend, so there is no source
# address for this process to name. XRP has no daemon of ours and no wallet -- a
# Payment names its own `Account` field, and that field is what gets debited -- so
# the source is a value this process has to supply and therefore a value it has to
# be configured with.
#
# SOL IS DELIBERATELY ABSENT, and not by oversight. Solana's payout path is its own
# change, and whether a SOL payout needs a source named here is that change's
# question to answer -- this set is read by services/payout_service.broadcast_payout()
# to decide which keywords a send gets, so a row added speculatively would start
# passing a source to an adapter that may not take one. Deliberately NOT written as a
# claim about chains/solana.py's current state either: a comment that says what
# another in-flight file does today is stale by the time it is read, which is rule
# 16's wrong comment with a short fuse. Add SOL in the change that gives SOL a
# signing path, not before.
SHARED_ACCOUNT_PAYOUT_ASSETS = frozenset({"XRP"})


def payout_source_account(config, asset: str) -> str:
    """The account an `asset` payout DEBITS. Reads config, writes nothing, touches no network.

    THE MIRROR OF deposit_account() ABOVE, and it lives beside it so that the one
    question "which account does this chain use" has one answer in one file (rule 8).
    It reads the SAME variable through the SAME table: TAG_ATTRIBUTION[asset][0]. A
    second spelling of "XRP_DEPOSIT_ACCOUNT" is what rule 8 calls a bug with a delay
    on it, and this one would be the worst kind -- the deposit side and the payout
    side quietly pointing at two different accounts, with deposits arriving in one
    and payouts debiting another, each file looking correct on its own.

    ONE ACCOUNT FOR BOTH DIRECTIONS IS THE DESIGN AND IS WORTH SAYING OUT LOUD. An
    XRP swap's customer deposit lands in XRP_DEPOSIT_ACCOUNT, and an XRP payout to
    some other customer is debited from it. That is exactly what the single Gridcoin
    wallet already does -- deposits in, payouts out, one balance -- and it is why
    there is no second variable to configure. It is also why the reserve check in
    chains/xrp_signing.require_reserve_headroom() matters: the account funds payouts
    and must stay above its reserve, and that figure comes from a live account_info
    read rather than from anything cached.

    RAISES ValueError FOR AN UNSET OR UNUSABLE ACCOUNT, with the variable named, and
    returns only a value that passed a local decode. Raising rather than returning ""
    is the same choice deposit_account() makes and for the same reason: an empty
    string here would flow into a Payment's `Account` field and produce an
    account_info error several layers from the cause.

    WHY A LOCAL DECODE AND NOT THE ADAPTER'S validate_address(). On this one chain
    the adapter's yes is the weaker answer -- XRPAdapter.validate_address() accepts
    ANY X-address without verifying its checksum, which is the review finding
    chains/xrp.py records -- and an X-address is specifically wrong here: it packs a
    destination tag into the string, and a Payment's `Account` field takes a classic
    address. So this uses modules/address_authority, the same authority the payout
    worker's burn guard uses on the destination.

    AN ASSET NOT IN SHARED_ACCOUNT_PAYOUT_ASSETS RETURNS "" rather than raising. Its
    payout has no source for this process to name, the adapter's daemon picks the
    inputs, and services/payout_service.broadcast_payout() passes no source keyword
    at all for it.
    """
    if asset not in SHARED_ACCOUNT_PAYOUT_ASSETS:
        return ""
    variable, _discriminator, network = TAG_ATTRIBUTION[asset]
    account = (config.get(variable) or "").strip()
    if not account:
        raise ValueError(
            f"{asset} payouts are debited from one shared account and {variable} is not set, so there "
            f"is no account to pay FROM. It is the same account {asset} deposits are paid INTO -- one "
            f"balance, both directions, exactly as the single Gridcoin wallet works -- and it is a "
            f"custody decision with no default. Set {variable} to the account whose seed is in "
            f"XRP_PAYOUT_SECRET_SEED; a mismatch between the two is refused before signing by "
            f"chains/xrp_signing.derive_and_check()."
        )
    verdict = check_address(asset, account)
    if verdict.refuses:
        raise ValueError(
            f"{variable} ({account}) cannot be a {network} payout source -- {verdict.why}. Refused "
            f"locally, with no daemon asked and nothing written. A Payment's `Account` field must be a "
            f"CLASSIC address: an X-address packs a destination tag into the string and belongs on the "
            f"destination side, not here."
        )
    return account


def derive_deposit_address(adapter, asset: str, swap_id: str) -> str:
    """getnewaddress, and if the wallet is LOCKED, do the lock cycle and try once more.

    OPERATOR INSTRUCTION 2026-10-03: "on our end the terminal it will have to be
    able to lock-unlock-lock-return to staking unlock our OWN hot wallet."

    THAT CYCLE ALREADY EXISTED AND ONLY WRAPPED THE SEND.
    chains/gridcoin_wallet_lock.unlocked_for_payout() does exactly
    lock -> unlock past staking -> body -> lock -> unlock for staking, with the
    restore in a `finally` so it runs even when the body raises. But
    services/payout_service.py is its only caller, so it covered the one wallet
    write that pays a customer and not the other one.

    DERIVING AN ADDRESS IS ALSO A WALLET WRITE. On a Bitcoin-derived daemon
    `getnewaddress` succeeds while the keypool holds spare keys and fails with
    WALLET_UNLOCK_NEEDED (rpc -13) once it must top the pool up. So an encrypted
    wallet unlocked only for staking creates swaps fine until the keypool empties,
    and then stops -- with a customer on the page.

    NOT MEASURED AGAINST A GRIDCOIN DAEMON FROM HERE, and said rather than implied:
    this container has no Gridcoin node. It is the documented behavior of the family
    Gridcoin forked, and the operator's own wallet has not hit it yet because their
    keypool is not exhausted.

    TRY FIRST, UNLOCK ONLY IF THE WALLET SAYS SO. The alternative -- unlock on every
    swap creation -- would hold the hot wallet open for sending on a path that only
    needs to read a key out of a pool, and would do it for every customer who loads
    the form. The cost of this order is one failed RPC before the retry; the cost of
    the other is a wallet unlocked for no reason, repeatedly.

    A FAILURE THAT IS NOT A LOCK IS RE-RAISED UNCHANGED. needs_wallet_unlock() is
    deliberately narrow: a refused connection, a bad label and a malformed response
    must stay failures, because only the lock is something this process can fix and
    then re-attempt. Rule 12's BLE001 note is the reason this is not `except
    Exception: retry`.

    AND IF NO PASSPHRASE IS SET, THE ORIGINAL ERROR IS RAISED, not a complaint about
    the variable. The operator needs to see what the daemon said; the missing
    variable is named in the message this adds to it.
    """
    label = f"swap_{swap_id}"
    try:
        return adapter.get_new_address(label)
    except Exception as error:
        if asset not in WALLET_UNLOCK_ASSETS or not needs_wallet_unlock(error):
            raise
        passphrase = os.environ.get(WALLET_UNLOCK_ENV_VAR, "")
        if not passphrase:
            raise type(error)(
                f"{error}  <- the wallet is locked and {WALLET_UNLOCK_ENV_VAR} is not set in this "
                f"process, so this swap cannot derive a deposit address. getnewaddress needs the "
                f"wallet unlocked once the keypool is exhausted; a staking-only unlock is not enough"
            ) from error
        logger.info(
            "swap %s: %s getnewaddress needs an unlocked wallet, doing lock -> unlock -> derive -> "
            "lock -> unlock for staking", swap_id, asset,
        )
        with unlocked_for_payout(adapter, passphrase):
            return adapter.get_new_address(label)


def _refuse_unusable_deposit_address(config, asset: str, address: str, source: str) -> None:
    """Raise unless `address` can actually receive `asset` on the network we believe we are on.

    THE RECEIVE PATH IS NOT THE SEND PATH, AND THIS IS THE DIFFERENCE. Operator, 2026-09-27:
    "make the receive path burn proof too."

    On the payout side a bad address burns OUR fee or a customer's payout, and the address
    came from a stranger typing it. Here we HAND A CUSTOMER an address and they pay into it
    with their own money, so the loss is THEIRS and they cannot detect it before paying. It
    is the worse of the two failures, which is why this one REFUSES where
    services/payout_service.py's guard is careful not to.

    WHAT CAN ACTUALLY GO WRONG HERE, since a typo is not it -- the string comes from our own
    daemon:

      - A DAEMON ON THE WRONG NETWORK. `gridcoinresearchd getnewaddress` with no `-testnet`
        put RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV into the operator's live MAINNET staking
        wallet on 2026-09-27. Handed to a customer by a terminal that believes it is on
        testnet, that is real money paid onto a chain nothing here is watching. This is why
        the check is check_receive_address() and not check_address(): the NETWORK half is
        the half that catches the accident that actually happened.
      - A wallet answering with an error string, or a truncated RPC response, reaching
        `swaps.deposit_address` as a non-address.
      - XRP_DEPOSIT_ACCOUNT set by hand to something that is not an account.

    WHY REFUSING IS SAFE HERE AND NOT ON THE PAYOUT PATH. At this point in create_swap()
    NOTHING HAS MOVED: no deposit taken, no swap row written (the INSERT is still ahead of
    us), no tag allocated, no instruction shown to anyone. A refusal costs a retry. Letting
    it through costs a customer's entire deposit. tests/test_xrp_swap_attribution.py::
    test_a_refused_xrp_swap_leaves_no_row_behind already pins the "no row behind" half, and
    raising from here -- a pure function called before the INSERT -- keeps it true by
    construction rather than by cleanup.

    WHAT IT DOES NOT DO. NO_VALIDATOR does not refuse: see modules/address_authority.py's
    header. And `expected_network()` returns None for XRP and SOL, and for any chain on
    a port network_target.py has no convention for, so on those the network half is SKIPPED
    rather than guessed -- rule 17: "I could not tell" must never be written as "it is
    wrong". The decode half still runs everywhere.
    """
    verdict = check_receive_address(asset, address, expected_network(asset, config.get("RPC")))
    if verdict.refuses:
        raise ValueError(
            f"NO SWAP WAS CREATED, and nothing was written. The {asset} deposit address from {source} "
            f"cannot receive a deposit: {verdict.why}. A customer paying into it would lose the money "
            f"with nothing to show for it, so the swap is refused before the row exists rather than "
            f"after they have paid."
        )
    if verdict.unchecked:
        # Rule 14: an unchecked deposit address must not be indistinguishable from a checked
        # one. Not an exception, because it is not a refusal -- see the docstring. `.unchecked`
        # covers BOTH ways of passing unverified (no validator for the chain, and a validator
        # that could not place the address); the second is the one the operator's 2026-09-27
        # regtest run actually produced, via a missing bech32 hrp.
        logger.warning(
            "deposit address for %s from %s was NOT CHECKED (%s): %s  <- the swap is being created anyway, "
            "because refusing an address we cannot place would break a working chain.",
            asset, source, verdict.state, verdict.why,
        )


def refuse_unless_the_payout_can_be_funded(adapters, config, quote, from_asset: str, to_asset: str) -> None:
    """Raise if the destination wallet cannot fund this quote's payout. Writes nothing.

    EXTRACTED FROM create_swap() THE MOMENT IT LANDED, because adding the gate took
    that function to C901 11 > 10, and CLAUDE.md rule 12 says exactly what to do
    about that: "a main() past the ceiling is orchestration that has swallowed
    decisions ... the fix is to extract the decision so it can be called with
    seeded inputs, not to raise the ceiling." The decision itself is one level
    further down in services/payout_capacity.why_the_payout_cannot_be_funded();
    what lives here is the raise and the warning, which is the only part
    create_swap() was holding.

    TAKES THE QUOTE ROW RATHER THAN TWO FLOATS, so no caller can pair an amount
    with a reserve from somewhere else. Both come off the one row the payout will
    itself be computed from, which is what makes this gate and the payout agree.
    """
    # AND THE WALLET MUST ACTUALLY HOLD IT. THE FIFTH REFUSAL, AND THE ONE A REAL
    # DEPOSIT PAID FOR.
    #
    # MEASURED 2026-10-03, the first BTC -> GRC swap this terminal ever ran. The
    # deposit was flawless -- 0.001 BTC, 2 of 2 confirmations, COUNTED by the gate,
    # credited at 12:46:16 and irreversible -- and the payout claimed 11 seconds
    # later died on "Insufficient funds (rpc code -4)" from the Gridcoin daemon,
    # which was right:
    #
    #     need   9049.68583412 GRC
    #     have   3780.08854497 GRC spendable
    #     short  5269.59728915 GRC   -- the wallet held 41.8% of the payout
    #
    # Every gate above passed. GRC had an adapter, could sign, had a payout source,
    # and the fee covered the chain cost. The one question nobody asked was whether
    # the money was there, and grepping that day found why: quote_service.py and
    # this file contained ZERO get_balance() calls between them, so the payout's
    # funding was first tested BY THE DAEMON -- at the only moment when refusing
    # costs a customer their deposit instead of a retry.
    #
    # THAT IS THE SAME SENTENCE THE FOUR GATES ABOVE ARE EACH AN INSTANCE OF, so
    # this one belongs beside them rather than in the payout worker: refusing
    # before the swap row exists is the only stage at which nothing has been taken.
    #
    # LIVE POSTURE, AND IT WAS HANDED OVER BEFORE IT WAS BUILT (rule 16). A gate
    # that refuses swap creation changes what this terminal will trade, so the
    # numbers above went to the operator with three shapes to choose from -- refuse
    # here, cap the quotable input, or warn only -- and they authorized refusing.
    #
    # THE RESERVE IS ADDED, NOT SUBTRACTED: create_quote() no longer takes it out of
    # the payout (the fee-floor paragraph directly above carries that measurement),
    # so the wallet needs the payout AND the fee that sends it. That sum is exactly
    # what the daemon was short of.
    funding = why_the_payout_cannot_be_funded(
        adapters, to_asset, float(quote["output_amount_estimate"]), float(quote["network_fee_reserve"]),
        # THE SAME AUTHORITY create_swap() ALREADY ASKED, not a second reading of it:
        # "" means the daemon picks the payout's inputs, so its own wallet balance IS
        # the payable figure. A named account means it is not, and payout_capacity
        # says so instead of refusing on a method that would answer the wrong
        # question. config is a plain dict here, which is why this reads RPC-free.
        source_account=payout_source_account(config, to_asset),
    )
    if funding.refuses:
        raise ValueError(
            f"No swap was created, because {funding.why}. Nothing was written and nothing was taken."
        )
    if funding.unchecked:
        # THE SAME HANDLING payout_verdict.unchecked GETS TWENTY LINES UP, and for the
        # same reason: a question this process could not ask must reach the log rather
        # than be rendered as a pass. See payout_capacity.FundingVerdict.
        logger.warning(
            "payout funding for a new %s->%s swap was NOT CHECKED (%s)  <- proceeding; the daemon "
            "answers this at payout time, which is after the deposit is irreversible.",
            from_asset, to_asset, funding.why,
        )


def refuse_unusable_payout_address(config, adapters: dict, to_asset: str, payout_address: str) -> None:
    """Every refusal a PAYOUT address can earn, in the order that costs least. Raises or returns.

    EXTRACTED FROM create_swap() ON 2026-10-04, AND RUFF IS WHAT ASKED FOR IT.
    Adding the "is this address ours" gate took create_swap() to C901 11 > 10, and
    rule 12's note on that code is exact: a function past the ceiling is
    orchestration that has swallowed a decision, and the fix is to extract the
    decision rather than raise the ceiling. These four checks are one job --
    "may this address be paid" -- and they were already sitting together.

    THE ORDER IS THE ORDER THAT COSTS LEAST and it is not arbitrary; the comments
    inside say why at each step. Local decode first (no daemon, cannot be fooled by
    a loose adapter), then the daemon's network-scoped verdict, then whether the
    address is the desk's own. Everything here happens BEFORE the balance read and
    BEFORE deposit_account() derives a key, so a swap that is going to be refused
    leaves no derived key behind.

    RAISES ValueError WITH THE CUSTOMER'S SENTENCE, same as it did inline, so
    routes/swaps.py and open_swap.py both surface the reason unchanged. It returns
    None on success rather than a verdict: there is nothing for a caller to decide,
    and a boolean here would be a second place to get the polarity wrong.
    """
    # THE PAYOUT ADDRESS, DECODED LOCALLY BEFORE THE DAEMON IS ASKED. Added 2026-09-27.
    #
    # This is the EARLIEST point at which a burn can be stopped on the send side, and it is
    # the only one the web form reaches: open_swap.py's CLI carries the same local decode,
    # and routes/swaps.py POSTs straight here. services/payout_service.py guards the send
    # itself as a last resort, but by the time a swap reaches that worker the customer's
    # deposit has already been taken and credited -- a refusal there strands them. Here,
    # nothing has been written and nothing has been taken, so a refusal costs a retry.
    #
    # BEFORE the daemon, not instead of it, and the two are kept because they differ:
    # this one needs no daemon and cannot be fooled by an adapter that answers loosely,
    # and the daemon's answers whether THAT wallet on THAT network will accept it.
    #
    # DECODE ONLY -- deliberately NOT check_receive_address(). A payout address belongs to
    # the CUSTOMER's wallet, and refusing it for being on the wrong network would be this
    # process's own configuration overruling theirs. The daemon's validate_address() below
    # is network-scoped and is the right authority for that half.
    payout_verdict = check_address(to_asset, payout_address)
    if payout_verdict.refuses:
        raise ValueError(
            f"No swap was created: {payout_address!r} cannot receive a {to_asset} payout -- "
            f"{payout_verdict.why}. Refused here, before any daemon was asked and before any row was "
            f"written, because money sent to it would be unspendable by anybody. Nothing was written."
        )
    if payout_verdict.unchecked:
        logger.warning(
            "payout address for a new %s swap was NOT CHECKED locally (%s): %s  <- proceeding to the "
            "daemon's own validate_address(), which is the authority this could not stand in for.",
            to_asset, payout_verdict.state, payout_verdict.why,
        )
    if not adapters[to_asset].validate_address(payout_address):
        raise ValueError(f"Invalid {to_asset} payout address")
    # IS THE PAYOUT ADDRESS OURS? Added 2026-10-04 after swap s_ae76ec53236ffcf6
    # was created with its payout address set to XRP_DEPOSIT_ACCOUNT -- the desk's
    # own account, which the swap page PRINTS because a customer has to send to
    # it. Everything above accepted it and was right to: check_address() decodes,
    # validate_address() asks the daemon whether it is well-formed. Neither asks
    # whose it is. See services/custody_separation.payout_to_the_desk_refusal()
    # for the incident and for why the two chain families need different questions.
    #
    # PLACED HERE, which is the same reasoning the comment below gives for the
    # funding check: the daemon has already been asked once on this line, so a
    # second read costs nothing new, and this is still BEFORE the balance read and
    # before deposit_account() derives a key. A swap that is going to be refused
    # must not leave a derived key behind.
    #
    # owns_address() IS NOT CALLED ON A TAG CHAIN. chains/xrp.py overrides it to
    # return None by design -- the XRP Ledger has no `ismine` -- so asking it on
    # the one chain this incident happened on would always answer "not
    # established" and never refuse. The account comparison is what works there,
    # and payout_source_account() is already the single reader of that config.
    desk_account = payout_source_account(config, to_asset)
    owns = None if desk_account else adapters[to_asset].owns_address(payout_address)
    refusal = payout_to_the_desk_refusal(to_asset, payout_address, desk_account, owns)
    if refusal:
        raise ValueError(
            f"No swap was created: {refusal}. Refused before any key was derived and before any row "
            f"was written, so nothing here is to undo."
        )
    if owns is None and not desk_account:
        # NOT A REFUSAL, and the asymmetry is in the decision function's docstring:
        # refusing here would decline a customer's swap over our own daemon outage.
        # It is logged at WARNING because a payout wallet that cannot answer
        # `ismine` is a condition an operator should see, exactly as
        # chains/base.address_ownership() argues for the same reason.
        logger.warning(
            "payout address for a new %s swap could NOT be checked for being the desk's own: %s  <- "
            "proceeding, because refusing a customer over our own outage is the wrong direction. A "
            "payout to our own address would move nothing and still mark the swap completed.",
            to_asset, payout_address,
        )
    # PLACED AFTER THE ADDRESS CHECKS AND BEFORE deposit_account(), WHICH IS A
    # DELIBERATE ORDERING AND NOT WHERE IT WAS FIRST WRITTEN. It went in beside the
    # four local refusals above, and tests/test_address_authority.py::
    # test_a_deposit_address_that_cannot_receive_refuses_the_swap_and_writes_nothing
    # failed -- correctly. Its stub destination has no get_balance(), so this gate
    # refused first and the address refusal it exists to pin never ran. The fixture
    # was not the defect: the ordering was. Everything above this line is LOCAL and
    # free, this is a network read, and deposit_account() below DERIVES A KEY in the
    # hot wallet -- so the sequence is local checks, then the daemon's own address
    # verdict, then the balance, then the first thing that changes any state. A swap
    # that is going to be refused should not leave a derived key behind.


def create_swap(db, config, adapters: dict, quote_id: str, payout_address: str) -> dict:
    quote = get_quote_or_raise(db, quote_id)
    to_asset = quote["to_asset"]
    from_asset = quote["from_asset"]
    payout_address = payout_address.strip()
    # BOTH CHAINS MUST HAVE AN ADAPTER IN THIS PROCESS, AND THE MESSAGE HAS TO SAY
    # SO. Checked before the address validation below, because that line is where
    # the failure used to happen and it happened as a subscript.
    #
    # 2026-09-26, from the operator's browser: `No swap was created: 'GRC'`. That
    # is str(KeyError("GRC")) -- `adapters[to_asset]` raised, routes/swaps.py's
    # HTTP boundary returned str(exc), and a KeyError's str is the repr of the key
    # and nothing else. A running Gridcoin daemon on 25715, three workers printing
    # `GRC rpc=127.0.0.1:25715`, a priced quote, and the page said `'GRC'`.
    #
    # The cause was that the SERVER process had no GRC_RPC_PORT (the workers were
    # started from a shell that did), so build_adapters() skipped Gridcoin. The
    # information needed to fix it was one env var name, and none of it reached the
    # screen.
    #
    # from_asset is checked here as well, though deposit_account() below would
    # raise on it a few lines later: one refusal naming both missing chains beats
    # two consecutive single-chain failures, and a swap whose SOURCE chain has no
    # adapter has no deposit watcher looking at it either.
    missing = unconfigured_chains(adapters, from_asset, to_asset)
    if missing:
        raise ValueError(
            "No swap was created, because "
            + " Also: ".join(why_unconfigured(asset, config.get("RPC")) for asset in missing)
            + f" The {from_asset}->{to_asset} pair is in ALLOWED_PAIRS, which is why the quote priced -- "
            f"ALLOWED_PAIRS says what this terminal is WILLING to swap and the adapters say what it can "
            f"REACH, and those are different questions. Nothing was written."
        )
    # REACHABLE IS NOT THE SAME AS ABLE TO PAY, and this is the authority rather
    # than the page. routes/ui.py stops OFFERING such a pair, but a POST to
    # /api/swaps does not come from the page, so the gate that matters is here.
    #
    # GRC -> XRP on 2026-09-26: an XRP adapter exists and reaches the testnet, so the
    # check above passes. XRPAdapter held no signing key and payout_service called
    # send_to_address() unarmed, so the payout RAISED -- the customer's GRC would be
    # taken, credited, and the swap left in `failed` needing a person. Refusing
    # before the swap row exists is the only stage at which nothing has been taken.
    #
    # BOTH CLAUSES WERE FIXED ON 2026-10-02 and this gate is unchanged by that, which
    # is the point of leaving the paragraph. The adapter can sign and this call site
    # is wired, so `can_spend` is now the answer to "can this process actually sign an
    # XRP payout" -- and with nothing exported, which is the default, this check
    # refuses exactly as it did. What changed is that the operator has a way to make
    # it pass.
    #
    # AND SINCE 2026-10-03 THAT QUESTION IS NOT "IS THE VARIABLE SET". It was, and
    # the measurement that ended it is in chains/xrp_payout_seed.payout_capability():
    # XRP_PAYOUT_SECRET_SEED held a nine-character placeholder on the operator's host,
    # so can_spend was True, this gate PASSED, and the swap it would have created was
    # one whose payout could never have been signed -- discovered with the deposit
    # already irreversible. can_spend requires the seed to DECODE now, so this gate
    # refuses that state too, before any row exists.
    #
    # Checked BEFORE validate_address(), deliberately: for XRP that validator accepts
    # any X-address without verifying its checksum (found by review the same day), so
    # a chain that cannot be a destination must never be asked for one.
    cannot_pay = why_cannot_pay_out(adapters, to_asset)
    if cannot_pay:
        raise ValueError(
            f"No swap was created, because {cannot_pay} The {from_asset}->{to_asset} pair is in "
            f"ALLOWED_PAIRS and both chains are reachable -- but a swap that cannot be paid out takes "
            f"a deposit it can never settle. Nothing was written."
        )
    # AND THE ACCOUNT THE PAYOUT WOULD BE DEBITED FROM MUST EXIST, which the check
    # above cannot ask. Added 2026-10-02 with the XRP payout wiring, to close a hole
    # that wiring would otherwise have opened.
    #
    # why_cannot_pay_out() reads `can_spend` off the ADAPTER, and XRPAdapter sets that
    # from one question: does XRP_PAYOUT_SECRET_SEED hold a seed that DECODES in this
    # process (presence alone until 2026-10-03 -- see
    # chains/xrp_payout_seed.payout_capability() for the placeholder that cost). The
    # adapter has no Config and cannot be asked the second question -- is
    # XRP_DEPOSIT_ACCOUNT set -- so a host with the seed exported and the account
    # unset would pass the gate above, create the swap, TAKE AND CREDIT THE
    # CUSTOMER'S DEPOSIT, and then refuse at
    # services/payout_service.broadcast_payout() with nothing left to do but find a
    # person. That is the precise failure the whole GRC -> XRP paragraph in
    # chains/xrp.py is about, arriving through the one door the adapter cannot see.
    #
    # So it is asked HERE, where the config IS in scope and where nothing has been
    # written or taken yet, and a refusal costs a retry. Two variables, two checks,
    # both before the row exists.
    #
    # payout_source_account() RETURNS "" FOR EVERY CHAIN THAT HAS NO SOURCE TO NAME
    # (BTC, LTC, GRC -- their daemon picks the inputs), so this adds no condition to
    # them and the `ValueError` can only come from a SHARED_ACCOUNT_PAYOUT_ASSETS
    # destination. It is re-raised with the "No swap was created" framing the rest of
    # this function uses rather than allowed to propagate raw, because its own
    # sentence is written for the payout worker and says nothing about a swap.
    try:
        payout_source_account(config, to_asset)
    except ValueError as error:
        raise ValueError(
            f"No swap was created: {error} The {from_asset}->{to_asset} pair is in ALLOWED_PAIRS, both "
            f"chains are reachable and {to_asset} has a signing seed -- but the account it would pay "
            f"FROM is not configured, so the payout would refuse after the deposit had already been "
            f"credited. Nothing was written."
        ) from error
    # A SWAP WHOSE OWN FEE CANNOT PAY FOR ITS OWN PAYOUT.
    #
    # THIS GUARD USED TO BE AN ACCIDENT and 2026-10-02 turned it into a decision.
    # create_quote() computed max(gross * (1 - fee) - network_fee_reserve, 0.0), so
    # a small enough input clamped to exactly 0.0 and this refused it -- flagged by
    # review 2026-09-26, when open_swap.py was printing "payout (est.) 0.0 GRC <-
    # what payout_worker broadcasts" and exiting 0.
    #
    # The reserve is no longer subtracted from the payout (see
    # quote_service.create_quote for the measurement that required that), so the
    # clamp no longer fires and `<= 0` no longer catches anything: every positive
    # input now prices to a positive payout, however tiny. The floor the clamp was
    # providing was real and has to be stated rather than lost.
    #
    # STATED AS THE THING IT ALWAYS MEANT: the desk's fee on this swap must cover
    # what the desk pays the chain to deliver it. Below that line the swap loses
    # money by construction, whatever the customer receives, which is strictly what
    # the old clamp was groping at and 66x stricter than it:
    #
    #   old, at GRC_NETWORK_FEE_RESERVE=0.01   refused a gross at or under 0.01015
    #   new, same figure                       refuses a gross under 0.667
    #
    # Between those two a swap was CREATED whose 150bps fee was a fraction of a
    # cent against a transaction costing more than it earned. At a 0.05 GRC gross
    # the fee is 0.00075 and the chain costs 0.001: a loss, accepted.
    #
    # The comparison is against the CONFIGURED figure, not the measured 0.001,
    # deliberately. It is the operator's number for what a payout costs, it is now
    # conservative by about ten times, and because it no longer touches any
    # customer's payout, lowering it only widens what this terminal will accept --
    # which is a decision with a measurement behind it rather than a risk (rule 16).
    gross = float(quote["input_amount"]) * float(quote["quoted_rate"])
    retained = gross * float(quote["fee_bps"]) / 10000.0
    chain_cost = float(quote["network_fee_reserve"])
    if float(quote["output_amount_estimate"]) <= 0 or retained < chain_cost:
        raise ValueError(
            f"No swap was created: {quote['input_amount']} {from_asset} prices to a payout of "
            f"{quote['output_amount_estimate']} {to_asset}, on which the {quote['fee_bps']} bps fee is "
            f"{retained:.8f} {to_asset} -- less than the {chain_cost} {to_asset} one payout on this chain "
            f"costs. The swap would lose money however the customer's deposit arrives. Deposit more "
            f"{from_asset}. Nothing was written."
        )
    refuse_unusable_payout_address(config, adapters, to_asset, payout_address)
    refuse_unless_the_payout_can_be_funded(adapters, config, quote, from_asset, to_asset)
    swap_id = new_id("s")
    # The swap row must exist before a tag can reference it: xrp_destination_tags
    # has a FOREIGN KEY to swaps(id). Handled inside create_swap() below by
    # inserting the row first and allocating second -- see the note there.
    # Both config checks happen HERE, before anything is written: an unset or
    # invalid XRP_DEPOSIT_ACCOUNT must abort the swap rather than leave a row
    # behind. The tag itself is allocated after the INSERT, below.
    if from_asset in DB_ALLOCATED_DEPOSIT_ASSETS:
        # The address is not knowable yet -- see DB_ALLOCATED_DEPOSIT_ASSETS. The
        # placeholder is replaced below, after the INSERT, inside this transaction.
        deposit_address, needs_tag = DEPOSIT_ADDRESS_PENDING_ALLOCATION, False
    else:
        deposit_address, needs_tag = deposit_account(config, adapters, from_asset, swap_id)
    now = utc_now_iso()
    swap = {
        "id": swap_id,
        "quote_id": quote["id"],
        "from_asset": from_asset,
        "to_asset": to_asset,
        "deposit_address": deposit_address,
        # Filled in after the INSERT for a tag-attributed chain; see below.
        "deposit_tag": None,
        "payout_address": payout_address,
        "expected_input_amount": float(quote["input_amount"]),
        "actual_input_amount": None,
        "quoted_rate": float(quote["quoted_rate"]),
        "fee_bps": int(quote["fee_bps"]),
        "network_fee_reserve": float(quote["network_fee_reserve"]),
        "output_amount_estimate": float(quote["output_amount_estimate"]),
        "status": "awaiting_deposit",
        "min_confirmations": get_min_confirmations(config, from_asset),
        "deposit_txid": None,
        "payout_txid": None,
        "created_at": now,
        "updated_at": now,
        "credited_at": None,
        "completed_at": None,
        "expires_at": quote["expires_at"],
        "failed_reason": None,
    }
    db.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, deposit_tag, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            swap["id"], swap["quote_id"], swap["from_asset"], swap["to_asset"], swap["deposit_address"],
            swap["deposit_tag"], swap["payout_address"], swap["expected_input_amount"], swap["actual_input_amount"], swap["quoted_rate"],
            swap["fee_bps"], swap["network_fee_reserve"], swap["output_amount_estimate"], swap["status"],
            swap["min_confirmations"], swap["deposit_txid"], swap["payout_txid"], swap["created_at"],
            swap["updated_at"], swap["credited_at"], swap["completed_at"], swap["expires_at"], swap["failed_reason"],
        ),
    )
    # ALLOCATED HERE, after the INSERT and before the commit, so the swap row the
    # FOREIGN KEY needs exists and the whole thing is still one transaction. If
    # allocation raises, nothing is committed: no swap, no tag, no half-created
    # row handing a customer a deposit instruction with no way to recognize the
    # payment. That is the outcome to want -- a failed creation costs a retry,
    # while a swap that takes a deposit it cannot attribute costs the deposit.
    if from_asset in DB_ALLOCATED_DEPOSIT_ASSETS:
        # AFTER the INSERT because icp_deposit_subaccounts has a FOREIGN KEY to swaps,
        # and BEFORE the commit because a swap must never be visible carrying the
        # placeholder. Both halves of that sentence are load-bearing: reverse the first
        # and the allocating INSERT fails on the constraint; reverse the second and a
        # customer can be shown PENDING_SUBACCOUNT_ALLOCATION as an address to pay.
        deposit_address = allocate_db_deposit_address(db, adapters, from_asset, swap_id)
        swap["deposit_address"] = deposit_address
        db.execute("UPDATE swaps SET deposit_address = ? WHERE id = ?", (deposit_address, swap_id))

    if needs_tag:
        # from_asset is PASSED, and before 2026-10-01 it was not: the account was
        # checked against the XRP Ledger's format on every tag chain, so the first
        # real SOL -> GRC swap was refused 400 with "not a valid XRPL classic
        # address" for a correct Solana account. See ACCOUNT_VALIDATORS in
        # services/xrp_tag_service.py for the measurement.
        swap["deposit_tag"] = allocate_destination_tag(db, deposit_address, swap_id, from_asset)
        db.execute("UPDATE swaps SET deposit_tag = ? WHERE id = ?", (swap["deposit_tag"], swap_id))

    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, None, "awaiting_deposit", "Swap created", now),
    )
    db.commit()
    return swap


def get_swap(db, swap_id: str) -> dict | None:
    swap = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    if not swap:
        return None
    deposit_events = db.execute(
        "SELECT * FROM deposit_events WHERE swap_id = ? ORDER BY id ASC",
        (swap_id,),
    ).fetchall()
    payouts = db.execute(
        "SELECT * FROM payouts WHERE swap_id = ? ORDER BY id ASC",
        (swap_id,),
    ).fetchall()
    swap["deposit_events"] = deposit_events
    swap["payouts"] = payouts
    return swap
