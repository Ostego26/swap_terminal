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
import inspect
import logging
import os
import struct
from decimal import Decimal
from json import dumps as json_dumps

import base58
import pytest
import requests as requests_module
from config import Config
from modules import atomic_btc_client as btc_module
from modules import atomic_grc_client as grc_module
from modules import atomic_ltc_client as ltc_module
from modules.address_network import address_network
from modules.atomic_btc_client import BTCClient
from modules.atomic_grc_client import GRCClient
from modules.atomic_htlc_scripts import build_htlc_redeem_script, p2sh_script_for
from modules.atomic_htlc_scripts import push_data as client_push_data
from modules.atomic_ltc_client import LTCClient
from modules.htlc_fee import (
    BROADCAST_CEILING_COIN_PER_KVB,
    PLATFORM_FEE_ADDRESS_VARIABLE,
    PLATFORM_FEE_RATE,
    PLATFORM_FEE_TESTNET_DEFAULT,
    assert_no_output_is_dust,
    assert_within_broadcast_ceiling,
    dust_threshold_satoshis,
    effective_rate_coin_per_kvb,
    fee_rate_coin_per_kvb,
    is_witness_program,
    minimum_fee_coin,
    platform_fee_address,
    platform_fee_coin,
    redeem_miner_fee,
)
from modules.htlc_rpc import (
    assert_output_pays_the_contract,
    build_hashlock_spend,
    build_refund_spend,
    describe_rpc_payload,
    ensure_watch_only_import,
    find_output_by_script,
    lookup_contract_output,
    read_transaction_outputs,
    rpc_result,
    wait_for_tx_output,
)
from modules.htlc_spend import (
    MAX_DER_SIGNATURE_WITH_HASHTYPE,
    ParsedTransaction,
    TransactionLayoutError,
    coins_to_satoshis,
    decode_wif,
    estimated_script_sig_length,
    hashlock_script_sig,
    parse_transaction,
    public_key_for,
    refund_script_sig,
    satoshis_to_coins,
    sign_digest,
    spend_key_matches_script,
    with_locktime,
)
from regtest.keys import generate_key
from regtest.steps import _p2sh_script_for
from regtest.txbuild import SEQUENCE_FINAL, Outpoint
from regtest.txbuild import legacy_sighash as harness_legacy_sighash
from regtest.txbuild import push_data as harness_push_data
from regtest.txbuild import redeem_script_sig as harness_redeem_script_sig

# ScriptFailure and _eval_p2sh_spend come from the sibling test module. ONE
# stack interpreter in this repository, imported rather than copied (rule 8);
# pytest puts tests/ on sys.path, which is what makes this importable.
from test_regtest_harness_units import ScriptFailure, _eval_p2sh_spend
from valid_addresses import INVALID_PLACEHOLDERS, LTC_PLATFORM_FEE

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
#: What a two-argument createrawtransaction returns on every chain.
SEQUENCE_FINAL_BYTES = struct.pack("<I", 0xFFFFFFFF)
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


def _manual_unsigned_transaction(
    txid: str, vout: int, outputs: list[tuple[int, bytes]], prefix: bytes, final: bool = False
) -> str:
    """Lay out an unsigned one-input transaction BY HAND, in this test file.

    Deliberately not built with modules/htlc_spend.ParsedTransaction.serialize():
    the parser under test must be fed bytes that its own serializer did not
    produce, or a round-trip assertion proves only that a function is its own
    inverse.

    THERE IS NO `locktime` PARAMETER, AND THAT IS THE POINT. A two-argument
    `createrawtransaction` returns nLockTime 0 on every chain; only Bitcoin Core's optional
    third argument changes it, and Gridcoin does not have one. A fixture that could emit a
    non-zero nLockTime would be modelling a daemon the product cannot rely on. A spend that
    needs one gets it from htlc_spend.with_locktime(), which is the code under test.

    `final` exists for the other half of the same fact. This emitted SEQUENCE_NON_FINAL_BYTES
    unconditionally until 2026-09-28, which handed the refund path the very property it exists
    to establish -- a fake supplying the answer is a test that cannot fail.
    """
    body = prefix
    body += b"\x01"
    sequence = SEQUENCE_FINAL_BYTES if final else SEQUENCE_NON_FINAL_BYTES
    locktime = 0
    body += bytes.fromhex(txid)[::-1] + struct.pack("<I", vout) + b"\x00" + sequence
    body += bytes([len(outputs)])
    for satoshis, script in outputs:
        body += struct.pack("<q", satoshis) + bytes([len(script)]) + script
    body += struct.pack("<I", locktime)
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

    def _rpc_createrawtransaction(self, inputs, outputs, *refused):
        """TWO ARGUMENTS, AND A THIRD IS REFUSED THE WAY GRIDCOIN REFUSES IT.

        This took `locktime=0` as a third positional until 2026-09-28, which is Bitcoin Core's
        signature. Gridcoin's takes exactly two -- measured on the operator's daemon, which
        answered `code=-1` and printed its own help text -- so a fake that accepted three could
        not express the chain the product actually runs on, and the three-argument call that
        could never work there passed every test in this file.

        IT ALSO EMITS A FINAL SEQUENCE AND A ZERO nLockTime NOW, which is the stronger half of
        the fixture. It used to return SEQUENCE_NON_FINAL_BYTES unconditionally, so a refund
        looked correctly non-final whether the code under test had set that field or not --
        the fake was supplying the very property the refund path exists to get right. Now those
        bytes can only come from htlc_spend.with_locktime(), which is the code being tested.
        """
        if refused:
            raise Exception(
                "RPC Error: createrawtransaction takes exactly 2 arguments -- Gridcoin has no "
                "locktime parameter (measured on the operator's daemon 2026-09-28)"
            )
        entry = inputs[0]
        laid_out = []
        for address, amount in outputs.items():
            if address not in self.scripts:
                raise Exception(f"RPC Error: Invalid address {address}")
            laid_out.append((coins_to_satoshis(Decimal(str(amount))), self.scripts[address]))
        return _manual_unsigned_transaction(
            entry["txid"], int(entry["vout"]), laid_out, self.prefix, final=True
        )

    def _rpc_sendrawtransaction(self, raw_hex, *_rest):
        self.broadcast.append(raw_hex)
        return "dd" * 32

    @property
    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]


# WHY A NON-EMPTY VALUE IS REQUIRED HERE, and why it is named rather than inline.
# ensure_fully_unlocked() returns immediately when the passphrase is empty, so a test that
# left it unset would pass whether the unlock call were present or absent -- the shape of
# green test that measures nothing. It has to be non-empty.
#
# Named, and named without the word ruff's S105/S106 scan for, because a literal at the call
# site is flagged and the answer to that is to have no literal rather than a suppression
# (rule 19). It unlocks nothing: no wallet in this suite is encrypted, and conftest's
# RPC_FIXTURE_AUTH is the same idea one file over.
FIXTURE_UNLOCK_VALUE = "fixture-unlock-value-not-a-real-one"


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
            # THE REFUND KEY'S OWN ADDRESS, added 2026-09-28. A refund pays the refund key,
            # and this node did not know that address -- so even after it learned to take a
            # locktime it answered "Invalid address" for the one destination a refund has.
            # Two separate ways the fake could not represent a refund, in a suite whose only
            # GRC refund test read the client's source for a substring.
            contract["refund"].address: contract["refund"].p2pkh_script,
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
        SEQUENCE_FINAL,
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
        SEQUENCE_FINAL,
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
    assert not spend_key_matches_script(stranger.public_key, contract["redeem_script"])
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
    assert spend_key_matches_script(contract["refund"].public_key, contract["redeem_script"])
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
        SEQUENCE_FINAL,
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


