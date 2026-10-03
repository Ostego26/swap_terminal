#!/usr/bin/env python3
"""What the CUSTOMER PAGE actually says about XRP -> GRC and GRC -> XRP, in all four postures.

Role: tests (read-only)
Reads: config.Config (ALLOWED_PAIRS is overridden per call, RPC is deep-copied),
       chains/registry.build_adapters(), services/pair_view.allowed_pair_rows()
       and services/pair_view.customer_availability(). No database, no socket --
       the XRP url is unreachable.invalid and nothing here opens it.
Writes: nothing
Can move funds: no. Nothing constructed here signs or submits; the only seed
       value used is an obvious non-seed literal (see THE SEED IS NOT A SEED
       below) and no payout, quote or swap is created.
Mainnet-safe: yes

WHY THIS FILE EXISTS: TWO LINES WERE WRITTEN FROM REASONING AND ONE OF THEM WAS
WRONG.

On 2026-10-03 the customer page's XRP rows were described, in prose, as reading:

    XRP -> GRC    UNAVAILABLE   XRP_DEPOSIT_ACCOUNT is unset
    GRC -> XRP    UNAVAILABLE   no seed exported, so can_spend is False

Both were derived by reading the source rather than by calling the functions.
MEASURED the same day by running services/pair_view.allowed_pair_rows() and
services/pair_view.customer_availability() over seeded config and adapters built
the way swap_terminal/app.py builds them -- chains/registry.build_adapters() with
a deep copy of Config.RPC -- across all four XRP postures:

    seed  deposit | XRP -> GRC                      | GRC -> XRP
    ------+-------+---------------------------------+--------------------------------
    unset unset   | UNAVAILABLE  cannot take deposits | UNAVAILABLE  cannot pay out
    unset set     | AVAILABLE    ready to quote       | UNAVAILABLE  cannot pay out
    set   unset   | UNAVAILABLE  cannot take deposits | UNAVAILABLE  no XRP reserve
    set   set     | AVAILABLE    ready to quote       | UNAVAILABLE  no XRP reserve

THE FIRST CLAIMED LINE IS RIGHT IN SUBSTANCE: with XRP_DEPOSIT_ACCOUNT unset,
XRP -> GRC is UNAVAILABLE and its reason is exactly

    XRP cannot take deposits: XRP_DEPOSIT_ACCOUNT is unset or not a valid account

and it stays UNAVAILABLE whether or not a signing seed is exported, because the
XRP leg is the SOURCE there and a source never sends.

THE SECOND CLAIMED LINE IS WRONG, in two separate ways, and both matter to an
operator:

  the string         no reason this page can produce contains "can_spend" or
                     "no seed exported". `can_spend` is an adapter attribute,
                     read by chains/registry.why_cannot_pay_out(); what reaches
                     the row is chains/xrp_payout_seed.missing_seed_refusal(),
                     which NAMES XRP_PAYOUT_SECRET_SEED and XRP_DEPOSIT_ACCOUNT
                     because those are what the reader has to export. A summary
                     that paraphrases it into an attribute name sends the reader
                     to grep the tree instead of to their shell.
  the posture        it is only the reason in TWO of the four postures. Export
                     the seed and GRC -> XRP is STILL UNAVAILABLE, for a
                     different cause entirely -- XRP_NETWORK_FEE_RESERVE does not
                     exist, so services/quote_service.why_cannot_quote() refuses.
                     Someone who believed the claimed line would export the seed,
                     expect the pair to come up, and find it unchanged.

WHAT A CUSTOMER SEES IS NOT THE THIRD COLUMN AT ALL, and this was measured too
rather than assumed. templates/index.html renders one tile per pair holding the
pair label and the badge only -- "XRP -> GRC UNAVAILABLE" -- plus a per-STATE key
carrying customer_availability()'s `note`. The variable-naming `reason` has
rendered on /admin and not on / since 2026-10-02, by the operator instruction
quoted in that template. So the claimed lines are a three-column shape the
customer page does not have; the words are right, the sentence beside them is the
OPERATOR's.

WHAT THIS TEST PINS, and the thing worth failing on: every UNAVAILABLE reason
must keep NAMING THE VARIABLE an operator has to export. That is the whole of its
usefulness (rule 14 -- echo the parameter that decides the answer), and the three
sentences come from three different modules, so nothing but an assertion holds
them together.

THE SEED IS NOT A SEED. chains/xrp_payout_seed.signing_seed_is_present() reads
only bool() of the variable, so the literal below is never decoded, never signed
with and reaches no network -- the same arrangement, and the same literal, as
tests/test_allowed_pairs_are_serviceable.py's comment of that name. The url is
unreachable.invalid for the matching reason: construction opens no socket, and if
that ever changes this test fails loudly rather than quietly reaching a server.
"""

