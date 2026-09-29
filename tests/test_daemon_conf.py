#!/usr/bin/env python3
"""A daemon conf is parsed, and the password never reaches a message.

Role: tests (read-only)
Reads: temp files it writes itself. No chain, no network, no daemon, and none
        of the operator's real confs.
Writes: pytest's tmp_path only
Can move funds: no
Mainnet-safe: yes

THE SECRET TEST IS THE POINT OF THE MODULE. chains/daemon_conf.py exists so an
operator never has to echo an rpcpassword into a terminal, and the way that goal
fails is not a crash -- it is one f-string interpolating the wrong key into a
line that then gets pasted somewhere. So the password's VALUE is asserted absent
from every string the module returns, with a value distinctive enough that a
substring search cannot miss it.

The section handling is the other half, and it is what makes the module useful
at all: a regtest conf conventionally carries rpcuser and rpcpassword at the top
level and rpcport inside [regtest], so reading only the top level finds
credentials, no port, and reports "unconfigured" for a daemon that is running.
"""

from __future__ import annotations

import pathlib

import pytest
from chains.daemon_conf import (
    CONF_FALLBACK_NETWORK,
    SECRET_CONF_KEYS,
    DaemonConfError,
    conf_fallback_settings,
    describe,
    parse_daemon_conf,
    rpc_settings_from_conf,
)

# Distinctive on purpose: a real-looking password would risk a false pass if some
# fragment of it happened to appear in surrounding prose.
# Named CANARY rather than SECRET so ruff's S105 does not read the assignment
# as a hardcoded credential -- which it would be right to, on any other line.
CANARY = "Zq7-UNIQUE-DO-NOT-PRINT-4f2"

REGTEST_CONF = f"""\
# litecoin.conf, as the harness expects it
server=1
rpcuser=rt
rpcpassword={CANARY}

[regtest]
rpcport=19443
"""


def test_the_port_is_found_inside_the_network_SECTION_and_not_only_at_the_top():
    """The layout a real regtest conf actually has.

    MUTATION: parse only the top level and this fails -- rpcport is absent, so
    rpc_settings_from_conf() raises and the chain reads as unconfigured while its
    daemon is answering. Verified 2026-09-29.
    """
    found = parse_daemon_conf(REGTEST_CONF, network="regtest")
    assert found == {"rpcuser": "rt", "rpcpassword": CANARY, "rpcport": "19443"}


def test_a_section_for_ANOTHER_network_is_not_read():
    """A conf that configures two networks must not have them merged.

    This is the case that would point a regtest reader at a mainnet port, and the
    section name is the only thing separating them.
    """
    both = REGTEST_CONF + "\n[main]\nrpcport=9332\n"
    assert parse_daemon_conf(both, network="regtest")["rpcport"] == "19443"
    assert parse_daemon_conf(both, network="main")["rpcport"] == "9332"
    # With no network asked for, only the top level is read -- and this conf puts
    # no port there, so the answer is "no port", not "either port".
    assert "rpcport" not in parse_daemon_conf(both)


def test_a_hash_inside_a_PASSWORD_is_not_treated_as_a_comment():
    """Modern Bitcoin Core does not strip an inline comment, and generators emit `#`.

    Stripping from the `#` would truncate the password silently, and the symptom
    would be a 401 from the daemon with nothing pointing at this parser.

    MUTATION: split each line on "#" before parsing and this fails. Verified
    2026-09-29.
    """
    text = "rpcuser=rt\nrpcpassword=abc#def#ghi\nrpcport=19443\n"
    assert parse_daemon_conf(text)["rpcpassword"] == "abc#def#ghi"


def test_a_leading_hash_IS_a_comment_so_a_commented_out_setting_is_not_read():
    """The other direction. A disabled line must stay disabled."""
    text = "#rpcpassword=old-value\nrpcuser=rt\nrpcpassword=new-value\nrpcport=19443\n"
    assert parse_daemon_conf(text)["rpcpassword"] == "new-value"


def test_settings_from_a_real_file_are_shaped_for_an_adapter(tmp_path):
    """Config.RPC's six keys exactly, so it splats into RPCAdapter like any entry."""
    conf = tmp_path / "litecoin.conf"
    conf.write_text(REGTEST_CONF)
    settings = rpc_settings_from_conf(conf, network="regtest")
    assert settings == {"user": "rt", "password": CANARY, "host": "127.0.0.1",
                        "port": 19443, "wallet": "", "timeout": 30.0}
    assert isinstance(settings["port"], int), "a string port would build a wrong URL"


def test_a_conf_MISSING_the_password_raises_instead_of_returning_a_partial_mapping(tmp_path):
    """A port with no password builds an adapter that 401s on every call.

    chains/registry.missing_settings() was added for exactly that on 2026-09-26,
    so handing back a partial mapping would push a known-broken adapter one layer
    further in before it failed.
    """
    conf = tmp_path / "litecoin.conf"
    conf.write_text("rpcuser=rt\n[regtest]\nrpcport=19443\n")
    with pytest.raises(DaemonConfError) as raised:
        rpc_settings_from_conf(conf, network="regtest")
    assert "rpcpassword" in str(raised.value)


