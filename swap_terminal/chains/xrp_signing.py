"""Every decision the XRP payout path makes, as functions with seeded inputs.

Role: function layer (rule 10 -- the smallest testable pieces, called by
      chains/xrp.py's send_to_address() and by xrp_send_tagged.py)
Reads: nothing. Arguments only. No environment, no file, no socket, and NO KEY
      PATH -- a seed arrives as an argument from whoever already had it.
Writes: nothing. Not one function here opens a socket or submits anything;
      build_payment() returns an UNSIGNED transaction object.
Can move funds: NO. Every function here either refuses or returns a value.
      The one function that touches a signing library is derive_and_check(),
      and it derives a PUBLIC address in order to refuse -- it signs nothing.
      Signing and submission live in chains/xrp.py, which calls these guards
      first.
Mainnet-safe: yes. require_non_mainnet() is the function whose whole job is
      making the rest of the path unreachable on mainnet, and it is pure: it
      decides from a network id the caller read off the server, so it can be
      exercised against every id including 0 without a network.

WHY THESE ARE HERE AND NOT IN chains/xrp.py

Rule 10: the thing that actually decides should be the smallest, most testable
piece at the bottom. On this path the decisions are "is this mainnet", "did the
caller arm this explicitly", "does the seed match the account we announced",
"does this payment breach the reserve" and "is the partial-payment bit set" --
five questions, five functions, each callable with seeded arguments and each
mutation-checkable on its own. Buried inside a method that also opens sockets,
the only way to test any of them is to run the whole send.

WHY derive_and_check() MOVED HERE (rule 8)

It was written in xrp_send_tagged.py on 2026-09-26 for the testnet fixture.
chains/xrp.py's payout path needs the identical guard, and rule 8 is explicit
that two copies of one rule is a bug with a delay on it: the copies agree on
the day they are written and drift from then on, invisibly, because each looks
correct in its own file. So this is the survivor and it owns the concept.
xrp_send_tagged.py imports it; its three tests in tests/test_xrp_send_tagged.py
call it through that import and are unchanged.

THE THREAT MODEL THE DERIVATION GUARD ANSWERS, restated because it is the whole
reason local signing is safe to add at all:

rippled's legacy `submit` takes tx_json PLUS a `secret`, and the SERVER derives
the key, compares it against the `Account` field, and rejects a mismatch. When
we sign locally, WE choose which account the transaction claims. A seed paired
with the wrong address therefore signs a Payment debiting an account nobody
announced -- the operator reads "from rABC..." in a preview and a different
account is debited. Nothing in the ledger's rules stops that; the only thing
that stops it is deriving the address from the seed and refusing on a mismatch.

AND THE MESSAGE NAMES ADDRESSES, NEVER THE SEED. An error message is exactly
where a secret leaks, because the debugging impulse on a key mismatch is to
print the key. Both public addresses go in the text; the seed never does, and
tests/test_xrp_adapter.py and tests/test_xrp_send_tagged.py each assert the
absence rather than trusting it.

WHAT IS DELIBERATELY NOT HERE

No key path, no key file reader, no environment variable name. This module
cannot find a seed; it can only be handed one. That is what keeps
chains/xrp.py's promise ("holds no key, reads no key path") true even though
the adapter can now sign: the seed has to come from a caller who already had
it, which on this tree is the operator running a fixture, and NOT from the
payout worker, which calls send_to_address() with two positional arguments and
therefore gets a refusal.
"""

from __future__ import annotations

from .xrp_units import to_drops

# MAINNET, IDENTIFIED BY THE NETWORK'S OWN ID RATHER THAN BY A HOSTNAME.
#
# Established from xrp_send_tagged.py, which carried this constant and used it
# in refuse_mainnet() -- confirmed against a live server on 2026-09-25 from the
# operator's host: s.altnet.rippletest.net reports network_id 1, and mainnet is
# 0. The value is not re-derived here; the constant moved with the function so
# there is exactly one spelling of it in the tree (rule 8).
#
# A URL is not evidence. "s.altnet.rippletest.net" is a hostname, and a
# hostname resolves to whatever DNS says today -- an operator's /etc/hosts, a
# split-horizon resolver or a typo'd copy of a config can point it at a mainnet
# validator while every log line in the run still prints the testnet name. The
# SERVER's own network id cannot be mistaken by the client, which is why the
# check is on the id and the URL is only echoed for the reader.
MAINNET_NETWORK_IDS = frozenset({0})

