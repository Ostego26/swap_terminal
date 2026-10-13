#!/usr/bin/env python3
"""The comparison needs the values. The REPORT must never be able to hold one.

Role: tests (read-only)
Reads: chains/credential_match.py, and credential_check.py through
       conftest.root_entry_point(). Conf files are written under tmp_path.
Writes: files under tmp_path only
Can move funds: no
Mainnet-safe: yes, and the mainnet refusal is exercised here rather than assumed

WHY THIS FILE EXISTS, AND THE FIRST TEST IS THE WHOLE POINT.

CLAUDE.md: a credential is "never read back into a terminal, and never printed into a
log or a commit." Comparing two credentials requires both of them; reporting the result
must not leak either. So the gate below seeds values that could not occur by accident
and asserts that no string any function returns, and nothing the entry point prints,
contains them.

THAT IS NOT A STYLE CHECK. This repository published a live GRIDCOIN_RPC_PASSWORD to
GitHub once, by committing a .env backup -- CLAUDE.md rule 2 tells that story -- and the
remediation is still open in OPEN_FINDINGS. A tool whose job is to look at two passwords
is the one that has to be unable to say them.

WHY THE TOOL EXISTS AT ALL, which is a correction of mine. The operator's GRC daemon
answered 401 from the host on loopback, which rules out rpcallowip, the bind and the
firewall together. I asked them to `grep -cE '^(rpcuser|rpcpassword)'` both conf files
and said "a 0 for the testnet file is the whole answer". It came back 2 and 2: the check
tested PRESENCE where the open question was EQUALITY, and
regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK had already said the net-specific file
"carries DIFFERENT credentials from the mainnet file beside it".
"""

from __future__ import annotations

import pytest
from chains import registry
from chains.credential_match import (
    BOTH_ABSENT,
    DIFFERENT,
    ONLY_IN_CONF,
    ONLY_IN_ENV,
    SAME,
    compare,
    headline,
    how_to_settle_a_401,
)
from chains.daemon_capabilities import refusal_remedy
from conftest import root_entry_point
from regtest.daemons import GRC_CREDENTIALS_ARE_PER_NETWORK

checker = root_entry_point("credential_check.py", "credential_check_entry")

#: Values that cannot occur by accident, so a substring search for them is conclusive.
#: Not realistic credentials -- a realistic one would risk somebody pasting it somewhere
#: -- and deliberately not short, because a three-character value could appear inside an
#: unrelated word and make this gate pass by coincidence.
#:
#: S105 ("possible hardcoded password") IS SUPPRESSED ON TWO LINES AND THE CLAIM IS THIS:
#: these are not credentials and authenticate nothing. They are tracer values whose only
#: property is that `sentinel in output` cannot match by coincidence, which is exactly
#: what makes the leak gate conclusive. A generated value would defeat the purpose -- the
#: assertion needs to know what to look for -- and reading one from the environment would
#: make the gate depend on the thing it is testing. Rule 19: a noqa is a claim somebody
#: checked, and this is the check.
ENV_USER = "zzz-env-user-sentinel-7f3a"
ENV_PASS = "zzz-env-pass-sentinel-91c4"  # noqa: S105 -- a tracer, not a credential: see above
CONF_USER = "zzz-conf-user-sentinel-b5e8"
CONF_PASS = "zzz-conf-pass-sentinel-2d6f"  # noqa: S105 -- a tracer, not a credential: see above
SENTINELS = (ENV_USER, ENV_PASS, CONF_USER, CONF_PASS)


def test_NO_VALUE_can_reach_anything_this_module_returns():
    """THE GATE. Every string out of compare() and headline(), against four sentinels.

    Checked over the FULL CROSS PRODUCT of mismatch shapes rather than one, because a
    leak would most plausibly arrive in an error path -- the branch that wants to say
    "expected X, got Y" is exactly the branch somebody adds to be helpful.
    """
    postures = [
        ({"user": ENV_USER, "password": ENV_PASS}, {"rpcuser": CONF_USER, "rpcpassword": CONF_PASS}),
        ({"user": ENV_USER, "password": ENV_PASS}, {"rpcuser": ENV_USER, "rpcpassword": ENV_PASS}),
        ({"user": ENV_USER, "password": ENV_PASS}, {}),
        ({}, {"rpcuser": CONF_USER, "rpcpassword": CONF_PASS}),
        ({"user": ENV_USER, "password": ""}, {"rpcuser": "", "rpcpassword": CONF_PASS}),
    ]
    for configured, conf_values in postures:
        verdicts = compare(configured, conf_values)
        emitted = " ".join(
            [headline(verdicts)]
            + [f"{v.label} {v.env_key} {v.conf_key} {v.verdict}" for v in verdicts]
        )
        for sentinel in SENTINELS:
            assert sentinel not in emitted, (
                f"{sentinel!r} reached the output for {configured!r} vs {conf_values!r}. A "
                f"comparison may hold a credential; a report may not."
            )


