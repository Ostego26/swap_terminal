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

import ast
import pathlib

import pytest
from network_target import CHAIN_PORTS
from regtest.daemons import CHAIN_DEFAULTS

import configure_env


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point configure_env at a throwaway .env, a throwaway database, and keep docker out.

    THREE HALVES NOW, AND THE THIRD WAS ADDED BECAUSE THE SECOND WAS NOT ENOUGH. Without
    the .env redirect these tests would rewrite the repository's own .env; without
    stubbing discover_icp they would shell out to docker, which is neither available nor
    appropriate in a suite that promises to open no socket.

    AND WITHOUT THE SWAP_DB_PATH REDIRECT they would read the operator's real database,
    because discover_all() now reads the shared deposit accounts out of it. That is a
    read-only read, so it would have broken nothing -- it would have done something
    worse: made every assertion about what gets written depend on whether the host
    running the suite happens to have SOL swaps in its database. A test whose answer
    changes with the machine cannot distinguish a regression from a different laptop.
    SWAP_DB_PATH in the process environment is the first branch of
    config.database_path(), so setting it is a complete redirect rather than a partial
    one, and the file it names does not exist -- which is the state
    accounts_from_the_database() reports and does not raise on.
    """
    path = tmp_path / ".env"
    monkeypatch.setattr(configure_env, "ENV_FILE", path)
    monkeypatch.setattr(configure_env, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(configure_env, "discover_icp", lambda: {})
    monkeypatch.setenv("SWAP_DB_PATH", str(tmp_path / "swap_terminal.db"))
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
    found = configure_env.discover_all({})
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


# =============================================================================
# A VARIABLE IN NEITHER LIST WAS SILENTLY ABSENT
# =============================================================================
#
# FOUND ON THE OPERATOR'S FIRST SUCCESSFUL RUN. The three Bitcoin-family ports were
# discovery-only -- probed against CHAIN_PORTS, and absent from ASKED because probing was
# expected to answer. Nothing was listening on 18443 or 18332, so their run printed
#
#     WOULD WRITE      7 discovered value(s) and 9 asked value(s)
#
# with BTC_RPC_PORT in NEITHER list. BTC would have stayed unset, the container would have
# built no BTC adapter, and the ATM page would have shown BTC as NONE after a
# configuration run that reported success. That is the defect this whole tool exists for
# -- a variable nothing sets and nothing mentions -- reproduced inside the fix for it, and
# rule 13's "treat 'skipped' plus 'success' in the same output as a defect in the output".
#
# AND THE FIRST REPAIR HAD THE SAME HOLE. A hand-written second list covering the three
# ports left the two ICP values -- which are discovered from the replica -- falling through
# it: a replica that does not answer reported them as unset and never asked. The ask list
# is now the COMPLEMENT of what this tool can set, which has no gap by construction.


def test_a_port_that_discovery_misses_becomes_a_question(env_file, monkeypatch, capsys):
    """THE REGRESSION TEST. MUTATION: make asked_for() return ASKED unchanged.

    Nothing listening anywhere is exactly the operator's BTC case: their daemon was down
    or on another port, and a probe cannot tell those apart -- which is why the answer is
    to ask rather than to guess.
    """
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr("sys.argv", ["configure_env.py"])

    configure_env.main()
    would_ask = capsys.readouterr().out.split("WOULD ASK FOR", 1)[1]

    for ports in CHAIN_PORTS.values():
        assert ports.port_variable in would_ask, (
            f"{ports.port_variable} was neither discovered nor asked for, so the chain would stay "
            f"unconfigured after a run that reported success"
        )
    # AND THE PROMPT IS ANSWERABLE, which is the half a bare name does not satisfy: the
    # hint comes from ChainPorts.test_hint, the same string the workers' banner prints.
    assert "18443 regtest, 18332 testnet" in would_ask
    assert "down or on another port" in would_ask


def test_an_undiscoverable_icp_value_also_becomes_a_question(env_file, monkeypatch, capsys):
    """The case the FIRST repair missed, which is why the list is a complement now.

    The ICP values are read from the running replica. A replica that does not answer is
    an ordinary state -- it is started for a session of work and stopped after -- so
    "could not discover" must turn into a question, not into silence.
    """
    monkeypatch.setattr(configure_env, "discover_icp", lambda: {})
    monkeypatch.setattr("sys.argv", ["configure_env.py"])

    configure_env.main()
    would_ask = capsys.readouterr().out.split("WOULD ASK FOR", 1)[1]

    for variable in ("ICP_LEDGER_CANISTER_ID", "ICP_OWNER_PRINCIPAL"):
        assert variable in would_ask, f"{variable} was reported unset and never asked for"
    # A canister id is not guessable -- every fresh replica issues different ones -- so the
    # prompt has to name the command that prints it.
    assert "dfx canister id icp_ledger_canister" in would_ask
    assert "swap_stack.py status" in would_ask


def dry_run_sets(monkeypatch, capsys):
    """A dry run's two derivations, as sets: what it will ASK for, and what it says is unset.

    THEY ARE COMPUTED INDEPENDENTLY AND MUST AGREE, which is what makes the comparison
    below a gate rather than a tautology. asked_for() walks ASKED plus the complement of
    every_variable_this_tool_can_set(); still_unset() walks that same table against what
    was discovered and what .env already holds, and never consults asked_for() at all. A
    variable that falls out of one and not the other is the hole BTC_RPC_PORT fell
    through on the operator's first good run.
    """
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr("sys.argv", ["configure_env.py"])
    already = configure_env.existing_assignments()
    discovered = configure_env.discover_all(already)
    capsys.readouterr()
    values = {name: value for name, (value, _why) in discovered.items() if not already.get(name)}
    will_ask = {name for name, _what in configure_env.asked_for(discovered, already)
                if not already.get(name) and name not in values}
    reported_unset = set(configure_env.still_unset(values, already))
    return will_ask, reported_unset, set(values), already


def test_nothing_this_tool_can_set_falls_between_discovery_and_the_prompt(env_file, monkeypatch, capsys):
    """THE GENERAL GATE, REWRITTEN 2026-10-11 BECAUSE ITS OLD FORM COULD NOT FAIL.

    The property is unchanged and is the one that survives somebody adding an eighteenth
    variable: after a run, every name this tool can set is already in .env, discovered, or
    queued to be asked. A name in none of those is the hole, whatever its chain.

    WHAT IT USED TO ASSERT, and why that was worthless:

        assert "(none)  <- every variable this tool can set is accounted for" in printed

    say_dry_run() computed that `(none)` as

        every_variable_this_tool_can_set() - (values | asked | already)

    and asked_for() builds its list AS the complement of every_variable_this_tool_can_set(),
    so the subtrahend always covers the minuend. The difference is empty for every input --
    measured over 4000 randomized (already, discovered) pairs, 0 non-empty. The gate was
    asserting that a constant was itself.

    IT NOW COMPARES TWO INDEPENDENT DERIVATIONS of the same set, so dropping a name from
    either one makes them disagree. The test below this is the control and it fails when
    asked_for() drops a name -- which the old control could not establish, because it
    patched asked_for() and thereby patched away the construction that made the old block
    vacuous. It proved the FORMATTER could print a name while the thing it was guarding
    against was true all along.
    """
    will_ask, reported_unset, discovered_names, already = dry_run_sets(monkeypatch, capsys)

    assert reported_unset == will_ask, (
        f"the dry run says these are unset without asking for them: "
        f"{sorted(reported_unset - will_ask)}; and asks for these without reporting them "
        f"unset: {sorted(will_ask - reported_unset)}. Either way a variable has fallen "
        f"between the two halves of the report, which is how BTC_RPC_PORT reached the operator"
    )
    # And together the three groups cover the whole surface, with the denominator stated
    # (rule 3) so a count here cannot be read without what it was counted out of.
    everything = set(configure_env.every_variable_this_tool_can_set())
    covered = discovered_names | will_ask | {name for name in already if already.get(name)}
    assert everything - covered == set(), f"{sorted(everything - covered)} is in no group at all"


def test_the_gate_above_fails_when_a_name_is_dropped_from_the_ask_list(env_file, monkeypatch, capsys):
    """THE CONTROL, and the mutation it applies is the real defect rather than a stand-in.

    asked_for() drops SOL_HOT_WALLET. still_unset() does not consult asked_for(), so it
    still reports the variable as unset while nothing will ask for it -- the two sets
    disagree and the gate above fails. That is precisely the shape of the first defect:
    a variable discovery did not answer and the question list did not carry.

    THE OLD CONTROL APPLIED THE SAME PATCH AND ASSERTED THE OPPOSITE THING -- that the
    "STILL UNSET" section named SOL_HOT_WALLET. It passed, and it had to: patching
    asked_for() is the one way to make a set that is otherwise empty by construction
    non-empty. So the control demonstrated the formatter, not the system, and the
    constant it was written to catch stayed a constant for its whole life.
    """
    monkeypatch.setattr(
        configure_env, "asked_for",
        lambda _discovered, _already: [(name, what) for name, what in configure_env.ASKED
                                       if name != "SOL_HOT_WALLET"],
    )
    will_ask, reported_unset, _discovered, _already = dry_run_sets(monkeypatch, capsys)

    assert "SOL_HOT_WALLET" in reported_unset
    assert "SOL_HOT_WALLET" not in will_ask
    assert reported_unset != will_ask, (
        "the gate above cannot fail: a variable nothing asks for was not reported unset"
    )


def test_a_skipped_prompt_is_counted_after_the_write_not_only_as_it_scrolls_past(
        env_file, monkeypatch, capsys):
    """MUTATION: drop say_still_unset() from say_what_was_written().

    THE APPLY PATH HAD NO SUCH BLOCK AT ALL, which is the second half of the defect.
    collect_answers() prints one line per skip as it happens, so an operator who pressed
    enter past three BTC prompts saw three "skipped" lines, then a WROTE block counting
    only what succeeded, and nothing that added them up. Rule 13: "treat 'skipped' plus
    'success' in the same output as a defect in the output."

    ST_ANSWERS supplies two values and omits every other, so the rest are skipped -- the
    real collect_answers(), the real write, the real report.
    """
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply"])
    monkeypatch.setenv("ST_ANSWERS", "BTC_RPC_USER=btcuser;SOL_HOT_WALLET=SoLHotWallet")

    assert configure_env.main() == 0
    printed = capsys.readouterr().out
    after = printed.split("STILL UNSET", 1)
    assert len(after) == 2, f"the write reported nothing about what it left unset:\n{printed}"

    total = len(configure_env.every_variable_this_tool_can_set())
    assert f"of {total} this tool can set" in after[1], (
        "the count was printed without its denominator (rule 3)"
    )
    # BTC_RPC_PASS was asked for and skipped, so it is unset and must be named.
    assert "BTC_RPC_PASS" in after[1]
    # BTC_RPC_USER was answered, so it must NOT be -- otherwise the block is a copy of
    # the ask list rather than a report of the outcome.
    assert "BTC_RPC_USER" not in after[1].split("the chains these belong to", 1)[0]


def test_the_dry_run_names_what_depends_on_an_answer(env_file, monkeypatch, capsys):
    """MUTATION: drop the listing loop under AFTERWARDS, leaving only the counts.

    A COUNT IS NOT AN ANSWER TO "which ones". The operator's run on 2026-10-11 had six
    variables waiting on an answer and three of them were unanswerable -- no bitcoin.conf,
    no cookie file, nothing listening on 18332 or 18443. Which three is the whole content
    of that block; "6 of 17" alone sends them back up the screen to work it out.

    The block the names replaced said "(none)", so this is also the regression test for
    that: the section must name what it is counting.
    """
    # The expected set, derived the way the tool derives it -- with `values` holding what
    # discovery answered. still_unset({}, already) would be the wrong comparison and said
    # so when I wrote it that way first: it named ICP_DFX_SERVICE, which discovery HAD
    # answered, so the test was asserting that a found value was still a question.
    _will_ask, expected_pending, _discovered, _already = dry_run_sets(monkeypatch, capsys)

    assert configure_env.main() == 0
    printed = capsys.readouterr().out

    parts = printed.split("AFTERWARDS", 1)
    assert len(parts) == 2, f"the dry run printed no AFTERWARDS section:\n{printed}"
    section = parts[1]
    total = len(configure_env.every_variable_this_tool_can_set())
    assert f"of {total}" in section, "the counts were printed without their denominator (rule 3)"
    assert "ONLY IF YOU ANSWER" in section
    # Every name that is waiting on an answer is listed, not just counted. Derived here
    # the same way the tool derives it, so an eighteenth variable is covered without this
    # test being edited.
    assert expected_pending, "nothing is pending, so this test is not measuring the listing"
    for name in sorted(expected_pending):
        assert name in section, f"{name} is waiting on an answer and the section does not name it"


def test_the_dry_run_still_names_a_variable_the_ask_list_dropped(env_file, monkeypatch, capsys):
    """WHY the AFTERWARDS section is derived from still_unset() and not from `needed`.

    MUTATION: `pending = [name for name, _what in needed]`. It passes every other test in
    this file, and it has to -- the gate above asserts those two sets are EQUAL, so while
    the gate holds, either source prints the same screen. This is the case the gate exists
    to catch, measured on the OUTPUT rather than on the sets: with a hole punched in the
    ask list, the operator still sees the variable named.

    That is the difference between a report derived from the question list and one derived
    from the table of everything this tool can set. The first can only ever tell you about
    questions it remembered to ask. BTC_RPC_PORT was exactly the variable it would have
    forgotten.
    """
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr(
        configure_env, "asked_for",
        lambda _discovered, _already: [(name, what) for name, what in configure_env.ASKED
                                       if name != "SOL_HOT_WALLET"],
    )
    monkeypatch.setattr("sys.argv", ["configure_env.py"])

    assert configure_env.main() == 0
    section = capsys.readouterr().out.split("AFTERWARDS", 1)[1]

    assert "SOL_HOT_WALLET" in section, (
        "a variable nothing will ask for was not reported as unset, so a hole in the ask "
        "list is invisible on the screen an operator reads"
    )


def test_the_nothing_to_write_exit_says_what_it_left_dark(env_file, monkeypatch, capsys):
    """MUTATION: drop say_still_unset() from main()'s `if not values` exit.

    "NOTHING TO WRITE. .env is unchanged." IS THE SAME SENTENCE FOR OPPOSITE STATES. On a
    host that is already fully configured it means there was nothing to do. On a host
    where every prompt was skipped it means nothing got configured. Rule 14: "make 'did
    nothing' look different from 'did work'" -- and this is the harder case, where "did
    nothing" is correct and the question is whether it was the good kind.

    CONSTRUCTED SO NOTHING IS DISCOVERED AND NOTHING IS ANSWERED: the three
    always-discoverable values are already in .env, no port is listening, the replica is
    stubbed out, there is no database to read an account from, and ST_ANSWERS is empty so
    every prompt is skipped. That is the state the operator would be in if they pressed
    enter past all six of their questions.
    """
    env_file.write_text(
        "SOL_RPC_URL='https://api.devnet.solana.com'\n"
        "XRP_RPC_URL='https://s.altnet.rippletest.net:51234'\n"
        "ICP_DFX_SERVICE='icp-replica'\n"
    )
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply"])
    monkeypatch.setenv("ST_ANSWERS", "")

    assert configure_env.main() == 0
    printed = capsys.readouterr().out

    assert "NOTHING TO WRITE" in printed
    after = printed.split("STILL UNSET", 1)
    assert len(after) == 2, (
        f"a run that skipped every prompt said only 'nothing to write', which is the same "
        f"output a fully configured host produces:\n{printed}"
    )
    # BOUNDED TO THE BLOCK, not to the rest of the screen. This assertion read everything
    # after "STILL UNSET" and started failing the moment say_arming() was added below it,
    # because that block prints its own `(none)` when no arming overlay exists. The claim
    # was always about THIS block; the slice was wider than the claim, which is the same
    # shape as the prose-read-as-code defect two commits ago -- an assertion true of text
    # adjacent to the thing it names.
    block = after[1].split("PAYOUTS ARE ARMED", 1)[0]
    assert "(none)" not in block, "a run that configured nothing reported nothing unset"
    for name in ("BTC_RPC_USER", "BTC_RPC_PASS", "SOL_HOT_WALLET", "XRP_DEPOSIT_ACCOUNT"):
        assert name in block, f"{name} was skipped and the report does not name it"


def test_the_still_unset_report_varies_with_what_was_actually_set(capsys):
    """Called with seeded dicts, both branches, because `(none)` has to be able to vary.

    THIS IS THE ASSERTION THE OLD CONTROL WAS REACHING FOR and could not make through
    main(): that the block's empty case is a RESULT and not a constant. say_still_unset()
    is an extracted function for exactly this reason (rule 10) -- the decision is callable
    with seeded inputs, so neither branch needs a terminal, a database or a patch to the
    mechanism under test.
    """
    everything = configure_env.every_variable_this_tool_can_set()

    configure_env.say_still_unset(dict.fromkeys(everything, "set"), {})
    assert "(none)" in capsys.readouterr().out, "a fully configured run did not report (none)"

    configure_env.say_still_unset(dict.fromkeys(everything[1:], "set"), {})
    printed = capsys.readouterr().out
    assert "(none)" not in printed
    assert everything[0] in printed, "the one unset variable was not named"

    # AND AN EMPTY VALUE IN .env COUNTS AS UNSET, which is not a nicety:
    # docker-compose.web.yml passes every chain variable as `${VAR:-}`, so an empty
    # string reaches the container as an empty string and build_adapters() skips the
    # chain. Forty-four variables present and empty is the state this tool was written
    # for, and a report that counted them as set would describe it as fully configured.
    configure_env.say_still_unset({}, dict.fromkeys(everything, ""))
    assert everything[0] in capsys.readouterr().out, "an empty .env value was counted as set"


def test_an_overlays_prose_example_is_not_read_as_a_requirement():
    """MUTATION: drop the comment-line filter in arming_overlays().

    MEASURED AGAINST THE REAL OVERLAY FILES, not a fixture, because the hazard is in
    their actual text: every one of them explains compose's "at most one ${VAR:?} per
    file" rule IN PROSE, quoting the literal `${VAR:?}` in order to name it. A raw-text
    scan returns `VAR` as the requirement of two of the three.

    THIS IS THE THIRD TIME IN ONE DAY. tests/test_daemon_conf.py's chain-resolver register
    classified configure_env.py as an unclassified resolver because its docstring NAMES
    build_adapters(); the two gates below it had already learned the same lesson from
    testnet_wallets.py on 2026-10-10. A sentence that explains a rule is not an instance
    of it, and a scan that cannot tell the difference reports on the explanation.
    """
    overlays = configure_env.arming_overlays()

    assert overlays, "no arming overlay was found at the repository root"
    for asset, filename, variable in overlays:
        assert variable != "VAR", (
            f"{filename}'s prose example was read as {asset}'s requirement, so the block "
            f"would tell an operator to set a variable that does not exist"
        )
        # AN EMPTY VARIABLE IS A LEGITIMATE ANSWER and was not, until
        # docker-compose.web.armed-icp.yml. This assertion read `assert variable` and
        # failed the moment that overlay dropped its ${VAR:?} -- correctly, because the
        # data model changed under it. ICP mounts the replica's identity volume instead
        # of naming a host path, so it requires nothing and `ls` on the operator's host
        # is why: `default` and `minter` both live on the volume, with no host file to
        # point at. What still must hold is that a variable REPORTED is a variable the
        # file actually interpolates.
        if variable:
            assert variable in pathlib.Path(filename).read_text(), (
                f"{filename} does not interpolate {variable}, so the block would tell an "
                f"operator to set something that file never reads"
            )


def test_the_arming_block_reports_presence_and_never_a_value(capsys):
    """MUTATION: print `already[variable]` beside the name.

    ONE OF THESE IS A WALLET PASSPHRASE AND ONE IS AN XRP SECRET SEED. The banner's
    "already in .env" line follows the same rule and says so; here the consequence is
    larger, and the only way to test a negative about printing is to seed a sentinel and
    assert its absence.
    """
    required = [variable for _asset, _filename, variable in configure_env.arming_overlays()]
    configure_env.say_arming(dict.fromkeys(required, LEAK_SENTINEL))
    printed = capsys.readouterr().out

    assert LEAK_SENTINEL not in printed, "a signing credential was printed by a report"
    # The NAMES are what make it actionable, so they must be there.
    for _asset, _filename, variable in configure_env.arming_overlays():
        assert variable in printed
    assert "set in .env" in printed


def test_the_arming_block_says_whether_the_overlay_is_actually_in_use(capsys):
    """MUTATION: print a constant instead of testing membership in COMPOSE_FILE.

    A variable set in .env and an overlay named on the command line are two separate
    conditions and BOTH are required -- compose refuses to start on the unset variable,
    and without the overlay the variable is never passed to the container at all. An
    operator who has done one of the two has done nothing, and the block has to tell them
    which half is missing.
    """
    # OVERLAYS THAT REQUIRE A VARIABLE, because the variable name is how a line is
    # found. ICP requires none -- it mounts the replica's identity volume -- so there
    # is no name to grep for and its own case is covered by the test below.
    overlays = [entry for entry in configure_env.arming_overlays() if entry[2]]
    assert len(overlays) >= 2, f"fewer than two overlays take a variable: {overlays}"
    first = overlays[0]

    configure_env.say_arming({"COMPOSE_FILE": f"docker-compose.yml:{first[1]}"})
    printed = capsys.readouterr().out
    lines = [line for line in printed.splitlines() if first[2] in line]
    assert lines and "named in COMPOSE_FILE" in lines[0], f"the active overlay was not recognized: {lines}"
    # And the others, which are not named, must read the other way.
    for _asset, filename, variable in overlays[1:]:
        other = [line for line in printed.splitlines() if variable in line]
        assert other and "NOT in COMPOSE_FILE" in other[0], f"{filename} was reported as active"


def test_an_overlay_that_needs_no_variable_says_so_rather_than_printing_a_blank(capsys):
    """MUTATION: drop the `if not variable` branch from say_arming().

    Without it the line reads "needs   <- NOT set in .env" -- a missing name where the
    operator looks for what to set, in the one block whose entire job is to say what is
    missing. That is rule 14's blank gap, and worse than silence because it reads as a
    variable whose name failed to render.

    DOCKER-COMPOSE.WEB.ARMED-ICP.YML IS THAT CASE and it is why this exists: its signing
    identity is created by dfx inside the replica container and lives on the `icp-state`
    named volume, so there is no host path to demand. The overlay mounts that volume
    read-only, nothing is copied, and naming the file on the command line is the act.
    """
    without = [entry for entry in configure_env.arming_overlays() if not entry[2]]
    assert without, "no overlay requires zero variables; this test has stopped measuring"

    configure_env.say_arming({})
    printed = capsys.readouterr().out

    assert "needs no variable" in printed
    assert "naming the file IS the arming" in printed
    # And no line offers an empty name to set.
    assert "needs   <-" not in printed, "a line printed a blank where a variable name belongs"


def test_the_arming_block_is_a_result_when_there_is_no_overlay(env_file, capsys):
    """Rule 14: a blank gap is ambiguous between "nothing to arm" and "the scan broke".

    The env_file fixture points REPO_ROOT at tmp_path, so the glob finds nothing -- which
    is also the real state of a checkout where the overlays were deleted, and that state
    means NO chain can sign a payout from it. Silence would read as "nothing to worry
    about" for the one condition that disables every payout path at once.
    """
    del env_file
    configure_env.say_arming({})
    printed = capsys.readouterr().out

    assert "(none)" in printed
    assert "no chain can sign a payout" in printed.replace("\n", " ").replace("   ", "")


def test_every_exit_from_main_reports_the_arming_state(env_file, monkeypatch, capsys):
    """MUTATION: drop say_arming() from any one of main()'s three returns.

    THE WIRING GAP AGAIN, and it is the second time in this file: the first was
    accounts_from_the_database(), correct and fully tested and called by nothing. A report
    that only some exits print is worse than one that prints nowhere, because the operator
    learns to expect it and then does not get it on the run that mattered.

    Each case below is a real exit, driven through the real main(): report-only, write,
    and write-nothing.
    """
    always_present = (
        "SOL_RPC_URL='https://api.devnet.solana.com'\n"
        "XRP_RPC_URL='https://s.altnet.rippletest.net:51234'\n"
        "ICP_DFX_SERVICE='icp-replica'\n"
    )
    monkeypatch.setattr(configure_env, "listening_ports", set)

    for label, argv, answers, env_text in (
        ("the dry run", ["configure_env.py"], None, ""),
        ("a run that writes", ["configure_env.py", "--apply"], "BTC_RPC_USER=u", ""),
        ("a run with nothing to write", ["configure_env.py", "--apply"], "", always_present),
    ):
        env_file.write_text(env_text)
        monkeypatch.setattr("sys.argv", argv)
        if answers is None:
            monkeypatch.delenv("ST_ANSWERS", raising=False)
        else:
            monkeypatch.setenv("ST_ANSWERS", answers)

        assert configure_env.main() == 0
        printed = capsys.readouterr().out
        assert "PAYOUTS ARE ARMED" in printed, f"{label} did not report the arming state"


def test_no_exit_added_later_can_skip_the_arming_report():
    """The structural half, which the three cases above cannot give.

    A FOURTH EXIT ADDED LATER would pass the test above untouched -- it enumerates the
    exits it knows, and CLAUDE.md's note on this exact failure is that "a check that
    enumerates what it checks measures the list". So this walks main()'s own AST and
    requires every `return` to have a say_arming() call somewhere before it in the same
    block.

    IT IS A COVERAGE PROPERTY OVER CODE THAT DOES NOT EXIST YET, which is the one case
    where reading the source is the only form of check available. It does not replace the
    behavioral test above and is not evidence the report is correct -- only that no exit
    is missing it.
    """
    source = pathlib.Path(configure_env.__file__).read_text()
    main = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef) and node.name == "main")

    # ANY OF THE REPORTERS, AND LEXICALLY BEFORE THE RETURN RATHER THAN IN ITS OWN BLOCK.
    # Both halves of that rule were learned by this gate being wrong, twice:
    #
    #   it knew ONE reporter      --arm grew its own exit, which reports through
    #                             say_arm_result() instead of the four-chain survey.
    #                             Accepting only say_arming() would have forced an --arm
    #                             run to print both, which is two answers to one question
    #                             on one screen.
    #   it required the SAME      --disarm made main() exceed C901, so the posture
    #   BLOCK                     decision was extracted to change_posture() and main()
    #                             became `posture = change_posture(...)` then `if posture
    #                             is not None: return posture`. That return is legitimate
    #                             -- the reporting happened inside the function that
    #                             produced the value -- and the per-block rule could not
    #                             see a call in the enclosing scope.
    #
    # So the test is now "some reporter was called at a lower line number", which is
    # weaker as a proof and right as a rule: it accepts delegation, which is the shape
    # rule 10 asks for. What it still refuses is an exit with no reporting before it
    # anywhere, which is the thing it exists to catch.
    REPORTERS = {"say_arming", "say_arm_result", "say_disarm_result", "change_posture"}
    reported_at = sorted(
        node.lineno for node in ast.walk(main)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in REPORTERS
    )
    assert reported_at, f"main() calls none of {sorted(REPORTERS)}; this gate measures nothing"

    returns = 0
    for node in ast.walk(main):
        if not isinstance(node, ast.Return):
            continue
        returns += 1
        assert any(line < node.lineno for line in reported_at), (
            f"the `return` at line {node.lineno} is reached before anything reports the arming "
            f"state, so an operator leaving main() by that path is told the 17 variables are the "
            f"whole story"
        )
    assert returns >= 4, f"main() has {returns} returns; this test has stopped measuring"


def test_every_help_string_tells_the_operator_what_to_do(env_file):
    """A prompt is only as good as its help, and these are the ones nobody can guess.

    MUTATION: empty any FALLBACK_HELP value. A port can at least be looked up; a canister
    id cannot, so an empty help string there is a question with no answerable form.
    """
    for variable, help_text in configure_env.FALLBACK_HELP.items():
        assert help_text.strip(), f"{variable} has empty help"
        assert "BLANK" in help_text, (
            f"{variable}'s help does not say that leaving it blank is allowed, so an operator who "
            f"cannot answer has no stated way past it"
        )

    # And the port fallback, which is generated rather than listed.
    for ports in CHAIN_PORTS.values():
        generated = configure_env._port_help(ports.port_variable)
        assert ports.test_hint in generated, "the generated port help does not name the test ports"
        assert "BLANK" in generated

    # A name with no entry anywhere must still get a usable prompt rather than a KeyError:
    # a crash in a configuration tool is worse than an unhelpful prompt.
    assert configure_env._port_help("SOMETHING_NEW").strip()


# =============================================================================
# READING CREDENTIALS OUT OF EACH DAEMON'S OWN CONF
# =============================================================================
#
# Operator, 2026-10-11, after the tool prompted for six credentials: "ji don't know any
# of those passwords". They were right not to. Measured on their host: their LTC and GRC
# confs both carry rpcuser and rpcpassword, and ~/.bitcoin/bitcoin.conf does not exist at
# all with bitcoind not running.
#
# I DECLINED TO BUILD THIS AT FIRST, citing chain-safety's "never move, copy, or read back
# a credential", and that refusal is what left them stuck. The rule is about CLAUDE: a
# credential must not be pulled into a conversation. A tool that runs on their machine,
# reads their conf, writes it into their .env and never prints it is a different thing.
# It is opt-in so the move is theirs.

#: A value that must never reach stdout. The tests below assert its ABSENCE from captured
#: output, which is the only way to test a negative about printing.
LEAK_SENTINEL = "a-password-that-must-never-be-printed"


@pytest.fixture
def litecoin_conf(tmp_path, monkeypatch, env_file):
    """A real litecoin.conf with credentials at the top level and the port in [test].

    THAT LAYOUT IS THE CONVENTIONAL ONE and chains/daemon_conf.parse_daemon_conf()'s
    docstring records why: "a regtest daemon is conventionally configured with rpcuser and
    rpcpassword at the top level and rpcport inside [regtest], because the port differs per
    network and the credentials do not." A fixture with everything at the top level would
    not exercise the section logic at all.
    """
    conf = tmp_path / "litecoin.conf"
    conf.write_text(
        f"# a comment\nrpcuser=ltcuser\nrpcpassword={LEAK_SENTINEL}\n[test]\nrpcport=19332\n"
    )
    monkeypatch.setattr(
        configure_env, "DAEMON_CONFS",
        {"LTC": (str(conf), "LTC_RPC_USER", "LTC_RPC_PASS")},
    )
    monkeypatch.setattr(configure_env, "listening_ports", lambda: {19332})
    return conf


def test_the_harness_conf_is_found_because_the_path_is_not_respelled():
    """MUTATION: drop the CHAIN_DEFAULTS branch from conf_candidates().

    =========================================================================
    I RESPELLED A PATH regtest/daemons.CHAIN_DEFAULTS ALREADY OWNS
    =========================================================================

    Measured 2026-10-11: CHAIN_DEFAULTS puts BTC's conf at ~/regtest/btc/bitcoin.conf and
    configure_env.DAEMON_CONFS said ~/.bitcoin/bitcoin.conf. chains/daemon_conf.py imports
    CHAIN_DEFAULTS for exactly this and its header says why: it "is the one place that owns
    each chain's datadir and conf name. Respelling them here would be two sources for the
    fact that moves whenever the harness is pointed somewhere else (rule 8)."

    THE COST WAS A CHAIN THE OPERATOR COULD NOT CONFIGURE. The moment they start this
    repository's own BTC regtest daemon, bitcoind writes that conf with both credentials --
    and --read-daemon-confs would have reported "no conf" about a file this tree had just
    created, then asked them for an rpcuser they already had.

    ASSERTED AGAINST CHAIN_DEFAULTS rather than against the literal path, so pointing the
    harness somewhere else moves this test with it instead of breaking it.
    """
    for chain, spec in CHAIN_DEFAULTS.items():
        expected = pathlib.Path(spec["datadir"]).expanduser() / spec["conf_name"]
        candidates = configure_env.conf_candidates(chain, spec["port"])

        assert expected in candidates, (
            f"{chain}'s harness conf at {expected} is in no candidate this tool looks at, so a "
            f"daemon started by this repository's own harness cannot be read"
        )
        # On the harness's own port the harness conf is tried FIRST: that daemon is the
        # harness, so its conf is the authority over any conventional file that also exists.
        assert candidates[0] == expected, f"{chain} on its regtest port tried {candidates[0]} first"


def test_a_testnet_port_prefers_the_operators_own_conf():
    """MUTATION: order the candidates the same way whatever the port.

    THE DISCRIMINATOR IS THE PORT AND IT WAS ALREADY MEASURED. CHAIN_DEFAULTS' ports are
    the regtest ones, so a daemon answering on a TESTNET port is not the harness -- it is
    the operator's own node, and its conf is the conventional one. Their litecoind answers
    on 19332 and its conf is ~/.litecoin/litecoin.conf; reading ~/regtest/ltc first would
    find a stale harness conf on a host that has run both.
    """
    conventional = pathlib.Path(configure_env.conventional_conf("LTC")).expanduser()

    assert configure_env.conf_candidates("LTC", 19332)[0] == conventional
    assert configure_env.conf_candidates("LTC", 19443)[0] != conventional, (
        "the regtest port did not prefer the harness conf, so the port is not deciding"
    )


def test_a_chain_the_harness_does_not_run_gets_one_candidate():
    """GRC has no CHAIN_DEFAULTS entry, and that is correct rather than a gap.

    The regtest harness runs Bitcoin and Litecoin. Gridcoin's conf lives in
    ~/.GridcoinResearch and is shared by mainnet and testnet both, which is why
    chains/daemon_conf.CONF_FALLBACK_NETWORK excludes it entirely and why this tool needs
    a separate flag to read it at all. A second candidate invented for it would be a path
    that does not exist on any host.
    """
    assert "GRC" not in CHAIN_DEFAULTS, "the harness grew a Gridcoin entry; this test must be revisited"

    candidates = configure_env.conf_candidates("GRC", 25715)

    assert len(candidates) == 1
    assert candidates[0] == pathlib.Path(configure_env.conventional_conf("GRC")).expanduser()
    # And a chain in neither table gets nothing rather than a crash.
    assert configure_env.conf_candidates("DOGE", 0) == []


def test_the_missing_conf_message_names_every_path_it_looked_in(env_file, monkeypatch, capsys):
    """MUTATION: name only the first candidate.

    An operator told "no conf at ~/.bitcoin/bitcoin.conf" goes and creates one there. The
    harness writes to ~/regtest/btc. A message naming one of two paths sends them to the
    wrong file, which is rule 14's whole subject: the output has to be usable a day later
    by somebody who does not already know the layout.
    """
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"BTC": ("/nowhere/conventional/bitcoin.conf", "BTC_RPC_USER", "BTC_RPC_PASS")})
    monkeypatch.setattr(configure_env, "listening_ports", set)

    configure_env.credentials_from_confs({}, {}, gridcoin=False)
    printed = capsys.readouterr().out

    assert "/nowhere/conventional/bitcoin.conf" in printed
    harness = pathlib.Path(CHAIN_DEFAULTS["BTC"]["datadir"]).expanduser() / CHAIN_DEFAULTS["BTC"]["conf_name"]
    assert str(harness) in printed, "the harness path was not named, so half the search is invisible"


def test_two_tables_naming_one_path_is_tried_once(monkeypatch):
    """MUTATION: drop the dedupe and this returns the same file twice.

    ADDED BECAUSE THE MUTATION SURVIVED. With BTC, LTC and GRC as they stand, the harness
    path and the conventional path never coincide, so the dedupe was a branch no test
    could reach -- and an unreachable branch is dead code (rule 9) unless it is either
    deleted or made live. It is reachable the moment DAEMON_CONFS is pointed at the
    harness directory, which is a thing an operator running only regtest would do.

    WHY IT IS WORTH KEEPING RATHER THAN DELETING: the duplicate is not harmless. The
    "no conf" line joins the candidates with " or ", so a host with neither file would
    read "no conf at ~/regtest/btc/bitcoin.conf or ~/regtest/btc/bitcoin.conf" -- output
    that contradicts itself about what it did, which rule 14 treats as a defect in the
    output rather than a cosmetic one.
    """
    harness = pathlib.Path(CHAIN_DEFAULTS["LTC"]["datadir"]).expanduser() / CHAIN_DEFAULTS["LTC"]["conf_name"]
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"LTC": (str(harness), "LTC_RPC_USER", "LTC_RPC_PASS")})

    for port in (CHAIN_DEFAULTS["LTC"]["port"], 19332):
        candidates = configure_env.conf_candidates("LTC", port)
        assert candidates == [harness], f"port {port} produced {candidates}"


def test_the_conf_table_is_read_at_call_time_and_not_copied_at_import(monkeypatch):
    """MUTATION: `CONVENTIONAL_CONF = {...}` at module scope instead of a function.

    IT WAS THAT FOR ONE EDIT AND BROKE SIX TESTS, in the commit whose whole subject is
    two sources for one fact. The fixtures in this file redirect a conf by monkeypatching
    DAEMON_CONFS; a dict comprehension evaluated at IMPORT had already taken its copy, so
    the patch redirected nothing and the tool read the operator's real ~/.litecoin while
    the test watched a file under tmp_path. A test that silently reads a real file instead
    of its fixture is the worst failure available to a suite -- it passes.
    """
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"LTC": ("/redirected/litecoin.conf", "LTC_RPC_USER", "LTC_RPC_PASS")})

    assert configure_env.conventional_conf("LTC") == "/redirected/litecoin.conf"
    assert pathlib.Path("/redirected/litecoin.conf") in configure_env.conf_candidates("LTC", 19332)


def test_a_credential_is_written_and_NEVER_printed(litecoin_conf, env_file, monkeypatch, capsys):
    """THE ONE THAT CANNOT BE GOT WRONG. MUTATION: put the value in the reason string.

    Scrollback cannot be unseen, and an operator pastes a configuration tool's output into
    a chat or an issue without thinking about it -- this conversation is full of exactly
    that. So the assertion is on the ABSENCE of the value from everything the tool said,
    which is the only way to test a negative about printing.
    """
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply", "--read-daemon-confs"])
    monkeypatch.setenv("ST_ANSWERS", "")

    configure_env.main()
    printed = capsys.readouterr().out

    assert LEAK_SENTINEL not in printed, "the rpcpassword was printed to stdout"
    assert LEAK_SENTINEL in env_file.read_text(), "the rpcpassword was not stored"
    assert "LTC_RPC_USER='ltcuser'" in env_file.read_text()
    # The REASON names the file and says the value was withheld, which is the same split
    # chains/daemon_conf.describe() makes.
    assert str(litecoin_conf) in printed
    assert "NOT shown" in printed


def test_the_section_comes_from_the_probed_port_not_a_fixed_default(litecoin_conf, env_file, monkeypatch, capsys):
    """MUTATION: pass network="regtest" as chains/daemon_conf.CONF_FALLBACK_NETWORK does.

    That table hardcodes regtest for LTC. The operator's litecoind answers on 19332, which
    is TESTNET, so a fixed regtest would read the wrong section of their real conf. Here it
    is asserted by the reason line naming the section that was actually consulted.
    """
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--read-daemon-confs"])

    configure_env.main()
    printed = capsys.readouterr().out

    assert "[test]" in printed, (
        "the conf was read without the section the probed port implies; 19332 is testnet and "
        "chains/daemon_conf.CONF_FALLBACK_NETWORK would have said regtest"
    )
    assert "[regtest]" not in printed


def test_gridcoin_is_refused_without_its_own_flag(tmp_path, env_file, monkeypatch, capsys):
    """chains/daemon_conf.py excludes Gridcoin from conf resolution, and the reason is money.

    Its conf is shared by mainnet and testnet, and a Gridcoin mainnet wallet is typically a
    live staking wallet. A single --read-daemon-confs would let an operator accept that
    without reading it, so the hazard has its own flag.
    """
    conf = tmp_path / "gridcoinresearch.conf"
    conf.write_text(f"rpcuser=grcuser\nrpcpassword={LEAK_SENTINEL}\n")
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"GRC": (str(conf), "GRC_RPC_USER", "GRC_RPC_PASS")})
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply", "--read-daemon-confs"])
    monkeypatch.setenv("ST_ANSWERS", "")

    configure_env.main()
    printed = capsys.readouterr().out

    assert "GRC_RPC_PASS" not in env_file.read_text(), "Gridcoin's credential was read without the flag"
    assert LEAK_SENTINEL not in printed
    assert "NOT READ" in printed
    assert "live staking wallet" in printed, "the hazard was not stated, so the refusal is unexplained"
    assert "--read-gridcoin-conf" in printed, "the way to proceed was not named"


def test_gridcoin_is_read_when_its_flag_is_passed(tmp_path, env_file, monkeypatch, capsys):
    """The opt-in works, or the refusal above is a dead end rather than a gate.

    The operator's GRC testnet daemon's credentials genuinely are in that shared file, and
    GRC_RPC_PORT is established by probing -- so the port decides which daemon they reach,
    and chains/daemon_network.test_network_verdict() is still the backstop that refuses a
    mainnet answer before anything is asked of a wallet.
    """
    conf = tmp_path / "gridcoinresearch.conf"
    conf.write_text(f"rpcuser=grcuser\nrpcpassword={LEAK_SENTINEL}\n")
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"GRC": (str(conf), "GRC_RPC_USER", "GRC_RPC_PASS")})
    monkeypatch.setattr(
        "sys.argv", ["configure_env.py", "--apply", "--read-daemon-confs", "--read-gridcoin-conf"])
    monkeypatch.setenv("ST_ANSWERS", "")

    configure_env.main()

    written = env_file.read_text()
    assert "GRC_RPC_USER='grcuser'" in written
    assert LEAK_SENTINEL in written
    assert LEAK_SENTINEL not in capsys.readouterr().out


def test_the_gridcoin_flag_alone_does_nothing_and_says_so(env_file, monkeypatch, capsys):
    """A flag that silently did nothing would be the worst of the three outcomes.

    An operator who passes only --read-gridcoin-conf has stated an intent; the tool cannot
    act on it without the other flag, so it says that rather than appearing to comply.
    """
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--read-gridcoin-conf"])

    configure_env.main()
    printed = capsys.readouterr().out

    assert "DOES NOTHING ON ITS OWN" in printed
    assert "READING CREDENTIALS" not in printed


def test_a_missing_conf_is_a_reported_result_not_a_crash(tmp_path, env_file, monkeypatch, capsys):
    """The operator's BTC case exactly: no conf, and bitcoind not running.

    A configuration tool that raised here would be unusable on precisely the host that
    needs it, and `(none)` is a result (rule 14).
    """
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"BTC": (str(tmp_path / "nothing-here.conf"), "BTC_RPC_USER", "BTC_RPC_PASS")})
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--read-daemon-confs"])

    configure_env.main()
    printed = capsys.readouterr().out

    assert "no conf at" in printed
    assert "nothing to read" in printed
    # And it still queues the credential as a question, so the chain is not silently lost.
    assert "BTC_RPC_USER" in printed.split("WOULD ASK FOR", 1)[1]


def test_an_rpcauth_only_conf_says_a_hash_is_not_a_password(tmp_path, env_file, monkeypatch, capsys):
    """rpcauth= is a salted hash and cannot be used as a password. Saying so is the fix.

    chains/base.py authenticates with auth=(user, password) and has no rpcauth path, so a
    conf using it has no usable credential here -- and an operator told only "no
    rpcpassword" would go looking for a line that is deliberately absent.
    """
    conf = tmp_path / "bitcoin.conf"
    conf.write_text("rpcauth=someuser:deadbeef$cafebabe\n")
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"BTC": (str(conf), "BTC_RPC_USER", "BTC_RPC_PASS")})
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--read-daemon-confs"])

    configure_env.main()
    printed = capsys.readouterr().out

    assert "has no rpcuser, rpcpassword" in printed
    assert "rpcauth" in printed and "HASH" in printed


def test_a_value_read_from_a_conf_is_not_ALSO_asked_for(litecoin_conf, env_file, monkeypatch, capsys):
    """Rule 13: 'skipped' plus 'success' in one output is a defect in the output.

    MUTATION: drop `name not in values` from the `needed` filter.

    The first real run of --read-daemon-confs printed, in order: the credential read from
    the conf, a prompt for it, "skipped LTC_RPC_USER (left unset)", and then LTC_RPC_USER
    in the WROTE list. Four lines, each true of the step that printed it, saying three
    incompatible things -- and the prompt was worse than the wording: an operator pressing
    enter past a value that was already found has been asked a question with no right
    answer.
    """
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply", "--read-daemon-confs"])
    monkeypatch.setenv("ST_ANSWERS", "")

    configure_env.main()
    printed = capsys.readouterr().out

    assert "skipped LTC_RPC_USER" not in printed, (
        "a value read from the conf was also asked for and then reported as skipped, while "
        "being written"
    )
    assert "LTC_RPC_USER" in env_file.read_text()


def test_the_conf_reader_uses_the_parser_that_already_exists(tmp_path, env_file, monkeypatch, capsys):
    """Rule 8: chains/daemon_conf.parse_daemon_conf() owns the two things this gets wrong.

    A `#` ONLY STARTS A COMMENT AT THE START OF A LINE, which that module records against a
    measurement: "an rpcpassword containing `#` is both legal and likely from a generator.
    Stripping inline would silently truncate it, and the failure would arrive as a 401 from
    the daemon with nothing pointing here."

    So a password containing `#` must survive intact, and that property comes free from
    using the shared parser -- which is the point. A hand-rolled reader here would be the
    second place to get it wrong.
    """
    conf = tmp_path / "litecoin.conf"
    awkward = "pass#with$hash"
    conf.write_text(f"rpcuser=ltcuser\nrpcpassword={awkward}\n")
    monkeypatch.setattr(configure_env, "DAEMON_CONFS",
                        {"LTC": (str(conf), "LTC_RPC_USER", "LTC_RPC_PASS")})
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--apply", "--read-daemon-confs"])
    monkeypatch.setenv("ST_ANSWERS", "")

    configure_env.main()

    assert f"LTC_RPC_PASS='{awkward}'" in env_file.read_text(), (
        "a password containing '#' was truncated, which means the inline-comment rule was "
        "re-implemented here instead of coming from the shared parser"
    )
    assert awkward not in capsys.readouterr().out


# ===========================================================================
# ARMING A CHAIN BY CHANGING COMPOSE_FILE, rather than by hand-editing a file
# that holds a wallet passphrase
# ===========================================================================
#
# 2026-10-11. Arming ICP is one change to one line of .env. What the operator got was
#
#     :docker-compose.web.armed-icp.yml
#
# in a code block, which they pasted at a shell prompt, which said "command not found".
# Then they recreated the container -- unarmed, because the line had never changed.
#
# THE DEFECT WAS THAT THERE WAS NOTHING TO RUN. .env holds GRIDCOIN_WALLET_PASSPHRASE,
# and asking somebody to hand-append a colon-separated fragment to one line of it is
# asking for the edit that takes the passphrase with it. write_env() has done this
# safely since the file was written.


@pytest.fixture
def env_file_with_overlays(env_file, tmp_path):
    """env_file, plus the REAL arming overlays copied in where REPO_ROOT now points.

    COPIED RATHER THAN PATCHED, so arm_verdict() runs the real glob over the real file
    contents -- including the prose that quotes `${VAR:?}` while explaining the
    one-required-variable rule, which is what arming_overlays()' comment filter exists
    for. Monkeypatching arming_overlays() to return a literal list would make these
    tests pass with that filter deleted.

    THE env_file FIXTURE POINTS REPO_ROOT AT tmp_path, which it must: write_env() puts
    its temp file there before the rename, so a real REPO_ROOT would mean a test writing
    `.env.new.*` into the checkout. That redirect is also why the glob found nothing
    until this fixture existed -- four arming tests failed with "this tree has (none)",
    which is the fixture telling the truth about an empty directory.
    """
    real_root = pathlib.Path(configure_env.__file__).resolve().parent
    copied = 0
    for overlay in sorted(real_root.glob("docker-compose.web.armed-*.yml")):
        (tmp_path / overlay.name).write_text(overlay.read_text())
        copied += 1
    assert copied >= 3, f"only {copied} overlay(s) copied; this fixture has stopped measuring"
    return env_file


def test_arming_appends_the_overlay_and_keeps_the_rest_of_compose_file(env_file_with_overlays):
    """MUTATION: build the new value from the overlay alone instead of appending.

    COMPOSE_FILE is ordered and cumulative: the base file defines the services, the
    overlays add to them. Replacing rather than appending would disarm GRC in the act of
    arming ICP -- and the operator's .env has carried armed-grc all day.
    """
    env_file_with_overlays.write_text(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.web.armed-grc.yml\n"
        "GRIDCOIN_WALLET_PASSPHRASE='must survive'\n"
    )
    already = configure_env.existing_assignments()

    value, why = configure_env.arm_verdict("icp", already)

    assert value == ("docker-compose.yml:docker-compose.web.armed-grc.yml"
                     ":docker-compose.web.armed-icp.yml"), value
    assert "armed-grc" in value, "arming ICP disarmed GRC"
    assert "ICP" in why
    # And writing it keeps every other line, which is the reason to go through write_env.
    configure_env.write_env({configure_env.COMPOSE_FILE_VARIABLE: value})
    written = env_file_with_overlays.read_text()
    assert "GRIDCOIN_WALLET_PASSPHRASE='must survive'" in written
    assert written.count("COMPOSE_FILE") == 1, "two COMPOSE_FILE lines; the effective value now "
    "depends on which parser reads it"


def test_arming_is_refused_when_the_overlays_own_variable_is_unset(env_file_with_overlays):
    """THE GUARD THAT MATTERS. MUTATION: drop the `variable and not already.get` branch.

    compose interpolates EVERY variable in EVERY file it is given BEFORE it looks at
    which service was named. So a COMPOSE_FILE naming armed-sol with no
    SOL_PAYOUT_KEYPAIR_HOST_FILE does not break the SOL payout -- it breaks EVERY docker
    compose command in the project, `up` and `ps` and `config` alike. That defect landed
    twice in one day on 2026-10-05.

    AND WRITING IT TO .env WOULD MAKE IT SURVIVE THE SHELL, which is worse than the
    original: the next command fails and nothing typed in that shell explains why.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    already = configure_env.existing_assignments()

    value, why = configure_env.arm_verdict("sol", already)

    assert value == "", "armed SOL with no keypair variable, breaking every compose command"
    assert "SOL_PAYOUT_KEYPAIR_HOST_FILE" in why
    assert "every docker compose command" in why.lower()
    # ICP takes no variable, so the same call for ICP must SUCCEED on the same .env --
    # which is what makes this a test of the guard rather than of refusing everything.
    assert configure_env.arm_verdict("icp", already)[0], "ICP needs no variable and was refused"


