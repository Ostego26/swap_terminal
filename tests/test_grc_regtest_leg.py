"""The Gridcoin regtest leg of regtest_htlc_verify.py, pinned without a daemon.

Role: test (behavioral, no chain)
Reads: regtest_htlc_verify.py, swap_terminal/regtest/{daemons,steps,txbuild}.py,
       chains/gridcoin_genesis.py, modules/htlc_spend.py (as an independent parser)
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes -- no socket is opened; every RPC is answered by a recording stub
Live-safe: yes

=========================================================================
WHAT IS PINNED HERE, AND WHAT ESTABLISHED IT
=========================================================================

Every behavior below was first MEASURED on 2026-11-23 against Gridcoin Core
v5.5.1.7-30a89f2ab0bb on regtest, by running `regtest_htlc_verify.py --chain grc --wipe`
on a real node. These tests do not re-prove any of those measurements -- a stub cannot --
they pin the harness's RESPONSE to each one, so a later edit that quietly undoes the
response fails here instead of on the next person's chain:

  the network gate     getblockchaininfo has no `chain`, and `testnet` reads False on regtest
                       exactly as on mainnet -> the genesis hash decides, regtest ONLY
  the wipe             the conf lives INSIDE regtest/ -> --wipe keeps it, or the next start
                       writes a random password
  stake mining         bare generatetoaddress stalls at ~25 -> +3600s mock clock per
                       10-block batch, 500 of 500 blocks
  the clock            a daemon starts on the wall clock, behind its tip -> "Failed to select
                       coins" until the clock is put past the tip
  the layout           nVersion nTime vin vout nLockTime vContracts(0x00) -> the control
                       spender serializes AND SIGNS that layout
  two absent RPC forms no gettxout; sendrawtransaction takes ONE argument
  standardness         every HTLC spend refused `bad-txns-nonstandard-inputs` before any
                       script ran -> never scored as a timelock refusal

The last one is the run's finding rather than a harness detail, and it is why the leg
exits 1 on an unmodified Gridcoin regtest node: see docs/branch_coverage.md.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest
from ecdsa import BadSignatureError, SECP256k1, VerifyingKey
from ecdsa.util import sigdecode_der

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.base import RPCError
from chains.daemon_launch import MAINNET_DATADIR_SEGMENTS
from chains.gridcoin_genesis import GRIDCOIN_GENESIS
from modules.htlc_spend import parse_transaction
from regtest import daemons, steps
from regtest.console import FAIL, OK, Console
from regtest.daemons import CHAIN_DEFAULTS, RegtestSetupError
from regtest.keys import generate_key
from regtest.txbuild import Outpoint, build_branch_spend, legacy_sighash, serialize_transaction

GENESIS_BY_NETWORK = {network: digest for digest, network in GRIDCOIN_GENESIS.items()}

#: A placeholder where a ChainConfig wants a credential; nothing here reaches a daemon.
NOT_A_CREDENTIAL = "placeholder"


class _Stub:
    """Answers RPCs from a dict and records every call, in order."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    def call(self, method, *params):
        self.calls.append((method, params))
        answer = self.answers.get(method)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(*params)
        if answer is None:
            raise RPCError(f"{method}: no canned answer")
        return answer


def _grc_config(datadir: Path) -> daemons.ChainConfig:
    """The real GRC row, pointed at a test datadir."""
    config = daemons.resolve_chain_config("GRC")
    config.datadir = datadir
    return config


def _run(config, wallet: str = "") -> steps.Run:
    return steps.Run(console=Console(total_steps=steps.TOTAL_STEPS), config=config, wallet=wallet)


def _use(monkeypatch, stub: _Stub) -> None:
    """Every adapter the harness builds is this stub."""
    monkeypatch.setattr(daemons, "adapter_for", lambda config, wallet="": stub)


# ---------------------------------------------------------------------------
# the GRC row: its own table, and CHAIN_DEFAULTS unchanged
# ---------------------------------------------------------------------------