from __future__ import annotations

import copy

import pytest
from chains.registry import build_adapters
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR
from config import Config
from conftest import RPC_FIXTURE_AUTH, RPC_FIXTURE_USER
from services.pair_view import allowed_pair_rows, customer_availability
from valid_addresses import XRP_HOT_ACCOUNT
from workers.common import get_config_dict

#: Never resolved, never connected to. See THE SEED IS NOT A SEED in the header.
UNREACHABLE_URL = "http://unreachable.invalid"

#: NOT A SEED AND NEVER DECODED. The literal is the one
#: tests/test_allowed_pairs_are_serviceable.py already uses, so a reader who greps
#: either file finds the same obvious non-value rather than two inventions.
NOT_A_SEED = "not-a-real-seed-and-never-decoded"

#: The two pairs under measurement, and only those: Config.ALLOWED_PAIRS carries
#: thirteen directions and the other eleven would make the table below a report on
#: the whole desk rather than an assertion about XRP.
PAIRS = {("XRP", "GRC"), ("GRC", "XRP")}


def _build(*, seed: bool, deposit_account: bool, monkeypatch):
    """(config, adapters) for one posture, assembled the way swap_terminal/app.py does.

    THE ADAPTERS ARE REAL AND BUILT BY THE REAL REGISTRY, which is the point of
    the exercise: app.py:165 is `build_adapters(app.config["RPC"])` and a stub
    dict here would be a paraphrase of the thing under measurement. Construction
    opens no socket for either chain.

    GRC IS GIVEN A PORT AND CREDENTIALS so that the XRP posture is the only
    variable. Without them chains/registry.missing_settings() reports GRC
    unconfigured, build_adapters() skips Gridcoin, and both rows come back
    OFFLINE for a reason that has nothing to do with XRP -- which would measure
    the fixture instead of the page. The credentials are conftest's fixture
    constants; no socket is opened with them.

    THE SEED IS SET ON THE ENVIRONMENT rather than in config, because that is
    where chains/xrp_payout_seed.py reads it from, at adapter CONSTRUCTION time --
    so the adapter must be built after the variable is set, which is why this
    helper does both. monkeypatch reverts it, and tests/conftest.py has already
    removed any real one from this process so a developer with the variable
    exported measures the same thing CI does.

    XRP_DEPOSIT_ACCOUNT IS SET IN THE CONFIG DICT and not in the environment,
    because config.Config reads the environment at CLASS-DEFINITION time -- it is
    already baked in by the time any test runs, which conftest.py's header
    records. The serving path takes the value from the config mapping, which is
    what this overrides.
    """
    if seed:
        monkeypatch.setenv(SIGNING_SEED_ENV_VAR, NOT_A_SEED)
    else:
        monkeypatch.delenv(SIGNING_SEED_ENV_VAR, raising=False)

    rpc = copy.deepcopy(Config.RPC)
    rpc["XRP"]["url"] = UNREACHABLE_URL
    rpc["GRC"].update({"port": 15715, "user": RPC_FIXTURE_USER, "password": RPC_FIXTURE_AUTH})

    adapters = build_adapters(rpc)
    assert sorted(adapters) == ["GRC", "XRP"], (
        f"this posture was supposed to have exactly a Gridcoin and an XRP adapter and has "
        f"{sorted(adapters)}; every reason below would then be about a missing chain instead "
        f"of about XRP custody"
    )

    config = dict(get_config_dict())
    config["RPC"] = rpc
    config["ALLOWED_PAIRS"] = PAIRS
    config["XRP_DEPOSIT_ACCOUNT"] = XRP_HOT_ACCOUNT if deposit_account else ""
    return config, adapters


def _rows(config, adapters) -> dict[str, dict]:
    """{"XRP -> GRC": row-with-customer} through the real two functions, unaltered."""
    rendered = {}
    for row in allowed_pair_rows(config, adapters):
        rendered[row["label"]] = {**row, "customer": customer_availability(row)}
    return rendered