def test_arming_a_chain_that_is_already_armed_changes_nothing(env_file_with_overlays):
    """Rule 13: "skipped" and "success" must not share an output.

    A second --arm is the ordinary case -- an operator who cannot remember whether they
    ran it. Appending a second copy of the same overlay is what compose would then be
    handed, and the same file twice is a state the overlays' own headers warn about.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.web.armed-icp.yml\n")
    already = configure_env.existing_assignments()

    value, why = configure_env.arm_verdict("icp", already)

    assert value == "", "arming an armed chain produced a new value"
    assert "already armed" in why


def test_arming_an_unknown_chain_lists_the_ones_that_exist(env_file_with_overlays):
    """MUTATION: return a bare refusal with no list.

    "BTC has no arming overlay" leaves an operator guessing which chains do. The list is
    derived from the overlays on disk, so it cannot name one that was deleted.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    value, why = configure_env.arm_verdict("doge", configure_env.existing_assignments())

    assert value == ""
    for armed in ("GRC", "ICP", "SOL", "XRP"):
        assert armed in why, f"the refusal does not say {armed} can be armed: {why}"


def test_a_dry_run_arm_writes_nothing_and_says_so(env_file_with_overlays, monkeypatch, capsys):
    """The default is still to report. MUTATION: drop the `args.apply` guard on the write."""
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    before = env_file_with_overlays.read_text()
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--arm", "icp"])

    assert configure_env.main() == 0

    assert env_file_with_overlays.read_text() == before, "--arm without --apply modified .env"
    assert "Nothing was written" in capsys.readouterr().out