def test_GRC_resolves_from_its_own_table_and_not_from_CHAIN_DEFAULTS():
    """CHAIN_DEFAULTS' other four readers assume the conf sits at the datadir root."""
    config = daemons.resolve_chain_config("GRC")

    assert "GRC" not in CHAIN_DEFAULTS, "a GRC row there hands four modules a conf path that holds nothing"
    assert config.port == 35715, "Gridcoin's own regtest RPC port, src/chainparamsbase.cpp"
    assert config.conf_path == config.datadir / "regtest" / "gridcoinresearch.conf"
    assert "-devbuild=override" in config.spawn_args, "without it the wallet refuses every send"
    assert "-staking=0" in config.spawn_args, "without it the tip moves under the locktime test"
    assert config.has_multiwallet is False
    assert config.mines_by_stake and config.tx_carries_ntime and config.identifies_network_by_genesis


def test_the_Core_rows_carry_none_of_the_Gridcoin_behavior():
    """Every new field defaults to Core's behavior, so BTC and LTC are untouched by them."""
    for asset in ("BTC", "LTC"):
        config = daemons.resolve_chain_config(asset)
        assert config.conf_path == config.datadir / config.conf_name
        assert config.spawn_args == ()
        assert config.has_multiwallet is True
        assert not (config.mines_by_stake or config.tx_carries_ntime or config.identifies_network_by_genesis)


def test_the_GRC_conf_hint_says_NOT_to_put_the_network_in_the_conf(tmp_path):
    """A conf-file regtest=1 is IGNORED by Gridcoin and the node comes up on MAINNET."""
    hint = daemons.conf_requirements(_grc_config(tmp_path))

    assert "Do NOT put regtest=1" in hint, hint
    assert "SUBDIRECTORY" in hint
    assert "[regtest]" not in hint, "that is Bitcoin Core's shape and does nothing on Gridcoin"


# ---------------------------------------------------------------------------
# the network gate: the genesis hash, and regtest ONLY
# ---------------------------------------------------------------------------

def _gate(monkeypatch, tmp_path, genesis: str):
    stub = _Stub({
        "getblockhash": genesis,
        "getblockchaininfo": {"blocks": 0, "testnet": False},
        "getdeploymentinfo": RPCError("Method not found"),
    })
    _use(monkeypatch, stub)
    return daemons.assert_regtest(_Console(), _grc_config(tmp_path), we_started_it=True)


class _Console:
    def __init__(self):
        self.lines: list[str] = []
        self.checks: list[tuple[str, str]] = []

    def say(self, *parts):
        self.lines.append(" ".join(str(p) for p in parts))

    def check(self, label, got, expected, outcome):
        self.checks.append((label, outcome))
        return outcome


def test_the_regtest_genesis_is_accepted_and_names_the_chain(monkeypatch, tmp_path):
    info = _gate(monkeypatch, tmp_path, GENESIS_BY_NETWORK["regtest"])

    assert info["chain"] == "regtest", "step 2 prints this; it must be the network the hash established"


@pytest.mark.parametrize(("network", "words"), [
    ("main", "MAINNET"),
    ("test", "'test'"),
])
def test_every_other_Gridcoin_network_is_REFUSED_by_name(monkeypatch, tmp_path, network, words):
    """TESTNET IS REFUSED TOO: this harness mines and mocks the clock, and on testnet that is
    the operator's desk wallet staking under a fake clock."""
    with pytest.raises(RegtestSetupError) as caught:
        _gate(monkeypatch, tmp_path, GENESIS_BY_NETWORK[network])

    assert "REFUSING TO RUN" in str(caught.value)
    assert words in str(caught.value), str(caught.value)


def test_an_unknown_genesis_is_refused_as_absence_of_evidence_not_as_mainnet(monkeypatch, tmp_path):
    with pytest.raises(RegtestSetupError) as caught:
        _gate(monkeypatch, tmp_path, "ab" * 32)

    message = str(caught.value)
    assert "absence of evidence" in message, message
    assert "MAINNET" not in message, "an unknown hash is not a statement of mainnet"


def test_the_testnet_boolean_is_NOT_what_decides(monkeypatch, tmp_path):
    """`testnet: False` is what Gridcoin regtest reports. If it decided, regtest would be refused."""
    info = _gate(monkeypatch, tmp_path, GENESIS_BY_NETWORK["regtest"])

    assert info["testnet"] is False, "the fixture must carry the misleading boolean for this to mean anything"
    assert info["chain"] == "regtest"


# ---------------------------------------------------------------------------
# --wipe: the conf survives, and the mainnet datadir is still refused first
# ---------------------------------------------------------------------------

