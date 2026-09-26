"""The four fund-path defects, each pinned by a test that fails without its fix.

Role: test (read-only; no socket, no subprocess, no chain)
Reads: swap_terminal/modules/htlc_spend.py, htlc_rpc.py, htlc_fee.py, the three
       atomic_*_client.py, and the REAL modules/atomic_htlc_scripts.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- nothing here opens a connection or starts a process. The
       "node" every test talks to is FakeNode below, a dict of recorded calls.

WHAT THIS FILE PROVES, AND WHAT IT CANNOT.

Four defects were fixed on 2026-09-25, all four measured first against real
Bitcoin Core 28.1.0 and Litecoin Core 0.21.4 regtest daemons:

  1. redeem_contract() never pushed the preimage, so a funded contract was
     recoverable only by refund.
  2. redeem_contract()'s first call searched only the mempool, so it could not
     read back a CONFIRMED contract -- which every real swap redeems.
  3. create_contract() called importaddress, refused on Core 28.1's default
     descriptor wallet, so no contract could be created at all.
  4. wait_for_tx_output() matched on scriptPubKey.addresses, removed in Core
     22.0, so the poll never found a perfectly funded output.

And a fifth that only became reachable once 1 was fixed: the flat 0.0001 miner
fee is not a fee RATE at all, so it is the right amount at exactly one size and
drifts under the minimum relay fee as a transaction grows.

THIS PARAGRAPH SAID SOMETHING ELSE UNTIL 2026-09-25 -- "the flat 0.0001 miner
fee is 0.31 coin/kvB on a ~323-byte redeem, three times over
sendrawtransaction's 0.10 default maxfeerate" -- and that is the 1000x error
modules/htlc_fee.py's header exists to correct, restated here as fact in the
file whose own test_the_flat_fee_was_never_over_the_ceiling asserts the
opposite thirty functions down. 0.0001 over 0.323 kvB is 0.00031 coin/kvB, 323
times UNDER the ceiling. A reader who trusted this header would have concluded
the fee rule exists to avoid a refusal that could never have happened.

AND A SIXTH, found by the review of the five above: there was no DUST check at
all. See the dust section near the end of this file.

THE STRONGEST THING AVAILABLE WITHOUT A CHAIN is to run the REAL builder's
redeem script against the REAL client's scriptSig in a stack machine. That is
what `test_the_real_clients_scriptsig_satisfies_the_real_redeem_script` does,
and the signature it checks is verified against a sighash computed by a
DIFFERENT implementation -- regtest/txbuild.py's, which the harness uses for
its independent control spend. If the production serializer and the harness's
disagreed by one byte, the signature would not verify and that test would say
so.

The stack machine itself is imported from tests/test_regtest_harness_units.py
rather than copied. Rule 8: one interpreter, one place. It implements the
eleven opcodes an HTLC uses and nothing else -- no minimal-push rule, no
signature encoding policy, no dust, no standardness, no mempool. It proves the
stack arithmetic. Only the operator's `python3 regtest_htlc_verify.py --wipe`
run proves the rest.

WHAT IS NOT PROVEN HERE AT ALL: Gridcoin. There is no GRC node in this setup.
`test_a_gridcoin_shaped_transaction_round_trips` exercises the Peercoin-line
transaction layout (a 4-byte nTime between the version and the input count)
against a synthetic transaction this file builds by hand, which establishes
that the parser handles the shape -- not that Gridcoin's shape is that one.
"""

import hashlib
import logging
import os
import struct
from decimal import Decimal
from json import dumps as json_dumps

import pytest
from modules import atomic_btc_client as btc_module
from modules import atomic_grc_client as grc_module
from modules import atomic_ltc_client as ltc_module
from modules.atomic_btc_client import BTCClient
from modules.atomic_grc_client import GRCClient
from modules.atomic_htlc_scripts import build_htlc_redeem_script, p2sh_script_for
from modules.atomic_htlc_scripts import push_data as client_push_data
from modules.atomic_ltc_client import LTCClient
from modules.htlc_fee import (
    BROADCAST_CEILING_COIN_PER_KVB,
    assert_no_output_is_dust,
    assert_within_broadcast_ceiling,
    dust_threshold_satoshis,
    effective_rate_coin_per_kvb,
    fee_rate_coin_per_kvb,
    is_witness_program,
    minimum_fee_coin,
    platform_fee_coin,
    redeem_miner_fee,
)
from modules.htlc_rpc import (
    assert_output_pays_the_contract,
    build_hashlock_spend,
    describe_rpc_payload,
    ensure_watch_only_import,
    find_output_by_script,
    lookup_contract_output,
    read_transaction_outputs,
    wait_for_tx_output,
)
from modules.htlc_spend import (
    MAX_DER_SIGNATURE_WITH_HASHTYPE,
    TransactionLayoutError,
    coins_to_satoshis,
    decode_wif,
    estimated_script_sig_length,
    hashlock_script_sig,
    parse_transaction,
    participant_key_matches_script,
    public_key_for,
    satoshis_to_coins,
    sign_digest,
)
from regtest.keys import generate_key
from regtest.steps import _p2sh_script_for
from regtest.txbuild import Outpoint
from regtest.txbuild import legacy_sighash as harness_legacy_sighash
from regtest.txbuild import push_data as harness_push_data
from regtest.txbuild import redeem_script_sig as harness_redeem_script_sig

# ScriptFailure and _eval_p2sh_spend come from the sibling test module. ONE
# stack interpreter in this repository, imported rather than copied (rule 8);
# pytest puts tests/ on sys.path, which is what makes this importable.
from test_regtest_harness_units import ScriptFailure, _eval_p2sh_spend

CONTRACT_COINS = Decimal("1.0")
# Not a credential: a literal handed to a FakeNode that has no wallet. It is
# here so that the GRC case actually makes the `walletpassphrase` call whose
# parameter used to be printed, and so the assertion has a value to look for.
GRC_WALLET_UNLOCK_LITERAL = "not-a-real-passphrase-0000"
CONTRACT_SATOSHIS = 100_000_000
LOCKTIME = 400_000

# Transaction field bytes the fake node emits. Version 2 and a non-final
# sequence are what regtest/txbuild.py also emits, which is what lets a
# signature built by the production path be checked against a sighash computed
# by the harness's independent one.
VERSION_2_PREFIX = struct.pack("<i", 2)
SEQUENCE_NON_FINAL_BYTES = struct.pack("<I", 0xFFFFFFFE)
# A Peercoin-line prefix: the same 4-byte version, then a 4-byte nTime. This is
# the layout Gridcoin is believed to use. Synthetic -- see the module docstring.
GRIDCOIN_PREFIX = VERSION_2_PREFIX + struct.pack("<I", 1_700_000_000)

# The measured failure, verbatim from Bitcoin Core 28.1.0 on 2026-09-25.
NO_SUCH_MEMPOOL_TRANSACTION = (
    "RPC Error: {'code': -5, 'message': 'No such mempool transaction. Use -txindex or provide a block hash "
    "to enable blockchain transaction queries. Use gettransaction for wallet transactions.'}"
)
# The measured failure from `importaddress` on a Core 28.1 descriptor wallet.
ONLY_LEGACY_WALLETS = "RPC Error: {'code': -4, 'message': 'Only legacy wallets are supported by this command'}"


# --------------------------------------------------------------------------
# a fake node: it records every call, and serializes by hand
# --------------------------------------------------------------------------


def _manual_unsigned_transaction(txid: str, vout: int, outputs: list[tuple[int, bytes]], prefix: bytes) -> str:
    """Lay out an unsigned one-input transaction BY HAND, in this test file.

    Deliberately not built with modules/htlc_spend.ParsedTransaction.serialize():
    the parser under test must be fed bytes that its own serializer did not
    produce, or a round-trip assertion proves only that a function is its own
    inverse.
    """
    body = prefix
    body += b"\x01"
    body += bytes.fromhex(txid)[::-1] + struct.pack("<I", vout) + b"\x00" + SEQUENCE_NON_FINAL_BYTES
    body += bytes([len(outputs)])
    for satoshis, script in outputs:
        body += struct.pack("<q", satoshis) + bytes([len(script)]) + script
    body += struct.pack("<I", 0)
    return body.hex()


def _manual_segwit_transaction(txid: str, vout: int, outputs: list[tuple[int, bytes]]) -> str:
    """The SAME transaction, serialized with a witness. Laid out by hand, here.

    This is the shape a default Bitcoin Core 28.1 or Litecoin 0.21.4 wallet
    produces for an ordinary send, because those wallets hold the operator's
    own coins in bech32 P2WPKH -- so it is the shape of every FUNDING
    transaction this package's create_contract() broadcasts and then polls for.

    BIP144: after the 4-byte version come a 0x00 marker and a 0x01 flag, and
    after the outputs come one witness stack per input. modules/htlc_spend.
    parse_transaction() reads the marker as an input count of zero and refuses,
    which is correct of it -- an HTLC P2SH input has no witness and it is a
    SPEND parser -- and was the defect when a LOOKUP used it.
    """
    body = VERSION_2_PREFIX + b"\x00\x01"
    body += b"\x01"
    body += bytes.fromhex(txid)[::-1] + struct.pack("<I", vout) + b"\x00" + SEQUENCE_NON_FINAL_BYTES
    body += bytes([len(outputs)])
    for satoshis, script in outputs:
        body += struct.pack("<q", satoshis) + bytes([len(script)]) + script
    # One witness stack for the single input: a 71-byte signature and a
    # 33-byte compressed pubkey, which is what spending a P2WPKH costs.
    body += b"\x02" + b"\x47" + b"\x30" * 0x47 + b"\x21" + b"\x02" * 0x21
    body += struct.pack("<I", 0)
    return body.hex()