def test_an_apply_arm_writes_and_demands_a_recreate(env_file_with_overlays, monkeypatch, capsys):
    """End to end through main(). MUTATION: drop the recreate line from say_arm_result().

    AN ALREADY-RUNNING CONTAINER WAS CREATED FROM THE OLD COMPOSE_FILE. Writing .env
    changes which files the NEXT command reads and nothing about the process that is
    serving -- which the operator hit on this exact change: they recreated the container
    before the line was edited, so it came up unarmed, and nothing said why.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--arm", "icp", "--apply"])

    assert configure_env.main() == 0

    assert "armed-icp" in env_file_with_overlays.read_text()
    printed = capsys.readouterr().out
    assert "--force-recreate web" in printed, "the required next step was not named"
    assert "custody posture" in printed
    # And it does not claim the payout works, which it never has on any chain.
    assert "never been exercised" in printed


def test_a_refused_arm_exits_nonzero(env_file_with_overlays, monkeypatch, capsys):
    """MUTATION: `return 0` unconditionally.

    An exit code is what a script wrapping this reads. A refusal that exits 0 makes
    `configure_env.py --arm sol --apply && docker compose up -d web` bring up a stack
    whose compose files name a file whose variable is unset -- which fails on the next
    command, after the && has already succeeded.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--arm", "sol", "--apply"])

    assert configure_env.main() == 1
    assert "REFUSED" in capsys.readouterr().out
    assert "armed-sol" not in env_file_with_overlays.read_text(), "a refused arm wrote to .env anyway"


