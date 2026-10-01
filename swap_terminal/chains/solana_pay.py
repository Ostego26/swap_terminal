"""The Solana Pay URI for a swap's deposit, built where it can be tested.

Role: function level (one decision -- the string a wallet is handed -- callable
      with seeded inputs per CLAUDE.md rule 10)
Reads: nothing. Every input is an argument. No environment, no file, no socket.
Writes: nothing
Can move funds: no. It produces a REQUEST. A wallet shows it to its owner, who
      approves or does not; nothing here signs and nothing here can send.
Mainnet-safe: it names no cluster. A Solana Pay URI carries a recipient, an
      amount and a memo, and the wallet decides which cluster it is on -- so
      this cannot select a network and cannot be pointed at one.

WHY THIS IS SERVER-SIDE, WHICH IS THE WHOLE POINT.

Asked for by the operator 2026-10-01: wallet connect and a QR code, "do both
they can choose". Both need the same thing -- a payment request carrying the
deposit account, the exact amount, and the memo tag -- and the obvious way to
get it is to assemble it in the browser.

That would put the deposit address through third-party JavaScript. The usual
wallet libraries are a megabyte of code this repository cannot audit, loaded
from a CDN, running on the one page that tells a customer where to send money. A
compromised or merely buggy copy substitutes one base58 string for another and
the customer's payment is gone, with every server-side check passing because the
server was never asked.

So the string is built here, from values read out of the swap row, and the
browser's only job is to hand it to a wallet. The decision is in Python where a
test can assert on it; the browser holds no logic that could be wrong.

THE FORMAT, AND THE PARTS DELIBERATELY NOT USED.

    solana:<recipient>?amount=<decimal>&memo=<text>&label=<text>&message=<text>

  recipient   the shared deposit account. For a native SOL transfer this is the
              wallet address, NOT an associated token account -- there is no
              spl-token parameter here and that absence is deliberate: this
              terminal takes native SOL, and chains/solana.py's validate_address
              refuses an off-curve key for exactly the reason an ATA must not
              appear in a native request.
  amount      in SOL, not lamports, per the Solana Pay specification. Rendered
              from a Decimal and never from a float's repr -- see
              _decimal_amount() for the measurement behind that.
  memo        the bare tag. chains/solana_memo.deposit_tag_from() reads the
              WHOLE memo as the discriminator, so "swap 7" or "tag=7" does not
              match and the payment strands at a shared account. The one place
              that could decorate it is here, and it does not.
  label       the terminal's name, which wallets show as the payee. Cosmetic and
              carries no authority: a wallet displays whatever it is given, so
              this is a courtesy to the human approving, never a claim.

  NOT USED -- reference    Solana Pay's `reference` is a pubkey a merchant polls
              to find the transaction. This terminal already has a discriminator
              that works for every tag chain (the memo), and
              services/deposit_service.attributable_events() joins on it. A
              second attribution mechanism for one chain is rule 8's duplicate
              with the delay already expired -- two answers to "whose deposit is
              this", and the one that loses an argument costs a customer.
  NOT USED -- spl-token    see `recipient` above.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

#: The URI scheme wallets register for payment requests.
SOLANA_PAY_SCHEME = "solana"

#: What wallets show as the payee. A courtesy to the human approving the
#: transaction and not a claim about anything -- a wallet renders whatever
#: string it is handed, so nothing may be inferred from it.
DEFAULT_LABEL = "swap_terminal"


def _decimal_amount(amount) -> str:
    """The amount as a plain decimal string. NEVER a float's repr.

    `str(0.01)` happens to be "0.01", and that is luck rather than a property.
    Two shapes break it, and both are reachable from a swaps row:

        a small amount         str(1e-07) is "1e-07", which is not a decimal
                               number to any URI parser. SOL has 9 decimals, so
                               0.0000001 SOL is an ordinary quantity here.
        a repeating binary     str(0.1 + 0.2) is "0.30000000000000004". A quote
        fraction               priced to an awkward figure can land one of
                               these in expected_input_amount.

    Decimal(str(amount)) rather than Decimal(amount): the former takes the
    shortest decimal that round-trips the float, which is the number a human saw
    on the page. Decimal(float) would expand the full binary expansion --
    0.01 becomes 0.01000000000000000020816681711721685... -- and a wallet asking
    its owner to approve that is a wallet nobody approves.

    normalize() drops trailing zeros so 1.0 becomes "1" rather than "1.0", and
    format(value, "f") is what turns an exponent back into positional notation.
    normalize() produces exponents in BOTH directions -- "1E-7" for 0.0000001
    and "1E+1" for 10 -- and the "f" presentation type handles both.

    MEASURED WHILE WRITING THIS, and recorded because the first version was
    wrong in a way that looked careful. It special-cased an integral amount with
    `str(value.to_integral_value())`, reasoning that an integer would otherwise
    normalize to an exponent. It does -- and to_integral_value() on an ALREADY
    NORMALIZED Decimal("1E+1") returns Decimal("1E+1"), so the branch written to
    avoid "1E+1" returned exactly that. The check that caught it was running the
    function over 0.01, 1e-07, 0.1+0.2, 10, 0.5 and 1.0 and reading the output,
    which took less time than the paragraph defending the branch.

    format(value, "f") alone is correct for every one of those, so the branch is
    gone rather than fixed (rule 9: the helper whose only caller was wrong).
    """
    return format(Decimal(str(amount)).normalize(), "f")


def payment_uri(account: str, amount, memo, label: str = DEFAULT_LABEL) -> str:
    """The URI a wallet is handed, or raise. Pure.

    Raises ValueError on an empty account or a missing memo rather than
    producing a URI that is missing half the instruction. On a shared deposit
    account a payment with no memo is the `deferred` case
    chains/solana.py reports and cannot credit -- so a request that would
    produce one is refused here, where the cost is an exception, instead of
    on chain, where the cost is the deposit.

    The memo is REQUIRED and has no default. Every chain this function serves
    attributes by memo, and a default would make the one mandatory half
    omissible by forgetting it -- rule 19's test for a patch applied to a
    signature.
    """
    account = (account or "").strip()
    if not account:
        raise ValueError(
            "a Solana Pay request needs the deposit account, and none was given. A URI without a "
            "recipient is not a payment request; it would open a wallet with nothing to approve."
        )
    if memo is None or str(memo).strip() == "":
        raise ValueError(
            f"a Solana Pay request for {account} needs the memo tag, and none was given. That account "
            f"is shared by every swap, so a payment carrying no memo cannot be matched to one -- it is "
            f"the case services/unattributable_deposit_service.py records and a person has to resolve "
            f"by hand. Refusing here costs an exception; not refusing costs the deposit."
        )
    query = [
        f"amount={_decimal_amount(amount)}",
        # quote() with no safe characters, because a memo is an integer today and
        # a label is prose: anything that is not unreserved gets escaped rather
        # than trusted to be harmless in a query string.
        f"memo={quote(str(memo).strip(), safe='')}",
        f"label={quote(label, safe='')}",
    ]
    return f"{SOLANA_PAY_SCHEME}:{account}?{'&'.join(query)}"


def describe(uri: str) -> str:
    """One line saying what the URI does, for a page or a diagnostic (rule 14).

    Separate from payment_uri() so that the string handed to a wallet carries no
    explanatory text, and the explanation carries no authority. A reader of a
    page sees both; a wallet sees only the first.
    """
    return (
        f"{uri}  <- a payment REQUEST. Opening it asks your wallet to send that amount, to that "
        f"account, with that memo. Nothing is sent until you approve it in the wallet, and this "
        f"terminal never sees your key."
    )