def _client_for(asset: str):
    """An LTC or GRC client pointed at a loopback URL no socket is ever opened on.

    ONE constructor for the three platform-fee tests below. They each spelled the same
    two-branch `if asset == "LTC": LTCClient(...) else: GRCClient(...)`, which is rule 8's
    shape at the smallest possible scale -- and the GRC branch carries a keyword the LTC one
    does not (`wallet_passphrase=""`, which skips the unlock), so the two are not
    interchangeable and a reader has to be told that once rather than three times.

    BTC is here too, though the two UNUSABLE-address tests below do not parameterize it in:
    its client adds no fee output when the variable is unset, so an assertion on output COUNT
    would hold there for the wrong reason. It IS driven by
    test_the_btc_client_threads_the_fee_output_through(), which sets the variable and therefore
    measures the output rather than its absence.
    """
    if asset == "BTC":
        return BTCClient("http://127.0.0.1:18443/wallet/w", "u", "p")
    if asset == "LTC":
        return LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p")
    return GRCClient("http://127.0.0.1:15715", "u", "p", wallet_passphrase="")

@pytest.mark.parametrize("asset", ["LTC", "GRC"])
def test_an_unusable_platform_fee_address_does_not_block_the_redeem(asset, contract, monkeypatch, caplog):
    """THE DIRECTION THAT MATTERS, DRIVEN THROUGH THE REAL CLIENT. Added 2026-09-27.

    A redeem is time-critical: the hashlock branch has to be spent before the counterparty's
    timelock expires, and NO CLIENT IN THIS PACKAGE IMPLEMENTS A REFUND. So refusing a
    customer's redeem because OUR fee address is malformed would strand their whole leg to
    protect our 1.5%. The right answer is to drop the fee output, warn loudly, and let the
    redeem through -- which is exactly what an UNSET variable already did.

    THREE ASSERTIONS AND THE ORDER IS THE ARGUMENT:
      1. the transaction WAS broadcast -- the redeem is not blocked
      2. it has ONE output -- the unpayable fee output was dropped, not paid
      3. the warning names the variable and the reason (rule 14)

    Without (1) this would pass for a client that raised; without (2) it would pass for a
    client that burned the fee; without (3) the operator never learns which of two Nones
    they got.

    BTC is not parameterized in: its client adds no fee output when the variable is unset and
    the assertion `len(outputs) == 1` would hold for the wrong reason.

    MUTATION: revert either client to platform_fee_address() and assertion (2) fails -- the
    transaction grows a second output paying an address nobody can spend.
    """
    monkeypatch.setenv(PLATFORM_FEE_ADDRESS_VARIABLE[asset], INVALID_PLACEHOLDERS["base58 checksum"])
    prefix = GRIDCOIN_PREFIX if asset == "GRC" else VERSION_2_PREFIX
    node = _node_for(contract, prefix=prefix)
    client = _client_for(asset)
    client.rpc_call = node.rpc_call

    with caplog.at_level(logging.WARNING):
        txid = _drive_redeem(client, node, contract)

    assert txid == "dd" * 32, f"{asset}: the redeem was BLOCKED by our own fee address"
    assert len(node.broadcast) == 1
    parsed = parse_transaction(bytes.fromhex(node.broadcast[0]), contract["txid"], contract["vout"])
    assert len(parsed.outputs) == 1, (
        f"{asset}: the transaction still pays a fee output to an address that cannot be spent -- the fee "
        f"was burned"
    )
    assert "NO PLATFORM FEE CHARGED" in caplog.text
    assert PLATFORM_FEE_ADDRESS_VARIABLE[asset] in caplog.text
    assert "NOT A USABLE" in caplog.text, "the warning must say WHICH failure: unset and unusable differ"


@pytest.mark.parametrize("asset", ["LTC", "GRC"])
def test_a_fee_address_this_repository_CANNOT_PLACE_is_paid_and_says_it_was_not_checked(
    asset, contract, monkeypatch, caplog
):
    """THE HOLE REVIEW FOUND, 2026-09-28, AND IT IS RULE 14 EXACTLY.

    modules/htlc_fee.usable_platform_fee_address() has FOUR outcomes and the three clients
    branched on TWO of them -- `if fee_address: info() else: warning()`. An UNDETERMINED
    address (valid, and unplaceable by this repository's tables) passes through WITH the
    address, deliberately, because a gap in our tables is not evidence against an address.
    The truthiness branch then printed

        platform fee 0.00150000 to <address>

    which is the sentence a CHECKED address gets. Nothing had been checked. An operator
    skimming a log had no way to tell the two apart, which is the defect rule 14 names:
    "did nothing" and "did work" must not share a line, and here "verified" and "not
    verified" shared one.

    THIS IS NOT A HYPOTHETICAL STATE. Litecoin's regtest hrp `rltc` and its second P2SH
    version byte 0x3A were both UNDETERMINED on the operator's live 2026-09-27 regtest swap,
    on addresses litecoind itself produced. The table entries exist now; the next missing
    entry is what this pins.

    THE FIXTURE IS THE SHAPE THE INCIDENT HAD, and building it any other way would measure
    the wrong thing. The fee address is the contract's OWN platform key re-encoded under
    version 0x7B, and the fake node is given a script for it -- i.e. a DAEMON THAT ACCEPTS
    THE ADDRESS while this repository's tables cannot place it. That is precisely what
    litecoind's `Q...` form was: the daemon produced it, the daemon would have paid it, and
    only our table was missing. An address no daemon accepts is a different test, the one
    directly above.

    THREE ASSERTIONS, AND THE FIRST TWO ARE WHY THE FIX IS NOT "REFUSE IT":
      1. the redeem was broadcast
      2. it HAS TWO OUTPUTS -- the fee WAS paid, because refusing our own table gap would
         lose a fee that is probably fine, and refusing the redeem would lose the leg
      3. the log says NOT CHECKED, at WARNING

    MUTATION (run 2026-09-28, both killed): drop `self.verified` from
    PlatformFeeOutput.log_level and (3)'s level assertion fails; delete the unverified branch
    of outcome() and (3)'s text assertion fails -- while (1) and (2) stay green, which is the
    asymmetry the state exists for, since the money moved correctly and only the reporting
    lied.
    """
    unplaceable = base58.b58encode_check(bytes([0x7B]) + contract["platform"].hash160).decode()
    assert address_network(unplaceable)[0] == "unknown", (
        "0x7B has been assigned in modules/address_network.py, so this fixture no longer "
        "exercises UNDETERMINED -- pick another unassigned byte"
    )
    monkeypatch.setenv(PLATFORM_FEE_ADDRESS_VARIABLE[asset], unplaceable)
    prefix = GRIDCOIN_PREFIX if asset == "GRC" else VERSION_2_PREFIX
    node = _node_for(contract, prefix=prefix)
    # The daemon accepts it. That is the whole point: our table is the thing that is missing.
    node.scripts[unplaceable] = contract["platform"].p2pkh_script
    client = _client_for(asset)
    client.rpc_call = node.rpc_call

    with caplog.at_level(logging.INFO):
        txid = _drive_redeem(client, node, contract)

    assert txid == "dd" * 32, f"{asset}: the redeem was BLOCKED by an address we merely could not place"
    parsed = parse_transaction(bytes.fromhex(node.broadcast[0]), contract["txid"], contract["vout"])
    assert len(parsed.outputs) == 2, (
        f"{asset}: the fee output was DROPPED for an address that is well-formed and merely "
        f"unplaceable -- that is the false refusal, and it loses a fee that is probably fine"
    )
    unchecked = [r for r in caplog.records if "NOT CHECKED" in r.getMessage()]
    assert unchecked, (
        f"{asset}: a fee output was paid to an UNVERIFIED address and no line said so -- the log reads "
        f"exactly like one paid to a verified address (rule 14). Lines seen: "
        f"{[r.getMessage()[:80] for r in caplog.records] or '(none)'}"
    )
    assert all(r.levelno == logging.WARNING for r in unchecked), (
        f"{asset}: an unverified fee output was reported at "
        f"{sorted({r.levelname for r in unchecked})}; INFO is the level a VERIFIED one gets"
    )


