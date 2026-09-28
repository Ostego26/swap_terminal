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

WHAT THIS IS, AND WHY IT IS A SEPARATE FILE FROM adaptor_swap_scripts.py.

`docs/monero_swap_protocol.md` section 2 names five transactions. `adaptor_swap_scripts.py`
builds the SCRIPTS and the signature assembly they are made of -- the 2-of-2 redeem script,
its P2SH wrapper, the OP_0-dummy scriptSig, and the sighash. This file builds the
TRANSACTIONS: Tx_lock's output, and the four spends that hang off it.

    Tx_lock     pays a 2-of-2 {A_pk, B_pk}. No hashlock: the adaptor REPLACES the
                hashlock, it does not join it.
    Tx_redeem   lock -> ALICE. Both signatures; Bob's is an adaptor pre-signature under Y_a.
    Tx_cancel   lock -> a SECOND 2-of-2 {A_pk, B_pk}, nLockTime T1.
    Tx_refund   cancel output -> BOB. Both signatures; Alice's is a pre-signature under Y_b.
    Tx_punish   cancel output -> ALICE, nLockTime T2 > T1.

The adaptor signatures themselves are `modules/adaptor_ecdsa.py`'s and the protocol
decisions around them are `modules/monero_swap_protocol.py`'s. This module's only
contribution to that is the DIGEST: `digest` on each ChainTransaction below is exactly what
`adaptor_ecdsa.pre_sign` is handed and exactly what the completed signature is over, which
is the property that makes a pre-signature and its completion provably about the same bytes.

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

Everything in this file is tested in `tests/test_adaptor_swap_chain.py` against seeded
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

from modules.adaptor_swap_scripts import (
    AdaptorScriptError,
    two_of_two_p2sh_script,
    two_of_two_script_sig,
    two_of_two_sighash,
)
from modules.atomic_htlc_scripts import push_data
from modules.htlc_fee import assert_no_output_is_dust, dust_threshold_satoshis, redeem_miner_fee
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


