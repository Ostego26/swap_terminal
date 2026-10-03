"""The SOL fee quote: what the cluster says, or why it could not say it. Never a constant.

Role: test (the real chains/solana_fee_quote.py and the real
      chains/solana_transaction.py serializer, against a stubbed transport)
Reads: swap_terminal/chains/solana_fee_quote.py,
      swap_terminal/chains/solana_transaction.py,
      swap_terminal/chains/solana_units.py, and
      tests/test_solana_adapter.py::make_adapter for the seeded transport
Writes: nothing
Can move funds: no, and nothing here could even if it were wrong. NO SOCKET IS
      OPENED -- `SolanaAdapter.call` is replaced by a table of seeded responses
      in every test below, so no test can reach a cluster by accident. NO
      KEYPAIR IS CONSTRUCTED anywhere in this file and no signature is produced:
      the function under test prices a MESSAGE, which is the whole point of it.
Mainnet-safe: yes
Live-safe: yes

=============================================================================
WHAT THIS IS ABOUT, AND THE 2026-10-03 MEASUREMENT IT RECORDS
=============================================================================

The operator asked on 2026-10-03 what `SOL_NETWORK_FEE_RESERVE` should be. The
only SOL figure in the tree was `SIGNATURE_FEE_LAMPORTS = 5_000` at
swap_terminal/chains/solana_units.py:513, whose own comment two lines above it
reads "Reference value; getFeeForMessage is the authority" -- CONFIRMED at that
exact line on 2026-10-03. show_payout_fees.py::ask_base_fee() names SOL's
absence from its own answer and says why: getFeeForMessage prices a serialized
MESSAGE, so asking it needs a real blockhash and a built transfer.
chains/solana_fee_quote.py is that ask, and this file is what holds it.

**NO CLUSTER WAS REACHED, MEASURED 2026-10-03 AND SAID HERE RATHER THAN LEFT TO
BE ASSUMED (rule 17).** A POST of `getHealth` to api.devnet.solana.com from
this container returned

    curl: (56) CONNECT tunnel failed, response 403

and the agent proxy's own status endpoint recorded the matching rejection,
`"kind": "connect_rejected", "detail": "gateway answered 403 to CONNECT
(policy)"`. So every assertion below is about this repository's behavior against
a seeded transport. The REQUEST FORMAT it sends is the one
tests/test_solana_transaction.py pinned byte for byte against @solana/web3.js
(60 random vectors, 0 message mismatches, 0 wire mismatches); whether a real
cluster answers it is the operator's first run, and nothing here claims it.

=============================================================================
WHAT IS MUTATION-CHECKED, AND WHY EACH ONE HAD TO BE
=============================================================================

A test that passes with the behavior deleted is not a test, and the failures
this file guards are all SILENT ones -- a fee quote that falls back to a
constant still prints a number, and the number looks measured. Each test below
names in its own docstring the edit that must break it, and all six were run on
2026-10-03: the edit was applied to chains/solana_fee_quote.py, pytest was
confirmed to FAIL, the file was restored from a byte-for-byte copy with its MD5
re-checked, and pytest was confirmed to PASS again.

    mutation applied to chains/solana_fee_quote.py          tests failed
    ----------------------------------------------------   ------------
    the success provenance drops the blockhash                     2
    price bytes([1]) + bytes(64) + message, i.e. SIGNED            3
    FEE_QUOTE_COMMITMENT = "processed"                             1
    the `if value is None` branch deleted                          1
    the broad-catch branch returns 5_000 instead of None           3
    QUOTE_DESTINATION derived from QUOTE_PAYER_PHRASE              9

Twelve tests, `pytest exit: 0` unmutated, and no mutation left the suite green.

=============================================================================
WHY THE TRANSPORT STUB IS IMPORTED RATHER THAN WRITTEN AGAIN
=============================================================================

tests/test_solana_adapter.py::make_adapter() already builds a REAL
SolanaAdapter whose only stubbed member is `call`, keyed by method name and
accepting a callable so a response can depend on the params. That is exactly
what is needed here, and a second copy of it would be rule 8's bug with a delay
on it -- the copies would agree today and drift the first time the adapter's
transport signature changed.

Importing it also keeps `SolanaAdapter.latest_blockhash()` in the test: this
file stubs the WIRE, not the blockhash getter, so the real method -- including
its own refusal when a cluster answers without a blockhash -- runs as shipped.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import tokenize

import base58
import chains.solana_fee_quote as fee_quote_module
import pytest
from chains.solana_fee_quote import (
    BLOCKHASH_SOURCE,
    FEE_QUOTE_COMMITMENT,
    FEE_QUOTE_METHOD,
    QUOTE_DESTINATION,
    QUOTE_DESTINATION_PHRASE,
    QUOTE_LAMPORTS,
    QUOTE_PAYER,
    QUOTE_PAYER_PHRASE,
    transfer_fee_lamports_from_cluster,
)
from chains.solana_transaction import (
    BLOCKHASH_BYTES,
    TRANSFER_MESSAGE_BYTES,
    TRANSFER_MESSAGE_HEADER,
    TRANSFER_WIRE_BYTES,
    SolanaTransactionError,
    compile_transfer_message,
    parse_transfer_transaction,
    wire_transaction,
)
from chains.solana_units import BALANCE_COMMITMENT, SIGNATURE_FEE_LAMPORTS
from test_solana_adapter import make_adapter
from valid_addresses import SOL_DEPOSIT_ACCOUNT, SOL_PAYOUT

#: The blockhash every seeded cluster below hands back. Thirty-two 0x03 bytes,
#: the same fixture tests/test_solana_transaction.py gave @solana/web3.js, so
#: the message bytes asserted here are bytes that reference implementation has
#: already agreed with. DERIVED rather than written: a Solana blockhash carries
#: no checksum, so a typo in a base58 literal is undetectable by reading.
BLOCKHASH = base58.b58encode(bytes([3]) * BLOCKHASH_BYTES).decode("ascii")

#: A SECOND blockhash, so a test asserting the provenance names the hash it
#: priced against cannot pass by naming the only hash in the file.
OTHER_BLOCKHASH = base58.b58encode(bytes([5]) * BLOCKHASH_BYTES).decode("ascii")

#: What a devnet cluster is expected to quote for one signature. NOT asserted as
#: the answer anywhere -- it is the stub's seeded value, and the point of the
#: function under test is that the figure comes from the cluster rather than from
#: a constant. A DIFFERENT number from SIGNATURE_FEE_LAMPORTS on purpose, so a
#: test cannot pass by accident if the function started answering from the tree.
SEEDED_FEE_LAMPORTS = 4_321


def blockhash_response(blockhash: str = BLOCKHASH, last_valid: int = 506_014_088) -> dict:
    """getLatestBlockhash's shape, as chains/solana.latest_blockhash() reads it."""
    return {"context": {"slot": last_valid}, "value": {"blockhash": blockhash, "lastValidBlockHeight": last_valid}}


