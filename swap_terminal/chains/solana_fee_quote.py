#!/usr/bin/env python3
"""What the CLUSTER says one native SOL transfer costs, asked rather than typed.

Role: submodule (one decision -- "what does this cluster quote, right now, for
      the message of one one-signature native SOL transfer" -- callable with a
      stubbed transport and no cluster, rule 10)
Reads: the cluster, through the adapter handed in: getLatestBlockhash (via
      SolanaAdapter.latest_blockhash()) and getFeeForMessage (via
      SolanaAdapter.call()). No file, no database, no environment, NO KEY and
      no keypair.
Writes: nothing. Two reads and some arithmetic.
Can move funds: NO, AND IT CANNOT EVEN TRY. It builds a MESSAGE and never a
      signed transaction, it never calls chains/solana_transaction.wire_transaction(),
      it holds no secret and it calls no sending method. getFeeForMessage is a
      read; the money path is chains/solana.py::send_to_address().
Mainnet-safe: yes. It opens no socket of its own -- the adapter chose the
      endpoint long before this module saw it -- and the two methods it names
      are reads on any cluster.
Live-safe: yes. Safe to run against a live configuration while a worker cycles:
      it takes no lock, touches no database and changes no state.

=============================================================================
WHY THIS EXISTS
=============================================================================

The operator asked on 2026-10-03 what the four `*_NETWORK_FEE_RESERVE` values
should be. For SOL the tree could only answer with a figure somebody typed:
chains/solana_units.py:513 carries

    SIGNATURE_FEE_LAMPORTS = 5_000

and its own comment three lines up says "Reference value; getFeeForMessage is
the authority." show_payout_fees.py::ask_base_fee() names SOL's absence from it
explicitly and says why -- getFeeForMessage prices a SERIALIZED MESSAGE, so
asking it needs a real blockhash and a built transfer, which is "reachable and
not a one-line read, so it is named work rather than a number invented here."

This is that named work. It is the authority the constant points at, and the
constant stays exactly where it is and keeps its job: what a diagnostic prints
as "expected" next to what was read.

NO CALLER YET, AND THAT IS SAID HERE RATHER THAN LEFT TO BE DISCOVERED. Rules 2
and 9 are about not leaving uncalled code lying around looking authoritative, so
the honest state is this: the natural call site is
show_payout_fees.py::ask_base_fee(), whose docstring already describes the SOL
gap this fills, and wiring it in is one `elif asset == "SOL"` branch returning
`(f"{quoted / LAMPORTS_PER_SOL:.9f} SOL ({quoted} lamports)", provenance)` --
the same pair that function already returns for XRP, with the SAME failure
contract, so the printer above it needs no change at all. That edit was NOT made
in the commit this arrived in because show_payout_fees.py was being changed by
somebody else at the time, and two writers on one file is how one of the two
edits disappears. It is named work, not a baseline: tests/test_solana_fee_quote.py
pins the contract the branch will call, so the branch cannot be written against a
shape this does not have.

It is also usable on its own by anybody holding an adapter -- that is the point of
it being a function at the bottom rather than a branch inside a report (rule 10):

    from chains.registry import build_adapters
    from chains.solana_fee_quote import transfer_fee_lamports_from_cluster
    quoted, provenance = transfer_fee_lamports_from_cluster(build_adapters(Config.RPC)["SOL"])

SETTING SOL_NETWORK_FEE_RESERVE FROM WHAT COMES BACK IS THE OPERATOR'S CALL AND
NOT THIS MODULE'S (rule 16). It reads a number and says where the number came
from; it changes no configuration, and nothing here writes anything anywhere.

=============================================================================
IT NEVER FALLS BACK TO A CONSTANT, AND THAT IS THE WHOLE POINT
=============================================================================

A fee figure produced by a FAILED read is worse than no figure at all, because
it looks measured. It would be read off a screen, written into a config value,
and from then on be indistinguishable from a number a cluster actually quoted.

So every failure path here returns a REASON and no number:

    (lamports, provenance)   the cluster answered, and the provenance names the
                             method and the exact blockhash it priced against
    (None, reason)           the cluster could not be asked, or answered
                             without a figure. NO NUMBER. Not 5_000, not the
                             last value, not an estimate.

That is the same contract show_payout_fees.py::ask_base_fee() already has for
XRP -- a failed ask returns its reason in the slot a figure would have gone and
the caller prints "FAILED -- <reason>". Mirrored deliberately rather than
invented, so an operator reading either block reads the same shape (rule 8),
and the one difference is named: ask_base_fee() also returns a bare `None` for
"this asset is not mine to answer for", which this function never needs because
it answers for exactly one chain.

=============================================================================
WHY A MESSAGE AND NOT A SIGNED TRANSACTION. THIS IS A SAFETY PROPERTY
=============================================================================

getFeeForMessage takes a base64 MESSAGE -- the bytes a signature would be
computed over -- and needs NO SIGNATURE. That is why this module exists in the
shape it does: **a fee quote must not require the ability to spend.** Asking
"what does a transfer cost" with a signed transaction would mean this code path
holding a key, and a diagnostic that holds a key is a diagnostic that can be
made to send.

Concretely, the split is already in the tree and this module stays on the safe
side of it: chains/solana_transaction.py lays out bytes and cannot sign because
it never sees a secret, chains/solana_signing.py is where a key is handled, and
nothing below imports the latter. The one function from the former that this
calls is compile_transfer_message() -- the MESSAGE serializer, verified byte for
byte against @solana/web3.js (tests/test_solana_transaction.py, 60/60 random
vectors, 0 mismatches). wire_transaction(), which prepends a signature, is NOT
called here and must not be: it is what turns a priced message into something
broadcastable.

NO SECOND SERIALIZER (rule 8). The bytes this base64-encodes come from the one
serializer this repository has. A fee quote built on a hand-rolled copy of that
layout would price a message no payout would ever send, and the two would drift
apart silently -- the quote would keep answering, with the wrong number.

=============================================================================
WHERE THE TWO PUBKEYS COME FROM, AND WHY ANY WELL-FORMED PAIR WILL DO
=============================================================================

A native SOL transfer's fee is per SIGNATURE, not per byte, and the message
carries its required-signature count in its first header byte -- which for a
native transfer is always 1 (chains/solana_transaction.TRANSFER_MESSAGE_HEADER
is (1, 0, 1)). Swapping either pubkey changes 32 bytes inside a message whose
LENGTH is fixed at TRANSFER_MESSAGE_BYTES and whose header is untouched, so the
quantity the cluster prices is identical. The same is true of the lamport
amount: it is a fixed-width u64.

What is MEASURED here and what is REASONED (rule 17), said plainly because the
distinction is the difference between this comment and a guess:

  MEASURED, by tests/test_solana_fee_quote.py: two unrelated key pairs and two
      different amounts produce messages of the same length, with the same
      header and the same required-signature count, and a transport that prices
      by that count quotes them identically.
  REASONED, NOT MEASURED: that a real cluster therefore quotes them
      identically. No cluster has been reached from this container --
      api.devnet.solana.com answered 403 at the proxy again on 2026-10-03
      ("gateway answered 403 to CONNECT (policy)"), as it has all session. The
      first real reading is the operator's to take, and the provenance string
      this returns is written so that reading is self-describing.

So the default pair below is two throwaway pubkeys DERIVED IN THIS MODULE from
fixed phrases, and that choice is deliberate in three ways:

  * NOBODY HOLDS A KEY FOR EITHER. They are SHA-256 of a phrase, which is a
    32-byte string nobody can invert to a secret. tests/valid_addresses.py's
    solana_address_for() makes the same construction for the same reason and
    says the same thing about it; a Solana address is a bare 32-byte key with
    no checksum, so any 32 bytes is well formed and the only malformation
    available is a wrong LENGTH.
  * DERIVED, NOT SPELLED. A 44-character base58 literal has no checksum to
    protect it, so a typo in one is undetectable by reading -- see
    tests/valid_addresses.py's note on XRP's ACCOUNT_ZERO, where exactly that
    cost a character.
  * NOT THE OPERATOR'S ACCOUNTS, so a fee quote never prints a real deposit
    account into a log that gets pasted. A caller who WANTS the quote to name
    the accounts a payout would really use passes them; the answer does not
    change, but the provenance line then describes the real transfer.

Two distinct keys rather than one twice, because compile_transfer_message()
REFUSES a self-transfer -- a real client de-duplicates the key list, which
shifts every account index, so that layout would be wrong rather than merely
pointless.
"""

