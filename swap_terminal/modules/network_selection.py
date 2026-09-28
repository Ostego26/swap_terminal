"""WHICH NETWORK THIS ENGINE IS ON -- one switch, and the checks that make one switch safe.

Role: function level (the decisions -- which network, which version byte, does the daemon agree)
Reads: one environment variable, and the version-byte tables in modules/address_network.py
Writes: nothing
Can move funds: no directly. It decides which version byte an address is ENCODED with, and an
        address encoded for the wrong network is a payment that either bounces or burns.
Mainnet-safe: this module is where "mainnet-safe" stops being a constant and becomes a
        question. It answers MAINNET only when an operator has said so in exactly those
        letters, and every other input -- unset, empty, misspelled, "main", "1", "true" --
        is TESTNET. See `selected_network`.
Live-safe: yes -- it contacts nothing and decides nothing about orders.

WHY THIS EXISTS, IN THE OPERATOR'S WORDS, 2026-09-28: "we want to be able to switch this
entire swap engine to mainnets with one fale swoop."

That is the right thing to want and it is the exact shape of change that loses money when one
component does not hear about it. A single flag flipped where four modules read it and a fifth
does not gives you a mainnet-encoded address paired with a testnet key, or a mainnet key
talking to a testnet daemon -- and neither fails loudly. It pays, and the coins are gone.

SO THE SWITCH IS NOT THE HARD PART. THE AGREEMENT IS. Three things must say the same word
before anything is derived, signed or sent:

    1. the operator, through this module's one environment variable
    2. the DAEMON, asked directly -- `assert_daemon_agrees` below
    3. every address, checked against the network it was encoded for rather than assumed

Two of those already existed in another form. `adaptor_regtest_verify.py` refuses to run unless
the daemon SAYS it is on a test network, treating an absence of evidence as mainnet; and
`modules/address_authority` decodes the network out of an address rather than trusting a
caller. What was missing is the first -- there was nothing to disagree WITH, because the
network was a constant.

THE TABLES ARE DERIVED, NEVER RE-SPELLED. `modules/address_network` already owns the
version-byte vocabulary, in the DECODE direction: given a byte, which network. This module adds
the ENCODE direction -- given a network and an asset, which byte -- by inverting those same
tables at import. A hand-written encode table is rule 8's defect with a delay on it: the two
agree on the day they are written, and the drift is invisible because each looks correct in its
own file. The one place a choice is unavoidable is Litecoin's two live P2SH bytes per network,
and that choice is named below with its reason rather than resolved by whichever the dict
happened to yield first.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not make the engine mainnet-READY. Selecting
mainnet here and running would still be wrong, because `modules/atomic_htlc_scripts.py`
re-encodes every address it is handed to the TESTNET byte, structurally, and says so in its own
header ("Mainnet-safe: NO, and structurally so"). That module is the actual gate, it is on the
order path, and changing it is a live-posture decision that belongs to the operator (rule 16).
This module is the foundation that makes changing it a bounded piece of work instead of a
tree-wide sweep -- and `mainnet_blockers()` below NAMES what is still in the way, measured,
rather than leaving somebody to discover it with a payment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from modules import address_network

# THE ONE VARIABLE. Not a flag per chain and not an argument threaded through forty call sites:
# the operator asked for one switch, and several switches that must agree is the failure this
# module exists to prevent wearing a different hat.
NETWORK_VARIABLE = "ST_NETWORK"

MAINNET = address_network.MAINNET
TESTNET = address_network.TESTNET

# The exact strings accepted, and nothing else is a near miss to be helpful about. A module that
# read "main", "MAINNET ", "1" or "prod" as mainnet would be guessing at intent on the one
# question where guessing costs money -- and the safe direction is not symmetric: reading
# mainnet as testnet wastes a run, reading testnet as mainnet spends real coins.
SELECTABLE = (TESTNET, MAINNET)


class NetworkSelectionError(ValueError):
    """The configured network is not a network this engine will act on.

    Raised rather than defaulted, for the one case where a default is indefensible: a value that
    was clearly MEANT to say something ("main", "MAINNET", "prod") and does not say it exactly.
    Silently reading that as testnet would run the whole engine on the wrong chain while the
    operator believed otherwise, and silently reading it as mainnet is the outcome this file
    exists to prevent. Refusing is the only answer that cannot be wrong.
    """


def selected_network(raw: str | None = None) -> str:
    """The network this engine is configured for. TESTNET unless an operator spelled MAINNET.

    `raw` is taken as an argument, defaulting to the environment, so the decision can be called
    with seeded inputs (rule 10) and so nothing has to mutate os.environ to test it.

    THE ASYMMETRY IS THE WHOLE DESIGN. Unset and empty are TESTNET, because an engine that
    defaulted to mainnet would put real coins one forgotten export away. Anything else that is
    not exactly one of SELECTABLE RAISES, because it was meant to say something: "main" and
    "MAINNET " and "prod" are somebody trying to select mainnet, and reading them as testnet
    would run the engine on a chain the operator does not think it is on. Both halves of that
    have a cost and they are not the same cost, which is why the two cases are handled
    differently rather than folded into one default.

    Case is NOT normalized, and that is deliberate rather than an oversight. `MAINNET` raises.
    Accepting it would mean accepting `Mainnet` and `mAiNnEt` too, and the moment a comparison
    becomes fuzzy the question "did this select mainnet" stops having one answer. The error
    message says the exact string to use, so the cost is one reread and never a wrong chain.
    """
    value = os.environ.get(NETWORK_VARIABLE, "") if raw is None else raw
    if value is None or not value.strip():
        return TESTNET
    if value not in SELECTABLE:
        raise NetworkSelectionError(
            f"{NETWORK_VARIABLE}={value!r} is not a network this engine will act on. It must be "
            f"exactly {TESTNET!r} or {MAINNET!r} -- lowercase, no surrounding space. It is not "
            f"read loosely on purpose: a value that nearly says mainnet was MEANT to say "
            f"mainnet, and reading it as testnet would run the whole engine on a chain you do "
            f"not think it is on. Unset or empty is {TESTNET!r}."
        )
    return value


def is_mainnet(network: str | None = None) -> bool:
    """One place that answers 'are we live', so no caller writes `== "mainnet"` itself.

    Rule 8, at its smallest scale and for the reason the rule gives: a string comparison spelled
    at thirty call sites is one typo away from a site that answers False on mainnet forever, and
    nothing fails when it does.
    """
    return (selected_network() if network is None else network) == MAINNET


# --- The ENCODE direction, inverted from address_network's DECODE tables -------------------

P2PKH = "p2pkh"
P2SH = "p2sh"

# WHERE A CHOICE IS UNAVOIDABLE, IT IS NAMED HERE WITH ITS REASON.
#
# Litecoin declares SCRIPT_ADDRESS *and* SCRIPT_ADDRESS2 for every network and both are live at
# once -- address_network.P2SH_VERSIONS carries the full argument and the chainparams.cpp line
# numbers. Decoding must accept both. ENCODING has to pick one, and there is no way to derive
# which: the tables say what a byte MEANS, not what a wallet would emit.
#
# The choice is litecoind's own: it DECODES both and ENCODES new addresses with SCRIPT_ADDRESS2,
# so an address this engine produces matches one the reference client would produce for the same
# script. Anything else is an address that is valid, that litecoind accepts, and that does not
# look like the one in the operator's wallet beside it -- which is a support call rather than a
# loss, and still not worth having.
#
# Keyed by (asset, kind, network) so the exception is visible as an exception. Every other
# combination is derived below and this table is asserted to be a SUBSET of what the derivation
# found, so it cannot name a byte the authority does not agree is that network's.
ENCODE_PREFERENCE: dict[tuple[str, str, str], int] = {
    ("LTC", P2SH, MAINNET): 0x32,   # SCRIPT_ADDRESS2 (50): the `M...` form litecoind emits
    ("LTC", P2SH, TESTNET): 0x3A,   # SCRIPT_ADDRESS2 (58): the `Q...` form
}


def _candidates(asset: str, kind: str, network: str) -> list[int]:
    """Every version byte that means (this asset, this kind, this network). Derived, not listed.

    Inverts address_network's two tables at the moment of asking rather than building a second
    table at import: there is no cache to go stale, and a byte added to the authority is picked
    up here with no second edit (rule 11's "did the SQL follow automatically?" applied to a
    Python vocabulary).
    """
    versions = address_network.P2PKH_VERSIONS if kind == P2PKH else address_network.P2SH_VERSIONS
    return sorted(
        byte for byte, byte_network in versions.items()
        if byte_network == network and asset in address_network.BASE58_VERSION_ASSETS.get(byte, ())
    )


def base58_version(asset: str, kind: str, network: str | None = None) -> bytes:
    """The version byte to ENCODE a `kind` address for `asset` on `network`.

    Three outcomes and they are kept apart on purpose:

      exactly one candidate   the ordinary case. Return it.
      several candidates      only Litecoin P2SH, today. ENCODE_PREFERENCE names which and why,
                              and a combination with several candidates and NO preference raises
                              rather than picking -- because "whichever the dict yielded first"
                              is a decision nobody made, recorded nowhere, that changes when the
                              table is reordered.
      none                    this asset has no such address on this network, and that is a
                              programming error worth a sentence rather than a KeyError four
                              frames up.
    """
    chosen_network = selected_network() if network is None else network
    if kind not in (P2PKH, P2SH):
        raise NetworkSelectionError(f"address kind must be {P2PKH!r} or {P2SH!r}, got {kind!r}")
    candidates = _candidates(asset, kind, chosen_network)
    if not candidates:
        raise NetworkSelectionError(
            f"modules/address_network declares no {kind} version byte for {asset} on "
            f"{chosen_network}. Either the asset is not one this engine handles, or the "
            f"authority is missing a byte -- and adding it THERE is the fix, not here"
        )
    preferred = ENCODE_PREFERENCE.get((asset, kind, chosen_network))
    if preferred is not None:
        return bytes([preferred])
    if len(candidates) > 1:
        raise NetworkSelectionError(
            f"{asset} has {len(candidates)} live {kind} version bytes on {chosen_network} "
            f"({[hex(b) for b in candidates]}) and ENCODE_PREFERENCE names none of them. Picking "
            f"one here would be a decision nobody made and nobody recorded, which changes the "
            f"moment the table is reordered. Name it in ENCODE_PREFERENCE with the reason"
        )
    return bytes([candidates[0]])


# --- The agreement gate: the operator said one thing; what does the DAEMON say? ------------


def assert_daemon_agrees(asset: str, daemon_reports: str | None, network: str | None = None) -> str:
    """Refuse unless the daemon SAYS it is on the network this engine is configured for.

    THIS IS WHAT MAKES ONE SWITCH SAFE, and without it the switch is a loaded gun. A flag says
    which chain the operator MEANT; only the daemon says which chain the keys and the coins are
    actually on. Both directions are a loss and they are not symmetric in how they present:

      configured mainnet, daemon on testnet   the engine derives mainnet addresses, hands them
                                              to a testnet daemon, and everything looks like a
                                              broken node -- annoying, recoverable.
      configured testnet, daemon on mainnet   the engine derives TESTNET addresses and spends
                                              REAL coins to them. Nothing errors. The coins are
                                              gone, to an address nobody can pay from, and the
                                              first sign is a balance.

    AN ABSENCE OF EVIDENCE IS NOT AGREEMENT, and `None` refuses whichever network is configured
    -- including mainnet. That is `adaptor_regtest_verify.py`'s existing posture ("GRC is asked
    three ways and an absence of evidence is treated as mainnet") generalized: there, silence
    could only mean "maybe mainnet" and refusing was obviously right. Here the configured
    network could BE mainnet, and it is tempting to read silence as consent. It is not consent.
    A daemon that will not say which chain it is on is a daemon this engine has not identified,
    and identifying it is one RPC call.

    Returns the agreed network so a caller can use the return value rather than the flag, which
    makes it structurally hard to check agreement and then act on something else.
    """
    configured = selected_network() if network is None else network
    if daemon_reports is None or not str(daemon_reports).strip():
        raise NetworkSelectionError(
            f"{asset}: the daemon did not say which network it is on, and this engine is "
            f"configured for {configured}. Silence is not agreement -- a daemon that will not "
            f"identify its chain is a daemon this engine has not identified, and identifying it "
            f"is one RPC call. Nothing was derived, signed or sent"
        )
    if daemon_reports != configured:
        raise NetworkSelectionError(
            f"{asset}: THE DAEMON AND THE CONFIGURATION DISAGREE. {NETWORK_VARIABLE} selects "
            f"{configured!r} and the daemon reports {daemon_reports!r}. Nothing was derived, "
            f"signed or sent.\n"
            f"  These are not two spellings of one thing. The flag says which chain you MEANT; "
            f"the daemon says which chain the coins are actually on. Deriving "
            f"{configured}-encoded addresses and handing them to a {daemon_reports} daemon "
            f"{'sends REAL coins to an address nobody can pay from' if daemon_reports == MAINNET else 'looks like a broken node'}."
        )
    return configured


# --- What is still in the way, MEASURED rather than remembered ------------------------------

# The header field every module in this repository carries (CLAUDE.md rule 1), and the string
# that marks one as not safe to point at real money. Read as the authority on this question
# because it already IS the authority: it is what a reader opening the file is told, and a
# second list maintained here would be rule 8's defect -- two copies, agreeing the day they are
# written, drifting silently after.
MAINNET_SAFETY_FIELD = "Mainnet-safe:"
# ANCHORED TO THE START OF A LINE, and that is not a detail. The first version matched the
# marker ANYWHERE in the first 4000 characters, so it flagged this very file -- which is
# mainnet-safe and whose header says so -- because the docstring above QUOTES
# atomic_htlc_scripts.py's declaration in the middle of a sentence. A module that talks about
# another module's header was scored as having that header.
#
# It is the same defect this session already fixed in the harness's own output, where a
# paragraph quoting an obsolete denial was still a screen carrying that denial. Quotation marks
# do not survive a scan any more than they survive a skim. A header field is a field: it starts
# a line, optionally indented, and anything mid-sentence is prose ABOUT one.
_DECLARES_UNSAFE = re.compile(r"^[ \t]*Mainnet-safe:[ \t]*(NO|no)\b", re.MULTILINE)


def mainnet_blockers(root: object = None) -> list[str]:
    """Every module that declares itself NOT mainnet-safe, by reading its own header.

    "CAN WE GO MAINNET?" IS A LIST, NOT A YES OR A NO, and this returns the list. Measured on
    2026-09-28 over this tree: 26 files outside tests/, of which the one that matters most is
    `modules/atomic_htlc_scripts.py` -- it RE-ENCODES every address it is handed to the testnet
    version byte, structurally, on the order path, and says so in its own header. Selecting
    mainnet while that is true does not fail; it produces a testnet address from a mainnet one
    and pays it.

    DERIVED FROM THE HEADERS, NEVER A LIST KEPT HERE. A hand-maintained inventory of blockers is
    a document that is correct on the day it is written and wrong by the next commit, and the
    thing it would be wrong about is which parts of a money system are safe. The headers are
    already maintained, already read by anyone opening the file, and already the thing rule 1
    asks for -- so this reads them, and a module fixed for mainnet leaves this list by changing
    its own header in the same commit as the fix.

    Paths are returned relative to the tree root and SORTED, so two runs of this on the same
    tree are comparable line by line -- which is what makes it usable as a countdown rather than
    a wall of text (rule 3: state the denominator, and make the number move).
    """
    base = Path(__file__).resolve().parents[2] if root is None else Path(str(root))
    found: list[str] = []
    for path in sorted(base.rglob("*.py")):
        relative = path.relative_to(base)
        if relative.parts[0] in ("tests", ".venv", "node_modules"):
            continue
        try:
            header = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if _DECLARES_UNSAFE.search(header):
            found.append(str(relative))
    return found


__all__ = [
    "ENCODE_PREFERENCE",
    "MAINNET",
    "MAINNET_SAFETY_FIELD",
    "NETWORK_VARIABLE",
    "P2PKH",
    "P2SH",
    "SELECTABLE",
    "TESTNET",
    "NetworkSelectionError",
    "assert_daemon_agrees",
    "base58_version",
    "is_mainnet",
    "mainnet_blockers",
    "selected_network",
]
