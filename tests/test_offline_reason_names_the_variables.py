"""What a customer is told, and what an operator is told, when BTC and LTC are unconfigured.

Role: test (behavioral verification of services/pair_view.py's two strings, of
      chains/registry.why_unconfigured()'s completeness, and of the derivation
      chains/registry.missing_settings() performs on config.py's variable names)
Reads: the real Flask app's test client, a copy of config.Config.RPC with the
      BTC and LTC settings emptied, and swap_terminal/config.py's own source
Writes: nothing. No database row, no file, no environment variable.
Can move funds: no. Nothing here constructs an adapter that can spend, nothing
      POSTs, and the only adapters dict any test installs is the EMPTY one.
Mainnet-safe: yes -- no socket is opened to any chain. build_adapters() opens
      none by construction (chains/registry.py's header makes that promise), and
      every input below is a mapping built in this file.

WHY THIS FILE EXISTS, measured 2026-10-03.

The operator reported BTC -> LTC and LTC -> BTC rendering OFFLINE on the
customer page, and reported the reason as reading roughly "no credentials in
this shell". Two different strings answer that description and they live on two
different pages, which is the thing this file pins so the next report can be
read without re-deriving it:

    the row `reason`          chains/registry.why_unconfigured(), one paragraph
                              per unconfigured chain, naming the variables.
                              Rendered on /admin ONLY -- once per ASSET in the
                              Chains table.
    the customer `note`       services/pair_view._CUSTOMER_AVAILABILITY's
                              "unreachable" entry. Rendered on / ONLY -- once in
                              the key, not once per tile. Names no variable, on
                              purpose, and that comment says why: a customer has
                              no shell on this machine, and the page is
                              unauthenticated on the same port as /admin.

MEASURED, by running the real functions and rendering the real templates against
a config whose BTC and LTC entries carry no port, user or password -- which is
exactly what config.py produces from an environment with no BTC_RPC_* or
LTC_RPC_* exported:

    adapters built                     []  (build_adapters, real)
    missing_settings(BTC)              ['BTC_RPC_PORT', 'BTC_RPC_USER', 'BTC_RPC_PASS']
    missing_settings(LTC)              ['LTC_RPC_PORT', 'LTC_RPC_USER', 'LTC_RPC_PASS']
    customer tile  BTC -> LTC          swaptile-unreachable, word OFFLINE
    customer note                      "One of these two chains is not reachable
                                        from this server right now."
    BTC_RPC_PORT appears on /          no   (0 occurrences)
    BTC_RPC_PORT appears on /admin     yes
    "has no adapter in this process" on /admin   6

THE COMPLETENESS QUESTION, WHICH IS THE ONE WORTH A TEST. A reason that named
BTC_RPC_PORT alone would be the 2026-09-26 incident again from the other end:
that day the operator's GRC_RPC_PORT was correct and GRC_RPC_PASS was the
missing value, under a name nothing in the tree reads. An operator who exports
the one variable a message names, restarts, and sees the same OFFLINE badge has
been sent round a loop by the output. So the assertion here is on the FULL set
of three per chain, and on both chains of the pair rather than the first.

AND ON THE DERIVATION ITSELF, which was the one unpinned string in the path.
chains/registry.missing_settings() takes the primary name from
network_target.configuring_variable() -- checked against config.py's source by
tests/test_network_target.test_configuring_variable_matches_what_config_py_actually_reads()
-- but DERIVES the credential names as f"{asset}_RPC_USER" / f"{asset}_RPC_PASS",
and nothing checked those against config.py. A rename on either side would have
produced a reason naming a variable nothing reads, which is the precise failure
mode the GRC_TESTNET_RPC_PASS incident is. test_the_derived_credential_names_...
below closes that by reading config.py.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT: that the customer note SHOULD name
a variable. It must not, and services/pair_view._CUSTOMER_AVAILABILITY's comment
is the authority for that. The assertion runs the other way -- that no variable
name reaches /, and that every one of them reaches /admin.
"""

import re
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from chains.registry import build_adapters, missing_settings, why_unconfigured
from config import Config
from db import SCHEMA, dict_factory
from services.pair_view import allowed_pair_rows, customer_availability

# `app` imports and calls create_app() at module scope, and conftest.py has
# already pointed SWAP_DB_PATH at a temp file by the time this import runs. Same
# import-order note tests/test_web_surfaces.py carries, for the same reason.
import app as app_module  # isort: skip

