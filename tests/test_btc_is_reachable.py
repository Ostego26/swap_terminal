"""Everything that has to be true before a Bitcoin daemon can be reached, proven without one.

Role: test (read-only; no socket, no subprocess, no chain)
Reads: configure_env.py, swap_terminal/chains/daemon_conf.py,
       swap_terminal/chains/registry.py, swap_terminal/regtest/daemons.py,
       swap_terminal/modules/atomic_*_client.py, swap_terminal/modules/htlc_fee.py.
       Conf files it writes itself under pytest's tmp_path.
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes -- every conf here is written by this file into a temporary
       directory, nothing opens a connection, and nothing starts a process.

=============================================================================
WHY THIS FILE EXISTS: BTC WAS THE ONE CHAIN OF SIX THE TERMINAL COULD NOT REACH
=============================================================================

Measured on the operator's host 2026-10-11. GRC, LTC, SOL, XRP and ICP are
configured and reachable; BTC is not, and nothing is listening on 18443 or 18332.
`bitcoind` IS installed -- `command -v bitcoind` answers /usr/local/bin/bitcoind,
`bitcoind -version` answers "Bitcoin Core version v28.1.0" -- so a missing binary
was never the blocker. Nothing had started it, and `configure_env.py
--read-daemon-confs` could not supply the credentials when it was.

THE CODE DEFECT WAS A PORT BEING TREATED AS THE NAME OF A NETWORK.

OPEN_FINDINGS.md finding 1 records the 2026-10-10 migration of all three script
daemons off regtest, at the operator's instruction, and two measured facts about
what it left behind:

    BTC needs `[testnet4]`, LTC needs `[test]`   Core v28.1.0 has two testnets with
                                                 different conf section names.
    The RPC ports did NOT have to change         18443 and 19443 are in
                                                 network_target.CHAIN_PORTS' TEST sets,
                                                 not only the regtest ones.

So their bitcoind serves TESTNET4 on rpcport 18443 -- Bitcoin's conventional
REGTEST port. `configure_env.CONF_SECTION_FOR_PORT` mapped 18443 to `regtest` and
nothing else looked at the conf, so the read went to a `[regtest]` block. Measured
by running the real parser over confs in both shapes a migration produces:

    one [testnet4] block, nothing else   -> {} , and the tool reported
                                            "has no rpcuser, rpcpassword" about a
                                            file that has both
    [regtest] left in beside [testnet4]  -> the [regtest] block's credentials, which
                                            bitcoind itself ignores on testnet4

The first is the symptom the operator saw. The second is worse and silent: those
credentials go into `.env`, every BTC call 401s, and a 401 reads exactly like a
daemon that is down. configure_env.conf_section_for_port() now asks the conf which
block configures the port and falls back to the convention, and the tests below
are that function's four outcomes plus the two it must not regress.

WHAT THIS FILE DOES NOT AND CANNOT PROVE. No daemon ran. `bitcoind` and
`bitcoin-cli` are not installed in the container this was written in and docker is
not usable here, so nothing below establishes that a real Core 28.1 node answers
on the credentials this path now finds. Every claim here is about code reached
with seeded inputs, which is the only kind of claim this file is allowed to make
(rule 17).
"""

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from chains.bitcoin import BitcoinAdapter
from chains.daemon_conf import TOP_LEVEL, conf_sections, parse_daemon_conf
from chains.registry import build_adapters, missing_settings, why_cannot_pay_out
from modules.htlc_fee import (
    PLATFORM_FEE_ADDRESS_VARIABLE,
    PLATFORM_FEE_RATE,
    assert_no_output_is_dust,
    platform_fee_address,
    platform_fee_coin,
)
from network_target import CHAIN_PORTS
from regtest.daemons import CHAIN_DEFAULTS, RegtestSetupError, resolve_chain_config, start_daemon
from source_tree import REPOSITORY_ROOT

# configure_env.py is a ROOT entry point, so it sorts after the swap_terminal/
# packages above rather than among them -- ruff's isort pass puts first-party root
# modules in their own block, and tests/conftest.py is what puts both on sys.path.
from configure_env import CONF_SECTION_FOR_PORT, conf_section_for_port

# =============================================================================
# THE CONF SHAPES, written out rather than built by a helper
# =============================================================================
#
# Each is a real bitcoin.conf or litecoin.conf layout, and the differences between
# them are the whole subject -- a factory taking five keyword arguments would hide
# the one line that matters in each. The credentials are the strings `desk` and
# `rt`; nothing here is a real credential and nothing here is read from the host.