def fee_response(value) -> dict:
    """getFeeForMessage's shape: a context and a `value` that may be null."""
    return {"context": {"slot": 506_014_088}, "value": value}


def quoting_cluster(fee=SEEDED_FEE_LAMPORTS, blockhash: str = BLOCKHASH):
    """An adapter whose cluster answers both reads, and that RECORDS the payload it was sent.

    `adapter.priced` is the list of base64 strings handed to getFeeForMessage,
    and `adapter.options` the options objects beside them. Recorded rather than
    inferred: the central claim of this file is about WHAT was sent, and a test
    that re-derives the payload from the function's inputs would be asserting
    its own arithmetic.
    """
    priced: list[str] = []
    options: list[dict] = []

    def fee_for_message(encoded, opts):
        priced.append(encoded)
        options.append(opts)
        return fee_response(fee)

    adapter = make_adapter(
        {"getLatestBlockhash": blockhash_response(blockhash), FEE_QUOTE_METHOD: fee_for_message}
    )
    adapter.priced = priced
    adapter.options = options
    return adapter


def test_a_cluster_that_quotes_a_figure_returns_that_figure_and_names_its_provenance():
    """The ordinary case: the number came from the cluster and the sentence says so.

    MUTATION: in chains/solana_fee_quote.py, drop the blockhash from the success
    provenance (delete ``priced against blockhash {blockhash} ``). The figure is
    unchanged and the function still "works" -- which is the point: a quote whose
    provenance does not name what it was priced against cannot be checked a day
    later, and rule 14 asks the pasted line to be self-describing. CONFIRMED to
    fail with that edit and to pass with it reverted.
    """
    adapter = quoting_cluster(fee=SEEDED_FEE_LAMPORTS)
    quoted, provenance = transfer_fee_lamports_from_cluster(adapter)
    assert quoted == SEEDED_FEE_LAMPORTS
    # THE SEEDED FIGURE AND NOT THE TREE'S. These two differ deliberately.
    assert quoted != SIGNATURE_FEE_LAMPORTS
    assert FEE_QUOTE_METHOD in provenance
    assert BLOCKHASH in provenance
    assert BLOCKHASH_SOURCE in provenance
    assert FEE_QUOTE_COMMITMENT in provenance
    # Both methods were actually called, in that order, which is what makes the
    # provenance a description of work rather than of an argument list.
    assert [method for method, _ in adapter.calls] == ["getLatestBlockhash", FEE_QUOTE_METHOD]