def test_compose_files_parses_a_quoted_and_an_unquoted_line(env_file_with_overlays):
    """write_env() single-quotes what it writes; a hand-edited line may not be.

    MUTATION: drop the .strip("'\\"").  Two readers of one file disagreeing about its
    syntax is rule 8's shape in the small, and the operator's .env has lines of both
    kinds in it right now -- COMPOSE_FILE unquoted from a hand edit,
    GRIDCOIN_WALLET_PASSPHRASE single-quoted by set_grc_passphrase.sh.
    """
    for line in ("COMPOSE_FILE=docker-compose.yml:a.yml\n",
                 "COMPOSE_FILE='docker-compose.yml:a.yml'\n"):
        env_file_with_overlays.write_text(line)
        parsed = configure_env.compose_files(configure_env.existing_assignments())
        assert parsed == ["docker-compose.yml", "a.yml"], f"{line!r} parsed as {parsed}"


# ===========================================================================
# DISARMING, the inverse --arm did not have
# ===========================================================================
#
# 2026-10-11. I armed ICP because its payout method is complete and its transport can
# sign -- both true. What I had not established is that anything CALLS it correctly:
# services/payout_service.broadcast_payout() passes send_to_address(address, amount)
# with two positional arguments, so created_at_time arrives as None and the adapter
# refuses before any ledger call. So arming made five `* -> ICP` directions offerable
# against a payout path that refuses every time, taking a customer's deposit first.
#
# The safe move is to stop offering them, and the only way to do that was to hand-edit
# the COMPOSE_FILE line in a .env holding GRIDCOIN_WALLET_PASSPHRASE -- the exact thing
# --arm exists to avoid. An operation with no inverse is one an operator cannot back out
# of safely.


