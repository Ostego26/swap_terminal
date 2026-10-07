"""One lamp per coin: green when every direction works, amber when some, red when none.

Role: tests (read-only)
Reads: swap_terminal/services/pair_view.py -- asset_rollups() and ASSET_ROLLUP_STATES
Writes: nothing
Can move funds: no. No config, no adapters, no chain, no database -- asset_rollups()
        is pure and takes the rows allowed_pair_rows() already built.
Mainnet-safe: yes

Operator, 2026-10-07: "grc led ltc led, etc should indicate which pairs are available
for that coin. if all are available green. if some but not all, yellow. none....red."

WHAT THESE TESTS ARE REALLY ABOUT, beyond the three colours. A single lamp per coin
collapses two questions that have opposite answers, and ICP is the live instance:
measured 2026-10-07, ICP -> BTC/GRC/LTC quote and NOTHING -> ICP does, because an ICP
payout needs the desk's dfx identity and a read does not. An amber lamp over that is
true and useless -- it says "some" to a customer whose question is "can I get ICP out"
or "can I get ICP in". So the split is asserted as hard as the colour.
"""

from __future__ import annotations

from pathlib import Path

from config import Config
from jinja2 import Environment, FileSystemLoader
from services.admin_view import pair_rows
from services.pair_view import ASSET_ROLLUP_STATES, allowed_pair_rows, asset_rollups


def _rows(*specs: tuple[str, str, bool]) -> list[dict]:
    """Rows shaped like allowed_pair_rows()' output, with only the keys the rollup reads."""
    return [
        # `serviceable` is what the rollup reads, and the name matters: `enabled`
        # means the verdict in pair_view's rows and "is in ALLOWED_PAIRS" in
        # admin_view's, which is what painted /admin's lamps green while the customer
        # page's were red. Seeding only `serviceable` means a rollup that went back to
        # reading `enabled` fails here with a KeyError rather than quietly.
        {"from_asset": source, "to_asset": destination, "serviceable": enabled, "label": f"{source} -> {destination}"}
        for source, destination, enabled in specs
    ]


def _by_asset(rows):
    return {lamp["asset"]: lamp for lamp in asset_rollups(rows)}


def test_a_coin_whose_every_direction_works_is_GREEN():
    lamps = _by_asset(_rows(("BTC", "LTC", True), ("LTC", "BTC", True)))
    for asset in ("BTC", "LTC"):
        assert lamps[asset]["key"] == "all"
        assert lamps[asset]["level"] == "ok"
        assert lamps[asset]["available"] == lamps[asset]["total"] == 2


def test_a_coin_with_no_working_direction_is_RED():
    lamps = _by_asset(_rows(("BTC", "SOL", False), ("SOL", "BTC", False)))
    assert lamps["SOL"]["key"] == "none"
    assert lamps["SOL"]["level"] == "halted"
    assert lamps["SOL"]["available"] == 0


def test_the_ICP_SHAPE_is_AMBER_and_the_lamp_says_WHICH_HALF():
    """The measured case, and the reason the split exists.

    ICP out works, ICP in does not. The colour alone is "some", which is true and
    answers neither of the two questions a customer actually has.
    """
    lamps = _by_asset(
        _rows(
            ("ICP", "BTC", True), ("ICP", "GRC", True), ("ICP", "LTC", True),
            ("BTC", "ICP", False), ("GRC", "ICP", False), ("LTC", "ICP", False),
        )
    )
    icp = lamps["ICP"]
    assert icp["key"] == "some"
    assert icp["level"] == "waiting"
    assert (icp["out_available"], icp["out_total"]) == (3, 3), "every outbound direction works"
    assert (icp["in_available"], icp["in_total"]) == (0, 3), "and no inbound one does"
    assert "3 of 3 outbound" in icp["detail"]
    assert "0 of 3 inbound" in icp["detail"]


def test_a_coin_that_is_only_a_DESTINATION_in_a_broken_pair_is_still_counted():
    """BTC appears here only as a source; its inbound total is zero and must not vacuously pass.

    A coin with no inbound direction at all has in_available == in_total == 0, and
    "all of zero" is the vacuous truth that would paint a lamp green for a coin nobody
    can send TO. The overall key is what guards it -- it is driven by the combined
    counts -- and this pins that the zero is reported rather than hidden.
    """
    lamps = _by_asset(_rows(("BTC", "LTC", True)))
    assert lamps["BTC"]["out_total"] == 1
    assert lamps["BTC"]["in_total"] == 0
    assert lamps["LTC"]["in_total"] == 1
    assert lamps["LTC"]["out_total"] == 0


