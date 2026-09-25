"""Everything in the regtest harness that can be proven without a daemon.

Role: test (read-only; no socket, no subprocess, no chain)
Reads: swap_terminal/regtest/*, and the real modules/atomic_htlc_scripts.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- nothing here opens a connection or starts a process.

WHY THIS FILE EXISTS, STATED HONESTLY.

`regtest_htlc_verify.py` needs bitcoind and litecoind. It was written on a
machine that has neither and cannot install either, so THE HARNESS ITSELF HAS
NEVER BEEN RUN. That is not a thing to gloss: the first execution will be on
the operator's computer.

What can be separated from the daemons is the part that decides anything --
the scriptSig layout, the sighash preimage, the address and key encodings, the
verdict wording, the configuration resolution -- and all of it is exercised
here. What cannot be separated is the RPC conversation: which methods a given
daemon build accepts, what its error strings say, whether a wallet is legacy or
descriptor. Those are unexercised, and the harness is written to report them
rather than to assume them.

THE SCRIPT INTERPRETER BELOW IS A TEST INSTRUMENT AND IS NOT SHIPPED.

`_eval_p2sh_spend()` executes the eleven opcodes an HTLC redeem script uses.
It exists because the single highest-risk thing in the harness is the ORDER of
the pushes in a scriptSig: get it wrong and a real node says
`mandatory-script-verify-flag-failed`, which is the same thing it says when the
script itself is wrong -- so a mistake here would make the harness report the
wrong culprit for the exact defect it was built to diagnose. Running the real
builder's script against the real scriptSig, in a stack machine, catches that
without a chain.

It is NOT a substitute for the chain. It does not implement minimal-push rules,
signature encoding policy, dust, standardness, the mempool's non-final check,
or a hundred other things a node enforces. It proves the stack arithmetic and
nothing else, and the harness's own step 8 still needs a node to prove the
parts a stack machine cannot.
"""

import hashlib
import logging
import os
from io import StringIO

import base58
import pytest
from chains.base import RPCError
from ecdsa import SECP256k1, VerifyingKey
from ecdsa.util import sigdecode_der
from modules.atomic_htlc_scripts import (
    build_htlc_redeem_script,
    parse_and_reencode_as_testnet_p2pkh,
    script_to_p2sh_address,
)
from regtest.console import FAIL, OK, SKIP, XFAIL, Console, redact, value
from regtest.daemons import RegtestRPC, RegtestSetupError, resolve_chain_config
from regtest.keys import RegtestKey, generate_key, hash160
from regtest.steps import (
    REDEEM_FAILED_ON_LOOKUP,
    REDEEM_FAILED_ON_SIGNING,
    REDEEM_FAILED_UNCLASSIFIED,
    ChainOutcome,
    Mined,
    Run,
    _find_vout_by_script,
    _p2sh_script_for,
    _verbose_tx,
    classify_redeem_failure,
)
from regtest.txbuild import (
    SEQUENCE_NON_FINAL,
    Outpoint,
    build_branch_spend,
    coins_to_satoshis,
    describe_script_sig,
    legacy_sighash,
    push_data,
    serialize_transaction,
    varint,
)

CONTRACT_SATOSHIS = 100_000_000
FEE_SATOSHIS = 10_000
LOCKTIME = 389


# --------------------------------------------------------------------------
# a minimal script interpreter, for this file only
# --------------------------------------------------------------------------

OP_DUP, OP_EQUALVERIFY, OP_SHA256, OP_HASH160 = 0x76, 0x88, 0xA8, 0xA9
OP_CHECKSIG, OP_CLTV, OP_DROP = 0xAC, 0xB1, 0x75
OP_IF, OP_ELSE, OP_ENDIF = 0x63, 0x67, 0x68
LOCKTIME_THRESHOLD = 500_000_000
SEQUENCE_FINAL = 0xFFFFFFFF


class ScriptFailure(AssertionError):
    """The script evaluated to false, or an opcode refused. Carries which one."""


def _parse_script(script: bytes):
    """Yield (opcode, data) pairs. `data` is None for a non-push opcode."""
    index = 0
    while index < len(script):
        opcode = script[index]
        index += 1
        if 0x01 <= opcode <= 0x4B:
            yield opcode, script[index : index + opcode]
            index += opcode
        elif opcode == 0x4C:
            length = script[index]
            index += 1
            yield opcode, script[index : index + length]
            index += length
        else:
            yield opcode, None


