"""The posture canister's seed, and the mirror that must not drift from config.py.

Role: test module (verification only)
Reads: icp_operator_admin.py, swap_terminal/config.py, icp/dfx.json and the generated
      icp/operator_admin_init.did
Writes: nothing
Can move funds: no -- the tool under test prints and writes one candid file; the
      canister it describes holds no key and makes no outcall
Mainnet-safe: yes; nothing here opens a socket or reaches a replica

THE TEST THAT EARNS THIS FILE is test_the_generated_init_argument_matches_config,
and it is the whole reason the seed is a generated file rather than a hand-written
one. icp/operator_admin_init.did is a MIRROR of config.py (rule 5), written because
dfx cannot read a Python value -- DFINITY's own ledger documentation: "`dfx.json`
does not support referring to values through environment variables. Values must be
hardcoded in plain text."

A mirror with no check is a mirror that goes stale, and this one goes stale silently
in the direction that costs: `dfx deploy` succeeds, the canister installs a posture
that is not the configured one, and the console then displays it with authority. The
same shape as the ledger's init file going stale on a recreated container, which
tests/test_dfx_canisters_are_buildable.py records -- "the deploy succeeds, the ledger
mints its supply to an account nobody on this replica controls, and the first symptom
arrives at a transfer".

WHAT THE REST OF THESE COVER: the encoder, because a malformed candid blob fails at
the dfx call with a message about bytes rather than about pairs; and the derivation,
because "which assets have thresholds" is read out of Config rather than listed, and
a reader has to be able to trust that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config import Config  # noqa: E402

import icp_operator_admin  # noqa: E402
from icp_operator_admin import (  # noqa: E402
    CANISTER,
    GENERATED_HEADER,
    INIT_ARG_FILE,
    candid_seed,
    candid_text,
    configured_assets,
    seed_posture,
)

DFX = APP_ROOT / "icp" / "dfx.json"


class Stub:
    """A stand-in Config with two chains and nothing else.

    A CLASS RATHER THAN THE REAL Config for the derivation tests, so they assert
    what the code DOES with what it is given instead of re-stating today's
    environment. seed_posture() takes `config` as a parameter for exactly this.
    """

    # ClassVar, which is what config.Config annotates its own ALLOWED_PAIRS with
    # and what answers ruff's RUF012 honestly: a set on a class IS shared mutable
    # state, and saying so is better than a noqa claiming nobody mutates it.
    ALLOWED_PAIRS: ClassVar[set[tuple[str, str]]] = {("BTC", "GRC"), ("GRC", "BTC")}
    DEFAULT_FEE_BPS = 150
    BTC_MIN_CONFIRMATIONS = 2
    GRC_MIN_CONFIRMATIONS = 6
    # Lowercase and non-threshold attributes must be ignored by the derivation.
    btc_min_confirmations = 99
    SOMETHING_ELSE = 7


# --------------------------------------------------- the mirror and its authority


def test_the_generated_init_argument_matches_config():
    """The file dfx reads must say what config.py says. THE REASON THIS FILE EXISTS.

    If this fails, somebody changed ALLOWED_PAIRS, DEFAULT_FEE_BPS or a
    *_MIN_CONFIRMATIONS and did not regenerate:

        python3 icp_operator_admin.py --write

    A STALE MIRROR IS WORSE THAN NO MIRROR HERE, because nothing announces it: the
    deploy succeeds, the canister holds a posture nobody chose, and the console
    renders it in the same confident type as a correct one. That is the ledger's
    stale-init-file failure with different values in it.
    """
    assert INIT_ARG_FILE.exists(), (
        f"{INIT_ARG_FILE} is missing and dfx.json names it as init_arg_file, so `dfx deploy "
        f"{CANISTER}` cannot build an argument blob. Generate it: python3 "
        f"icp_operator_admin.py --write"
    )
    assert INIT_ARG_FILE.read_text() == GENERATED_HEADER + candid_seed(seed_posture()) + "\n", (
        "icp/operator_admin_init.did and swap_terminal/config.py disagree. dfx reads the FILE, "
        "so a deploy now would install a posture that is not the configured one. Regenerate: "
        "python3 icp_operator_admin.py --write"
    )


def test_dfx_names_the_generated_file_as_the_init_argument():
    """Without this line in dfx.json, `dfx deploy` dies on "Expected arguments but found none".

    tests/test_dfx_canisters_are_buildable.py already holds that invariant for every
    canister; this asserts the specific path, because that gate would also be
    satisfied by an init_arg_file pointing somewhere this tool never writes.
    """
    spec = json.loads(DFX.read_text())["canisters"][CANISTER]
    assert spec.get("init_arg_file") == INIT_ARG_FILE.name
    assert (APP_ROOT / "icp" / spec["init_arg_file"]).resolve() == INIT_ARG_FILE.resolve()


def test_the_generated_file_carries_no_account_identifier():
    """So that tracking it stays legitimate rather than becoming the ledger's hazard.

    tests/test_dfx_canisters_are_buildable.py measures "environment state" as a
    64-hex account identifier, because the same identity has a different account on
    every fresh replica. A posture has none -- which is what makes this file
    trackable, and what would stop being true if somebody ever seeded an address
    into it.
    """
    import re  # noqa: PLC0415 -- checked: one pattern, used only here.

    text = INIT_ARG_FILE.read_text()
    assert not re.search(r"\b[0-9a-f]{64}\b", text), (
        "a 64-hex value reached the seed. That is environment state, so this file would have to "
        "become gitignored -- and a gitignored init_arg_file means a fresh checkout cannot deploy "
        "without a generation step"
    )


# --------------------------------------------------------------- the derivation


def test_the_threshold_assets_are_derived_and_not_listed():
    """`<ASSET>_MIN_CONFIRMATIONS` is the rule, so a new chain is covered by construction.

    MUTATION: drop the `.isupper()` check and `btc_min_confirmations` is picked up
    as an asset called "btc", which validate_asset() in the canister then refuses --
    a deploy that fails on a lowercase asset nobody typed.
    """
    assert configured_assets(Stub) == ["BTC", "GRC"]


def test_the_real_config_declares_a_threshold_for_every_chain_the_terminal_serves():
    """Six chains, six thresholds. A chain with none would be seeded without one.

    This is about the SEED being complete rather than about config.py being right:
    `confirmations_for()` in the canister answers None for an absent asset, and a
    caller that gets None must refuse -- so an asset missing here would quietly
    become unquotable if the terminal were ever wired to read from the canister.
    """
    assert configured_assets() == ["BTC", "GRC", "ICP", "LTC", "SOL", "XRP"]


def test_the_seed_is_read_not_decided():
    """Every value is whatever config says, with no filtering and no defaulting."""
    seed = seed_posture(Stub)
    assert seed["pairs"] == [("BTC", "GRC"), ("GRC", "BTC")]
    assert seed["fee_bps"] == 150
    assert seed["confirmations"] == [("BTC", 2), ("GRC", 6)]


def test_the_seed_is_sorted_so_two_runs_produce_the_same_bytes():
    """ALLOWED_PAIRS is a set, and an unsorted seed would churn the generated file.

    A mirror whose bytes change without its meaning changing makes every diff
    unreadable and every regenerate look like a posture change.
    """
    assert seed_posture()["pairs"] == sorted(seed_posture()["pairs"])
    assert candid_seed(seed_posture()) == candid_seed(seed_posture())


def test_armed_is_empty_and_is_never_guessed():
    """Whether a chain can pay out is adapter.can_spend, not a config value.

    It depends on a keypair file existing (SOL) or a passphrase being in the
    environment at send time (GRC), so seeding it from anything available to this
    tool would be a claim about ARMED STATE -- which is the category rule 16 sends
    back to the operator rather than deriving.
    """
    assert seed_posture()["armed"] == []
    assert seed_posture(Stub)["armed"] == []


# ------------------------------------------------------------------ the encoder


@pytest.mark.parametrize(("value", "expected"), [
    (True, "true"),
    (False, "false"),
    (150, "150"),
    ("BTC", '"BTC"'),
    (("BTC", 2), 'record { "BTC"; 2 }'),
    ([], "vec {  }"),
    (["BTC", "GRC"], 'vec { "BTC"; "GRC" }'),
    ({"from": "BTC", "to": "GRC"}, 'record { from = "BTC"; to = "GRC" }'),
])
def test_the_encoder_writes_the_candid_dfx_expects(value, expected):
    assert candid_text(value) == expected


def test_a_quote_in_a_string_is_escaped_rather_than_breaking_the_blob():
    """An asset code should never contain one, and a silent break would be worse.

    A malformed argument fails at the dfx call with a message about bytes, which is
    a long way from the pair that caused it.
    """
    assert candid_text('a"b') == '"a\\"b"'
    assert candid_text("a\\b") == '"a\\\\b"'


def test_a_value_with_no_encoding_raises_rather_than_guessing():
    with pytest.raises(TypeError, match="no candid encoding"):
        candid_text(1.5)


def test_the_numeric_widths_are_annotated_because_candid_infers_int():
    """`fee_bps : nat16` and an unannotated 150 is a TYPE ERROR at the dfx call.

    MUTATION: drop the `: nat16` suffix. This test fails, and without it the only
    symptom would be a deploy that refuses with a candid type error after the
    canister has already been created.
    """
    seed = candid_seed(seed_posture())
    assert "fee_bps = 150 : nat16" in seed
    assert "2 : nat32" in seed, "the confirmation counts need their width too"


def test_the_whole_seed_is_one_line_of_parseable_candid():
    """Balanced and complete, so a reader can see it is a single record.

    Not a candid parser -- that would be a second implementation of somebody else's
    grammar (rule 8) -- but the two failures a hand-rolled encoder actually has are
    unbalanced braces and a dropped field, and both are visible from here.
    """
    seed = candid_seed(seed_posture())
    assert seed.startswith("(record {")
    assert seed.endswith("})")
    assert seed.count("{") == seed.count("}")
    assert seed.count("(") == seed.count(")")
    for field in ("pairs =", "fee_bps =", "confirmations =", "armed ="):
        assert field in seed, f"the seed is missing {field}"
    assert "\n" not in seed, "one line, so it pastes into a shell without continuation"


# ------------------------------------------------------------------- the tool


def test_the_tool_prints_and_writes_nothing_but_the_init_argument(capsys):
    """Its header says PRINTS ONLY, and the only exception is --write.

    Asserted over the source rather than by running it, because "writes nothing" is
    a claim about every path including the ones a test does not take.
    """
    source = (APP_ROOT / "icp_operator_admin.py").read_text()
    writes = [line for line in source.splitlines()
              if ".write_text(" in line and "INIT_ARG_FILE" not in line]
    assert writes == [], f"a write to something other than the init argument: {writes}"
    assert "subprocess" not in source, "it prints commands; it does not run them"
    assert "requests" not in source, "it reaches nothing"


def test_a_plain_run_changes_nothing_and_says_the_mirror_is_in_step(capsys):
    assert icp_operator_admin.main([]) == 0
    out = capsys.readouterr().out
    assert "PRINTS ONLY" in out
    assert f"{INIT_ARG_FILE.name} matches config.py" in out
    assert f"dfx deploy {CANISTER}" in out


def test_the_run_states_every_count_with_what_it_came_from(capsys):
    """Rule 3: a number with no denominator, and rule 14: say what it means.

    Every figure on that screen is one somebody will compare against config.py, so
    each says which attribute produced it.
    """
    icp_operator_admin.main([])
    out = capsys.readouterr().out
    assert f"pairs         {len(Config.ALLOWED_PAIRS)}  <- Config.ALLOWED_PAIRS" in out
    assert f"fee           {Config.DEFAULT_FEE_BPS} bps  <- Config.DEFAULT_FEE_BPS" in out
    assert "armed         (none)" in out, "an empty list must print as a result, not a blank"


def test_every_run_says_the_canister_is_not_wired(capsys):
    """A surface that looks authoritative and is not is worse than no surface.

    The canister's console carries the same sentence on every page; this is the
    half of it that reaches somebody who never opens the page.
    """
    icp_operator_admin.main([])
    out = capsys.readouterr().out
    assert "NOT WIRED" in out
    assert "nothing else" in out
    assert "when the replica is down" in out, "the undecided question must be named, not implied"


def test_a_canister_id_turns_the_console_url_into_a_real_one(capsys):
    icp_operator_admin.main(["--canister-id", "be2us-64aaa-aaaaa-qaabq-cai"])
    out = capsys.readouterr().out
    assert "http://be2us-64aaa-aaaaa-qaabq-cai.localhost:4943/" in out


def test_without_a_canister_id_it_says_how_to_find_one(capsys):
    icp_operator_admin.main([])
    out = capsys.readouterr().out
    assert "canister_ids.json" in out, "rule 14: name the command rather than the gap"