from __future__ import annotations

import base64
import hashlib

import base58

from .solana_transaction import compile_transfer_message
from .solana_units import BALANCE_COMMITMENT

#: The method, named once. Every string that goes on the wire and every string
#: that goes in a provenance line reads it from here, so the sentence an
#: operator reads cannot name a method other than the one that was called.
FEE_QUOTE_METHOD = "getFeeForMessage"

#: The blockhash read, by its adapter method rather than its RPC name, because
#: that is what this module calls. Named for the provenance line.
BLOCKHASH_SOURCE = "SolanaAdapter.latest_blockhash() -> getLatestBlockhash"

#: THE SAME COMMITMENT THE BLOCKHASH WAS READ AT, and that is a correctness
#: requirement rather than tidiness. chains/solana.BROADCAST_COMMITMENT -- which
#: latest_blockhash() fetches at -- is defined as BALANCE_COMMITMENT, so reading
#: the constant from chains/solana_units.py here names the SAME value without a
#: submodule reaching up into the adapter module (rule 10) and without a second
#: spelling of "finalized" that could drift from it (rule 8). A fee query at a
#: looser commitment than the blockhash read can hit a node that does not know
#: that blockhash yet, and a node that does not know the blockhash answers
#: `value: null` -- which this function would correctly report as "no figure"
#: for a cluster that was perfectly able to give one.
FEE_QUOTE_COMMITMENT = BALANCE_COMMITMENT