def test_wipe_KEEPS_the_conf_that_lives_inside_the_directory_it_empties(tmp_path, monkeypatch):
    monkeypatch.setattr(daemons, "rpc_answers", lambda config: False)
    config = _grc_config(tmp_path / "grc")
    net = config.regtest_dir
    (net / "txleveldb").mkdir(parents=True)
    (net / "txleveldb" / "000001.ldb").write_bytes(b"chain")
    (net / "blk0001.dat").write_bytes(b"blocks")
    config.conf_path.write_text("rpcuser=rt\nrpcpassword=x\n", encoding="utf-8")

    console = _Console()
    daemons.wipe_datadir(console, config)

    assert sorted(path.name for path in net.iterdir()) == ["gridcoinresearch.conf"]
    assert config.conf_path.read_text(encoding="utf-8") == "rpcuser=rt\nrpcpassword=x\n"
    assert any("KEPT" in line for line in console.lines), console.lines


def test_wipe_does_not_follow_a_symlink_out_of_the_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(daemons, "rpc_answers", lambda config: False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep", encoding="utf-8")
    config = _grc_config(tmp_path / "grc")
    config.regtest_dir.mkdir(parents=True)
    config.conf_path.write_text("rpcuser=rt\n", encoding="utf-8")
    (config.regtest_dir / "link").symlink_to(outside, target_is_directory=True)

    daemons.wipe_datadir(_Console(), config)

    assert (outside / "precious").read_text(encoding="utf-8") == "keep"
    assert not (config.regtest_dir / "link").exists()


@pytest.mark.parametrize("asset", ["GRC", "BTC"])
def test_wipe_REFUSES_a_regtest_dir_that_is_a_symlink_and_deletes_nothing(tmp_path, monkeypatch, asset):
    """THE REVIEW'S REPRODUCTION, 2026-11-23: dd/regtest -> victim/ came back with victim/ EMPTY.

    The GRC path listed the link's destination and deleted every entry but the conf. BTC's
    rmtree happened to refuse a symlinked root; both now refuse by name, before anything is
    listed.
    """
    monkeypatch.setattr(daemons, "rpc_answers", lambda config: False)
    victim = tmp_path / "victim"
    (victim / "blocks").mkdir(parents=True)
    (victim / "wallet.dat").write_bytes(b"a real wallet")
    config = daemons.resolve_chain_config(asset)
    config.datadir = tmp_path / "dd"
    config.datadir.mkdir()
    config.regtest_dir.symlink_to(victim, target_is_directory=True)

    with pytest.raises(RegtestSetupError) as caught:
        daemons.wipe_datadir(_Console(), config)

    assert "SYMLINK" in str(caught.value)
    assert sorted(p.name for p in victim.iterdir()) == ["blocks", "wallet.dat"], "the link's target was emptied"


def test_wipe_REFUSES_a_regtest_dir_that_resolves_outside_the_datadir(tmp_path, monkeypatch):
    """The other half: the leaf is a real directory, but a PARENT component is a link."""
    monkeypatch.setattr(daemons, "rpc_answers", lambda config: False)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "regtest").mkdir(parents=True)
    (elsewhere / "regtest" / "wallet.dat").write_bytes(b"x")
    config = _grc_config(tmp_path / "dd")
    config.datadir.mkdir()
    monkeypatch.setattr(type(config), "regtest_dir", property(lambda self: elsewhere / "link" / "regtest"))
    (elsewhere / "link").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(RegtestSetupError) as caught:
        daemons.wipe_datadir(_Console(), config)

    assert "outside the datadir" in str(caught.value)
    assert (elsewhere / "regtest" / "wallet.dat").exists()


def test_a_GRC_datadir_that_names_the_mainnet_wallet_is_still_REFUSED(tmp_path, monkeypatch):
    """The GRC row exists now, and the guard in front of the delete must not have moved."""
    datadir = tmp_path / MAINNET_DATADIR_SEGMENTS[0]
    monkeypatch.setenv("ST_REGTEST_GRC_DATADIR", str(datadir))
    config = daemons.resolve_chain_config("GRC")
    config.conf_path.parent.mkdir(parents=True)
    config.conf_path.write_text("rpcuser=rt\n", encoding="utf-8")

    with pytest.raises(RegtestSetupError) as caught:
        daemons.wipe_datadir(_Console(), config)

    assert "MAINNET" in str(caught.value)
    assert config.conf_path.exists(), "something was deleted before the refusal"