# THE ARMING TOKEN. Sending requires this exact string at the call site.
#
# A boolean was the obvious choice and it is the wrong one. `send=True` can
# arrive from a truthy variable, a parsed config value, a `**kwargs` splat or a
# positional argument that drifted one place left, and every one of those is a
# send nobody wrote. An exact string match has no accidental spelling: the only
# way to pass it is to have typed it, which is what "explicit, unambiguous
# opt-in" has to mean on a path that moves money it cannot get back.
#
# It also reads correctly in a diff and in a grep. `grep -rn CONFIRM_XRP_SEND`
# finds every site in the tree that can send XRP, which a `True` never would.
CONFIRM_XRP_SEND = "I-HAVE-READ-THE-PREVIEW-AND-AUTHORIZE-THIS-XRP-SEND"

# A CONSERVATIVE FEE ALLOWANCE FOR THE PRE-FLIGHT RESERVE CHECK ONLY.
#
# NOT the fee that gets paid. xrpl-py's submit_and_wait() autofills the real
# fee from the server at submit time, which is correct -- the XRPL fee floats
# with load and a compiled-in number would be wrong during a fee escalation.
# This figure exists so the reserve check can subtract SOMETHING before
# signing, and it is deliberately larger than the 10-drop base fee this ledger
# has charged for years, so the check errs toward refusing a payment that would
# have just fit rather than toward permitting one that would not.
#
# NOT MEASURED AGAINST A SERVER FROM HERE. server_info reports
# validated_ledger.base_fee_xrp, and chains/xrp.py reads it when present and
# says on screen which figure it used (rule 14). That field was NOT among the
# ones confirmed live on 2026-09-25, so this is the fallback and the preview
# line names it as such rather than letting the reader assume it was read.
FEE_ALLOWANCE_DROPS = 1_000

# THE PARTIAL PAYMENT BIT, tfPartialPayment.
#
# MEASURED against the installed xrpl-py 5.2.0:
# xrpl.models.transactions.payment.PaymentFlag.TF_PARTIAL_PAYMENT == 131072
# (0x00020000). Written as the literal here with the derivation beside it
# rather than imported, because importing it would make this module -- which
# every guard in the tree imports -- depend on the optional signing library.
#
# WHY IT MATTERS ON THE SEND SIDE, which is the opposite of why it matters on
# the receive side. chains/xrp_payments.py defends the RECEIVE side by reading
# meta.delivered_amount and never Amount, because a payer who sets this flag
# can make a transaction that says "Amount: 1000 XRP" and delivers one drop.
# On the SEND side the same flag turns Amount from an exact figure into a
# CEILING: the ledger may deliver less and still return tesSUCCESS. A payout
# with this bit set can therefore under-pay a customer and be recorded as a
# successful broadcast, with a real hash and a real tesSUCCESS, and nothing in
# the payouts table saying the customer got less than they were owed.
#
# So it must never be set on a payout. MEASURED, not assumed: a Payment built
# with account/destination/destination_tag/amount and nothing else has
# flags=None and serializes with no `Flags` key at all under to_xrpl(), so the
# bit is absent by default. refuse_partial_payment() is the guard that keeps it
# absent if a future caller starts passing flags, because "absent by default"
# is a property of today's call site and not of the ledger.
TF_PARTIAL_PAYMENT = 0x00020000


class XRPSigningRefused(RuntimeError):
    """A guard on the XRP payout path refused. Nothing was signed or submitted.

    RuntimeError rather than a new root, for one measured reason: this is the
    type xrp_send_tagged.py's derive_and_check() already raised, and
    tests/test_xrp_send_tagged.py asserts on it. Moving the function must not
    change what its callers catch (rule 2: a test dies with the code it pins or
    changes to pin the stronger invariant -- neither applies to a pure move).

    Every subclass below is a DISTINCT guard, and they are distinct classes
    rather than one exception with a message because an operator reading a
    failure needs to tell "this is mainnet" from "you did not arm it" from "the
    seed is for another account" from "this would breach the reserve". Those
    four call for four different next actions, and three of them are not
    "retry".
    """


class XRPMainnetRefused(XRPSigningRefused):
    """The server reports a mainnet network id, or will not say which it is."""


class XRPSendNotArmed(XRPSigningRefused):
    """send_to_address() was called without the exact arming token, or with no seed."""