class FakeNode:
    """Every RPC the fixed clients make, answered from memory and recorded.

    `calls` is the list of (method, params) in order, which is how the tests
    assert on what was NOT called -- no signrawtransaction*, no importprivkey.
    An RPC with no answer configured RAISES, so a test cannot pass by accident
    on a call nobody thought about.
    """

    def __init__(self, scripts: dict[str, bytes], prefix: bytes = VERSION_2_PREFIX, descriptors: bool = True):
        self.scripts = scripts
        self.prefix = prefix
        self.descriptors = descriptors
        self.calls: list[tuple[str, list]] = []
        self.unspent: dict[tuple[str, int], tuple[Decimal, str]] = {}
        self.raw_transactions: dict[str, str] = {}
        self.wallet_transactions: dict[str, str] = {}
        # raw hex -> the outputs that went into it. A real daemon decodes its
        # OWN serialization by construction, whatever shape that is; this is
        # how the fake node does the same thing without a parser, which is the
        # property the segwit test is about.
        self.decoded_outputs: dict[str, list[tuple[int, bytes]]] = {}
        self.broadcast: list[str] = []
        self.fail: dict[str, str] = {}
        self.sent_to: list[tuple[str, float]] = []

    def rpc_call(self, method: str, params=None):
        params = list(params or [])
        self.calls.append((method, params))
        if method in self.fail:
            raise Exception(self.fail[method])
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            raise Exception(f"RPC Error: the fake node was not told how to answer {method!r}")
        return handler(*params)

    # -- reads ----------------------------------------------------------------
    def _rpc_gettxout(self, txid, vout, _include_mempool=True):
        entry = self.unspent.get((txid, int(vout)))
        if entry is None:
            return None
        value, script_hex = entry
        return {"value": float(value), "scriptPubKey": {"hex": script_hex}, "confirmations": 3}

    def remember_wallet_transaction(self, txid, outputs, prefix=VERSION_2_PREFIX, witness=False) -> str:
        """Serialize a transaction the wallet knows, and record how to decode it."""
        raw = (
            _manual_segwit_transaction(txid, 0, outputs)
            if witness
            else _manual_unsigned_transaction("22" * 32, 0, outputs, prefix)
        )
        self.wallet_transactions[txid] = raw
        self.decoded_outputs[raw] = outputs
        return raw

    def _rpc_decoderawtransaction(self, raw_hex):
        outputs = self.decoded_outputs.get(raw_hex)
        if outputs is None:
            raise Exception(f"RPC Error: the fake node did not serialize {raw_hex[:16]}... and cannot decode it")
        return {
            "vout": [
                {
                    "value": float(satoshis_to_coins(satoshis)),
                    "n": index,
                    # `hex` and no `address`/`addresses`, which is Core 28.1's
                    # shape minus the fields nothing here reads.
                    "scriptPubKey": {"hex": script.hex()},
                }
                for index, (satoshis, script) in enumerate(outputs)
            ]
        }

    def _rpc_gettransaction(self, txid):
        raw = self.wallet_transactions.get(txid)
        if raw is None:
            raise Exception(f"RPC Error: {{'code': -5, 'message': 'Invalid or non-wallet transaction id {txid}'}}")
        return {"hex": raw, "confirmations": 3}

    def _rpc_getrawtransaction(self, txid, _verbose=False, _blockhash=None):
        raise Exception(NO_SUCH_MEMPOOL_TRANSACTION)

    def _rpc_getwalletinfo(self):
        return {"walletname": "fake", "descriptors": self.descriptors}

    def _rpc_getdescriptorinfo(self, descriptor):
        return {"descriptor": f"{descriptor}#checksum0"}

    def _rpc_decodescript(self, script_hex):
        return {"p2sh": f"p2sh-for-{script_hex[:8]}"}

    # -- writes ---------------------------------------------------------------
    # The GRC client unlocks the wallet before create_contract() and before
    # redeem_contract(), and `walletpassphrase` carries the operator's
    # passphrase as its first parameter. Answered here so that
    # test_no_client_logs_the_preimage can drive that call and assert the
    # passphrase never reaches a log -- it is the second secret in the line
    # that used to print the payload verbatim.
    def _rpc_walletlock(self):
        return None

    def _rpc_walletpassphrase(self, _passphrase, _timeout):
        return None

    def _rpc_importdescriptors(self, _requests):
        return [{"success": True}]

    def _rpc_importaddress(self, *_args):
        return None

    def _rpc_sendtoaddress(self, address, amount):
        self.sent_to.append((address, amount))
        return "cc" * 32

    def _rpc_createrawtransaction(self, inputs, outputs):
        entry = inputs[0]
        laid_out = []
        for address, amount in outputs.items():
            if address not in self.scripts:
                raise Exception(f"RPC Error: Invalid address {address}")
            laid_out.append((coins_to_satoshis(Decimal(str(amount))), self.scripts[address]))
        return _manual_unsigned_transaction(entry["txid"], int(entry["vout"]), laid_out, self.prefix)

    def _rpc_sendrawtransaction(self, raw_hex, *_rest):
        self.broadcast.append(raw_hex)
        return "dd" * 32

    @property
    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]


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
    destination = generate_key()
    platform = generate_key()
    return {
        "participant": participant,
        "refund": refund,
        "destination": destination,
        "platform": platform,
        "secret": secret,
        "secret_hash": secret_hash,
        "redeem_script": redeem_script,
        "txid": "ab" * 32,
        "vout": 1,
    }


def p2wpkh_script(key) -> bytes:
    """OP_0 <20-byte hash160> -- what a bech32 address decodes to.

    Needed because the SHIPPED default PLATFORM_FEE_LTC_ADDRESS is a bech32
    `tltc1q...`, so the platform-fee output on a real LTC redeem is P2WPKH and
    carries Litecoin's LOWER dust threshold (2,940 rather than 5,460). A test
    that laid the fee out as P2PKH like everything else here would be measuring
    the easier case.
    """
    return b"\x00\x14" + key.hash160


def _node_for(contract, prefix: bytes = VERSION_2_PREFIX, platform_script=None, value=CONTRACT_COINS) -> FakeNode:
    """A node that knows this contract's output and the addresses it can pay.

    `platform_script` and `value` default to what every pre-existing test here
    used -- a P2PKH platform fee and a 1.0 contract. Both are parameters now so
    the dust tests can build the shape a real LTC redeem has (a P2WPKH fee
    address) at the amount a small swap has, which is the combination nothing
    in this file could express before.
    """
    node = FakeNode(
        scripts={
            contract["destination"].address: contract["destination"].p2pkh_script,
            contract["platform"].address: platform_script or contract["platform"].p2pkh_script,
            contract["participant"].address: contract["participant"].p2pkh_script,
        },
        prefix=prefix,
    )
    node.unspent[(contract["txid"], contract["vout"])] = (
        value,
        p2sh_script_for(contract["redeem_script"]).hex(),
    )
    return node


# --------------------------------------------------------------------------
# defect 1: the preimage reaches the stack, and the script accepts it
# --------------------------------------------------------------------------


def test_the_real_clients_scriptsig_satisfies_the_real_redeem_script(contract):
    """The whole chain, end to end, in a stack machine.

    THIS IS THE ONE THAT FAILS WITHOUT THE FIX. Before 2026-09-25 there was no
    scriptSig to test: redeem_contract() handed the spend to
    signrawtransactionwithwallet, which answered `Unable to sign input, invalid
    stack size (possibly missing key)` and produced nothing.

    The signature is verified against a sighash computed by
    regtest/txbuild.legacy_sighash -- a DIFFERENT implementation, the one the
    regtest harness uses for its independent control spend. So this asserts
    that the production serializer and the harness's agree byte for byte, which
    is the property that makes the harness's verdict ("the script is sound, the
    client is wrong") trustworthy.
    """
    node = _node_for(contract)
    spend = build_hashlock_spend(
        asset="BTC",
        rpc_call=node.rpc_call,
        contract_txid=contract["txid"],
        contract_vout=contract["vout"],
        contract_value=CONTRACT_COINS,
        redeem_script=contract["redeem_script"],
        secret=contract["secret"],
        wif=contract["participant"].wif,
        destination_address=contract["destination"].address,
    )
    fee_satoshis = coins_to_satoshis(spend.miner_fee)
    digest = harness_legacy_sighash(
        Outpoint(txid=contract["txid"], vout=contract["vout"], value_satoshis=CONTRACT_SATOSHIS),
        contract["redeem_script"],
        [(CONTRACT_SATOSHIS - fee_satoshis, contract["destination"].p2pkh_script)],
        0,
    )
    assert _eval_p2sh_spend(spend.script_sig, digest, tx_locktime=0) is True


def test_the_scriptsig_actually_contains_the_preimage(contract):
    """Five elements, and the third is the preimage. The parameter used to be ignored.

    Asserted on the BYTES rather than on the stack machine's verdict, because
    those are two different claims: the machine says the script accepts it, and
    this says the preimage is what was pushed. A scriptSig that satisfied the
    script some other way would pass the first and fail this.
    """
    node = _node_for(contract)
    spend = build_hashlock_spend(
        asset="BTC",
        rpc_call=node.rpc_call,
        contract_txid=contract["txid"],
        contract_vout=contract["vout"],
        contract_value=CONTRACT_COINS,
        redeem_script=contract["redeem_script"],
        secret=contract["secret"],
        wif=contract["participant"].wif,
        destination_address=contract["destination"].address,
    )
    assert push_of(contract["secret"]) in spend.script_sig
    assert spend.script_sig.endswith(push_of(contract["redeem_script"]))
    # OP_1 immediately before the redeem script's push: the TRUE that sends
    # OP_IF down the hashlock branch rather than the refund branch.
    assert spend.script_sig[-len(push_of(contract["redeem_script"])) - 1] == 0x51


def push_of(data: bytes) -> bytes:
    """The bytes a push of `data` occupies, via the client's own encoder."""
    return client_push_data(data)