# ---------------------------------------------------------------------------
# the Gridcoin transaction layout, checked against an INDEPENDENT parser
# ---------------------------------------------------------------------------

_OUTPOINT = Outpoint(txid="11" * 32, vout=1, value_satoshis=100_000_000)
_OUTPUTS = [(99_990_000, bytes.fromhex("76a914" + "22" * 20 + "88ac"))]
_NTIME = 1_796_930_096


def test_the_Gridcoin_layout_puts_nTime_after_the_version_and_vContracts_last():
    raw = serialize_transaction(_OUTPOINT, b"", _OUTPUTS, 2021, ntime=_NTIME)

    assert raw[:4] == struct.pack("<i", 2)
    assert raw[4:8] == struct.pack("<I", _NTIME)
    assert raw[-5:] == struct.pack("<I", 2021) + b"\x00", "nLockTime, then the EMPTY vContracts vector"


def test_without_nTime_the_bytes_are_exactly_the_Bitcoin_layout():
    bitcoin = serialize_transaction(_OUTPOINT, b"", _OUTPUTS, 2021)
    gridcoin = serialize_transaction(_OUTPOINT, b"", _OUTPUTS, 2021, ntime=_NTIME)

    assert gridcoin == bitcoin[:4] + struct.pack("<I", _NTIME) + bitcoin[4:] + b"\x00"


def test_the_product_parser_reads_the_harness_layout_as_Gridcoin():
    """Two implementations written separately, agreeing on the bytes.

    modules/htlc_spend.parse_transaction() decides layout by PROOF -- it accepts only a
    reading that re-serializes to the exact input -- so if it takes this blob as the 8-byte
    Peercoin-line prefix with the vContracts byte in the suffix, the harness wrote what the
    product reads. This is a cross-check, not the harness importing what it measures: the
    harness's own code never calls the parser.
    """
    key = generate_key()
    raw_hex, _ = build_branch_spend(
        outpoint=_OUTPOINT, redeem_script=b"\x51", key=key, destination_script=_OUTPUTS[0][1],
        fee_satoshis=10_000, locktime=2021, secret=None, ntime=_NTIME,
    )
    parsed = parse_transaction(bytes.fromhex(raw_hex), expected_txid=_OUTPOINT.txid, expected_vout=_OUTPOINT.vout)

    assert len(parsed.prefix) == 8, f"read as a {len(parsed.prefix)}-byte prefix, not Gridcoin's version+nTime"
    assert parsed.suffix == struct.pack("<I", 2021) + b"\x00"


def test_the_nTime_is_SIGNED_not_just_serialized():
    """Gridcoin's SignatureHash serializes the whole transaction, nTime included."""
    one = legacy_sighash(_OUTPOINT, b"\x51", _OUTPUTS, 2021, ntime=_NTIME)
    other = legacy_sighash(_OUTPOINT, b"\x51", _OUTPUTS, 2021, ntime=_NTIME + 1)
    bitcoin = legacy_sighash(_OUTPOINT, b"\x51", _OUTPUTS, 2021)

    assert one != other, "a signature that does not cover nTime would verify against a different nTime"
    assert one != bitcoin


def test_the_control_spends_SIGNATURE_covers_the_nTime_it_carries():
    """The signature in the scriptSig verifies against the GRIDCOIN digest, and not the Bitcoin one.

    ADDED BECAUSE A MUTATION SURVIVED, 2026-11-23: dropping `ntime=` from the sighash call
    inside build_branch_spend() left every other test here green, since they check the
    serializer and the digest function separately. And the chain cannot vouch for it yet:
    every Gridcoin regtest spend was refused by input standardness BEFORE any script or
    signature check ran. So this is the only thing pinning that the control spender signs
    what it sends.
    """
    key = generate_key()
    redeem_script = b"\x51"
    destination = _OUTPUTS[0][1]
    raw_hex, script_sig = build_branch_spend(
        outpoint=_OUTPOINT, redeem_script=redeem_script, key=key, destination_script=destination,
        fee_satoshis=10_000, locktime=2021, secret=None, ntime=_NTIME,
    )
    der_with_hashtype = script_sig[1:1 + script_sig[0]]
    der = der_with_hashtype[:-1]
    outputs = [(_OUTPOINT.value_satoshis - 10_000, destination)]
    verifier = VerifyingKey.from_string(key.public_key, curve=SECP256k1)

    gridcoin_digest = legacy_sighash(_OUTPOINT, redeem_script, outputs, 2021, ntime=_NTIME)
    assert verifier.verify_digest(der, gridcoin_digest, sigdecode=sigdecode_der)

    bitcoin_digest = legacy_sighash(_OUTPOINT, redeem_script, outputs, 2021)
    with pytest.raises(BadSignatureError):
        verifier.verify_digest(der, bitcoin_digest, sigdecode=sigdecode_der)
    assert raw_hex  # the transaction itself was produced