#: The pair the operator reported, both directions. A pair and not a chain,
#: because the row reason joins BOTH sides with " Also: " and a test that looked
#: at one chain would pass while the second half of the sentence was missing.
REPORTED_PAIRS = (("BTC", "LTC"), ("LTC", "BTC"))

#: Every variable an operator must export before chains/registry.build_adapters()
#: will construct these two chains, spelled out ONCE here so the assertions below
#: read as comparisons rather than as a set built from the function under test.
#:
#: NOT derived from missing_settings() on purpose: deriving the expectation from
#: the thing being measured is how a test comes to assert that a function equals
#: itself. These three suffixes are chains/registry._REQUIRED_SETTINGS' entry for
#: a Bitcoin-derived chain, and config.py is what pins the spelling (see
#: test_the_derived_credential_names_are_what_config_py_actually_reads below).
EXPECTED_SETTINGS = {
    "BTC": ["BTC_RPC_PORT", "BTC_RPC_USER", "BTC_RPC_PASS"],
    "LTC": ["LTC_RPC_PORT", "LTC_RPC_USER", "LTC_RPC_PASS"],
}


def unconfigured_rpc() -> dict:
    """Config.RPC with BTC and LTC emptied, which is what an unset shell produces.

    A COPY OF THE REAL MAPPING rather than a hand-built one, so the entries keep
    every key chains/registry.build_adapters() splats into the adapter
    constructors. A hand-built two-key dict would pass this file and tell us
    nothing about the real shape -- and would go on passing if config.py grew a
    seventh key the constructors needed.

    port 0 / user "" / password "" is not an invented sentinel: config.py
    defaults the port to network_target.UNCONFIGURED_PORT (0) and both
    credentials to "" precisely so that a key is always PRESENT and falsiness is
    the test. See chains/registry.missing_settings()'s docstring, which says why
    that is deliberate.

    GRC, SOL and XRP are left exactly as the environment running this suite has
    them. That is deliberate too: the operator's report is about BTC and LTC, and
    emptying the other three as well would make this file assert about a server
    posture nobody has.
    """
    rpc = deepcopy(dict(Config.RPC))
    for asset in EXPECTED_SETTINGS:
        rpc[asset] = {**rpc[asset], "port": 0, "user": "", "password": ""}
    return rpc


@pytest.fixture
def seeded_config() -> dict:
    """The (config, adapters) pair every test below measures against.

    Returned as a dict with both halves because services/pair_view takes them as
    two arguments and they must agree: adapters built from a DIFFERENT mapping
    than the one `reason` is derived from is the 2026-10-02 defect
    services/pair_view.py's header records, where two surfaces disagreed about
    one pair in one process.
    """
    rpc = unconfigured_rpc()
    return {
        "config": {"ALLOWED_PAIRS": Config.ALLOWED_PAIRS, "RPC": rpc},
        "adapters": build_adapters(rpc),
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app on a per-test database, with the unconfigured RPC mapping installed.

    BOTH app.config["RPC"] AND app.config["ADAPTERS"] are replaced, from the one
    mapping. Replacing only ADAPTERS would leave why_unconfigured() reading the
    real Config.RPC, so the page would badge a pair OFFLINE and then name
    whichever variables this machine's shell happens to be missing -- a test
    whose result depends on the shell that started it, which conftest.py's own
    header calls a suite nobody can use to establish anything.
    """
    db_path = tmp_path / "offline_reason.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    rpc = unconfigured_rpc()
    flask_app = app_module.app
    monkeypatch.setitem(flask_app.config, "DB_PATH", str(db_path))
    monkeypatch.setitem(flask_app.config, "RPC", rpc)
    monkeypatch.setitem(flask_app.config, "ADAPTERS", build_adapters(rpc))
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


def visible_text(markup: str) -> str:
    """Tags stripped and whitespace collapsed, so an assertion matches what is READ.

    A variable name split across a line break by the template's own wrapping is
    still a variable name on the screen, and a raw-bytes `in` test would miss it.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup))


# --- what the two surfaces actually say ---------------------------------------