#: EXACTLY WHAT THE SOURCE CANNOT TAKE A DEPOSIT SENTENCE IS, from
#: services/swap_service.why_cannot_take_deposits(). Spelled out in full rather
#: than matched on a fragment: the variable's NAME is the operative content and a
#: substring match on "cannot take deposits" would pass a sentence that had
#: dropped it.
CANNOT_TAKE = "XRP cannot take deposits: XRP_DEPOSIT_ACCOUNT is unset or not a valid account"

#: The opening of chains/xrp_payout_seed.missing_seed_refusal(), as
#: chains/registry.why_cannot_pay_out() prefixes it with the asset. Matched on the
#: opening plus two required names rather than on all six sentences, because the
#: prose is deliberately long (it explains the default) and pinning every word
#: would fail on a rewording that kept every fact.
CANNOT_PAY_OPENING = f"XRP cannot pay out: {SIGNING_SEED_ENV_VAR} is not set"

#: services/quote_service.why_cannot_quote()'s refusal, same treatment.
CANNOT_QUOTE_OPENING = "No quote: nobody has recorded what one XRP payout costs this desk"

#: THE MEASUREMENT, as a table: posture -> {label -> expected word}.
#:
#: TWO ROWS CHANGED LATER THE SAME DAY, AND THE CHANGE IS A LIVE POSTURE CHANGE.
#: As first measured on 2026-10-03, GRC -> XRP was UNAVAILABLE in all four postures
#: because no XRP_NETWORK_FEE_RESERVE existed. The operator then set it to 0.00001,
#: from a figure read off their own rippled
#: (server_info.validated_ledger.base_fee_xrp, 10 drops), and the two seed-set rows
#: flipped:
#:
#:     (True, False)   GRC -> XRP   UNAVAILABLE  ->  AVAILABLE
#:     (True, True)    GRC -> XRP   UNAVAILABLE  ->  AVAILABLE
#:
#: So GRC -> XRP is now a pair that TRADES on a host with the seed exported, where
#: for seven days it was a pair that refused every quote. That is the whole content
#: of the change and it is why this table is not simply edited quietly: the
#: assertion message tells the next reader a changed verdict is live posture.
#:
#: THE SEED-UNSET ROWS DID NOT MOVE, and that is the half worth keeping. Without a
#: signing seed XRP still cannot pay out, so the pair is still refused -- by
#: `cannot_pay` now rather than by `cannot_quote`. The page is therefore what stops
#: a customer being quoted a number on an unarmed host, which is the hazard
#: tests/test_allowed_pairs_are_serviceable.py spent three days declining to open.
EXPECTED_WORDS = {
    (False, False): {"XRP -> GRC": "UNAVAILABLE", "GRC -> XRP": "UNAVAILABLE"},
    (False, True): {"XRP -> GRC": "AVAILABLE", "GRC -> XRP": "UNAVAILABLE"},
    (True, False): {"XRP -> GRC": "UNAVAILABLE", "GRC -> XRP": "AVAILABLE"},
    (True, True): {"XRP -> GRC": "AVAILABLE", "GRC -> XRP": "AVAILABLE"},
}

#: The two notes a customer can actually read for these rows, from
#: services/pair_view._CUSTOMER_AVAILABILITY. Asserted because the note is what
#: templates/index.html puts in its key, and an UNAVAILABLE row must never inherit
#: the OFFLINE wording -- "not reachable right now" would promise a customer that
#: coming back later helps, and no XRP custody variable changes while this server
#: runs as it is.
NOTE_AVAILABLE = "Ready to quote now."
NOTE_UNAVAILABLE = "This terminal cannot complete this direction."