def test_the_quote_is_priced_against_the_blockhash_the_cluster_just_handed_back():
    """A second blockhash, so "names the hash" cannot pass by naming the only hash there is.

    MUTATION: as above. With the blockhash dropped from the provenance this fails
    on the OTHER_BLOCKHASH assertion; CONFIRMED.
    """
    adapter = quoting_cluster(blockhash=OTHER_BLOCKHASH)
    quoted, provenance = transfer_fee_lamports_from_cluster(adapter)
    assert quoted == SEEDED_FEE_LAMPORTS
    assert OTHER_BLOCKHASH in provenance
    assert BLOCKHASH not in provenance
    priced = base64.b64decode(adapter.priced[0])
    assert priced == compile_transfer_message(QUOTE_PAYER, QUOTE_DESTINATION, QUOTE_LAMPORTS, OTHER_BLOCKHASH)


def test_what_is_priced_is_a_MESSAGE_and_not_a_signed_transaction():
    """THE SAFETY PROPERTY: a fee quote must not require the ability to spend.

    getFeeForMessage takes the bytes a signature would be computed over and needs
    no signature, which is why this path can hold no key. Four independent ways of
    saying the payload is a message:

      * it is TRANSFER_MESSAGE_BYTES long (150) and NOT TRANSFER_WIRE_BYTES (215)
      * its first three bytes ARE the message header (1, 0, 1), where a wire
        transaction's first byte is a signature COUNT followed by 64 signature bytes
      * it is byte-identical to the serializer's compile_transfer_message() output
      * parse_transfer_transaction() -- the wire-format parser -- REFUSES it.
        MEASURED 2026-10-03: handed the bare message it reads the header at offset
        65 as (67, 216, 164) and raises. The same bytes with a signature prepended
        by wire_transaction() parse cleanly, asserted below, so the refusal is
        about the SHAPE and not about the message being malformed.

    MUTATION: in chains/solana_fee_quote.py, price a signed transaction instead --
    ``encoded = base64.b64encode(bytes([1]) + bytes(64) + message)``, which is the
    wire layout with an all-zero signature and is exactly the mistake this test
    exists for, since the cluster would answer a figure either way. CONFIRMED to
    fail with that edit and to pass with it reverted.
    """
    adapter = quoting_cluster()
    quoted, _ = transfer_fee_lamports_from_cluster(adapter)
    assert quoted == SEEDED_FEE_LAMPORTS
    assert len(adapter.priced) == 1
    priced = base64.b64decode(adapter.priced[0])

    assert len(priced) == TRANSFER_MESSAGE_BYTES
    assert len(priced) != TRANSFER_WIRE_BYTES
    assert priced[:3] == bytes(TRANSFER_MESSAGE_HEADER)
    assert priced == compile_transfer_message(QUOTE_PAYER, QUOTE_DESTINATION, QUOTE_LAMPORTS, BLOCKHASH)
    with pytest.raises(SolanaTransactionError):
        parse_transfer_transaction(priced)
    # The same bytes, signed, DO parse -- so the refusal above is the shape check
    # and not an accident of a bad message. An all-zero signature is used because
    # no key exists anywhere in this file to make a real one with, and the wire
    # parser does not verify signatures.
    assert parse_transfer_transaction(wire_transaction(bytes(64), priced))["lamports"] == QUOTE_LAMPORTS


