"""Which pairs are allowed, and which of those this process can actually complete.

Role: submodule (assembles rows from two authorities; holds no decision about
      amounts, addresses or fund movement)
Reads: config["ALLOWED_PAIRS"], config["RPC"], and the adapters dict
      chains/registry.build_adapters() produced. No database, no socket.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS RATHER THAN THE FUNCTION STAYING IN routes/ui.py.

allowed_pair_rows() was defined in routes/ui.py, because the swap page was the
only thing that needed it. Then /api/health needed the same answer -- it echoed
`allowed_pairs` and nothing about what the server could REACH, which is the same
overclaim the swap page carried until 2026-09-26 and on the one endpoint an
operator can curl.

The choice was to import a function from one route module into another, or to put
the decision below both. Rule 10 settles it: a decision lives in a function the
layer above calls, and `routes/health` depending on `routes/ui` would make a
reader ask why the health probe needs the customer surface. The answer -- "because
that is where the decision happened to be written" -- is the defect.

So it sits beside services/swap_view.py and services/admin_view.py, which already
assemble rows for templates from the same shape of input.

WHAT IT IS NOT. services/admin_view.pair_rows() answers a DIFFERENT question: it
builds the OPERATOR's full N x N matrix of every asset the tree knows, so a chain
that was wired and never enabled is visible. This builds the CUSTOMER's offer list
-- only the allowed pairs.

THE TWO QUESTIONS DIFFER; THE VERDICT DOES NOT, AND SPLITTING THEM COST A WRONG
ANSWER ON A LIVE PAGE. Until 2026-10-02 admin_view.pair_rows() computed its own
verdict from `unconfigured_chains()` alone, and the two surfaces then disagreed
about the same pair in the same process at the same moment:

    /admin   XRP -> GRC   ENABLED      "both chains have an adapter here"
    /        XRP -> GRC   DISABLED     "XRP cannot take deposits: XRP_DEPOSIT_ACCOUNT
                                        is unset or not a valid account"

    /admin   GRC -> XRP   ENABLED      "both chains have an adapter here"
    /        GRC -> XRP   DISABLED     "XRP cannot pay out: it holds no signing key"

The customer page was right, and the operator page was wrong in the direction that
costs money: it told the operator a pair was fine when a deposit on it would be
credited and the payout would then raise, leaving the swap `failed` with the
customer's coins already taken. Worse, /admin contradicted ITSELF -- its Chains
table printed "payouts=PREVIEW-ONLY unless armed at the call site" for XRP three
panels above a pairs entry reading ENABLED for a pair ending in XRP.

THE SAME HOLE HAD ALREADY BEEN FOUND TWICE MORE: payout_service.payable_assets()
had it, which made supervisor.py's spawn banner print "a payout worker CAN
broadcast on GRC, XRP" (fixed 02c5d56, 2026-10-02), and the swap page itself had
it until 2026-09-26. Four implementations of one rule, three of them wrong, each
found separately. That is rule 8's shape exactly, and the repair rule 8 asks for
is a survivor that owns the concept -- so pair_serviceability() below is the only
place the three conditions are evaluated, and admin_view.pair_rows() calls it
instead of reasoning about adapters at all.
"""

from chains.registry import unconfigured_chains, why_cannot_pay_out, why_unconfigured

from .swap_service import why_cannot_take_deposits


