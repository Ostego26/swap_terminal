"""Emptying a seed-derived funding address, and the two refusals that make it safe.

Role: tests (offline; no daemon, no network)
Reads: reclaim_funding.py, regtest/adaptor_steps.py, modules/network_selection.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- nothing here opens a socket
Live-safe: yes

MEASURED ON THE OPERATOR'S HOST, 2026-09-28: two addresses funded from two different seeds
across one evening, holding 3.499 and 4.60 GRC, and nothing in this tree could reach either.
The wallet does not own them, `importaddress` is False on Gridcoin v5.5.1.0 and `gettxout` is
False too -- so the coins were invisible to the harness and to the wallet alike. The seeds were
in the shell history, which is the only reason recovery is possible at all.

The two properties worth pinning are not "it builds a transaction". They are:

  --send is SEPARATE      everything but the broadcast runs by default, so the operator reads
                          the destination, the amount and the fee before anything moves.
  the network is CHECKED  the destination's version byte is compared against the configured
                          network before a byte is built. Paying an address is exactly where a
                          network mismatch costs coins.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import base58
import pytest
from conftest import RPC_FIXTURE_AUTH, RPC_FIXTURE_USER
from modules import adaptor_swap_chain as chain
from modules import network_selection
from regtest import adaptor_steps
from regtest.console import Console
from regtest.daemons import ChainConfig, RegtestSetupError
from regtest.keys import generate_key


def _entry():
    spec = importlib.util.spec_from_file_location(
        "reclaim_funding_under_test", Path(__file__).resolve().parents[1] / "reclaim_funding.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch, node=None) -> tuple[adaptor_steps.Run, io.StringIO]:
    stream = io.StringIO()
    run = adaptor_steps.Run(
        console=Console(adaptor_steps.TOTAL_STEPS, stream=stream),
        config=ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid",
        ),
        wallet="",
    )
    if node is not None:
        monkeypatch.setattr(adaptor_steps, "adapter_for", lambda config, wallet="": node)
    return run, stream


# ---------------------------------------------------------------------------
# The destination's network is checked BEFORE anything is built.
# ---------------------------------------------------------------------------


def test_a_testnet_address_gives_the_p2pkh_script_that_pays_it():
    key = generate_key()
    assert adaptor_steps.p2pkh_script_for_address("GRC", key.address) == key.p2pkh_script


def test_a_MAINNET_address_is_REFUSED_while_this_engine_is_on_testnet():
    """THE REFUSAL THAT MATTERS. Paying an address is exactly where a network mismatch costs
    coins, and the two directions are both losses: test coins to a mainnet address are gone to
    somewhere nobody can pay from, and real coins to a testnet address likewise.

    Built here by re-encoding a real testnet hash160 under GRC's MAINNET version byte, taken
    from the authority rather than written as 0x3E -- so this test cannot disagree with the
    module it is testing about what mainnet's byte is.
    """
    key = generate_key()
    mainnet_byte = network_selection.base58_version("GRC", network_selection.P2PKH, network_selection.MAINNET)
    mainnet_address = base58.b58encode_check(mainnet_byte + key.hash160).decode()

    with pytest.raises(RegtestSetupError) as raised:
        adaptor_steps.p2pkh_script_for_address("GRC", mainnet_address)

    message = str(raised.value)
    assert "other network" in message, "it has to say what paying it would do, not just refuse"
    assert "Nothing was built, signed or broadcast" in message


@pytest.mark.parametrize("bad", ["not base58 at all", "", "1111111111111111111114oLvT2"])
def test_a_destination_that_is_not_a_P2PKH_address_is_refused(bad):
    """P2SH and bech32 are refused rather than half-handled: this exists to return a stranded
    output to an ordinary wallet address, and a reclaim tool that silently built the wrong
    script for a fancier destination would strand it again somewhere harder to reach."""
    with pytest.raises(RegtestSetupError):
        adaptor_steps.p2pkh_script_for_address("GRC", bad)


# ---------------------------------------------------------------------------
# Building the spend.
# ---------------------------------------------------------------------------


def _source(value: int = 350_000_000) -> chain.Outpoint:
    return chain.Outpoint(txid="ab" * 32, vout=1, value_satoshis=value)


def test_the_whole_output_goes_to_the_destination_less_the_fee(monkeypatch):
    """One input, one output, NO CHANGE. The point is to empty the address, and a change output
    returning to it would leave a second outpoint there to be stranded again."""
    run, _stream = _run(monkeypatch)
    key, destination = generate_key(), generate_key()
    source = _source()

    raw, predicted, value = adaptor_steps.reclaim_p2pkh(run, key, source, destination.address)

    assert 0 < value < source.value_satoshis, "the fee comes out of it, and something is left"
    assert len(predicted) == 64
    assert raw, "signed bytes, ready to broadcast and not broadcast"


def test_an_output_too_small_to_cover_its_own_fee_is_refused_by_name(monkeypatch):
    """Rather than building a transaction with a zero or negative output, which the daemon would
    refuse with a message about amounts that says nothing about why."""
    run, _stream = _run(monkeypatch)
    key, destination = generate_key(), generate_key()

    with pytest.raises(RegtestSetupError) as raised:
        adaptor_steps.reclaim_p2pkh(run, key, _source(value=1), destination.address)
    assert "cannot be emptied" in str(raised.value)


# ---------------------------------------------------------------------------
# --send IS SEPARATE. The default builds everything and moves nothing.
# ---------------------------------------------------------------------------


class _Node:
    """A daemon that refuses to be asked for a broadcast, so the default path can be asserted."""

    #: A CHAIN WITH NOTHING IN IT, served to every stub that does not override it. The dry run
    #: now WALKS BLOCKS to decide whether the output is still there -- Gridcoin answers
    #: `testmempoolaccept: code=-32601 message=Method not found`, measured 2026-09-28, so the
    #: check this tool used to rely on had never once executed. A stub serving no blocks is the
    #: "nothing spent it, carry on" case, which is what most of these tests are about.
    AN_EMPTY_CHAIN = {  # noqa: RUF012 -- a fixture table, read-only, never mutated
        "getblockcount": 0,
        "getblockhash": lambda height, *_: f"hash-of-{height}",
        "getblock": lambda *_: {"tx": []},
    }

    def __init__(self, answers: dict) -> None:
        self.answers, self.sent = {**self.AN_EMPTY_CHAIN, **answers}, []

    def call(self, method, *params):
        if method == "sendrawtransaction":
            self.sent.append(params[0])
            raise AssertionError("sendrawtransaction must NOT be reached without --send")
        if method in self.answers:
            answer = self.answers[method]
            return answer(*params) if callable(answer) else answer
        raise RPCErrorStub(f"{method}: no answer configured")


class RPCErrorStub(adaptor_steps.RPCError):
    """An unconfigured method, raised as the DAEMON would raise it.

    Subclassing the real `RPCError` rather than `Exception` on 2026-09-28, because the code
    under test catches `RPCError` to mean "the daemon would not answer that" -- which is the
    ordinary case for `testmempoolaccept` on Gridcoin, a method it does not have. A stub raising
    some other type escapes that handler, so the test exercises a path no daemon can produce and
    passes or fails for reasons unrelated to the code.
    """


def test_without_send_the_transaction_is_BUILT_AND_SIGNED_and_never_broadcast(monkeypatch):
    """THE PROPERTY THAT MAKES THIS SAFE TO RUN. Everything except the broadcast happens by
    default, so the operator reads the destination, the amount and the fee BEFORE anything
    moves -- and the transaction they then approve is the one that goes.

    The stub RAISES on sendrawtransaction rather than counting it, so what is asserted is that
    the call never happens, not that a counter stayed at zero.
    """
    entry = _entry()
    key, destination = generate_key(), generate_key()
    node = _Node({"testmempoolaccept": [{"allowed": True}]})
    monkeypatch.setattr(entry.adaptor_steps, "resolve_config", lambda asset: ChainConfig(
        asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
        host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
        conf_name="c.conf", pid_name="c.pid",
    ))
    monkeypatch.setattr(entry.adaptor_steps, "step_1_reachable", lambda run: None)
    monkeypatch.setattr(entry.adaptor_steps, "assert_test_network", lambda run: None)
    monkeypatch.setattr(entry.adaptor_steps, "operator_funding_key", lambda run: key)
    monkeypatch.setattr(entry.adaptor_steps, "discover_operator_funding_txid", lambda run, k: "cd" * 32)
    monkeypatch.setattr(entry.adaptor_steps, "find_operator_funding", lambda run, k, txid: _source())
    monkeypatch.setattr(entry.adaptor_steps, "adapter_for", lambda config, wallet="": node)

    stream = io.StringIO()
    code = entry.main(["--to", destination.address, "--chain", "grc"], Console(entry.TOTAL_STEPS, stream=stream))

    assert code == 0
    assert node.sent == [], "nothing may reach the daemon without --send"
    printed = stream.getvalue()
    assert "NOTHING WAS BROADCAST" in printed
    assert "--send" in printed, "and it says how to actually move it"
    assert destination.address in printed, "and names where it would go, before it goes"


def test_an_unset_seed_says_where_the_seed_usually_is(monkeypatch):
    """The refusal has to name the recovery route, or it is the dead end this whole tool exists
    because of: the operator's shell history is the only place a lost seed actually is."""
    entry = _entry()
    monkeypatch.setattr(entry.adaptor_steps, "resolve_config", lambda asset: ChainConfig(
        asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
        host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
        conf_name="c.conf", pid_name="c.pid",
    ))
    monkeypatch.setattr(entry.adaptor_steps, "step_1_reachable", lambda run: None)
    monkeypatch.setattr(entry.adaptor_steps, "assert_test_network", lambda run: None)
    monkeypatch.setattr(entry.adaptor_steps, "operator_funding_key", lambda run: None)

    stream = io.StringIO()
    assert entry.main(["--to", generate_key().address], Console(entry.TOTAL_STEPS, stream=stream)) == 1
    printed = stream.getvalue()
    assert "history | grep" in printed
    assert "Nothing was built, signed or broadcast" in printed