def signature_slack(script_sig: bytes) -> int:
    """How many bytes shorter this scriptSig's signature is than the maximum.

    THE FEE IS SIZED BEFORE THE SIGNATURE EXISTS, from
    modules/htlc_spend.estimated_script_sig_length(), which reserves
    MAX_DER_SIGNATURE_WITH_HASHTYPE bytes for it. The broadcast transaction is
    therefore this many bytes shorter than the one the fee was computed for --
    exactly, not approximately, because the signature is the only part of the
    scriptSig whose length can vary.

    WHY A FUNCTION AND WHY THIS IS NOT A `<= 2` ANYWHERE ANY MORE. Three
    assertions in this file bounded that difference at 2, and the module docstring
    claimed the same thing. All four were wrong about roughly 1 signature in 256:
    DER drops a byte from r or s whenever the top bit is clear (the common case,
    slack 1 or 2) and drops ANOTHER whenever the value's leading byte is itself
    zero (slack 3, and 4 a further 1/256 down). Under pytest-randomly, which
    reseeds each run, that surfaced as a test that passed 8 times in isolation and
    failed in a full suite: a 235-byte scriptSig against a 238-byte estimate, and
    an LTC redeem paying 30 sat/byte over 357 bytes for a 354-byte transaction.
    Both slack 3.

    So the bound is computed rather than guessed, and the assertions became
    equalities that cannot flake.

    READING IT NEEDS NO PARSER (rule 8: there is no push DECODER in this tree and
    this is not the place to add the first one). A DER signature with its hashtype
    is at most 73 bytes, which is below OP_PUSHDATA1's 76-byte threshold, so the
    first byte of the scriptSig IS the signature's length. The read is checked back
    through the client's own ENCODER below, so a wrong assumption fails here rather
    than silently shifting every fee assertion in the file.
    """
    length = script_sig[0]
    assert push_of(script_sig[1 : 1 + length]) == script_sig[: 1 + length], (
        "the first push of a hashlock scriptSig is the signature; if this fails the "
        "scriptSig layout changed and every fee assertion below is reading the wrong byte"
    )
    assert length <= MAX_DER_SIGNATURE_WITH_HASHTYPE, (
        f"a {length}-byte signature exceeds the {MAX_DER_SIGNATURE_WITH_HASHTYPE}-byte maximum the "
        f"estimate reserves, so the fee was sized for LESS than what is broadcast"
    )
    return MAX_DER_SIGNATURE_WITH_HASHTYPE - length


def test_a_wrong_preimage_is_refused_by_the_script(contract):
    """Mutation check: the stack machine is not rubber-stamping the scriptSig."""
    node = _node_for(contract)
    spend = build_hashlock_spend(
        asset="BTC",
        rpc_call=node.rpc_call,
        contract_txid=contract["txid"],
        contract_vout=contract["vout"],
        contract_value=CONTRACT_COINS,
        redeem_script=contract["redeem_script"],
        secret=b"\x00" * 32,
        wif=contract["participant"].wif,
        destination_address=contract["destination"].address,
    )
    fee_satoshis = coins_to_satoshis(spend.miner_fee)
    digest = harness_legacy_sighash(
        Outpoint(txid=contract["txid"], vout=contract["vout"], value_satoshis=CONTRACT_SATOSHIS),
        contract["redeem_script"],
        [(CONTRACT_SATOSHIS - fee_satoshis, contract["destination"].p2pkh_script)],
        0,
    )
    with pytest.raises(ScriptFailure):
        _eval_p2sh_spend(spend.script_sig, digest, tx_locktime=0)


def test_the_client_and_the_harness_build_the_same_scriptsig(contract):
    """modules/htlc_spend.py and regtest/txbuild.py are two implementations, on purpose.

    They are NOT merged, because the harness's control spend is what answers
    "is the redeem SCRIPT sound, or is the CLIENT wrong?" and an instrument
    that imported the thing it measures would fail in lockstep with it. This
    test is the mechanical link that duplication normally lacks: the two must
    produce byte-identical output for the same inputs, and if either drifts
    this says so on the next run.
    """
    key = contract["participant"]
    private_key, compressed = decode_wif(key.wif)
    signature = sign_digest(private_key, b"\x11" * 32)
    public_key = public_key_for(private_key, compressed)
    assert public_key == key.public_key
    mine = hashlock_script_sig(signature, public_key, contract["secret"], contract["redeem_script"])
    theirs = harness_redeem_script_sig(signature, key, contract["secret"], contract["redeem_script"])
    assert mine == theirs


def test_the_two_push_encoders_agree_byte_for_byte():
    """The other deliberate duplicate: two push_data implementations, one meaning.

    Every boundary either one branches on, plus the bytes on each side of it.
    A scriptSig whose pushes disagreed with the redeem script's pushes by one
    byte would hash to a different P2SH and fail for a reason no error message
    names.
    """
    for length in (0, 1, 0x4B, 0x4C, 0x4D, 0xFE, 0xFF, 0x100, 0x101, 0xFFFF, 0x10000):
        data = b"\xab" * length
        assert client_push_data(data) == harness_push_data(data), f"push_data disagrees at {length} bytes"


def test_the_harness_and_the_client_agree_on_the_p2sh_script(contract):
    """The third deliberate duplicate: the contract's scriptPubKey, computed twice."""
    assert p2sh_script_for(contract["redeem_script"]) == _p2sh_script_for(contract["redeem_script"])


def test_a_key_that_is_not_in_the_script_is_refused_before_signing(contract):
    """The wrong key produces `mandatory-script-verify-flag-failed` on chain, which is
    also what a wrong preimage, a wrong branch selector and a wrong script produce.
    Four candidates and no way to choose. Refused here, where it can be named."""
    stranger = generate_key()
    assert not participant_key_matches_script(stranger.public_key, contract["redeem_script"])
    node = _node_for(contract)
    with pytest.raises(ValueError, match="does not"):
        build_hashlock_spend(
            asset="BTC",
            rpc_call=node.rpc_call,
            contract_txid=contract["txid"],
            contract_vout=contract["vout"],
            contract_value=CONTRACT_COINS,
            redeem_script=contract["redeem_script"],
            secret=contract["secret"],
            wif=stranger.wif,
            destination_address=contract["destination"].address,
        )
    assert node.broadcast == [], "nothing may be broadcast after a refusal"


def test_the_refund_key_is_accepted_by_the_guard_and_refused_by_the_script(contract):
    """The guard is a guard, not a validator, and says so in its docstring.

    The refund key's hash160 IS in the script -- in the other branch -- so the
    cheap local check passes and the script's own OP_EQUALVERIFY is what
    refuses it. Pinned so that nobody later reads the guard as proof of which
    branch a key is for.
    """
    assert participant_key_matches_script(contract["refund"].public_key, contract["redeem_script"])
    node = _node_for(contract)
    spend = build_hashlock_spend(
        asset="BTC",
        rpc_call=node.rpc_call,
        contract_txid=contract["txid"],
        contract_vout=contract["vout"],
        contract_value=CONTRACT_COINS,
        redeem_script=contract["redeem_script"],
        secret=contract["secret"],
        wif=contract["refund"].wif,
        destination_address=contract["destination"].address,
    )
    fee_satoshis = coins_to_satoshis(spend.miner_fee)
    digest = harness_legacy_sighash(
        Outpoint(txid=contract["txid"], vout=contract["vout"], value_satoshis=CONTRACT_SATOSHIS),
        contract["redeem_script"],
        [(CONTRACT_SATOSHIS - fee_satoshis, contract["destination"].p2pkh_script)],
        0,
    )
    with pytest.raises(ScriptFailure):
        _eval_p2sh_spend(spend.script_sig, digest, tx_locktime=0)


# --------------------------------------------------------------------------
# the transaction parser, which is what lets one signer serve three chains
# --------------------------------------------------------------------------


def test_a_bitcoin_shaped_transaction_round_trips(contract):
    raw_hex = _manual_unsigned_transaction(
        contract["txid"], contract["vout"], [(99_990_000, contract["destination"].p2pkh_script)], VERSION_2_PREFIX
    )
    parsed = parse_transaction(bytes.fromhex(raw_hex), contract["txid"], contract["vout"])
    assert parsed.serialize().hex() == raw_hex
    assert parsed.prefix == VERSION_2_PREFIX
    assert parsed.output_total == 99_990_000


def test_a_gridcoin_shaped_transaction_round_trips(contract):
    """The Peercoin-line layout: a 4-byte nTime between the version and the inputs.

    SYNTHETIC. No Gridcoin node produced these bytes and none is available here
    (rule 17). What this establishes is that the parser resolves the layout
    from the bytes rather than assuming Bitcoin's, and that the extra field
    survives into the re-serialization -- so it is covered by the sighash,
    which is the part that would silently sign the wrong thing.
    """
    raw_hex = _manual_unsigned_transaction(
        contract["txid"], contract["vout"], [(99_990_000, contract["destination"].p2pkh_script)], GRIDCOIN_PREFIX
    )
    parsed = parse_transaction(bytes.fromhex(raw_hex), contract["txid"], contract["vout"])
    assert parsed.prefix == GRIDCOIN_PREFIX
    assert parsed.serialize().hex() == raw_hex


def test_the_parser_refuses_a_transaction_that_spends_something_else(contract):
    """The outpoint check is what makes the layout decision a proof, not a guess."""
    raw_hex = _manual_unsigned_transaction(
        "ee" * 32, 0, [(99_990_000, contract["destination"].p2pkh_script)], VERSION_2_PREFIX
    )
    with pytest.raises(TransactionLayoutError, match="asked to spend"):
        parse_transaction(bytes.fromhex(raw_hex), contract["txid"], contract["vout"])


def test_the_parser_refuses_bytes_it_cannot_reproduce():
    with pytest.raises(TransactionLayoutError, match="could not take apart"):
        parse_transaction(b"\x02\x00\x00\x00\xff\xff")


def test_the_parser_refuses_an_encoding_it_cannot_reproduce_exactly(contract):
    """The round-trip proof, on the one input that gets past every other check.

    A NON-MINIMAL compact size -- 0xfd 0x01 0x00 for the number one, instead of
    0x01 -- parses cleanly, has the right input count, and carries the right
    outpoint. Everything except the bytes agrees. It is caught only because
    re-serializing produces a different blob than the daemon sent.

    That is not a pedantic difference. The sighash is computed over THIS
    module's serialization and the transaction that gets broadcast is also this
    module's serialization, so if the daemon's bytes and ours diverge anywhere,
    the signature commits to a transaction that is not the one being sent --
    and the failure surfaces as a bare script error with nothing pointing at
    the encoding.

    ADDED AFTER A MUTATION TEST FOUND THE GAP: disabling the round-trip check
    broke nothing in this file, because every other case was already refused by
    the outpoint check one line below it.
    """
    raw = bytearray(bytes.fromhex(_manual_unsigned_transaction(
        contract["txid"], contract["vout"], [(99_990_000, contract["destination"].p2pkh_script)], VERSION_2_PREFIX
    )))
    assert raw[4] == 0x01, "the input count is the byte right after a 4-byte version"
    raw[4:5] = b"\xfd\x01\x00"
    with pytest.raises(TransactionLayoutError, match="re-serializing did not reproduce the bytes"):
        parse_transaction(bytes(raw), contract["txid"], contract["vout"])