def test_disarming_removes_only_that_overlay(env_file_with_overlays):
    """MUTATION: rebuild the value from the base file alone instead of filtering.

    COMPOSE_FILE is ordered and cumulative, so disarming ICP must leave GRC armed. A
    rebuild-from-base would silently disarm every other chain in the act of disarming
    one -- and GRC is the chain whose payout path actually works today.
    """
    env_file_with_overlays.write_text(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.web.armed-grc.yml"
        ":docker-compose.web.armed-icp.yml\n"
        "GRIDCOIN_WALLET_PASSPHRASE='must survive'\n"
    )
    already = configure_env.existing_assignments()

    value, why = configure_env.disarm_verdict("icp", already)

    assert value == "docker-compose.yml:docker-compose.web.armed-grc.yml", value
    assert "armed-grc" in value, "disarming ICP disarmed GRC"
    assert "armed-icp" not in value
    assert "ICP" in why and "disarmed" in why
    # And the write keeps every other line, which is the reason to go through write_env.
    configure_env.write_env({configure_env.COMPOSE_FILE_VARIABLE: value})
    written = env_file_with_overlays.read_text()
    assert "GRIDCOIN_WALLET_PASSPHRASE='must survive'" in written
    assert written.count("COMPOSE_FILE") == 1


def test_disarming_a_chain_that_is_not_armed_changes_nothing(env_file_with_overlays):
    """Rule 13: "skipped" and "success" must not share an output.

    A second --disarm is the ordinary case. It must not rewrite COMPOSE_FILE to the same
    value and report success, because an operator reading "disarmed" would then recreate
    a container for no reason -- and on the arming side that same confusion is what left
    them with an unarmed container after a recreate.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")

    value, why = configure_env.disarm_verdict("icp", configure_env.existing_assignments())

    assert value == "", "disarming an unarmed chain produced a new value"
    assert "not armed" in why
    assert "cannot sign" in why, "the refusal does not say what the current state IS"


def test_disarm_and_arm_round_trip_to_the_same_value(env_file_with_overlays):
    """MUTATION: make either verdict rebuild the list rather than add or remove one entry.

    THE INVERSE PROPERTY IS THE WHOLE POINT and it is the one thing neither verdict can
    establish alone: arming then disarming must land exactly where it started, or an
    operator backing out of a posture change gets a COMPOSE_FILE that is almost right.
    Asserted on the string, because that string is what compose reads.
    """
    start = "docker-compose.yml:docker-compose.web.armed-grc.yml"
    env_file_with_overlays.write_text(f"COMPOSE_FILE={start}\n")

    armed, _why = configure_env.arm_verdict("icp", configure_env.existing_assignments())
    assert armed.endswith(":docker-compose.web.armed-icp.yml")

    back, _why = configure_env.disarm_verdict(
        "icp", {configure_env.COMPOSE_FILE_VARIABLE: armed})

    assert back == start, f"the round trip did not return to {start}: {back}"


def test_an_apply_disarm_says_the_running_container_is_STILL_ARMED(
        env_file_with_overlays, monkeypatch, capsys):
    """MUTATION: drop the recreate block from say_disarm_result().

    FORGETTING THE RECREATE IS WORSE ON THE WAY OUT THAN ON THE WAY IN. Forget it after
    --arm and the chain cannot pay, which shows up as a NONE lamp. Forget it after
    --disarm and the container still holds the mounted credential and can still sign --
    which looks exactly like success. So the output has to say the running process is
    unchanged, not merely that .env was written.
    """
    env_file_with_overlays.write_text(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.web.armed-icp.yml\n")
    monkeypatch.setattr("sys.argv", ["configure_env.py", "--disarm", "icp", "--apply"])

    assert configure_env.main() == 0

    written = env_file_with_overlays.read_text()
    assert "armed-icp" not in written
    printed = capsys.readouterr().out
    assert "STILL ARMED IN THE RUNNING CONTAINER" in printed
    assert "--force-recreate web" in printed
    assert "REQUIRED" in printed


def test_arming_and_disarming_in_one_command_is_refused(env_file_with_overlays, monkeypatch, capsys):
    """MUTATION: drop the both-flags branch.

    They are opposite acts on one line of .env. Whichever order a tool picked would be
    right half the time and unexplained both times, so it picks neither and writes
    nothing. Asserted on .env being byte-unchanged, not just on the exit code.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    before = env_file_with_overlays.read_text()
    monkeypatch.setattr(
        "sys.argv", ["configure_env.py", "--arm", "icp", "--disarm", "grc", "--apply"])

    assert configure_env.main() == 1
    assert env_file_with_overlays.read_text() == before, "a refused command wrote to .env"
    assert "nothing was done" in capsys.readouterr().out