# ---------------------------------------------------------------------------
# stake mining and the clock
# ---------------------------------------------------------------------------

class _StakeChain(_Stub):
    """A chain whose tip time is the last mock clock set, as a staked block's is."""

    def __init__(self, tip_time: int = 1_000_000):
        self.height = 0
        self.tip_time = tip_time
        super().__init__({
            "getnewaddress": "mAddr",
            "getbestblockhash": lambda: f"hash{self.height}",
            "getblock": lambda _hash: {"time": self.tip_time},
            "getblockcount": lambda: self.height,
            "setmocktime": self._set,
            "generatetoaddress": self._mine,
        })
        self.clock = 0

    def _set(self, value):
        self.clock = value

    def _mine(self, count, _address):
        self.height += count
        self.tip_time = self.clock
        return [f"h{self.height - i}" for i in range(count)]


def test_stake_mining_moves_the_clock_before_EVERY_batch_of_at_most_ten(monkeypatch, tmp_path):
    chain = _StakeChain()
    _use(monkeypatch, chain)
    run = _run(_grc_config(tmp_path))

    mined = steps._mine(run, 25)

    batches = [params[0] for method, params in chain.calls if method == "generatetoaddress"]
    assert batches == [10, 10, 5], batches
    for index, (method, _) in enumerate(chain.calls):
        if method == "generatetoaddress":
            assert chain.calls[index - 1][0] == "setmocktime", "a batch was mined without moving the clock"
    clocks = [params[0] for method, params in chain.calls if method == "setmocktime"]
    assert clocks == sorted(clocks) and len(set(clocks)) == len(clocks), "the clock must only move forward"
    assert mined.height == 25 and len(mined.hashes) == 25


def test_after_stake_mining_the_clock_is_left_PAST_the_new_tip(monkeypatch, tmp_path):
    """Every caller sends from the wallet next, and a clock at or behind the tip cannot."""
    chain = _StakeChain()
    _use(monkeypatch, chain)
    run = _run(_grc_config(tmp_path))

    steps._mine(run, 10)

    assert run.facts.mock_clock > chain.tip_time
    assert chain.calls[-1] == ("setmocktime", (run.facts.mock_clock,))


def test_the_clock_is_put_past_a_tip_that_is_AHEAD_of_the_wall_clock(monkeypatch, tmp_path):
    """The 2026-11-23 restart: blocks 402 hours ahead, wallet unable to select a coin."""
    far_future = 1_796_915_664
    chain = _StakeChain(tip_time=far_future)
    _use(monkeypatch, chain)
    run = _run(_grc_config(tmp_path))

    steps._clock_past_tip(run, "test")

    assert run.facts.mock_clock == far_future + steps.CLOCK_MARGIN_SECONDS


def test_a_Core_chain_never_touches_the_clock(monkeypatch):
    stub = _Stub({"getnewaddress": "addr", "generatetoaddress": lambda n, a: ["h"] * n, "getblockcount": 5})
    _use(monkeypatch, stub)
    run = _run(daemons.resolve_chain_config("BTC"), wallet="w")

    steps._mine(run, 5)
    steps._clock_past_tip(run, "test")

    assert not [m for m, _ in stub.calls if m == "setmocktime"], stub.calls


# ---------------------------------------------------------------------------
# the two RPC shapes, probed rather than assumed
# ---------------------------------------------------------------------------