def test_the_customer_tile_for_the_reported_pairs_is_offline_and_names_no_variable(client):
    """MEASURED 2026-10-03: the customer sees the word OFFLINE and no setting at all.

    Through the real app and the real template, because the artifact a customer
    reads is the rendered page. The three things asserted are the three a reader
    of the operator's report needs:

      the STATE   services/pair_view.customer_availability() returns the
                  "unreachable" key for a row whose `missing` is non-empty, and
                  templates/index.html renders it as a `swaptile-unreachable`
                  class plus the word OFFLINE.
      the NOTE    the generic sentence, present ONCE in the key rather than once
                  per tile. Asserting it is present at all is the rule 14 half:
                  a glyph whose word has no entry in the key is a glyph a
                  customer cannot look up.
      the SILENCE no BTC_RPC_* or LTC_RPC_* name anywhere on the page. This page
                  is unauthenticated on the same port as /admin, so the names of
                  unset variables on this host are a small disclosure as well as
                  being useless to their reader.
    """
    body = client.get("/").get_data(as_text=True)
    text = visible_text(body)

    for from_asset, to_asset in REPORTED_PAIRS:
        tile = re.search(
            rf'<li class="swaptile swaptile-(\w+)">\s*<span class="pair-label">\s*'
            rf"{from_asset}\s*&#8594;\s*{to_asset}\s*</span>(.*?)</li>",
            body,
            re.DOTALL,
        )
        assert tile, f"the customer page rendered no tile for {from_asset} -> {to_asset}"
        assert tile.group(1) == "unreachable", (
            f"{from_asset} -> {to_asset} rendered as {tile.group(1)!r}; with neither chain's "
            "adapter constructed the state is 'unreachable'"
        )
        assert "OFFLINE" in visible_text(tile.group(2)), (
            f"the {from_asset} -> {to_asset} tile carries no word beside its glyph"
        )

    assert "One of these two chains is not reachable from this server right now." in text, (
        "the OFFLINE note is missing from the key, so the word on the tile cannot be looked up"
    )

    for names in EXPECTED_SETTINGS.values():
        for name in names:
            assert name not in body, (
                f"the customer page named {name}. The customer-facing note is deliberately "
                "generic -- see services/pair_view._CUSTOMER_AVAILABILITY -- and this page is "
                "unauthenticated on the same port as /admin"
            )


def test_the_row_reason_names_every_variable_for_both_chains_of_the_pair(seeded_config):
    """MEASURED 2026-10-03: the reason names three variables PER CHAIN, and both chains.

    THE FULL SET, NOT THE FIRST. On 2026-09-26 the operator's GRC_RPC_PORT was
    already correct and GRC_RPC_PASS was the missing value, so a message naming
    only the port would have sent them to check the one setting that needed
    nothing. The same message naming only BTC_RPC_PORT here would send them to
    export one of three, restart, and read the identical OFFLINE badge.

    AND BOTH CHAINS, because pair_serviceability() joins the two sides with
    " Also: " and a single-chain assertion would pass while the second paragraph
    was absent.
    """
    rows = {(row["from_asset"], row["to_asset"]): row for row in allowed_pair_rows(
        seeded_config["config"], seeded_config["adapters"]
    )}

    for pair in REPORTED_PAIRS:
        row = rows[pair]
        assert row["enabled"] is False
        assert row["missing"] == list(pair), (
            f"{pair} reported missing={row['missing']}; with neither adapter constructed both "
            "sides are missing, in the order the pair names them"
        )
        for asset in pair:
            for name in EXPECTED_SETTINGS[asset]:
                assert name in row["reason"], (
                    f"the reason for {pair} does not name {name}, which is one of the "
                    f"{len(EXPECTED_SETTINGS[asset])} settings chains/registry.build_adapters() "
                    f"needs before it constructs {asset}. An operator who exports only what this "
                    "string names gets the same OFFLINE badge back"
                )
        assert " Also: " in row["reason"], (
            "both chains are unconfigured, so both paragraphs belong in the reason"
        )


def test_the_customer_note_and_the_row_reason_are_different_strings(seeded_config):
    """The two answers to "what does the page say" are not the same answer.

    Pinned because the operator's report named one string and two exist, and
    because the thing that would break this is a well-meant edit putting the
    reason back on the customer tile -- which is what templates/index.html
    removed on 2026-10-02 after it rendered the export sentence eleven times in
    a thirteen-tile paste.
    """
    row = next(
        r
        for r in allowed_pair_rows(seeded_config["config"], seeded_config["adapters"])
        if (r["from_asset"], r["to_asset"]) == ("BTC", "LTC")
    )
    note = customer_availability(row)["note"]

    assert note != row["reason"]
    assert note == "One of these two chains is not reachable from this server right now."
    assert "BTC_RPC" not in note and "export" not in note, (
        "the customer-facing note names no variable and promises no remedy; see "
        "services/pair_view._CUSTOMER_AVAILABILITY"
    )