def test_neither_posture_flag_lets_the_run_carry_on(env_file_with_overlays, monkeypatch, capsys):
    """change_posture() returns None, not 0, and the difference is load-bearing.

    MUTATION: return 0 instead of None. `--apply` alone would then stop before
    discovering or asking for anything, and report success for having done nothing --
    rule 14's "make 'did nothing' look different from 'did work'" failing in the
    direction that looks like work.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr("sys.argv", ["configure_env.py"])

    assert configure_env.main() == 0
    printed = capsys.readouterr().out
    assert "DISCOVERING" in printed, "the run stopped before discovery"
    assert "DISARMING" not in printed and "ARMING " not in printed


def test_the_keypair_PATH_is_askable_and_the_secrets_are_not():
    """MUTATION: restore the `# SECRET` marker on SOL_PAYOUT_KEYPAIR_HOST_FILE.

    THE DISTINCTION IS VALUE VERSUS LOCATION. GRIDCOIN_WALLET_PASSPHRASE and
    XRP_PAYOUT_SECRET_SEED ARE the credential. SOL_PAYOUT_KEYPAIR_HOST_FILE names a FILE,
    and the credential is its contents -- which the overlay mounts read-only and which
    chains/solana_payout_keypair.py exists specifically never to open.

    THE MARKER IS WHAT MAKES A PROMPT NO-ECHO, so marking a path SECRET cost twice and
    bought nothing: configure_env.py would not ask for it at all, so arming SOL meant
    hand-editing the file that holds GRIDCOIN_WALLET_PASSPHRASE -- the hand edit
    write_env() was written to make unnecessary -- and had it asked, the answer would have
    been typed blind, where a mistyped path is invisible until a payout fails at the send.
    """
    secrets = configure_env.secret_names()
    asked = {name for name, _what in configure_env.ASKED}

    assert "SOL_PAYOUT_KEYPAIR_HOST_FILE" in asked, (
        "the keypair PATH is not askable, so arming SOL requires hand-editing .env"
    )
    assert "SOL_PAYOUT_KEYPAIR_HOST_FILE" not in secrets, (
        "the keypair path is marked SECRET, so its prompt would be no-echo and a typo invisible"
    )
    # The two that ARE the credential stay out of reach, both ways.
    for credential in ("GRIDCOIN_WALLET_PASSPHRASE", "XRP_PAYOUT_SECRET_SEED"):
        assert credential not in asked, f"{credential} is the credential and must never be asked for"


def test_the_arming_block_separates_what_it_can_set_from_what_it_will_not(capsys):
    """MUTATION: restore "none of these is settable here: each names a signing credential".

    That sentence was true of two of the four and is what kept the askable one out of
    ASKED. An operator reading it goes to edit .env by hand for a path -- which is the
    thing this tool exists to prevent, recommended by the tool itself.
    """
    configure_env.say_arming({})
    printed = capsys.readouterr().out

    assert "CAN set" in printed
    assert "SOL_PAYOUT_KEYPAIR_HOST_FILE" in printed.split("CAN set", 1)[1]
    assert "never set" in printed
    withheld = printed.split("never set", 1)[1]
    assert "GRIDCOIN_WALLET_PASSPHRASE" in withheld and "XRP_PAYOUT_SECRET_SEED" in withheld
    assert "none of these" not in printed, "the sentence that called all four credentials is back"


# ===========================================================================
# --set, because an interactive prompt is the wrong shape for a non-secret
# ===========================================================================
#
# 2026-10-11. I spent the afternoon telling the operator to press enter past every
# prompt, because for three BTC variables there was nothing to type. Then two prompts
# appeared that DID need answers -- SOL_HOT_WALLET and SOL_PAYOUT_KEYPAIR_HOST_FILE,
# both of which they already had -- and five enters went past all five. The run
# reported NOTHING TO WRITE, correctly, and SOL stayed unconfigured.
#
# A prompt cannot be pasted as part of a command, re-run from shell history, or kept in
# a note, so every attempt is a fresh chance to skip or mistype. getpass() earns that
# cost for a credential. A public key and a file path do not.


def test_a_non_secret_can_be_set_without_a_prompt(env_file_with_overlays, monkeypatch, capsys):
    """MUTATION: drop the `args.assignments` branch from main().

    The whole point: one command, re-runnable, visible in shell history, no tty.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr("sys.argv", [
        "configure_env.py", "--set", "SOL_HOT_WALLET=SoLHotWalletPublicKey",
        "--set", "SOL_PAYOUT_KEYPAIR_HOST_FILE=/home/somebody/.config/solana/hot.json", "--apply"])

    assert configure_env.main() == 0

    written = env_file_with_overlays.read_text()
    assert "SOL_HOT_WALLET='SoLHotWalletPublicKey'" in written
    assert "SOL_PAYOUT_KEYPAIR_HOST_FILE='/home/somebody/.config/solana/hot.json'" in written
    assert "COMPOSE_FILE=docker-compose.yml" in written, "a line it does not own was lost"
    # And the values ARE echoed, which is safe only because a SECRET name is refused
    # upstream -- it is the only way an operator sees a typo in a path before a recreate.
    assert "/home/somebody/.config/solana/hot.json" in capsys.readouterr().out