class XRPReserveRefused(XRPSigningRefused):
    """The payment would leave the account below its reserve, so the ledger would reject it."""


class XRPPartialPaymentRefused(XRPSigningRefused):
    """The transaction carries tfPartialPayment, which would let it under-deliver."""


def require_non_mainnet(network_id, url: str = "") -> str:
    """Refuse unless the SERVER says it is on a non-mainnet network.

    Returns a one-line description for the preview (rule 14: always print the
    network). Raises XRPMainnetRefused otherwise.

    THREE REFUSALS, NOT ONE, and the second and third are the interesting ones:

      a mainnet id      network_id in MAINNET_NETWORK_IDS. The obvious case.
      no id at all      network_id is None, which is what a server that did not
                        report the field looks like. Refused, because "I could
                        not read the network" is not "this is not mainnet"
                        (rule 17's general form, applied to the one question on
                        this path where guessing wrong is unrecoverable). An
                        older rippled that omits network_id will therefore
                        refuse to pay, and that is the correct direction: the
                        remedy is a server that answers, not a default.
      an unreadable id  anything that will not convert to an integer. Same
                        reasoning -- a field of an unexpected shape means the
                        response is not what this path expects, and the safe
                        reading of an unexpected response is not "permitted".

    NOTE WHAT IS NOT CHECKED: the url. It is echoed into the description so the
    reader can see where the answer came from, and it is not consulted for the
    decision. See MAINNET_NETWORK_IDS above for why a hostname is not evidence.
    """
    if network_id is None:
        raise XRPMainnetRefused(
            f"the server at {url or '(url not given)'} did not report a network_id, so this path CANNOT "
            f"establish that it is not mainnet. NOTHING was signed and nothing was submitted. Not "
            f"reading the network is not the same as reading a safe one -- the field is what this guard "
            f"is, and without it there is no guard. Point this at a server that reports network_id."
        )
    try:
        identifier = int(network_id)
    except (TypeError, ValueError) as error:
        raise XRPMainnetRefused(
            f"the server at {url or '(url not given)'} reported network_id={network_id!r}, which is not "
            f"an integer. NOTHING was signed. A response field of an unexpected shape means this is not "
            f"the response this path was written against, and the safe reading of that is 'refuse'."
        ) from error
    if identifier in MAINNET_NETWORK_IDS:
        raise XRPMainnetRefused(
            f"the server at {url or '(url not given)'} reports network_id {identifier}, which is "
            f"MAINNET. NOTHING was signed and nothing was submitted. This is checked against the id the "
            f"SERVER reports and not against the hostname, because a hostname resolves to whatever DNS "
            f"says today -- so a url reading 'testnet' is not evidence. There is no flag, environment "
            f"variable or argument that turns this off: mainnet payouts are the operator's decision and "
            f"they are not made here (CLAUDE.md rule 16)."
        )
    return f"network_id {identifier} (NOT mainnet; mainnet is {sorted(MAINNET_NETWORK_IDS)}) via {url or 'the configured url'}"


def require_send_confirmation(token: str, seed: str) -> None:
    """Refuse unless the caller armed this send explicitly AND supplied a seed.

    Both halves in one function because they are one question -- "did somebody
    mean for money to leave" -- and splitting them would let a call site pass
    the token with no seed and get a confusing failure two guards later.

    The token is compared for EXACT equality against CONFIRM_XRP_SEND. Not a
    prefix, not a case-insensitive match, not `in`: xrp_send_tagged.py already
    carried a substring-matching bug this repository wrote a test about
    (`"ignInvalid" in str(status)`), and a substring test on an arming token is
    that defect on the side where it costs money rather than an exit code.

    THE DEFAULT IS THE REFUSAL, which is the property that matters. A caller
    that passes neither argument -- services/payout_service.py:219 calls
    `send_to_address(address, amount)` with two positional arguments and
    nothing else -- lands here and is refused. A forgotten opt-in cannot
    degrade into a send.
    """
    if token != CONFIRM_XRP_SEND:
        raise XRPSendNotArmed(
            f"this XRP send was NOT armed, so nothing was signed and nothing was submitted. "
            f"send_to_address() previews by default and requires confirm_send={CONFIRM_XRP_SEND!r} "
            f"spelled exactly, at the call site, to submit anything. "
            f"{'No token was passed.' if not token else 'The token passed did not match.'} A boolean "
            f"was deliberately not used: a truthy variable, a parsed config value or a positional "
            f"argument that drifted could all produce a send nobody wrote."
        )
    if not seed:
        raise XRPSendNotArmed(
            "this XRP send was armed but no signing seed was supplied, so nothing was signed. The "
            "adapter holds no key and reads no key path -- deliberately, so that configuration alone "
            "can never arm a payout -- which means a seed has to be handed in by a caller that already "
            "had it. The seed's VALUE is never printed by anything on this path."
        )