class AdaptorChainError(ValueError):
    """A transaction that must not be built, rather than one that failed to build.

    Its own class, separate from AdaptorScriptError, because the caller's response differs:
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
            raise AdaptorChainError(f"txid {self.txid!r} is not hex: {exc}") from exc
        if len(self.txid) != TXID_HEX_LEN:
            raise AdaptorChainError(
                f"txid {self.txid!r} is {len(self.txid)} characters, not {TXID_HEX_LEN}. A txid is "
                f"printed big-endian and serialized little-endian; passing the serialized form here "
                f"produces a transaction referencing an outpoint nobody has, which a node reports as "
                f"bad-txns-inputs-missingorspent -- and that reads as 'already spent'"
            )
        if not any(decoded):
            raise AdaptorChainError(
                "txid is all zeros, which is the COINBASE outpoint. A transaction claiming to spend "
                "it is a coinbase, and no swap transaction is one"
            )
        if self.vout < 0:
            raise AdaptorChainError(f"vout must not be negative, got {self.vout}")
        if self.value_satoshis <= 0:
            raise AdaptorChainError(
                f"value_satoshis must be positive, got {self.value_satoshis}. A zero-value input "
                f"would size every fee below from nothing"
            )


@dataclass(frozen=True)
class ChainContext:
    """Everything the four transactions share, so it cannot be assembled two different ways.

    Grouped rather than passed as loose arguments for the reason
    monero_swap_protocol.TimelockPlan gives: ruff's PLR0913 was right that this many wanted
    grouping, rule 12 says to answer a complexity finding by extracting rather than by
    raising a ceiling, and -- the half that actually matters here -- a context that travels
    as one value cannot be assembled inconsistently at two call sites. The lock script, the
    cancel script and the two destinations have to be the SAME bytes in all four
    transactions, because Tx_cancel's output must be exactly the script Tx_refund and
    Tx_punish satisfy. A second, subtly different copy at one call site produces a cancel
    output nobody can spend, and nothing local would catch it.

      asset                  "GRC", "BTC" or "LTC". Picks the layout and the fee rule.
      lock_redeem_script     the first 2-of-2 {A_pk, B_pk}. Tx_lock pays its P2SH; the
                             redeem and the cancel satisfy it.
      cancel_redeem_script   the SECOND 2-of-2. Tx_cancel pays its P2SH; the refund and the
                             punish satisfy it.
      alice_script           where Tx_redeem and Tx_punish pay -- a scriptPubKey, never an
                             address, so no version byte is decided in this module.
      bob_script             where Tx_refund pays.
      ntime                  Gridcoin's nTime. Required on GRC, refused on BTC and LTC, and
                             passed in rather than read off a clock (see CHAIN_LAYOUT).

    THE TWO 2-of-2s MAY BE THE SAME BYTES, AND THAT COSTS A DIAGNOSTIC RATHER THAN SAFETY.
    The design document says "a SECOND 2-of-2 {A_pk, B_pk}" -- the same two parties -- so
    byte-equal scripts are protocol-legal, and a signature cannot be replayed from one to the
    other because a legacy sighash commits to the OUTPOINT, which differs. Nothing here
    requires them to differ and nothing here makes them differ, because making them differ
    would mean either a third key pair (which the protocol does not have) or a different key
    ORDER (which is the transposition footgun adaptor_swap_scripts.two_of_two_script_sig()
    exists to warn about, deliberately introduced).

    What it costs is this: when the two scripts are equal, Tx_lock's output and Tx_cancel's
    output have the SAME scriptPubKey, so a P2SH scriptPubKey match cannot say which of the
    two it found -- and that match is how this repository locates every contract it has ever
    funded. A caller in that position must locate the cancel output by txid and vout instead,
    which is what adaptor_regtest_verify.py does and says on screen.
    """

    asset: str
    lock_redeem_script: bytes
    cancel_redeem_script: bytes
    alice_script: bytes
    bob_script: bytes
    ntime: int | None = None

    def __post_init__(self) -> None:
        _layout_for(self.asset)
        for name in ("lock_redeem_script", "cancel_redeem_script", "alice_script", "bob_script"):
            if not getattr(self, name):
                raise AdaptorChainError(f"{name} is empty; an empty script is an anyone-can-spend output")

    def script_for(self, stage: str) -> bytes:
        """The redeem script the named transaction's input satisfies. See _SPENDS_WHICH_SCRIPT."""
        if stage not in _SPENDS_WHICH_SCRIPT:
            raise AdaptorChainError(
                f"unknown stage {stage!r}; the chain is {sorted(_SPENDS_WHICH_SCRIPT)}. Tx_lock is not "
                f"in that list because it does not spend a 2-of-2, it CREATES one"
            )
        return self.lock_redeem_script if _SPENDS_WHICH_SCRIPT[stage] == "lock" else self.cancel_redeem_script

    @property
    def lock_script_pubkey(self) -> bytes:
        """The P2SH scriptPubKey Tx_lock pays, and the bytes a chain scan matches on."""
        return two_of_two_p2sh_script(self.lock_redeem_script)

    @property
    def cancel_script_pubkey(self) -> bytes:
        """The P2SH scriptPubKey Tx_cancel pays."""
        return two_of_two_p2sh_script(self.cancel_redeem_script)