#: THE OPERATOR'S BTC, as OPEN_FINDINGS finding 1 describes it after the migration:
#: one network section, named for Core 28's second testnet, carrying the port that
#: used to be the regtest one. This is the conf the old code could not read.
BTC_TESTNET4 = """\
server=1
testnet4=1
rpcallowip=127.0.0.1

[testnet4]
rpcuser=desk
rpcpassword=testnet4-credential-not-real
rpcport=18443
wallet=desk_hot
"""

#: The same migration with the old block left in place, which is what happens when
#: a section is ADDED rather than replaced. Two blocks claim one port and the file
#: cannot say which daemon came up; only the daemon knows.
BTC_STALE_REGTEST_BESIDE_TESTNET4 = """\
server=1
testnet4=1

[regtest]
rpcuser=rt
rpcpassword=rt
rpcport=18443

[testnet4]
rpcuser=desk
rpcpassword=testnet4-credential-not-real
rpcport=18443
"""

#: A conventional regtest deployment: credentials at the top level, port in the
#: section, which is the layout chains/daemon_conf.parse_daemon_conf()'s docstring
#: describes and the one the harness's own instructions produce.
BTC_CONVENTIONAL_REGTEST = """\
regtest=1
server=1
rpcuser=rt
rpcpassword=rt

[regtest]
rpcport=18443
"""

#: The operator's LTC: Litecoin Core v0.21.4 has one testnet and calls it `[test]`.
#: `port=19444` inside it is finding 1's other measurement -- litecoind could not
#: bind 19335 because litecoin-qt held it.
LTC_TESTNET = """\
testnet=1
server=1
rpcuser=rt
rpcpassword=rt

[test]
rpcport=19443
port=19444
"""

#: A conf whose only section is mainnet. Nothing may be taken from it: a mainnet
#: credential is the one thing this path must never hand to a probed TEST port.
BTC_MAIN_ONLY = """\
server=1

[main]
rpcuser=real
rpcpassword=real-mainnet-credential-not-read
rpcport=18443
"""


# =============================================================================
# conf_sections(): the walk that keeps the section names
# =============================================================================


def test_conf_sections_keeps_the_section_a_setting_was_written_in():
    """The names survive the walk, which is the whole point of the new function.

    MUTATION: make conf_sections() return `{TOP_LEVEL: merged}` for everything.
    This test fails on the [testnet4] key.
    """
    sections = conf_sections(BTC_TESTNET4)
    assert sorted(sections) == [TOP_LEVEL, "testnet4"]
    assert sections["testnet4"]["rpcuser"] == "desk"
    assert sections["testnet4"]["rpcport"] == "18443"
    # The top level of this conf carries no RPC setting at all -- server= and
    # testnet4= are not in CONF_TO_SETTING -- and it is still PRESENT, because a
    # caller counting network sections has to be able to exclude it.
    assert sections[TOP_LEVEL] == {}


def test_a_section_with_no_rpc_setting_is_present_and_empty():
    """"the conf has this header" and "the conf does not" are different answers.

    conf_section_for_port()'s third rule turns on exactly this: a `[testnet4]`
    header whose rpc settings are all at the top level still tells us the daemon is
    on testnet4.
    """
    sections = conf_sections("rpcuser=desk\nrpcpassword=x\nrpcport=18443\n\n[testnet4]\nwallet=desk_hot\n")
    assert "testnet4" in sections, "the header names the network even with nothing rpc-ish under it"
    assert sections["testnet4"] == {}
    assert sections[TOP_LEVEL]["rpcport"] == "18443"


def test_parse_daemon_conf_behavior_is_unchanged_by_the_rewrite():
    """It is conf_sections() plus one merge, and the merge must not have moved.

    Three claims, each of which was true of the hand-rolled walk: an empty network
    reads the top level alone, a named one overrides key by key, and a `#` only
    starts a comment at the start of a line.
    """
    assert parse_daemon_conf(BTC_CONVENTIONAL_REGTEST) == {"rpcuser": "rt", "rpcpassword": "rt"}
    assert parse_daemon_conf(BTC_CONVENTIONAL_REGTEST, network="regtest") == {
        "rpcuser": "rt", "rpcpassword": "rt", "rpcport": "18443",
    }
    # The section wins over the top level, key by key.
    merged = parse_daemon_conf(BTC_STALE_REGTEST_BESIDE_TESTNET4, network="testnet4")
    assert merged["rpcuser"] == "desk"
    # A `#` inside a value is part of the value. Modern Core does not strip it, and
    # truncating an rpcpassword here would arrive as a 401 with nothing pointing at
    # this function.
    assert parse_daemon_conf("rpcpassword=ab#cd\n")["rpcpassword"] == "ab#cd"
    # A `#` at the start of a line is a comment and the setting is not read.
    assert parse_daemon_conf("# rpcpassword=commented\n") == {}


