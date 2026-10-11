"""configure_env.py discovers what it can, withholds secrets, and never mangles .env.

Role: tests (configure_env.py's discovery, its no-echo rule, and its atomic write)
Reads: .env.example (for the SECRET markers), and files it writes under tmp_path
Writes: nothing outside tmp_path -- every test redirects configure_env.ENV_FILE
Can move funds: no
Mainnet-safe: yes -- no RPC call, and docker is never invoked (discover_icp is patched)

=============================================================================
WHY THE WRITE IS THE PART THAT HAD TO BE TESTED FIRST
=============================================================================

This tool rewrites the operator's real .env, which already holds
GRIDCOIN_WALLET_PASSPHRASE, SWAP_DB_DIR and COMPOSE_FILE. A write that dropped a line it
did not own would take the passphrase with it -- and the failure would not show up here,
it would show up as a GRC payout refusing hours later. So the carry-through, the mode and
the atomicity are asserted before anything else.

=============================================================================
WHAT THE TOOL IS FOR, MEASURED ON THE OPERATOR'S HOST 2026-10-11
=============================================================================

Their ATM page showed all six coins as NONE. Not a swap defect:

    chain variables present in the web container   44
    BTC/LTC/GRC _RPC_PORT, _RPC_USER, _RPC_PASS    EMPTY
    SOL_RPC_URL, SOL_DEPOSIT_ACCOUNT, SOL_HOT_WALLET   EMPTY
    XRP_RPC_URL, XRP_DEPOSIT_ACCOUNT               EMPTY
    ICP_LEDGER_CANISTER_ID, ICP_OWNER_PRINCIPAL,
      ICP_DFX_SERVICE                              EMPTY
    names in .env                                  COMPOSE_FILE, SWAP_DB_DIR,
                                                   GRIDCOIN_WALLET_PASSPHRASE

docker-compose.web.yml passes each one as `${VAR:-}`, so an unset SHELL variable becomes
an empty STRING in the container and chains/registry.build_adapters() skips every chain
with no port or url. Forty-four present and empty; three persisted. The operator had
named this defect twice already -- "new shell should not rat fuck the entire fucking
machine. it should persist" -- and it had been fixed for three variables out of
forty-seven.
"""

from __future__ import annotations

import pytest
from network_target import CHAIN_PORTS