@dataclass(frozen=True)
class ChainTransaction:
    """One unsigned transaction of the chain, with everything a signer needs beside it.

    Held together rather than returned as a tuple because the DIGEST and the TRANSACTION
    must not be able to drift: an adaptor pre-signature made over one transaction's digest
    and completed into a different transaction verifies against nothing, and the node
    reports a bare script failure. One object means the two came out of one call.

      name                 "redeem", "cancel", "refund" or "punish". Printed, not parsed.
      parsed               the unsigned transaction. `serialize({0: script_sig})` is how it
                           becomes the broadcastable bytes.
      digest               what BOTH parties sign, and what adaptor_ecdsa.pre_sign is given.
      redeem_script        the script the input is satisfying -- the script code the digest
                           was taken over, carried so the caller cannot pair a digest with
                           the wrong script when assembling.
      spends               the outpoint being spent.
      output_satoshis      what the single output pays.
      fee_satoshis         input value minus that. Stated rather than derived so a caller
                           can print the fee it MEANT beside the one the bytes encode
                           (ParsedTransaction.output_total is the other half of that check).
      script_sig_bound     the scriptSig length the fee was sized from. An UPPER bound; the
                           real scriptSig is this or a few bytes shorter, because a DER
                           integer whose top bit is clear encodes one byte smaller. See
                           htlc_spend.estimated_script_sig_length() for the measured
                           distribution -- the slack is commonly 1 or 2 and is NOT bounded
                           at 2, which a previous version of that docstring claimed.
      locktime             the nLockTime this transaction carries. 0 for redeem and refund.
    """

    name: str
    parsed: ParsedTransaction
    digest: bytes
    redeem_script: bytes
    spends: Outpoint
    output_satoshis: int
    fee_satoshis: int
    script_sig_bound: int
    locktime: int

    @property
    def unsigned_size_bound(self) -> int:
        """The serialized size once the scriptSig is filled in, as an upper bound.

        The number the fee was computed from, exposed so a caller can print it beside the fee
        and beside the REAL size afterwards (rule 14: state what the number means, next to
        the number). A real size larger than this is a defect in the bound, not a slack.
        """
        return self.parsed.size_with_script_sig(0, self.script_sig_bound)


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
        raise AdaptorChainError(
            f"a hash160 is {HASH160_BYTES} bytes, got {len(hash160_bytes)}. A 32-byte value here "
            f"is probably a sha256 digest, and the resulting script would be unspendable by anyone"
        )
    return P2PKH_PREFIX + bytes([HASH160_BYTES]) + hash160_bytes + P2PKH_SUFFIX


def _layout_for(asset: str) -> dict:
    if asset not in CHAIN_LAYOUT:
        raise AdaptorChainError(
            f"unknown script chain {asset!r}; this module knows {sorted(CHAIN_LAYOUT)}. Monero is "
            f"the OTHER leg of this swap and has no script, no transaction layout here, and no "
            f"timelock at all"
        )
    return CHAIN_LAYOUT[asset]