def test_the_verdict_type_has_no_field_a_value_could_occupy():
    """Structural, so a leak cannot be ADDED without this failing.

    The first test checks today's strings; this checks the shape. A `FieldVerdict` with
    an `expected` or `actual` field would pass the substring test the day it was added
    and then be filled in later -- which is how the broad-except defects in this tree
    got there, one honest step at a time.
    """
    verdicts = compare({"user": ENV_USER, "password": ENV_PASS}, {})

    assert verdicts
    assert set(verdicts[0]._fields) == {"label", "env_key", "conf_key", "verdict"}, (
        "FieldVerdict gained a field. If it can hold a value, the report can print one."
    )


@pytest.mark.parametrize("configured,conf_values,expected", [
    ({"user": "a", "password": "b"}, {"rpcuser": "a", "rpcpassword": "b"}, [SAME, SAME]),
    ({"user": "a", "password": "b"}, {"rpcuser": "a", "rpcpassword": "WRONG"}, [SAME, DIFFERENT]),
    ({"user": "a", "password": "b"}, {}, [ONLY_IN_ENV, ONLY_IN_ENV]),
    ({}, {"rpcuser": "a", "rpcpassword": "b"}, [ONLY_IN_CONF, ONLY_IN_CONF]),
    ({}, {}, [BOTH_ABSENT, BOTH_ABSENT]),
    # EMPTY IS ABSENT, which is config.py's own convention: an unset variable arrives as
    # "" rather than as a missing key, and neither can authenticate.
    ({"user": "", "password": ""}, {"rpcuser": "", "rpcpassword": ""}, [BOTH_ABSENT, BOTH_ABSENT]),
])
def test_the_five_verdicts_are_five(configured, conf_values, expected):
    """None collapses into another.

    BOTH_ABSENT must not read as a match -- an unencrypted regtest daemon with no
    credentials at all is a real state, and calling it SAME would tell an operator the
    login is fine when there is no login. That is rule 13's "did nothing must not look
    like did work" on an authentication question.
    """
    assert [verdict.verdict for verdict in compare(configured, conf_values)] == expected


def test_every_field_matching_points_at_the_WRONG_FILE_rather_than_declaring_success():
    """The sentence that matters when the 401 persists.

    Gridcoin uses a separate conf per network and the file itself does not say which
    network it belongs to, so "these match" and "the daemon still refuses" together mean
    the comparison was made against a file the daemon never read. Saying only "MATCH"
    there would end the investigation at the wrong place.
    """
    said = headline(compare({"user": "a", "password": "b"}, {"rpcuser": "a", "rpcpassword": "b"}))

    assert "EVERY FIELD MATCHES" in said
    assert "NOT the file it read" in said
    assert "Using data directory" in said, "it must name how to find the file the daemon chose"


def test_a_mainnet_port_is_refused_before_any_conf_is_opened():
    """Reading the staking wallet's conf for a diagnostic is itself the hazard.

    chain_daemons.refuse_to_touch() will not STOP a mainnet daemon and
    chains/daemon_launch.propose() will not SUGGEST starting one; this will not open its
    conf. Three verbs, one rule, and the refusal names the chain rather than the number.
    """
    for port, label in sorted(checker.chain_daemons.MAINNET_RPC_PORTS.items()):
        said = checker.refusal("GRC", {"port": port})
        assert "REFUSED" in said
        assert label in said, f"the refusal must name {label}"
    assert checker.refusal("GRC", {"port": 25779}) == "", (
        "a test port must not be refused, or the tool refuses everything"
    )


def test_an_unreadable_conf_exits_2_and_not_1(tmp_path, capsys):
    """Rule 13: a comparison that did not happen is not a comparison that failed.

    Exit 1 means "a field differs" -- a verdict about the credentials. A missing file is
    a verdict about a path, and collapsing the two would make `credential_check.py ||
    something` fire on a typo as though the login were wrong.
    """
    assert checker.main(["--asset", "GRC", "--conf", str(tmp_path / "nope.conf")]) == 2
    printed = capsys.readouterr().out

    assert "REFUSED" in printed
    assert "Nothing was compared" in printed