# =============================================================================
# conf_section_for_port(): the four outcomes, and the defect
# =============================================================================


def test_the_operators_testnet4_conf_is_read_from_its_own_section():
    """THE DEFECT. A testnet4 daemon on 18443, which the port convention calls regtest.

    This is the test that fails without the fix. With CONF_SECTION_FOR_PORT alone,
    the section is `regtest`, the conf has no such block, and parse_daemon_conf()
    returns the top level -- which here is empty, so the tool reports the conf has
    no rpcuser and no rpcpassword about a file that has both.

    MUTATION: replace the body of conf_section_for_port() with
    `return CONF_SECTION_FOR_PORT.get(port, ""), "the convention"`. This test fails
    on the rpcuser assertion.
    """
    section, why = conf_section_for_port("BTC", BTC_TESTNET4, 18443)
    assert section == "testnet4", why
    assert "18443" in why and "testnet4" in why, "say which block was read, and why (rule 14)"
    settings = parse_daemon_conf(BTC_TESTNET4, network=section)
    assert settings["rpcuser"] == "desk"
    assert settings["rpcpassword"], "the password is found; its value is never printed"


def test_the_port_convention_alone_would_have_read_the_wrong_block():
    """The measurement the fix is for, asserted so it cannot be argued with.

    Not a test of the new code: a test that the OLD rule is wrong for this conf, so
    that anybody who proposes going back to it has to delete this.
    """
    assert CONF_SECTION_FOR_PORT[18443] == "regtest", "18443 is Bitcoin's conventional regtest port"
    assert "regtest" not in conf_sections(BTC_TESTNET4), "and the operator's conf has no such block"
    assert parse_daemon_conf(BTC_TESTNET4, network=CONF_SECTION_FOR_PORT[18443]) == {}, (
        "so the convention finds nothing in a conf that carries both credentials"
    )


def test_two_blocks_claiming_one_port_take_nothing_and_say_so():
    """Ambiguity is refused, not guessed. Guessing writes a 401 into .env.

    The conventional block is the STALE one here, so falling back to the convention
    would hand over credentials bitcoind ignores -- the silent failure, which is
    worse than the loud one.

    MUTATION: delete the `if declaring:` branch so the convention is reached. This
    test fails because the section comes back as `regtest`.
    """
    section, why = conf_section_for_port("BTC", BTC_STALE_REGTEST_BESIDE_TESTNET4, 18443)
    assert section == "", why
    assert parse_daemon_conf(BTC_STALE_REGTEST_BESIDE_TESTNET4, network=section) == {}, (
        "nothing is taken from either block"
    )
    # The remedy is in the sentence, because the operator never reads the docstring.
    assert "[regtest]" in why and "[testnet4]" in why, "name both blocks"
    assert "401" in why, "say what picking the wrong one would have looked like"
    assert "stale" in why or "by hand" in why, "name the remedy"


def test_a_conventional_regtest_conf_still_reads_its_regtest_block():
    """The deployment every LTC and GRC read so far has been. It must not change.

    Here the `[regtest]` block declares the port, so rule 1 answers and the
    convention is not even consulted -- which is the same section either way.
    """
    section, why = conf_section_for_port("BTC", BTC_CONVENTIONAL_REGTEST, 18443)
    assert section == "regtest", why
    settings = parse_daemon_conf(BTC_CONVENTIONAL_REGTEST, network=section)
    assert settings == {"rpcuser": "rt", "rpcpassword": "rt", "rpcport": "18443"}


def test_litecoins_one_testnet_section_is_read_for_litecoin():
    """LTC calls its testnet `[test]` and has no testnet4, and that must keep working.

    Half of a fix is a regression: making BTC work by hardcoding `testnet4` would
    break the chain that already worked.
    """
    section, why = conf_section_for_port("LTC", LTC_TESTNET, 19443)
    assert section == "test", why
    assert parse_daemon_conf(LTC_TESTNET, network=section)["rpcport"] == "19443"


def test_testnet4_is_never_chosen_for_litecoin():
    """The allowlist is per chain, and Litecoin Core v0.21.4 predates testnet4.

    chains/daemon_network.CHAIN_TEST_NETWORKS deliberately withholds `testnet4` from
    LTC: its DATA DIRECTORY is named testnet4 while its daemon answers `test`, which
    is the near-match that would get waved through on a glance. A `[testnet4]` block
    in a litecoin.conf configures nothing, so nothing may be read out of it.
    """
    section, why = conf_section_for_port("LTC", BTC_TESTNET4, 18443)
    assert section == "", why
    assert "no LTC network section" in why, why