def pair_serviceability(config, adapters, from_asset: str, to_asset: str) -> dict:
    """Can THIS ordered pair actually complete in THIS process? The only copy.

    THREE CONDITIONS, AND A PAIR NEEDS ALL THREE. Each has its own authority and
    none of them is re-derived here:

      missing       chains/registry.unconfigured_chains() -- neither side has an
                    adapter in this process. "We cannot reach that chain."
      cannot_pay    chains/registry.why_cannot_pay_out() -- we CAN reach the
                    destination and still cannot pay you. Only the DESTINATION is
                    asked; a source chain never sends.
      cannot_take   services/swap_service.why_cannot_take_deposits() -- the source
                    cannot produce a deposit target. Only the SOURCE is asked; a
                    destination chain never receives a deposit.

    EVALUATED IN THAT ORDER AND SHORT-CIRCUITED, which is deliberate rather than an
    optimization. A chain with no adapter cannot be asked whether it can pay out --
    why_cannot_pay_out() returns "" for an absent adapter precisely because that is
    a different problem, and reporting both would print two reasons for one pair and
    leave the operator to work out which to act on. So the first condition that
    refuses is the one reported.

    `serviceable` is the single boolean every caller branches on. `reason` is prose
    for a person, and is never blank: an enabled pair gets a sentence saying all
    three tests passed, because a blank beside a verdict is rule 14's empty gap.

    WHY THIS IS A FUNCTION AND NOT A FLAG ON A ROW. Two callers want different ROW
    SHAPES -- the customer's offer list is only the allowed pairs, the operator's
    matrix is every ordered pair of every asset the tree knows -- and only one of
    them wants to distinguish "not in ALLOWED_PAIRS" from "allowed but not
    completable". The row shapes are the callers' business; the verdict is not, and
    it is the verdict that was wrong on a page for want of being shared.

    config.get("RPC") rather than config["RPC"]: a seeded config in a test may not
    carry the RPC mapping, and why_unconfigured() degrades to naming the chain's
    primary setting when it is absent rather than raising on a page.
    """
    missing = unconfigured_chains(adapters, from_asset, to_asset)
    cannot_pay = "" if missing else why_cannot_pay_out(adapters, to_asset)
    cannot_take = "" if missing or cannot_pay else why_cannot_take_deposits(config, adapters, from_asset)
    return {
        "missing": missing,
        "cannot_pay": cannot_pay,
        "cannot_take": cannot_take,
        "serviceable": not missing and not cannot_pay and not cannot_take,
        "reason": (
            " Also: ".join(why_unconfigured(asset, config.get("RPC")) for asset in missing)
            if missing
            else cannot_pay
            or cannot_take
            or "in ALLOWED_PAIRS, both chains have an adapter here, the source can take "
            "a deposit and the destination can pay out"
        ),
    }


def allowed_pair_rows(config, adapters) -> list[dict]:
    """Every allowed pair, as a row that says whether it can actually complete.

    TWO AUTHORITIES, NOT ONE, and the difference is the whole reason this function
    changed on 2026-09-26. config["ALLOWED_PAIRS"] is what the operator is WILLING
    to swap; `adapters` is what this process can REACH. A pair needs both, and the
    swap page used to read only the first -- so six pairs were badged ENABLED on a
    server that had built one adapter, the operator picked XRP -> GRC, the quote
    priced, and Create swap answered `No swap was created: 'GRC'`.

    Returns rows for ALL allowed pairs, disabled ones included, because a pair that
    is silently missing is indistinguishable from a pair that was never configured:
    the operator would see five entries where they set up six and have nothing to
    read. admin.html already lists disabled pairs rather than hiding them; this
    follows it. The caller takes the enabled SUBSET for anything that OFFERS a
    pair, so a form still cannot offer something the server would refuse.

    `reason` is prose for a person and is the only part of the row that should ever
    be shown next to DISABLED. It is built by chains/registry.why_unconfigured(),
    which names the environment variable through
    network_target.configuring_variable() -- so the page, the workers' startup
    banner, /api/health and create_swap()'s refusal all name the same variable from
    one place (rule 8).

    config.get("RPC") rather than config["RPC"]: a seeded config in a test may not
    carry the RPC mapping, and why_unconfigured() degrades to naming the chain's
    primary setting when it is absent rather than raising on a page.
    """
    rows = []
    for from_asset, to_asset in sorted(config["ALLOWED_PAIRS"]):
        # THE THREE CONDITIONS ARE NOT EVALUATED HERE ANY MORE, and that is the fix
        # the module header records: a second evaluation of them, in
        # services/admin_view.pair_rows(), had only the FIRST of the three and told
        # the operator a pair was ENABLED that the customer page was refusing in the
        # same process. The row shape below is unchanged to the key, so this page,
        # /api/health and open_swap.py see exactly what they saw before.
        verdict = pair_serviceability(config, adapters, from_asset, to_asset)
        rows.append(
            {
                "from_asset": from_asset,
                "to_asset": to_asset,
                "label": f"{from_asset} -> {to_asset}",
                "enabled": verdict["serviceable"],
                "missing": verdict["missing"],
                "cannot_pay": verdict["cannot_pay"],
                "cannot_take": verdict["cannot_take"],
                # `(none)` is never right here: a row is either enabled, in which
                # case the reason says all three tests passed, or it names what
                # refused. A blank reason beside DISABLED would be rule 14's empty
                # gap, which is why pair_serviceability() never returns one.
                "reason": verdict["reason"],
            }
        )
    return rows