@pytest.mark.parametrize(("seed", "deposit_account"), sorted(EXPECTED_WORDS))
def test_the_WORD_each_posture_renders_for_both_XRP_directions(seed, deposit_account, monkeypatch):
    """The badge word, measured 2026-10-03, for all four XRP postures.

    THE ASYMMETRY IS STILL THE FINDING, and half of it reversed when the operator
    set the reserve. XRP -> GRC turns AVAILABLE on the DEPOSIT account alone and is
    indifferent to the seed -- unchanged, because XRP is the SOURCE there and
    why_cannot_pay_out() is only asked about the destination. GRC -> XRP is the
    mirror: indifferent to the ACCOUNT and turning AVAILABLE on the SEED alone.

    "Export the seed and GRC -> XRP comes up" is a claim this parametrization
    REFUTED for seven days and now confirms, and the difference is one
    configuration line rather than any code. That is exactly why the table carries
    its dates: the sentence was false when written and is true now, and a reader
    who finds only one of those two states has to be told which they are looking at.

    MUTATION 2026-10-03: changing services/swap_service.why_cannot_take_deposits()
    to return "" for XRP flips XRP -> GRC to AVAILABLE in the two unset-account
    postures and this fails on the word. Confirmed.
    """
    config, adapters = _build(seed=seed, deposit_account=deposit_account, monkeypatch=monkeypatch)
    rows = _rows(config, adapters)
    assert sorted(rows) == ["GRC -> XRP", "XRP -> GRC"], (
        f"allowed_pair_rows() returned {sorted(rows)} for a two-pair ALLOWED_PAIRS, so the "
        f"rest of this test is measuring something other than the two XRP directions"
    )
    for label, word in EXPECTED_WORDS[(seed, deposit_account)].items():
        customer = rows[label]["customer"]
        assert customer["word"] == word, (
            f"{label} with {SIGNING_SEED_ENV_VAR} {'set' if seed else 'unset'} and "
            f"XRP_DEPOSIT_ACCOUNT {'set' if deposit_account else 'unset'} rendered "
            f"{customer['word']!r}, measured as {word!r} on 2026-10-03. A posture that changed "
            f"verdict is live posture (rule 16) -- read why before editing this table"
        )
        assert customer["note"] == (NOTE_AVAILABLE if word == "AVAILABLE" else NOTE_UNAVAILABLE), (
            f"{label} renders {customer['note']!r} beside {word}. An UNAVAILABLE row must not "
            f"carry the OFFLINE note: 'not reachable right now' tells a customer to come back, "
            f"and no XRP custody variable changes while this server runs as it is"
        )


@pytest.mark.parametrize("seed", [False, True])
def test_XRP_to_GRC_names_XRP_DEPOSIT_ACCOUNT_whenever_the_account_is_unset(seed, monkeypatch):
    """The first claimed line, and it is correct in substance: the SOURCE cannot receive.

    Parametrized over the seed to pin the indifference, which is the half the
    claimed line left out: a signing seed does nothing for this direction because
    XRP is the source here and chains/registry.why_cannot_pay_out() is only ever
    asked about the destination.

    MUTATION 2026-10-03: dropping the variable name from
    services/swap_service.why_cannot_take_deposits()'s sentence fails this on the
    equality, which is the regression it exists for -- a reason that stops naming
    what to export is a reason an operator cannot act on.
    """
    config, adapters = _build(seed=seed, deposit_account=False, monkeypatch=monkeypatch)
    row = _rows(config, adapters)["XRP -> GRC"]
    assert row["reason"] == CANNOT_TAKE, (
        f"XRP -> GRC with XRP_DEPOSIT_ACCOUNT unset reads {row['reason']!r}; measured as "
        f"{CANNOT_TAKE!r} on 2026-10-03. If the wording changed, it must still name "
        f"XRP_DEPOSIT_ACCOUNT -- that name is the whole of what the reader can act on"
    )
    assert "XRP_DEPOSIT_ACCOUNT" in row["reason"]
    assert row["cannot_take"] == row["reason"], (
        "the reason came from some condition other than cannot_take, so this test is pinning "
        "the wrong sentence for this posture"
    )


def test_GRC_to_XRP_with_NO_seed_names_BOTH_custody_variables_and_not_can_spend(monkeypatch):
    """The second claimed line, and the string in it does not exist.

    "no seed exported, so can_spend is False" describes the mechanism correctly
    and is not what any surface renders. What renders is
    chains/xrp_payout_seed.missing_seed_refusal(), prefixed with the asset by
    chains/registry.why_cannot_pay_out(), and it names BOTH custody variables --
    naming one sends the reader to do half the work, which
    tests/test_allowed_pairs_are_serviceable.py already records as a mistake
    someone made.

    `can_spend` is asserted ABSENT from the rendered string deliberately. It is an
    attribute name: a reader who finds it on a page has to grep this tree to learn
    what to export, and an operator reading /admin has a shell rather than a
    checkout.

    MUTATION 2026-10-03: removing the XRP_DEPOSIT_ACCOUNT sentence from
    missing_seed_refusal() fails this on the second name.
    """
    config, adapters = _build(seed=False, deposit_account=False, monkeypatch=monkeypatch)
    row = _rows(config, adapters)["GRC -> XRP"]
    assert row["reason"].startswith(CANNOT_PAY_OPENING), (
        f"GRC -> XRP with no seed reads {row['reason']!r}; measured as starting "
        f"{CANNOT_PAY_OPENING!r} on 2026-10-03"
    )
    assert SIGNING_SEED_ENV_VAR in row["reason"]
    assert "XRP_DEPOSIT_ACCOUNT" in row["reason"], (
        "the payout refusal no longer names the SECOND variable, so an operator who exports the "
        "seed alone still cannot pay out and has not been told why"
    )
    assert "can_spend" not in row["reason"], (
        "an adapter attribute name has reached a rendered reason. The reader has a shell, not "
        "this checkout -- name the variable to export"
    )
    assert row["cannot_pay"] == row["reason"]