def test_the_entry_point_prints_no_value_either(tmp_path, capsys, monkeypatch):
    """End to end, with sentinels on BOTH sides and a real conf file on disk.

    The module gate above covers its own return values; this covers the one place a
    value could still arrive -- the report, where the conf has been parsed and both
    sides are in scope at once.
    """
    conf = tmp_path / "gridcoinresearch.conf"
    conf.write_text(f"rpcuser={CONF_USER}\nrpcpassword={CONF_PASS}\nrpcport=25779\n")
    monkeypatch.setitem(
        checker.Config.RPC, "GRC",
        {"user": ENV_USER, "password": ENV_PASS, "host": "127.0.0.1", "port": 25779},
    )

    code = checker.main(["--asset", "GRC", "--conf", str(conf)])
    printed = capsys.readouterr().out

    assert code == 1, "the sentinels differ, so a field differs"
    assert "DIFFERENT" in printed
    for sentinel in SENTINELS:
        assert sentinel not in printed, f"{sentinel!r} was printed by the report"
    # AND THE NAMES ARE PRESENT, because a verdict with nothing to attach it to is
    # useless: the reader has to know which variable and which conf key (rule 14).
    #
    # THIS ASSERTION USED TO BE `"GRC_RPC_PASSWORD" in printed or "GRC_RPC_PASS" in
    # printed` AND THAT `or` IS WHY THE DEFECT SHIPPED. The report printed
    # GRC_RPC_PASSWORD -- a variable this tree does not read -- and an `or` over the
    # right name and the wrong one passes on either, so the test asserted only that
    # SOME name was printed. A disjunction between a correct value and an incorrect one
    # is not a check; it is the shape of a check.
    #
    # AND THE ABSENCE ASSERTION IS THE ONE DOING THE WORK, because "GRC_RPC_PASS" is a
    # SUBSTRING of "GRC_RPC_PASSWORD": the positive assertion alone still passes on the
    # wrong name. Pinning the right name without also excluding the wrong one would
    # have let the identical defect through a second time, which is why both lines are
    # here and why this sentence names the reason rather than leaving a reader to
    # notice the substring themselves.
    assert "GRC_RPC_PASS" in printed, (
        "the report must name the variable config.py actually reads -- grep "
        "`_env(\"GRC_RPC_PASS` in Config.RPC"
    )
    assert "GRC_RPC_PASSWORD" not in printed, (
        "GRC_RPC_PASSWORD does not exist in this tree. An operator who exported the "
        "printed name would change nothing and see the same 401."
    )
    assert "rpcpassword" in printed


def test_the_printed_variable_name_comes_from_the_table_not_from_the_dict_key():
    """setting_variable() answers from _REQUIRED_SETTINGS; a constructed name cannot.

    The DEFECT ASSERTION, stated as the inequality that was shipped: for all three
    Bitcoin-derived chains the Config.RPC dict key is `password` and the variable is
    PASS, so f"{asset}_RPC_{key.upper()}" and the real name are different strings. This
    test fails if anybody reintroduces the construction, because the construction
    cannot produce what the table holds.
    """
    for asset in ("BTC", "LTC", "GRC"):
        assert registry.setting_variable(asset, "password") == f"{asset}_RPC_PASS"
        assert registry.setting_variable(asset, "user") == f"{asset}_RPC_USER"
        constructed = f"{asset}_RPC_PASSWORD"
        assert registry.setting_variable(asset, "password") != constructed, (
            f"{constructed} is what the f-string produced and it is not a variable this "
            f"tree reads"
        )


def test_setting_variable_refuses_to_invent_a_name():
    """"" for a key no chain declares, and for an asset the table does not carry.

    A caller that gets "" has to say it cannot name the variable. A caller that got a
    plausible name would print it, which is the whole defect this function replaced --
    so the empty answer is the feature and is pinned here rather than left to the
    `for` loop falling off its end by accident.
    """
    assert registry.setting_variable("GRC", "port") == "GRC_RPC_PORT", (
        "port maps to None in the table, which means ask configuring_variable()"
    )
    assert registry.setting_variable("GRC", "secret_sauce") == ""
    assert registry.setting_variable("DOGE", "password") == ""
    # And ICP, whose names are NOT shaped <ASSET>_RPC_* at all -- the case that proved
    # the suffix scheme wrong in registry.py's own table comment.
    assert registry.setting_variable("ICP", "owner_principal") == "ICP_OWNER_PRINCIPAL"


