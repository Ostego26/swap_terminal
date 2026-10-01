"""The Solana Pay URI handed to a wallet, asserted on the string itself.

Role: test (pure functions; opens nothing)
Reads: chains/solana_pay.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- a Solana Pay URI names no cluster, so nothing here can
        select a network or reach one.

WHY THESE ASSERTIONS AND NOT A SHAPE CHECK.

The URI is the one artifact a customer's wallet acts on, and the browser is
deliberately given no logic that could alter it (see the module docstring for
why third-party JavaScript is not allowed near a deposit address). That makes
this file the only place the string is checked at all -- so it checks the
characters, not the shape.
"""

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

# E402 is not raised in tests (pyproject's per-file ignores), so the sys.path
# insert above needs no noqa claiming a check nobody asked for (rule 19).
from chains.solana_pay import (
    DEFAULT_LABEL,
    _decimal_amount,
    describe,
    payment_uri,
)

#: The operator's real devnet SOL_DEPOSIT_ACCOUNT, the account every SOL swap on
#: that host shares. Real rather than synthesized for the reason
#: tests/valid_addresses.py gives for the same string: a Solana address must be
#: ON the ed25519 curve and roughly half of valid 32-byte strings are not, so a
#: made-up one would be on-curve by luck.
ACCOUNT = "CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp"


def test_the_uri_carries_the_account_the_amount_and_the_bare_memo():
    """The whole instruction, and the memo undecorated.

    chains/solana_memo.deposit_tag_from() reads the WHOLE memo as the
    discriminator, so "swap 7" or "tag=7" does not match and the payment strands
    at a shared account. The only place that could decorate it is this builder.
    """
    uri = payment_uri(ACCOUNT, 0.01, 7)

    assert uri == f"solana:{ACCOUNT}?amount=0.01&memo=7&label={DEFAULT_LABEL}"
    assert "memo=7&" in uri or uri.endswith("memo=7"), "the memo is the bare tag, nothing added"


@pytest.mark.parametrize(
    ("amount", "rendered"),
    [
        (0.01, "0.01"),
        # THE TWO THAT A FLOAT'S repr GETS WRONG, and both are reachable from a
        # swaps row. SOL has 9 decimals, so 1e-07 SOL is an ordinary quantity;
        # str(1e-07) is "1e-07", which is not a decimal number to a URI parser.
        (1e-07, "0.0000001"),
        (1e-09, "0.000000001"),
        # A repeating binary fraction, as a quote priced to an awkward figure
        # produces. str() is right here and the test pins it so a future
        # "tidying" quantize cannot silently round a customer's amount.
        (0.1 + 0.2, "0.30000000000000004"),
        # THE ONE THAT CAUGHT A BUG IN THIS FILE'S SUBJECT. The first version of
        # _decimal_amount() special-cased an integral amount to avoid an
        # exponent, and returned "1E+1" for 10 -- because to_integral_value() on
        # an already-normalized Decimal("1E+1") is Decimal("1E+1"). The branch
        # written to avoid the exponent produced it.
        (10, "10"),
        (1.0, "1"),
        (0.5, "0.5"),
        (123.456, "123.456"),
    ],
)
def test_the_amount_is_a_plain_decimal_never_an_exponent(amount, rendered):
    """MUTATION: return str(amount) and the two small amounts fail.

    Asserted over a table rather than one value, because every wrong version of
    this function is right about 0.01 -- which is the only amount the rehearsal
    ever used.
    """
    assert _decimal_amount(amount) == rendered
    assert "e" not in _decimal_amount(amount).lower(), "no exponent may reach a wallet"


def test_an_empty_account_is_refused_rather_than_producing_half_a_request():
    with pytest.raises(ValueError, match="needs the deposit account"):
        payment_uri("", 0.01, 7)
    with pytest.raises(ValueError, match="needs the deposit account"):
        payment_uri("   ", 0.01, 7)


@pytest.mark.parametrize("memo", [None, "", "   "])
def test_a_missing_memo_is_refused_because_the_account_is_shared(memo):
    """The half that would strand the payment, refused where it costs an exception.

    A payment to the shared account carrying no memo is the case
    services/unattributable_deposit_service.py records and a person resolves by
    hand -- which happened for real on 2026-10-01 and is still open.

    MUTATION: give `memo` a default and this whole class of mistake becomes
    reachable by forgetting an argument.
    """
    with pytest.raises(ValueError, match="needs the memo tag"):
        payment_uri(ACCOUNT, 0.01, memo)


def test_the_memo_parameter_has_no_default():
    """"Required" is a property of the SIGNATURE, so that is what is asserted.

    MEASURED: a mutation giving `memo` a default of "" SURVIVED the refusal tests
    above, because every one of them passes a memo explicitly. A default is only
    reachable by a caller who OMITS the argument, and no test omits it -- so the
    tests that look like they cover this cover something else.

    It matters because every chain this builder serves attributes by memo. A
    default makes the one mandatory half of a two-part instruction omissible by
    forgetting it, and the failure is a payment at a shared account that nobody
    can claim rather than an exception anyone sees.
    """
    parameter = inspect.signature(payment_uri).parameters["memo"]
    assert parameter.default is inspect.Parameter.empty, (
        "memo must be required; a default makes a stranded deposit reachable by omission"
    )
    # And `account` for the same reason, while the signature is in hand.
    assert inspect.signature(payment_uri).parameters["account"].default is inspect.Parameter.empty


def test_tag_zero_is_a_real_memo_and_is_not_refused():
    """`0` is a legal tag, and the emptiness check must not swallow it.

    README's "Tag 0 is a real tag". A truthiness guard -- `if not memo` -- would
    refuse a perfectly valid swap, which is the same trap the swap page's
    template carries a comment about.
    """
    assert payment_uri(ACCOUNT, 0.01, 0).endswith("memo=0&label=" + DEFAULT_LABEL)


def test_the_memo_and_label_are_percent_encoded():
    """A memo is an integer today and a label is prose. Neither is trusted raw.

    Asserted with characters that are structural in a query string: an
    unescaped `&` would split one parameter into two and a wallet would read a
    different request than the one built.
    """
    uri = payment_uri(ACCOUNT, 0.01, 7, label="swap & terminal?x=1")

    assert "label=swap%20%26%20terminal%3Fx%3D1" in uri
    assert uri.count("&") == 2, "only the two separators this builder put in"


def test_no_spl_token_or_reference_parameter_is_emitted():
    """Two Solana Pay features deliberately unused, pinned so they stay unused.

    spl-token   this terminal takes NATIVE SOL. chains/solana.py's
                validate_address refuses an off-curve key for the same reason an
                associated token account must not appear in a native request.
    reference   a second attribution mechanism for one chain, when the memo
                already works for every tag chain and
                services/deposit_service.attributable_events() joins on it. Two
                answers to "whose deposit is this" is rule 8's duplicate, and
                the one that loses an argument costs a customer.
    """
    uri = payment_uri(ACCOUNT, 0.01, 7)

    assert "spl-token" not in uri
    assert "reference" not in uri


def test_describe_explains_without_being_part_of_the_request():
    """The wallet gets the URI; the human gets the sentence. Never the reverse.

    Explanatory text inside the URI would be a label a wallet renders as if the
    terminal had asserted it.
    """
    uri = payment_uri(ACCOUNT, 0.01, 7)
    sentence = describe(uri)

    assert sentence.startswith(uri)
    assert "Nothing is sent until you approve it" in sentence
    assert "never sees your key" in sentence
    # And the explanation is not smuggled into the request itself.
    assert "approve" not in uri