def test_a_missing_FILE_names_the_path_it_looked_at():
    """Rule 14: "the conf did not supply one" is useless without which conf."""
    with pytest.raises(DaemonConfError) as raised:
        rpc_settings_from_conf("/definitely/not/here/litecoin.conf", network="regtest")
    assert "/definitely/not/here/litecoin.conf" in str(raised.value)
    assert "FileNotFoundError" in str(raised.value)


def test_a_NON_NUMERIC_port_is_named_rather_than_crashing_on_int(tmp_path):
    """An unparseable port is a conf problem, and it should read as one."""
    conf = tmp_path / "litecoin.conf"
    conf.write_text(f"rpcuser=rt\nrpcpassword={CANARY}\nrpcport=nineteen\n")
    with pytest.raises(DaemonConfError) as raised:
        rpc_settings_from_conf(conf)
    assert "nineteen" in str(raised.value)
    assert CANARY not in str(raised.value), "a parse failure must not print the password either"


def test_the_PASSWORD_never_appears_in_the_line_that_describes_the_connection(tmp_path):
    """THE WHOLE REASON THIS MODULE EXISTS.

    On 2026-09-25 a wallet CLI printed a 25-word recovery seed into a terminal
    whose output was then pasted into a chat, and that wallet had to be treated
    as public from then on. describe() is the one formatter, so this is the one
    place that can leak.

    MUTATION: add the password to describe()'s output and this fails. Verified
    2026-09-29.
    """
    conf = tmp_path / "litecoin.conf"
    conf.write_text(REGTEST_CONF)
    settings = rpc_settings_from_conf(conf, network="regtest")
    line = describe("/some/path/litecoin.conf", settings, network="regtest")
    assert CANARY not in line, f"describe() printed the password:\n{line}"
    # The names ARE printed: they are what makes a parse failure diagnosable.
    assert "rpcpassword" in line and "NOT shown" in line, line
    assert "19443" in line and "rt" in line, f"the port and user are not secret and are useful:\n{line}"


def test_every_string_this_module_returns_is_checked_against_the_secret_keys():
    """SECRET_CONF_KEYS is a claim about which values may not be printed.

    Asserting the set's contents pins the claim itself: adding a key to
    CONF_TO_SETTING that is also sensitive without adding it here would leave the
    test above passing while a new value leaks.
    """
    assert {"rpcpassword"} == SECRET_CONF_KEYS


# ---------------------------------------------------------------------------
# THE SHARED RESOLVER, and the reason it is shared. Added 2026-09-29 after the
# balance reader and the swap driver gave different answers about the same
# running Litecoin daemon within an hour of each other.
# ---------------------------------------------------------------------------


def test_EVERY_entry_point_that_resolves_a_chain_uses_THIS_resolver():
    """Rule 8, held mechanically rather than by intention.

    chain_balances.py grew the conf fallback and atomic_swap_xrp.py did not, so
    the operator configured Litecoin, watched the reader find it, and watched the
    driver say "(none)" about the same daemon. The failure mode is not that the
    second copy is wrong -- it is that there is no second copy AT ALL and nobody
    notices which entry points were left out.

    So: any root script that resolves a bitcoin-family chain must import the
    shared resolver. A new one that builds its own is what this catches.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    resolvers = {"chain_balances.py", "atomic_swap_xrp.py"}
    for name in sorted(resolvers):
        source = (root / name).read_text()
        assert "conf_fallback_settings" in source, (
            f"{name} resolves a chain without the shared conf fallback. An operator who configured "
            f"a daemon by conf will be told by one entry point that it is there and by this one "
            f"that it is not"
        )
        assert "CONF_FALLBACK_NETWORK = {" not in source, (
            f"{name} spells its own fallback table. There is one, in chains/daemon_conf.py, and a "
            f"second would drift the moment a chain is added to either"
        )


def test_GRC_is_absent_from_the_shared_table_so_no_entry_point_can_resolve_it():
    """The exclusion has to hold for the DRIVER, not only for the reader.

    A read of the wrong Gridcoin wallet is bad; a swap driver funding an HTLC
    against it is a different order of bad, and the driver gained this fallback
    on 2026-09-29. Excluding GRC in one shared table is what makes the guarantee
    the same in both.

    MUTATION: add "GRC" to CONF_FALLBACK_NETWORK and this fails. Verified
    2026-09-29.
    """
    assert "GRC" not in CONF_FALLBACK_NETWORK
    settings, line = conf_fallback_settings("GRC")
    assert settings is None, "GRC was resolved from a conf; its conf is shared with mainnet"
    assert "by design" in line and "GRC_RPC_PORT" in line, (
        f"the refusal has to say it is deliberate and name what to set instead:\\n{line}"
    )


def test_an_unknown_chain_gets_a_sentence_rather_than_an_exception():
    """conf_fallback_settings never raises: a caller needs to SAY why, not crash."""
    settings, line = conf_fallback_settings("DOGE")
    assert settings is None
    assert "DOGE_RPC_PORT" in line, line