def test_a_matching_pair_exits_zero(tmp_path, capsys, monkeypatch):
    """So it is scriptable, and so the success path is exercised rather than assumed."""
    conf = tmp_path / "gridcoinresearch.conf"
    conf.write_text(f"rpcuser={ENV_USER}\nrpcpassword={ENV_PASS}\n")
    monkeypatch.setitem(
        checker.Config.RPC, "GRC",
        {"user": ENV_USER, "password": ENV_PASS, "host": "127.0.0.1", "port": 25779},
    )

    assert checker.main(["--asset", "GRC", "--conf", str(conf)]) == 0
    printed = capsys.readouterr().out
    assert "EVERY FIELD MATCHES" in printed
    for sentinel in SENTINELS:
        assert sentinel not in printed


# --- the two commands that settle a 401, and the ONE place they are spelled ------


def test_the_401_commands_name_flags_the_real_parsers_actually_take():
    """A remedy naming a flag that does not exist strands the reader it was written for.

    This sentence is printed by `swap_stack.py off`, `up` step 8 and `chains`, and read
    by somebody already stuck on a 401. Both commands are checked against the parsers
    themselves rather than spelled and trusted -- the same gate
    tests/test_swap_readiness.py puts on GRC_CREDENTIALS_ARE_PER_NETWORK, for the same
    reason: prose about an interface is worth what its test is worth.
    """
    sentence = how_to_settle_a_401("GRC")
    checker = root_entry_point("credential_check.py", "credential_check_401_commands")
    configurer = root_entry_point("configure_env.py", "configure_env_401_commands")

    assert "credential_check.py --asset GRC" in sentence
    for flag in ("--asset", "--conf"):
        assert flag in checker.build_parser().format_help(), f"credential_check.py lost {flag}"
    configure_help = configurer.build_parser().format_help()
    for flag in ("--read-daemon-confs", "--read-gridcoin-conf", "--conf", "--apply"):
        assert flag in sentence, f"the sentence stopped naming {flag}"
        assert flag in configure_help, f"configure_env.py lost {flag}"


def test_only_gridcoin_gets_the_gridcoin_only_opt_in():
    """--read-gridcoin-conf on a BTC command would be an argparse error, not a remedy.

    THE BRANCH IS THE DECISION and this is the whole of it: configure_env.py gates
    Gridcoin's conf behind its own flag because the DEFAULT datadir's file belongs to a
    live staking wallet, and BTC and LTC need no second flag. A sentence that printed it
    for every chain would hand a BTC operator a command that exits 2.
    """
    assert "--read-gridcoin-conf" in how_to_settle_a_401("GRC")
    assert "--read-gridcoin-conf" in how_to_settle_a_401("grc"), "the lookup must not be case-bound"
    for asset in ("BTC", "LTC"):
        assert "--read-gridcoin-conf" not in how_to_settle_a_401(asset)
        assert "--read-daemon-confs" in how_to_settle_a_401(asset)


def test_the_sentence_is_spelled_once_and_both_callers_derive_it():
    """MUTATION: spell the commands in either caller. Then GRC prints them TWICE.

    That is not hypothetical -- it was the state of the tree for about four hours on
    2026-10-13. regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK carried the commands
    inline, and when daemon_capabilities' 401 remedy needed them the duplication became
    visible: a GRC 401 in `swap_stack.py off` would have printed one copy from the
    recorded remedy and another from the shape command. daemon_capabilities' SHAPE_MEANS
    comment records catching the same thing once already.

    ASSERTED BY COUNTING, not by substring presence, because presence is what both a
    shared sentence and a duplicated one have.
    """
    shared = how_to_settle_a_401("GRC")

    assert GRC_CREDENTIALS_ARE_PER_NETWORK.count(shared) == 1, (
        "the GRC guidance does not derive the shared sentence, or spells a second copy"
    )
    remedy = refusal_remedy("GRC", "401 Client Error: Authorization Required")
    assert remedy.count("credential_check.py") == 1, (
        f"a GRC 401 names credential_check.py {remedy.count('credential_check.py')} times; "
        f"the recorded remedy and the shape command are both spelling it"
    )
    assert remedy.count("configure_env.py") == 1