def test_a_mainnet_only_conf_yields_nothing():
    """A [main] block is never the answer to a question about a test port.

    discover_ports() only ever probes CHAIN_PORTS' test_ports, so a mainnet section
    cannot be what configured the daemon -- and handing back a mainnet credential is
    the expensive direction of a wrong guess.

    MUTATION: drop the `if name in allowed` filter from `usable`. `[main]` becomes
    the conf's only section, rule 3 fires, and this test fails.
    """
    section, why = conf_section_for_port("BTC", BTC_MAIN_ONLY, 18443)
    assert section == "", why
    assert parse_daemon_conf(BTC_MAIN_ONLY, network=section) == {}, "nothing from [main]"
    assert "[main]" in why, "name what was found and skipped"


def test_nothing_listening_still_finds_the_one_network_section():
    """Port 0 is "nothing answered", and the conf can still say which network it is.

    This is the state the operator is in right now: bitcoind is not running, so
    discovery establishes no port. `_probed_port()` returns 0 rather than guessing,
    and rule 3 -- the conf's only network section -- is what makes the credentials
    readable before the daemon is ever started.
    """
    section, why = conf_section_for_port("BTC", BTC_TESTNET4, 0)
    assert section == "testnet4", why
    assert parse_daemon_conf(BTC_TESTNET4, network=section)["rpcuser"] == "desk"


def test_a_conf_with_no_section_at_all_reads_its_top_level():
    """The simplest conf there is, and the sentence must not be a blank gap (rule 14)."""
    section, why = conf_section_for_port("BTC", "rpcuser=rt\nrpcpassword=rt\n", 18443)
    assert section == ""
    assert "(none)" in why, "an empty listing prints (none), never nothing"
    assert parse_daemon_conf("rpcuser=rt\nrpcpassword=rt\n", network=section)["rpcuser"] == "rt"


def test_no_conf_shape_here_ever_yields_the_mainnet_credential():
    """One sweep over every shape in this file, as the property rather than per case.

    The string below is in BTC_MAIN_ONLY only, and no (chain, port) pair may reach
    it. Written as a sweep because the per-case tests above each cover one branch,
    and this covers the invariant across all of them.
    """
    never = "real-mainnet-credential-not-read"
    shapes = (BTC_TESTNET4, BTC_STALE_REGTEST_BESIDE_TESTNET4, BTC_CONVENTIONAL_REGTEST,
              LTC_TESTNET, BTC_MAIN_ONLY)
    for chain in ("BTC", "LTC"):
        for text in shapes:
            for port in (0, 18332, 18443, 19332, 19443):
                section, why = conf_section_for_port(chain, text, port)
                settings = parse_daemon_conf(text, network=section)
                assert settings.get("rpcpassword") != never, f"{chain} {port} {why}"
                assert section != "main", f"{chain} {port} chose [main]: {why}"


# =============================================================================
# THE HARNESS DOES NOT WRITE THE CONF, AND THE OPERATOR HAS TO
# =============================================================================


def test_the_harness_refuses_rather_than_writing_a_conf(tmp_path, monkeypatch):
    """start_daemon() will not create a bitcoin.conf, and names the file and the settings.

    THIS PINS A SENTENCE THAT WAS WRONG IN configure_env.py UNTIL 2026-10-11. It
    said "The moment they bring up this repository's OWN regtest harness, bitcoind
    writes ~/regtest/btc/bitcoin.conf with both" -- neither half is true. bitcoind
    does not write a conf, and the harness refuses to, deliberately:
    regtest_htlc_verify.py's header says "writing an rpcpassword into a file on the
    operator's behalf is not this harness's business."

    It matters because it is the one claim on that page an operator acts on. The
    refusal is the thing the host sequence has to be built around, so it is asserted
    here rather than described anywhere.

    NO PROCESS IS STARTED. The datadir exists and the conf does not, which is the
    branch above the `shutil.which` lookup and above every subprocess call --
    checked by the assertion that the directory is still empty afterwards.

    MUTATION: make start_daemon() write a default conf when conf_path is missing.
    This test fails on the raise.
    """
    datadir = tmp_path / "btc"
    datadir.mkdir()
    monkeypatch.setenv("ST_REGTEST_BTC_DATADIR", str(datadir))
    # A port nothing in this container listens on, so rpc_answers() cannot adopt a
    # daemon and the conf check is reached. 0 would be rejected by int(); this is an
    # ordinary unprivileged port and nothing is connected to -- a refused connection
    # is the answer the function wants.
    monkeypatch.setenv("ST_REGTEST_BTC_RPC_PORT", "18449")
    config = resolve_chain_config("BTC")
    assert config.conf_path == datadir / "bitcoin.conf"
    assert not config.conf_path.exists()

    with pytest.raises(RegtestSetupError) as refusal:
        start_daemon(_SilentConsole(), config)

    message = str(refusal.value)
    assert str(config.conf_path) in message, "name the file the operator must create"
    for setting in ("regtest=1", "server=1", "fallbackfee=0.0002", "rpcuser", "rpcpassword", "18449"):
        assert setting in message, f"the refusal must name {setting}"
    assert list(datadir.iterdir()) == [], "the harness wrote nothing, which is the whole claim"