def _decode_script_number(raw: bytes) -> int:
    """CScriptNum, sign-and-magnitude little-endian. The inverse of the builder's encoder."""
    if not raw:
        return 0
    magnitude = int.from_bytes(raw, "little")
    if raw[-1] & 0x80:
        magnitude &= (1 << (8 * len(raw) - 1)) - 1
        return -magnitude
    return magnitude


def _stack_from_script_sig(script_sig: bytes) -> list[bytes]:
    stack = []
    for opcode, data in _parse_script(script_sig):
        if data is not None:
            stack.append(data)
        elif opcode == 0x00:
            stack.append(b"")
        elif opcode == 0x51:
            stack.append(b"\x01")
        else:
            raise ScriptFailure(f"scriptSig contains a non-push opcode 0x{opcode:02x}")
    return stack


def _eval_p2sh_spend(script_sig: bytes, digest: bytes, tx_locktime: int, sequence: int = SEQUENCE_NON_FINAL) -> bool:
    """Run a P2SH spend: scriptSig pushes, last push is the redeem script, execute it."""
    stack = _stack_from_script_sig(script_sig)
    if not stack:
        raise ScriptFailure("empty scriptSig")
    redeem_script = stack.pop()
    executing = True
    skip_depth = 0
    for opcode, data in _parse_script(redeem_script):
        if opcode in (OP_IF, OP_ELSE, OP_ENDIF):
            executing, skip_depth = _control_flow(opcode, stack, executing, skip_depth)
            continue
        if not executing:
            continue
        if data is not None:
            stack.append(data)
            continue
        _apply_opcode(opcode, stack, digest, tx_locktime, sequence)
    return bool(stack) and stack[-1] not in (b"", b"\x00")


def _control_flow(opcode: int, stack: list[bytes], executing: bool, skip_depth: int):
    if opcode == OP_IF:
        if executing:
            condition = stack.pop()
            return condition not in (b"", b"\x00"), skip_depth
        return False, skip_depth + 1
    if opcode == OP_ELSE:
        if skip_depth == 0:
            return not executing, skip_depth
        return executing, skip_depth
    if skip_depth:
        return executing, skip_depth - 1
    return True, 0


def _op_dup(stack, _digest, _locktime, _sequence):
    stack.append(stack[-1])


def _op_sha256(stack, _digest, _locktime, _sequence):
    stack.append(hashlib.sha256(stack.pop()).digest())


def _op_hash160(stack, _digest, _locktime, _sequence):
    stack.append(hash160(stack.pop()))


def _op_equalverify(stack, _digest, _locktime, _sequence):
    first, second = stack.pop(), stack.pop()
    if first != second:
        raise ScriptFailure(f"OP_EQUALVERIFY: {first.hex()} != {second.hex()}")


def _op_drop(stack, _digest, _locktime, _sequence):
    stack.pop()


def _op_cltv(stack, _digest, tx_locktime, sequence):
    required = _decode_script_number(stack[-1])
    if sequence == SEQUENCE_FINAL:
        raise ScriptFailure("OP_CHECKLOCKTIMEVERIFY: the input sequence is final")
    if (required >= LOCKTIME_THRESHOLD) != (tx_locktime >= LOCKTIME_THRESHOLD):
        raise ScriptFailure("OP_CHECKLOCKTIMEVERIFY: a height and a timestamp cannot be compared")
    if tx_locktime < required:
        raise ScriptFailure(f"OP_CHECKLOCKTIMEVERIFY: nLockTime {tx_locktime} < required {required}")


def _op_checksig(stack, digest, _locktime, _sequence):
    public_key, signature = stack.pop(), stack.pop()
    verifier = VerifyingKey.from_string(public_key, curve=SECP256k1)
    try:
        # The trailing byte is the sighash type, which is not part of the DER.
        verifier.verify_digest(signature[:-1], digest, sigdecode=sigdecode_der)
    except Exception as exc:
        raise ScriptFailure(f"OP_CHECKSIG failed: {exc}") from exc
    stack.append(b"\x01")