def derive_and_check(secret: str, announced: str):
    """Derive the signing wallet and REFUSE if it is not the account we announced.

    Moved here from xrp_send_tagged.py on 2026-09-26 so that the testnet
    fixture and chains/xrp.py's payout path share ONE copy (rule 8). The
    fixture now imports it; nothing else defines it.

    See this module's docstring for the threat model at length. In one
    sentence: server-side `submit` sends the secret and `Account` separately so
    the server rejects a mismatch, while local signing lets US choose the
    account the transaction claims, so a seed paired with the wrong address
    signs a Payment debiting an account nobody announced.

    That is not hypothetical bookkeeping. xrp_send_tagged.saved_faucet_accounts()
    reads the address and the secret from separate key names over two nesting
    levels, so nothing structurally guarantees the two came from the same faucet
    file -- and on the adapter path the address arrives as a function argument
    while the seed arrives as a different one, which is weaker still.

    Returns the wallet. Raises XRPSigningRefused -- a RuntimeError, which is
    what the pre-move callers and their tests already catch -- with the mismatch
    named. The two ADDRESSES are public and are in the message because they are
    what the operator needs; the seed is not and is never in the text.
    """
    # Imported here, not at the top, and PLC0415 suppressed with the reason.
    # xrpl-py is an OPTIONAL dependency: every other guard in this module, the
    # whole preview path, and the entire test suite's collection work without
    # it. A module-level import would make a signing library mandatory to
    # import chains/xrp.py -- and chains/registry.py imports that
    # unconditionally, so it would become mandatory to start a read-only
    # deposit watcher. The same judgment is recorded at
    # tests/test_gridcoin_rpc_is_configured.py and was recorded here before the
    # move.
    from xrpl.wallet import Wallet  # noqa: PLC0415 -- checked: optional dependency; see above

    wallet = Wallet.from_seed(secret)
    if wallet.classic_address != announced:
        raise XRPSigningRefused(
            f"the seed supplied derives {wallet.classic_address}, not the "
            f"{announced} this run announced. REFUSING to sign: the address and the "
            f"seed reach this function through separate arguments and may not be the same "
            f"account. Nothing was submitted."
        )
    return wallet


def reserve_drops(base_reserve_xrp, owner_reserve_xrp=None, owner_count=None) -> tuple[int, str]:
    """The drops this account must retain, and a line saying how it was computed.

    An XRPL account's reserve is the BASE reserve plus the owner reserve
    increment for each ledger object it owns (trust lines, offers, escrows,
    tickets). Both figures are NETWORK PARAMETERS read from
    server_info.validated_ledger, never hardcoded: chains/xrp_units.py explains
    at length that the base reserve has been 20 XRP, then 10, then 1, so a
    compiled-in number would eventually make this terminal believe it has
    spendable balance it does not.

    WHEN owner_count IS None the reserve is computed as the base alone, and the
    returned description SAYS SO, in those words, because that figure
    UNDERSTATES the true reserve for any account that owns ledger objects. The
    alternative -- refusing outright when account_info omits OwnerCount --
    would block a payout over a missing field rather than over a real
    shortfall, and the shortfall it could miss is caught by the ledger itself
    (tecINSUFFICIENT_RESERVE), which submit_and_wait() surfaces as a failure
    rather than as a send. Rule 17: OwnerCount is a required AccountRoot field
    and this path expects it to be present, but it was NOT read off a live
    server from the container this was written in, so the absent case is
    handled and labeled instead of assumed away.
    """
    base = to_drops(base_reserve_xrp)
    if owner_count is None or owner_reserve_xrp is None:
        return base, (
            f"{base} drops = base reserve only. OwnerCount or reserve_inc_xrp was ABSENT from the "
            f"server's response, so any owner reserve is NOT included and this figure UNDERSTATES the "
            f"true reserve for an account that owns ledger objects."
        )
    increment = to_drops(owner_reserve_xrp)
    owned = int(owner_count)
    total = base + increment * owned
    return total, (
        f"{total} drops = {base} base + {increment} x {owned} owned objects, both figures read from "
        f"server_info.validated_ledger rather than hardcoded"
    )


