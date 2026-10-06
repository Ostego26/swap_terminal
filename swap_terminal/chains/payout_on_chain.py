"""What the CHAIN says one payout transaction actually delivered.

Role: module (one dispatcher over five per-chain reads; it opens a socket, which
      is why it is a module and not the function layer -- the three decisions it
      makes are pure functions in the modules it calls)
Reads: one read-only transaction lookup per call, on the chain that paid --
      `gettransaction` (BTC/LTC/GRC), the XRP Ledger's `tx` method, Solana's
      `getTransaction`. Nothing else: no file, no database, no environment, no
      key material.
Writes: nothing
Can move funds: no. It calls no send method, passes no arming token and signs
      nothing. Every method it calls is a read.
Mainnet-safe: yes in the sense that it only ever reads, against whatever
      endpoint the adapter it is handed points at. It chooses no endpoint.

=============================================================================
WHY THIS EXISTS, 2026-10-03
=============================================================================

chains/payout_quantization.quantize_for_chain() answers "what WILL this chain
send for this figure", by arithmetic. On the operator's host that arithmetic
says all 23 rows in `payouts` record an amount their chain cannot express -- 15
of them with a txid, so the money is already gone and the record is what is
wrong.

Correcting those 15 writes a claim into the ledger: "this is what the chain
sent." The strongest form of that claim ASKS THE CHAIN, and the weaker form
recomputes the quantization the adapter would have performed. Both are useful
and they are NOT the same claim, which is the entire reason this module is
separate from payout_quantization:

    quantize_for_chain()        what a chain WOULD send. Pure arithmetic, always
                                available, and it is a derivation of our own
                                code rather than an observation.
    delivered_to_destination()  what the chain SAYS it sent. One read against a
                                daemon, available only when that daemon is
                                reachable and the transaction is still known to
                                it.

One of the 15 is independently confirmed: id=23's XRP payment
799F8DED7CFB657411C5B1B9BE500C79CD62F07CF5634D2817804E89BDED7935 reads
`Amount "3315589"` drops, `Fee "10"`, tesSUCCESS, validated true on the XRP
testnet -- 3.315589 XRP, exactly what quantize_for_chain() computes from the
recorded 3.3155893288590605. THE OTHER FOURTEEN ARE NOT CONFIRMED and this
module is how they get to be, one daemon at a time, instead of being assumed
from the one that was (rule 17).

WHAT A DISAGREEMENT MEANS, because it is the case worth building for. If a
chain answers with a figure that is not what quantize_for_chain() computes,
then the quantizer is wrong about that chain -- which is a bigger finding than
any single row, since the same function decides what every FUTURE payout
records, reserves and sends. So a caller is given both numbers and must refuse
rather than pick; correct_payout_amounts.py does exactly that.

=============================================================================
WHY EVERY FAILURE IS A RETURNED SENTENCE AND NEVER A NUMBER
=============================================================================

Every reader below returns (amount, how) with `amount = None` on any failure,
and `how` always says which failure. There is no path that returns a plausible
figure for an unanswered question, because the caller's whole job is to write a
number into a money record and it must be able to tell an observation from a
guess. That is rule 12's BLE001 note in its positive form: a broad catch is
legitimate only when the caller can tell the failure from a real answer, and
here `None` is that telling.

THE EXCEPTIONS CAUGHT ARE NAMED RATHER THAN `Exception`. CHAIN_READ_FAILURES
below is a tuple of four classes plus requests' own base, and it was built by
reading what each adapter raises rather than by widening until nothing escaped:

    chains/base.RPCError            the Bitcoin-derived three, for a daemon
                                    that answered with an error object
    chains/xrp.XRPRPCError          rippled, including its 200-with-an-error
    chains/solana.SolanaRPCError    the cluster, including its throttling
    chains/xrp_payments.XRPPaymentError
                                    a response that is not a validated
                                    successful Payment to the address asked
                                    about
    requests.RequestException       every transport failure, for all five:
                                    connection refused, reset, timeout, a bad
                                    status, and requests' own JSONDecodeError
                                    (which subclasses InvalidJSONError, which
                                    subclasses RequestException -- checked
                                    against the installed requests rather than
                                    recalled)

Anything outside that set propagates, and that is deliberate: an unexpected
exception type means an assumption here is wrong, and
correct_payout_amounts.py commits per row, so the rows already corrected stay
corrected while the unexpected thing reaches a human instead of becoming
"the chain could not be reached" on fourteen rows at once.
"""