# One handler per opcode, dispatched rather than branched. A chain of elifs
# past the complexity ceiling is CLAUDE.md rule 12's C901 finding, and its fix
# is to extract rather than to raise the ceiling.
_OPCODE_HANDLERS = {
    OP_DUP: _op_dup,
    OP_SHA256: _op_sha256,
    OP_HASH160: _op_hash160,
    OP_EQUALVERIFY: _op_equalverify,
    OP_DROP: _op_drop,
    OP_CLTV: _op_cltv,
    OP_CHECKSIG: _op_checksig,
}


def _apply_opcode(opcode: int, stack: list[bytes], digest: bytes, tx_locktime: int, sequence: int) -> None:
    handler = _OPCODE_HANDLERS.get(opcode)
    if handler is None:
        raise ScriptFailure(f"this test interpreter does not implement opcode 0x{opcode:02x}")
    handler(stack, digest, tx_locktime, sequence)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def contract():
    """A real redeem script from the REAL builder, plus the keys that satisfy it."""
    participant = generate_key()
    refund = generate_key()
    secret = os.urandom(32)
    secret_hash = hashlib.sha256(secret).digest()
    redeem_script = build_htlc_redeem_script(
        secret_hash=secret_hash.hex(),
        participant_address=participant.address,
        refund_address=refund.address,
        locktime=LOCKTIME,
    )
    return {
        "participant": participant,
        "refund": refund,
        "secret": secret,
        "secret_hash": secret_hash,
        "redeem_script": redeem_script,
        "outpoint": Outpoint(txid="ab" * 32, vout=1, value_satoshis=CONTRACT_SATOSHIS),
    }


def _spend(contract, key: RegtestKey, secret, nlocktime: int):
    raw_hex, script_sig = build_branch_spend(
        outpoint=contract["outpoint"],
        redeem_script=contract["redeem_script"],
        key=key,
        destination_script=key.p2pkh_script,
        fee_satoshis=FEE_SATOSHIS,
        locktime=nlocktime,
        secret=secret,
    )
    digest = legacy_sighash(
        contract["outpoint"],
        contract["redeem_script"],
        [(CONTRACT_SATOSHIS - FEE_SATOSHIS, key.p2pkh_script)],
        nlocktime,
    )
    return raw_hex, script_sig, digest


# --------------------------------------------------------------------------
# the two branches actually execute -- the highest-risk thing in the harness
# --------------------------------------------------------------------------


def test_hashlock_branch_evaluates_true(contract):
    """<sig> <pubkey> <preimage> OP_1 satisfies the OP_IF branch of the real script."""
    _, script_sig, digest = _spend(contract, contract["participant"], contract["secret"], 0)
    assert _eval_p2sh_spend(script_sig, digest, tx_locktime=0) is True


def test_timelock_branch_evaluates_true_at_the_locktime(contract):
    _, script_sig, digest = _spend(contract, contract["refund"], None, LOCKTIME)
    assert _eval_p2sh_spend(script_sig, digest, tx_locktime=LOCKTIME) is True


def test_timelock_branch_refuses_below_the_locktime(contract):
    """This is step 8b in a stack machine: the script runs, and CLTV refuses it."""
    _, script_sig, digest = _spend(contract, contract["refund"], None, LOCKTIME - 1)
    with pytest.raises(ScriptFailure, match="CHECKLOCKTIMEVERIFY"):
        _eval_p2sh_spend(script_sig, digest, tx_locktime=LOCKTIME - 1)


def test_timelock_branch_refuses_a_final_sequence(contract):
    """BIP65: CLTV fails outright on a final input, whatever the heights say."""
    _, script_sig, digest = _spend(contract, contract["refund"], None, LOCKTIME)
    with pytest.raises(ScriptFailure, match="sequence is final"):
        _eval_p2sh_spend(script_sig, digest, tx_locktime=LOCKTIME, sequence=0xFFFFFFFF)


def test_the_harness_never_uses_a_final_sequence():
    assert SEQUENCE_NON_FINAL != 0xFFFFFFFF


def test_hashlock_branch_refuses_the_wrong_preimage(contract):
    """A wrong preimage must fail at OP_EQUALVERIFY, not somewhere vaguer."""
    _, script_sig, digest = _spend(contract, contract["participant"], b"\x00" * 32, 0)
    with pytest.raises(ScriptFailure, match="OP_EQUALVERIFY"):
        _eval_p2sh_spend(script_sig, digest, tx_locktime=0)


