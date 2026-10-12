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
from chains.credential_match import (
    BOTH_ABSENT,
    DIFFERENT,
    ONLY_IN_CONF,
    ONLY_IN_ENV,
    SAME,
    compare,
    headline,
)
from conftest import root_entry_point

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
    assert "GRC_RPC_PASSWORD" in printed or "GRC_RPC_PASS" in printed
    assert "rpcpassword" in printed


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