from __future__ import annotations

from typing import NamedTuple

import requests
from script_pub_key import pays_address

from .base import RPCError
from .coin_amounts import CHAIN_DECIMALS
from .icp import transfer_operation
from .icp_account import ICP_DECIMALS
from .solana import SolanaRPCError, native_delta_lamports
from .solana_units import SOL_DECIMALS, base_units_to_amount
from .xrp import XRPRPCError
from .xrp_payments import XRPPaymentError, delivered_drops_to
from .xrp_units import from_drops

#: See THE EXCEPTIONS CAUGHT ARE NAMED in this module's docstring. Every member is
#: a chain or a transport saying "I cannot answer that", which is a sentence for
#: the caller and never a number.
CHAIN_READ_FAILURES = (
    RPCError,
    XRPRPCError,
    SolanaRPCError,
    XRPPaymentError,
    requests.RequestException,
)

#: The RPC method each family is asked, named here so the printed provenance and
#: the call cannot drift apart: the sentence an operator reads says the method the
#: code actually called.
BITCOIN_FAMILY_METHOD = "gettransaction"
XRP_METHOD = "tx"
SOLANA_METHOD = "getTransaction"


class ChainAmount(NamedTuple):
    """What a chain said, or why it did not say anything.

    `amount` is None for EVERY failure, and `how` is never empty in either case:
    on success it names the method and the field the figure came from, so a
    corrected row can be re-derived by hand a year later; on failure it names the
    failure. Rule 14 -- state what the number means, next to the number, and never
    let an absence print as a blank.
    """

    amount: float | None
    how: str


def _delivered_bitcoin_family(adapter, asset: str, txid: str, address: str) -> ChainAmount:
    """BTC / LTC / GRC: the output of `gettransaction` that pays `address`.

    TWO RESPONSE SHAPES, because chains/base.RPCAdapter.get_transaction() falls
    back from `gettransaction` to `getrawtransaction` and this reads whichever it
    got. The wallet shape carries `details` (one entry per output, with `category`
    and a NEGATIVE amount, because it is a debit); the raw shape carries `vout`
    (with a value and a scriptPubKey). Both are handled, and a response with
    neither is a sentence rather than a zero.

    MATCHED ON script_pub_key.pays_address() FOR THE RAW SHAPE, not on
    `scriptPubKey.addresses`. That field was removed from Bitcoin Core in 22.0 and
    is still present on Litecoin 0.21.4, and reading one of the two spellings is a
    defect this tree has found FOUR times -- swap_terminal/script_pub_key.py's
    header names all four. This is the fifth site that needs the answer and it
    asks the one module that knows.

    SEVERAL OUTPUTS PAYING THE SAME ADDRESS WITH DIFFERENT VALUES IS A REFUSAL,
    not a sum. A payout is one output; a transaction with two of them to one
    address is not the shape this reads, and picking either figure -- or adding
    them -- would be a guess written into a money record. Identical values
    collapse to one answer because then there is nothing to choose between.
    """
    transaction = adapter.get_transaction(txid)
    details = transaction.get("details")
    if isinstance(details, list) and details:
        values = {
            abs(float(entry["amount"]))
            for entry in details
            if entry.get("category") == "send" and entry.get("address") == address
            and entry.get("amount") is not None
        }
        field = f"{BITCOIN_FAMILY_METHOD}.details[category=send, address={address}].amount"
    elif isinstance(transaction.get("vout"), list):
        values = {
            float(output["value"])
            for output in transaction["vout"]
            if pays_address(output.get("scriptPubKey", {}), address) and output.get("value") is not None
        }
        field = f"getrawtransaction.vout[scriptPubKey pays {address}].value"
    else:
        return ChainAmount(None, (
            f"the {asset} wallet answered for {txid} with neither a `details` list nor a `vout` list, "
            f"so there is no output to read an amount from. Keys present: {sorted(transaction)[:8]}"
        ))
    if not values:
        return ChainAmount(None, (
            f"no output of {txid} pays {address} according to the {asset} daemon, so this transaction "
            f"says nothing about this payout. NOT read as zero -- a payout of zero is a claim and this "
            f"is not that claim"
        ))
    if len(values) > 1:
        return ChainAmount(None, (
            f"{len(values)} outputs of {txid} pay {address} with DIFFERENT values "
            f"({', '.join(repr(value) for value in sorted(values))}). Refusing to choose or to sum: a "
            f"payout is one output, and either answer would be a guess written into a money record"
        ))
    return ChainAmount(values.pop(), f"read from the {asset} daemon: {field}")