def test_hashlock_branch_refuses_the_refund_key(contract):
    """The two branches need DIFFERENT keys -- which is why the builder refuses equal hashes."""
    _, script_sig, digest = _spend(contract, contract["refund"], contract["secret"], 0)
    with pytest.raises(ScriptFailure, match="OP_EQUALVERIFY"):
        _eval_p2sh_spend(script_sig, digest, tx_locktime=0)


def test_timelock_branch_refuses_the_participant_key(contract):
    _, script_sig, digest = _spend(contract, contract["participant"], None, LOCKTIME)
    with pytest.raises(ScriptFailure, match="OP_EQUALVERIFY"):
        _eval_p2sh_spend(script_sig, digest, tx_locktime=LOCKTIME)


# --------------------------------------------------------------------------
# encodings: keys, addresses, scripts, amounts
# --------------------------------------------------------------------------


def test_generated_address_survives_the_real_reencoder():
    """If it did not, the hash160 in the script would not match the key we sign with."""
    key = generate_key()
    assert parse_and_reencode_as_testnet_p2pkh(key.address) == key.address


def test_p2pkh_script_matches_the_address_payload():
    key = generate_key()
    decoded = base58.b58decode_check(key.address)
    assert decoded[0] == 0x6F
    assert decoded[1:] == key.hash160
    assert key.p2pkh_script == b"\x76\xa9\x14" + key.hash160 + b"\x88\xac"


def test_wif_is_testnet_and_compressed():
    key = generate_key()
    decoded = base58.b58decode_check(key.wif)
    assert decoded[0] == 0xEF
    assert decoded[1:33] == key.private_key
    assert decoded[33] == 0x01


def test_signatures_are_low_s_and_verify():
    """A high-S signature is rejected by a node's standardness rules, and would read as a script bug."""
    key = generate_key()
    digest = hashlib.sha256(b"regtest").digest()
    signature = key.sign_digest(digest)
    _, s_value = sigdecode_der(signature, SECP256k1.order)
    assert s_value <= SECP256k1.order // 2
    verifier = VerifyingKey.from_string(key.public_key, curve=SECP256k1)
    assert verifier.verify_digest(signature, digest, sigdecode=sigdecode_der)


def test_p2sh_script_matches_the_builders_address(contract):
    """The harness asserts on the scriptPubKey hex; this pins it to the builder's address."""
    script = _p2sh_script_for(contract["redeem_script"])
    decoded = base58.b58decode_check(script_to_p2sh_address(contract["redeem_script"]))
    assert decoded[0] == 0xC4
    assert script == b"\xa9\x14" + decoded[1:] + b"\x87"


def test_varint_boundaries():
    assert varint(0) == b"\x00"
    assert varint(0xFC) == b"\xfc"
    assert varint(0xFD) == b"\xfd\xfd\x00"
    assert varint(0xFFFF) == b"\xfd\xff\xff"
    assert varint(0x10000) == b"\xfe\x00\x00\x01\x00"


def test_push_data_boundaries():
    assert push_data(b"\x01") == b"\x01\x01"
    assert push_data(b"a" * 0x4B)[0] == 0x4B
    assert push_data(b"a" * 0x4C)[:2] == b"\x4c\x4c"


def test_coins_to_satoshis_is_exact():
    """float arithmetic gets 0.1 and 1.1 wrong, and a one-satoshi error reads as a chain bug."""
    assert coins_to_satoshis("0.1") == 10_000_000
    assert coins_to_satoshis("1.0") == 100_000_000
    assert coins_to_satoshis("1.1") == 110_000_000
    assert coins_to_satoshis("0.00000001") == 1


def test_serialized_transaction_carries_the_fields_a_refund_depends_on(contract):
    raw_hex, _, _ = _spend(contract, contract["refund"], None, LOCKTIME)
    raw = bytes.fromhex(raw_hex)
    assert raw[:4] == b"\x02\x00\x00\x00"
    # txids serialize little-endian; a reversed one references an outpoint nobody has.
    assert raw[4:5] == b"\x01"
    assert raw[5:37] == bytes.fromhex(contract["outpoint"].txid)[::-1]
    assert raw[-4:] == LOCKTIME.to_bytes(4, "little")
    assert SEQUENCE_NON_FINAL.to_bytes(4, "little") in raw


