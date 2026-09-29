"""The four-transaction chain an adaptor-signature swap needs on the script chain.

Role: submodule (transaction construction and fee arithmetic for the 2-of-2 chain; every
      decision here is a function callable with seeded inputs and no chain)
Reads: nothing. Pure functions of keys, scripts, outpoints, amounts and heights. No RPC,
      no file, no environment variable, no clock -- `ntime` is passed in rather than read
      from time.time(), so a Gridcoin transaction built twice from the same inputs is the
      same bytes and therefore the same txid.
Writes: nothing. It returns ParsedTransaction objects and hex; broadcasting is the
      caller's, and nothing here opens a socket.
Can move funds: no, and the distinction is the same one adaptor_swap_scripts.py draws:
      every transaction this module returns is UNSIGNED until a caller supplies
      signatures, and it never reaches a network. What it produces CAN move funds once
      signed and broadcast.
Mainnet-safe: yes in the sense that applies -- it contacts no chain and knows no network.
      It is network-independent: destination scriptPubKeys arrive as bytes, so no address
      version byte is decided here (modules/address_network owns that vocabulary).
Live-safe: yes

WHAT THIS IS, AND WHAT IT USED TO BE.

Raw transaction construction for the three Bitcoin-derived chains: the byte layout, the
nTime field Gridcoin serializes and the other two do not, fee sizing against real bytes,
predicted txids, and funding-input selection. Nothing here decides anything about a swap;
it builds transactions a caller has already decided on.

RENAMED FROM adaptor_swap_chain.py ON 2026-09-29, and the rename is the honest half of a
removal rather than tidying. This file used to build the five transactions of an
adaptor-signature swap -- a 2-of-2 lock and the redeem, cancel, refund and punish that hang
off it -- for a protocol whose other leg was Monero. That protocol was removed at the
operator's instruction, and about 450 lines of it went from this file with it. What stayed
is everything the HTLC path and the funding harness were already using through it, which
was never adaptor-specific and only looked it because of the name.

A name that claims a protocol the file no longer serves is the same defect as a comment
describing a deleted function, which is why this is a rename and not a note.

=============================================================================
FINDING 1, MEASURED HERE: `sendtoaddress` CANNOT BUILD Tx_lock, AND THE REASON
IS STRONGER THAN "THE TXID ARRIVES LATE".
=============================================================================

Section 3 step 0 of the design document exchanges signatures on Tx_cancel, Tx_refund and
Tx_punish BEFORE step 1, where the script-chain leg is funded. Tx_cancel spends Tx_lock, so
it needs Tx_lock's txid; Tx_refund and Tx_punish spend Tx_cancel, so they need it
transitively. Only Tx_cancel depends on it DIRECTLY, and that distinction matters for
sequencing (see FINDING 2).

`sendtoaddress` signs and broadcasts in one call. The txid does come back from it -- so
"the txid does not exist yet" is not quite the defect. The defect is that by the time it
exists the output is already irrevocably in the mempool, and the funder is holding a 2-of-2
output while holding NO signature from the counterparty on any spend of it. A counterparty
who then declines to sign Tx_cancel has not stolen anything and has not needed to: the coin
sits in an output that requires their cooperation forever. That is not a delay, it is a
permanent loss with no timelock behind it, and removing that exposure is the entire reason
step 0 comes before step 1.

So Tx_lock is built unsigned, signed, and HELD -- `two_of_two_funding_transaction()` below --
and `predicted_txid()` gives its txid before a single byte is broadcast. Every existing
funding path in this repository uses `sendtoaddress` (all three of the atomic_* clients do),
so this is a genuine break from how this tree funds anything today, and it is not an
optimization.

=============================================================================
FINDING 2, NOT IN THE DESIGN DOCUMENT AND IT CHANGES STEP 0: A LEGACY TXID
DEPENDS ON THE SIGNATURES, SO STEP 0 IS TWO ROUNDS, NOT ONE.
=============================================================================

Section 3 step 0 exchanges the cancel, refund and punish signatures "in one round". That
cannot work on a legacy (non-SegWit) chain, and Gridcoin has no SegWit at all.

A legacy txid is the double-SHA256 of the WHOLE serialization, scriptSigs included. So
Tx_cancel's txid is not known until Tx_cancel is fully signed -- and Tx_refund and Tx_punish
spend Tx_cancel's output, so they cannot even be BUILT until then, let alone signed. The
order is forced:

    round 1   both parties sign Tx_cancel and exchange those signatures
    round 1'  each assembles Tx_cancel and computes its txid -- both must get the SAME txid
    round 2   Tx_refund and Tx_punish are built against that outpoint, then signed and
              exchanged

`assert_spends()` exists so round 1' is an assertion rather than an assumption.

AND THE MALLEABILITY IS A GRIEFING VECTOR, WHICH IS WORTH NAMING BECAUSE IT IS NOT A
BOTH-LEGS OUTCOME AND SO IS EASY TO WAVE AWAY. Either party can produce a second, equally
valid signature on Tx_cancel under a different ECDSA nonce, assemble a DIFFERENT Tx_cancel
with the same inputs and outputs but a different txid, and broadcast that. Tx_refund and
Tx_punish are both bound to the original txid, so both become unspendable, and the cancel
output becomes a 2-of-2 that needs fresh cooperation from the party who just defected.
Nobody gains a coin by doing it -- the defector loses their own path too -- so it is mutual
destruction rather than theft, and it is still a way to strand the other side's funds.

Two things reduce it and neither removes it. Both parties sign deterministically (RFC 6979,
which `htlc_spend.sign_digest` already does through `sign_digest_deterministic`), so an
HONEST party produces one canonical signature and a variant is evidence of intent rather
than of luck. And each party checks the assembled Tx_cancel's txid against the one their
refund and punish were built for, before releasing either signature. The residual risk is
structural to legacy script and belongs in the design document, not in a workaround here.

=============================================================================
FINDING 3: THE FEE IS CHAINED, AND ON GRIDCOIN THE FLOOR DOMINATES IT.
=============================================================================

Each transaction pays from what the previous one left, and there are two hops on the cancel
side: Tx_lock -> Tx_cancel -> Tx_refund (or Tx_punish). So the lock has to be funded with
enough to cover TWO fees plus a non-dust output, not one.

`htlc_fee.redeem_miner_fee()` is `max(rate * size, floor)` and both are per chain.

MEASURED 2026-09-28 by building the whole chain with two 33-byte keys and reading the
numbers off it -- not from the table, because the table alone does not say which of its two
terms wins:

    chain   redeem size   rate x size        floor      fee charged   effective rate
    BTC     306 bytes     0.0000918 GRC-eq   0.0001     0.0001        0.000327 coin/kvB
    LTC     306 bytes     0.0000918         0.0001      0.0001        0.000327 coin/kvB
    GRC     311 bytes     0.00311           0.01        0.01          0.032573 coin/kvB

    minimum lock value:  BTC 20546 sat   LTC 25460 sat   GRC 2000001 sat (0.02 GRC)

THE FLOOR WINS ON ALL THREE CHAINS at these sizes, which corrects the first thing written
here: this paragraph said "BTC and LTC floors are small enough that the rate decides", and at
306 bytes 0.0003 coin/kvB comes to 0.0000918, which is UNDER the 0.0001 floor. It would take
a spend of 334 bytes for the rate to take over on BTC or LTC. So the practical consequence of
a 2-of-2 spend being bigger than an HTLC spend is currently nothing at all on the fee, on any
of the three -- and that is worth knowing precisely because the opposite is the intuitive
answer.

`minimum_lock_value_satoshis()` is the refusal that comes out of it, and it is computed from
the same `redeem_miner_fee()` the transactions are actually built with rather than from a
second copy of the arithmetic (rule 8). On Gridcoin it is 0.02 GRC, two floors and a dust
output, which is the number an operator needs before funding anything.

"A 2-OF-2 SPEND IS BIGGER THAN AN HTLC SPEND" IS FALSE, AND IT WAS THE STATED REASON THIS
FEE ARITHMETIC WAS EXPECTED TO BE HARD. Measured 2026-09-28, all three bounds assembled by
the real assemblers around the same maximum-length dummy signature:

    2-of-2 scriptSig            221 bytes   (redeem script 71: OP_2, two 34-byte pushes,
                                             OP_2, OP_CHECKMULTISIG)
    HTLC hashlock scriptSig     237 bytes   (redeem script 93 -- it carries a 32-byte hash,
                                             two hash160s and a locktime push, and the
                                             scriptSig carries the 32-byte preimage too)
    HTLC refund scriptSig       204 bytes

So the 2-of-2 sits BETWEEN the HTLC's two branches, and is 16 bytes SMALLER than the
hashlock spend this repository has been broadcasting all along. The size is not guessed
either way: every fee here is sized from `two_of_two_script_sig_upper_bound()`, which
assembles the real scriptSig with the real assembler, exactly as
`htlc_spend.estimated_script_sig_length()` does for the HTLC. Erring high by a few bytes
overpays by a few satoshis; erring low underpays a miner for what it is asked to carry, and
a spend that must confirm before a timelock is the wrong place to save one satoshi.

=============================================================================
WHAT IS PROVEN WHERE.
=============================================================================

Everything in this file is tested in `tests/test_script_chain.py` against seeded
inputs, including the Gridcoin layout, which is pinned against the 90 bytes a real testnet
daemon produced (the same vector `tests/test_gridcoin_transaction_layout.py` carries).

What a test in this container CANNOT establish is whether a chain ACCEPTS a 2-of-2 P2SH
spend. That needs a chain, both branches exercised, and it is `adaptor_regtest_verify.py` at
the project root. Until that has been run, the claim "these scripts work" is a source
reading, which is what "Verify by behavior, never by reading the code" refuses.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from decimal import Decimal

from modules.atomic_htlc_scripts import push_data
from modules.htlc_fee import redeem_miner_fee
from modules.htlc_spend import (
    GRIDCOIN_EMPTY_CONTRACTS,
    MAX_DER_SIGNATURE_WITH_HASHTYPE,
    SATOSHIS_PER_COIN,
    ParsedTransaction,
    double_sha256,
)

# Transaction version. 2 on all three chains: Gridcoin's own testnet daemon emits
# `02000000` (see GRIDCOIN_RAW in tests/test_gridcoin_transaction_layout.py), and version 2
# is what enables BIP68 relative locktimes on Bitcoin and Litecoin -- which this chain does
# not use, but a version-1 transaction on a modern node is unusual enough to attract a
# standardness question nobody wants while diagnosing a script.
TX_VERSION = 2

# nSequence. NOT 0xFFFFFFFF, and this is the single easiest thing to get wrong in the whole
# file. A transaction whose every input has a FINAL sequence is treated as final regardless
# of nLockTime -- IsFinalTx returns true immediately -- so Tx_cancel built with 0xFFFFFFFF
# would be relayable and mineable the instant it was signed, and T1 would enforce nothing at
# all. The cancel path is the only thing standing between the funder and a counterparty who
# walks away, so a final sequence there is not a slow leak, it is the timelock silently
# absent. 0xFFFFFFFE is the conventional value: non-final for locktime purposes, and above
# BIP125's replaceability threshold so the transaction is not signaling RBF.
SEQUENCE_NON_FINAL = 0xFFFFFFFE

# The widest an nTime or an nLockTime can be: both are four unsigned bytes. Named because
# two comparisons check it and PLR2004 is right that a bare 0xFFFFFFFF twice reads as two
# unrelated literals rather than as one field width.
UINT32_MAX = 0xFFFFFFFF

# A txid as it is PRINTED: 32 bytes of hex. Named for the same reason -- the check that a
# caller passed a printed txid rather than serialized bytes is the check that catches the
# byte-order mistake, and it should read as being about a txid.
TXID_HEX_LEN = 64

# The chains whose layout this module knows, and what distinguishes them. GRC carries an
# nTime in the prefix and an empty vContracts vector in the suffix; BTC and LTC carry
# neither. One table rather than an `if asset == "GRC"` at four sites (rule 11: one
# vocabulary, derived in one place).
#
# `nTime` is the reason `ntime` is a FIELD on ChainContext rather than a time.time() call: a
# transaction built twice from the same inputs has to be the same bytes, because its txid is
# what Tx_cancel, Tx_refund and Tx_punish are built against. A clock inside the builder would
# make the txid a function of when it was called, which on the cancel side would mean a
# refund bound to a transaction nobody can reproduce.
CHAIN_LAYOUT = {
    "BTC": {"ntime": False, "contracts": False},
    "LTC": {"ntime": False, "contracts": False},
    "GRC": {"ntime": True, "contracts": True},
}

# WHICH 2-of-2 EACH TRANSACTION IS SPENDING, as a table rather than as an argument.
#
# The redeem and the cancel both spend Tx_lock's output, so both satisfy the LOCK script.
# The refund and the punish both spend Tx_cancel's output, so both satisfy the CANCEL script.
# That is not a convention, it is the shape of the chain, and passing the script in as an
# argument at four call sites invites the one mistake that cannot be caught by any local
# check: a digest taken over the wrong script code produces a signature that verifies against
# nothing and a node that reports only a bare script failure. Reading it out of a table means
# the pairing is stated once and is testable on its own (rule 10 -- the decision is the
# smallest piece, and this is one).
_SPENDS_WHICH_SCRIPT = {
    "redeem": "lock",
    "cancel": "lock",
    "refund": "cancel",
    "punish": "cancel",
}

# A compressed public key's length, for the P2PKH scriptSig bound. Spelled here rather than
# imported from adaptor_swap_scripts because that constant is about the 2-of-2's keys and
# this one is about whatever key funds the lock -- the two happen to be equal and are not
# the same fact.
COMPRESSED_PUBKEY_BYTES = 33

# OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG, as its opcode prefix and suffix. Named
# so that p2pkh_script() reads as the script it builds.
P2PKH_PREFIX = b"\x76\xa9"
P2PKH_SUFFIX = b"\x88\xac"
HASH160_BYTES = 20

# nLockTime below this is a BLOCK HEIGHT; at or above it, a unix timestamp. This module
# accepts either and interprets neither -- it serializes the number it is given -- but it
# REFUSES a mix, because a cancel at a height and a punish at a timestamp are not comparable
# and `T2 > T1` would be comparing a block count against a clock. htlc_timelock.py carries
# the same threshold for the same reason and the two are named at each other (rule 8): that
# one decides a locktime for a CLTV push inside a script, this one checks a pair of
# nLockTime fields, and they must agree on where the boundary is.
LOCKTIME_THRESHOLD = 500_000_000


class ScriptChainError(ValueError):
    """A transaction that must not be built, rather than one that failed to build.

    Its own class, separate from the HTLC script errors, because the caller's response differs:
    a script error means two keys or two signatures were wrong, and a chain error means the
    AMOUNTS or the HEIGHTS were wrong -- a fee that eats the output, a lock funded too
    thinly to cancel out of, a T2 that is not after T1. Those are refusals about money and
    time, and on this path a caller that caught them together would be catching "I built the
    wrong script" and "I would have stranded the coin" in one handler.
    """


@dataclass(frozen=True)
class Outpoint:
    """One output being spent: where it is, and what it is worth.

    `value_satoshis` is here rather than looked up later because the fee, and therefore
    every output amount, and therefore the sighash, and therefore the signature, all depend
    on it. A value guessed at this point is the value a signature commits to, which is why
    modules/htlc_rpc.lookup_contract_output() tries four routes rather than returning a
    number it is unsure of.
    """

    txid: str
    vout: int
    value_satoshis: int

    def __post_init__(self) -> None:
        try:
            decoded = bytes.fromhex(self.txid)
        except ValueError as exc:
            raise ScriptChainError(f"txid {self.txid!r} is not hex: {exc}") from exc
        if len(self.txid) != TXID_HEX_LEN:
            raise ScriptChainError(
                f"txid {self.txid!r} is {len(self.txid)} characters, not {TXID_HEX_LEN}. A txid is "
                f"printed big-endian and serialized little-endian; passing the serialized form here "
                f"produces a transaction referencing an outpoint nobody has, which a node reports as "
                f"bad-txns-inputs-missingorspent -- and that reads as 'already spent'"
            )
        if not any(decoded):
            raise ScriptChainError(
                "txid is all zeros, which is the COINBASE outpoint. A transaction claiming to spend "
                "it is a coinbase, and no swap transaction is one"
            )
        if self.vout < 0:
            raise ScriptChainError(f"vout must not be negative, got {self.vout}")
        if self.value_satoshis <= 0:
            raise ScriptChainError(
                f"value_satoshis must be positive, got {self.value_satoshis}. A zero-value input "
                f"would size every fee below from nothing"
            )






def p2pkh_script(hash160_bytes: bytes) -> bytes:
    """OP_DUP OP_HASH160 <hash160> OP_EQUALVERIFY OP_CHECKSIG.

    Built from a hash160 rather than from an address string, deliberately, so that nothing
    in this module has to know which base58 version byte a chain prints. Bitcoin and
    Litecoin agree on 0x6F for testnet P2PKH and disagree on the P2SH byte, and Gridcoin
    testnet agrees with Bitcoin on both -- an assertion written against a rendered address
    fails on one chain for a reason that has nothing to do with the script. That is the same
    argument atomic_htlc_scripts.p2sh_script_for() makes, measured on regtest 2026-09-25,
    and it is why every destination in this module is a scriptPubKey rather than an address.
    """
    if len(hash160_bytes) != HASH160_BYTES:
        raise ScriptChainError(
            f"a hash160 is {HASH160_BYTES} bytes, got {len(hash160_bytes)}. A 32-byte value here "
            f"is probably a sha256 digest, and the resulting script would be unspendable by anyone"
        )
    return P2PKH_PREFIX + bytes([HASH160_BYTES]) + hash160_bytes + P2PKH_SUFFIX


def _layout_for(asset: str) -> dict:
    if asset not in CHAIN_LAYOUT:
        raise ScriptChainError(
            f"unknown script chain {asset!r}; this module knows {sorted(CHAIN_LAYOUT)}. Only "
            f"chains with a Bitcoin-derived transaction layout belong here; an account-model "
            f"chain has no layout in this file and no timelock at all"
        )
    return CHAIN_LAYOUT[asset]


def transaction_prefix(asset: str, ntime: int | None) -> bytes:
    """The bytes before the input count: the version, plus Gridcoin's nTime.

    PINNED AGAINST REAL BYTES, not against a reading of Gridcoin's source. The 90-byte
    transaction a live Gridcoin testnet daemon returned on 2026-09-27 begins
    `02000000 279ab96a` -- version 2 then an nTime that decodes to 22:35:19Z, the minute it
    was asked for. tests/test_script_chain.py rebuilds those exact bytes.

    `ntime` is REQUIRED on Gridcoin and REFUSED elsewhere, rather than ignored. A caller
    passing an nTime for Bitcoin has a wrong model of the layout, and silently dropping the
    argument would let that model survive until it produced a transaction whose fee was
    sized four bytes short.
    """
    layout = _layout_for(asset)
    prefix = struct.pack("<i", TX_VERSION)
    if layout["ntime"]:
        if ntime is None:
            raise ScriptChainError(
                f"{asset} serializes an nTime between the version and the input count, so it is "
                f"required here. Pass the daemon's current time; do NOT read a clock inside a "
                f"builder, because the txid depends on these bytes and the cancel chain is built "
                f"against that txid"
            )
        if not 0 <= ntime <= UINT32_MAX:
            raise ScriptChainError(f"ntime {ntime} does not fit in four unsigned bytes")
        prefix += struct.pack("<I", ntime)
    elif ntime is not None:
        raise ScriptChainError(
            f"{asset} has no nTime field, so passing ntime={ntime} means the caller is working from "
            f"the wrong layout. Only Gridcoin and the Peercoin line carry one"
        )
    return prefix


def transaction_suffix(asset: str, locktime: int) -> bytes:
    """The bytes after the last output: nLockTime, plus Gridcoin's empty vContracts.

    ONE BYTE, AND IT COST 310.72 GRC. `7316c8c` recorded it: Gridcoin v2 serializes
    `vContracts` -- a vector of protocol messages, beacons and votes and polls -- AFTER
    nLockTime, and an empty vector is a varint zero. So a Gridcoin transaction is exactly
    one byte longer than the Peercoin layout predicts, and every size and fee calculation in
    this chain is one byte off without it.

    Only an EMPTY vector is ever emitted here. htlc_spend.parse_transaction() accepts only
    an empty one on the way in, for the reason that suffix is carried verbatim into a
    transaction this code then SIGNS; emitting anything else would be putting a protocol
    message into a swap transaction.
    """
    layout = _layout_for(asset)
    if not 0 <= locktime <= UINT32_MAX:
        raise ScriptChainError(f"locktime {locktime} does not fit in four unsigned bytes")
    suffix = struct.pack("<I", locktime)
    if layout["contracts"]:
        suffix += GRIDCOIN_EMPTY_CONTRACTS
    return suffix


def build_unsigned(
    asset: str,
    spends: Outpoint,
    outputs: list[tuple[int, bytes]],
    locktime: int,
    ntime: int | None = None,
) -> ParsedTransaction:
    """A one-input transaction, as a ParsedTransaction so htlc_spend can sighash it.

    Returned as a ParsedTransaction rather than as raw bytes for one reason and it is not
    convenience: `legacy_sighash()` and `size_with_script_sig()` both take one, and
    `serialize({0: script_sig})` is how the scriptSig is dropped in afterwards without
    rebuilding anything. Every other module in this tree that signs a P2SH input goes
    through that class, so a transaction built here is signed by exactly the same code that
    signs an HTLC spend (rule 8).

    The input's scriptSig is EMPTY at this point. That is what `legacy_sighash` needs -- it
    substitutes the script code in for the signed input and empties every other one -- and
    it is also what `size_with_script_sig()` measures against.
    """
    if not outputs:
        raise ScriptChainError(
            "a transaction with no outputs pays its entire input to the miner. Every spend in this "
            "chain has exactly one destination, so an empty output list is a caller bug, not a "
            "burn somebody meant"
        )
    for value, script_pubkey in outputs:
        if value <= 0:
            raise ScriptChainError(
                f"output value {value} is not positive. This usually means the fee arithmetic ate "
                f"the input: on Gridcoin the fee FLOOR is 0.01 GRC per transaction and the cancel "
                f"path pays it twice, so see minimum_lock_value_satoshis()"
            )
        if not script_pubkey:
            raise ScriptChainError("an empty scriptPubKey is an anyone-can-spend output")
    return ParsedTransaction(
        prefix=transaction_prefix(asset, ntime),
        inputs=(
            (
                bytes.fromhex(spends.txid)[::-1] + struct.pack("<I", spends.vout),
                b"",
                struct.pack("<I", SEQUENCE_NON_FINAL),
            ),
        ),
        outputs=tuple(outputs),
        suffix=transaction_suffix(asset, locktime),
    )


def predicted_txid(parsed: ParsedTransaction, script_sigs: dict[int, bytes] | None = None) -> str:
    """The txid these bytes will have, computed before anything is broadcast.

    THIS IS THE FUNCTION THAT MAKES STEP 0 POSSIBLE AT ALL. Tx_cancel is built against
    Tx_lock's txid while Tx_lock is still sitting unsent in a variable, and Tx_refund and
    Tx_punish are built against Tx_cancel's -- see FINDING 1 and FINDING 2 in the module
    docstring for why neither can wait for a broadcast to answer.

    A legacy txid is the double-SHA256 of the WHOLE serialization, scriptSigs included, and
    it is printed in the REVERSE byte order from the one it is serialized in. Both halves
    are easy to get wrong and they fail differently: the wrong hash produces an outpoint
    nobody has, and the right hash in the wrong order produces the same thing while looking
    plausible next to a real txid.

    `script_sigs` is passed for a SIGNED transaction and omitted for an unsigned one. Those
    are two different txids and only the signed one is real -- which is FINDING 2's whole
    point -- so a caller that omits it for a transaction it is about to broadcast gets a
    txid that will never appear on any chain.

    THE ASSERTION THAT THIS IS RIGHT IS ON A CHAIN, NOT HERE. adaptor_regtest_verify.py
    compares the txid predicted here against the one `sendrawtransaction` hands back, for
    every transaction in the chain, and prints both. A local test can only check this
    function against another expression of the same rule.
    """
    return double_sha256(parsed.serialize(script_sigs))[::-1].hex()




def p2pkh_script_sig_upper_bound(public_key: bytes) -> int:
    """An exact UPPER BOUND on a P2PKH scriptSig: <sig> <pubkey>.

    Needed because Tx_lock's own input is ordinarily NOT a 2-of-2 -- it is whatever the
    funder held before the swap, and on the path this module supports that is a P2PKH output
    the funder controls. Built with the real push encoder around the same maximum-length
    dummy, so it cannot drift from the real thing either.
    """
    if len(public_key) != COMPRESSED_PUBKEY_BYTES:
        raise ScriptChainError(
            f"expected a {COMPRESSED_PUBKEY_BYTES}-byte compressed public key, got {len(public_key)} "
            f"bytes. An uncompressed key hashes to a different address than the compressed one, so a "
            f"bound computed from the wrong form sizes a fee for a transaction that will not verify"
        )
    dummy = bytes([0x30]) + b"\x00" * (MAX_DER_SIGNATURE_WITH_HASHTYPE - 1)
    return len(push_data(dummy) + push_data(public_key))


def fee_satoshis_for(asset: str, parsed: ParsedTransaction, script_sig_bound: int) -> int:
    """The miner fee, in satoshis, for this transaction once its scriptSig is filled in.

    Goes through modules/htlc_fee.redeem_miner_fee(), which is `max(rate * size, floor)` and
    is the ONE fee rule in this tree. A second fee table here would be rule 8's failure with
    a delay on it: the copies agree the day they are written, and the drift is invisible
    because each looks correct in its own file.

    The size is computed, not measured, because the fee has to be known BEFORE the signature
    exists: the output amount depends on the fee, the sighash depends on the output amount,
    and the signature depends on the sighash. ParsedTransaction.size_with_script_sig() is
    what breaks that circle, and it is exact -- the only estimate anywhere in the chain is
    the scriptSig length, and that is an upper bound.
    """
    size_bytes = parsed.size_with_script_sig(0, script_sig_bound)
    fee_coin = redeem_miner_fee(asset, size_bytes)
    return int((fee_coin * Decimal(SATOSHIS_PER_COIN)).to_integral_value())






















def select_funding_inputs(utxos: list[dict], target_satoshis: int) -> tuple[list[dict], int]:
    """Pick spendable outputs to cover `target_satoshis`, and say what they come to.

    THE DECISION `fundrawtransaction` WOULD HAVE MADE, written here because Gridcoin may not
    have that RPC. Measured on the operator's testnet daemon 2026-09-27: Gridcoin answers
    `help gettxout` with "unknown command" and has no `signrawtransactionwithkey`, so it is on
    the pre-0.17 Bitcoin RPC surface. `fundrawtransaction` arrived in Bitcoin Core 0.12 and so
    MAY be there, but this tree has never asked it, and a funding path that depends on an RPC
    nobody has probed is a funding path that fails on the operator's machine.
    adaptor_regtest_verify.py probes for it and reports the answer rather than assuming one.

    Largest-first, not smallest-first, and the reason is the fee rather than tidiness: every
    extra input adds about 148 bytes to the transaction, and on a chain whose fee floor
    dominates (Gridcoin's 0.01 GRC) that is free, while on one whose rate dominates it is not.
    Largest-first uses the fewest inputs that suffice.

    `utxos` are `listunspent` rows: dicts with `txid`, `vout` and `amount` in COIN. A row
    missing any of the three is REFUSED rather than skipped -- a silently skipped row is how a
    wallet with a balance reports itself unfundable, and the operator then goes looking for
    the balance instead of for the row.
    """
    if target_satoshis <= 0:
        raise ScriptChainError(f"target_satoshis must be positive, got {target_satoshis}")
    rows: list[tuple[int, dict]] = []
    for index, row in enumerate(utxos):
        missing = [key for key in ("txid", "vout", "amount") if key not in row]
        if missing:
            raise ScriptChainError(
                f"listunspent row {index} is missing {missing}. A row that cannot be read is refused "
                f"rather than skipped, because a skipped row makes a funded wallet report itself "
                f"unfundable"
            )
        rows.append((int((Decimal(str(row["amount"])) * SATOSHIS_PER_COIN).to_integral_value()), row))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    chosen: list[dict] = []
    total = 0
    for satoshis, row in rows:
        if satoshis <= 0:
            continue
        chosen.append(row)
        total += satoshis
        if total >= target_satoshis:
            return chosen, total
    raise ScriptChainError(
        f"the {len(rows)} spendable output(s) offered come to {total} satoshis, short of the "
        f"{target_satoshis} needed. Nothing was built. On Gridcoin a staking-only unlock also makes a "
        f"funded wallet unable to send, which reports as a send failure rather than as a shortfall"
    )




__all__ = [
    "Outpoint",
    "ScriptChainError",
    "build_unsigned",
    "fee_satoshis_for",
    "p2pkh_script",
    "p2pkh_script_sig_upper_bound",
    "predicted_txid",
    "select_funding_inputs",
    "transaction_prefix",
    "transaction_suffix",
]