def _delivered_xrp(adapter, asset: str, txid: str, address: str) -> ChainAmount:
    """XRP: `meta.delivered_amount` of the `tx` response, through xrp_payments.

    IT DOES NOT READ `Amount`, AND THAT IS NOT A STYLE CHOICE. A Payment carrying
    tfPartialPayment reports the intended `Amount` while delivering less, and
    crediting `Amount` is the best-known way to drain an exchange on this ledger.
    chains/xrp_payments.py owns that distinction for the deposit path and
    delivered_drops_to() was added there so this module cannot hold a second
    opinion about it (rule 8).

    A PARTIAL PAYMENT ON OUR OWN PAYOUT IS REFUSED RATHER THAN RECORDED. The two
    figures come back together precisely so this can compare them: if the ledger
    delivered less than the Payment intended, then what the customer received and
    what the desk's record should say are a question an operator has to answer,
    not a quantization. Nothing this terminal sends sets that flag -- checked:
    chains/xrp_signing.py builds the Payment and never sets Flags -- so this
    branch firing means something is true that this tree does not believe.
    """
    response = adapter.call(XRP_METHOD, {"transaction": txid})
    delivered, intended = delivered_drops_to(response, address)
    if intended is not None and intended != delivered:
        return ChainAmount(None, (
            f"transaction {txid} delivered {delivered} drops against an intended Amount of {intended} "
            f"drops -- a PARTIAL PAYMENT. Refused rather than recorded: what the customer received and "
            f"what this row should say is an operator's question, and nothing this terminal sends sets "
            f"that flag"
        ))
    return ChainAmount(from_drops(delivered), (
        f"read from the XRP Ledger: {XRP_METHOD}.meta.delivered_amount = {delivered} drops "
        f"({from_drops(delivered)!r} {asset}), on a validated tesSUCCESS Payment to {address}"
    ))