#: The amount priced. ONE lamport, and the amount is irrelevant to the answer:
#: a transfer's lamport field is a fixed-width u64, so every value produces a
#: message of identical length and identical header, and the fee is per
#: signature regardless (see the module docstring's measured/reasoned split).
#:
#: One rather than zero because chains/solana_transaction.transfer_instruction_data()
#: REFUSES zero, deliberately: a zero-lamport transfer is a valid transaction
#: that costs a fee and delivers nothing. Asking for a quote is not a reason to
#: route around that refusal, and inheriting it costs nothing here.
QUOTE_LAMPORTS = 1

#: The two throwaway pubkeys the default quote is priced over. SHA-256 of a
#: phrase, base58-encoded with no checksum, which is the format -- the module
#: docstring has the full argument for why these rather than a literal, why
#: nobody holds a key for them, and why any well-formed pair prices the same.
#: The phrases are kept as named constants so the test can re-derive the
#: addresses from them rather than pinning the base58 output, which would pin a
#: string a typo could change without anything noticing.
QUOTE_PAYER_PHRASE = "swap_terminal SOL fee quote payer -- no key exists for this account"
QUOTE_DESTINATION_PHRASE = "swap_terminal SOL fee quote destination -- no key exists for this account"


def _throwaway_pubkey(phrase: str) -> str:
    """A well-formed Solana pubkey from a phrase, with no secret behind it.

    SHA-256 gives exactly the 32 bytes a Solana public key is, deterministically,
    and base58 with NO checksum is the encoding Solana uses. It is not required
    to be on the ed25519 curve: an off-curve key is a Program Derived Address,
    which is a legitimate account, and getFeeForMessage prices a message's
    LAYOUT rather than validating the accounts in it -- nothing in a fee query
    reaches for the curve.

    Private because it is an implementation detail of the two constants below;
    a caller who wants a specific pair passes the addresses, not a phrase.
    """
    return base58.b58encode(hashlib.sha256(phrase.encode("utf-8")).digest()).decode("ascii")


QUOTE_PAYER = _throwaway_pubkey(QUOTE_PAYER_PHRASE)
QUOTE_DESTINATION = _throwaway_pubkey(QUOTE_DESTINATION_PHRASE)