def test_the_harness_also_refuses_a_datadir_that_does_not_exist(tmp_path, monkeypatch):
    """The step before it, and a different remedy, so the two must not share a message."""
    missing = tmp_path / "not-created"
    monkeypatch.setenv("ST_REGTEST_BTC_DATADIR", str(missing))
    monkeypatch.setenv("ST_REGTEST_BTC_RPC_PORT", "18449")
    with pytest.raises(RegtestSetupError) as refusal:
        start_daemon(_SilentConsole(), resolve_chain_config("BTC"))
    message = str(refusal.value)
    assert str(missing) in message
    assert "ST_REGTEST_BTC_DATADIR" in message, "name the override, not just the default path"
    assert not missing.exists(), "nothing was created"


def test_configure_env_says_who_actually_writes_the_conf():
    """The corrected sentence, held against the file, because it is operator-facing.

    A source read and not a behavior test, deliberately: the defect WAS a sentence,
    and the two tests above already pin the behavior it got wrong.

    ASSERTED BY PRESENCE AND NEVER BY ABSENCE, and that is not a style choice. The
    first version of this test asserted the old sentence was GONE -- and failed, on
    the corrected file, because the correction QUOTES the old sentence in order to
    name it. HANDOFF.md section 6 lists that as the fifth thing this repository got
    wrong in one session: "a detector that read its own search string". A rule-1
    correction always leaves the refuted claim on the page; a test that forbids the
    string forbids writing the correction down.
    """
    source = (REPOSITORY_ROOT / "configure_env.py").read_text()
    assert "bitcoind does not write a conf at all" in source, (
        "the page has to say who writes the conf, because that is the one claim on it "
        "an operator acts on"
    )
    assert "start_daemon() raises" in source, (
        "name the function that refuses, so the next reader can check it"
    )


class _SilentConsole:
    """A ConsoleLike that keeps what it was told instead of printing it.

    The four members funding_steps.py actually reaches off a console are say,
    elapsed, step and check (regtest/console.ConsoleLike counted them); start_daemon()
    uses `say` alone. Keeping the lines rather than discarding them means a failure
    here can print what the harness was saying when it refused.
    """

    def __init__(self):
        self.lines: list[str] = []

    def say(self, line: str) -> None:
        self.lines.append(line)


# =============================================================================
# BTC NEEDS NO ARMING OVERLAY: the daemon signs its own sends
# =============================================================================


#: The stub halves of an HTTP credential for an adapter that never opens a socket.
#: NAMED rather than defaulted in the signature, which is ruff's S107 answered rather
#: than suppressed (rule 19): the rule flags a PARAMETER DEFAULT that looks like a
#: credential, and these are two letters in a test fixture. tests/conftest.py's
#: RPC_FIXTURE_AUTH is the same move for the same rule.
#: Both halves spelled the way tests/conftest.py spells its own
#: (RPC_FIXTURE_AUTH = "fixture-rpc-auth-value"): a name ruff does not read as
#: holding a credential, and a value that says what it is out loud.
RPC_FIXTURE_USER = "fixture-rpc-user"
RPC_FIXTURE_AUTH = "fixture-rpc-auth-value"


def _btc_rpc(port: int, user: str = RPC_FIXTURE_USER, password: str = RPC_FIXTURE_AUTH) -> dict:
    """A Config.RPC-shaped BTC entry. Not a credential: two letters, never on a socket."""
    return {"BTC": {"user": user, "password": password, "host": "127.0.0.1", "port": port,
                    "wallet": "desk_hot", "timeout": 30.0}}