def test_no_rows_at_all_is_NONE_rather_than_vacuously_ALL():
    """An empty set satisfies "every direction works" and must not read as green.

    It cannot arise from ALLOWED_PAIRS today, which is exactly why it is pinned: the
    case nothing can currently produce is the case nobody notices the day something
    can (rule 17's vacuous-pass trap, in a view).
    """
    assert asset_rollups([]) == []

    orphan = _rows(("BTC", "BTC", False))
    lamps = _by_asset(orphan)
    assert lamps["BTC"]["key"] == "none"


def test_the_lamp_states_are_DERIVED_and_cover_what_a_lamp_can_be():
    """The page's key is built from the same table the lamps are, or it drifts (rule 8)."""
    keys = {state["key"] for state in ASSET_ROLLUP_STATES}
    assert keys == {"all", "some", "none"}
    for state in ASSET_ROLLUP_STATES:
        assert state["word"] and state["note"], f"{state['key']} has nothing a reader can look up"
        assert state["level"] in {"ok", "waiting", "halted"}, (
            "the lamp levels must reuse the pair vocabulary so one stylesheet colors both"
        )


def test_a_lamp_can_never_be_greener_than_the_rows_it_came_from():
    """The rollup is a MIRROR of allowed_pair_rows(), never a second evaluation.

    This module's own header records what a second evaluation cost: admin_view's
    pair_rows() once carried the first of three serviceability conditions and told the
    operator a pair was ENABLED that the customer page was refusing in the same
    process. Mutation-shaped: flipping any row to unavailable must be able to move the
    lamp, and no lamp may claim more available than the rows contain.
    """
    rows = _rows(("BTC", "LTC", True), ("LTC", "BTC", True))
    assert _by_asset(rows)["BTC"]["key"] == "all"

    rows[0]["serviceable"] = False
    moved = _by_asset(rows)["BTC"]
    assert moved["key"] == "some", "a row going unavailable must move the lamp off green"
    assert moved["available"] <= moved["total"]


# --- and the same three states, RENDERED, because a decision nobody draws is invisible


def _rendered(lamps):
    """index.html's lamp strip, rendered with seeded lamps and nothing else real.

    The template is rendered directly rather than through a request, because the
    question here is what the LAMP MARKUP does with a given rollup -- and routing a
    real request through it would require a config whose adapters produce the mixed
    state, which is the one state this container cannot produce (it has no chains).
    The decision itself is covered above against seeded rows.
    """
    root = Path(__file__).resolve().parents[1] / "swap_terminal" / "templates"
    env = Environment(loader=FileSystemLoader(str(root)), autoescape=True)
    source = (root / "index.html").read_text()
    start = source.index('<ul class="lampstrip"')
    end = source.index("</ul>", start) + len("</ul>")
    return env.from_string(source[start:end]).render(lamps=lamps)


def test_each_lamp_renders_its_colour_class_its_word_and_BOTH_counts():
    """The colour answers "is anything wrong"; the counts answer "which half".

    Pinned together because the colour alone is what the operator asked for and the
    counts are what make an amber lamp actionable. A render that dropped the counts
    would satisfy the brief and still leave ICP's lamp saying "some" to a customer
    whose question has two opposite answers.
    """
    markup = _rendered(
        asset_rollups(
            _rows(
                ("ICP", "BTC", True), ("BTC", "ICP", False),
                ("BTC", "LTC", True), ("LTC", "BTC", True),
                ("SOL", "BTC", False), ("BTC", "SOL", False),
            )
        )
    )

    assert 'class="lamp lamp-all"' in markup, "LTC's every direction works and must be green"
    assert 'class="lamp lamp-some"' in markup, "ICP works one way only and must be amber"
    assert 'class="lamp lamp-none"' in markup, "SOL works neither way and must be red"

    for word in ("ALL", "SOME", "NONE"):
        assert f">{word}</span>" in markup, f"{word} is a colour with no word beside it"

    assert "out 1/1" in markup and "in 0/1" in markup, (
        "ICP's split must be ON THE TILE, not only in a tooltip -- a customer reads the screen"
    )