def test_serialize_transaction_is_stable_for_the_same_inputs(contract):
    """Deterministic signatures mean two runs produce identical bytes, so pastes compare."""
    key = contract["refund"]
    outputs = [(CONTRACT_SATOSHIS - FEE_SATOSHIS, key.p2pkh_script)]
    first = serialize_transaction(contract["outpoint"], b"\x00", outputs, LOCKTIME)
    second = serialize_transaction(contract["outpoint"], b"\x00", outputs, LOCKTIME)
    assert first == second


def test_sighash_changes_with_the_locktime(contract):
    """If it did not, step 8b and step 9 would be signing the same transaction."""
    outputs = [(CONTRACT_SATOSHIS - FEE_SATOSHIS, contract["refund"].p2pkh_script)]
    early = legacy_sighash(contract["outpoint"], contract["redeem_script"], outputs, LOCKTIME - 1)
    late = legacy_sighash(contract["outpoint"], contract["redeem_script"], outputs, LOCKTIME)
    assert early != late


def test_build_branch_spend_refuses_a_fee_that_eats_the_output(contract):
    with pytest.raises(ValueError, match="nothing would be left"):
        build_branch_spend(
            outpoint=contract["outpoint"],
            redeem_script=contract["redeem_script"],
            key=contract["refund"],
            destination_script=contract["refund"].p2pkh_script,
            fee_satoshis=CONTRACT_SATOSHIS,
            locktime=LOCKTIME,
            secret=None,
        )


# --------------------------------------------------------------------------
# the preimage never reaches the screen
# --------------------------------------------------------------------------


def test_redact_returns_none_of_the_preimage():
    secret = os.urandom(32)
    rendered = redact(secret)
    assert secret.hex() not in rendered
    assert str(secret) not in rendered


def test_describe_script_sig_abbreviates_the_preimage(contract):
    """The hashlock scriptSig legitimately CONTAINS the preimage; printing it must not."""
    _, script_sig, _ = _spend(contract, contract["participant"], contract["secret"], 0)
    rendered = describe_script_sig(script_sig)
    assert contract["secret"].hex() not in rendered
    assert "push[32]=" in rendered


def test_the_harness_does_not_log_the_preimage_when_building_a_contract(caplog):
    """The real builder logs at DEBUG. Assert the preimage is in none of it."""
    participant, refund = generate_key(), generate_key()
    secret = os.urandom(32)
    with caplog.at_level(logging.DEBUG):
        build_htlc_redeem_script(
            secret_hash=hashlib.sha256(secret).hexdigest(),
            participant_address=participant.address,
            refund_address=refund.address,
            locktime=LOCKTIME,
        )
    text = " ".join(record.getMessage() + repr(record.args) for record in caplog.records)
    assert secret.hex() not in text


# --------------------------------------------------------------------------
# reporting: the operator reads the screen, not the source
# --------------------------------------------------------------------------


def test_empty_results_never_print_nothing():
    assert value(None) == "(none)"
    assert "(none" in value("")
    assert "(none" in value([])
    assert value(0) == "0"


def test_console_counts_each_outcome_separately():
    console = Console(total_steps=9, stream=StringIO())
    console.check("a", 1, 1, OK)
    console.check("b", 2, 3, FAIL)
    console.check("c", "boom", "a txid", XFAIL)
    console.check("d", None, "n/a", SKIP)
    assert console.counts == {OK: 1, FAIL: 1, XFAIL: 1, SKIP: 1}
    assert len(console.failures) == 1
    assert "b" in console.failures[0]


def test_a_predicted_failure_is_not_counted_as_a_failure():
    """XFAIL is the known defect confirmed. Counting it as FAIL invites somebody to 'fix' the assertion."""
    console = Console(total_steps=9, stream=StringIO())
    console.check("redeem", "TypeError", "a txid", XFAIL)
    assert console.counts[FAIL] == 0
    assert console.failures == []


def test_check_prints_got_and_expected_on_one_line():
    stream = StringIO()
    console = Console(total_steps=9, stream=stream)
    console.check("height", 101, ">= 101", OK)
    line = stream.getvalue()
    assert "got=101" in line
    assert "expected=>= 101" in line
    assert "µfn" in line, "durations are microfortnights (rule 6)"