def transaction_prefix(asset: str, ntime: int | None) -> bytes:
    """The bytes before the input count: the version, plus Gridcoin's nTime.

    PINNED AGAINST REAL BYTES, not against a reading of Gridcoin's source. The 90-byte
    transaction a live Gridcoin testnet daemon returned on 2026-09-27 begins
    `02000000 279ab96a` -- version 2 then an nTime that decodes to 22:35:19Z, the minute it
    was asked for. tests/test_adaptor_swap_chain.py rebuilds those exact bytes.

    `ntime` is REQUIRED on Gridcoin and REFUSED elsewhere, rather than ignored. A caller
    passing an nTime for Bitcoin has a wrong model of the layout, and silently dropping the
    argument would let that model survive until it produced a transaction whose fee was
    sized four bytes short.
    """
    layout = _layout_for(asset)
    prefix = struct.pack("<i", TX_VERSION)
    if layout["ntime"]:
        if ntime is None:
            raise AdaptorChainError(
                f"{asset} serializes an nTime between the version and the input count, so it is "
                f"required here. Pass the daemon's current time; do NOT read a clock inside a "
                f"builder, because the txid depends on these bytes and the cancel chain is built "
                f"against that txid"
            )
        if not 0 <= ntime <= UINT32_MAX:
            raise AdaptorChainError(f"ntime {ntime} does not fit in four unsigned bytes")
        prefix += struct.pack("<I", ntime)
    elif ntime is not None:
        raise AdaptorChainError(
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
        raise AdaptorChainError(f"locktime {locktime} does not fit in four unsigned bytes")
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
        raise AdaptorChainError(
            "a transaction with no outputs pays its entire input to the miner. Every spend in this "
            "chain has exactly one destination, so an empty output list is a caller bug, not a "
            "burn somebody meant"
        )
    for value, script_pubkey in outputs:
        if value <= 0:
            raise AdaptorChainError(
                f"output value {value} is not positive. This usually means the fee arithmetic ate "
                f"the input: on Gridcoin the fee FLOOR is 0.01 GRC per transaction and the cancel "
                f"path pays it twice, so see minimum_lock_value_satoshis()"
            )
        if not script_pubkey:
            raise AdaptorChainError("an empty scriptPubKey is an anyone-can-spend output")
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


def two_of_two_script_sig_upper_bound(redeem_script: bytes) -> int:
    """An exact UPPER BOUND on a 2-of-2 scriptSig, before either signature exists.

    Assembled by the REAL assembler around a maximum-length dummy signature, exactly as
    htlc_spend.estimated_script_sig_length() does for the HTLC and for the same reason: the
    bound cannot drift from what is actually produced, because everything except the
    signature's length is known exactly and the signature is at its maximum.

    Measured 2026-09-28 for two 33-byte compressed keys: the redeem script is 71 bytes
    (OP_2, two 34-byte pushes, OP_2, OP_CHECKMULTISIG) and this returns 221 -- OP_0 at one
    byte, two 74-byte signature pushes, and a 72-byte push of the redeem script.

    The fee is computed from this, so the broadcast transaction is never larger than the one
    the fee was sized for. Erring high costs a few satoshis; erring low underpays a miner
    for what it is asked to carry, on a spend that has to confirm before a timelock.
    """
    dummy = bytes([0x30]) + b"\x00" * (MAX_DER_SIGNATURE_WITH_HASHTYPE - 1)
    return len(two_of_two_script_sig(dummy, dummy, redeem_script))


def p2pkh_script_sig_upper_bound(public_key: bytes) -> int:
    """An exact UPPER BOUND on a P2PKH scriptSig: <sig> <pubkey>.

    Needed because Tx_lock's own input is ordinarily NOT a 2-of-2 -- it is whatever the
    funder held before the swap, and on the path this module supports that is a P2PKH output
    the funder controls. Built with the real push encoder around the same maximum-length
    dummy, so it cannot drift from the real thing either.
    """
    if len(public_key) != COMPRESSED_PUBKEY_BYTES:
        raise AdaptorChainError(
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


def _spend_of(context: ChainContext, stage: str, spends: Outpoint,
              destination_script: bytes, locktime: int) -> ChainTransaction:
    """One 2-of-2 spend, fee-sized and sighashed. The shared body of all four builders.

    Every one of the four transactions is the same shape -- one 2-of-2 input, one output, an
    nLockTime that is either zero or a timelock -- and writing it four times is how the four
    drift. What differs between them is the stage, the outpoint, the destination and the
    locktime, which are exactly the arguments the callers vary, so the difference is visible
    at the call site rather than buried in four near-identical bodies (rule 8).

    The redeem script is NOT an argument: it is read out of the context by stage, because a
    digest taken over the wrong script code is the failure with no local symptom. See
    _SPENDS_WHICH_SCRIPT.
    """
    redeem_script = context.script_for(stage)
    bound = two_of_two_script_sig_upper_bound(redeem_script)
    # Built once with a placeholder output value to size the fee, then rebuilt with the real
    # one. The size does not depend on the VALUE -- an output value is eight fixed bytes --
    # so the placeholder cannot make the fee wrong. It is spelled as the input value so that
    # if this assumption ever stops holding, the placeholder is at least the right order of
    # magnitude rather than a 1 that would encode identically anyway.
    sizing = build_unsigned(
        context.asset, spends, [(spends.value_satoshis, destination_script)], locktime, context.ntime
    )
    fee = fee_satoshis_for(context.asset, sizing, bound)
    output_value = spends.value_satoshis - fee
    if output_value <= 0:
        raise AdaptorChainError(
            f"{stage}: a fee of {fee} satoshis is at or above the {spends.value_satoshis} satoshis "
            f"being spent, so nothing would be left to pay out. On {context.asset} the fee floor is "
            f"per transaction and the cancel path pays it twice -- see minimum_lock_value_satoshis()"
        )
    parsed = build_unsigned(
        context.asset, spends, [(output_value, destination_script)], locktime, context.ntime
    )
    # Refused HERE rather than by the node. A dust output is not relayed, so a cancel whose
    # output is dust is a cancel that cannot be published -- which removes the only path the
    # funder has out of the lock, silently, at the moment they need it.
    assert_no_output_is_dust(context.asset, list(parsed.outputs))
    return ChainTransaction(
        name=stage,
        parsed=parsed,
        digest=two_of_two_sighash(parsed, 0, redeem_script),
        redeem_script=redeem_script,
        spends=spends,
        output_satoshis=output_value,
        fee_satoshis=fee,
        script_sig_bound=bound,
        locktime=locktime,
    )


def build_redeem(context: ChainContext, lock: Outpoint) -> ChainTransaction:
    """Tx_redeem: the lock output to ALICE, no locktime.

    NO nLockTime, deliberately. The redeem is the transaction Alice wants confirmed as early
    and as deeply as possible -- hazard 3 in the design document is precisely the race
    between this confirming and T1 arriving -- so anything that delayed it would be working
    against the one quantity that bounds the only both-legs outcome in the protocol.

    Bob's half of the signature on this is an ADAPTOR PRE-SIGNATURE under Y_a, and `digest`
    is what it is made over. Bob withholds it until he has seen the Monero leg confirmed AND
    unlocked (step 3), and that withholding is his entire protection between steps 1 and 3 --
    monero_swap_protocol.redeem_presignature_may_be_released() is the refusal, and nothing
    here may compute or release it earlier "to have it ready".
    """
    return _spend_of(context, "redeem", lock, context.alice_script, 0)


def build_cancel(context: ChainContext, lock: Outpoint, t1_locktime: int) -> ChainTransaction:
    """Tx_cancel: the lock output to a SECOND 2-of-2, with nLockTime T1.

    T1 IS ENFORCED BY nLockTime AND NOT BY OP_CHECKLOCKTIMEVERIFY, and that is a design
    decision worth stating because the HTLC path in this repository does the opposite.

    An HTLC has to use CLTV because its refund branch is spendable by ONE key: the script
    itself must refuse before the deadline, since nothing else would. A 2-of-2 needs no
    opcode, because the output cannot move without both parties and the only jointly-signed
    transaction spending it carries nLockTime T1. Adding a CLTV here would be a second
    mechanism enforcing the same thing, which is rule 8's failure shape, and it would also
    put the deadline inside the script where changing T1 changes the lock ADDRESS.

    Two consequences, and both are load bearing:

      - The sequence must be non-final. SEQUENCE_NON_FINAL above carries the reasoning; with
        a final sequence nLockTime is ignored and T1 enforces nothing.
      - nLockTime finality is checked per transaction when a block is validated, not only by
        the mempool, so the claim to be tested is that a miner cannot include an early cancel
        any more than a node will relay one. That would be a STRONGER statement than the HTLC
        path can make about CLTV, which needs BIP65 active -- and it is why
        adaptor_regtest_verify.py does not have to mine past an activation height the way
        regtest_htlc_verify.py does. THAT CLAIM IS MEASURED ON A CHAIN BY THAT HARNESS, by
        asking the daemon to MINE an early cancel and recording what it says. It is not
        asserted from here, and until the harness has been run it is a reading of how
        validation is ordered rather than a measurement.

    Both parties hold both plain signatures on this from setup, so either can publish it once
    T1 has passed. Which is also hazard 1: from the moment the lock is funded, the
    counterparty can publish cancel at T1 and punish at T2 and take the coin, and the
    funder's only defense is publishing the refund in between.
    """
    _assert_locktime_is_set(t1_locktime, "T1")
    return _spend_of(context, "cancel", lock, context.cancel_script_pubkey, t1_locktime)


def build_refund(context: ChainContext, cancel_output: Outpoint) -> ChainTransaction:
    """Tx_refund: the cancel output back to BOB, no locktime.

    No locktime because it must be publishable the instant the cancel confirms -- the window
    Bob has to beat is Alice's punish at T2, and any delay here eats into it.

    Alice's half is an ADAPTOR PRE-SIGNATURE under Y_b, exchanged at SETUP, and the design
    document's step 0 explains why that is safe when Bob knows `s_b` and could complete it
    immediately: it spends an output Tx_cancel has not created yet, so completing it early
    produces a transaction with no input. And the scalar it leaks is worthless to Alice
    before she has locked any XMR.

    IT CANNOT BE BUILT AT SETUP IN ONE ROUND, though, and that is FINDING 2: this needs
    Tx_cancel's txid, which needs Tx_cancel's signatures. Round 2, not round 1.
    """
    return _spend_of(context, "refund", cancel_output, context.bob_script, 0)


def build_punish(context: ChainContext, cancel_output: Outpoint, t2_locktime: int) -> ChainTransaction:
    """Tx_punish: the cancel output to ALICE, with nLockTime T2 > T1.

    THIS IS COMPENSATION AND NOT RESTORATION, which is the thing most easily misread about
    it. If Alice publishes this, her XMR is locked forever -- Bob has no remaining incentive
    to leak `s_b` -- so she ends up with Bob's script coin instead of the trade she wanted.
    It exists so that a counterparty who vanishes cannot leave her with locked XMR and no
    recourse, and a punish path Bob could disarm would not be one.

    `T2 > T1` is checked by assert_timelocks_ordered(), but the interesting comparison is not
    that one: the SIZE of T2 - T1 is what decides how long Bob may be unresponsive without
    losing his coin, and that comparison is in seconds across three different block targets
    and lives in monero_swap_protocol.assert_timelock_ordering(). Two heights being in the
    right order says nothing about whether the gap between them is survivable.
    """
    _assert_locktime_is_set(t2_locktime, "T2")
    return _spend_of(context, "punish", cancel_output, context.alice_script, t2_locktime)


def _assert_locktime_is_set(locktime: int, label: str) -> None:
    if locktime <= 0:
        raise AdaptorChainError(
            f"{label} is {locktime}, and an nLockTime of 0 means NO TIMELOCK AT ALL -- the "
            f"transaction is final the instant it is signed. On the cancel path that is the whole "
            f"protection gone, silently, with a transaction that looks correct"
        )


def assert_timelocks_ordered(t1_locktime: int, t2_locktime: int) -> None:
    """T2 must be after T1, and both must be the same KIND of locktime.

    The kind check is the one worth having. Below LOCKTIME_THRESHOLD an nLockTime is a block
    HEIGHT; at or above it, a unix TIMESTAMP. A T1 at height 1400 and a T2 at timestamp
    1800000000 compare as `1800000000 > 1400` and pass every ordering check anybody would
    write, while meaning "T2 is fifty-seven years after the epoch and T1 is block 1400" --
    two quantities with no ordering between them at all. Mixing them is how a punish path
    becomes available immediately or never, and neither failure announces itself.
    """
    _assert_locktime_is_set(t1_locktime, "T1")
    _assert_locktime_is_set(t2_locktime, "T2")
    t1_is_height = t1_locktime < LOCKTIME_THRESHOLD
    t2_is_height = t2_locktime < LOCKTIME_THRESHOLD
    if t1_is_height != t2_is_height:
        raise AdaptorChainError(
            f"T1={t1_locktime} is a {'block height' if t1_is_height else 'unix timestamp'} and "
            f"T2={t2_locktime} is a {'block height' if t2_is_height else 'unix timestamp'}. "
            f"nLockTime switches meaning at {LOCKTIME_THRESHOLD}, so these two are not comparable "
            f"and 'T2 > T1' would be comparing a block count against a clock"
        )
    if t2_locktime <= t1_locktime:
        raise AdaptorChainError(
            f"T2={t2_locktime} is not after T1={t1_locktime}. The punish path must open strictly "
            f"LATER than the cancel path, or the counterparty who funded the script leg never gets "
            f"a window in which to refund and loses the coin outright"
        )


def assert_spends(parsed: ParsedTransaction, expected_txid: str, expected_vout: int) -> None:
    """Refuse a transaction that does not spend the outpoint it was supposed to.

    ROUND 1' OF FINDING 2, as an assertion. Tx_refund and Tx_punish are built against
    Tx_cancel's txid, and that txid is only fixed once Tx_cancel is fully signed. Before
    releasing either signature, each party re-derives the assembled cancel's txid and checks
    it against the outpoint their refund and punish actually reference. If a counterparty
    assembled a different cancel -- a second valid signature under a different nonce
    produces one -- these two numbers differ, and that is the moment to find out rather than
    after the cancel has confirmed and both remaining paths are dead.
    """
    outpoint = parsed.inputs[0][0]
    txid = outpoint[:32][::-1].hex()
    vout = struct.unpack("<I", outpoint[32:36])[0]
    if txid != expected_txid.lower() or vout != expected_vout:
        raise AdaptorChainError(
            f"this transaction spends {txid}:{vout}, not {expected_txid.lower()}:{expected_vout}. "
            f"On a legacy chain a txid depends on the signatures, so a cancel assembled from "
            f"different signatures has a different txid and every transaction built against the "
            f"old one is unspendable -- see FINDING 2"
        )


def minimum_lock_value_satoshis(context: ChainContext) -> int:
    """The least the lock may be funded with and still have a publishable cancel path.

    TWO FEES AND A NON-DUST OUTPUT, because the cancel path has two hops: the lock pays
    Tx_cancel's fee, and what Tx_cancel leaves pays Tx_refund's or Tx_punish's. A lock sized
    for one fee produces a cancel that confirms and a refund that cannot be built, which
    strands the coin in the second 2-of-2 at exactly the moment the funder needs it out.

    Computed by BUILDING the transactions rather than by adding up numbers from a table, so
    it cannot disagree with what build_cancel() and build_refund() actually charge. That is
    the same reason two_of_two_script_sig_upper_bound() assembles a real scriptSig instead of
    summing lengths: a second expression of one rule drifts from the first.

    It works backwards from the dust threshold: the smallest legal final output, plus the
    refund's fee, gives the smallest cancel output; plus the cancel's fee gives the answer.
    The punish fee is computed separately from the refund's rather than assumed equal -- the
    two differ only in nLockTime, which does not change the serialized length on BTC or LTC,
    and assuming that stays true on a fourth chain is the kind of assumption this file exists
    to avoid.
    """
    dust = max(
        dust_threshold_satoshis(context.asset, context.bob_script),
        dust_threshold_satoshis(context.asset, context.alice_script),
    )
    placeholder = Outpoint(txid="11" * 32, vout=0, value_satoshis=SATOSHIS_PER_COIN)
    cancel_bound = two_of_two_script_sig_upper_bound(context.cancel_redeem_script)
    lock_bound = two_of_two_script_sig_upper_bound(context.lock_redeem_script)
    refund_shape = build_unsigned(context.asset, placeholder, [(dust, context.bob_script)], 0, context.ntime)
    punish_shape = build_unsigned(
        context.asset, placeholder, [(dust, context.alice_script)], LOCKTIME_THRESHOLD - 1, context.ntime
    )
    second_hop_fee = max(
        fee_satoshis_for(context.asset, refund_shape, cancel_bound),
        fee_satoshis_for(context.asset, punish_shape, cancel_bound),
    )
    cancel_output = dust + second_hop_fee
    cancel_shape = build_unsigned(
        context.asset, placeholder, [(cancel_output, context.cancel_script_pubkey)], 1, context.ntime
    )
    return cancel_output + fee_satoshis_for(context.asset, cancel_shape, lock_bound)


def two_of_two_funding_transaction(context: ChainContext, spends: Outpoint,
                                   funding_script_sig_bound: int, lock_satoshis: int | None,
                                   change_script_pubkey: bytes | None) -> tuple[ParsedTransaction, int, int]:
    """Tx_lock, unsigned: one input the funder controls, paying the 2-of-2 plus change.

    Returns `(parsed, lock_vout, fee_satoshis)`. `lock_vout` is which output pays the 2-of-2,
    and it is RETURNED rather than assumed to be 0 -- the whole cancel chain is built against
    that index, and an assumed vout is how a chain of four transactions ends up spending the
    change.

    THIS IS THE FUNCTION THAT REPLACES `sendtoaddress`, and FINDING 1 in the module docstring
    is why it has to exist. `sendtoaddress` signs and broadcasts in one call, so by the time
    its txid is available the output is already in the mempool and the funder holds no
    counterparty signature on any spend of it. A counterparty who then declines to sign
    Tx_cancel has locked the coin permanently with no timelock behind it.

    So the shape is: build here, sign, hold, build the cancel chain against predicted_txid(),
    exchange those signatures, and only then broadcast. Every existing funding path in this
    repository does the opposite.

    `lock_satoshis=None` with `change_script_pubkey=None` spends the whole input into the lock
    minus the fee, which is the shape a harness uses when it prepared the input itself. On a
    real wallet there is change, and it must go somewhere the funder controls -- passing a
    script here that the funder does not control is an irreversible gift, and nothing
    downstream would notice.
    """
    if (lock_satoshis is None) != (change_script_pubkey is None):
        raise AdaptorChainError(
            "lock_satoshis and change_script_pubkey go together: either name an exact lock amount AND "
            "where the change goes, or pass None for both to sweep the whole input into the lock less "
            "the fee. Naming an amount with nowhere for the change to go would pay the remainder to "
            "the miner, and naming a change script with no amount has no meaning"
        )
    lock_script = context.lock_script_pubkey
    if lock_satoshis is None:
        sizing = build_unsigned(
            context.asset, spends, [(spends.value_satoshis, lock_script)], 0, context.ntime
        )
        fee = fee_satoshis_for(context.asset, sizing, funding_script_sig_bound)
        funded = spends.value_satoshis - fee
        if funded <= 0:
            raise AdaptorChainError(
                f"a fee of {fee} satoshis is at or above the {spends.value_satoshis} satoshis being "
                f"spent, so the lock would be funded with nothing"
            )
        outputs = [(funded, lock_script)]
    else:
        if lock_satoshis <= 0:
            raise AdaptorChainError(f"lock_satoshis must be positive, got {lock_satoshis}")
        sizing = build_unsigned(
            context.asset,
            spends,
            [(lock_satoshis, lock_script), (spends.value_satoshis, change_script_pubkey)],
            0,
            context.ntime,
        )
        fee = fee_satoshis_for(context.asset, sizing, funding_script_sig_bound)
        change = spends.value_satoshis - lock_satoshis - fee
        if change <= 0:
            raise AdaptorChainError(
                f"{spends.value_satoshis} satoshis in, {lock_satoshis} to the lock and {fee} in fee "
                f"leaves {change} in change. A non-positive change output is refused here rather "
                f"than dropped, because dropping it would pay the difference to the miner without "
                f"the caller having asked for that"
            )
        outputs = [(lock_satoshis, lock_script), (change, change_script_pubkey)]
    parsed = build_unsigned(context.asset, spends, outputs, 0, context.ntime)
    assert_no_output_is_dust(context.asset, list(parsed.outputs))
    return parsed, 0, fee


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
        raise AdaptorChainError(f"target_satoshis must be positive, got {target_satoshis}")
    rows: list[tuple[int, dict]] = []
    for index, row in enumerate(utxos):
        missing = [key for key in ("txid", "vout", "amount") if key not in row]
        if missing:
            raise AdaptorChainError(
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
    raise AdaptorChainError(
        f"the {len(rows)} spendable output(s) offered come to {total} satoshis, short of the "
        f"{target_satoshis} needed. Nothing was built. On Gridcoin a staking-only unlock also makes a "
        f"funded wallet unable to send, which reports as a send failure rather than as a shortfall"
    )


def assemble(transaction: ChainTransaction, first_signature: bytes, second_signature: bytes) -> tuple[str, str]:
    """Fill in the 2-of-2 scriptSig and return `(raw hex, txid)`.

    The signature ORDER is the caller's and it must match the key order in the redeem
    script -- adaptor_swap_scripts.two_of_two_script_sig()'s docstring carries what goes
    wrong when it does not, and it is the footgun that produces a same-length, well-formed
    scriptSig verifying nothing. Nothing here can check it: both orders are structurally valid
    bytes, and only an interpreter running the script can tell them apart. That is exactly why
    adaptor_regtest_verify.py asserts the transposed order is REFUSED on a real chain rather
    than asserting something about the bytes.

    The txid comes back beside the hex because this is the moment it becomes real (FINDING 2)
    and because the next transaction in the chain is built against it.
    """
    script_sig = two_of_two_script_sig(first_signature, second_signature, transaction.redeem_script)
    raw = transaction.parsed.serialize({0: script_sig})
    return raw.hex(), double_sha256(raw)[::-1].hex()


__all__ = [
    "AdaptorChainError",
    "AdaptorScriptError",
    "ChainContext",
    "ChainTransaction",
    "Outpoint",
    "assemble",
    "assert_spends",
    "assert_timelocks_ordered",
    "build_cancel",
    "build_punish",
    "build_redeem",
    "build_refund",
    "build_unsigned",
    "fee_satoshis_for",
    "minimum_lock_value_satoshis",
    "p2pkh_script",
    "p2pkh_script_sig_upper_bound",
    "predicted_txid",
    "select_funding_inputs",
    "transaction_prefix",
    "transaction_suffix",
    "two_of_two_funding_transaction",
    "two_of_two_script_sig_upper_bound",
]