def test_the_seed_is_never_an_argument(monkeypatch):
    """A value on the command line is world-readable through /proc and `ps`, and lands in the
    shell history of whoever runs it. The seed derives a key that controls coins."""
    entry = _entry()
    with pytest.raises(SystemExit):
        entry.parse_args(["--to", "x", "--seed", "anything"])
    assert "seed" not in vars(entry.parse_args(["--to", "x"])), (
        "no argument may carry the seed. It goes in the environment or nowhere"
    )


# ---------------------------------------------------------------------------
# --to-wallet: "i don't know what the wallet address was", and you should not have to.
# ---------------------------------------------------------------------------


def test_an_address_the_wallet_already_owns_is_found_from_listunspent(monkeypatch):
    """listunspent RATHER THAN getnewaddress, and that is the whole point.

    The operator's wallet is unlocked FOR STAKING ONLY. `getnewaddress` writes a new key into
    the wallet, which a locked or staking-only wallet may refuse -- and whether Gridcoin
    v5.5.1.0 refuses it is NOT established anywhere, so this does not find out the hard way.
    listunspent only reads, and every address it returns is one the wallet demonstrably
    controls, because it is holding coins at it.
    """
    owned = generate_key().address

    class _Wallet:
        def call(self, method, *params):
            assert method == "listunspent", (
                f"{method} must not be reached: getnewaddress would write a key into a wallet "
                f"that may be unlocked for staking only"
            )
            return [{"txid": "ab" * 32, "vout": 0, "amount": 1.0, "address": owned}]

    run, _stream = _run(monkeypatch, _Wallet())
    assert adaptor_steps.wallet_owned_address(run) == owned