def test_the_size_estimate_is_an_upper_bound_and_close(contract):
    """The fee is sized from an estimate made before the signature exists.

    It must never be SMALLER than the transaction that gets broadcast, or the
    fee was computed for something lighter than what a miner is asked to carry.
    The slack is the DER signature's own variability, and it is computed from the
    signature this transaction actually carries rather than bounded at 2 -- see
    signature_slack() for the run where `<= 2` was measured false.
    """
    node = _node_for(contract)
    spend = build_hashlock_spend(
        asset="BTC",
        rpc_call=node.rpc_call,
        contract_txid=contract["txid"],
        contract_vout=contract["vout"],
        contract_value=CONTRACT_COINS,
        redeem_script=contract["redeem_script"],
        secret=contract["secret"],
        wif=contract["participant"].wif,
        destination_address=contract["destination"].address,
    )
    assert spend.size_bytes <= spend.estimated_size_bytes
    assert spend.estimated_size_bytes - spend.size_bytes == signature_slack(spend.script_sig)


def test_the_estimated_script_sig_length_matches_what_is_built(contract):
    """The estimate is exactly the real scriptSig plus the bytes the signature did
    not use -- asserted as an equality, since the signature is in hand here.

    This is where `<= 2` was measured false: a full-suite run under pytest-randomly
    produced estimate 238 against actual 235. The signature had a 31-byte r.
    """
    private_key, compressed = decode_wif(contract["participant"].wif)
    public_key = public_key_for(private_key, compressed)
    estimate = estimated_script_sig_length(public_key, contract["secret"], contract["redeem_script"])
    signature = sign_digest(private_key, b"\x22" * 32)
    actual = len(
        hashlock_script_sig(signature, public_key, contract["secret"], contract["redeem_script"])
    )
    assert actual <= estimate
    assert estimate - actual == MAX_DER_SIGNATURE_WITH_HASHTYPE - len(signature)


def test_a_three_byte_signature_slack_is_computed_rather_than_bounded_away():
    """THE RARE CASE, PINNED SO IT IS NO LONGER RARE.

    Three assertions in this file bounded the estimate's slack at 2 bytes, and the
    slack is 3 whenever r or s encodes in 31 bytes instead of 32. Measured here
    2026-09-26 over 20,000 signatures of distinct digests under one key:

        72 bytes  slack 1   9995   49.98%
        71 bytes  slack 2   9912   49.56%
        70 bytes  slack 3     93    0.47%   <- `<= 2` fails

    0.465%, so about 1 signature in 215. Five such assertions run per suite, which
    is why it surfaced as a test that passed 8 of 8 runs in isolation and failed
    once in a full run under pytest-randomly -- roughly a 2% chance per suite.

    A test that only meets this case on an unlucky seed is not covering it. So the
    key and the digest are FIXED at a pair that produces a 70-byte signature
    (found by the scan above), and the signature length is asserted first: if a
    future ecdsa release changes its nonce derivation, this fails saying the case
    is no longer reached, rather than quietly passing as a slack-2 test.
    """
    # Not a credential: a fixed 32-byte scalar, chosen only because digest 57 under
    # it signs to 70 bytes. Nothing is funded and nothing is broadcast.
    private_key = bytes.fromhex("123456789abcdef0112233445566778899aabbccddeeff00123456789abcdef0")
    public_key = public_key_for(private_key, True)
    secret = b"\x11" * 32
    # Only its LENGTH reaches the estimate, so the bytes are arbitrary.
    redeem_script = b"\x63" + b"\xa8" * 40

    signature = sign_digest(private_key, (57).to_bytes(32, "big"))
    assert len(signature) == MAX_DER_SIGNATURE_WITH_HASHTYPE - 3, (
        f"this key/digest pair no longer signs to a 70-byte signature (got {len(signature)}), so the "
        f"three-byte-slack case is NOT being exercised -- find another pair rather than deleting this"
    )

    estimate = estimated_script_sig_length(public_key, secret, redeem_script)
    actual = len(hashlock_script_sig(signature, public_key, secret, redeem_script))

    assert estimate - actual == 3
    assert estimate - actual == MAX_DER_SIGNATURE_WITH_HASHTYPE - len(signature)
    # And the estimate is still an UPPER bound, which is the property that matters:
    # the fee is computed from it, so the broadcast transaction is never larger
    # than the one the miner was paid for.
    assert actual < estimate


def test_signature_slack_refuses_a_signature_longer_than_the_estimate_reserved():
    """The direction that would be a real underpayment, not a rounding cost.

    signature_slack() asserts rather than returning a negative number, because a
    scriptSig whose signature exceeds MAX_DER_SIGNATURE_WITH_HASHTYPE means the fee
    was sized for FEWER bytes than are broadcast -- a spend that must confirm before
    a timelock expires, underpaying a miner. Silence there is the one failure mode
    worse than erring high.
    """
    oversized = push_of(b"\x30" * (MAX_DER_SIGNATURE_WITH_HASHTYPE + 1)) + b"\x51"

    with pytest.raises(AssertionError, match="sized for LESS than what is broadcast"):
        signature_slack(oversized)


def test_satoshi_conversion_round_trips_without_float_error():
    for coins in ("1.0", "0.00000001", "0.1", "12.34567891", "0.99983850"):
        assert satoshis_to_coins(coins_to_satoshis(coins)) == Decimal(coins)


def test_a_wif_that_is_not_a_wif_does_not_appear_in_the_error():
    """Never print a key. base58's own exception carries the offending string."""
    with pytest.raises(ValueError) as caught:
        decode_wif("NOTAWIF-but-shaped-like-a-secret-0000000000")
    assert "NOTAWIF" not in str(caught.value)


# --------------------------------------------------------------------------
# defect 2: reading a CONFIRMED contract back, on a node with no -txindex
# --------------------------------------------------------------------------


def test_the_contract_is_found_when_getrawtransaction_gives_the_measured_error(contract):
    """The exact failure both daemons produced on 2026-09-25, and the lookup survives it.

    FakeNode._rpc_getrawtransaction always raises `No such mempool transaction`
    -- which is what a default node does for every CONFIRMED transaction, and
    every contract a real swap redeems is confirmed. The old code called that
    one RPC first and gave up.
    """
    node = _node_for(contract)
    found = lookup_contract_output(node.rpc_call, contract["txid"], contract["vout"])
    assert found.value == CONTRACT_COINS
    assert found.script_pubkey_hex == p2sh_script_for(contract["redeem_script"]).hex()
    assert "gettxout" in found.route


def test_the_wallet_record_is_used_when_the_output_is_already_gone_from_gettxout(contract):
    """Route 3. gettxout answers null for a SPENT output, and the wallet still knows it."""
    node = _node_for(contract)
    del node.unspent[(contract["txid"], contract["vout"])]
    node.remember_wallet_transaction(
        contract["txid"],
        [
            (500, contract["destination"].p2pkh_script),
            (CONTRACT_SATOSHIS, p2sh_script_for(contract["redeem_script"])),
        ],
    )
    found = lookup_contract_output(node.rpc_call, contract["txid"], contract["vout"])
    assert found.value == CONTRACT_COINS
    assert "gettransaction" in found.route


def test_every_route_is_named_when_they_all_fail(contract):
    """Rule 14: an operator must see which four things were tried, not only the last."""
    node = _node_for(contract)
    del node.unspent[(contract["txid"], contract["vout"])]
    with pytest.raises(LookupError) as caught:
        lookup_contract_output(node.rpc_call, contract["txid"], contract["vout"], block_hash="ff" * 32)
    message = str(caught.value)
    for route in ("gettxout", "getrawtransaction with block hash", "gettransaction"):
        assert route in message
    assert "-txindex" in message, "the message must say the fix it deliberately does NOT take"


def test_an_output_that_is_not_this_contract_is_refused(contract):
    node = _node_for(contract)
    node.unspent[(contract["txid"], contract["vout"])] = (CONTRACT_COINS, "76a914" + "00" * 20 + "88ac")
    found = lookup_contract_output(node.rpc_call, contract["txid"], contract["vout"])
    with pytest.raises(ValueError, match="different output"):
        assert_output_pays_the_contract(found, contract["redeem_script"], "test")


# --------------------------------------------------------------------------
# defect 3: the import, on either wallet type, and never fatal
# --------------------------------------------------------------------------


def test_a_descriptor_wallet_gets_importdescriptors(contract):
    node = _node_for(contract)
    node.descriptors = True
    outcome = ensure_watch_only_import(node.rpc_call, "2Nfakeaddress")
    assert "importdescriptors" in outcome
    assert "importdescriptors" in node.methods
    assert "importaddress" not in node.methods


def test_a_legacy_wallet_gets_importaddress(contract):
    node = _node_for(contract)
    node.descriptors = False
    outcome = ensure_watch_only_import(node.rpc_call, "2Nfakeaddress")
    assert "importaddress" in outcome
    assert "importaddress" in node.methods
    assert "importdescriptors" not in node.methods


def test_the_measured_descriptor_wallet_refusal_is_not_fatal(contract):
    """`code=-4, Only legacy wallets are supported by this command`, and the swap goes on."""
    node = _node_for(contract)
    node.descriptors = False
    node.fail["importaddress"] = ONLY_LEGACY_WALLETS
    outcome = ensure_watch_only_import(node.rpc_call, "2Nfakeaddress")
    assert "FAILED" in outcome
    assert "not fatal" in outcome
    assert "Only legacy wallets" in outcome


def test_an_unknown_wallet_type_skips_the_import_rather_than_guessing(contract):
    node = _node_for(contract)
    node.fail["getwalletinfo"] = "RPC Error: method not found"
    outcome = ensure_watch_only_import(node.rpc_call, "2Nfakeaddress")
    assert "SKIPPED" in outcome
    assert "importaddress" not in node.methods
    assert "importdescriptors" not in node.methods