def test_a_401_remedy_still_prints_no_value_and_no_path_it_cannot_know():
    """The placeholder is the finding, not a gap, and the commands carry no credential.

    `<the conf that daemon read>` stays a placeholder because three Gridcoin wallets run
    on this host and guessing the datadir is the failure measured on 2026-09-29 and
    again on 2026-10-12. A sentence that named one would be right one time in three, and
    the two times it was wrong it would send somebody to a different wallet's conf.
    """
    for asset in ("GRC", "BTC", "LTC"):
        sentence = how_to_settle_a_401(asset)
        for sentinel in SENTINELS:
            assert sentinel not in sentence
        assert "<" in sentence and ">" in sentence, (
            "the conf path stopped being a placeholder -- a named path is a guess at which "
            "of this host's three Gridcoin datadirs the daemon used"
        )
        assert "Using data directory" in GRC_CREDENTIALS_ARE_PER_NETWORK, (
            "nothing tells the reader where to GET the path the placeholder stands for"
        )


# --- the exact command, with the real path, in the tool they actually run -------


def test_a_DIFFERING_report_ends_with_the_complete_pasteable_command(tmp_path, capsys, monkeypatch):
    """THE GAP THAT COST THREE ROUNDS: the diagnostic proved the cause and not the fix.

    2026-10-13, three pastes in three messages, each `credential_check.py` then
    `swap_stack.py restart`, each DIFFERENT on both fields. The command that copies the
    credentials was in the 401 remedy and in a chat message -- not in the output of the
    tool being run, which ended at "Set the environment to match this file".

    NO PLACEHOLDER, which is the point. Every other surface must write `<the conf that
    daemon read>` because guessing the datadir is the measured failure. This tool takes
    --conf as a REQUIRED argument, so it is the one place that already knows, and the
    assertion is that the printed line carries the real path rather than a stand-in.
    """
    conf = tmp_path / "desk" / "testnet" / "gridcoinresearch.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"rpcuser={CONF_USER}\nrpcpassword={CONF_PASS}\n")
    monkeypatch.setitem(
        checker.Config.RPC, "GRC",
        {"user": ENV_USER, "password": ENV_PASS, "host": "127.0.0.1", "port": 25779},
    )

    assert checker.main(["--asset", "GRC", "--conf", str(conf)]) == 1
    printed = capsys.readouterr().out

    assert "configure_env.py --read-daemon-confs --read-gridcoin-conf" in printed
    assert f"--conf GRC={conf} --apply" in printed, (
        "the command does not carry the real path, so it cannot be pasted"
    )
    assert "<" not in printed.split("to fix it")[1], (
        "a placeholder reached the command block of the one tool that knows the path"
    )
    assert "swap_stack.py restart" in printed, "the required recreate is not named"
    for sentinel in SENTINELS:
        assert sentinel not in printed


def test_a_MATCHING_report_prints_no_command_at_all(tmp_path, capsys, monkeypatch):
    """Printing a fix for a thing that is not broken teaches an operator to skip the block.

    headline() already says what a 401 with every field SAME means -- the daemon read a
    different file -- and no command this tool could print would help with that.
    """
    conf = tmp_path / "gridcoinresearch.conf"
    conf.write_text(f"rpcuser={ENV_USER}\nrpcpassword={ENV_PASS}\n")
    monkeypatch.setitem(
        checker.Config.RPC, "GRC",
        {"user": ENV_USER, "password": ENV_PASS, "host": "127.0.0.1", "port": 25779},
    )

    assert checker.main(["--asset", "GRC", "--conf", str(conf)]) == 0
    printed = capsys.readouterr().out

    assert "to fix it" not in printed
    assert "configure_env.py" not in printed
    assert "EVERY FIELD MATCHES" in printed


def test_the_gridcoin_opt_in_appears_only_for_gridcoin(tmp_path, capsys, monkeypatch):
    """--read-gridcoin-conf on a BTC command would exit 2, not configure anything."""
    conf = tmp_path / "bitcoin.conf"
    conf.write_text(f"rpcuser={CONF_USER}\nrpcpassword={CONF_PASS}\n")
    monkeypatch.setitem(
        checker.Config.RPC, "BTC",
        {"user": ENV_USER, "password": ENV_PASS, "host": "127.0.0.1", "port": 18443},
    )

    assert checker.main(["--asset", "BTC", "--conf", str(conf)]) == 1
    printed = capsys.readouterr().out

    assert "--read-daemon-confs" in printed
    assert "--read-gridcoin-conf" not in printed
    assert f"--conf BTC={conf} --apply" in printed