def test_btc_is_payable_the_moment_it_is_reachable():
    """can_spend is True at class level, so BTC needs no key this process holds.

    Confirmed rather than trusted, which is what the brief for this work asked for:
    a Bitcoin-family adapter sends with `sendtoaddress` and the DAEMON holds the
    key, where SOL, XRP and ICP each derive can_spend per instance from a credential
    this process may or may not have. So there is no BTC equivalent of
    docker-compose.web.armed-sol.yml, and why_cannot_pay_out() returns "" -- the
    empty string meaning "it can".
    """
    adapters = build_adapters(_btc_rpc(18443))
    assert sorted(adapters) == ["BTC"]
    assert BitcoinAdapter.can_spend is True
    assert BitcoinAdapter.payout_refusal == "", "nothing to refuse, so nothing to say"
    assert why_cannot_pay_out(adapters, "BTC") == "", "BTC can be a payout destination"


def test_btc_needs_all_three_variables_and_names_all_three():
    """A port with no credential builds an adapter that 401s on every call.

    The three names are what an operator has to export, and naming one of three
    sends them back for a second round (chains/registry.missing_settings()).
    """
    unset = {"BTC": {"user": "", "password": "", "host": "127.0.0.1", "port": 0,
                     "wallet": "", "timeout": 30.0}}
    assert missing_settings(unset, "BTC") == ["BTC_RPC_PORT", "BTC_RPC_USER", "BTC_RPC_PASS"]
    assert build_adapters(unset) == {}, "no adapter rather than one aimed at a default port"
    # A port alone is not enough, which is the 2026-09-26 measurement registry.py
    # records for Gridcoin applied to this chain.
    port_only = _btc_rpc(18443, user="", password="")
    assert missing_settings(port_only, "BTC") == ["BTC_RPC_USER", "BTC_RPC_PASS"]
    assert build_adapters(port_only) == {}


def test_the_harness_and_the_adapter_agree_about_bitcoins_regtest_port():
    """One number, two tables, and they have to match or the harness is unreachable.

    regtest.daemons.CHAIN_DEFAULTS puts the harness on 18443 and
    network_target.CHAIN_PORTS lists 18443 as a BTC test port, so a daemon the
    harness starts is one configure_env.discover_ports() can find. This is the join
    the whole host sequence depends on.
    """
    assert CHAIN_DEFAULTS["BTC"]["port"] == 18443
    assert CHAIN_DEFAULTS["BTC"]["conf_name"] == "bitcoin.conf"
    assert CHAIN_DEFAULTS["BTC"]["daemon"] == "bitcoind"
    assert CHAIN_DEFAULTS["BTC"]["env_prefix"] == "ST_REGTEST_BTC"
    assert CHAIN_DEFAULTS["BTC"]["port"] in CHAIN_PORTS["BTC"].test_ports
    assert CHAIN_PORTS["BTC"].mainnet_port not in CHAIN_PORTS["BTC"].test_ports


# =============================================================================
# THE THREE HTLC CLIENTS: what they claim about themselves must be true
# =============================================================================

_CLIENTS = {
    "atomic_btc_client.py": "BTCClient",
    "atomic_ltc_client.py": "LTCClient",
    "atomic_grc_client.py": "GRCClient",
}


def _client_source(name: str) -> str:
    return (REPOSITORY_ROOT / "swap_terminal" / "modules" / name).read_text()