def require_reserve_headroom(
    balance_drops: int,
    send_drops: int,
    fee_drops: int,
    required_reserve_drops: int,
) -> str:
    """Refuse a payment that would leave the account below its reserve.

    Returns a one-line arithmetic summary for the preview. Raises
    XRPReserveRefused when the payment does not fit.

    ALL FOUR ARGUMENTS ARE INTEGER DROPS and the arithmetic is integer
    throughout, so there is no float on the money path at any point (rule: XRP
    amounts are drops, six decimals, and chains/xrp_units.py owns the
    conversion). A reserve check done in XRP floats could refuse a payment that
    fits, or permit one that does not, by one drop in either direction -- and
    the direction would depend on where the binary representation happened to
    land, which is the failure chains/monero_units.py documents at length.

    WHY REFUSE HERE WHEN THE LEDGER WOULD ALSO REFUSE. The ledger's refusal is
    a tecUNFUNDED_PAYMENT or tecINSUFFICIENT_RESERVE, which arrives AFTER the
    transaction reached the ledger and CLAIMED A FEE, and which reads to an
    operator as an opaque four-letter code. Refusing before signing costs
    nothing, names the shortfall in drops, and leaves no transaction behind.
    That is rule 14's "state what the number means, next to the number" applied
    to the one number that decides whether a payout is possible.
    """
    remaining = balance_drops - send_drops - fee_drops
    summary = (
        f"balance {balance_drops} - send {send_drops} - fee allowance {fee_drops} = {remaining} drops "
        f"remaining, against a required reserve of {required_reserve_drops} drops"
    )
    if remaining < required_reserve_drops:
        raise XRPReserveRefused(
            f"this payment would breach the account's reserve, so NOTHING was signed and nothing was "
            f"submitted. {summary} -- short by {required_reserve_drops - remaining} drops. Every funded "
            f"XRPL account must retain its reserve; the ledger would reject this with a tec* code that "
            f"had already claimed a fee, so it is refused here instead. The reserve figures come from "
            f"server_info, not from a constant in this repository."
        )
    return f"{summary} -- fits, with {remaining - required_reserve_drops} drops of headroom"


def refuse_partial_payment(serialized: dict) -> None:
    """Refuse a transaction carrying tfPartialPayment. Takes Payment.to_xrpl().

    See TF_PARTIAL_PAYMENT above for why this bit is the one flag that must
    never appear on a payout: it turns Amount from an exact figure into a
    ceiling, so the ledger may deliver less and still return tesSUCCESS, and
    the payouts row would record a real hash for an under-payment.

    Takes the SERIALIZED dict rather than the Payment object, and that is the
    point rather than convenience. `Flags` in the serialized form is what
    actually goes to the ledger, so checking it verifies the transaction that
    will be signed rather than the arguments somebody intended -- the
    behavioral-verification principle ("never accept 'the code contains a check
    for X' as evidence X is enforced") applied inside one function. A check on
    the constructor's keyword argument would pass while a flag set any other
    way sailed through.

    Absent `Flags` is the normal case and is fine: measured against xrpl-py
    5.2.0, a Payment built with account/destination/destination_tag/amount
    serializes with no Flags key at all.
    """
    flags = serialized.get("Flags")
    if flags is None:
        return
    try:
        bits = int(flags)
    except (TypeError, ValueError) as error:
        raise XRPPartialPaymentRefused(
            f"the transaction's Flags field is {flags!r}, which is not an integer, so whether "
            f"tfPartialPayment is set CANNOT be established. NOTHING was signed. An unreadable flags "
            f"field is not an absent one."
        ) from error
    if bits & TF_PARTIAL_PAYMENT:
        raise XRPPartialPaymentRefused(
            f"the transaction carries tfPartialPayment (Flags={bits}, bit {TF_PARTIAL_PAYMENT}), so "
            f"NOTHING was signed. With that bit set the ledger may deliver LESS than Amount and still "
            f"return tesSUCCESS -- a payout that under-pays the customer while recording a real hash "
            f"and a successful result. This is the send-side half of the exploit "
            f"chains/xrp_payments.py defends against on the receive side by reading "
            f"meta.delivered_amount and never Amount."
        )