def transfer_fee_lamports_from_cluster(
    adapter,
    *,
    payer: str = QUOTE_PAYER,
    destination: str = QUOTE_DESTINATION,
    lamports: int = QUOTE_LAMPORTS,
) -> tuple[int | None, str]:
    """What this cluster quotes, right now, for one native SOL transfer. READ-ONLY.

    Returns `(lamports, provenance)` when the cluster answered, where the
    provenance names the method, the commitment and the exact blockhash the
    figure was priced against -- so a line pasted into a log a day later still
    says what it is a measurement OF.

    Returns `(None, reason)` on every other outcome. NEVER A CONSTANT, never a
    fallback, never a last-known value: see the module docstring. The three ways
    to get there, each with its own sentence so the reader can tell them apart:

        the cluster could not be asked     a transport failure, a refused
                                           endpoint, an RPC error, or a
                                           malformed input this refuses to
                                           build a message out of
        the cluster answered no figure     `value: null`, whose DOCUMENTED cause
                                           is a blockhash the node does not
                                           recognize -- usually an expired one.
                                           A real answer, and not a number.
        the answer was not a figure        a shape this code cannot read as
                                           lamports, reported rather than coerced

    THE NULL CASE IS NOT HYPOTHETICAL AND IT IS NOT AN ERROR. getFeeForMessage
    answers `{"context": {...}, "value": null}` for a blockhash it cannot find,
    and a blockhash lives roughly 150 slots -- 60-90 seconds of wall clock
    (chains/solana.CONFIRMATION_DEADLINE_SECONDS carries that same figure and
    the same reasoning). The hash is read immediately before the quote here, so
    the window is small, but "small" is not "closed": a slow hop between the two
    calls is enough, and the failure is silent unless it is reported. Nothing
    retries it. A retry would make this function take an unbounded amount of
    time to answer a question the caller can simply ask again, and a caller that
    asked twice and got two reasons has learned more than one that waited.

    NO KEY, NO KEYPAIR, NO SIGNATURE, by construction rather than by promise.
    What is base64-encoded below is the MESSAGE -- the bytes a signature would be
    over -- because getFeeForMessage needs no signature, and a fee quote must not
    require the ability to spend. wire_transaction() is deliberately not imported.

    MEASURED 2026-10-03: no cluster. api.devnet.solana.com answered 403 at this
    container's proxy ("gateway answered 403 to CONNECT (policy)"), so every
    assertion about this function comes from tests/test_solana_fee_quote.py
    against a stubbed transport. The FORMAT it sends is the one
    tests/test_solana_transaction.py checked byte for byte against
    @solana/web3.js; that no cluster has priced it is stated here rather than
    left to be assumed.
    """
    # BUILT BEFORE IT IS SENT, and the build is inside the same guarded block as
    # the two reads on purpose: compile_transfer_message() refuses a self-transfer,
    # a malformed blockhash and a zero amount, and each of those refusals is a
    # reason this function must report rather than a crash in a diagnostic. The
    # caller gets a sentence either way and never a number.
    try:
        blockhash, last_valid_block_height = adapter.latest_blockhash()
        message = compile_transfer_message(payer, destination, lamports, blockhash)
        encoded = base64.b64encode(message).decode("ascii")
        result = adapter.call(FEE_QUOTE_METHOD, encoded, {"commitment": FEE_QUOTE_COMMITMENT})
    except Exception as exc:  # noqa: BLE001 -- checked: the failure is RETURNED as the reason and no number is ever produced from it, so the caller prints "could not be asked" rather than a figure that looks read. This is the one shape rule 12 allows a broad catch in, and it is the contract show_payout_fees.ask_base_fee() already has. Narrow is not available: the transport raises chains/solana.SolanaRPCError, which a submodule cannot import without reaching up into the adapter module (rule 10), and the serializer raises SolanaTransactionError -- plus a stubbed or future transport raises whatever it likes.
        return None, (
            f"the cluster could not be asked: {type(exc).__name__}: {exc} "
            f"<- NO FEE FIGURE was produced; nothing here substitutes one"
        )

    if not isinstance(result, dict) or "value" not in result:
        return None, (
            f"{FEE_QUOTE_METHOD} answered a shape this cannot read as a fee: {str(result)[:200]} "
            f"<- expected {{'context': ..., 'value': <lamports or null>}}; NO FIGURE was taken from it"
        )
    value = result["value"]
    if value is None:
        # `value: null` IS AN ANSWER, which is why it is not in the block above.
        # The node recognized the request and did not recognize the blockhash.
        return None, (
            f"{FEE_QUOTE_METHOD} answered value=null for blockhash {blockhash} at commitment "
            f"{FEE_QUOTE_COMMITMENT} <- the documented cause is a blockhash the node cannot find, which "
            f"for a hash read seconds ago means it EXPIRED between the two calls (a blockhash lives ~150 "
            f"slots; this one was valid through block height {last_valid_block_height}). Ask again. NO FEE "
            f"FIGURE was produced and none was invented."
        )
    try:
        quoted = int(value)
    except (TypeError, ValueError):
        return None, (
            f"{FEE_QUOTE_METHOD} answered value={value!r}, which is not a lamport count "
            f"<- reported rather than coerced; NO FIGURE was taken from it"
        )
    return quoted, (
        f"{FEE_QUOTE_METHOD} at commitment {FEE_QUOTE_COMMITMENT}, over the {len(message)}-byte MESSAGE "
        f"(not a signed transaction -- a fee quote needs no signature and this path holds no key) of a "
        f"one-signature native SOL transfer of {lamports} lamports from {payer} to {destination}, priced "
        f"against blockhash {blockhash} from {BLOCKHASH_SOURCE} "
        f"<- READ FROM THE CLUSTER, not from chains/solana_units.SIGNATURE_FEE_LAMPORTS"
    )