def test_a_wallet_with_no_unspent_outputs_says_so_rather_than_paying_nowhere(monkeypatch):
    class _Empty:
        def call(self, method, *params):
            return []

    run, _stream = _run(monkeypatch, _Empty())
    with pytest.raises(RegtestSetupError) as raised:
        adaptor_steps.wallet_owned_address(run)
    assert "--to" in str(raised.value), "and it names the way round it"


def test_rows_without_an_address_are_skipped_rather_than_paying_an_empty_string(monkeypatch):
    """A row with no `address` field is not an address. Paying "" would be refused downstream by
    p2pkh_script_for_address, but four frames from the cause and as a base58 error."""
    owned = generate_key().address

    class _Mixed:
        def call(self, method, *params):
            return [{"txid": "ab" * 32, "vout": 0}, {"address": ""}, {"address": owned}]

    run, _stream = _run(monkeypatch, _Mixed())
    assert adaptor_steps.wallet_owned_address(run) == owned


def test_to_and_to_wallet_are_mutually_exclusive_and_one_is_required():
    """A destination is never defaulted. Two ways to name one, and naming none is an error --
    a reclaim tool that guessed where to send would be the defect it exists to prevent."""
    entry = _entry()
    for argv in ([], ["--to", "x", "--to-wallet"]):
        with pytest.raises(SystemExit):
            entry.parse_args(argv)
    assert entry.parse_args(["--to-wallet"]).to is None
    assert entry.parse_args(["--to", "x"]).to_wallet is False