#: WHAT A CUSTOMER IS TOLD, as a table rather than as branches in a template.
#:
#: Operator instruction 2026-10-02: "the user screen should just have graphical
#: indicators to what's availble for them to swap."
#:
#: THREE STATES AND NOT THE OPERATOR'S FOUR. services/admin_view.pair_rows() has
#: enabled / unreachable / cannot_complete / disabled, and the fourth exists only
#: because that page shows every ordered pair including the ones NOT in
#: ALLOWED_PAIRS. This page lists the allowed pairs only, so there is no such row
#: to describe and a fourth state here would be vocabulary copied because it
#: exists.
#:
#: WHY TWO UNAVAILABLE STATES AND NOT ONE, which is the judgment call in this
#: table. A customer deciding whether to come back later is asking a different
#: question from a customer deciding whether to pick another pair, and the two
#: causes answer it differently:
#:
#:   `missing`      no adapter for a chain in THIS server process. That is a
#:                  configuration or a daemon, and it is the kind of thing that
#:                  changes without a release. "Not reachable right now" is true
#:                  and useful.
#:   cannot_pay /   the destination holds no signing key, or the source has no
#:   cannot_take    deposit account. Neither changes while this server runs as it
#:                  is. Telling that customer to come back later would be a
#:                  promise nothing is going to keep.
#:
#: NEITHER NOTE PROMISES ANYTHING AND NEITHER NAMES A VARIABLE. "Not reachable
#: right now" is a statement about now, not a forecast; the operator-facing
#: remedy ("export BTC_RPC_PORT in the shell that starts the server") has moved
#: to /admin entirely, where its reader is and where that reader has a shell.
#:
#: The GLYPHS are templates/_badges.html's existing ones, reused rather than
#: invented: a check, an ellipsis and a filled square are three distinct SHAPES,
#: so the indicator survives a grayscale paste and a screen reader reads the word
#: beside each one. Color is the third channel here as it is everywhere else on
#: these pages.
_CUSTOMER_AVAILABILITY = {
    "available": {
        "available": True,
        "level": "ok",
        "word": "AVAILABLE",
        "note": "Ready to quote now.",
    },
    "unreachable": {
        "available": False,
        "level": "waiting",
        "word": "OFFLINE",
        "note": "One of these two chains is not reachable from this server right now.",
    },
    "unavailable": {
        "available": False,
        "level": "halted",
        "word": "UNAVAILABLE",
        "note": "This terminal cannot complete this direction.",
    },
}


#: The three states in reading order, for the page's key. DERIVED from the table
#: above rather than spelled a second time: a legend that listed them by hand would
#: be rule 8's shape, and the first thing to drift would be a state the key does not
#: explain -- which is a glyph a customer cannot look up.
CUSTOMER_STATES = tuple(
    {"key": key, **value} for key, value in _CUSTOMER_AVAILABILITY.items()
)


def customer_availability(row: dict) -> dict:
    """What to show a CUSTOMER for one pair row. The only place that decides it.

    Takes a row from allowed_pair_rows() -- so the verdict is
    pair_serviceability()'s and is not re-derived here, which is the arrangement
    the 2026-10-02 defect was about: a second evaluation of this verdict had
    /admin and / disagreeing about the same pair in one process.

    A FUNCTION AND NOT BRANCHES IN THE TEMPLATE (rule 10). "What does a customer
    see for this pair" is a decision, and a decision in a template cannot be
    called with seeded inputs -- which is precisely how the customer page came to
    badge a reachable-but-unpayable pair DISABLED while /admin called it
    CANNOT COMPLETE: templates/index.html had `'ENABLED' if pair.enabled else
    'DISABLED'`, a binary, and nothing could test the third case because there
    was nowhere for it to live.

    `key` is returned so a caller can style or group on the state without
    re-deriving it from the booleans.
    """
    if row["enabled"]:
        key = "available"
    elif row["missing"]:
        key = "unreachable"
    else:
        key = "unavailable"
    return {"key": key, **_CUSTOMER_AVAILABILITY[key]}


def offerable_pairs(rows: list[dict]) -> list[dict]:
    """The rows a caller may actually OFFER: allowed, and both chains reachable.

    Takes the rows rather than (config, adapters) so that a caller which needs BOTH
    lists -- the swap page shows every allowed pair and offers the reachable subset
    -- builds them once. A version taking the config would have the page assembling
    the same rows twice and the two lists free to disagree if the adapters dict
    changed between the calls.

    A function rather than a comprehension at each call site, because "which pairs
    can complete" is asked by the swap page and by /api/health, and two copies of a
    filter is how a page and an endpoint come to answer one question differently.
    """
    return [row for row in rows if row["enabled"]]