def test_GRC_to_XRP_IS_NOW_THE_SEED_ALONE_AND_THE_REASON_NAMES_WHICH(monkeypatch):
    """The seed is the only switch on this pair now, and the refusal must say so.

    THIS TEST WAS test_GRC_to_XRP_WITH_a_seed_is_STILL_unavailable_for_want_of_the_reserve
    and it asserted that arming the seed did NOT bring the pair up. That was
    measured and true on 2026-10-03, and its own docstring said what its failure
    would mean: "if XRP_NETWORK_FEE_RESERVE is ever set, GRC -> XRP becomes
    AVAILABLE, that is a live-posture change and the operator's (rule 16), and this
    assertion is where it surfaces rather than on a customer's screen."

    IT SURFACED THERE. The operator set the reserve to 0.00001 later the same day,
    from a figure read off their own rippled, and this test failed by name. So it is
    rewritten to pin the stronger invariant rather than deleted (rule 2) -- the
    posture it guarded has ended, and the one that replaced it has a hazard of its
    own worth holding.

    WHAT IS STRONGER. The old version pinned one state: this pair cannot come up.
    The real property has two, and the second is the one that could regress
    silently:

      seed SET     -> AVAILABLE, and the quote must actually price. Otherwise the
                      page offers a direction the next click refuses, which is the
                      defect this whole file was opened for.
      seed UNSET   -> UNAVAILABLE, and the reason must be the PAYOUT refusal naming
                      the seed. A reserve exists now, so `cannot_quote` is empty and
                      a reason that still mentioned the reserve would send the
                      operator to set a variable that is already set.

    THE SECOND IS WHAT KEEPS A CUSTOMER FROM BEING QUOTED A NUMBER NOBODY CAN PAY.
    With the reserve in place the quote prices on an unarmed host too, so the badge
    is the only thing standing between a teller and a promise the desk cannot keep.
    """
    armed_config, armed_adapters = _build(seed=True, deposit_account=True, monkeypatch=monkeypatch)
    assert "XRP_NETWORK_FEE_RESERVE" in armed_config, (
        "the reserve is gone again. If that was deliberate, git history holds the version of this "
        "test that asserted its absence -- but a pair enabled with no reserve refuses every quote"
    )
    armed = _rows(armed_config, armed_adapters)["GRC -> XRP"]
    assert armed["customer"]["word"] == "AVAILABLE", (
        f"GRC -> XRP is armed and priced and still reads {armed['customer']['word']!r}. Both custody "
        f"halves and the reserve are present, so a refusal here means a fourth condition nobody has "
        f"named: {armed['reason'][:160]}"
    )
    assert not armed["cannot_quote"], (
        f"the pair is still refused for want of a fee reserve on a config that has one: "
        f"{armed['cannot_quote'][:160]}"
    )

    unarmed_config, unarmed_adapters = _build(seed=False, deposit_account=True, monkeypatch=monkeypatch)
    unarmed = _rows(unarmed_config, unarmed_adapters)["GRC -> XRP"]
    assert unarmed["customer"]["word"] == "UNAVAILABLE", (
        "GRC -> XRP went AVAILABLE with NO signing seed. The quote prices now, so the badge is the "
        "only thing stopping a customer being quoted a payout this desk cannot sign"
    )
    assert unarmed["reason"].startswith(CANNOT_PAY_OPENING), (
        f"the unarmed refusal reads {unarmed['reason'][:160]!r}; it must be the payout refusal, which "
        f"names {SIGNING_SEED_ENV_VAR} -- the one thing left to export"
    )
    assert not unarmed["cannot_quote"], (
        "the unarmed pair is refused for the RESERVE, which is set. That reason would send the "
        "operator to configure a number that is already there and leave the seed unexported"
    )
