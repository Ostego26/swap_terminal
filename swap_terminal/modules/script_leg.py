"""Fund and claim an HTLC on a script chain. ONE implementation, three chains.

Role: submodule (it composes the chain clients; the decisions are theirs and keys.py's)
Reads: nothing from disk. The chain, through the client it is handed.
Writes: nothing. It SENDS a funding transaction and a claim, when called.
Can move funds: YES, on the script chain. Testnet only, by the clients' own refusals.
Live-safe: no -- both calls move coins. Neither happens without a caller asking.

WHY THIS EXISTS, MEASURED 2026-09-29. There were TWO implementations of "fund an HTLC on
a script chain" in this tree and they had different reach:

    atomic_swap.py          -> BTCClient/LTCClient/GRCClient.create_contract(), which
                               build the P2SH themselves. Works on all three chains.
    atomic_swap_xrp.py      -> adapter.call("createhtlc", ...), a GRIDCOIN RPC. bitcoind
                               and litecoind answer "Method not found".

That is rule 8's defect with the drift already arrived: the copies did not merely risk
disagreeing, one of them covered a third of what the other did. It was found by RUNNING
`--chain btc`, which passed the adapter check, the network check, the addresses and the
commitment before anything suggested a problem -- and would have failed at step 6, AFTER
the XRP escrow was funded at step 5.

THE KEYS ARE MINTED HERE AND NEVER LEAVE, and that is the other reason this is a module
rather than four lines at each call site. `redeem_contract()` signs with a WIF rather than
asking the wallet, so the claim needs a private key -- and the obvious way to get one is
`dumpprivkey`, which CLAUDE.md's chain-safety rules forbid ("never move, copy or read back
a key") and which Bitcoin Core refuses on a descriptor wallet anyway. atomic_swap.py
already solved this by generating throwaway keypairs in-process; this does the same, reusing
regtest/keys.generate_key() rather than re-spelling it.

The minted keys control NOTHING but the contract branches built around them seconds later.
They are never printed, never logged, never written, and never returned as text -- the
dataclass below holds them and the call sites take addresses off it.

TWO KEYS AND NOT ONE, for the reason atomic_swap.py's mint_parties() gives at length:
build_htlc_redeem_script() refuses a script whose two branches hash to the same key, a
guard added after a contract was built unclaimable. Two distinct keys means that state
cannot be constructed here at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from modules.htlc_contract_api import create_contract_kwargs
from regtest.keys import RegtestKey, generate_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScriptLegKeys:
    """The two throwaway keypairs one HTLC needs: who may claim, and who may refund.

    NEVER PRINTED AND NEVER FORMATTED. There is deliberately no __str__ or __repr__
    override that would make either safe, because an override is a promise every future
    field has to keep. The rule is at the call sites: take `.claim.address` and
    `.refund.address`, never the objects.
    """

    # RegtestKey, NOT `object`, WHICH IS WHAT THESE SAID UNTIL 2026-10-09. generate_key() is
    # already imported from the same module, so naming the type it returns costs one word and
    # buys the three attribute reads below -- `.address` twice and `.wif` once -- being checked
    # at all. Under `object` none of them was: a checker reports `address` as unknown, which is
    # three findings, and a RENAME of any of those attributes in regtest/keys.py would have
    # been invisible here until the claim failed on a funded contract.
    claim: RegtestKey
    refund: RegtestKey

    @property
    def claim_address(self) -> str:
        return self.claim.address

    @property
    def refund_address(self) -> str:
        return self.refund.address


def mint_leg_keys() -> ScriptLegKeys:
    """Two fresh keypairs, generated in this process, valid on all three testnets.

    regtest/keys.generate_key()'s P2PKH version byte 0x6F is correct for Bitcoin,
    Litecoin and Gridcoin testnet alike, which is what makes it chain-generic despite the
    `regtest` in its module name -- atomic_swap.py records the same reuse and the same
    reason. The name is the thing that is wrong there, not the reuse.
    """
    claim, refund = generate_key(), generate_key()
    if claim.address == refund.address:
        # Cannot happen with two independent 256-bit scalars, and is checked anyway
        # because the failure it would cause is an UNCLAIMABLE contract with coins in it.
        raise ValueError("the two minted keys collided; refusing to build an HTLC whose "
                         "branches hash to one key")
    return ScriptLegKeys(claim=claim, refund=refund)


def fund_the_script_leg(client, amount: Decimal, secret_hash: str, keys: ScriptLegKeys,  # noqa: PLR0913 -- checked: same judgment as htlc_contract_api.create_contract_kwargs, which this hands straight to. Six facts, none derivable from another, and `chain` is what selects the amount keyword. PLR0917 does not fire because `chain` is keyword-only.
                        locktime: int, *, chain: str) -> dict:
    """Build the P2SH HTLC, fund it, and return what the claim will need.

    Returns the client's own dict -- {txid, vout, redeemScript, p2shAddress} -- rather
    than a reshaped one, so a reader comparing this against atomic_swap.py sees the same
    keys. Reshaping it here would be a second vocabulary for one thing.

    THE CLAIM BRANCH IS keys.claim AND THE REFUND BRANCH IS keys.refund, and getting that
    backwards builds a contract the wrong party can take. It is passed positionally in the
    clients' own order (participant_address, refund_address) and named here so the two can
    be read against each other.
    """
    logger.info("funding a script-chain HTLC for %s, locktime %d", amount, locktime)
    # BY KEYWORD, THROUGH THE ONE TABLE. This call was positional until 2026-09-29 and
    # was correct for two of the three chains this module advertises: LTCClient takes
    # its parameters in a different ORDER with secret_hash LAST and defaulting to None,
    # so the positional form handed it the secret hash as a participant address and the
    # refund address as a locktime. It failed with
    #
    #     TypeError: '<=' not supported between instances of 'str' and 'int'
    #
    # on a --run that had already funded the XRP leg -- and the type mismatch is the
    # ONLY thing that stopped it. Every other misrouted argument was a string landing
    # where a string was expected, so with luckier types this would have funded an HTLC
    # with no hashlock at all.
    return client.create_contract(**create_contract_kwargs(
        chain,
        amount=amount,
        secret_hash=secret_hash,
        participant_address=keys.claim_address,
        refund_address=keys.refund_address,
        locktime=locktime,
    ))


def claim_the_script_leg(client, contract: dict, secret: bytes, keys: ScriptLegKeys,
                         destination: str) -> str:
    """Spend the hashlock branch by revealing the preimage. Returns the claim txid.

    THIS IS WHAT PUBLISHES THE SECRET, which is the entire mechanism: the preimage lands
    in a scriptSig in a block, and the counterparty reads it back off the chain to finish
    the other leg. A claim that succeeded without publishing would be a swap with one
    party able to take both sides.

    `redeemScript` comes back from fund_the_script_leg as bytes from the clients; it is
    passed through unchanged rather than re-derived, because re-deriving it would mean
    rebuilding the script from the same inputs and comparing nothing -- the value that
    matters is the one the funded output actually commits to.
    """
    return client.redeem_contract(
        contract["txid"],
        int(contract["vout"]),
        contract["redeemScript"],
        secret,
        keys.claim.wif,
        destination,
    )