def test_create_contract_survives_an_import_that_refuses(contract, monkeypatch):
    """The whole of defect 3: a contract could not be created at all on Core 28.1.

    The import is attempted, refused with the measured error, and the contract
    is still funded. Before the fix this raised out of create_contract().
    """
    monkeypatch.delenv("BTC_HTLC_PRIVKEY", raising=False)
    node = _node_for(contract)
    node.descriptors = False
    node.fail["importaddress"] = ONLY_LEGACY_WALLETS
    client = BTCClient("http://127.0.0.1:18443/wallet/w", "u", "p")
    client.rpc_call = node.rpc_call
    funding_txid = "cc" * 32
    node.remember_wallet_transaction(
        funding_txid, [(CONTRACT_SATOSHIS, p2sh_script_for(contract["redeem_script"]))]
    )
    result = client.create_contract(
        amount_btc=CONTRACT_COINS,
        secret_hash=contract["secret_hash"].hex(),
        participant_address=contract["participant"].address,
        refund_address=contract["refund"].address,
        locktime=LOCKTIME,
    )
    assert result["txid"] == funding_txid
    assert result["vout"] == 0
    assert "importprivkey" not in node.methods, "the HTLC private key is no longer imported into the wallet"
    assert "sendtoaddress" in node.methods


# --------------------------------------------------------------------------
# defect 4: finding the output by script hex, on either daemon's field shape
# --------------------------------------------------------------------------


def test_the_output_is_found_on_a_daemon_with_no_addresses_field(contract):
    """Core 28.1 returns ['address', 'asm', 'desc', 'hex', 'type'] -- no `addresses`."""
    node = _node_for(contract)
    funding_txid = "cc" * 32
    node.remember_wallet_transaction(
        funding_txid,
        [
            (500, contract["destination"].p2pkh_script),
            (CONTRACT_SATOSHIS, p2sh_script_for(contract["redeem_script"])),
        ],
    )

    class Holder:
        rpc_call = staticmethod(node.rpc_call)

    index, outputs = wait_for_tx_output(
        Holder, funding_txid, p2sh_script_for(contract["redeem_script"]).hex(), max_wait=1
    )
    assert index == 1
    assert len(outputs) == 2


def test_find_output_by_script_is_indifferent_to_the_address_field():
    outputs = [(Decimal("0.5"), "76a914" + "11" * 20 + "88ac"), (Decimal("1.0"), "a914" + "22" * 20 + "87")]
    assert find_output_by_script(outputs, "a914" + "22" * 20 + "87") == 1
    assert find_output_by_script(outputs, "a914" + "33" * 20 + "87") is None


# test_the_address_is_read_from_either_daemons_field_shape MOVED to
# tests/test_deposit_vout_matching.py on 2026-09-25, with the code it tests.
# address_of() lived in modules/htlc_rpc.py with no production caller at all,
# and the thing that needed it was chains/base.py on the brokered Flask path --
# a FOURTH copy of the same defect, reading `addresses` alone. The decision now
# lives in swap_terminal/script_pub_key.py and its tests live beside the caller
# that actually exercises it. Rule 2: when something moves, its test moves with
# it or changes to pin the stronger invariant; the replacement does both.


# --------------------------------------------------------------------------
# defect 5: the fee, which only became reachable once signing worked
# --------------------------------------------------------------------------


def test_the_old_flat_fee_was_never_anywhere_near_the_refusal_ceiling():
    """THE FIFTH "DEFECT" WAS AN ARITHMETIC ERROR, AND THIS IS WHERE THAT IS PINNED.

    The brief for this work, and this repository's own regtest harness, both
    said a flat 0.0001 over a ~250-byte redeem is "roughly 0.4 coin/kvB, about
    four times" sendrawtransaction's 0.10 default maxfeerate, and that fixing
    the signing defect would therefore move the failure to `absurdly-high-fee`.

    It is 0.0004 coin/kvB. A thousandth of the claim, and 250 times UNDER the
    ceiling rather than four times over. Asserted here so the figure cannot be
    copied forward again from a comment -- which is how it travelled the first
    time.
    """
    assert effective_rate_coin_per_kvb(Decimal("0.0001"), 250) == Decimal("0.00040000")
    assert effective_rate_coin_per_kvb(Decimal("0.0001"), 323) == Decimal("0.00030960")
    assert effective_rate_coin_per_kvb(Decimal("0.0001"), 323) < BROADCAST_CEILING_COIN_PER_KVB
    # And the node would have accepted it: the guard does not fire.
    assert_within_broadcast_ceiling("BTC", Decimal("0.0001"), 323)
    # A fee a THOUSAND times larger is what the claim actually describes, and
    # THAT is refused -- which is what the guard is for.
    with pytest.raises(ValueError, match="absurdly-high-fee"):
        assert_within_broadcast_ceiling("BTC", Decimal("0.1"), 250)


def test_the_new_fee_is_what_the_header_says_it_is():
    """The numbers in modules/htlc_fee.py's header, asserted so they cannot drift.

    A measurement written only in prose ages -- this file exists partly because
    one did. These are the figures the operator reads when deciding whether the
    fee rule is acceptable, and two of the three are "unchanged".
    """
    assert redeem_miner_fee("BTC", 323) == Decimal("0.0001"), "a typical BTC redeem pays exactly what it always did"
    assert redeem_miner_fee("LTC", 357) == Decimal("0.00010710"), "LTC's second output puts it over the crossover"
    assert redeem_miner_fee("GRC", 360) == Decimal("0.01"), "GRC is unchanged, on the chain nobody can test"


def test_the_crossover_between_the_floor_and_the_rate_is_where_the_header_says():
    """Below 334 bytes the old flat fee stands; above it, the fee follows the size."""
    assert redeem_miner_fee("BTC", 333) == minimum_fee_coin("BTC")
    assert redeem_miner_fee("BTC", 334) > minimum_fee_coin("BTC")


def test_the_floor_binds_on_a_small_transaction_and_the_rate_on_a_large_one():
    """The case a flat fee gets wrong: a large transaction paying a constant drifts
    toward the minimum RELAY fee, and a redeem that will not relay is a lost swap."""
    assert redeem_miner_fee("BTC", 100) == minimum_fee_coin("BTC")
    assert redeem_miner_fee("BTC", 10_000) == Decimal("0.003")
    assert effective_rate_coin_per_kvb(Decimal("0.0001"), 10_000) == Decimal("0.00001000"), (
        "the flat fee at that size is exactly Bitcoin Core's default minimum relay fee -- the edge of not relaying"
    )


def test_no_plausible_redeem_size_trips_the_broadcast_ceiling():
    """Sized from the transaction, the fee can never be refused for being too high.

    Swept rather than spot-checked, because the floor and the rate cross over
    somewhere in this range and the crossover is where a bug would live.
    """
    for asset in ("BTC", "LTC", "GRC"):
        for size in range(150, 2_000, 7):
            fee = redeem_miner_fee(asset, size)
            assert effective_rate_coin_per_kvb(fee, size) <= BROADCAST_CEILING_COIN_PER_KVB


def test_the_fee_rate_can_be_raised_for_a_congested_chain(monkeypatch):
    monkeypatch.setenv("SWAP_REDEEM_FEE_COIN_PER_KVB_BTC", "0.002")
    assert fee_rate_coin_per_kvb("BTC") == Decimal("0.002")
    assert redeem_miner_fee("BTC", 323) == Decimal("0.00064600")
    assert redeem_miner_fee("BTC", 323) > minimum_fee_coin("BTC"), "an override must be able to beat the floor"


def test_a_malformed_override_raises_rather_than_silently_paying_the_old_rate(monkeypatch):
    """An operator who sets this is doing it because a swap is at risk."""
    monkeypatch.setenv("SWAP_REDEEM_FEE_COIN_PER_KVB_BTC", "fast please")
    with pytest.raises(ValueError, match="not a decimal number"):
        redeem_miner_fee("BTC", 323)
    monkeypatch.setenv("SWAP_REDEEM_FEE_COIN_PER_KVB_BTC", "1.0")
    with pytest.raises(ValueError, match="absurdly-high-fee"):
        redeem_miner_fee("BTC", 323)


def test_the_fee_paid_is_the_fee_the_bytes_encode(contract):
    """Measured off the parsed transaction, not off the arithmetic that built it."""
    node = _node_for(contract)
    spend = build_hashlock_spend(
        asset="BTC",
        rpc_call=node.rpc_call,
        contract_txid=contract["txid"],
        contract_vout=contract["vout"],
        contract_value=CONTRACT_COINS,
        redeem_script=contract["redeem_script"],
        secret=contract["secret"],
        wif=contract["participant"].wif,
        destination_address=contract["destination"].address,
    )
    parsed = parse_transaction(bytes.fromhex(spend.raw_hex), contract["txid"], contract["vout"])
    assert CONTRACT_SATOSHIS - parsed.output_total == coins_to_satoshis(spend.miner_fee)
    assert spend.destination_amount == CONTRACT_COINS - spend.miner_fee


def test_a_contract_too_small_to_cover_the_fee_is_refused(contract):
    node = _node_for(contract)
    with pytest.raises(ValueError, match="nothing would be left"):
        build_hashlock_spend(
            asset="BTC",
            rpc_call=node.rpc_call,
            contract_txid=contract["txid"],
            contract_vout=contract["vout"],
            contract_value=Decimal("0.00001"),
            redeem_script=contract["redeem_script"],
            secret=contract["secret"],
            wif=contract["participant"].wif,
            destination_address=contract["destination"].address,
        )


def test_the_destination_may_not_also_be_the_platform_fee_address(contract):
    """The node would merge the two outputs and the asserted amounts would be wrong."""
    node = _node_for(contract)
    with pytest.raises(ValueError, match="also an extra output"):
        build_hashlock_spend(
            asset="LTC",
            rpc_call=node.rpc_call,
            contract_txid=contract["txid"],
            contract_vout=contract["vout"],
            contract_value=CONTRACT_COINS,
            redeem_script=contract["redeem_script"],
            secret=contract["secret"],
            wif=contract["participant"].wif,
            destination_address=contract["destination"].address,
            extra_outputs={contract["destination"].address: Decimal("0.0025")},
        )