def test_a_SECRET_name_is_refused_because_argv_is_world_readable(env_file_with_overlays):
    """THE GUARD THAT MATTERS. MUTATION: drop the `name in secrets` branch.

    /proc/<pid>/cmdline is readable by every process on the host for as long as the
    command runs, which is exactly why set_grc_passphrase.sh prompts instead of taking
    an argument. Allowing --set for a passphrase would undo that, by a route that looks
    like a convenience.
    """
    secrets = configure_env.secret_names()
    assert "GRIDCOIN_WALLET_PASSPHRASE" in secrets, "the fixture no longer marks it secret"

    values, refusals = configure_env.set_verdict(
        ["GRIDCOIN_WALLET_PASSPHRASE=hunter2", "SOL_HOT_WALLET=ok"], secrets)

    assert "GRIDCOIN_WALLET_PASSPHRASE" not in values, "a passphrase was accepted on argv"
    assert values == {"SOL_HOT_WALLET": "ok"}, "the non-secret in the same call was dropped too"
    assert any("/proc" in line for line in refusals), f"the refusal does not say why: {refusals}"
    # And the refusal must not echo the value it refused.
    assert not any("hunter2" in line for line in refusals), "the refused secret was printed"


def test_an_unmanaged_name_is_refused_with_a_suggestion(env_file_with_overlays):
    """MUTATION: drop the `name not in manageable` branch.

    `--set SOL_HOTWALLET=...` would otherwise write a line nothing reads, leaving an
    operator looking at a correctly-written .env and an unconfigured chain -- the
    silent-absence defect this whole tool exists for, arriving through its own door.
    """
    values, refusals = configure_env.set_verdict(
        ["SOL_HOTWALLET=typo"], configure_env.secret_names())

    assert values == {}
    assert any("not a variable this tool manages" in line for line in refusals)
    assert any("SOL_HOT_WALLET" in line for line in refusals), (
        f"the refusal suggests no real name: {refusals}"
    )