def test_every_lamp_is_a_BUTTON_carrying_the_asset_it_filters():
    """point/click, keyboard-reachable, and inert-but-honest with JavaScript off.

    A <button> rather than a div with a handler: reachable by keyboard and announced
    as pressable with no hand-added ARIA. aria-pressed starts false and the filter
    script is the only thing that changes it, so a page with JS off shows an unpressed
    button beside counts that are already correct -- the panel complete and merely
    unfiltered.
    """
    markup = _rendered(asset_rollups(_rows(("BTC", "LTC", True), ("LTC", "BTC", True))))

    assert markup.count("<button") == 2, "one button per coin"
    assert 'type="button"' in markup, "a submit button inside a page with forms would submit one"
    assert 'aria-pressed="false"' in markup, "the pressed state has to be announced, not just colored"
    for asset in ("BTC", "LTC"):
        assert f'data-asset="{asset}"' in markup, f"{asset}'s button does not say what it filters"


# --- the two surfaces cannot disagree about a coin


def test_BOTH_row_builders_carry_the_verdict_under_THE_SAME_NAME():
    """`enabled` means two different things in the two row shapes. Measured 2026-10-07.

    The coin lamps landed on the customer page and then on /admin, and the two pages
    drew OPPOSITE colours -- six red and six green -- in the same process, from the same
    config. Not a second evaluation of serviceability, which this tree already guards:
    a NAME COLLISION.

        services.pair_view.allowed_pair_rows()     enabled == the verdict
        services.admin_view.pair_rows()            enabled == "is in ALLOWED_PAIRS",
                                                   and `serviceable` is the verdict

    asset_rollups() read `enabled`, so it was right on one shape and wrong on the other,
    and nothing could tell -- both are bools and both are present. That is rule 8
    arriving through a key name rather than through duplicated logic, which is the
    variety no amount of "don't evaluate it twice" catches.

    THE PROPERTY PINNED HERE is the one that makes the fix hold: both builders carry
    `serviceable`, and on BOTH it means the verdict. Whether `enabled` keeps its second
    meaning is not this test's business -- five consumers read it and renaming it is a
    bigger change than naming the collision -- but a reader of either shape must be able
    to ask one question with one word.
    """
    config = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}

    customer = allowed_pair_rows(config, {})
    operator = pair_rows(config, {})
    assert customer and operator, "both builders must produce rows or this test checked nothing"

    for label, rows in (("pair_view", customer), ("admin_view", operator)):
        for row in rows:
            assert "serviceable" in row, (
                f"{label}'s rows carry no `serviceable` key, so asset_rollups() cannot read the "
                f"verdict by one name and the two surfaces can disagree about a coin again"
            )
            assert isinstance(row["serviceable"], bool), f"{label}: {row['serviceable']!r} is not a verdict"


def test_THE_TWO_PAGES_ROLL_UP_TO_THE_SAME_LAMPS():
    """Same function, same rows, same answer -- asserted end to end rather than argued.

    This is the assertion that would have failed on 2026-10-07 and did not exist. The
    rollup is run over BOTH row builders' output and the lamps compared key by key: a
    colour, and both counts. If a future row shape reintroduces the collision under
    another name, this fails naming the coin.
    """
    config = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}

    customer = {lamp["asset"]: lamp for lamp in asset_rollups(allowed_pair_rows(config, {}))}
    operator = {lamp["asset"]: lamp for lamp in asset_rollups(pair_rows(config, {}))}

    assert set(customer) == set(operator), (
        f"the two surfaces do not even list the same coins: {sorted(customer)} vs {sorted(operator)}"
    )
    for asset in sorted(customer):
        for field in ("key", "out_available", "out_total", "in_available", "in_total"):
            assert customer[asset][field] == operator[asset][field], (
                f"{asset}'s lamp differs between the customer page and /admin on {field}: "
                f"{customer[asset][field]!r} vs {operator[asset][field]!r}. The two surfaces are "
                f"reading the same rows through the same function, so a difference is a row-shape "
                f"disagreement -- which is what painted six red lamps and six green ones."
            )