def test_a_daemon_without_gettxout_or_maxfeerate_is_driven_without_them(monkeypatch, tmp_path):
    stub = _Stub({
        "help": lambda method: ("help: unknown command: gettxout" if method == "gettxout"
                                else 'sendrawtransaction "hex_string"\n\nSubmit a raw transaction'),
        "sendrawtransaction": "txid",
    })
    _use(monkeypatch, stub)
    run = _run(_grc_config(tmp_path))
    steps.probe_spend_rpcs(run)

    assert run.facts.has_gettxout is False
    assert run.facts.sendraw_takes_maxfeerate is False
    steps._broadcast(run, "00")
    assert stub.calls[-1] == ("sendrawtransaction", ("00",)), "Gridcoin answers -32700 to a second argument"


def test_a_Core_daemon_still_gets_the_fee_ceiling_removed(monkeypatch):
    stub = _Stub({
        "help": lambda method: "gettxout ..." if method == "gettxout" else "sendrawtransaction ... maxfeerate ...",
        "sendrawtransaction": "txid",
    })
    _use(monkeypatch, stub)
    run = _run(daemons.resolve_chain_config("BTC"), wallet="w")
    steps.probe_spend_rpcs(run)

    steps._broadcast(run, "00")
    assert stub.calls[-1] == ("sendrawtransaction", ("00", steps.NO_FEE_LIMIT))


# ---------------------------------------------------------------------------
# standardness: a refusal from before the script is never a timelock pass
# ---------------------------------------------------------------------------

def test_the_daemons_reject_reason_is_graded_as_nonstandard():
    assert steps.classify_refusal("TX rejected bad-txns-nonstandard-inputs") == steps.REFUSAL_NONSTANDARD
    # And the existing kinds are unmoved by the new marker coming first.
    assert steps.classify_refusal("non-mandatory-script-verify-flag (x)") == steps.REFUSAL_POLICY
    assert steps.classify_refusal("mandatory-script-verify-flag-failed (x)") == steps.REFUSAL_CONSENSUS
    assert steps.classify_refusal("TX rejected") == steps.REFUSAL_OTHER


def _contract() -> steps.Contract:
    participant, refund = generate_key(), generate_key()
    return steps.Contract(
        redeem_script=b"\x63\x51\x67\x51\x68", p2sh_address="2Mx", p2sh_script=b"\xa9\x14" + b"\x00" * 20 + b"\x87",
        locktime=2021, secret=b"\x01" * 32, secret_hash=b"\x02" * 32, participant=participant, refund=refund,
    )


def test_a_nonstandard_refusal_of_the_early_refund_is_a_FAIL_not_a_pass(monkeypatch, tmp_path):
    """THE 2026-11-23 OVERCLAIM: two step-8 checks scored OK for a timelock never asked."""
    stub = _Stub({
        "sendrawtransaction": RPCError("sendrawtransaction: code=-22 message=TX rejected"),
        "help": lambda method: "testmempoolaccept [rawtxs]",
        "testmempoolaccept": [{"allowed": False, "reject-reason": "bad-txns-nonstandard-inputs"}],
    })
    _use(monkeypatch, stub)
    run = _run(_grc_config(tmp_path))
    run.facts.sendraw_takes_maxfeerate = False

    outcome, kind = steps._attempt_refund_expecting_refusal(run, _contract(), _OUTPOINT, 2020, "8b [control]")

    assert kind == steps.REFUSAL_NONSTANDARD
    assert outcome == FAIL, "a refusal from before the script ran was scored as the timelock refusing"


def test_a_generic_refusal_with_no_testmempoolaccept_is_graded_as_before(monkeypatch, tmp_path):
    """No reason available means the old grading -- never an invented one."""
    stub = _Stub({
        "sendrawtransaction": RPCError("sendrawtransaction: code=-22 message=TX rejected"),
        "help": lambda method: "help: unknown command: testmempoolaccept",
    })
    _use(monkeypatch, stub)
    run = _run(_grc_config(tmp_path))
    run.facts.sendraw_takes_maxfeerate = False

    outcome, kind = steps._attempt_refund_expecting_refusal(run, _contract(), _OUTPOINT, 2020, "8b [control]")

    assert kind == steps.REFUSAL_OTHER
    assert outcome == OK
    assert "testmempoolaccept" not in [m for m, _ in stub.calls]


class _RefusingClient:
    """A real-client stand-in whose refund is refused with a message that NAMES the reason."""

    def refund_contract(self, **_kwargs):
        raise RuntimeError("RPC Error: {'code': -26, 'message': 'bad-txns-nonstandard-inputs'}")


