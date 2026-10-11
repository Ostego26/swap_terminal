"""One address, both verdicts, for the field the customer is typing into.

Role: submodule -> function (live_address_verdict is the decision)
Reads: the destination adapter (at most TWO read-only RPC calls through
       chains/base.RPCAdapter.address_report, or the chain's own validator),
       and modules/address_authority's per-chain decoders. No database.
Writes: nothing
Can move funds: no. It REFUSES or PERMITS a payout address before a swap exists,
       which is the same question services/swap_service.
       refuse_unusable_payout_address() asks at create time -- and it is the SAME
       code answering it, deliberately. See WHY ONE FUNCTION below.
Mainnet-safe: yes. Read-only; it signs nothing, derives no key, and writes no row.

=============================================================================
WHY THIS EXISTS. Operator, 2026-10-11:
=============================================================================

    "any address must be verfied on the UI in situ after being entered with a
     green check or a red x and not being able to proceed. it should be both a
     regex and a blockchain check."

BOTH HALVES ALREADY EXISTED AND NOTHING JOINED THEM. The local decode is
modules/address_authority.check_address() -- the "regex" half, though it is
base58check and bech32 rather than a pattern. The chain half is the daemon's own
validateaddress. Until now the customer met both for the first time AFTER
pressing submit, on the server, as a sentence on an error page.

WHY ONE FUNCTION AND NOT A SECOND OPINION FOR THE BROWSER. templates/
_atm_payout_address.html already carried the right instinct and the wrong
conclusion: "NO BROWSER VALIDATION PATTERN. A pattern here would be a third
spelling of the per-chain address rule, in a language the server cannot check."
That is correct and this does not break it -- the browser gets NO address rules.
It asks the server, and the server answers with the same two checks create_swap
will run, so a green check cannot disagree with the thing that happens next. A
green check that is followed by a refusal is worse than no check at all.

WHY A GREEN CHECK IS WORTH THE ROUND TRIP, and the case is already in this tree.
From _atm_payout_address.html, the operator's host 2026-10-01: Brave autofilled
the payout field with a stale Bitcoin testnet address, which passed every check
the server had -- Gridcoin shares Bitcoin testnet's 0xc4 P2SH version byte and
validateaddress returned isvalid: true -- and 82.65 tGRC was broadcast to an
address nobody held the key for. A verdict the customer can SEE next to the
field, before they commit, is the only point in the flow where that is still
recoverable.

=============================================================================
THREE STATES, NOT TWO. The operator asked for a green check or a red x; a
daemon that does not answer is neither, and rendering it as either is a lie.
=============================================================================

  GOOD      both halves answered yes. Green check. May proceed.
  BAD       a half answered no. Red x, with the reason. May NOT proceed, and
            asking again will not help -- the address is wrong.
  UNKNOWN   the chain could not be asked. NOT a red x: the address may be
            perfect and our daemon is down. May NOT proceed either, because a
            swap created now would choke exactly where the operator's GRC -> LTC
            attempt choked -- but the message must send somebody to the DAEMON
            and not to the customer's clipboard.

That third state is the whole reason this module exists rather than a boolean
endpoint. chains/base.py has been burned twice by collapsing it: validate_address()
returned False for a daemon that was down until 2026-09-24, so an outage read as a
bad customer address, and owns_address() returned None for both a transport failure
and a daemon with no `ismine` field until 2026-10-01.

OURS IS A FOURTH ANSWER AND IT IS A WARNING, NOT A REFUSAL on the chains that can
answer it. A payout address inside the desk's own wallet is the 2026-10-04 incident
(swap s_ae76ec53236ffcf6, paid to XRP_DEPOSIT_ACCOUNT) and services/
custody_separation.payout_to_the_desk_refusal() owns that decision. This module
reports what it learns and lets that function refuse, rather than re-deciding it.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from modules.address_authority import check_address

logger = logging.getLogger(__name__)

#: The three verdicts the field can render. ASCII identifiers (rule 6's interface
#: boundary) and the UI picks the mark; this module never names a color or a glyph,
#: because a decision about what the customer may do must not be inferred from a
#: character in a template.
GOOD = "good"
BAD = "bad"
UNKNOWN = "unknown"


class LiveAddressVerdict(NamedTuple):
    """What the field should show, and whether Next may be pressed.

    `may_proceed` IS THE ONLY FIELD THE SUBMIT GATE READS, and it is False for both
    BAD and UNKNOWN. Separating it from `state` rather than letting the browser
    compute `state == GOOD` is deliberate: the gate is a fund-path decision and it
    belongs here, where tests/test_live_address_verdict.py can assert it, not in a
    comparison in JavaScript that a later edit can quietly widen.
    """

    state: str
    #: One sentence for the customer, next to the field. Never names a secret, never
    #: echoes anything but the address they typed.
    headline: str
    #: The local decode's reading: the state name and its own sentence.
    local_state: str
    local_why: str
    #: The chain's reading. None for `chain_ok` means NOBODY ANSWERED, never "no".
    chain_ok: bool | None
    chain_why: str
    #: Whether the address is in the desk's own wallet. None is not established.
    #: Reported, not refused on, here -- see the module header.
    ours: bool | None
    may_proceed: bool
    #: Which RPC methods were actually called. Carried so a test can pin the COST of
    #: a keystroke rather than trust a comment about it (rule 17).
    methods_called: tuple[str, ...] = ()


def live_address_verdict(adapters: dict, asset: str, address: str) -> LiveAddressVerdict:
    """The verdict for one address on one chain. NEVER RAISES.

    Every failure -- no adapter, a bad decode, an unreachable daemon, an adapter that
    throws something nobody anticipated -- comes back as a state and a sentence. That
    is what rule 12 requires of this shape: the caller can tell a failure from an
    answer because the failure IS the answer, and `may_proceed` is False for all of
    them.

    THE LOCAL HALF RUNS FIRST AND A REFUSAL SHORT-CIRCUITS, so a customer with a
    typo pays no RPC at all. That ordering is the same one create_swap uses and for
    the same stated reason -- "refused here, before any daemon was asked" -- which
    also means a field being typed into character by character cannot hammer a chain
    with one call per keystroke for strings that cannot possibly be addresses.
    """
    address = (address or "").strip()
    if not address:
        return LiveAddressVerdict(
            UNKNOWN, "Enter your address.", "EMPTY", "nothing was entered",
            None, "not asked -- there is nothing to ask about", None, False,
        )

    adapter = adapters.get(asset)
    if adapter is None:
        # NOT the customer's problem and the sentence must not read like it is.
        return LiveAddressVerdict(
            UNKNOWN,
            f"{asset} is not configured on this terminal, so your address cannot be checked. "
            f"This is not a problem with the address.",
            "NOT_ASKED", f"no {asset} adapter exists in this process",
            None, f"no {asset} adapter exists in this process", None, False,
        )

    local = check_address(asset, address)
    if local.refuses:
        return LiveAddressVerdict(
            BAD,
            f"That is not a usable {asset} address -- {local.why}",
            local.state, local.why,
            None, "not asked -- the local check already refused, so no daemon was troubled",
            None, False,
        )

    chain_ok, chain_why, ours, methods = _ask_the_chain(adapter, asset, address)
    if chain_ok is None:
        return LiveAddressVerdict(
            UNKNOWN,
            f"The {asset} daemon did not answer, so your address could not be confirmed. "
            f"This is NOT a statement about the address -- the terminal cannot reach {asset} "
            f"right now.",
            local.state, local.why, None, chain_why, ours, False, methods,
        )
    if not chain_ok:
        return LiveAddressVerdict(
            BAD,
            f"The {asset} daemon rejected that address. It decoded locally as "
            f"{local.network.lower()} -- check you copied the whole thing, and that it is "
            f"a {asset} address and not another chain's.",
            local.state, local.why, False, chain_why, ours, False, methods,
        )

    headline = f"Valid {asset} address."
    if ours is True:
        # STILL GOOD HERE. payout_to_the_desk_refusal() is the authority on whether an
        # address in our own wallet refuses a swap, and it reads differently per chain
        # family. Pre-empting it with a red x would mean two places deciding one thing
        # (rule 8) on the field that decides where money lands.
        headline = (
            f"Valid {asset} address -- but this terminal's own wallet holds it. "
            f"Use an address from YOUR wallet."
        )
    return LiveAddressVerdict(
        GOOD, headline, local.state, local.why, True, chain_why, ours, True, methods,
    )


def _ask_the_chain(adapter, asset: str, address: str):
    """(chain_ok, chain_why, ours, methods_called), asking the chain ONCE where it can.

    `address_report` IS ASKED FOR RATHER THAN ASSUMED. Only chains/base.RPCAdapter has
    it, and it answers validity and ownership from a single pass -- see
    chains/base.AddressReport for the 2026-10-11 measurement where asking the two
    questions separately cost four round trips and killed a gunicorn worker. XRP, SOL
    and ICP have no such pass and no `ismine` at all, so they take the fallback and
    cost one call.

    THE BROAD CATCH IS THE POINT OF THE FUNCTION rather than a concession: this is the
    boundary that turns every possible adapter failure into the UNKNOWN state, and the
    reason is returned, not swallowed. An uncaught exception here would reach the
    customer as a 500 on a keystroke, which is the failure mode this whole module was
    written to end.
    """
    combined = getattr(adapter, "address_report", None)
    if callable(combined):
        try:
            report = combined(address)
        except Exception as exc:  # noqa: BLE001 -- checked: address_report() is documented never to raise, so reaching this means something below it did something undocumented. It becomes UNKNOWN with the reason attached rather than a 500 on a keystroke, and the WARNING below is what makes it visible instead of silent.
            logger.warning(
                "live address check for %s raised through address_report(), which is documented "
                "never to raise: %s  <- reported to the customer as 'could not be confirmed', "
                "not as a bad address",
                asset, exc,
            )
            return None, f"{type(exc).__name__}: {exc}", None, ()
        return report.valid, report.why_valid, report.mine, report.methods_called

    try:
        valid = bool(adapter.validate_address(address))
    except Exception as exc:  # noqa: BLE001 -- checked: validate_address() RAISES by contract when the daemon could not be asked (chains/base.py, fixed 2026-09-24), and that is precisely the UNKNOWN state rather than a refusal. Catching it here is how "nobody answered" stays distinguishable from "no"; the reason is returned.
        return None, f"{type(exc).__name__}: {exc}", None, ()

    # OWNERSHIP IS ASKED SEPARATELY ONLY ON THESE CHAINS, where it costs no RPC:
    # chains/xrp.py, chains/solana.py and chains/icp.py all override owns_address() to
    # answer None without a call, because those ledgers have no `ismine`.
    try:
        ours = adapter.owns_address(address)
    except Exception:  # noqa: BLE001 -- checked: ownership is a WARNING-level fact, never a refusal (see the module header), so a failure to establish it must not change the address verdict the customer sees. None is already this field's "not established".
        ours = None
    return valid, "the chain's own validator answered", ours, ()