def test_durations_use_the_micro_sign_and_no_space():
    stream = StringIO()
    console = Console(total_steps=9, stream=stream)
    console.check("x", 1, 1, OK)
    text = stream.getvalue()
    assert "ufn" not in text, "an ASCII u in displayed output is a defect, not a fallback"
    assert " µfn" not in text, "no space between the number and the unit"


# --------------------------------------------------------------------------
# the verdict, which is the single most valuable line the harness prints
# --------------------------------------------------------------------------


def test_verdict_when_only_the_refund_branch_spends():
    outcome = ChainOutcome(asset="BTC", real_redeem=XFAIL, control_redeem=FAIL, refund_after_expiry=OK)
    assert "ONLY THE REFUND BRANCH SPENDS" in outcome.verdict()


def test_verdict_when_the_script_is_sound_but_the_client_is_not():
    outcome = ChainOutcome(asset="BTC", real_redeem=XFAIL, control_redeem=OK, refund_after_expiry=OK)
    verdict = outcome.verdict()
    assert "SCRIPT is sound" in verdict
    assert "only by refund" in verdict


def test_verdict_when_both_branches_spend_through_real_code():
    outcome = ChainOutcome(asset="BTC", real_redeem=OK, control_redeem=SKIP, refund_after_expiry=OK)
    assert "both branches spend" in outcome.verdict()


def test_verdict_when_the_refund_branch_is_the_broken_one():
    outcome = ChainOutcome(asset="BTC", real_redeem=XFAIL, control_redeem=OK, refund_after_expiry=FAIL)
    assert "REFUND branch does not" in outcome.verdict()


def test_verdict_refuses_to_conclude_when_nothing_was_shown():
    outcome = ChainOutcome(asset="LTC")
    assert "neither branch was shown to spend" in outcome.verdict()


# --------------------------------------------------------------------------
# configuration and output location
# --------------------------------------------------------------------------


def test_chain_defaults_match_the_operators_machine():
    btc = resolve_chain_config("BTC")
    ltc = resolve_chain_config("LTC")
    assert (btc.port, ltc.port) == (18443, 19443)
    assert (btc.rpc_user, btc.rpc_password) == ("rt", "rt")
    assert btc.datadir.name == "btc"
    assert btc.conf_path.name == "bitcoin.conf"
    assert ltc.conf_path.name == "litecoin.conf"
    assert btc.base_url == "http://127.0.0.1:18443"


def test_every_chain_setting_is_overridable(monkeypatch):
    monkeypatch.setenv("ST_REGTEST_BTC_DATADIR", "/opt/regtest-elsewhere")
    monkeypatch.setenv("ST_REGTEST_BTC_RPC_PORT", "29443")
    monkeypatch.setenv("ST_REGTEST_BTC_RPC_USER", "someone")
    monkeypatch.setenv("ST_REGTEST_RPC_HOST", "10.0.0.5")
    config = resolve_chain_config("BTC")
    assert str(config.datadir) == "/opt/regtest-elsewhere"
    assert config.port == 29443
    assert config.rpc_user == "someone"
    assert config.base_url == "http://10.0.0.5:29443"


def test_the_pid_file_lives_under_the_regtest_subdirectory():
    """The wipe deletes exactly this directory, and the stop reads exactly this file."""
    config = resolve_chain_config("BTC")
    assert config.pid_path == config.datadir / "regtest" / "bitcoind.pid"
    assert config.regtest_dir == config.datadir / "regtest"


def test_vouts_are_found_by_script_not_by_the_addresses_field():
    """Bitcoin Core removed `addresses` in 22.0; matching on it is why the real code cannot find its own output."""
    raw_tx = {
        "vout": [
            {"scriptPubKey": {"hex": "aa" * 10}},
            {"scriptPubKey": {"hex": "bb" * 10, "address": "somewhere"}},
        ]
    }
    assert _find_vout_by_script(raw_tx, "bb" * 10) == 1
    assert _find_vout_by_script(raw_tx, "cc" * 10) is None
    assert _find_vout_by_script({"vout": []}, "bb" * 10) is None