def _delivered_solana(adapter, asset: str, txid: str, address: str) -> ChainAmount:
    """SOL: how many lamports the destination's balance rose, from `getTransaction`.

    THE SUBTRACTION IS chains/solana.native_delta_lamports(), which lives beside
    the deposit path's copy of it with the three genuine differences written out
    at both sites (rule 8). It returns None with a sentence for every shape it
    cannot read, so nothing here has to invent a zero.

    A NON-POSITIVE DELTA IS A REFUSAL AND NOT A ZERO. The destination of a payout
    this desk recorded as broadcast cannot have come out of that transaction
    unchanged or poorer; if it did, the signature in the row does not describe the
    payment the row claims, and that is a finding rather than an amount.

    maxSupportedTransactionVersion IS 0, the same cap chains/solana.py's deposit
    read uses and for the same reason its comment gives: a versioned transaction
    whose addresses come from a lookup table would misalign `accountKeys` against
    the balance arrays, and a misaligned index here would read SOMEBODY ELSE's
    balance change as this payout's amount. A capped read answers -32015 and is
    reported as unread, which is the correct trade on a line that writes a money
    record.
    """
    transaction = adapter.call(
        SOLANA_METHOD, txid,
        {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0},
    )
    if not transaction:
        return ChainAmount(None, (
            f"the cluster returned nothing for signature {txid}. That is not evidence the payment did "
            f"not happen -- a node that has pruned its history answers the same way -- so nothing is "
            f"read from it"
        ))
    delta, why = native_delta_lamports(transaction, address)
    if delta is None:
        return ChainAmount(None, f"the cluster's answer for {txid} cannot be read: {why}")
    if delta <= 0:
        return ChainAmount(None, (
            f"{address}'s balance moved by {delta} lamports in {txid} ({why}), which is not a credit. "
            f"A payout recorded as broadcast cannot have left its destination unchanged or poorer, so "
            f"this signature does not describe the payment this row claims"
        ))
    return ChainAmount(base_units_to_amount(delta, SOL_DECIMALS), (
        f"read from the Solana cluster: {SOLANA_METHOD} {why}, at SOL_DECIMALS={SOL_DECIMALS}"
    ))


#: asset -> the function that asks that chain what it delivered.
#:
#: THE BITCOIN FAMILY IS DERIVED FROM CHAIN_DECIMALS rather than listed, exactly as
#: chains/payout_quantization.QUANTIZERS is and for the same reason (rule 11's
#: test: if a chain had to be added to a second place by hand, that second place is
#: the bug). The two tables are deliberately keyed the same way, because a chain
#: this terminal can quantize for and cannot read back is a gap worth seeing --
#: tests/test_correct_payout_amounts.py asserts the two key sets are identical.
def _delivered_icp(adapter, asset: str, txid: str, address: str) -> ChainAmount:
    """ICP: the e8s the ledger recorded as delivered, from the block the txid names.

    THE TXID IS A BLOCK INDEX, which makes this the cheapest read-back of the five: no
    scan, no balance subtraction, no versioned-transaction hazard. query_blocks(start=N,
    length=1) returns exactly the block whose number the payout row holds, and the
    Transfer inside it carries the amount the ledger actually moved.

    THE DESTINATION IS CHECKED, and that is the whole point rather than a formality. A
    payout row pairs a block index with an address; if the block at that index paid
    SOMEBODY ELSE, the row does not describe the payment it claims, and reporting its
    amount anyway would write a confident figure about the wrong transaction. So a
    mismatch is a refusal naming both accounts, not an amount.

    A MINT IS NOT A PAYOUT either, for the same reason it is not a deposit: block 0 of a
    freshly initialized ledger is a Mint, and a payout row pointing at one is a row that
    is wrong about something.

    ARCHIVED BLOCKS ARE A REFUSAL. An old payout's block will have migrated to an
    archive canister, and the ledger then answers with an empty `blocks` and the range in
    `archived_blocks`. Reading an archive is not implemented, so this says so -- a
    corrected amount derived from a block nobody read would be the
    wrong-number-under-the-right-label failure correct_payout_amounts.py exists to
    remove, with VERIFIED as the label.
    """
    index = str(txid).strip()
    if not index.isdigit():
        return ChainAmount(None, (
            f"{asset} payout txid {txid!r} is not a block index. ICP has no transaction hashes -- "
            f"the closest thing is the ledger block number `transfer` returns -- so a non-numeric "
            f"value here is a row written by something that did not know that"
        ))
    page = adapter._call_json(
        "query_blocks", f"(record {{ start = {index} : nat64; length = 1 : nat64 }})"
    )
    archived = page.get("archived_blocks") or []
    blocks = page.get("blocks") or []
    if not blocks:
        if archived:
            return ChainAmount(None, (
                f"{asset} block {index} has migrated to an archive canister ({len(archived)} range(s) "
                f"reported), and reading an archive is not implemented. The amount is UNREAD rather "
                f"than derived: a figure from a block nobody read would carry the label VERIFIED"
            ))
        return ChainAmount(None, (
            f"the {asset} ledger returned no block at index {index} (chain_length "
            f"{page.get('chain_length')!r}), so nothing was read. This is not a zero amount"
        ))
    transfer = transfer_operation(blocks[0])
    if transfer is None:
        return ChainAmount(None, (
            f"{asset} block {index} holds no Transfer operation -- a Mint or Burn, or an absent "
            f"operation. A payout row pointing at one is wrong about something, so no amount is "
            f"reported from it"
        ))
    paid_to = bytes(transfer.get("to") or []).hex()
    if paid_to != address.strip().lower():
        return ChainAmount(None, (
            f"{asset} block {index} paid {paid_to} and this payout row names {address}. The row does "
            f"not describe that block, so its amount is NOT this payout's: reporting it would write a "
            f"confident figure about somebody else's transaction"
        ))
    e8s = int((transfer.get("amount") or {}).get("e8s", 0))
    return ChainAmount(
        e8s / 10**ICP_DECIMALS,
        f"read from the {asset} ledger: query_blocks block {index}, Transfer.amount.e8s = {e8s}",
    )