# --------------------------------------------------------------------------
# the three real clients, driven end to end against the fake node
# --------------------------------------------------------------------------


def _drive_redeem(client, node, contract):
    return client.redeem_contract(
        contract["txid"],
        contract["vout"],
        contract["redeem_script"],
        contract["secret"],
        contract["participant"].wif,
        contract["destination"].address,
    )


@pytest.mark.parametrize("asset", ["BTC", "LTC", "GRC"])
def test_every_client_redeems_a_confirmed_contract_without_asking_the_wallet_to_sign(asset, contract, monkeypatch):
    """All three, one test, because all three had the same four defects.

    Asserts three things at once, and the second is the one that used to be
    impossible: the spend is broadcast, NO signrawtransaction* call is made
    (the wallet cannot sign a conditional script and is no longer asked), and
    the broadcast transaction's scriptSig carries the preimage.
    """
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", contract["platform"].address)
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", contract["platform"].address)
    prefix = GRIDCOIN_PREFIX if asset == "GRC" else VERSION_2_PREFIX
    node = _node_for(contract, prefix=prefix)
    if asset == "BTC":
        client = BTCClient("http://127.0.0.1:18443/wallet/w", "u", "p")
    elif asset == "LTC":
        client = LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p")
    else:
        client = GRCClient("http://127.0.0.1:15715", "u", "p", wallet_passphrase="")
    client.rpc_call = node.rpc_call

    txid = _drive_redeem(client, node, contract)

    assert txid == "dd" * 32
    assert len(node.broadcast) == 1
    for method in node.methods:
        assert not method.startswith("signrawtransaction"), f"{asset} asked the wallet to sign: {method}"
    parsed = parse_transaction(bytes.fromhex(node.broadcast[0]), contract["txid"], contract["vout"])
    script_sig = parsed.inputs[0][1]
    assert push_of(contract["secret"]) in script_sig, f"{asset} did not push the preimage"
    # BTC pays one output; LTC and GRC also pay the platform fee. That row of
    # the divergence table is deliberately not merged -- see the client headers.
    assert len(parsed.outputs) == (1 if asset == "BTC" else 2)

    # THE FEE THE TRANSACTION ENCODES IS THE FEE RULE APPLIED TO THE
    # TRANSACTION'S OWN SIZE. Not to an estimate, not to a constant. This is
    # the assertion that makes "sized from the actual transaction" mean
    # something -- ADDED AFTER A MUTATION TEST showed that sizing the fee from
    # a made-up length broke nothing, because on BTC the floor binds at any
    # ordinary size and hides the difference.
    size = len(bytes.fromhex(node.broadcast[0]))
    # Everything the contract does not pay out went to a miner. Read off the
    # bytes rather than off the client's own arithmetic.
    paid = CONTRACT_SATOSHIS - parsed.output_total
    assert paid >= coins_to_satoshis(redeem_miner_fee(asset, size)), (
        f"{asset}: the {size}-byte transaction pays {paid} satoshis, under the fee rule for its own size"
    )
    # THE UPPER END IS AN EQUALITY, NOT A TOLERANCE. The fee is sized before the
    # signature exists, from an upper bound that reserves the maximum DER length;
    # the broadcast transaction is shorter by exactly the bytes that signature did
    # not use, and signature_slack() reads that off the scriptSig. So the fee must
    # be the fee rule applied to (actual size + that slack) and nothing else.
    #
    # This line read `size + 2` until 2026-09-26, when a seeded full-suite run
    # failed with "LTC: paid 10710 satoshis over 354 bytes" -- 30 sat/byte over 357,
    # slack 3, because r encoded in 31 bytes. A tolerance here was a claim about a
    # distribution; an equality is a claim about this transaction.
    slack = signature_slack(script_sig)
    assert paid == coins_to_satoshis(redeem_miner_fee(asset, size + slack)), (
        f"{asset}: paid {paid} satoshis over {size} bytes with {slack} bytes of signature slack, which is "
        f"not the fee rule applied to the {size + slack} bytes the fee was sized for"
    )


class _FakeHTTPResponse:
    """What `requests.post` hands back, carrying only what rpc_call reads off it.

    `text` is rendered the way a daemon renders it, because all three clients
    log `response.text` and a test that made it empty would not be measuring
    the line that prints it.
    """

    status_code = 200

    def __init__(self, result=None, error=None):
        self._body = {"result": result, "error": error, "id": "atomic-swap"}

    @property
    def text(self) -> str:
        return json_dumps(self._body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        return None


def _post_through(node):
    """A `requests.post` stand-in that answers out of the FakeNode.

    THIS IS THE WHOLE POINT OF THE TEST BELOW, so it is worth saying why the
    obvious shortcut is wrong. Every other test in this file replaces the
    client's `rpc_call` with the node's -- which is right when the question is
    what the client ASKS. It is useless when the question is what the client
    PRINTS, because `rpc_call` is the method that does the printing and
    replacing it deletes the code under test. Stubbing one level lower, at
    `requests.post`, leaves the real `rpc_call` running: its logging, its error
    branches and its response handling all execute.
    """

    # `json` shadows the stdlib module's name on purpose: it is
    # requests.post's own keyword and all three clients pass it by that
    # name, so the stand-in has to accept it by that name. json.dumps is
    # imported as json_dumps at the top of this file for the same reason.
    def post(url, json=None, auth=None, timeout=None, **_kwargs):
        try:
            result = node.rpc_call(json["method"], json["params"])
        except Exception as exc:  # noqa: BLE001 -- checked: the FakeNode signals a daemon-side refusal by raising, and a real daemon signals it by answering 200 with an `error` object. Translating one into the other is this stub's job; nothing is swallowed, because the error text is handed straight back to rpc_call, which raises on it.
            return _FakeHTTPResponse(error={"code": -1, "message": str(exc)})
        return _FakeHTTPResponse(result=result)

    return post


@pytest.mark.parametrize("asset", ["BTC", "LTC", "GRC"])
def test_no_client_logs_the_preimage(asset, contract, caplog, capfd, monkeypatch):
    """The single most dangerous value in this tree, driven through the REAL rpc_call.

    THIS TEST WAS STRUCTURALLY UNABLE TO FAIL UNTIL 2026-09-25. It did
    `client.rpc_call = node.rpc_call` -- replacing the only method that ever
    handles the raw transaction, and therefore the only method that could leak
    it. It then asserted that a redeem driven through a method the client does
    not own printed no preimage, which it could not have done.

    WHAT IT WAS NOT CATCHING, measured on this branch by driving the real
    BTCClient.redeem_contract() with requests.post stubbed and no logging
    configuration of its own: the 32-byte preimage appeared in full on stderr,
    inside

        RPC Call Payload: {... 'method': 'sendrawtransaction' ...}

    immediately followed by `51 4c5e` -- OP_1, OP_PUSHDATA1 94 -- which is the
    tail of the scriptSig. `rpc_call` logged the whole payload one line BEFORE
    requests.post, and each client did logger.setLevel(DEBUG) plus a
    StreamHandler AT IMPORT, so it reached a terminal with no application
    opt-in at all.

    So this now asserts on BOTH sinks, because they fail for different reasons
    and a fix could close one and leave the other:

      caplog  the log RECORD exists at all, wherever it would have been sent.
      capfd   it reached the process's stderr, which is what an operator sees
              and pastes. This is the assertion that the import-time handler
              was the delivery mechanism.

    The sibling test_no_module_here_installs_a_logging_handler pins the CAUSE;
    this pins the outcome. Rule 19: a check on the symptom alone is a patch.
    """
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", contract["platform"].address)
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", contract["platform"].address)
    prefix = GRIDCOIN_PREFIX if asset == "GRC" else VERSION_2_PREFIX
    node = _node_for(contract, prefix=prefix)
    modules = {"BTC": btc_module, "LTC": ltc_module, "GRC": grc_module}
    clients = {
        "BTC": lambda: BTCClient("http://127.0.0.1:18443/wallet/w", "u", "p"),
        "LTC": lambda: LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p"),
        # A NON-EMPTY passphrase, unlike every other test here, because
        # ensure_fully_unlocked() returns early on an empty one and the
        # `walletpassphrase` call -- which carries the operator's wallet
        # passphrase as parameter 0 -- would never be made. That call goes
        # through the same rpc_call and used to print it, so the GRC case
        # carries two secrets and both are asserted below.
        "GRC": lambda: GRCClient("http://127.0.0.1:15715", "u", "p", wallet_passphrase=GRC_WALLET_UNLOCK_LITERAL),
    }
    # Patched on the CLIENT'S module, not on `requests` globally: each client
    # does `import requests` and calls `requests.post`, so the attribute the
    # client resolves is the one on its own module's `requests` reference.
    monkeypatch.setattr(modules[asset].requests, "post", _post_through(node))
    # ensure_fully_unlocked() sleeps three SECONDS after unlocking (an
    # interface, not a report -- rule 6). Nothing here is racing a daemon.
    monkeypatch.setattr(grc_module.time, "sleep", lambda _seconds: None)

    capfd.readouterr()  # discard anything earlier in the session
    client = clients[asset]()
    with caplog.at_level("DEBUG"):
        _drive_redeem(client, node, contract)
    on_stderr = capfd.readouterr().err

    secret_hex = contract["secret"].hex()
    for where, text in (("the log records", caplog.text), ("stderr", on_stderr)):
        assert secret_hex not in text, f"the HTLC preimage reached {where}"
        assert secret_hex.upper() not in text, f"the HTLC preimage reached {where}, upper-cased"
        assert contract["participant"].wif not in text, f"the signing key reached {where}"
        if asset == "GRC":
            assert GRC_WALLET_UNLOCK_LITERAL not in text, f"the wallet passphrase reached {where}"

    # The redaction has to leave the line USEFUL, or the next person puts the
    # payload back. The METHOD is never a secret and is the half an operator
    # reading a failure actually needs.
    assert "sendrawtransaction" in caplog.text
    assert node.broadcast, f"{asset} did not broadcast, so this proved nothing about the broadcast's payload"


def test_no_module_here_installs_a_logging_handler():
    """The CAUSE, pinned separately from the leak it delivered.

    A library module that calls setLevel() and attaches a StreamHandler at
    import decides logging policy for every program that imports it, and the
    application cannot turn it back off short of reaching into the logger
    object. All three clients did exactly that, at DEBUG, which is why the
    payload line above reached a terminal by default rather than only in a
    debugging session.

    modules/utils.py had the identical pair removed on 2026-09-24 for the
    identical reason, and tests/test_secrets_are_not_logged.py has held it ever
    since. This is that assertion extended to the three files that still had
    it -- one rule, and now it is checked everywhere it applies rather than in
    the one place somebody happened to look (rule 8).
    """
    for name in (
        "modules.atomic_btc_client",
        "modules.atomic_ltc_client",
        "modules.atomic_grc_client",
        "modules.htlc_rpc",
        "modules.htlc_spend",
        "modules.htlc_fee",
        "modules.atomic_htlc_scripts",
        "modules.atomic_swapper",
        "modules.utils",
    ):
        module_logger = logging.getLogger(name)
        assert module_logger.handlers == [], f"{name} attaches its own logging handler at import"
        assert module_logger.level == logging.NOTSET, f"{name} sets its own logging level at import"


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        # The leak, and what replaced it. 321 bytes is an ordinary BTC redeem.
        ("sendrawtransaction", ["ab" * 321], "sendrawtransaction params=[<raw tx, 321 bytes>]"),
        # Both signing routes, neither of which this repository still calls --
        # listed so that reaching for one again does not reintroduce the leak.
        ("signrawtransactionwithwallet", ["ab" * 10], "signrawtransactionwithwallet params=[<raw tx, 10 bytes>]"),
        (
            "signrawtransactionwithkey",
            ["ab" * 10, ["cPrivateKeyWIF"]],
            "signrawtransactionwithkey params=[<raw tx, 10 bytes>, <signing key, redacted>]",
        ),
        # The second secret in the same line, on the GRC client only.
        ("walletpassphrase", ["hunter2", 120], "walletpassphrase params=[<wallet passphrase, redacted>, 120]"),
        # And everything else stays verbatim, or the line stops being useful.
        ("gettxout", ["ab" * 32, 1, True], f"gettxout params=['{'ab' * 32}', 1, True]"),
        ("getblockcount", [], "getblockcount params=[]"),
    ],
)
def test_describe_rpc_payload_redacts_exactly_the_secret_bearing_parameters(method, params, expected):
    """The decision, called with seeded inputs (rule 10).

    Asserted on the whole rendered string rather than on "the secret is
    absent", because absence alone is satisfied by printing nothing, and a line
    that says nothing is rule 14's defect traded for rule 12's.
    """
    assert describe_rpc_payload(method, params) == expected