def test_the_fee_query_asks_at_the_same_commitment_the_blockhash_was_read_at():
    """A looser commitment than the blockhash read is how a good cluster answers null.

    chains/solana.BROADCAST_COMMITMENT -- what latest_blockhash() fetches at -- is
    defined as chains/solana_units.BALANCE_COMMITMENT, so asserting against the
    units constant asserts against the same value without a second spelling.

    MUTATION: set FEE_QUOTE_COMMITMENT = "processed" in chains/solana_fee_quote.py.
    Nothing raises and the stub still answers; CONFIRMED to fail here.
    """
    adapter = quoting_cluster()
    transfer_fee_lamports_from_cluster(adapter)
    assert adapter.options == [{"commitment": BALANCE_COMMITMENT}]
    assert FEE_QUOTE_COMMITMENT == BALANCE_COMMITMENT


def test_a_cluster_that_answers_null_returns_the_reason_and_no_number():
    """`value: null` is a real answer, and the documented cause is a blockhash it cannot find.

    NOT an error and not an exception -- the node understood the request. So it
    gets its own branch and its own sentence, and that sentence has to name the
    blockhash, because "ask again" is only actionable if the reader knows which
    hash went stale.

    MUTATION: delete the ``if value is None`` branch in
    chains/solana_fee_quote.py. int(None) then raises TypeError into the
    coercion guard below it, so the function STILL returns no number -- and the
    reason stops naming the blockhash or the expiry, which is the whole content
    of the answer. CONFIRMED to fail with that edit and to pass with it reverted.
    """
    adapter = make_adapter({"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: fee_response(None)})
    quoted, reason = transfer_fee_lamports_from_cluster(adapter)
    assert quoted is None
    assert "value=null" in reason
    assert BLOCKHASH in reason
    assert "EXPIRED" in reason
    assert "NO FEE FIGURE" in reason


def test_a_cluster_that_raises_returns_the_reason_and_no_number():
    """The cluster could not be asked, which is never an answer about a fee.

    Two shapes, because the raise can come from either read: a transport that
    fails on the blockhash, and one that fails on the fee query after a perfectly
    good blockhash. The second is the one a narrower guard would miss.

    MUTATION: see test_no_failure_path_ever_returns_a_constant below, which is
    the same edit and the stronger assertion.
    """
    class ClusterDown(Exception):
        """Stands in for chains/solana.SolanaRPCError without importing the adapter's error type."""

    def raiser(*_params):
        raise ClusterDown("getFeeForMessage returned HTTP 403 from the proxy")

    on_the_fee = make_adapter({"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: raiser})
    quoted, reason = transfer_fee_lamports_from_cluster(on_the_fee)
    assert quoted is None
    assert "could not be asked" in reason
    assert "ClusterDown" in reason
    assert "403" in reason

    on_the_blockhash = make_adapter({"getLatestBlockhash": raiser, FEE_QUOTE_METHOD: fee_response(1)})
    quoted, reason = transfer_fee_lamports_from_cluster(on_the_blockhash)
    assert quoted is None
    assert "could not be asked" in reason
    # The fee query was never reached, so nothing was priced against a blockhash
    # the function never got. Asserted because the alternative -- building a
    # message with some other hash -- is the failure this guards.
    assert [method for method, _ in on_the_blockhash.calls] == ["getLatestBlockhash"]


def test_a_cluster_that_answers_without_a_blockhash_returns_the_reason_and_no_number():
    """The adapter's OWN refusal, reached through this function rather than copied.

    chains/solana.latest_blockhash() raises when a cluster answers without a
    blockhash, and that refusal is shipped code this file does not reimplement.
    What is asserted is that it arrives here as a reason rather than as a crash
    in a diagnostic -- and that no message was ever built, since there was no
    hash to build one over.
    """
    adapter = make_adapter(
        {"getLatestBlockhash": {"context": {}, "value": {}}, FEE_QUOTE_METHOD: fee_response(1)}
    )
    quoted, reason = transfer_fee_lamports_from_cluster(adapter)
    assert quoted is None
    assert "could not be asked" in reason
    assert [method for method, _ in adapter.calls] == ["getLatestBlockhash"]


def test_a_shape_this_cannot_read_as_a_fee_returns_the_reason_and_no_number():
    """A bare integer, a list, a string: reported rather than coerced.

    A `value` that is not a lamport count is its own case because coercing one
    is how a wrong number gets produced from a right-looking answer. `"many"`
    would raise inside int() and `[]` has no `value` at all.
    """
    for answer in ({"context": {}}, [], "5000", 5000):
        adapter = make_adapter({"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: answer})
        quoted, reason = transfer_fee_lamports_from_cluster(adapter)
        assert quoted is None, f"{answer!r} produced a figure"
        assert "NO FIGURE" in reason

    not_a_count = make_adapter(
        {"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: fee_response("five thousand")}
    )
    quoted, reason = transfer_fee_lamports_from_cluster(not_a_count)
    assert quoted is None
    assert "not a lamport count" in reason


def test_no_failure_path_ever_returns_a_constant():
    """THE CENTRAL CLAIM, over every failure this function has: a reason, never a number.

    A fee figure produced by a failed read is worse than no figure, because it
    looks measured -- it gets read off a screen and written into
    SOL_NETWORK_FEE_RESERVE, and from then on nothing distinguishes it from
    something a cluster quoted. So this sweeps every failure shape at once and
    asserts the figure is None and that no lamport-looking number from the tree
    appears in the reason either.

    MUTATION: make the broad-catch branch in chains/solana_fee_quote.py return
    ``5_000`` -- which is SIGNATURE_FEE_LAMPORTS' value, written as the literal
    because that module deliberately does not import the constant -- instead of
    None. That is the exact fallback its docstring refuses and the one a future
    reader is most likely to add as a kindness. CONFIRMED 2026-10-03 to fail in
    THREE tests (this one and the two raise/no-blockhash cases) and to pass with
    it reverted.
    """
    class Boom(Exception):
        pass

    def raiser(*_params):
        raise Boom("the endpoint is gone")

    failures = {
        "the fee query raised": {"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: raiser},
        "the blockhash read raised": {"getLatestBlockhash": raiser},
        "value was null": {"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: fee_response(None)},
        "no value key": {"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: {"context": {}}},
        "value was prose": {
            "getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: fee_response("free"),
        },
        "no blockhash in the answer": {"getLatestBlockhash": {"value": {}}},
    }
    for label, responses in failures.items():
        quoted, reason = transfer_fee_lamports_from_cluster(make_adapter(responses))
        assert quoted is None, f"{label}: produced {quoted!r}, which a failed read must never do"
        assert isinstance(reason, str) and reason.strip(), f"{label}: failed without saying why"
        assert str(SIGNATURE_FEE_LAMPORTS) not in reason, f"{label}: the reason quotes the tree's constant"
        assert "5000" not in reason, f"{label}: the reason carries a fee-shaped number"


def test_any_well_formed_pubkey_pair_prices_the_same_quantity():
    """The claim the default pubkeys rest on, verified rather than asserted in a comment.

    A native transfer's fee is per SIGNATURE and the required-signature count is
    the message's first header byte, which is 1 for every one of these. Swapping
    either key changes 32 bytes inside a message of FIXED length; swapping the
    amount changes 8 bytes of a fixed-width u64.

    WHAT THIS MEASURES: that the quantity handed to the cluster is the same shape
    -- same length, same header, same signature count -- for unrelated key pairs
    and unrelated amounts, and that a transport pricing by that count quotes them
    identically. The pairs include the operator's real devnet SOL_DEPOSIT_ACCOUNT
    and the suite's derived SOL_PAYOUT, so it is not only the module's own two.

    WHAT IT DOES NOT MEASURE, and rule 17 is why this sentence is here: that a
    REAL cluster prices them identically. No cluster was reachable (403 at the
    proxy, 2026-10-03). This is the mechanism checked against this repository's
    own serializer, not a reading off a chain.

    MUTATION: derive QUOTE_DESTINATION from QUOTE_PAYER_PHRASE in
    chains/solana_fee_quote.py, making the default pair a SELF-TRANSFER.
    compile_transfer_message() refuses it -- correctly, because a real client
    de-duplicates the key list and shifts every account index -- so the default
    quote becomes a permanent "could not be asked". CONFIRMED 2026-10-03 to fail
    in NINE of the twelve tests in this file -- every one that uses the default
    pair -- and to pass with it reverted. That blast radius is itself the finding:
    the defaults are load-bearing, not decoration.
    """
    priced: list[bytes] = []

    def price_by_signature_count(encoded, _opts):
        """The stub prices the way the chain does: lamports per required signature.

        Read out of the message's first header byte rather than assumed, so the
        figure this returns is a function of what was actually sent. The payload
        is recorded here rather than taken from quoting_cluster(), because this
        test needs a transport whose ANSWER depends on the bytes and that helper's
        does not.
        """
        priced.append(base64.b64decode(encoded))
        return fee_response(SIGNATURE_FEE_LAMPORTS * base64.b64decode(encoded)[0])

    pairs = (
        (QUOTE_PAYER, QUOTE_DESTINATION, QUOTE_LAMPORTS),
        (SOL_DEPOSIT_ACCOUNT, SOL_PAYOUT, 1),
        (SOL_PAYOUT, SOL_DEPOSIT_ACCOUNT, 1_000_000_000),
        (QUOTE_PAYER, SOL_DEPOSIT_ACCOUNT, 0xFFFFFFFFFFFFFFFF),
    )
    quotes = []
    for payer, destination, lamports in pairs:
        adapter = make_adapter(
            {"getLatestBlockhash": blockhash_response(), FEE_QUOTE_METHOD: price_by_signature_count}
        )
        quoted, provenance = transfer_fee_lamports_from_cluster(
            adapter, payer=payer, destination=destination, lamports=lamports
        )
        assert quoted == SIGNATURE_FEE_LAMPORTS, f"{payer}->{destination} priced as {quoted!r}"
        # The provenance names the pair it priced, which is what makes a quote
        # taken over the operator's real accounts distinguishable from the default.
        assert payer in provenance
        assert destination in provenance
        quotes.append(quoted)

    # EVERY MESSAGE THE SAME SHAPE: same length, same header, one required
    # signature -- which is the quantity the fee is charged on.
    assert len(priced) == len(pairs)
    assert {len(message) for message in priced} == {TRANSFER_MESSAGE_BYTES}
    assert {message[:3] for message in priced} == {bytes(TRANSFER_MESSAGE_HEADER)}
    assert {message[0] for message in priced} == {1}
    # And every message DIFFERENT, so the equality above is about the shape rather
    # than about four identical payloads.
    assert len(set(priced)) == len(pairs)
    assert len(set(quotes)) == 1, f"well-formed pairs priced differently: {quotes}"


def test_the_default_pubkeys_are_derived_from_their_phrases_and_hold_no_key():
    """Derived, distinct, and 32 bytes -- the three things the defaults have to be.

    Re-derived here from the PHRASES the module exports rather than pinned as
    base58 literals. A Solana address carries no checksum, so a pinned literal
    with a typo'd character is still a well-formed address and nothing would ever
    notice; a derivation cannot be typo'd into a different valid address without
    changing the phrase, which is readable.

    Nobody holds a key for either: both are SHA-256 of a sentence, which is a
    32-byte string no secret can be recovered from. tests/valid_addresses.py's
    solana_address_for() makes the identical construction for the identical
    reason, and that is the second site rule 8 asks be named from here.
    """
    def from_phrase(phrase: str) -> str:
        """base58 of SHA-256, written out here rather than imported from the module.

        Deliberately a SECOND spelling of the derivation: a fixture built by the
        same function that produces it proves only that the function agrees with
        itself. tests/valid_addresses.py's bech32m note makes the identical
        argument about its checksum, for the identical reason.
        """
        return base58.b58encode(hashlib.sha256(phrase.encode("utf-8")).digest()).decode("ascii")

    assert from_phrase(QUOTE_PAYER_PHRASE) == QUOTE_PAYER
    assert from_phrase(QUOTE_DESTINATION_PHRASE) == QUOTE_DESTINATION
    assert QUOTE_PAYER != QUOTE_DESTINATION
    assert len(base58.b58decode(QUOTE_PAYER)) == 32
    assert len(base58.b58decode(QUOTE_DESTINATION)) == 32
    # A self-transfer is refused by the serializer, so the pair being distinct is
    # load-bearing rather than cosmetic. Asserted through the real refusal.
    with pytest.raises(SolanaTransactionError):
        compile_transfer_message(QUOTE_PAYER, QUOTE_PAYER, QUOTE_LAMPORTS, BLOCKHASH)


def test_the_module_imports_nothing_that_can_sign():
    """No keypair, no signing module, no wire_transaction. Checked, not promised.

    chains/solana_signing.py is where a key is handled in this tree and
    chains/solana_transaction.wire_transaction() is what turns a priced message
    into something broadcastable. Neither may be REACHED from the fee-quote
    module, and the header's "Can move funds: no" is otherwise a sentence nobody
    verified.

    CHECKED OVER CODE TOKENS, NOT OVER THE FILE'S TEXT, and that distinction was
    not a refinement -- two earlier drafts of this test FAILED on the module's own
    docstring, which names chains/solana_signing.py and wire_transaction()
    precisely in order to say that it does not use them. A text scan therefore
    punishes rule 1: the more carefully the module explains why it is safe, the
    louder the safety check complains. `tokenize` gives the NAME tokens only --
    identifiers that are actually evaluated -- so prose can say anything and the
    assertion still means what it claims. tests/test_solana_adapter.py reaches for
    tokenize for the same reason; this is the second site, named here (rule 8).

    A getattr reach would slip past a NAME scan, so the module's import lines are
    checked as well -- a signing module cannot be reached without being imported,
    and that is the door rule 2 asks be watched by NAME.
    """
    source_path = pathlib.Path(fee_quote_module.__file__)
    with tokenize.open(source_path) as handle:
        names = {token.string for token in tokenize.generate_tokens(handle.readline)
                 if token.type == tokenize.NAME}
    assert names, "no NAME tokens were read at all, so this check proved nothing"
    assert "compile_transfer_message" in names, "the serializer is not called, so this file is not what it was"
    for forbidden in ("wire_transaction", "solana_signing", "SigningKey", "nacl", "Keypair",
                      "keypair", "secret_key", "from_seed", "sign"):
        assert forbidden not in names, f"the fee-quote module evaluates {forbidden}"

    import_lines = [line for line in source_path.read_text(encoding="utf-8").splitlines()
                    if line.startswith(("import ", "from "))]
    assert import_lines, "no import lines were found at all, so this check proved nothing"
    for line in import_lines:
        assert "signing" not in line, f"the fee-quote module imports a signing module: {line}"