READERS = dict.fromkeys(CHAIN_DECIMALS, _delivered_bitcoin_family) | {
    "XRP": _delivered_xrp,
    "SOL": _delivered_solana,
    # ICP is listed rather than derived, matching chains/payout_quantization.QUANTIZERS
    # and for the same reason: its ledger takes and reports integer e8s, so it is not in
    # CHAIN_DECIMALS and the fromkeys() above does not reach it.
    "ICP": _delivered_icp,
}


def delivered_to_destination(adapter, asset: str, txid: str, address: str) -> ChainAmount:
    """What `asset`'s chain says `txid` delivered to `address`. One read-only call.

    THE DISPATCHER. Returns ChainAmount(None, why) and never raises for any of the
    reasons a chain cannot answer -- no adapter, no reader, no txid, a daemon that
    refused, a transport that failed, a response shape that cannot be read -- so a
    caller correcting a batch of rows gets a per-row sentence instead of a stack
    trace that ends the batch.

    A MISSING ADAPTER IS THE COMMONEST CASE AND IS NOT AN ERROR. An operator
    running this with only their Gridcoin daemon configured should have their GRC
    rows verified against the chain and be TOLD, per row, that the BTC ones were
    not. That is rule 14's "make did-nothing look different from did work" on the
    one line that says whether a figure was observed or computed.
    """
    if not (txid or "").strip():
        return ChainAmount(None, "the row carries no txid, so there is no transaction to read")
    if adapter is None:
        return ChainAmount(None, (
            f"no {asset} adapter exists in this process, so the chain was NOT asked. Its RPC settings "
            f"are unset -- chains/registry.why_unconfigured({asset!r}) names which variables"
        ))
    reader = READERS.get(asset)
    if reader is None:
        return ChainAmount(None, (
            f"{asset} has no entry in chains/payout_on_chain.READERS, so this terminal does not know "
            f"how to ask that chain what it delivered. An asset reaching here is a gap in READERS, not "
            f"a property of the asset"
        ))
    try:
        return reader(adapter, asset, txid, address)
    except CHAIN_READ_FAILURES as error:
        # Named types, not `except Exception` -- see this module's docstring for
        # what each one is and why anything else propagates. The failure becomes
        # the `how` sentence and the amount stays None, so the caller cannot read
        # a refusal as a figure.
        return ChainAmount(None, (
            f"the {asset} chain could not be asked about {txid}: {type(error).__name__}: {error}"
        ))
