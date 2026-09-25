"""Turning XRP Ledger transactions into this application's deposit events.

Role: function layer (rule 10 -- one decision per function, seeded inputs)
Reads: nothing. The transaction list is passed in.
Writes: nothing
Can move funds: no. It decides whether a deposit COUNTS, which releases a
      payout downstream -- so it is on the money path.
Mainnet-safe: yes

READ THE PARTIAL PAYMENT SECTION BELOW BEFORE CHANGING ANYTHING HERE.

THE PARTIAL PAYMENT EXPLOIT, AND WHY `Amount` IS NEVER READ

An XRP Ledger Payment carries an `Amount` field saying what the sender INTENDED
to deliver. With the tfPartialPayment flag set, the ledger is permitted to
deliver LESS than that -- and the transaction still succeeds, still reports
`tesSUCCESS`, and still shows the original, larger `Amount`.

What was actually delivered is in `meta.delivered_amount`, and only there.

An exchange that credits `Amount` on a successful payment can be drained: send
a partial payment claiming 1,000,000 XRP, deliver 1 drop, get credited a
million. This is not hypothetical and it is not obscure -- it is the
best-known integration mistake on this ledger, and it has taken real money off
real exchanges.

So: this module reads `meta.delivered_amount` and NOTHING ELSE for the figure
it credits. If that field is missing, the transaction is refused rather than
falling back to `Amount`, because the fallback IS the exploit.

WHAT ELSE IS CHECKED, AND WHY EACH ONE MATTERS

    TransactionType == "Payment"      an OfferCreate or TrustSet touching this
                                      account is not a deposit
    meta.TransactionResult
        == "tesSUCCESS"               every other code means it did not happen;
                                      a tec* code is INCLUDED in a ledger and
                                      claims a fee while transferring nothing
    validated is True                 an unvalidated ledger can still change
    Destination == our address        account_tx returns everything touching
                                      the account, including payments OUT
    delivered_amount is a STRING      a JSON object here means an ISSUED
                                      CURRENCY (someone's IOU), not XRP.
                                      Crediting a stranger's token as XRP at
                                      face value is the second way this path
                                      gets drained.
    DestinationTag present            without it the money cannot be attributed
                                      to a swap; see below

WHY THE DESTINATION TAG IS THE `vout`

db.py keys deposits on (asset, txid, vout) and the XRP Ledger has no output
index -- a Payment has exactly one Destination. The tag is what identifies
WHICH SWAP the money arrived for, it is an integer read from the transaction,
and one Payment carries exactly one of them, so (txid, tag) is unique by
construction rather than by assumption.

A payment with NO destination tag is money that arrived and cannot be
attributed. It is reported, never credited and never guessed at -- the same
refusal chains/monero_transfers.py makes for a transfer with no subaddress
index, and for the same reason: the alternative is inventing an identifier,
which is the fabrication chains/base.py:169 documents and migrate_deposit_
vouts.py exists to clean up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .xrp_units import from_drops, ledger_rank

# ---------------------------------------------------------------------------
# UNVERIFIED FIELD NAMES, gathered in one block. xrpl.org was unreachable from
# the environment this was written in, so these came from prior knowledge and
# NOT from the published API reference. See THE HONEST STATUS in chains/xrp.py.
#
# The API version matters here: rippled's v2 API renamed the per-entry `tx`
# object to `tx_json`. Both are accepted below, and which one a real server
# sends is exactly what xrp_chain_check.py is for.
# ---------------------------------------------------------------------------
FIELD_TX = "tx"
FIELD_TX_V2 = "tx_json"
FIELD_META = "meta"
FIELD_META_ALT = "metaData"
FIELD_VALIDATED = "validated"
FIELD_HASH = "hash"
FIELD_TRANSACTION_TYPE = "TransactionType"
FIELD_DESTINATION = "Destination"
FIELD_DESTINATION_TAG = "DestinationTag"
FIELD_DELIVERED_AMOUNT = "delivered_amount"
FIELD_TRANSACTION_RESULT = "TransactionResult"

PAYMENT_TYPE = "Payment"
SUCCESS_RESULT = "tesSUCCESS"


class XRPPaymentError(ValueError):
    """A ledger response could not be turned into deposit events safely.

    Every raise here is a refusal to guess. None is fixed by retrying the same
    scan: each means an assumption in this file is wrong, or the server sent
    something unexpected, and both want a human before money moves.
    """


@dataclass
class PaymentScan:
    """Credited events, and money that arrived but was not credited.

    Two lists rather than one, because "arrived and held back" and "did not
    arrive" are different facts and rule 14 forbids rendering them the same
    way. A payment with no destination tag is the common case here, and an
    operator needs to see it -- that is a customer whose deposit is sitting in
    the account unattributed.
    """

    events: list[dict] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)


def _unwrap(entry: dict) -> tuple[dict, dict]:
    """Pull the transaction and its metadata out of an account_tx entry.

    Accepts both API shapes rather than choosing one: rippled v1 nests the
    transaction under `tx`, v2 under `tx_json`, and metadata appears as `meta`
    or `metaData` depending on the call. Guessing wrong would make every
    deposit invisible -- an empty result that reads as "no deposits yet".
    """
    tx = entry.get(FIELD_TX) or entry.get(FIELD_TX_V2) or {}
    meta = entry.get(FIELD_META) or entry.get(FIELD_META_ALT) or {}
    if not isinstance(tx, dict) or not isinstance(meta, dict):
        raise XRPPaymentError(
            f"an account_tx entry has {FIELD_TX}/{FIELD_TX_V2}={type(tx).__name__} and "
            f"{FIELD_META}={type(meta).__name__}; expected objects. NOT credited -- this is the shape "
            f"the whole scan depends on, and reading it wrong would silently credit nothing."
        )
    return tx, meta


def _delivered_drops(meta: dict, tx_hash: str) -> int:
    """The amount ACTUALLY delivered, in drops. Never `Amount`.

    See the partial payment section in this module's docstring. There is no
    fallback to `Amount` and there must never be one: the fallback is the
    exploit, not a convenience.
    """
    if FIELD_DELIVERED_AMOUNT not in meta:
        raise XRPPaymentError(
            f"payment {tx_hash} has no {FIELD_META}.{FIELD_DELIVERED_AMOUNT}. NOT credited, and "
            f"DELIBERATELY NOT falling back to the Amount field: a partial payment reports the "
            f"intended Amount while delivering less, and crediting Amount is the exploit that has "
            f"drained real exchanges. If this field is genuinely absent from this server's responses, "
            f"the adapter is talking to something that cannot be trusted for deposits."
        )
    delivered = meta[FIELD_DELIVERED_AMOUNT]
    if isinstance(delivered, dict):
        currency = delivered.get("currency", "?")
        issuer = delivered.get("issuer", "?")
        raise XRPPaymentError(
            f"payment {tx_hash} delivered an ISSUED CURRENCY ({currency} from {issuer}), not XRP. NOT "
            f"credited: an issued amount is a claim against its issuer, and crediting one as XRP at "
            f"face value would pay out real XRP for a token anybody can mint."
        )
    if not isinstance(delivered, str):
        raise XRPPaymentError(
            f"payment {tx_hash} reports {FIELD_DELIVERED_AMOUNT}={delivered!r}, which is neither a "
            f"drop string nor an issued-currency object. NOT credited."
        )
    try:
        return int(delivered)
    except ValueError as error:
        raise XRPPaymentError(
            f"payment {tx_hash} reports {FIELD_DELIVERED_AMOUNT}={delivered!r}, which is not an "
            f"integer number of drops. NOT credited."
        ) from error


def _reject_duplicate_tags(events: list[dict]) -> None:
    """Refuse a scan in which two events would claim the same (txid, tag).

    A Payment carries exactly one destination tag, so this cannot happen
    against a real ledger. If it ever does, the response shape is not what this
    module assumes -- and the alternatives are to drop one (losing money the
    ledger delivered), sum them (guessing), or let both through (where
    UNIQUE(asset, txid, vout) silently overwrites one). All three are wrong in
    a way nobody would notice, so the scan stops.
    """
    seen: dict[tuple[str, int], dict] = {}
    for event in events:
        key = (event["txid"], event["vout"])
        if key in seen:
            raise XRPPaymentError(
                f"two payments in transaction {key[0]} both report destination tag {key[1]}, which "
                f"cannot happen -- a Payment carries exactly one tag. NOTHING was credited from this "
                f"scan; the response shape is not what this module assumes."
            )
        seen[key] = event


def _classify(entry: dict, address: str) -> tuple[dict | None, str | None]:
    """One transaction -> (event, None), (None, reason it was held back), or (None, None).

    Extracted so the decision is a function that can be called with a seeded
    transaction (rule 10, and rule 12's note that a loop past the complexity
    ceiling is orchestration which has swallowed a decision). (None, None)
    means "not ours" -- an OfferCreate, or a payment OUT of this account --
    which is not a result worth reporting.
    """
    tx, meta = _unwrap(entry)
    tx_hash = tx.get(FIELD_HASH) or entry.get(FIELD_HASH)
    if not tx_hash:
        raise XRPPaymentError(
            f"an account_tx entry for {address} has no {FIELD_HASH}. NOT credited -- an event with "
            f"no transaction id cannot be deduplicated, so every rescan would credit it again."
        )

    if tx.get(FIELD_TRANSACTION_TYPE) != PAYMENT_TYPE:
        return None, None
    if tx.get(FIELD_DESTINATION) != address:
        return None, None

    result = meta.get(FIELD_TRANSACTION_RESULT)
    if result != SUCCESS_RESULT:
        return None, (
            f"{tx_hash}  {FIELD_TRANSACTION_RESULT}={result}  <- NOT credited; only {SUCCESS_RESULT} "
            f"transfers value, and a tec* code still claims a fee while delivering nothing"
        )

    tag = tx.get(FIELD_DESTINATION_TAG)
    if tag is None:
        return None, (
            f"{tx_hash}  NO {FIELD_DESTINATION_TAG}  <- arrived and CANNOT be attributed to a swap. "
            f"Not credited and not guessed at; this needs an operator to match it by hand."
        )
    if not isinstance(tag, int) or isinstance(tag, bool):
        raise XRPPaymentError(
            f"payment {tx_hash} has a non-integer {FIELD_DESTINATION_TAG} ({tag!r}). NOT credited: "
            f"deposit_events keys on it as `vout` (db.py:110)."
        )

    drops = _delivered_drops(meta, tx_hash)
    validated = entry.get(FIELD_VALIDATED, tx.get(FIELD_VALIDATED))
    return {
        "txid": tx_hash,
        "vout": tag,
        "address": address,
        "amount": from_drops(drops),
        "confirmations": ledger_rank(validated),
    }, None


def deposit_events_from_transactions(entries, address: str, min_confirmations: int) -> PaymentScan:
    """account_tx entries -> the deposit-event dicts the application speaks.

    `min_confirmations` decides only whether something is reported as deferred.
    The event carries the real rank and the release decision stays in
    services/deposit_service.py, where it is for every other chain --
    duplicating the comparison here would be rule 8's two copies of one rule.
    """
    scan = PaymentScan()
    for entry in entries or []:
        event, reason = _classify(entry, address)
        if reason:
            scan.deferred.append(reason)
        if event is None:
            continue
        if event["confirmations"] < min_confirmations:
            scan.deferred.append(
                f"{event['txid']}  {event['amount']} XRP  tag={event['vout']}  <- arrived, ledger NOT "
                f"yet validated"
            )
        scan.events.append(event)
    _reject_duplicate_tags(scan.events)
    return scan
