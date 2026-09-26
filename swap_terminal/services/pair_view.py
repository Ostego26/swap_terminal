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

WHAT IT IS NOT. services/admin_view.pair_rows() answers a DIFFERENT question and
deliberately stays separate: it builds the OPERATOR's full N x N matrix of every
asset the tree knows, so a chain that was wired and never enabled is visible. This
builds the CUSTOMER's offer list -- only the allowed pairs. Same two authorities,
different question, and each names the other (rule 8) so a reader who finds one
knows the other exists.
"""

from chains.registry import unconfigured_chains, why_unconfigured


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
        missing = unconfigured_chains(adapters, from_asset, to_asset)
        rows.append(
            {
                "from_asset": from_asset,
                "to_asset": to_asset,
                "label": f"{from_asset} -> {to_asset}",
                "enabled": not missing,
                "missing": missing,
                # `(none)` is never right here: a row is either enabled, in which
                # case the reason says both chains are reachable, or it names every
                # missing chain. A blank reason beside DISABLED would be rule 14's
                # empty gap.
                "reason": (
                    "in ALLOWED_PAIRS, and both chains have an adapter in this process"
                    if not missing
                    else " Also: ".join(why_unconfigured(asset, config.get("RPC")) for asset in missing)
                ),
            }
        )
    return rows


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