def test_describe_rpc_payload_never_echoes_any_part_of_a_raw_transaction():
    """A prefix of a preimage is a preimage with a head start on it.

    The summary carries the SIZE and nothing else. This is the assertion that
    would fail if somebody later made the line "more useful" by showing the
    first and last few bytes -- which is safe for regtest/console.py's
    describe_script_sig(), where the caller has already decided the audience,
    and is not safe here, where the audience is a default-on log.
    """
    raw = "deadbeef" * 80
    rendered = describe_rpc_payload("sendrawtransaction", [raw])
    assert "deadbeef" not in rendered
    assert "dead" not in rendered
    assert rendered == "sendrawtransaction params=[<raw tx, 320 bytes>]"


@pytest.mark.parametrize("asset", ["BTC", "LTC", "GRC"])
def test_no_client_reads_a_confirmed_contract_with_getrawtransaction_alone(asset, contract, monkeypatch):
    """Defect 2 per client: the first RPC is no longer the one that cannot answer."""
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", contract["platform"].address)
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", contract["platform"].address)
    prefix = GRIDCOIN_PREFIX if asset == "GRC" else VERSION_2_PREFIX
    node = _node_for(contract, prefix=prefix)
    clients = {
        "BTC": lambda: BTCClient("http://127.0.0.1:18443/wallet/w", "u", "p"),
        "LTC": lambda: LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p"),
        "GRC": lambda: GRCClient("http://127.0.0.1:15715", "u", "p", wallet_passphrase=""),
    }
    client = clients[asset]()
    client.rpc_call = node.rpc_call
    _drive_redeem(client, node, contract)
    # The fake node raises the measured `No such mempool transaction` for every
    # getrawtransaction, so a redeem that completed cannot have depended on one.
    assert node.broadcast, f"{asset} did not broadcast"
    assert node.methods[0] == "gettxout" if asset != "GRC" else True


# --------------------------------------------------------------------------
# dust: the other end of the amount a redeem may pay
# --------------------------------------------------------------------------
#
# There was NO dust check on this path until 2026-09-25. The only amount guard
# was `destination_amount <= 0`, and the LTC and GRC platform fee is a fixed
# 0.25% of the contract, so it shrinks with the contract while a dust limit
# does not. The harness could not catch it: CONTRACT_AMOUNT is "1.0", which
# puts the platform fee 85x over the limit, and regtest does not enforce
# standardness anyway.

P2PKH_SCRIPT = bytes.fromhex("76a914" + "11" * 20 + "88ac")
P2SH_SCRIPT = bytes.fromhex("a914" + "11" * 20 + "87")
P2WPKH_SCRIPT = bytes.fromhex("0014" + "11" * 20)
P2WSH_SCRIPT = bytes.fromhex("0020" + "11" * 32)


@pytest.mark.parametrize(
    ("asset", "script", "expected"),
    [
        # Bitcoin Core's own published dust limits, which is what makes this a
        # table with an external referent rather than a restatement of the
        # implementation: 546 for P2PKH is the number every Bitcoin wallet
        # quotes, and 294 for P2WPKH is the witness-discounted one.
        ("BTC", P2PKH_SCRIPT, 546),
        ("BTC", P2SH_SCRIPT, 540),
        ("BTC", P2WPKH_SCRIPT, 294),
        ("BTC", P2WSH_SCRIPT, 330),
        # Litecoin Core 0.21.4's DUST_RELAY_TX_FEE is ten times Bitcoin's, so
        # every row is exactly ten times the row above it. That is the fact
        # that turns the platform fee into a live defect.
        ("LTC", P2PKH_SCRIPT, 5460),
        ("LTC", P2SH_SCRIPT, 5400),
        ("LTC", P2WPKH_SCRIPT, 2940),
        ("LTC", P2WSH_SCRIPT, 3300),
        # Gridcoin has no dust rule at all -- read from its policy source, not
        # assumed from Bitcoin's. Its IsStandardTx() rejects an output only for
        # `nValue == 0`, so the universal floor of one satoshi is the whole
        # rule and it is the same for every script shape.
        ("GRC", P2PKH_SCRIPT, 1),
        ("GRC", P2WPKH_SCRIPT, 1),
        ("GRC", P2WSH_SCRIPT, 1),
    ],
)
def test_the_dust_table_reproduces_each_chains_published_limit(asset, script, expected):
    """The decision, called with seeded inputs (rule 10).

    Derived from the output's SHAPE -- Core's (txout bytes + spend bytes) x
    rate / 1000 -- rather than from one constant per chain, because a P2WPKH
    output and a P2PKH one have different limits on the same chain and the
    platform fee is the first and the destination is the second.
    """
    assert dust_threshold_satoshis(asset, script) == expected


@pytest.mark.parametrize(
    ("script", "witness"),
    [
        (P2WPKH_SCRIPT, True),
        (P2WSH_SCRIPT, True),
        # OP_1 <32>, a taproot output: a witness program at version 1.
        (bytes.fromhex("5120" + "11" * 32), True),
        (P2PKH_SCRIPT, False),
        (P2SH_SCRIPT, False),
        # OP_0 followed by a push of ONE byte. Not a witness program: BIP141
        # requires 2 to 40, and getting this wrong the other way would apply
        # the witness discount to something that does not earn it.
        (bytes.fromhex("000111"), False),
        # OP_RETURN, which is neither.
        (bytes.fromhex("6a0411111111"), False),
        (b"", False),
    ],
)
def test_is_witness_program_reads_the_bytes(script, witness):
    """Which spend size the threshold uses, and it is a 2x difference on the answer."""
    assert is_witness_program(script) is witness


def test_a_small_ltc_redeem_refuses_before_signing_because_the_platform_fee_is_dust(contract, monkeypatch):
    """THE MEASUREMENT, through the real LTCClient.redeem_contract().

    A 0.01 LTC contract builds outputs of [986790, 2500] satoshis. The 2500 is
    the 0.25% platform fee; PLATFORM_FEE_LTC_ADDRESS's shipped default is a
    bech32 `tltc1q...`, so that output is P2WPKH and Litecoin Core 0.21.4's
    dust limit for one is 2,940. Before 2026-09-25 this was signed and handed
    to `sendrawtransaction`, which refuses the WHOLE transaction with
    `code=-26 dust` -- and no client here implements a refund, so the redeemer
    loses their own already-funded leg.

    The assertion that matters is not the exception, it is
    `node.broadcast == []` plus the absence of sendrawtransaction from the
    call list: the refusal has to arrive while a different decision is still
    possible, which means before a signature exists.
    """
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", contract["platform"].address)
    node = _node_for(
        contract,
        platform_script=p2wpkh_script(contract["platform"]),
        value=Decimal("0.01"),
    )
    client = LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p")
    client.rpc_call = node.rpc_call

    with pytest.raises(ValueError) as raised:
        _drive_redeem(client, node, contract)

    message = str(raised.value)
    assert "DUST" in message
    assert "2500 satoshis" in message, message
    assert "2940 satoshis" in message, message
    # The arithmetic, not just the verdict (rule 14).
    assert "31-byte output + 67-byte witness spend" in message, message
    assert "30000 sat/kvB" in message, message
    # And it says what it will NOT do on the operator's behalf (rule 16).
    assert "operator's call" in message, message

    assert node.broadcast == [], "a dust transaction was broadcast anyway"
    assert "sendrawtransaction" not in node.methods, "the refusal arrived after signing, which is too late"