@pytest.mark.parametrize("asset", ["LTC", "GRC"])
def test_a_verified_fee_address_is_reported_at_info_and_not_as_unchecked(asset, contract, monkeypatch, caplog):
    """The other direction, so the assertion above cannot pass by the log always saying it.

    Without this, a mutation that hard-coded "NOT CHECKED" into every fee line -- or set the
    level to WARNING unconditionally -- would leave the test above green. Both assertions
    below go red on that mutation, which is the pair working as one measurement. This is the
    lesson from the four tests that passed while the code was mutated: one direction is not
    a measurement, it is half of one.

    The fee address is the contract's own platform key, which the fake node already knows and
    which decodes as an ordinary testnet address on all three chains.
    """
    monkeypatch.setenv(PLATFORM_FEE_ADDRESS_VARIABLE[asset], contract["platform"].address)
    prefix = GRIDCOIN_PREFIX if asset == "GRC" else VERSION_2_PREFIX
    node = _node_for(contract, prefix=prefix)
    client = _client_for(asset)
    client.rpc_call = node.rpc_call

    with caplog.at_level(logging.INFO):
        _drive_redeem(client, node, contract)

    fee_lines = [r for r in caplog.records if "platform fee" in r.getMessage()]
    assert fee_lines, f"{asset}: the redeem reported no platform fee at all"
    assert "NOT CHECKED" not in caplog.text, f"{asset}: a VERIFIED fee address was reported as unchecked"
    assert all(r.levelno == logging.INFO for r in fee_lines), (
        f"{asset}: a verified fee output was reported at {sorted({r.levelname for r in fee_lines})}, not "
        f"INFO -- a warning on the ordinary case is how a real warning stops being read"
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


def _requests_module_for(asset: str):
    """The `requests` binding to stub for one client: each imports its own.

    All three clients do `import requests` at module scope, so monkeypatching
    `requests.post` globally would work too -- and would also silently stub the other two,
    which is how a test starts passing because of a sibling. Naming the module makes the
    stub reach exactly one client.
    """
    return {"BTC": btc_module, "LTC": ltc_module, "GRC": grc_module}[asset].requests

@pytest.mark.parametrize("asset", ["BTC", "GRC"])
def test_a_probe_for_a_method_the_chain_does_not_have_is_quiet_and_names_the_chains(
    asset, caplog, monkeypatch
):
    """RULE 14 BACKWARDS: THE THING THAT WORKED LOOKED BROKEN. Fixed 2026-09-28.

    Gridcoin has no `gettxout` -- measured on the operator's testnet daemon 2026-09-27 with
    `help gettxout`, which answered "unknown command: gettxout" while `getrawtransaction`,
    `gettransaction` and `signrawtransaction` all existed. It is route 1 of
    modules/htlc_rpc.lookup_contract_output()'s four routes, so it misses on EVERY GRC spend,
    and route 4 covers it.

    What that cost was output, not correctness: `except Exception: logger.exception(...)` is
    ERROR plus a full stack, so ~40 lines of traceback printed in front of a spend that
    SUCCEEDED (a 401 printed ~100). An operator reading that pastes it back and asks what
    broke. Nothing broke.

    DRIVEN THROUGH THE REAL rpc_call, via requests.post, for the reason _post_through()
    already spells out: rpc_call IS the method that does the printing, so replacing it deletes
    the code under test. The daemon's answer here is the exact body a real one sends --
    HTTP 200 with an `error` object carrying code -32601 -- because
    modules/htlc_rpc.rpc_result() reads the body before the status and that is the shape it
    reads.

    BTC is parameterized in even though bitcoind HAS gettxout: the handler is the same shape
    in both clients, and the rule must not be one client's local quirk (rule 8). Litecoin is
    absent because its rpc_call catches RequestException only -- measured 2026-09-28 -- so a
    -32601 there propagates with no logging at all, which is a third behavior and is named in
    the report rather than changed here.

    MUTATION: return logging.ERROR unconditionally from rpc_failure_report() and assertion (2)
    fails; drop the OPTIONAL_PROBE_METHODS check so any -32601 is quieted and
    test_a_method_not_found_on_a_method_this_repository_NEEDS_stays_loud below fails instead.
    """
    client = _client_for(asset)
    monkeypatch.setattr(
        _requests_module_for(asset), "post",
        lambda *_a, **_k: _FakeHTTPResponse(error={"code": -32601, "message": "Method not found"}),
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(Exception, match="-32601"):
        client.rpc_call("gettxout", ["ab" * 32, 1, True])

    # (1) the miss is still reported -- silence would be its own rule 14 defect
    reports = [r for r in caplog.records if "gettxout" in r.getMessage() and "no `gettxout`" in r.getMessage()]
    assert reports, (
        f"{asset}: the probe miss was reported by no line at all. Lines seen: "
        f"{[r.getMessage()[:70] for r in caplog.records] or '(none)'}"
    )
    # (2) at DEBUG, with no traceback. Both halves: the stack was the expensive part.
    assert all(r.levelno == logging.DEBUG for r in reports), (
        f"{asset}: a routine probe miss reported at {sorted({r.levelname for r in reports})} -- this is the "
        f"~40 lines in front of a successful spend"
    )
    # `not r.exc_info` rather than `is None`: logging stores exc_info=False verbatim on the
    # record, so `is None` would fail for a record that carries no traceback at all. Measured
    # while writing this -- the first version of this assertion was wrong in the safe
    # direction, which is still wrong.
    assert all(not r.exc_info for r in reports), f"{asset}: a routine probe miss still carries a traceback"
    # (3) rule 14: it names the chains the method IS expected on, so nobody goes installing it
    assert "expected on BTC and LTC" in reports[0].getMessage()
    assert "NOTHING IS WRONG" in reports[0].getMessage()


@pytest.mark.parametrize("asset", ["BTC", "GRC"])
def test_a_method_not_found_on_a_method_this_repository_NEEDS_stays_loud(asset, caplog, monkeypatch):
    """The control, and it is the reason the quieting is a table rather than an error code.

    A bare `-32601` check would quiet EVERY missing method, including one whose absence means
    the daemon cannot do the job: `signrawtransaction` missing on Gridcoin would mean nothing
    can sign a refund, and that must stay as loud as it has ever been.

    Without this test, deleting the OPTIONAL_PROBE_METHODS lookup and quieting all -32601s
    would leave the test above green -- which is the single-direction measurement this session
    was told to stop producing.
    """
    client = _client_for(asset)
    monkeypatch.setattr(
        _requests_module_for(asset), "post",
        lambda *_a, **_k: _FakeHTTPResponse(error={"code": -32601, "message": "Method not found"}),
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(Exception, match="-32601"):
        client.rpc_call("signrawtransaction", ["deadbeef"])

    loud = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert loud, (
        f"{asset}: a daemon that cannot sign was reported at "
        f"{sorted({r.levelname for r in caplog.records}) or '(none)'} -- below ERROR, which is where the "
        f"routine probe miss lives, so the two are now indistinguishable"
    )
    assert any(r.exc_info is not None for r in loud), f"{asset}: a real failure lost its traceback"
    assert all("NOTHING IS WRONG" not in r.getMessage() for r in loud)


@pytest.mark.parametrize("asset", ["BTC", "GRC"])
def test_an_ordinary_rpc_failure_is_still_loud_with_its_traceback(asset, caplog, monkeypatch):
    """A rejected transaction, which is the failure that actually matters on the spend path.

    `-26 non-mandatory-script-verify-flag` is the answer a daemon gives when relay policy
    declines a refund, and modules/htlc_rpc.rpc_result() exists to keep that message readable.
    Quieting it would undo that work, so it is pinned alongside the two -32601 cases.
    """
    client = _client_for(asset)
    monkeypatch.setattr(
        _requests_module_for(asset), "post",
        lambda *_a, **_k: _FakeHTTPResponse(
            error={"code": -26, "message": "non-mandatory-script-verify-flag (Locktime requirement not satisfied)"}
        ),
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(Exception, match="non-mandatory-script-verify-flag"):
        client.rpc_call("sendrawtransaction", ["deadbeef"])

    loud = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert loud, f"{asset}: a REJECTED TRANSACTION was reported below ERROR"
    assert any("non-mandatory-script-verify-flag" in r.getMessage() for r in loud), (
        "the daemon's own reason is what tells a policy refusal from a consensus one -- it must reach the log"
    )


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

    THE CONTRACT VALUE IN THIS TEST CHANGED ON 2026-09-27 BECAUSE THE RATE DID,
    and the reason is worth reading before the assertions.

    The fee output is P2WPKH (PLATFORM_FEE_LTC_ADDRESS is a bech32 `tltc1q...`) and
    Litecoin Core 0.21.4's dust limit for one is 2,940 satoshis. So the dust
    boundary is the contract value at which the platform fee equals 2,940:

        at 0.25%   2940 / 0.0025 = 1,176,000 sat = 0.01176000 LTC
        at 1.5%    2940 / 0.015  =   196,000 sat = 0.00196000 LTC

    Raising the rate six-fold moved that boundary six-fold DOWN. This test used a
    0.01 LTC contract, whose fee was 2,500 satoshis and therefore dust; at 1.5% the
    same contract pays 15,000 and is nowhere near it, so the test stopped raising --
    which is how the change was noticed rather than shipped.

    THE BEHAVIORAL CONSEQUENCE, STATED PLAINLY: a redeem between 0.00196 and 0.01176
    LTC used to be REFUSED before signing and now proceeds. That is strictly better
    -- fewer redeems blocked, and the block was never desirable -- but it is a change
    in what the code does, not just in what it charges, and it belongs in the record.

    0.001 LTC is the new fixture: 1,500 satoshis of fee, comfortably under 2,940.

    The assertion that matters is not the exception, it is `node.broadcast == []`
    plus the absence of sendrawtransaction from the call list: the refusal has to
    arrive while a different decision is still possible, which means before a
    signature exists. Before 2026-09-25 this was signed and handed to
    `sendrawtransaction`, which refuses the WHOLE transaction with `code=-26 dust`
    -- and no client here implements a refund, so the redeemer loses their own
    already-funded leg.
    """
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", contract["platform"].address)
    node = _node_for(
        contract,
        platform_script=p2wpkh_script(contract["platform"]),
        value=Decimal("0.001"),
    )
    client = LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p")
    client.rpc_call = node.rpc_call

    with pytest.raises(ValueError) as raised:
        _drive_redeem(client, node, contract)

    message = str(raised.value)
    assert "DUST" in message
    assert "1500 satoshis" in message, message
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

    UPDATED 2026-09-27, AND THE ASSERTION GOT STRONGER RATHER THAN JUST NEWER.
    The rate moved from 0.25% to 1.5% at the operator's instruction, so the two
    lines that restated the old constant had to go -- and rewriting them as
    `== 0.015 * value` would have recreated exactly the weakness this docstring
    already complained about: a literal here passes whenever the table and this
    line are edited together, which is the drift a shared table exists to prevent.

    So the invariant is now the one whose violation was the actual bug. The
    brokered path charges DEFAULT_FEE_BPS (config.py) and the atomic path charges
    PLATFORM_FEE_RATE, and until today those were 150 bps and 0.25% -- a SIX-FOLD
    divergence in what the same customer pays for the same pair depending on which
    route they took, with nothing in either file pointing at the other. That is
    rule 8's shape across two subsystems rather than two files, and it is what this
    test now pins: the two rates must be equal, whatever they are.
    """
    brokered_rate = Decimal(int(Config.DEFAULT_FEE_BPS)) / Decimal(10000)
    for value in (Decimal("1.0"), Decimal("0.01"), Decimal("123.456789"), Decimal("0.00000001")):
        assert platform_fee_coin("LTC", value) == platform_fee_coin("GRC", value)
        # The atomic rate IS the brokered rate. Not a restated literal: if either
        # side moves alone, the two routes have diverged and this fails.
        assert platform_fee_coin("LTC", value) == (brokered_rate * value).quantize(
            Decimal("0.00000001")
        )

    # And the rate is in the band the operator asked for, which is the one thing a
    # literal IS the right check for -- a table edited to 15% or 0.15% would satisfy
    # every equality above.
    assert Decimal("0.01") <= brokered_rate <= Decimal("0.02"), (
        f"the operator asked for 1-2%; the shared rate is {brokered_rate}"
    )


def test_btc_now_charges_the_same_rate_as_the_other_two():
    """THE OPERATOR SETTLED THE ROW THIS TEST USED TO PIN AS OPEN.

    It asserted `platform_fee_coin("BTC", ...)` raises, on the grounds that "BTC charges
    nothing" and "BTC is missing from the table" are different sentences and only the
    operator could decide which. They decided on 2026-09-27: "take care of btc and ltc
    deposit wallet accounts for fee collection."

    So the test changes rather than being deleted, and it changes to the STRONGER
    invariant (rule 2): not "BTC has a rate" but "BTC has the SAME rate as the others",
    which is the property that would break silently. Three assets each with their own
    literal is rule 8's shape, and a table where one drifts is exactly how the same
    customer pays two prices.

    The row and the collection landed together, which is the part worth insisting on: a
    rate in a table that no code collects is worse than no rate, because a reader finds
    it and stops looking. test_the_btc_client_threads_the_fee_output_through() below is
    the other half.
    """
    for value in (Decimal("1.0"), Decimal("0.01"), Decimal("123.456789")):
        assert platform_fee_coin("BTC", value) == platform_fee_coin("LTC", value)
        assert platform_fee_coin("BTC", value) == platform_fee_coin("GRC", value)
    assert set(PLATFORM_FEE_RATE) == {"BTC", "LTC", "GRC"}, (
        "all three chains this package can redeem on, and no fourth invented"
    )


def test_the_btc_client_threads_the_fee_output_through(contract, monkeypatch, caplog):
    """The half that makes the rate real -- asserted on the TRANSACTION, not on the source.

    BTC was absent from the fee table for a MECHANICAL reason, not a policy one:
    redeem_contract() passed no `extra_outputs` at all, so a row in the table would have
    claimed a fee no code collected. Adding the row without this is the failure mode the old
    comment warned about, so the fee output is pinned here rather than left to the rate test
    to imply.

    REWRITTEN 2026-09-28, AND THE OLD VERSION IS WHY THE HOUSE RULE EXISTS. It read

        source = inspect.getsource(BTCClient.redeem_contract)
        assert "extra_outputs=extra_outputs" in source
        assert 'platform_fee_address("BTC")' in source
        assert "if fee_address else {}" in source

    -- five assertions about the TEXT of a method, and they fail in both directions:

      FALSE NEGATIVE, observed. Renaming the local `fee_address` to `fee` and moving the log
      sentence into modules/htlc_fee.py changed no behavior whatsoever -- same outputs, same
      amounts, same address -- and this test went red on the rename. A test that fails on a
      refactor teaches a reader to edit the test, which is how the next real break gets
      edited away with it.

      FALSE POSITIVE, and worse. `platform_fee_address("BTC")` appearing in the source proves
      nothing about whether the RESULT reaches an output: the line could compute it and
      discard it, or the whole branch could be unreachable, and every assertion would still
      pass. Three tests in this repository passed that way on 2026-09-27 while the code under
      them was mutated.

    So all five claims are now asserted on the broadcast transaction, which is the only thing
    that can be wrong in a way that costs money:

      1. TWO outputs -- the fee output reached the spend
      2. one of them pays exactly platform_fee_coin("BTC", contract value)
      3. and pays it to the script of the address in PLATFORM_FEE_BTC_ADDRESS
      4. with the variable UNSET, ONE output and the redeem still broadcasts (the None case
         omits the output rather than blocking the redeem -- a redeem is time-critical and no
         client here implements a refund)
      5. and that case says NO PLATFORM FEE CHARGED (rule 14)
    """
    fee_key = contract["platform"]
    monkeypatch.setenv("PLATFORM_FEE_BTC_ADDRESS", fee_key.address)
    node = _node_for(contract)
    client = _client_for("BTC")
    client.rpc_call = node.rpc_call

    txid = _drive_redeem(client, node, contract)

    assert txid == "dd" * 32
    parsed = parse_transaction(bytes.fromhex(node.broadcast[0]), contract["txid"], contract["vout"])
    assert len(parsed.outputs) == 2, (
        f"the BTC fee output never reached the spend: {len(parsed.outputs)} output(s). A rate in "
        f"PLATFORM_FEE_RATE that no code collects is worse than no rate, because a reader finds it "
        f"and stops looking"
    )
    expected = coins_to_satoshis(platform_fee_coin("BTC", CONTRACT_COINS))
    # ParsedTransaction.outputs is a tuple of (value_satoshis, script_pubkey) pairs.
    fee_outputs = [value for value, script in parsed.outputs if script == fee_key.p2pkh_script]
    assert len(fee_outputs) == 1, "no output pays the address PLATFORM_FEE_BTC_ADDRESS names"
    assert fee_outputs[0] == expected, (
        f"the fee output pays {fee_outputs[0]} satoshis where platform_fee_coin('BTC') says "
        f"{expected} -- the table and the collection disagree, which is the same customer paying two prices"
    )

    # (4) and (5): the SAME client, with the variable unset.
    monkeypatch.delenv("PLATFORM_FEE_BTC_ADDRESS", raising=False)
    unset_node = _node_for(contract)
    unset_client = _client_for("BTC")
    unset_client.rpc_call = unset_node.rpc_call
    with caplog.at_level(logging.WARNING):
        unset_txid = _drive_redeem(unset_client, unset_node, contract)

    assert unset_txid == "dd" * 32, "an unset fee variable BLOCKED the redeem, which is the wrong trade"
    unset_parsed = parse_transaction(
        bytes.fromhex(unset_node.broadcast[0]), contract["txid"], contract["vout"]
    )
    assert len(unset_parsed.outputs) == 1, "with no fee address the redeem must pay one output, not two"
    assert "NO PLATFORM FEE CHARGED" in caplog.text, "rule 14: charging nothing must log differently"


def test_btc_has_no_shipped_testnet_default_and_must_not_gain_one():
    """LTC and GRC shipped testnet defaults that BURNED the fee on mainnet. BTC was added
    after that was found, so it never had one -- and inventing a third would be repeating
    a documented mistake. An unset variable means charge no fee, which reaches the same
    safe outcome without the defect."""
    assert "BTC" in PLATFORM_FEE_ADDRESS_VARIABLE
    assert "BTC" not in PLATFORM_FEE_TESTNET_DEFAULT
    assert platform_fee_address("BTC", environment={}) is None


# ---------------------------------------------------------------------------
# THE REFUND BRANCH, added 2026-09-26 with refund_contract(). The harness
# (regtest_htlc_verify.py steps 8 and 9) proves it against a real daemon, which
# is the only proof that counts for "does the chain accept it". These cover the
# parts a chain cannot isolate: which bytes the scriptSig carries, and the
# error-reporting order that decides whether the harness can tell a policy
# refusal from a consensus one.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Just enough of requests.Response for rpc_result: a body and a status."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        if self._payload is _NOT_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_module.exceptions.HTTPError(f"{self.status_code} Server Error: for url: http://x")


_NOT_JSON = object()


def test_a_rejected_rpc_keeps_its_reason_even_though_the_status_is_500():
    """THE ORDER OF TWO LINES, and the whole refund measurement rests on it.

    Bitcoin Core, Litecoin Core and Gridcoin all answer a rejected
    sendrawtransaction with HTTP 500 AND a JSON body naming the reason. All
    three clients called raise_for_status() before reading that body until
    2026-09-26, so every refusal arrived as `500 Server Error` with the message
    discarded. regtest_htlc_verify.py step 8 exists to say WHICH layer refused
    an early refund -- relay policy or consensus -- and it reads that from the
    message, so the reorder is what makes the assertion possible at all.

    Mutation check: move response.raise_for_status() back above the error read
    in modules/htlc_rpc.rpc_result and this fails on the match, because the
    HTTPError's text contains neither -26 nor the flag name.
    """
    refused = _FakeResponse(
        {"result": None, "error": {"code": -26, "message": "non-mandatory-script-verify-flag (Locktime requirement not satisfied)"}, "id": "atomic-swap"},
        status_code=500,
    )
    with pytest.raises(Exception, match="non-mandatory-script-verify-flag") as caught:
        rpc_result(refused, "RPC Error")
    # The CODE has to survive too: -26 is how a caller tells a rejected
    # transaction from a missing method, and step 8 prints it.
    assert "-26" in str(caught.value)
    assert "500 Server Error" not in str(caught.value)


def test_a_non_json_body_still_reports_the_http_status():
    """The one case where the status IS the only information there is.

    Something in front of the daemon answering with an HTML error page has no
    JSON error to report, so raise_for_status() is the right diagnosis and must
    not be skipped just because the body could not be parsed.
    """
    with pytest.raises(requests_module.exceptions.HTTPError, match="502"):
        rpc_result(_FakeResponse(_NOT_JSON, status_code=502, text="<html>bad gateway</html>"), "RPC Error")


def test_a_response_with_neither_error_nor_result_is_refused_not_returned_as_none():
    """LTC and GRC used to return None here, which a caller cannot interpret.

    `rj.get("result")` turned a malformed response into the same value a daemon
    returns for "no such transaction". BTC raised KeyError on the same input.
    The three are resolved toward refusing with a message that names what the
    daemon actually sent.
    """
    with pytest.raises(Exception, match=r"neither `error` nor `result`"):
        rpc_result(_FakeResponse({"id": "atomic-swap"}), "RPC Error")


def test_the_refund_script_sig_takes_the_else_branch_and_carries_no_preimage(contract):
    signature = b"\x30" + b"\x11" * 71
    script_sig = refund_script_sig(signature, contract["refund"].public_key, contract["redeem_script"])
    # OP_0 selects the timelock branch. It must be the single byte 0x00 -- a
    # push of a one-byte zero (0x01 0x00) is a one-byte TRUE on the stack and
    # would take the HASHLOCK branch with no preimage behind it.
    assert script_sig.endswith(client_push_data(contract["redeem_script"]))
    selector_at = len(client_push_data(signature)) + len(client_push_data(contract["refund"].public_key))
    assert script_sig[selector_at : selector_at + 1] == b"\x00"
    assert contract["secret"] not in script_sig


def test_the_refund_script_sig_is_shorter_than_the_redeem_by_exactly_the_preimage_push(contract):
    """33 bytes: the preimage's whole push, because the two selectors cancel.

    A hashlock scriptSig carries push(32-byte secret) = 33 bytes plus OP_1; a
    refund carries OP_0. Both selectors are one byte, so they cancel and the
    difference is exactly the 33-byte push. This was written as 32 first, from
    reasoning rather than measurement, and the assertion is what caught it --
    which is the argument for asserting the exact difference instead of `<`:
    the fee is sized from this estimate, so a branch mix-up must not merely
    look plausible.
    """
    pubkey = contract["refund"].public_key
    with_secret = estimated_script_sig_length(pubkey, contract["secret"], contract["redeem_script"])
    without = estimated_script_sig_length(pubkey, None, contract["redeem_script"])
    assert len(contract["secret"]) == 32
    assert with_secret - without == 33


def test_build_hashlock_spend_refuses_a_missing_preimage_rather_than_building_a_refund():
    """secret=None is how the shared builder selects the OTHER branch.

    Letting it through would sign a spend paying the REFUND key from a call site
    whose name says hashlock, and in a log the two differ by four bytes. It is
    refused before anything is built, and the message names the function to call
    if a refund was actually meant.
    """
    with pytest.raises(ValueError, match="build_refund_spend"):
        build_hashlock_spend(
            secret=None,
            asset="BTC",
            rpc_call=lambda *a, **k: pytest.fail("nothing should have been asked of the daemon"),
            contract_txid="00" * 32,
            contract_vout=0,
            contract_value=Decimal("1.0"),
            redeem_script=b"\x00",
            wif="unused",
            destination_address="unused",
        )


# ---------------------------------------------------------------------------
# The platform fee ADDRESS, and the burn that used to be the default.
# ---------------------------------------------------------------------------


def test_an_unset_fee_address_returns_none_rather_than_a_testnet_literal(monkeypatch):
    """THE BURN THIS REPLACED, pinned so it cannot come back.

    Until 2026-09-27 each client defaulted its fee address inline:

        os.environ.get("PLATFORM_FEE_LTC_ADDRESS", "tltc1qzxllez2...")
        os.environ.get("PLATFORM_FEE_GRC_ADDRESS", "mnTh582mZM12...")

    `tltc1q...` is a Litecoin TESTNET bech32 address and `mnTh...` is base58 with
    the 0x6F testnet P2PKH version byte, where a mainnet Gridcoin address starts
    with S. So on MAINNET with the variable unset, the platform fee was paid to an
    address nobody can spend: burned, on every redeem, silently. The GRC client's
    comment DESCRIBED that exactly and then said fixing it was the operator's job,
    which was the wrong division of labor -- naming a burn is not fixing one.

    At 0.25% it was a leak. The rate is now 1.5%, so leaving it would have been six
    times the leak. None means charge no fee, which costs the operator one swap's
    fee and costs the redeemer nothing.
    """
    for asset, variable in PLATFORM_FEE_ADDRESS_VARIABLE.items():
        monkeypatch.delenv(variable, raising=False)
        assert platform_fee_address(asset) is None, f"{asset} fell back to a default"

        # Empty and whitespace are unset too: an env var exported as "" is the
        # shape a half-written deployment script leaves, and treating it as an
        # address would put the fee nowhere at all.
        for blank in ("", "   ", "\t"):
            monkeypatch.setenv(variable, blank)
            assert platform_fee_address(asset) is None, f"{asset} accepted {blank!r}"


def test_the_testnet_literals_are_still_recorded_but_are_not_defaults():
    """They stay NAMED, because a burn that is deleted without a trace teaches
    nobody -- and because an operator on testnet may legitimately want them. What
    they must not be is what happens when nobody chose."""
    assert PLATFORM_FEE_TESTNET_DEFAULT["LTC"].startswith("tltc1q"), "a Litecoin testnet bech32"
    # DECODED, NOT SPELLED. This assertion used to read `not ...startswith("S")` with the
    # comment "a MAINNET Gridcoin address starts with S", and that claim is false: measured
    # over 200,000 random hash160s, 13.08% of mainnet GRC addresses start with R. So the old
    # assertion would have PASSED for a mainnet address -- using a false premise as the
    # evidence that this literal is unspendable, which is the one thing it existed to prove.
    # The same day it was found, a real `getnewaddress` without -testnet produced
    # RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV in the operator's live staking wallet.
    network, why = address_network(PLATFORM_FEE_TESTNET_DEFAULT["GRC"])
    assert network == "testnet", f"the GRC burn literal must decode as testnet: {why}"
    # And they are not reachable through the resolver by any environment at all.
    for asset in PLATFORM_FEE_ADDRESS_VARIABLE:
        assert platform_fee_address(asset, environment={}) is None


def test_a_configured_fee_address_is_returned_verbatim(monkeypatch):
    """The other direction, so the function cannot pass the tests above by always
    answering None."""
    monkeypatch.setenv("PLATFORM_FEE_LTC_ADDRESS", LTC_PLATFORM_FEE)
    assert platform_fee_address("LTC") == LTC_PLATFORM_FEE
    # Surrounding whitespace is stripped -- a trailing newline is what a `$(cat
    # file)` in a deployment script leaves, and it would make an otherwise valid
    # address unusable.
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", "  SomeGridcoinAddress \n")
    assert platform_fee_address("GRC") == "SomeGridcoinAddress"


def test_an_asset_with_no_fee_address_rule_is_refused_by_name():
    """BTC used to be the example here and is not any more -- it has a rule as of
    2026-09-27. XRP is the right example now and for a sharper reason: this package has
    no XRP HTLC client at all, so there is no redeem to take a fee out of. An asset with
    no rule must be refused BY NAME rather than silently returning None, because None
    means "charge no fee" and would make a typo look like a policy."""
    for absent in ("XRP", "XMR", "SOL", "DOGE"):
        with pytest.raises(ValueError, match=f"no platform fee address rule for asset '{absent}'"):
            platform_fee_address(absent)
        assert absent not in PLATFORM_FEE_RATE, (
            f"{absent} has no address rule, so it must not have a rate either -- the two "
            f"going out of step is how a fee gets claimed and never collected"
        )


def test_a_redeem_with_no_fee_address_still_broadcasts(contract, monkeypatch):
    """THE PROPERTY THAT MAKES None SAFE, through the real LTCClient.redeem_contract().

    A redeem is time-critical -- the hashlock branch has to be spent before the
    counterparty's timelock expires, and no client in this package implements a
    refund. So an unresolvable fee address must never block it. Refusing would
    trade a 1.5% fee for the entire leg, which is the wrong direction by three
    orders of magnitude.

    Asserted on the real broadcast: the transaction goes out, and it carries ONE
    output rather than two.
    """
    monkeypatch.delenv("PLATFORM_FEE_LTC_ADDRESS", raising=False)
    node = _node_for(
        contract,
        platform_script=p2wpkh_script(contract["platform"]),
        value=Decimal("0.01"),
    )
    client = LTCClient("http://127.0.0.1:19443/wallet/w", "u", "p")
    client.rpc_call = node.rpc_call

    _drive_redeem(client, node, contract)

    assert len(node.broadcast) == 1, "the redeem must still go out with no fee address"
    assert "sendrawtransaction" in node.methods


# ---------------------------------------------------------------------------
# GRC's refund, added 2026-09-27. The last hole in this package's atomicity.
# ---------------------------------------------------------------------------


def test_all_three_clients_can_create_redeem_AND_refund():
    """THE ATOMICITY TRIANGLE, and GRC was missing a side until 2026-09-27.

    LTC got a refund on 2026-09-26 and BTC with it. GRC could FUND a contract and REDEEM
    one and had no way to get its own coins back. A contract that can be funded and
    cannot be recovered is the WORST of the three states -- worse than one that cannot be
    funded at all -- because the funding is the irreversible half.

    It mattered most on the leg GRC usually is: in atomic_swap_xrp_grc.py's GRC-first
    direction the Gridcoin leg carries the INITIATOR's longer timelock, so it is the leg
    still locked when a counterparty walks away. That is exactly what a refund is for,
    on the one chain that could not perform one.

    Asserted as a matrix rather than three separate tests, because what matters is the
    PARITY: a fourth client, or a fourth method, should fail this rather than be
    discovered missing on a funded contract.
    """
    for client in (BTCClient, LTCClient, GRCClient):
        for method in ("create_contract", "redeem_contract", "refund_contract"):
            assert method in vars(client), f"{client.__name__} has no {method}"


def test_the_grc_refund_is_keyword_only_like_its_ltc_sibling():
    """`refund_privkey` versus the participant key is exactly the confusion positional
    arguments create on a fund path, which is the reason LTC's is keyword-only. The two
    are deliberately identical so a reader can diff them."""
    for client in (LTCClient, GRCClient):
        parameters = inspect.signature(client.refund_contract).parameters
        for name in ("contract_txid", "contract_vout", "redeem_script", "locktime",
                     "refund_privkey", "refund_address"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{client.__name__}.refund_contract's {name} must be keyword-only"
            )


def test_the_grc_refund_and_redeem_NEVER_TOUCH_THE_WALLET_LOCK(contract):
    """THIS TEST USED TO ASSERT THE OPPOSITE, AND IT WAS PINNING A STALE GUARD.

    It was `test_the_grc_refund_unlocks_the_wallet_before_it_signs`, and it read:

        source = inspect.getsource(GRCClient.refund_contract)
        assert "self.ensure_fully_unlocked()" in source

    Two things wrong with that, and the second is the one this repository has a
    principle about.

    ONE: THE CLAIM WAS FALSE. Its docstring justified the unlock as "GRC adds is an
    ENCRYPTED wallet: `signrawtransaction` needs it open". redeem_contract()'s own
    docstring, a hundred lines up in the same file, says "The old `signrawtransaction`
    call is gone." Measured 2026-09-28: broadcast_refund() calls
    lookup_contract_output(), build_refund_spend(wif=refund_privkey) and
    sendrawtransaction. The signing happens IN PROCESS with the key the function is
    handed. Nothing consults the lock.

    What the guard actually did was `walletlock` then `walletpassphrase <pass> 120`. On
    the operator's Gridcoin wallet -- unlocked FOR STAKING ONLY with a deadline about a
    year out -- `walletlock` DISCARDS that deadline and STOPS STAKING, which Gridcoin's
    own CWallet::ElevateToFull docstring names as the cost. So the refund, the path that
    matters most on the chain that carries the initiator's longer timelock, mutated the
    operator's wallet in order to enable signing that does not go through the wallet.

    TWO: IT WAS A SOURCE-TEXT ASSERTION. "Verify by row-level behavioral outcome, never
    by literal SQL text" is about gates and SQL, and the shape is general: a test that
    greps the code under test for a substring measures the spelling, not the behavior,
    and goes green for a call that is present and unreachable. This one is driven through
    the real client against a node that REFUSES to answer any wallet-lock call, so the
    absence is what the daemon sees rather than what the file says.
    """
    node = _node_for(contract, prefix=GRIDCOIN_PREFIX)
    refused: list[str] = []
    real_rpc = node.rpc_call

    def refusing_rpc(method, params=None):
        if method in ("walletlock", "walletpassphrase"):
            refused.append(method)
            raise AssertionError(
                f"{method} must never be reached from a refund or a redeem: both sign with "
                f"the key they are handed, and this call locks a staking wallet"
            )
        return real_rpc(method, params)

    # A passphrase IS configured, which is the only case where the guard did anything. With
    # it unset ensure_fully_unlocked() returns immediately, so a test with no passphrase
    # would pass whether the call were there or not -- the shape of green test that measures
    # nothing.
    client = GRCClient("http://127.0.0.1:15715", "u", "p", wallet_passphrase=FIXTURE_UNLOCK_VALUE)
    client.rpc_call = refusing_rpc

    txid = client.refund_contract(
        contract_txid=contract["txid"],
        contract_vout=contract["vout"],
        redeem_script=contract["redeem_script"],
        locktime=LOCKTIME,
        refund_privkey=contract["refund"].wif,
        refund_address=contract["refund"].address,
    )
    assert txid == "dd" * 32
    assert refused == [], "the refund reached the wallet lock"

    client.rpc_call = refusing_rpc
    _drive_redeem(client, node, contract)
    assert refused == [], "the redeem reached the wallet lock"


def test_create_contract_KEEPS_its_unlock_because_sendtoaddress_needs_one(contract):
    """THE DIFFERENCE BETWEEN THE THREE PATHS, and why this is not a blanket removal.

    create_contract() funds with `sendtoaddress`, which asks the WALLET to build and sign a
    transaction -- that genuinely needs an unlocked wallet, and removing the guard there
    would turn a readable refusal into a failure at the send. The redeem and the refund ask
    the wallet for nothing but `sendrawtransaction`, which consults no lock.

    Asserted on the call graph rather than the source text: ensure_fully_unlocked is
    replaced with a recorder, and what is measured is which of the three paths reaches it.
    """
    node = _node_for(contract, prefix=GRIDCOIN_PREFIX)
    client = GRCClient("http://127.0.0.1:15715", "u", "p", wallet_passphrase=FIXTURE_UNLOCK_VALUE)
    client.rpc_call = node.rpc_call
    reached: list[str] = []
    client.ensure_fully_unlocked = lambda *a, **k: reached.append("unlock")

    _drive_redeem(client, node, contract)
    client.refund_contract(
        contract_txid=contract["txid"], contract_vout=contract["vout"],
        redeem_script=contract["redeem_script"], locktime=LOCKTIME,
        refund_privkey=contract["refund"].wif, refund_address=contract["refund"].address,
    )
    assert reached == [], "neither the redeem nor the refund may unlock"

    assert "self.ensure_fully_unlocked()" in inspect.getsource(GRCClient.create_contract), (
        "and create_contract KEEPS it: sendtoaddress asks the wallet to build a transaction"
    )


def test_no_platform_fee_is_charged_on_any_refund():
    """A refund returns the funder's OWN coins after a counterparty failed to show. The
    swap did not happen, so there is no service to charge for -- and charging one would
    take a cut of a recovery. None of the three refunds mentions the fee at all, which is
    asserted here rather than left as an absence nobody checks."""
    for client in (BTCClient, LTCClient, GRCClient):
        source = inspect.getsource(client.refund_contract)
        assert "platform_fee" not in source, f"{client.__name__} charges a fee on a refund"
        assert "extra_outputs" not in source, f"{client.__name__} adds an output to a refund"


# ---------------------------------------------------------------------------
# GRIDCOIN'S createrawtransaction TAKES TWO ARGUMENTS, AND THE REFUND PATH ASKED FOR THREE
# ---------------------------------------------------------------------------


def test_the_refund_builder_NEVER_asks_for_a_third_createrawtransaction_argument(contract):
    """MEASURED ON THE OPERATOR'S DAEMON 2026-09-28, which answered with its own help text:

        createrawtransaction: code=-1
        Arguments:
        1. "transactions"  (string, required) A json array of json objects
        2. "outputs"       (string, required) a json object with outputs

    The refund builder appended a THIRD argument -- Bitcoin Core's `locktime`, which also makes
    Core set each input's sequence to SEQUENCE_FINAL-1. That call can never succeed on
    Gridcoin, so `GRCClient.refund_contract()` could not build a transaction AT ALL. The refund
    branch of a Gridcoin HTLC had never executed, and this is why: it failed at the first RPC,
    before any script ran, which is why it never looked like a script problem.

    The assertion is on the ARGUMENT COUNT rather than on the outcome, because a daemon that
    ignored the extra argument would let a wrong call pass unnoticed -- and the Bitcoin and
    Litecoin daemons do exactly that, which is how this survived for as long as it did.
    """
    calls = []

    def _rpc(method, params=None):
        calls.append((method, list(params or [])))
        if method == "createrawtransaction":
            inputs, outputs = params[0], params[1]
            entry = inputs[0]
            laid = [(coins_to_satoshis(Decimal(str(v))), contract["destination"].p2pkh_script)
                    for v in outputs.values()]
            return _manual_unsigned_transaction(entry["txid"], int(entry["vout"]), laid, b"\x02\x00\x00\x00",
                                                final=True)
        raise AssertionError(f"{method} must not be reached")

    # THE WHOLE REFUND IS BUILT FROM THAT ONE RPC, which is worth asserting rather than
    # stopping at the call shape: `createrawtransaction` is the only daemon call on this path,
    # so a test that reached it and gave up would leave the signing and the locktime unchecked.
    spend = build_refund_spend(
            asset="GRC",
            rpc_call=_rpc,
            contract_txid=contract["txid"],
            contract_vout=contract["vout"],
            contract_value=Decimal("1.0"),
            redeem_script=contract["redeem_script"],
            wif=contract["refund"].wif,
            destination_address=contract["destination"].address,
            locktime=3296338,
        )

    created = [params for method, params in calls if method == "createrawtransaction"]
    assert created, "createrawtransaction was never called"
    assert len(created[0]) == 2, (
        f"createrawtransaction was called with {len(created[0])} arguments. Gridcoin takes "
        f"exactly 2 and answers code=-1 to a third; the locktime is set by "
        f"htlc_spend.with_locktime() instead"
    )
    assert [method for method, _ in calls] == ["createrawtransaction"], (
        f"and it is the ONLY daemon call the refund build makes, so nothing else can be the "
        f"reason a chain refuses it. Calls were {[m for m, _ in calls]}"
    )

    # AND THE BYTES CARRY BOTH FIELDS. The argument count alone would pass for a build that
    # dropped the locktime entirely, which is a refund that CLTV refuses for the right reason
    # at the wrong time -- indistinguishable, on a chain answering `-22`, from the refusal the
    # refund exists to avoid.
    built = parse_transaction(bytes.fromhex(spend.raw_hex), contract["txid"], contract["vout"])
    assert built.suffix[:4] == struct.pack("<I", 3296338), "the nLockTime reached the bytes"
    assert built.inputs[0][2] == struct.pack("<I", 0xFFFFFFFE), (
        "and so did the non-final sequence -- CLTV fails outright without it"
    )


def test_with_locktime_sets_BOTH_the_nlocktime_and_the_sequence():
    """A correct nLockTime with a FINAL sequence is refused by the script, and looks identical
    to a refund that is merely too early.

    CLTV fails outright on an input whose sequence is 0xffffffff no matter what the heights
    say, so setting one field and not the other produces a refusal that means something else
    entirely -- which on a chain answering `-22 TX rejected` is indistinguishable from the
    refusal the refund is trying to avoid. They are set together, and asserted together.
    """
    script = b"\x76\xa9\x14" + b"\x22" * 20 + b"\x88\xac"
    raw = bytes.fromhex(_manual_unsigned_transaction(
        "cd" * 32, 1, [(100_000, script)], b"\x02\x00\x00\x00", final=True))
    parsed = parse_transaction(raw, "cd" * 32, 1)

    assert parsed.inputs[0][2] == SEQUENCE_FINAL_BYTES, "the daemon's answer is final"
    assert parsed.suffix[:4] == b"\x00\x00\x00\x00", "and carries nLockTime 0"

    timelocked = with_locktime(parsed, 3296338)

    assert timelocked.inputs[0][2] == struct.pack("<I", 0xFFFFFFFE), "non-final, or CLTV fails"
    assert timelocked.suffix[:4] == struct.pack("<I", 3296338)
    assert timelocked.outputs == parsed.outputs, "and nothing else moves"
    assert timelocked.prefix == parsed.prefix


def test_with_locktime_keeps_GRIDCOINS_TRAILING_CONTRACTS_BYTE():
    """The suffix is four bytes on Bitcoin and FIVE on Gridcoin v2, which adds an empty
    vContracts vector after the nLockTime.

    Replacing the whole suffix would drop that byte, and the transaction would then fail to
    re-serialize to what any daemon expects -- silently, because only the leading four bytes
    look like a locktime. Only those four are replaced; the rest is carried verbatim, which is
    the rule ParsedTransaction already follows for everything it does not interpret.
    """
    script = b"\x76\xa9\x14" + b"\x33" * 20 + b"\x88\xac"
    gridcoin_prefix = b"\x02\x00\x00\x00" + struct.pack("<I", 1790622817)
    raw = bytes.fromhex(_manual_unsigned_transaction(
        "ef" * 32, 0, [(100_000, script)], gridcoin_prefix, final=True)) + b"\x00"
    parsed = parse_transaction(raw, "ef" * 32, 0)
    assert len(parsed.suffix) == 5, f"a Gridcoin v2 suffix is 5 bytes, got {len(parsed.suffix)}"

    timelocked = with_locktime(parsed, 3296338)

    assert timelocked.suffix == struct.pack("<I", 3296338) + b"\x00"
    assert timelocked.serialize()[-1:] == b"\x00", "the empty vContracts byte survives"


def test_with_locktime_REFUSES_a_suffix_too_short_to_hold_one():
    """It signs what it builds, and there is no version of guessing that can be taken back."""
    stub = ParsedTransaction(prefix=b"\x02\x00\x00\x00", inputs=(), outputs=(), suffix=b"\x00\x00")
    with pytest.raises(TransactionLayoutError) as raised:
        with_locktime(stub, 1)
    assert "cannot hold" in str(raised.value)
    assert "Nothing was modified" in str(raised.value)