def test_the_spawn_record_survives_a_step_that_raises():
    """rule 13: the reaper must not depend on the spawner returning normally.

    The first version of the entry point read `we_started_it` from
    step_2_daemon()'s RETURN value, so a daemon that spawned and then timed out
    waiting for RPC was left running by a teardown that believed it had adopted
    it. This pins the mutable record that closed that window.
    """
    run = Run(console=Console(total_steps=9, stream=StringIO()), config=resolve_chain_config("BTC"))
    assert run.spawn.started is False
    try:
        run.spawn.started = True
        raise RegtestSetupError("RPC never answered")
    except RegtestSetupError:
        pass
    assert run.spawn.started is True, "the teardown would have orphaned the daemon"


def test_two_runs_do_not_share_a_spawn_record():
    """A default_factory, not a shared default: one chain's teardown must not read the other's."""
    console = Console(total_steps=9, stream=StringIO())
    first = Run(console=console, config=resolve_chain_config("BTC"))
    second = Run(console=console, config=resolve_chain_config("LTC"))
    first.spawn.started = True
    assert second.spawn.started is False


# --------------------------------------------------------------------------
# bug 1, measured on the operator's machine 2026-09-25: a mined transaction
# could not be read back, which killed BTC before steps 7, 8 and 9 ran
# --------------------------------------------------------------------------


class _StubNode:
    """Records calls and answers them from a script of canned results.

    Not a mock framework: the assertions here are about WHICH RPCs were tried
    and in what ORDER, so the recording is the point.
    """

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

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


_TXINDEX_ERROR = RPCError(
    "getrawtransaction: code=-5 message=No such mempool transaction. "
    "Use -txindex or provide a block hash to enable blockchain transaction queries."
)


def test_a_mined_transaction_is_read_back_with_its_block_hash():
    """The exact failure from the first live run: mined, then unreadable."""
    node = _StubNode({
        "getrawtransaction": lambda txid, verbose, *rest: (
            {"vout": [], "confirmations": 1} if rest else _raise(_TXINDEX_ERROR)
        ),
    })
    result = _verbose_tx(node, "ab" * 32, "beef" * 16)
    assert result["confirmations"] == 1
    assert node.calls[0][0] == "getrawtransaction"
    assert len(node.calls[0][1]) == 3, "the block hash must be passed, which is what makes it work"


def _raise(exc):
    raise exc


def test_a_wallet_transaction_is_read_back_without_an_index_or_a_block_hash():
    """Route 2: gettransaction carries the hex and the confirmations, and needs no index."""
    node = _StubNode({
        "gettransaction": {"hex": "00", "confirmations": 3, "blockhash": "cd" * 32},
        "decoderawtransaction": {"vout": [{"scriptPubKey": {"hex": "aa"}}]},
    })
    result = _verbose_tx(node, "ab" * 32)
    assert result["confirmations"] == 3
    assert result["blockhash"] == "cd" * 32
    assert [call[0] for call in node.calls] == ["gettransaction", "decoderawtransaction"]


def test_a_non_wallet_spend_falls_through_every_route_and_names_them_all():
    """The redeem and refund spends are not wallet transactions, so route 2 cannot see them."""
    node = _StubNode({
        "getrawtransaction": _TXINDEX_ERROR,
        "gettransaction": RPCError("gettransaction: code=-5 message=Invalid or non-wallet transaction id"),
    })
    with pytest.raises(RegtestSetupError) as caught:
        _verbose_tx(node, "ab" * 32, "beef" * 16)
    message = str(caught.value)
    assert "getrawtransaction with blockhash" in message
    assert "gettransaction + decoderawtransaction" in message
    assert "needs -txindex" in message
    assert "does NOT add -txindex=1" in message, "the harness must not reindex the operator's datadir"


def test_mined_keeps_the_block_hashes_generatetoaddress_returned():
    mined = Mined(height=102, hashes=["aa" * 32, "bb" * 32])
    assert mined.first_hash == "aa" * 32
    assert Mined(height=0, hashes=[]).first_hash is None


# --------------------------------------------------------------------------
# bug 2, measured the same run: LTC died at step 3 with a bare HTTP status
# --------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def _call_with(monkeypatch, response):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        return response

    monkeypatch.setattr("regtest.daemons.requests.post", fake_post)
    # Built from the resolved config rather than from literals: there is then
    # no credential spelled in this file at all, and the test also pins that
    # the LTC defaults are what the harness would really connect with.
    config = resolve_chain_config("LTC")
    node = RegtestRPC(
        user=config.rpc_user,
        password=config.rpc_password,
        host=config.host,
        port=config.port,
    )
    return node, captured