def test_step_8b_real_client_branch_fails_on_a_NAMED_standardness_refusal(monkeypatch, tmp_path):
    """THE REVIEW'S SECOND FINDING, 2026-11-23: this branch scored any refusal OK.

    Gridcoin's bare `TX rejected` grades OTHER and falls through to the control, which is
    the only reason the first run did not print an OK here. A message that names the reason
    skips that fallback, so the branch itself has to refuse to credit it.
    """
    chain = _StakeChain()
    chain.answers.update({
        "sendrawtransaction": RPCError("sendrawtransaction: code=-22 message=TX rejected"),
        "help": lambda method: "testmempoolaccept [rawtxs]" if method == "testmempoolaccept" else "x",
        "testmempoolaccept": [{"reject-reason": "bad-txns-nonstandard-inputs"}],
    })
    _use(monkeypatch, chain)
    run = _run(_grc_config(tmp_path))
    run.facts.sendraw_takes_maxfeerate = False
    contract = _contract()
    chain.height = contract.locktime - 1
    outcome = steps.ChainOutcome(asset="GRC")
    monkeypatch.setattr(steps, "_fund_directly", lambda run, contract, label: _OUTPOINT)

    steps.step_8_refund_before_expiry(run, _RefusingClient(), contract, _OUTPOINT, outcome)

    assert outcome.real_refund_before_expiry_refused == FAIL
    assert outcome.refused_before_any_script is True
    assert outcome.refund_before_expiry_rejected == FAIL


def test_the_verdict_names_standardness_rather_than_blaming_the_script():
    outcome = steps.ChainOutcome(asset="GRC", real_redeem=FAIL, control_redeem=FAIL,
                                 refund_after_expiry=FAIL, refused_before_any_script=True)

    verdict = outcome.verdict()
    assert "NEITHER BRANCH WAS EXERCISED" in verdict, verdict
    assert "STANDARDNESS" in verdict


def test_the_cltv_verdict_does_not_credit_the_timelock_for_a_nonstandard_refusal():
    outcome = steps.ChainOutcome(asset="GRC", refund_before_expiry_rejected=FAIL,
                                 refund_refusal_kind=steps.REFUSAL_NONSTANDARD, height_at_refund_test=2020)

    assert "NOT ESTABLISHED" in outcome.cltv_verdict()


# ---------------------------------------------------------------------------
# the entry point: the client, and the --chain table
# ---------------------------------------------------------------------------

def _entry():
    path = Path(__file__).resolve().parent.parent / "regtest_htlc_verify.py"
    spec = importlib.util.spec_from_file_location("regtest_htlc_verify_grc_leg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_GRC_client_never_picks_up_the_operators_wallet_passphrase(monkeypatch, tmp_path):
    """None reads GRIDCOIN_WALLET_PASSPHRASE; the harness must state "" instead."""
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "the-desk-wallets-real-one")
    # setenv, NOT delenv. _build_grc_client() setdefault()s this variable straight on
    # os.environ, and a variable monkeypatch only DELETED is one it has no record of
    # restoring -- so until 2026-11-23 this test leaked a random regtest address into every
    # later test in the session, and two in test_htlc_spend.py failed on it depending on
    # collection order (found by review, reproduced with both files on one command line).
    # setenv gives monkeypatch the restore, and setdefault leaves an existing value alone.
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", NOT_A_CREDENTIAL)
    config = _grc_config(tmp_path)
    config.rpc_password = NOT_A_CREDENTIAL

    client = _entry().build_real_client("GRC", config, "")

    assert type(client).__name__ == "GRCClient"
    assert client.wallet_passphrase == ""
    assert client.rpc_url == config.base_url, "Gridcoin has one wallet, at the bare URL"


def test_every_chain_choice_is_accepted_and_both_still_means_BTC_and_LTC():
    entry = _entry()

    for choice in entry.CHAIN_CHOICES:
        assert entry.parse_args(["--chain", choice]).chain == choice
    assert entry.CHAIN_CHOICES["both"] == ["BTC", "LTC"]
    assert entry.CHAIN_CHOICES["grc"] == ["GRC"]
    assert set(entry.CHAIN_CHOICES["all"]) == {"BTC", "LTC", "GRC"}