def test_a_small_btc_redeem_refuses_when_the_destination_alone_is_dust(contract):
    """BTC charges NO platform fee and has the identical hole.

    The destination absorbs the miner fee, so a contract only a little above
    the fee leaves a destination below 546. 0.00010500 minus the 0.0001 floor
    is 500 satoshis, which is dust to a P2PKH output on Bitcoin.

    This is the case with no platform fee in it at all, which is why it is a
    separate test: a reader could otherwise conclude the defect was the fee.
    """
    node = _node_for(contract, value=Decimal("0.000105"))
    client = BTCClient("http://127.0.0.1:18443/wallet/w", "u", "p")
    client.rpc_call = node.rpc_call

    with pytest.raises(ValueError) as raised:
        _drive_redeem(client, node, contract)

    message = str(raised.value)
    assert "DUST" in message
    assert "500 satoshis" in message, message
    assert "546 satoshis" in message, message
    assert node.broadcast == []


def test_gridcoin_refuses_a_zero_value_output_and_nothing_larger():
    """Gridcoin's ONLY output rule, asserted on the decision rather than end to end.

    `IsStandardTx()` in src/policy/policy.cpp rejects an output for
    `nValue == 0` and for nothing else, so GRC's threshold is one satoshi for
    every script shape.

    WHY THIS DOES NOT DRIVE redeem_contract(). Measured: the GRC dust guard is
    currently UNREACHABLE through that path. GRC's miner-fee floor is 0.01 coin
    = 1,000,000 satoshis, so any contract large enough to cover the fee at all
    carries a 0.25% platform fee of at least 2,500 -- and any contract too
    small is already refused by the `nothing would be left to send` guard
    before an output exists. An end-to-end test would therefore pass on the
    OTHER refusal while claiming to measure this one, which is the "an
    assertion that accepts either outcome" defect this repository's harness was
    rewritten to remove.

    That unreachability is a property of the 0.01 fee floor, not of the rule.
    It stops holding the day somebody lowers the floor, which is exactly when
    the guard is needed and exactly why it is written and tested now.
    """
    # Zero is refused, on every script shape, because Gridcoin refuses it.
    for script in (P2PKH_SCRIPT, P2SH_SCRIPT, P2WPKH_SCRIPT):
        with pytest.raises(ValueError, match="DUST"):
            assert_no_output_is_dust("GRC", [(0, script)])
    # And one satoshi is not, which is the half that says this is Gridcoin's
    # rule rather than Bitcoin's applied to the wrong chain: the same output
    # on BTC or LTC IS dust, and the same call says so.
    assert_no_output_is_dust("GRC", [(1, P2PKH_SCRIPT)])
    for asset in ("BTC", "LTC"):
        with pytest.raises(ValueError, match="DUST"):
            assert_no_output_is_dust(asset, [(1, P2PKH_SCRIPT)])


def test_an_ordinary_contract_is_not_refused_by_the_dust_guard(contract, monkeypatch):
    """The guard must not break the path that works.

    Rule 19's test for a patch is whether it stops the cause or the symptom;
    the matching hazard for a new refusal is that it refuses everything. At
    CONTRACT_COINS the LTC platform fee is 250,000 satoshis, which is 85 times
    the P2WPKH limit, so this is the ordinary case with the awkward script
    shape.
    """
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", contract["platform"].address)
    node = _node_for(contract, platform_script=p2wpkh_script(contract["platform"]))
    client = LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p")
    client.rpc_call = node.rpc_call

    _drive_redeem(client, node, contract)
    assert node.broadcast, "the ordinary redeem stopped working"


# --------------------------------------------------------------------------
# the confirmed-contract fallback, on a WITNESS-serialized funding transaction
# --------------------------------------------------------------------------
#
# Route 2 of read_transaction_outputs() and route 3 of lookup_contract_output()
# are documented as "the one that keeps working after the funding transaction
# is confirmed, which is exactly when route 1 stops". Until 2026-09-25 they
# parsed the wallet record's hex in process with htlc_spend.parse_transaction(),
# which cannot read a segwit serialization and says so in its own error text.
#
# That reasoning holds for the SPEND -- an HTLC P2SH input has no witness -- and
# not for the FUNDING transaction, which spends the operator's own coins, held
# as bech32 P2WPKH on a default Core 28.1 or Litecoin 0.21.4 wallet. So the
# route that exists for the confirmed case was dead on exactly the wallets
# everybody has.


def test_the_spend_parser_still_refuses_a_witness_serialization(contract):
    """The premise, asserted rather than assumed.

    If parse_transaction() ever learns to read a witness serialization, the two
    tests below stop measuring anything and this one says so first.
    """
    raw = _manual_segwit_transaction("aa" * 32, 0, [(CONTRACT_SATOSHIS, p2sh_script_for(contract["redeem_script"]))])
    with pytest.raises(TransactionLayoutError):
        parse_transaction(bytes.fromhex(raw))


def test_read_transaction_outputs_reads_a_witness_serialized_funding_transaction(contract):
    """Route 2, on the shape a default wallet actually produces.

    getrawtransaction answers the measured `No such mempool transaction`, so
    only route 2 can answer -- which is the confirmed case this route exists
    for.
    """
    node = _node_for(contract)
    funding_txid = "cc" * 32
    node.remember_wallet_transaction(
        funding_txid,
        [
            (500, contract["destination"].p2pkh_script),
            (CONTRACT_SATOSHIS, p2sh_script_for(contract["redeem_script"])),
        ],
        witness=True,
    )
    outputs = read_transaction_outputs(node.rpc_call, funding_txid)
    assert len(outputs) == 2
    assert outputs[1] == (CONTRACT_COINS, p2sh_script_for(contract["redeem_script"]).hex())
    assert "decoderawtransaction" in node.methods, "the daemon was not asked to decode its own serialization"


def test_wait_for_tx_output_finds_a_confirmed_witness_funding_before_its_deadline(contract):
    """THE SYMPTOM, end to end, and it is the one defect 4 was fixed to remove.

    create_contract() broadcasts and then polls. Route 1 answers while the
    funding is unconfirmed; the moment a block lands inside the poll window
    route 1 returns `code=-5, No such mempool transaction` and -- before this
    fix -- route 2 could not parse the wallet's hex. wait_for_tx_output() then
    polled to its full 300-SECOND deadline and raised, with the coins already
    at the P2SH and the caller never learning the vout.

    max_wait is 1 second here only so a regression fails in one second rather
    than in five minutes. The assertion is that it does not time out at all.
    """
    node = _node_for(contract)
    funding_txid = "cc" * 32
    node.remember_wallet_transaction(
        funding_txid,
        [
            (500, contract["destination"].p2pkh_script),
            (CONTRACT_SATOSHIS, p2sh_script_for(contract["redeem_script"])),
        ],
        witness=True,
    )

    class Holder:
        rpc_call = staticmethod(node.rpc_call)

    index, outputs = wait_for_tx_output(
        Holder, funding_txid, p2sh_script_for(contract["redeem_script"]).hex(), max_wait=1
    )
    assert index == 1
    assert len(outputs) == 2


def test_lookup_contract_output_reads_a_witness_serialized_contract_back(contract):
    """Route 3, the same fix on the redeem side.

    gettxout answers null for an output that has already been SPENT, which is
    what sends the lookup to the wallet's own record -- and a contract funded
    by a transaction that also spent the operator's P2WPKH change carries a
    witness.
    """
    node = _node_for(contract)
    del node.unspent[(contract["txid"], contract["vout"])]
    node.remember_wallet_transaction(
        contract["txid"],
        [
            (500, contract["destination"].p2pkh_script),
            (CONTRACT_SATOSHIS, p2sh_script_for(contract["redeem_script"])),
        ],
        witness=True,
    )
    found = lookup_contract_output(node.rpc_call, contract["txid"], contract["vout"])
    assert found.value == CONTRACT_COINS
    assert found.script_pubkey_hex == p2sh_script_for(contract["redeem_script"]).hex()
    # The confirmation count is spliced in from the WALLET RECORD, because
    # decoderawtransaction is handed bytes and has no chain position to report.
    # Losing it would turn "three confirmations" into "this route does not
    # report them", which _as_int_or_none() keeps as different answers.
    assert found.confirmations == 3
    assert "decoderawtransaction" in found.route


def test_both_platform_fee_clients_read_one_rate_from_one_place():
    """ONE rule, two spellings, two files -- until 2026-09-25 (rule 8).

        atomic_ltc_client.py   (Decimal("0.25") / Decimal(100)) * found.value
        atomic_grc_client.py   Decimal("0.0025") * found.value

    They agreed, which is what makes this rule 8's shape rather than a bug
    report: two copies agree on the day they are written and drift from then
    on, invisibly, because each reads correctly in its own file. The GRC copy's
    COMMENT had already drifted -- it said "2.5% of the total amount" beside an
    expression computing 0.25% -- and that was caught only because somebody
    read the two side by side.

    Asserted as equality between the chains and against the arithmetic, not
    against a restated constant: `platform_fee_coin("LTC", x) == 0.0025 * x`
    checked against a literal would pass if both the table and this line were
    changed together, which is the failure mode a shared table is supposed to
    make impossible.
    """
    for value in (Decimal("1.0"), Decimal("0.01"), Decimal("123.456789"), Decimal("0.00000001")):
        assert platform_fee_coin("LTC", value) == platform_fee_coin("GRC", value)
        # The old LTC spelling and the old GRC spelling, both still true of the
        # survivor. If either stops being true, one client's payout moved.
        assert platform_fee_coin("LTC", value) == ((Decimal("0.25") / Decimal(100)) * value).quantize(
            Decimal("0.00000001")
        )
        assert platform_fee_coin("GRC", value) == (Decimal("0.0025") * value).quantize(Decimal("0.00000001"))


def test_btc_is_absent_from_the_platform_fee_table_rather_than_zero():
    """"BTC charges nothing" and "BTC is missing from the table" are different sentences.

    BTCClient.redeem_contract() passes no extra outputs, and whether it should
    charge a platform fee is fund movement and the operator's (rule 16) -- the
    one row of the divergence table this merge deliberately did not settle. A
    zero entry would read as the table having decided, and this asserts that it
    has not.
    """
    with pytest.raises(ValueError, match="BTC is absent on purpose"):
        platform_fee_coin("BTC", Decimal("1.0"))