@pytest.mark.parametrize(("given", "expected"), [
    ("SOL_HOT_WALLET", "is not NAME=VALUE"),
    ("SOL_HOT_WALLET=", "was given no value"),
    ("SOL_HOT_WALLET=it's", "single quote"),
])
def test_the_malformed_shapes_are_each_refused_by_name(given, expected):
    """One refusal per shape, each saying which. MUTATION: collapse them into one message.

    The single-quote case is the one with a cost beyond the retype: a value stored mangled
    does not fail here, it fails later as a path or authentication error nobody connects
    to this step -- which is why set_grc_passphrase.sh refuses one too.
    """
    values, refusals = configure_env.set_verdict([given], configure_env.secret_names())

    assert values == {}
    assert len(refusals) == 1
    assert expected in refusals[0], f"{given!r} produced {refusals[0]!r}"


def test_a_refused_set_exits_nonzero_even_when_another_value_was_written(
        env_file_with_overlays, monkeypatch, capsys):
    """Rule 13: "skipped" and "success" must not share an output -- or an exit code.

    MUTATION: `return 0` unconditionally. A script doing `--set A --set B --apply && next`
    would then proceed having written only one of the two, with the refusal scrolled past.
    """
    env_file_with_overlays.write_text("COMPOSE_FILE=docker-compose.yml\n")
    monkeypatch.setattr(configure_env, "listening_ports", set)
    monkeypatch.setattr("sys.argv", [
        "configure_env.py", "--set", "SOL_HOT_WALLET=good",
        "--set", "SOL_NONSENSE=bad", "--apply"])

    assert configure_env.main() == 1, "a partially refused run reported success"
    written = env_file_with_overlays.read_text()
    assert "SOL_HOT_WALLET='good'" in written, "the acceptable value was not written"
    assert "SOL_NONSENSE" not in written
    assert "REFUSED" in capsys.readouterr().out
