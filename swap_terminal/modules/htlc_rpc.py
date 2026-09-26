#!/usr/bin/env python3
"""The three RPC conversations an HTLC redeem needs, shared by all three clients.

Role: submodule (it talks to a daemon and delegates every decision downward to
      modules/htlc_spend.py and modules/htlc_fee.py)
Reads: a chain daemon through the caller's own `rpc_call` -- gettxout,
       gettransaction, getrawtransaction, getwalletinfo, getdescriptorinfo,
       createrawtransaction. Every one of those is read-only except the import.
Writes: THE WALLET, and only through the optional watch-only import, which
       cannot move a coin. Nothing here broadcasts: build_hashlock_spend()
       returns hex and the caller sends it.
Can move funds: no directly. It builds and SIGNS a spend of a funded contract;
       whoever broadcasts what it returns moves the coins. Treat a change here
       as fund movement.
Mainnet-safe: NO. It signs a transaction that is valid on whatever chain the
       `rpc_call` it was handed is pointed at.

WHY ONE FILE INSTEAD OF THREE COPIES.

modules/atomic_btc_client.py, modules/atomic_ltc_client.py and
modules/atomic_grc_client.py carried the same four defects, each spelled
slightly differently, and the divergence table in all three headers records
seventeen rows on which no two of them agree. Fixing one bug three times by
hand is how those seventeen rows got there (rule 8: two copies of one rule is a
bug with a delay on it). What is genuinely shared lives here; what genuinely
differs -- the platform fee, the wallet unlock, the amount keyword -- stays in
each client with a comment naming the others.

THE THREE CONVERSATIONS

  lookup_contract_output()   read a contract output back WITHOUT -txindex.
  build_hashlock_spend()     build, size the fee for, and sign the spend.
  ensure_watch_only_import() best-effort wallet visibility, on either wallet type.

WHAT WAS MEASURED, 2026-09-25, AGAINST REAL REGTEST DAEMONS.

  Bitcoin Core 28.1.0     Litecoin Core 0.21.4

  `redeem_contract()`'s first statement was `getrawtransaction(txid, True)`,
  which searches ONLY the mempool unless the node runs -txindex. On both chains
  it answered:

      code=-5, No such mempool transaction. Use -txindex or provide a block
      hash to enable blockchain transaction queries. Use gettransaction for
      wallet transactions.

  Every contract a real swap redeems has been CONFIRMED -- that is what the
  counterparty waited for before revealing anything -- so the redeem path
  failed on its own first line, always, before reaching anything to do with
  HTLCs. The daemon's error names the three fixes and this module takes the
  ones that do not re-shape the operator's node: -txindex is NOT used, because
  adding it to an existing datadir forces a reindex.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from microfortnights import format_duration
from modules.atomic_htlc_scripts import p2sh_script_for
from modules.htlc_fee import (
    assert_no_output_is_dust,
    assert_within_broadcast_ceiling,
    describe_fee,
    minimum_fee_coin,
    redeem_miner_fee,
)
from modules.htlc_spend import (
    decode_wif,
    estimated_script_sig_length,
    hashlock_script_sig,
    legacy_sighash,
    parse_transaction,
    participant_key_matches_script,
    public_key_for,
    satoshis_to_coins,
    sign_digest,
)

logger = logging.getLogger(__name__)

# How many times the fee is allowed to be re-derived from the transaction it
# changes the size of. The circle closes in two passes and not more: the output
# AMOUNTS depend on the fee, but the output SIZES do not -- eight bytes of value
# and the same scriptPubKey whatever the number in them -- so the size measured
# in pass one is the size in pass two, and the fee computed from it is stable.
# A third pass would mean that assumption is false, and this module refuses
# rather than looping: an unstable fee is a defect, not a thing to average.
MAX_FEE_PASSES = 2

# SECONDS between polls in wait_for_tx_output(). Seconds, not microfortnights,
# because it is handed to time.sleep() -- rule 6's report-versus-interface
# boundary. It is printed in µfn wherever a human reads it.
POLL_INTERVAL_SECONDS = 5


# --------------------------------------------------------------------------
# what an RPC payload may be PRINTED as -- the one place that decides it
# --------------------------------------------------------------------------

# WHY THIS EXISTS, MEASURED 2026-09-25 ON THIS BRANCH.
#
# All three clients' rpc_call() began with
#
#     logger.debug(f"RPC Call Payload: {payload}")
#
# one line BEFORE requests.post, and each module did logger.setLevel(DEBUG) and
# attached a StreamHandler AT IMPORT -- so that line reached stderr with no
# application opt-in at all. Until this branch nothing on the redeem path put a
# secret into a payload: redeem_contract() handed createrawtransaction's output
# (empty scriptSigs) to a signrawtransaction* call, and the daemon did the
# signing. This branch fixed that defect by signing in-process and calling
# `sendrawtransaction` with spend.raw_hex -- whose scriptSig is
# <sig> <pubkey> <PREIMAGE> OP_1 <redeemScript>. Driving the real
# BTCClient.redeem_contract() with requests.post stubbed and no logging config
# printed the 32-byte preimage in full on stderr, inside
#
#     RPC Call Payload: {... 'method': 'sendrawtransaction' ...}
#
# immediately followed by 51 4c5e -- OP_1, OP_PUSHDATA1 94 -- which is the tail
# of the scriptSig and confirms what the bytes are.
#
# THE MONEY PATH, and it is why this is a leak rather than an untidiness: a
# redeem whose broadcast is refused (node down, HTTP 500, relay refusal) puts
# NOTHING on any chain and the preimage in the operator's terminal. This
# repository's stated workflow is that the operator runs commands on their own
# machine and pastes the output back, so that DEBUG block is exactly what gets
# pasted when a redeem fails. A counterparty who reads it takes the other leg
# and then refunds their own when its timelock expires. Both legs, and nothing
# about it can be taken back.
#
# The three clients now log describe_rpc_payload(method, params) instead, and
# the two things that made the leak reach a terminal -- the setLevel and the
# handler -- are gone from all three (modules/utils.py had exactly this removed
# on 2026-09-24 for exactly this reason: a library does not set logging policy
# for its host).

# A raw transaction, as hex, in a parameter. Its scriptSig is the preimage's
# only hiding place on this path.
_RAW_TRANSACTION = "raw tx"
# A private key or a list of them.
_SIGNING_KEY = "signing key"
# The wallet passphrase. Named _WALLET_UNLOCK_PHRASE rather than _PASSPHRASE
# because ruff's S105 reads the NAME and would call this constant a hardcoded
# password -- it is the LABEL printed in place of one. Renaming is the fix;
# a `noqa` here would be a suppression standing in for a two-word rename
# (rule 19). GRCClient.ensure_fully_unlocked() calls
# `walletpassphrase` with self.wallet_passphrase as parameter 0, on EVERY
# create_contract() and EVERY redeem_contract(), so before this the operator's
# wallet passphrase went to stderr at DEBUG as well -- a second secret in the
# same line, found while fixing the first.
_WALLET_UNLOCK_PHRASE = "wallet passphrase"

# method -> {parameter index: what that parameter is}. A method absent from
# this table has its parameters logged verbatim, which is what makes a pasted
# log useful for everything that is not one of these.
#
# The sign* family and importprivkey are listed although THIS repository no
# longer calls them (the redeem signs in-process now, and importprivkey was
# deleted): the table has to be right on the day somebody reaches for one
# again, and a redaction table that only knows today's call sites is the
# duplicate-with-a-delay rule 8 is about.
_SECRET_BEARING_PARAMS: dict[str, dict[int, str]] = {
    "sendrawtransaction": {0: _RAW_TRANSACTION},
    "signrawtransaction": {0: _RAW_TRANSACTION, 2: _SIGNING_KEY},
    "signrawtransactionwithkey": {0: _RAW_TRANSACTION, 1: _SIGNING_KEY},
    "signrawtransactionwithwallet": {0: _RAW_TRANSACTION},
    "decoderawtransaction": {0: _RAW_TRANSACTION},
    "importprivkey": {0: _SIGNING_KEY},
    "walletpassphrase": {0: _WALLET_UNLOCK_PHRASE},
    "walletpassphrasechange": {0: _WALLET_UNLOCK_PHRASE, 1: _WALLET_UNLOCK_PHRASE},
    "encryptwallet": {0: _WALLET_UNLOCK_PHRASE},
}


def _summarize_secret_param(value: object, kind: str) -> str:
    """One bracketed summary standing in for a value that must not be printed.

    It carries the SIZE, because that is what a reader actually needs from a
    raw transaction in a log -- "did the thing I built get sent" is answered by
    321 bytes as well as by 642 hex characters, and only one of the two can
    reveal a preimage. Nothing here echoes any part of the value itself: a
    prefix or a suffix of a 32-byte secret is a 32-byte secret with a head
    start on it.
    """
    if kind is _RAW_TRANSACTION and isinstance(value, str):
        return f"<{kind}, {len(value) // 2} bytes>"
    return f"<{kind}, redacted>"


def describe_rpc_payload(method: str, params: object) -> str:
    """A log-safe rendering of one JSON-RPC call: the method, and its parameters.

    The method is ALWAYS named in full. That is the half of the old line worth
    keeping -- an operator reading a failure needs to know which call failed,
    and no RPC method name is a secret.

    Parameters are printed verbatim unless _SECRET_BEARING_PARAMS says this
    method carries one, in which case that position becomes a bracketed
    summary. So `gettxout` and `createrawtransaction` read exactly as they did,
    and `sendrawtransaction` reads

        sendrawtransaction params=[<raw tx, 321 bytes>]

    A NEGATIVE, stated because it is the thing to check when adding a method:
    this redacts the REQUEST. A response is logged separately by each client,
    and the only response shape in this package that could carry a preimage is
    a verbose read-back of a spend of a hashlock branch -- nothing here does
    that. lookup_contract_output() and read_transaction_outputs() are both
    pointed at the CONTRACT's funding transaction, whose scriptSigs spend the
    operator's own ordinary coins.
    """
    entries = list(params) if isinstance(params, (list, tuple)) else [params]
    redactions = _SECRET_BEARING_PARAMS.get(method, {})
    rendered = [
        _summarize_secret_param(value, redactions[index]) if index in redactions else repr(value)
        for index, value in enumerate(entries)
    ]
    return f"{method} params=[{', '.join(rendered)}]"


@dataclass(frozen=True)
class ContractOutput:
    """One funded contract output, read back from the chain.

    `route` names WHICH of the four lookups answered, because that is the
    difference between "the wallet happens to know this transaction" and "the
    chain says this output exists and is unspent", and an operator reading a
    pasted log needs to be able to tell (rule 14).
    """

    value: Decimal
    script_pubkey_hex: str
    confirmations: int | None
    route: str


def lookup_contract_output(rpc_call, txid: str, vout: int, block_hash: str | None = None) -> ContractOutput:
    """Read a contract output back, on a default node, confirmed or not.

    FOUR ROUTES, TRIED IN THIS ORDER, AND THE ORDER IS THE POINT.

      1. `gettxout txid n true`. The chain's own unspent-output set. It needs no
         index and no wallet, it sees both the mempool and the chain, and it
         answers null for an output that has already been SPENT -- which is a
         far better thing to learn before signing than after broadcasting.
      2. `getrawtransaction txid true <blockhash>`, when the caller knows the
         block. This is the route the daemon's own error message asks for.
      3. `gettransaction txid`, the wallet's own record, whose raw hex is
         handed back to `decoderawtransaction` for the DAEMON to take apart.
      4. `getrawtransaction txid true`, the call this used to be. It still
         works for a transaction in the mempool, or on a node with -txindex.

    ROUTE 3 USED TO PARSE THAT HEX IN PROCESS with
    modules/htlc_spend.parse_transaction(), justified as "one fewer round trip,
    and no dependence on a decoded field's name". Both halves were wrong, and
    the second one was already untrue on the line above it -- routes 2 and 4
    read `scriptPubKey.hex` off a decoded result, so this file depends on a
    decoded field's name whatever route 3 does.

    THE FIRST HALF COST MORE. parse_transaction() cannot read a SEGWIT
    serialization and says so in its own error text. That is fine for the
    SPEND, which is what that parser exists for -- an HTLC P2SH input has no
    witness, so the transaction this module signs has none either. It is not
    fine here: this route reads the FUNDING transaction, which spends the
    operator's own coins, and a default Bitcoin Core 28.1 or Litecoin 0.21.4
    wallet holds those in bech32 P2WPKH. Its hex therefore carries the 0x00
    marker and 0x01 flag, parse_transaction() raises TransactionLayoutError,
    and route 3 is dead on exactly the wallets everybody has.

    The daemon decodes its own serialization by construction, at the cost of
    one round trip, which is not a cost worth a dead route. regtest/steps.py's
    _verbose_tx() has always done it this way; rule 8 says the two should
    agree, and now they do.

    NOT `-txindex=1`. It is the third fix the daemon suggests and the only one
    that changes the operator's machine: enabling it on an existing datadir
    forces a full reindex.

    Raises:
        LookupError: naming every route tried and what each said. It never
            returns a value it is unsure of -- an amount guessed here is the
            amount a signature commits to.
    """
    attempts: list[str] = []

    try:
        entry = rpc_call("gettxout", [txid, vout, True])
    except Exception as exc:  # noqa: BLE001 -- checked: one route failing is not an answer, it is a reason to try the next. Every attempt is collected and re-raised together below, so no route can return a value the caller would mistake for a real output.
        attempts.append(f"gettxout: {exc}")
    else:
        if entry:
            return ContractOutput(
                value=Decimal(str(entry["value"])),
                script_pubkey_hex=entry["scriptPubKey"]["hex"],
                confirmations=_as_int_or_none(entry.get("confirmations")),
                route="gettxout (the chain's unspent-output set)",
            )
        attempts.append("gettxout: null -- no such output, or it has already been spent")

    if block_hash:
        try:
            decoded = rpc_call("getrawtransaction", [txid, True, block_hash])
            return _output_from_decoded(decoded, vout, f"getrawtransaction with block hash {block_hash}")
        except Exception as exc:  # noqa: BLE001 -- checked: same. A wrong or unknown block hash must not end the search.
            attempts.append(f"getrawtransaction with block hash: {exc}")

    try:
        wallet_record = rpc_call("gettransaction", [txid])
        decoded = rpc_call("decoderawtransaction", [wallet_record["hex"]])
        # `decoderawtransaction` carries no confirmation count -- it is handed
        # bytes, not a chain position -- so the wallet record's own count is
        # spliced in. Every caller asserts on it, and losing it here would turn
        # "three confirmations" into "this route does not report them", which
        # _as_int_or_none() is careful to keep as different answers.
        decoded["confirmations"] = wallet_record.get("confirmations")
        return _output_from_decoded(decoded, vout, "gettransaction + decoderawtransaction (the wallet's own record)")
    except Exception as exc:  # noqa: BLE001 -- checked: same. This route only knows transactions the wallet took part in, so its failure is expected for a contract the COUNTERPARTY funded and must not end the search.
        attempts.append(f"gettransaction: {exc}")

    try:
        decoded = rpc_call("getrawtransaction", [txid, True])
        return _output_from_decoded(decoded, vout, "getrawtransaction without a block hash (mempool, or -txindex)")
    except Exception as exc:  # noqa: BLE001 -- checked: the last route. Its failure ends the search and every attempt is named in the LookupError below, so the operator sees which four things were tried rather than only the last.
        attempts.append(f"getrawtransaction without a block hash: {exc}")

    raise LookupError(
        f"could not read contract output {txid}:{vout} back from the node. Tried, in order: "
        + "; ".join(attempts)
        + ". This does NOT add -txindex, because enabling it on an existing datadir forces a reindex."
    )


def _as_int_or_none(raw: object) -> int | None:
    """A confirmation COUNT, or None when the daemon did not supply one.

    A count, never a duration, and so never rendered in microfortnights
    (rule 6). None rather than 0 when it is missing, because zero confirmations
    is a real and different answer from "this route does not report them".
    """
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _output_from_decoded(decoded: dict, vout: int, route: str) -> ContractOutput:
    """Pull one output out of a verbose getrawtransaction result.

    Reads `scriptPubKey.hex` and never `scriptPubKey.addresses`. Measured
    2026-09-25: Core 28.1 returns ['address', 'asm', 'desc', 'hex', 'type'] and
    Litecoin 0.21.4 returns ['addresses', 'asm', 'hex', 'reqSigs', 'type'].
    `addresses` was deprecated in Core 0.20 and removed in 22.0; `hex` is on
    both, is the same bytes on both, and is what a scriptPubKey actually is.
    """
    outputs = decoded.get("vout", [])
    if vout >= len(outputs):
        raise LookupError(f"the transaction has {len(outputs)} output(s), so there is no vout {vout}")
    entry = outputs[vout]
    return ContractOutput(
        value=Decimal(str(entry["value"])),
        script_pubkey_hex=entry["scriptPubKey"]["hex"],
        confirmations=_as_int_or_none(decoded.get("confirmations")),
        route=route,
    )


@dataclass(frozen=True)
class SignedSpend:
    """A signed hashlock spend, and every number the operator needs beside it."""

    raw_hex: str
    script_sig: bytes
    size_bytes: int
    estimated_size_bytes: int
    miner_fee: Decimal
    fee_rate_coin_per_kvb: Decimal
    destination_amount: Decimal

    def describe(self, asset: str) -> str:
        """One self-describing line for a log, echoing what decided the answer."""
        return (
            f"{asset} hashlock spend: {self.size_bytes} bytes (sized from an upper bound of "
            f"{self.estimated_size_bytes}), miner fee {self.miner_fee} = {self.fee_rate_coin_per_kvb} coin/kvB, "
            f"paying {self.destination_amount} to the destination"
        )


def build_hashlock_spend(  # noqa: PLR0913 -- checked: these ten ARE the spend. Nine appear once each in the body and none can be defaulted -- the chain (which picks the fee rule), the RPC, the outpoint and its value, the script being satisfied, the preimage, the key, and where the coins go are independent inputs to one signature. Bundling them into a dataclass would add a type without removing an argument and would move the fund-path decisions away from the call site that makes them.
    *,
    asset: str,
    rpc_call,
    contract_txid: str,
    contract_vout: int,
    contract_value: Decimal,
    redeem_script: bytes,
    secret: bytes,
    wif: str,
    destination_address: str,
    extra_outputs: dict[str, Decimal] | None = None,
) -> SignedSpend:
    """Build, size the fee for, and SIGN a spend of the contract's hashlock branch.

    THE NODE SERIALIZES, THIS SIGNS. `createrawtransaction` turns the addresses
    into output scripts and lays the transaction out in whatever shape its own
    chain uses; modules/htlc_spend.parse_transaction() takes that apart, proves
    it read the layout correctly by reproducing the bytes exactly, and only the
    input's scriptSig is replaced. See that module's header for why this is not
    built from scratch -- addresses on three chains, and Gridcoin's extra nTime
    field.

    Args:
        asset: BTC, LTC or GRC. It selects the fee rule and nothing else.
        rpc_call: the client's own `rpc_call(method, params)`.
        contract_txid, contract_vout: the funded contract output.
        contract_value: its value, in coin, as read back by
            lookup_contract_output(). The fee and the destination amount are
            both derived from it.
        redeem_script: the HTLC script, exactly as the builder produced it.
        secret: the PREIMAGE. It is pushed onto the stack, which is what
            spending the hashlock branch means, and it becomes public when this
            is broadcast. Never log it.
        wif: the participant's private key. Never logged, never returned, and
            never placed in an exception message.
        destination_address: where the redeemed coins go. It absorbs the miner
            fee, so it receives the contract value minus the fee minus any
            extra outputs.
        extra_outputs: fixed amounts paid to other addresses -- the platform
            fee the LTC and GRC clients charge and the BTC one does not. Their
            amounts do NOT move with the miner fee.

    Raises:
        ValueError: if the key does not match the script, if an address is
            named twice, if nothing would be left after fees, if the fee does
            not settle, or if the fee the transaction actually encodes is not
            the fee that was intended. Every one of those refuses BEFORE
            anything is signed or broadcast.
    """
    extras = dict(extra_outputs or {})
    if destination_address in extras:
        raise ValueError(
            f"the destination address {destination_address} is also an extra output. The node would merge the two "
            "into one, and the amounts asserted here would not be the amounts paid. Nothing was built."
        )

    private_key, compressed = decode_wif(wif)
    public_key = public_key_for(private_key, compressed)
    if not participant_key_matches_script(public_key, redeem_script):
        raise ValueError(
            f"the supplied private key's hash160 ({'compressed' if compressed else 'uncompressed'} form) does not "
            "appear in the redeem script, so the spend could not satisfy OP_EQUALVERIFY. On chain this is "
            "`mandatory-script-verify-flag-failed`, which is also what a wrong preimage and a wrong script look "
            "like -- refused here instead, where it can be named. The key itself is not shown."
        )

    extras_total = sum(extras.values(), Decimal(0))
    script_sig_length = estimated_script_sig_length(public_key, secret, redeem_script)

    miner_fee = minimum_fee_coin(asset)
    parsed = None
    estimated_size = 0
    for _ in range(MAX_FEE_PASSES):
        parsed, estimated_size = _unsigned_transaction(
            rpc_call=rpc_call,
            contract_txid=contract_txid,
            contract_vout=contract_vout,
            contract_value=contract_value,
            destination_address=destination_address,
            extras=extras,
            extras_total=extras_total,
            miner_fee=miner_fee,
            script_sig_length=script_sig_length,
        )
        settled = redeem_miner_fee(asset, estimated_size)
        if settled == miner_fee:
            break
        miner_fee = settled
    else:
        raise ValueError(
            f"{asset}: the miner fee did not settle in {MAX_FEE_PASSES} passes over a {estimated_size}-byte "
            "transaction. The fee is derived from the size and the size does not depend on the amounts, so this "
            "cannot happen unless the fee rule is not a function of the size alone. Nothing was signed."
        )

    # THE FEE THE BYTES ACTUALLY ENCODE, not the fee that was intended. The
    # amounts made a round trip through the daemon's JSON as floating point, and
    # the value read back here is the 8-byte integer the transaction carries.
    # If the two ever disagree, the difference went to a miner.
    encoded_fee = contract_value - satoshis_to_coins(parsed.output_total)
    if encoded_fee != miner_fee:
        raise ValueError(
            f"{asset}: the transaction the node built pays a fee of {encoded_fee} but {miner_fee} was intended, a "
            f"difference of {encoded_fee - miner_fee}. That gap would go to a miner. Nothing was signed."
        )

    # EVERY OUTPUT IS LARGE ENOUGH TO RELAY, and this is the last refusal
    # before a signature exists. Checked against the node's OWN layout --
    # parsed.outputs is what createrawtransaction actually built, scriptPubKeys
    # included -- rather than against this function's arithmetic about what it
    # asked for, which is the same reason the encoded-fee check above reads the
    # bytes instead of trusting the intent.
    #
    # Until 2026-09-25 the only amount guard on this path was
    # `destination_amount <= 0` in _unsigned_transaction() below, which never
    # looked at the extra outputs at all. The LTC and GRC platform fee is a
    # fixed 0.25% of the contract, so it shrinks with the contract while a dust
    # limit does not: a 0.01 LTC contract builds [986790, 2500] and Litecoin's
    # P2WPKH dust limit is 2,940. See modules/htlc_fee.py for the measurement
    # and for why this refuses rather than reallocating.
    assert_no_output_is_dust(asset, parsed.outputs)

    digest = legacy_sighash(parsed, 0, redeem_script)
    script_sig = hashlock_script_sig(sign_digest(private_key, digest), public_key, secret, redeem_script)
    raw = parsed.serialize({0: script_sig})
    size_bytes = len(raw)
    if size_bytes > estimated_size:
        raise ValueError(
            f"{asset}: the signed transaction is {size_bytes} bytes but the fee was sized from an upper bound of "
            f"{estimated_size}. The bound is supposed to be exact except for the signature's own length, so this "
            "means the fee was computed for a smaller transaction than the one about to be broadcast. "
            "Nothing was broadcast."
        )
    rate = assert_within_broadcast_ceiling(asset, miner_fee, size_bytes)
    logger.info("%s", describe_fee(asset, miner_fee, size_bytes))
    return SignedSpend(
        raw_hex=raw.hex(),
        script_sig=script_sig,
        size_bytes=size_bytes,
        estimated_size_bytes=estimated_size,
        miner_fee=miner_fee,
        fee_rate_coin_per_kvb=rate,
        destination_amount=contract_value - miner_fee - extras_total,
    )


def _unsigned_transaction(  # noqa: PLR0913 -- checked: one caller, one call site, and every argument is a value that caller already holds. Folding them into an object would hide the fee pass's only variable -- miner_fee -- inside a mutation.
    *,
    rpc_call,
    contract_txid: str,
    contract_vout: int,
    contract_value: Decimal,
    destination_address: str,
    extras: dict[str, Decimal],
    extras_total: Decimal,
    miner_fee: Decimal,
    script_sig_length: int,
):
    """Ask the node to lay out the unsigned spend, and measure what it will weigh.

    Returns (the parsed transaction, the size it will be once signed).
    """
    destination_amount = contract_value - miner_fee - extras_total
    if destination_amount <= 0:
        raise ValueError(
            f"a contract worth {contract_value} cannot cover a miner fee of {miner_fee} plus {extras_total} of "
            "other outputs; nothing would be left to send. Nothing was built."
        )
    outputs = {destination_address: float(destination_amount)}
    for address, amount in extras.items():
        outputs[address] = float(amount)
    inputs = [{"txid": contract_txid, "vout": contract_vout}]
    unsigned_hex = rpc_call("createrawtransaction", [inputs, outputs])
    parsed = parse_transaction(bytes.fromhex(unsigned_hex), contract_txid, contract_vout)
    return parsed, parsed.size_with_script_sig(0, script_sig_length)


def assert_output_pays_the_contract(found: ContractOutput, redeem_script: bytes, label: str) -> None:
    """Refuse to spend an output that is not this contract's.

    The clients used to take the caller's `contract_vout` on trust and sign a
    spend of whatever was at that index. An off-by-one, a reordered funding
    transaction or a stale record would then produce a perfectly valid
    signature over somebody else's output -- which fails on chain with a
    message about scripts, if it fails at all.

    The comparison is on the scriptPubKey HEX, not on a rendered address: the
    two daemons disagree about whether `scriptPubKey.addresses` exists at all,
    and Bitcoin and Litecoin do not even agree on the base58 P2SH version byte.
    """
    expected = p2sh_script_for(redeem_script).hex()
    if found.script_pubkey_hex != expected:
        raise ValueError(
            f"{label}: the output found on chain pays {found.script_pubkey_hex}, and this contract's redeem "
            f"script hashes to {expected}. That is a different output; nothing was signed. It was read via "
            f"{found.route}."
        )


def ensure_watch_only_import(rpc_call, p2sh_address: str, label: str = "HTLC-watch") -> str:
    """Best-effort: make the wallet WATCH the contract address. Never fatal.

    WHAT THIS IS FOR, AND WHAT IT IS NOT FOR. Establishing this was the fix; the
    call was the symptom.

    `BTCClient.create_contract()` called `importaddress` and raised if it
    failed, and on Bitcoin Core 28.1 -- which creates DESCRIPTOR wallets by
    default -- it failed on every run:

        code=-4, Only legacy wallets are supported by this command

    So contract creation was impossible on a default modern node. Measured
    2026-09-25: LTC's create_contract SUCCEEDED on the same run (Litecoin
    0.21.4 still makes legacy wallets), and the harness funded the identical
    P2SH with a plain `sendtoaddress` and then SPENT it -- so the import is not
    a precondition of anything about the contract.

    What in this repository needs the address in the wallet? Grepped the whole
    tree by name, not by import graph: `getreceivedbyaddress` is called only
    from `get_address_balance()`, and every call site of THAT
    (swap_terminal/atomic_swap_gui.py, six of them) passed an operator's own
    validated address, never a contract P2SH. So nothing here needs it -- and
    since that file was DELETED on 2026-09-26 as dead code, get_address_balance()
    now has no caller in this tree at all. The paragraph below is unchanged by
    that and is the reason why: "no caller in this tree" is still not "no
    caller".

    It is kept rather than deleted because "no caller in this tree" is not "no
    caller" (rule 2), and an operator running `listtransactions` or
    `getreceivedbyaddress` against a contract address on their own node is a
    use this cannot see. What changed is that it can no longer stop a swap: it
    is attempted, its outcome is returned and logged, and a failure costs
    wallet visibility and nothing else.

    The companion `importprivkey` call was DELETED rather than made
    conditional. Its only purpose was to let `signrawtransactionwithwallet`
    sign the redeem -- which never worked, cannot work on a conditional script,
    and has been replaced by signing in this process. Importing a signing key
    into a wallet that has no use for it is a liability with no remaining
    consumer.

    IT LOGS THE OUTCOME ITSELF, at the level the outcome deserves, and this
    changed on 2026-09-25. The suppression on the broad catch below claimed the
    failure "is returned in the string the caller logs at WARNING" -- and both
    callers did `logger.info(ensure_watch_only_import(...))`. So a success and
    a failure came out at the SAME level, on the same shape of sentence, which
    is rule 14's "make did-nothing look different from did-work" in the one
    place the function had already decided which it was. Doing it here rather
    than in each caller also stops the level being a third thing the three
    clients could disagree about (rule 8).

    Returns:
        The same sentence, for a caller that wants it in a report. It never
        raises.
    """
    descriptors = _wallet_is_descriptor(rpc_call)
    if descriptors is None:
        skipped = (
            "watch-only import SKIPPED: getwalletinfo did not answer, so the wallet type is unknown and the two "
            "import RPCs are mutually exclusive. The contract is unaffected -- only wallet visibility is."
        )
        logger.warning("%s", skipped)
        return skipped
    try:
        if descriptors:
            info = rpc_call("getdescriptorinfo", [f"addr({p2sh_address})"])
            rpc_call("importdescriptors", [[{
                "desc": info["descriptor"],
                "timestamp": "now",
                "label": label,
                "internal": False,
                "active": False,
            }]])
            succeeded = f"watch-only import OK via importdescriptors (descriptor wallet): {p2sh_address}"
            logger.info("%s", succeeded)
            return succeeded
        rpc_call("importaddress", [p2sh_address, label, False])
    except Exception as exc:  # noqa: BLE001 -- checked: this is the one call in create_contract() whose failure must NOT stop a contract, and the failure is not swallowed -- it is emitted at WARNING on the line below AND returned to the caller. That claim used to be false: both callers logged the returned string at INFO, so a failure and a success came out identically. Nothing downstream reads a value from it, and the contract's correctness does not depend on it (see the docstring's measurement).
        failed = (
            f"watch-only import FAILED on a {'descriptor' if descriptors else 'legacy'} wallet and was not fatal: "
            f"{exc}. The contract is unaffected; the wallet just will not track {p2sh_address}. Note that Bitcoin "
            "Core refuses a watch-only descriptor on a wallet that has private keys enabled, so this failing on a "
            "descriptor wallet is expected rather than alarming."
        )
        logger.warning("%s", failed)
        return failed
    succeeded = f"watch-only import OK via importaddress (legacy wallet): {p2sh_address}"
    logger.info("%s", succeeded)
    return succeeded


def _wallet_is_descriptor(rpc_call) -> bool | None:
    """True, False, or None when the daemon would not say.

    Read from `getwalletinfo.descriptors` AT RUNTIME and never inferred from a
    version string. A version string says what the software could do; this says
    what THIS wallet is, which is the thing `importaddress` refuses on. A wallet
    whose getwalletinfo does not carry the field predates descriptor wallets,
    which is itself the answer: it is legacy.
    """
    try:
        info = rpc_call("getwalletinfo", [])
    except Exception as exc:  # noqa: BLE001 -- checked: returns None rather than a boolean, so the caller can tell "unknown" from "legacy" and skips the import instead of guessing an RPC that would fail on the other wallet type.
        logger.warning("getwalletinfo failed, so the wallet type is unknown: %s", exc)
        return None
    return bool(info.get("descriptors", False))


# --------------------------------------------------------------------------
# waiting for a contract output to appear -- what create_contract() polls
# --------------------------------------------------------------------------


def read_transaction_outputs(rpc_call, txid: str) -> list[tuple[Decimal, str]]:
    """Every output of `txid` as (value in coin, scriptPubKey hex), without -txindex.

    Two routes, for the same reason lookup_contract_output() has four: the
    transaction may be in the mempool, or it may have been mined out of it
    between two polls, and those are answered by different RPCs on a node with
    no transaction index.

      1. `getrawtransaction txid true` -- the mempool, or any transaction at
         all on a node with -txindex.
      2. `gettransaction txid` -- the wallet's own record, whose raw hex is
         handed to `decoderawtransaction`. This is the one that keeps working
         after the funding transaction is confirmed, which is exactly when
         route 1 stops.

    UNTIL 2026-09-25 ROUTE 2 PARSED THAT HEX IN PROCESS, and so could not
    answer for the transaction it exists to answer for. modules/htlc_spend.
    parse_transaction() cannot read a SEGWIT serialization -- its own error
    text says so -- and the funding transaction spends the operator's own
    coins, which on a default Core 28.1 or Litecoin 0.21.4 wallet are bech32
    P2WPKH. Fed one, it raised TransactionLayoutError.

    WHAT THAT COST, and it is the symptom defect 4 was fixed to remove.
    create_contract() broadcasts and then polls through this function. Route 1
    answers while the funding is unconfirmed; the moment a block lands inside
    the poll window route 1 starts returning `code=-5, No such mempool
    transaction` and route 2 could not parse -- so wait_for_tx_output() polled
    to its full 300-SECOND deadline and raised. The coins are already at the
    P2SH and the caller never learns the vout. Two causes, one symptom, and
    only one of them was fixed.

    The daemon decodes its own serialization by construction. regtest/steps.py's
    _verbose_tx() has always done it that way (rule 8: the two should agree).

    Raises:
        LookupError: naming both routes and what each said.
    """
    attempts: list[str] = []
    try:
        decoded = rpc_call("getrawtransaction", [txid, True])
        return [
            (Decimal(str(entry["value"])), entry["scriptPubKey"]["hex"])
            for entry in decoded.get("vout", [])
        ]
    except Exception as exc:  # noqa: BLE001 -- checked: one route failing is a reason to try the next, not an answer. Both attempts are named in the LookupError below and neither can return a value a caller would mistake for a real transaction.
        attempts.append(f"getrawtransaction: {exc}")
    try:
        wallet_record = rpc_call("gettransaction", [txid])
        decoded = rpc_call("decoderawtransaction", [wallet_record["hex"]])
        return [
            (Decimal(str(entry["value"])), entry["scriptPubKey"]["hex"])
            for entry in decoded.get("vout", [])
        ]
    except Exception as exc:  # noqa: BLE001 -- checked: the last route; its failure ends the search and is reported with the first one's.
        attempts.append(f"gettransaction: {exc}")
    raise LookupError(f"could not read transaction {txid}: " + "; ".join(attempts))


def find_output_by_script(outputs: list[tuple[Decimal, str]], script_hex: str) -> int | None:
    """The index of the output paying `script_hex`, or None.

    MATCHED ON THE SCRIPT, NEVER ON AN ADDRESS, and this is defect four.
    Measured 2026-09-25 on real daemons:

        Bitcoin Core 28.1.0    scriptPubKey keys: ['address', 'asm', 'desc', 'hex', 'type']
        Litecoin Core 0.21.4   scriptPubKey keys: ['addresses', 'asm', 'hex', 'reqSigs', 'type']

    `addresses` (plural) was deprecated in Core 0.20 and removed in 22.0.
    Litecoin 0.21.4 still returns it. The old code compared its P2SH address
    against `scriptPubKey.addresses` and so found NOTHING on Bitcoin -- the
    poll ran to its 300-second deadline and timed out on a contract that had
    been funded perfectly. Litecoin's copy worked only because its daemon is
    four years behind, and would break the day it is upgraded.

    There is a second copy of this search in regtest/steps.py::_find_vout_by_script,
    which is the harness's own and is kept separate on purpose (rule 8 requires
    each to name the other; see modules/htlc_spend.py's header for why the
    harness does not import the code it measures).
    """
    for index, (_value, candidate) in enumerate(outputs):
        if candidate == script_hex:
            return index
    return None


# address_of() MOVED to swap_terminal/script_pub_key.py on 2026-09-25, and the
# move is the point rather than the tidying. It had NO production caller here:
# every comparison in this module is on the scriptPubKey HEX, so the one thing
# that knew how the two daemons spell an address was reachable only from a
# test. Meanwhile chains/base.RPCAdapter._extract_matching_vouts() -- the
# brokered Flask path -- read `addresses` alone and so was blind on Core 28.1,
# a FOURTH copy of the defect the three clients were fixed for.
#
# It could not simply be imported from here: this module imports
# modules/htlc_spend, which imports ecdsa, base58 and bech32, and
# swap_terminal/requirements.txt records that the Flask app is deployable
# without them. So the decision went to a leaf at the package root with no
# third-party imports, beside microfortnights.py, where both suites reach it
# without either dragging the other in (rule 8: let the survivor own the
# concept, somewhere both callers can reach).

def wait_for_tx_output(
    rpc_client,
    txid: str,
    expected_script_hex: str,
    max_wait: int = 60,
    expected_address: str = "",
) -> tuple[int, list[tuple[Decimal, str]]]:
    """Poll the node until an output of `txid` pays `expected_script_hex`.

    MOVED HERE FROM modules/utils.py ON 2026-09-25, with the defect fixed on
    the way. utils.py is the function level -- secret generation and hashing,
    nothing that opens a socket -- and a polling loop that holds an RPC
    conversation belongs one layer up (rule 10). It also could not be fixed
    where it was: the fix needs a transaction parser, and utils.py is imported
    BY the module that has one, so importing it back would be a cycle.

    Args:
        rpc_client: anything with an `rpc_call(method, params)` method.
        txid: the transaction to inspect.
        expected_script_hex: the scriptPubKey to look for, as hex. For an HTLC
            that is `modules.atomic_htlc_scripts.p2sh_script_for(redeem_script).hex()`.
            NOT an address -- see find_output_by_script() for the measurement.
        max_wait: SECONDS to keep polling. Seconds, not microfortnights: it is
            compared against time.monotonic() and passed to time.sleep(), which
            is an interface, not a report (rule 6). The µfn figure appears in
            the log lines, where a human reads it.
        expected_address: for the log lines only. The match never uses it.

    The old `interval` parameter is gone. It was never passed by either of the
    two call sites in this repository -- BTCClient.create_contract() sets
    max_wait=300 and GRCClient.create_contract() takes the defaults -- so it
    was a knob nobody turned, and rule 9 asks for less of the file to be left
    behind each time. POLL_INTERVAL_SECONDS below is the value it always had.

    Returns:
        (vout index, every output as (value in coin, scriptPubKey hex)).

    Raises:
        TimeoutError: carrying the count of RPC errors seen while waiting,
            because "the chain has not included it yet" and "the daemon has
            been refusing us for a minute" produce the same silence and must
            not produce the same message (rule 14).
    """
    started = time.monotonic()
    deadline = started + max_wait
    attempts = 0
    rpc_errors = 0
    last_error = ""
    logger.info(
        "waiting for an output of %s paying scriptPubKey %s%s; giving up after %s",
        txid,
        expected_script_hex,
        f" (address {expected_address})" if expected_address else "",
        format_duration(max_wait),
    )

    while time.monotonic() < deadline:
        attempts += 1
        elapsed = time.monotonic() - started
        try:
            outputs = read_transaction_outputs(rpc_client.rpc_call, txid)
            index = find_output_by_script(outputs, expected_script_hex)
            if index is not None:
                logger.info(
                    "found the output at index %d after %s (%d poll(s), %d rpc error(s))",
                    index,
                    format_duration(time.monotonic() - started),
                    attempts,
                    rpc_errors,
                )
                return index, outputs
            logger.info(
                "poll %d: %d output(s), none paying this contract yet, %s elapsed of %s, rpc_errors=%d",
                attempts,
                len(outputs),
                format_duration(elapsed),
                format_duration(max_wait),
                rpc_errors,
            )
        except Exception as exc:  # noqa: BLE001 -- checked: a transient RPC failure must not abort a wait that is otherwise going fine, but it is NOT swallowed: it is counted, logged at WARNING on every occurrence, and carried into the TimeoutError so the caller can tell a quiet chain from a broken daemon.
            rpc_errors += 1
            last_error = str(exc)
            logger.warning(
                "poll %d: rpc error after %s (%d of %d polls have failed): %s",
                attempts,
                format_duration(elapsed),
                rpc_errors,
                attempts,
                exc,
            )

        time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))

    waited = time.monotonic() - started
    detail = f"; {rpc_errors} of {attempts} polls raised, last: {last_error}" if rpc_errors else "; no rpc errors"
    logger.error("gave up on %s after %s%s", txid, format_duration(waited), detail)
    raise TimeoutError(
        f"output for txid {txid} paying scriptPubKey {expected_script_hex} did not appear within "
        f"{format_duration(waited)}{detail}"
    )