def _methods_of(source: str, class_name: str) -> set[str]:
    """Every method defined on `class_name`, by walking the AST rather than grepping.

    An AST walk and not a regex, because this file's subject is claims that went
    stale and a prose-reading detector is how four of them survived: a grep for
    `def refund_contract` matches the sentence that says there is no such method.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
    raise AssertionError(f"{class_name} is not defined in that source")


@pytest.mark.parametrize(("filename", "class_name"), sorted(_CLIENTS.items()))
def test_every_client_has_the_refund_it_used_to_say_nobody_had(filename, class_name):
    """All three define refund_contract(), so no header may claim none does.

    THE CLAIM THAT WAS FALSE FOR TWO WEEKS. Each client's header ended "NO CLIENT
    HERE IMPLEMENTS A REFUND AT ALL -- there is no refund_contract() on any of the
    three", written before `0a1cf51` added one to each on 2026-09-26 and never
    revisited. modules/htlc_fee.assert_no_output_is_dust() had already corrected the
    same sentence in its own docstring -- one fact, four copies, one of them fixed,
    which is rule 8 in its plainest form.

    The test is the invariant rather than the wording: the method exists, so the
    sentence may not be reintroduced.

    MUTATION: paste the old sentence back into any one of the three headers. The
    second assertion fails and names the file.
    """
    source = _client_source(filename)
    assert "refund_contract" in _methods_of(source, class_name), (
        f"{class_name}.refund_contract() is what this test is about"
    )
    # PRESENCE OF THE RETRACTION, NOT ABSENCE OF THE CLAIM. Asserting the old
    # sentence is gone fails on the corrected file, because the correction quotes it
    # in order to name it -- see _divergence_row() for the same trap and
    # HANDOFF.md section 6 for the session that fell into it four times.
    assert "`0a1cf51`" in source, (
        f"{filename}: the header must name the commit that added refund_contract(), so the "
        f"retracted no-refund claim cannot read as current"
    )
    # AND IT MUST CITE THE DOCUMENT THAT GRADES THE BRANCH AGAINST A RUN. This
    # assertion exists because the first correction of that header was ALSO wrong, and
    # in the opposite direction: it said "the refund branch has never been exercised by
    # anything but the regtest harness's own spender. No contract built with the
    # corrected locktime encoding has been refunded on any chain", taken from
    # CLAUDE.md's 2026-09-25 re-measurement and written in the register of a
    # measurement four days after the runs that settled it. docs/branch_coverage.md
    # section 2 grades `refund after expiry, BTC/LTC` as SPENT through the real
    # refund_contract() -- BTC `7cc92abdbad07a99...`, LTC `59ed52a9f4255332...`, both
    # 2026-09-29 -- and GRC's refund as SPENT on testnet by `ee9dc216bf055b22...`.
    #
    # So the invariant worth holding is not a particular sentence about the refund's
    # status: it is that the header cites the document whose grades move, rather than
    # asserting a status from memory. Two wrong versions of this paragraph in one day
    # is what that costs.
    assert "docs/branch_coverage.md" in source, (
        f"{filename}: the header must cite the document that grades each branch against a "
        f"run, not assert the branch's status from memory -- that paragraph has been wrong "
        f"in both directions"
    )


def _divergence_row(source: str, label: str) -> str:
    """The one line of the module docstring's divergence table carrying `label`.

    THE ROW AND NOT THE FILE, and that distinction is what makes this test possible
    at all. A search of the whole source for "BTC (charges nothing)" matches the
    PARAGRAPH that quotes the old row in order to retract it -- which is the
    self-reading detector HANDOFF.md section 6 names, and it is unavoidable here
    because rule 1 requires the refuted claim to stay on the page. A table ROW is a
    line that STARTS with its label after indentation, so a quotation inside a
    sentence cannot be mistaken for one.

    Reads ast.get_docstring() rather than the file, so a row-shaped line in a code
    comment could not stand in for the table either.
    """
    docstring = ast.get_docstring(ast.parse(source)) or ""
    rows = [line for line in docstring.splitlines() if line.startswith("  ") and line.strip().startswith(label)]
    assert len(rows) == 1, f"expected one {label!r} row in the divergence table, found {len(rows)}"
    return rows[0]


@pytest.mark.parametrize("filename", sorted(_CLIENTS))
def test_the_divergence_table_reports_the_platform_fee_as_merged(filename):
    """BTC has been in PLATFORM_FEE_RATE at 1.5% since 2026-09-27, so the row is closed.

    The row read "platform fee on redeem   none / 0.25% / 0.25% / BTC (charges
    nothing)" in all three headers until 2026-10-11 -- a closed divergence reported
    as open, in the one place a reader looks to find out whether the three clients
    agree. Measured by reading the table back out of the running code:
    PLATFORM_FEE_RATE is Decimal("0.015") for all three.

    MUTATION: restore the old row in any header. This test fails on that file, and
    says which.
    """
    row = _divergence_row(_client_source(filename), "platform fee on redeem")
    assert "PLATFORM_FEE_RATE" in row, f"{filename}: the row must point at the one table"
    assert "none" not in row, f"{filename}: BTC charges 1.5%, so no column is none"
    assert "0.25%" not in row, f"{filename}: the rate has been 1.5% since 2026-09-27"


@pytest.mark.parametrize("filename", sorted(_CLIENTS))
def test_the_rows_that_really_do_diverge_are_still_reported(filename):
    """A merge that empties the table is worse than a stale one, so check it did not.

    The point of correcting one row is that the remaining rows are trustworthy. Two
    were re-measured on 2026-10-11 and are still true, so they must still be on the
    page: BTC is the only client passing an HTTP timeout, and it is the only one
    whose create_contract() does not return the secret hash.
    """
    source = _client_source(filename)
    assert "timeout=30" in _divergence_row(source, "rpc HTTP timeout")
    assert "returns secret_hash" in _divergence_row(source, "returns secret_hash")


def test_the_rate_and_the_address_tables_hold_the_same_three_chains():
    """A rate with no address variable claims a fee nothing can collect.

    This is the invariant the corrected refusal messages now describe, and it is the
    reason the old ones were wrong: they named BTC as present in one table and
    absent from the other, which stopped being true when both rows landed together.
    """
    assert sorted(PLATFORM_FEE_RATE) == sorted(PLATFORM_FEE_ADDRESS_VARIABLE) == ["BTC", "GRC", "LTC"]
    assert len(set(PLATFORM_FEE_RATE.values())) == 1, "one rate across the three, so no row can drift"


@pytest.mark.parametrize("asset", ["XRP", "SOL", "ICP", "DOGE"])
def test_the_platform_fee_refusals_do_not_claim_btc_is_absent(asset):
    """Two refusals that listed BTC as present and then declared it absent.

    Measured 2026-10-11 by calling them:

        platform_fee_address("XRP", {}) -> "... there is one for LTC, GRC, BTC.
                                           BTC is absent for the reason given beside
                                           PLATFORM_FEE_RATE: its client passes no
                                           extra outputs at all"
        platform_fee_coin("XRP", 1)     -> "... it is charged on LTC, GRC, BTC.
                                           BTC is absent on purpose: BTCClient.
                                           redeem_contract() passes no extra outputs"

    Both sentences are self-contradicting, and the reason each gives for the absence
    -- that BTC's client passes no extra outputs -- was made false by the same
    2026-09-27 change that added the rows. One sentence, three ways wrong, printed
    at whoever asked about a fourth chain (rule 14: output that contradicts itself
    about what it did is a defect in the output).

    MUTATION: restore either sentence. This test fails on that asset.
    """
    with pytest.raises(ValueError) as address_refusal:
        platform_fee_address(asset, {})
    with pytest.raises(ValueError) as coin_refusal:
        platform_fee_coin(asset, Decimal(1))

    for refusal in (str(address_refusal.value), str(coin_refusal.value)):
        assert asset in refusal, "name the asset that was asked about"
        assert "BTC is absent" not in refusal, "BTC is in both tables"
        assert "passes no extra outputs" not in refusal, (
            "BTCClient.redeem_contract() threads extra_outputs through since 2026-09-27"
        )
        # And the sentence still has to be actionable: it names what IS charged.
        for present in PLATFORM_FEE_RATE:
            assert present in refusal, f"name {present}, which is what the table holds"


def test_the_dust_refusal_no_longer_claims_no_client_implements_a_refund():
    """The same retracted claim, in the string an operator actually reads.

    assert_no_output_is_dust()'s DOCSTRING records that "NO CLIENT IN THIS PACKAGE
    IMPLEMENTS A REFUND" is false and explains why the check is still right. Its
    raise message went on saying it -- the half of a wrong-comment fix that reaches
    somebody.

    The replacement states the property the sentence actually needed: a refund
    spends the other branch after the locktime, so it is not a recovery from a
    refused redeem.

    MUTATION: put "no client here implements a refund" back in the raise. This test
    fails.
    """
    # One satoshi to a P2WPKH output: below every threshold on every chain, so the
    # refusal fires. Nothing is signed and nothing is built -- this function is
    # called BEFORE a spend exists, which is its whole reason for being separate.
    p2wpkh = bytes([0x00, 0x14]) + bytes(20)
    with pytest.raises(ValueError) as refusal:
        assert_no_output_is_dust("BTC", [(1, p2wpkh)])
    message = str(refusal.value)
    assert "no client here implements a refund" not in message, (
        "all three clients have had refund_contract() since 2026-09-26"
    )
    assert "after the locktime" in message, "say why a refund is not a recovery from this"
    assert "Nothing was signed" in message, "the reassurance an operator needs first"


def test_this_file_reads_the_real_modules_it_names():
    """A guard on the guards: every source this file asserts about was actually found.

    Rule 17's cheapest form. Three of the tests above are source reads, and a source
    read of a path that silently came back empty would pass every `not in` assertion
    in this file while measuring nothing.
    """
    for filename in _CLIENTS:
        source = _client_source(filename)
        assert len(source) > 1000, f"{filename} read back as {len(source)} bytes"
        assert "refund_contract" in source
    assert "conf_section_for_port" in (REPOSITORY_ROOT / "configure_env.py").read_text()
    assert Path(REPOSITORY_ROOT / "swap_terminal" / "chains" / "daemon_conf.py").is_file()