import configure_env


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point configure_env at a throwaway .env and keep docker out of it.

    BOTH HALVES MATTER. Without the redirect these tests would rewrite the repository's
    own .env; without stubbing discover_icp they would shell out to docker, which is
    neither available nor appropriate in a suite that promises to open no socket.
    """
    path = tmp_path / ".env"
    monkeypatch.setattr(configure_env, "ENV_FILE", path)
    monkeypatch.setattr(configure_env, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(configure_env, "discover_icp", lambda: {})
    return path


def test_a_line_it_does_not_own_is_carried_through_byte_for_byte(env_file):
    """THE ONE THAT MATTERS MOST. MUTATION: drop the `keep` list and this fails.

    The operator's .env holds their Gridcoin wallet passphrase. A write that rebuilt the
    file from only the values it was given would delete it, and the failure would surface
    hours later as a payout refusing to unlock -- nowhere near this step.
    """
    env_file.write_text(
        "# a comment nobody should touch\n"
        "COMPOSE_FILE=docker-compose.yml:docker-compose.web.armed-grc.yml\n"
        "GRIDCOIN_WALLET_PASSPHRASE='a passphrase that must survive'\n"
        "SWAP_DB_DIR=/home/somebody/.local/share/swap_terminal\n"
    )

    configure_env.write_env({"BTC_RPC_PORT": "18443"})

    written = env_file.read_text()
    assert "# a comment nobody should touch" in written
    assert "GRIDCOIN_WALLET_PASSPHRASE='a passphrase that must survive'" in written
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.web.armed-grc.yml" in written
    assert "SWAP_DB_DIR=/home/somebody/.local/share/swap_terminal" in written
    assert "BTC_RPC_PORT='18443'" in written


def test_a_name_it_owns_is_replaced_and_not_duplicated(env_file):
    """Two lines for one name is worse than either value alone.

    dotenv parsers disagree about which wins, so a duplicate makes the effective value
    depend on the reader -- and compose and config.read_env_file() are two different
    readers of this same file.
    """
    env_file.write_text("BTC_RPC_PORT='8332'\nOTHER=keepme\n")

    configure_env.write_env({"BTC_RPC_PORT": "18443"})

    written = env_file.read_text().splitlines()
    assert [line for line in written if line.startswith("BTC_RPC_PORT=")] == ["BTC_RPC_PORT='18443'"]
    assert "OTHER=keepme" in written


def test_the_file_is_mode_600_and_no_backup_is_left_behind(env_file):
    """Rule 2: the backup habit is what put a live GRIDCOIN_RPC_PASSWORD on GitHub.

    .env.bak.2026-03-28_185451 is the measured instance. So there is exactly one file at
    the end of a write, and the temp file it went through is renamed rather than copied.
    """
    configure_env.write_env({"BTC_RPC_USER": "someuser"})

    assert oct(env_file.stat().st_mode)[-3:] == "600"
    leftovers = sorted(child.name for child in env_file.parent.iterdir())
    assert leftovers == [".env"], f"a temp file or a backup was left behind: {leftovers}"


def test_a_value_is_single_quoted_so_it_survives_dotenv(env_file):
    """Unquoted loses a trailing `#` to a comment; double-quoted takes `$`, `\\` and `"`.

    An RPC password is exactly the kind of value that contains those, and one stored
    mangled fails as an authentication error nobody connects to this step.
    """
    configure_env.write_env({"BTC_RPC_PASS": 'p@ss w0rd$with"quotes#and\\backslash'})

    assert "BTC_RPC_PASS='p@ss w0rd$with\"quotes#and\\backslash'" in env_file.read_text()


def test_a_missing_env_file_is_created_rather_than_refused(env_file):
    """A first run on a fresh clone has no .env, and that is the normal case.

    This is the one place in the tree where creating a missing file is right -- the
    operator asked for this file to exist. db.connect_db()'s refusal is about a DATABASE,
    where a missing file means a wrong path; here it means a first run.
    """
    assert not env_file.exists()

    configure_env.write_env({"SOL_RPC_URL": "https://api.devnet.solana.com"})

    assert env_file.is_file()
    assert oct(env_file.stat().st_mode)[-3:] == "600"


def test_the_secret_list_comes_from_the_template_and_covers_the_rpc_passwords():
    """MUTATION: hardcode a list here and the next `# SECRET` in the template is echoed.

    The marker in .env.example is the only thing that decides whether a value is read
    without echo, which is the one place in this tool where being wrong cannot be undone:
    scrollback cannot be unseen.
    """
    secrets = configure_env.secret_names()

    for credential in ("BTC_RPC_PASS", "LTC_RPC_PASS", "GRC_RPC_PASS",
                       "GRIDCOIN_WALLET_PASSPHRASE"):
        assert credential in secrets, f"{credential} is not marked SECRET, so it would be ECHOED"

    # And a public value must NOT be in there, or the prompt hides something it should show.
    for public in ("BTC_RPC_PORT", "SOL_RPC_URL", "SOL_DEPOSIT_ACCOUNT", "XRP_DEPOSIT_ACCOUNT"):
        assert public not in secrets, f"{public} is public; reading it with no echo hides it for nothing"


def test_every_asked_credential_is_marked_secret_in_the_template():
    """The two lists must agree, and the direction of the failure decides which to trust.

    ASKED names what the tool prompts for; the template decides which prompts hide their
    input. A credential in the first and not the second is echoed to a terminal. Checked
    as a cross-product rather than per name so a tenth entry added to ASKED is covered
    without this test being edited.
    """
    secrets = configure_env.secret_names()
    asked = {name for name, _what in configure_env.ASKED}

    for name in sorted(asked):
        looks_like_a_credential = name.endswith(("_PASS", "_PASSPHRASE", "_SECRET", "_SEED"))
        if looks_like_a_credential:
            assert name in secrets, (
                f"{name} is asked for and is not marked SECRET in .env.example, so it would be "
                f"echoed to the terminal"
            )


def test_discovery_reports_a_reason_beside_every_value(env_file, capsys):
    """A discovered value is a claim about the operator's machine, so it has to be checkable.

    `BTC_RPC_PORT 18443` alone gives them no way to verify it. "because something is
    listening there" is checkable in one command, which is rule 14's "state what the
    number means, next to the number".
    """
    found = configure_env.discover_all()
    printed = capsys.readouterr().out

    for variable in found:
        assert variable in printed
    assert "<-" in printed, "no reason was printed beside any value"
    # The two endpoint defaults are always discoverable and must name their network,
    # because a URL alone does not say devnet from mainnet to a reader in a hurry.
    assert "DEVNET" in printed
    assert "TESTNET" in printed


def test_a_dry_run_writes_nothing(env_file, monkeypatch, capsys):
    """The default is to report. MUTATION: drop the `--apply` guard.

    A configuration tool that wrote on its first invocation would be one an operator
    cannot safely explore, and exploring is what somebody does when six lamps read NONE.
    """
    env_file.write_text("COMPOSE_FILE=docker-compose.yml\n")
    before = env_file.read_text()
    monkeypatch.setattr("sys.argv", ["configure_env.py"])

    assert configure_env.main() == 0

    assert env_file.read_text() == before, "a run without --apply modified .env"
    assert "Nothing was written" in capsys.readouterr().out


def test_apply_writes_the_discovered_values_and_the_answers(env_file, monkeypatch, capsys):
    """End to end through main(), with the answers supplied rather than typed.

    ST_ANSWERS IS THE ONLY NON-TERMINAL PATH and it exists so this test runs the REAL
    main(), the real discovery and the real write -- not a paraphrase of them. The
    prompt itself is the one line it replaces.
    """
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply"])
    monkeypatch.setenv("ST_ANSWERS", "BTC_RPC_USER=btcuser;BTC_RPC_PASS=btcpass;SOL_HOT_WALLET=SoLHotWallet")

    assert configure_env.main() == 0

    written = env_file.read_text()
    assert "BTC_RPC_USER='btcuser'" in written
    assert "SOL_HOT_WALLET='SoLHotWallet'" in written
    # The discovered endpoint defaults land too, without being asked for.
    assert "SOL_RPC_URL='https://api.devnet.solana.com'" in written
    assert "ICP_DFX_SERVICE='icp-replica'" in written

    printed = capsys.readouterr().out
    assert "force-recreate" in printed, (
        "the REQUIRED next step is missing; an already-running container keeps the environment "
        "it started with, so .env alone changes nothing a serving process can see"
    )
    # AND NO SECRET REACHED THE TERMINAL.
    assert "btcpass" not in printed, "a credential was echoed"


def test_an_already_set_value_is_not_asked_for_again(env_file, monkeypatch, capsys):
    """A second run must be cheap, or it is a run nobody makes.

    MUTATION: drop the `already.get(name)` filter and every value is re-asked, which on
    the credential prompts means an operator retyping three passwords to change a port.
    """
    env_file.write_text("BTC_RPC_USER='alreadyset'\nSOL_RPC_URL='https://already.example'\n")
    monkeypatch.setattr("sys.argv", ["configure_env.py"])

    configure_env.main()
    printed = capsys.readouterr().out

    would_ask = printed.split("WOULD ASK FOR", 1)[1]
    assert "BTC_RPC_USER" not in would_ask, "a value already in .env was queued to be asked again"
    assert "LTC_RPC_USER" in would_ask, "a value that is NOT set must still be asked for"


def test_a_value_containing_a_single_quote_is_refused_rather_than_mangled(env_file, monkeypatch, capsys):
    """There is no escape for one inside single quotes in any dotenv dialect.

    Stored mangled it would not fail here. It would fail as an authentication error
    against a daemon, hours away, with nothing pointing back at this step.
    """
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply"])
    monkeypatch.setenv("ST_ANSWERS", "BTC_RPC_USER=it's-bad;LTC_RPC_USER=fine")

    configure_env.main()

    written = env_file.read_text()
    assert "BTC_RPC_USER" not in written, "a value with a single quote was written anyway"
    assert "LTC_RPC_USER='fine'" in written, "one refusal stopped the other values being written"
    assert "REFUSED" in capsys.readouterr().out


def test_the_ports_come_from_the_shared_table_and_never_mainnet(env_file, monkeypatch):
    """Rule 8: network_target.CHAIN_PORTS is the one place that answers this.

    =========================================================================
    THE FIRST VERSION OF THIS TEST PASSED WITHOUT EXECUTING THE LINE IT TESTED
    =========================================================================

    It monkeypatched `listening_ports` to return EVERY port in the table, so every chain
    had two or more listening test ports, `len(candidates) == 1` was never true, and
    discover_ports() returned {}. The assertions were a `for` loop over an empty dict,
    which passes. Meanwhile the line inside that loop said `ports.variable` and
    `ports.hint` -- neither of which is a field of ChainPorts -- and the operator got
    `AttributeError: 'ChainPorts' object has no attribute 'hint'` on the first real run.

    So: a non-empty assertion FIRST, and exactly one listening test port per chain, which
    is the configuration an operator is actually in. This is the same
    "check passed by looking at the wrong thing" shape this suite has paid for four times
    today, committed in the test written to prevent it.

    MAINNET IS STILL NEVER OFFERED, and it is asserted by including every mainnet port in
    the listening set. ChainPorts carries the mainnet port to CLASSIFY a configured one,
    not to suggest it -- config.py's header records what suggesting it cost: the three
    defaults used to be mainnet and refresh_wallet_inventory() polled the operator's live
    staking wallet on a loop.
    """
    mainnet = {ports.mainnet_port for ports in CHAIN_PORTS.values()}
    # ONE test port per chain, plus every mainnet port. The mainnet ports must be ignored
    # and the single test port must be offered.
    one_test_port_each = {min(ports.test_ports) for ports in CHAIN_PORTS.values()}
    monkeypatch.setattr(configure_env, "listening_ports", lambda: mainnet | one_test_port_each)

    found = configure_env.discover_ports()

    assert len(found) == len(CHAIN_PORTS), (
        f"discover_ports() returned {sorted(found)} for {len(CHAIN_PORTS)} chains with exactly one "
        f"listening test port each. An empty or short result makes every assertion below vacuous, "
        f"which is how `ports.hint` reached the operator."
    )
    every_test_port = {port for ports in CHAIN_PORTS.values() for port in ports.test_ports}
    for variable, (value, why) in found.items():
        assert int(value) in every_test_port, f"{variable} was offered {value}, which is not a test port"
        assert int(value) not in mainnet, f"{variable} was offered the MAINNET port {value}"
        # AND THE REASON IS NON-EMPTY, which is what actually exercises `test_hint`: the
        # crash was inside the f-string building this string, so an assertion that only
        # looked at the value would still not have reached it.
        assert why.strip(), f"{variable} was offered with no reason beside it"
        assert str(value) in why, "the reason does not name the port it is about"


def test_every_field_this_tool_reads_off_ChainPorts_exists(env_file):
    """MUTATION: rename a field in network_target.ChainPorts and this fails, not the operator.

    THE DIRECT ANSWER TO THE AttributeError. discover_ports() reads three fields --
    test_ports, port_variable and test_hint -- and it got two of the three names wrong by
    inferring them from the table's positional arguments. getattr() here is the assertion
    that the names are real, independent of whether any port happens to be listening, so
    it holds even on a machine where discover_ports() can return nothing at all.
    """
    for asset, ports in CHAIN_PORTS.items():
        for field in ("test_ports", "port_variable", "test_hint", "mainnet_port"):
            assert hasattr(ports, field), (
                f"configure_env.discover_ports() reads ChainPorts.{field} and {asset}'s entry has "
                f"no such field. This is what reached the operator as AttributeError on a real run."
            )
        assert ports.port_variable.endswith("_RPC_PORT"), (
            f"{asset}'s port_variable is {ports.port_variable!r}; configure_env writes it into .env "
            f"as a variable name, so a value that is not one would write a line compose never reads"
        )


def test_two_listening_test_ports_for_one_chain_are_not_resolved_by_guessing(env_file, monkeypatch):
    """Two networks of one chain up means picking one is a guess about what is meant.

    So neither is offered and the value is asked for instead -- rule 17: a reason to
    believe is not the same as having checked, and the operator knows which they mean.
    """
    btc = CHAIN_PORTS["BTC"]
    assert len(btc.test_ports) > 1, "this test needs a chain with more than one test port"
    monkeypatch.setattr(configure_env, "listening_ports", lambda: set(btc.test_ports))

    assert "BTC_RPC_PORT" not in configure_env.discover_ports()