def test_the_operator_page_names_every_variable_the_customer_page_withholds(client):
    """MEASURED 2026-10-03: all six names reach /admin, where the reader has a shell.

    THE PAIRING WITH THE TEST ABOVE IS THE POINT. "Nothing on / names a variable"
    is only safe if the information did not leave the SYSTEM, which is the audit
    templates/index.html's comment records having done by hand on 2026-10-02.
    This is that audit as an assertion: whatever the customer page withholds, the
    operator page says.

    Asserted against /admin's rendered bytes rather than against
    services/admin_view.chain_rows()'s dict, because the failure that matters is
    a template that stops printing a field -- `why_unconfigured` is rendered
    inside an `{% if %}` on the Chains table's endpoint cell, and a view function
    returning it correctly proves nothing about whether the cell shows it.
    """
    body = client.get("/admin").get_data(as_text=True)
    text = visible_text(body)

    for asset, names in EXPECTED_SETTINGS.items():
        for name in names:
            assert name in text, (
                f"/admin does not name {name}. The operator is the reader who can export it, and "
                f"this page is the only surface that still carries {asset}'s reason"
            )
    assert "has no adapter in this process" in text


# --- the one string in the path that is not taken from network_target ---------


def test_the_derived_credential_names_are_what_config_py_actually_reads():
    """Asserted against config.py's SOURCE, not against a list spelled only here.

    WHY THIS WAS A GAP. chains/registry.missing_settings() takes the primary name
    from network_target.configuring_variable(), and
    tests/test_network_target.test_configuring_variable_matches_what_config_py_actually_reads()
    greps config.py for it. The two CREDENTIAL names are not taken from anywhere
    -- they are built as f"{asset}_RPC_USER" and f"{asset}_RPC_PASS" inside
    missing_settings() -- and until 2026-10-03 nothing compared them with
    config.py. A rename on either side would have produced a reason naming a
    variable nothing in the tree reads, which is exactly the 2026-09-26 incident:
    the operator HAD the value, under GRC_TESTNET_RPC_PASS, and no message could
    connect the two.

    MATCHED AS A PATTERN rather than one literal, following the sibling test in
    tests/test_network_target.py: config.py's reads go through _env / _env_int /
    _env_float so a set-but-empty variable falls back instead of raising, and
    pinning one spelling would fail the day a fourth helper is added while the
    claim being made -- "config.py reads this variable" -- stayed true.

    ALL THREE BITCOIN-DERIVED CHAINS, not the two in the report. GRC shares the
    derivation and the suffixes, so a test covering only BTC and LTC would leave
    the chain the 2026-09-26 incident actually happened on unpinned.
    """
    source = (Path(__file__).resolve().parent.parent / "swap_terminal" / "config.py").read_text()

    for asset in ("BTC", "LTC", "GRC"):
        names = missing_settings({asset: {"port": 0, "user": "", "password": ""}}, asset)
        assert len(names) == 3, (
            f"missing_settings({asset!r}) returned {names}; build_adapters() requires a port and "
            "both halves of the HTTP credential, so an empty entry is missing three things"
        )
        for name in names:
            assert re.search(rf'_env(?:_int|_float)?\("{re.escape(name)}"', source), (
                f"missing_settings() names {name!r} for {asset}, which config.py never reads. "
                "An operator told to export it would export a variable nothing looks at"
            )
        assert names == EXPECTED_SETTINGS.get(asset, names), (
            f"missing_settings({asset!r}) returned {names}, which is not the set this file "
            "measured on 2026-10-03"
        )


def test_the_reason_degrades_to_the_primary_name_rather_than_raising_without_an_rpc_mapping():
    """why_unconfigured() with no mapping names ONE variable, and that is the known limit.

    Pinned because it is the branch that WOULD look like the defect this file was
    opened to find. Called without `rpc` the function cannot know which settings
    are missing, so it names the chain's primary one -- which is incomplete by
    construction, and is why its own docstring says to pass the mapping whenever
    you have it. services/pair_view.pair_serviceability() and
    services/admin_view.chain_rows() both pass `config.get("RPC")`, which is what
    makes the complete sentence reach /admin; this test exists so that a future
    reader who finds a one-variable reason on some surface knows to look for the
    caller that dropped the mapping rather than at this function.
    """
    degraded = why_unconfigured("BTC")

    assert "BTC_RPC_PORT" in degraded
    assert "BTC_RPC_PASS" not in degraded, (
        "without the mapping there is nothing to say a credential is missing; claiming it "
        "would be a guess presented as a measurement (rule 17)"
    )
    assert "BTC_RPC_PORT, BTC_RPC_USER and BTC_RPC_PASS" in why_unconfigured(
        "BTC", unconfigured_rpc()
    ), "with the mapping the same function names the complete set"