def test_the_dry_run_tells_you_to_re_run_with_the_EXACT_address_it_used(monkeypatch):
    """--to-wallet picks from listunspent, and that set changes as coins move. So the line the
    operator copies must name the address the dry run ACTUALLY used -- otherwise the --send run
    could pay a different one than the one they just read and approved."""
    entry = _entry()
    key, owned = generate_key(), generate_key().address
    node = _Node({"listunspent": [{"address": owned, "txid": "ab" * 32, "vout": 0}],
                  "testmempoolaccept": [{"allowed": True}]})
    for name, value in (
        ("resolve_config", lambda asset: ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid")),
        ("step_1_reachable", lambda run: None),
        ("assert_test_network", lambda run: None),
        ("operator_funding_key", lambda run: key),
        ("discover_operator_funding_txid", lambda run, k: "cd" * 32),
        ("find_operator_funding", lambda run, k, txid: _source()),
        ("adapter_for", lambda config, wallet="": node),
    ):
        monkeypatch.setattr(entry.adaptor_steps, name, value)

    stream = io.StringIO()
    assert entry.main(["--to-wallet", "--chain", "grc"], Console(entry.TOTAL_STEPS, stream=stream)) == 0

    printed = stream.getvalue()
    assert f"--to {owned}" in printed, (
        "the re-run line must carry the address actually used, not --to-wallet again"
    )
    assert node.sent == []


def test_no_step_number_is_printed_twice(monkeypatch):
    """THE FIRST REAL RUN PRINTED TWO `step 1/5` LINES AND TWO `step 2/5` LINES.

    `step_1_reachable` and `assert_test_network` are borrowed whole from
    adaptor_regtest_verify and print their OWN headers, and this file printed its own on top.
    A numbering that repeats is worse than none: the operator reads "step 2/5", sees another
    "step 2/5", and has to work out whether something re-ran.

    Asserted over the REAL step headers rather than by counting console.step() calls, because
    the duplicates came from a function this file calls, not from a call it makes.
    """
    entry = _entry()
    key, owned = generate_key(), generate_key().address
    node = _Node({
        "listunspent": [{"address": owned, "txid": "ab" * 32, "vout": 0}],
        "getblockcount": 100,
        "getblockchaininfo": {"testnet": True},
        "testmempoolaccept": [{"allowed": True}],
    })
    for name, value in (
        ("resolve_config", lambda asset: ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid")),
        ("operator_funding_key", lambda run: key),
        ("discover_operator_funding_txid", lambda run, k: "cd" * 32),
        ("find_operator_funding", lambda run, k, txid: _source()),
        ("adapter_for", lambda config, wallet="": node),
    ):
        monkeypatch.setattr(entry.adaptor_steps, name, value)

    stream = io.StringIO()
    entry.main(["--to-wallet", "--chain", "grc"], Console(entry.TOTAL_STEPS, stream=stream))

    numbers = [line.split()[1] for line in stream.getvalue().splitlines() if line.startswith("step ")]
    assert numbers == sorted(numbers, key=lambda n: int(n.split("/")[0])), "in order"
    assert len(numbers) == len(set(numbers)), f"each step number printed once; got {numbers}"
    assert all(n.endswith(f"/{entry.TOTAL_STEPS}") for n in numbers), (
        f"and the denominator must be the real total, not a stale one: {numbers}"
    )


def test_a_dry_run_over_an_ALREADY_SPENT_output_refuses_instead_of_looking_healthy(monkeypatch):
    """THE LIE THE DRY RUN USED TO TELL, measured on the operator's host 2026-09-28.

    They pointed this at a seed whose funding a completed harness run had already SPLIT AND
    SPENT, and the dry run reported `built and signed: 4.59000000 to ...` as if nothing were
    wrong. `find_operator_funding` reads the vout out of the FUNDING TRANSACTION, and a
    transaction's outputs do not stop existing when they are spent -- so it cannot tell.
    Gridcoin has no `gettxout`, which is the call that would normally answer.

    A dry run whose entire purpose is "see it before it moves" must not show a healthy-looking
    spend of an output that is gone. testmempoolaccept runs the same AcceptToMemoryPool without
    broadcasting, so the question costs nothing -- and it is asked HERE rather than discovered
    by --send.
    """
    entry = _entry()
    key, owned = generate_key(), generate_key().address
    node = _Node({
        "listunspent": [{"address": owned, "txid": "ab" * 32, "vout": 0}],
        "testmempoolaccept": [{"allowed": False, "reject-reason": "bad-txns-inputs-missingorspent"}],
    })
    for name, value in (
        ("resolve_config", lambda asset: ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid")),
        ("step_1_reachable", lambda run: None),
        ("assert_test_network", lambda run: None),
        ("operator_funding_key", lambda run: key),
        ("discover_operator_funding_txid", lambda run, k: "cd" * 32),
        ("find_operator_funding", lambda run, k, txid: _source()),
        ("adapter_for", lambda config, wallet="": node),
    ):
        monkeypatch.setattr(entry.adaptor_steps, name, value)

    stream = io.StringIO()
    code = entry.main(["--to-wallet", "--chain", "grc"], Console(entry.TOTAL_STEPS, stream=stream))

    assert code == 1, "a dry run that cannot succeed must not exit 0"
    printed = stream.getvalue()
    assert "bad-txns-inputs-missingorspent" in printed, "the daemon's own words"
    assert "ALREADY SPENT" in printed
    assert "NOTHING WILL BE" in printed, (
        "and it says so in its own words -- a run that CANNOT proceed must not read like one "
        "that is merely waiting to be told to (rule 13)"
    )
    assert "Re-run with --send" not in printed, (
        "and it must NOT go on to offer a --send line for a transaction that cannot be accepted"
    )
    assert node.sent == []


def test_the_dry_run_WALKS_THE_CHAIN_when_the_daemon_has_no_testmempoolaccept(monkeypatch):
    """THE CASE THAT ACTUALLY HAPPENS, and until 2026-09-28 the check for it had never run.

    The test above hands the daemon a testmempoolaccept answer. The operator's Gridcoin has no
    such method -- `code=-32601 Method not found`, measured -- so the guard written to stop a
    dry run looking healthy over a spent output returned "the daemon will not say" every single
    time, and this tool read that as acceptance. A check that cannot execute is not a weaker
    check, it is the absence of one, and it had been reporting as passing for a day.

    `getblock(hash, true)` carries every transaction's inputs, so walking blocks answers the
    same question with only calls this daemon actually has.
    """
    entry = _entry()
    key, owned = generate_key(), generate_key().address
    source = _source()
    node = _Node({
        "listunspent": [{"address": owned, "txid": "ab" * 32, "vout": 0}],
        "getblockcount": 4,
        "getblock": lambda block_hash, *_: (
            {"tx": [{"txid": "what-consumed-it",
                     "vin": [{"txid": source.txid, "vout": source.vout}]}]}
            if block_hash.endswith("-3") else {"tx": [{"txid": "cb", "vin": [{"coinbase": "00"}]}]}
        ),
    })
    for name, value in (
        ("resolve_config", lambda asset: ChainConfig(
            asset="GRC", daemon_path="x", cli_path="y", datadir=Path("/nonexistent"),
            host="127.0.0.1", port=1, rpc_user=RPC_FIXTURE_USER, rpc_password=RPC_FIXTURE_AUTH,
            conf_name="c.conf", pid_name="c.pid")),
        ("step_1_reachable", lambda run: None),
        ("assert_test_network", lambda run: None),
        ("operator_funding_key", lambda run: key),
        ("discover_operator_funding_txid", lambda run, k: "cd" * 32),
        ("find_operator_funding", lambda run, k, txid: source),
        ("adapter_for", lambda config, wallet="": node),
    ):
        monkeypatch.setattr(entry.adaptor_steps, name, value)

    stream = io.StringIO()
    code = entry.main(["--to-wallet", "--chain", "grc"], Console(entry.TOTAL_STEPS, stream=stream))

    assert code == 1, "a dry run over a spent output must not exit 0"
    printed = stream.getvalue()
    assert "the call failed" in printed and "testmempoolaccept" in printed, (
        "it says WHY the chain had to be walked, naming the method that could not answer"
    )
    assert "what-consumed-it" in printed, "and names the transaction that took it"
    assert "ALREADY SPENT" in printed
    assert "Re-run with --send" not in printed
    assert node.sent == []