def test_an_rpc_error_carried_on_http_500_is_still_parsed(monkeypatch):
    """Litecoin 0.21.4 answers an RPC error with HTTP 500 AND a JSON body.

    The first live run threw the body away and printed only `HTTPError: 500`,
    so the operator could not see which call failed or why.
    """
    payload = {"result": None, "error": {"code": -18, "message": "Wallet file verification failed"}}
    node, _ = _call_with(monkeypatch, _StubResponse(500, payload))
    with pytest.raises(RPCError) as caught:
        node.call("loadwallet", "regtest_htlc_harness")
    message = str(caught.value)
    assert "loadwallet" in message
    assert "code=-18" in message
    assert "Wallet file verification failed" in message


def test_a_successful_result_on_http_200_is_returned(monkeypatch):
    node, _ = _call_with(monkeypatch, _StubResponse(200, {"result": {"blocks": 101}, "error": None}))
    assert node.call("getblockchaininfo") == {"blocks": 101}


def test_a_non_json_body_reports_the_status_and_what_was_sent(monkeypatch):
    """No body to parse means the status IS the diagnosis -- and it says so."""
    node, _ = _call_with(monkeypatch, _StubResponse(403, None, text="<html>forbidden</html>"))
    with pytest.raises(RPCError) as caught:
        node.call("uptime")
    assert "HTTP 403" in str(caught.value)
    assert "forbidden" in str(caught.value)


def test_an_empty_non_json_body_still_prints_something(monkeypatch):
    node, _ = _call_with(monkeypatch, _StubResponse(500, None, text=""))
    with pytest.raises(RPCError, match=r"\(none: empty body\)"):
        node.call("uptime")


def test_a_non_2xx_with_no_error_object_is_not_treated_as_a_result(monkeypatch):
    """A result the caller cannot tell from a real one is the failure mode to avoid."""
    node, _ = _call_with(monkeypatch, _StubResponse(500, {"result": "surprise", "error": None}))
    with pytest.raises(RPCError, match="carried no error object"):
        node.call("uptime")


# --------------------------------------------------------------------------
# the harness must not claim a defect the run did not reach
# --------------------------------------------------------------------------


def test_a_txindex_lookup_failure_is_not_read_as_the_preimage_defect():
    """redeem_contract()'s FIRST line is a getrawtransaction, so it can fail before signing."""
    assert classify_redeem_failure(
        "RPCError: getrawtransaction: code=-5 message=No such mempool transaction. Use -txindex"
    ) == REDEEM_FAILED_ON_LOOKUP


def test_an_incomplete_signing_result_is_the_preimage_defect():
    assert classify_redeem_failure("Exception: BTC signing incomplete: {'complete': False}") == REDEEM_FAILED_ON_SIGNING


def test_an_unrecognized_failure_is_classified_as_unrecognized():
    """Not a synonym for 'signing': claiming a defect the run did not establish is the failure."""
    assert classify_redeem_failure("ConnectionResetError: [Errno 104]") == REDEEM_FAILED_UNCLASSIFIED


def test_the_verdict_refuses_to_judge_a_redeem_that_never_reached_signing():
    outcome = ChainOutcome(
        asset="BTC",
        real_redeem=XFAIL,
        real_redeem_stage=REDEEM_FAILED_ON_LOOKUP,
        control_redeem=OK,
        refund_after_expiry=OK,
    )
    verdict = outcome.verdict()
    assert "was NOT judged on the hashlock branch" in verdict
    assert "remains inferred from the source" in verdict
    assert "CANNOT spend the hashlock branch" not in verdict


def test_the_verdict_does_judge_a_redeem_that_reached_signing_and_failed():
    outcome = ChainOutcome(
        asset="BTC",
        real_redeem=XFAIL,
        real_redeem_stage=REDEEM_FAILED_ON_SIGNING,
        control_redeem=OK,
        refund_after_expiry=OK,
    )
    assert "CANNOT spend the hashlock branch" in outcome.verdict()
